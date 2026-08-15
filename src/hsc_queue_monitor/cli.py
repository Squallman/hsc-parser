"""HSC Parser command-line interface.

The two primary production commands are separated by a security boundary:

  refresh-session   Local only: open Chromium, authenticate through ID.GOV.UA's
                    electronic-signature flow, persist the session encrypted to
                    MongoDB, close the browser.
                    Needs: IDGOV_SIGNING_KEY_PATH, IDGOV_SIGNING_KEY_PASSWORD,
                            HSC_MONGODB_URI, HSC_SESSION_ENCRYPTION_KEY

  monitor-once      Headless: load the encrypted session from MongoDB, read
                    service-centre availability through the HSC read-only API
                    once, update the persisted session, detect changes against
                    the last snapshot, notify over Telegram if configured.
                    No browser, no signing key, no authentication.
                    Needs: HSC_MONGODB_URI, HSC_SESSION_ENCRYPTION_KEY
                    (optional: TELEGRAM_BOT_TOKEN, TELEGRAM_USERS)

Helper commands:

  init-config       Discover the HSC service-centre catalogue through the
                    persisted session. Uses MongoDB like monitor-once.

  telegram-test     Verify that Telegram notifications can reach all
                    configured recipients.

Diagnostic commands are listed in the full help. They are useful for development
and debugging, but not part of the normal operating procedure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from .api.availability import (
    ApiSchemaUnknown,
    render_api_availability,
    render_schema_stop,
    scan_centre,
)
from .api.availability_snapshot import (
    AvailabilitySnapshotStore,
    MongoAvailabilitySnapshotStore,
    NullAvailabilitySnapshotStore,
)
from .api.bootstrap import QueueBootstrap, wizard_fingerprint
from .api.client import LABEL_DEPARTMENTS, ApiRequestFailed, HscApiClient, client_for
from .api.config_init import run_config_init
from .api.endpoints import MEASURED_REQUESTS, require_read_only
from .api.headless_monitor import (
    EXIT_OK,
    EXIT_PERSISTENCE,
    run_headless_scan,
)
from .api.monitor import ApiMonitor, ApiSession, ApiSessionProvider
from .api.monitor_state import (
    MongoMonitorStateStore,
    MonitorStateStore,
    MonitorStatus,
    NullMonitorStateStore,
    record,
)
from .api.observer import ApiObserver
from .api.probe import (
    DEFAULT_ITEMS,
    KIND_NO_CONTENT,
    WIZARD_COOKIE,
    Fetch,
    ProbeOutcome,
    build_session,
    describe_cookies,
    hsc_cookies,
    http_get,
    perform,
    read_browser_cookies,
    read_user_agent,
    render_outcome,
    resolve_url,
)
from .api.session_store import (
    MongoSessionStore,
    PersistedSession,
    SessionCipher,
    SessionStore,
    cookies_from_jar,
    queue_expiry,
    session_from_cookies,
)
from .browser.diagnostics import Diagnostics, format_element
from .browser.manager import BrowserManager
from .browser.native_files import AccessibilityInspector, summarize_hierarchy
from .config import (
    ADVISED_MIN_MONITOR_INTERVAL_SECONDS,
    AppConfig,
    centers_to_scan,
    find_service_center,
    validate_monitor_interval,
    validate_slot_interval,
)
from .flow.availability import AvailabilityScanner, render_availability
from .flow.engine import FlowEngine, prompt_async
from .flow.steps import STEP_REGISTRY, FlowContext
from .logging_config import setup_logging
from .models import (
    ApiProbeError,
    ConfigError,
    DepartmentAvailability,
    HscMonitorError,
    LocatorAmbiguous,
    LocatorNotFound,
    SelectorNotConfigured,
    ServiceCenter,
)
from .monitor.monitor import Monitor
from .monitor.state import StateStore
from .notification.base import Notifier
from .notification.console import ConsoleNotifier
from .notification.telegram import TelegramNotifier

# Aliased: the browser `monitor` command has its own, older notifier stack in
# `notification/` (singular), and it is deliberately left alone.
from .notifications.base import Notifier as OutboundNotifier
from .notifications.dispatcher import NotificationDispatcher
from .notifications.selftest import send_test_message
from .notifications.telegram import TelegramNotifier as OutboundTelegramNotifier
from .pages.base_page import build_locator
from .pages.department_page import DepartmentPage
from .pages.login_page import LoginPage

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Session helper
# --------------------------------------------------------------------------- #


def load_config(args: argparse.Namespace) -> AppConfig:
    """Committed configuration, plus the secrets this process was given.

    The redactor is armed here, once, with every secret value — including each
    Telegram recipient id, because those name people.
    """
    config = AppConfig.load(
        config_dir=Path(args.config_dir) if args.config_dir else None,
        data_dir=Path(args.data_dir) if args.data_dir else None,
        headless=args.headless,
    )
    setup_logging(verbose=args.verbose, secrets=tuple(config.secrets.redactable()))
    return config


@asynccontextmanager
async def app_session(
    args: argparse.Namespace, *, debug_subdir: str | None = None
) -> AsyncIterator[tuple[AppConfig, FlowContext]]:
    """Open the persistent browser and build the page objects."""
    config = load_config(args)
    paths = config.paths
    debug_dir = paths.debug_dir if debug_subdir is None else paths.debug_dir / debug_subdir
    diagnostics = Diagnostics(debug_dir, enabled=True)

    async with BrowserManager(
        paths.profile_dir,
        headless=config.app.headless,
        default_timeout=config.flow.timeouts.default_locator,
    ) as browser:
        page = await browser.page()
        ctx = FlowContext(config=config, page=page, diagnostics=diagnostics)
        yield config, ctx


async def _navigate_home(ctx: FlowContext) -> None:
    """Land on the queue page when the browser has just opened."""
    if ctx.page.url in {"about:blank", ""}:
        await ctx.queue.open()


# --------------------------------------------------------------------------- #
# Commands that need no browser
# --------------------------------------------------------------------------- #


def cmd_selectors(args: argparse.Namespace) -> int:
    config = load_config(args)
    todo = config.selectors.todo_keys()
    done = config.selectors.configured_keys()

    print(f"\nConfigured ({len(done)}):")
    for key in done:
        spec = config.selectors.get(key)
        note = "  (value supplied at runtime)" if spec.is_dynamic else ""
        print(f"  ✓ {key:38s} {spec.describe()}{note}")

    print(f"\nStill TODO ({len(todo)}):")
    for key in todo:
        optional = " (optional)" if config.selectors.get(key).optional else ""
        print(f"  ✗ {key}{optional}")

    print("\nCentres:")
    for center in config.service_centers:
        mark = "✓" if center.enabled else "·"
        print(f"  {mark} {center.id:6s} {center.name}")
    print()
    return 0


def cmd_steps(args: argparse.Namespace) -> int:
    config = load_config(args)
    print("\nAvailable steps:")
    for name, step in STEP_REGISTRY.items():
        print(f"  {name:22s} {step.description:42s} {step.selector_key or ''}")
    print("\nConfigured flow (flow.yaml):")
    print("  open_queue")
    if config.flow.login_enabled:
        print("  login")
    for name in config.flow.queue_steps:
        print(f"  {name}")
    print()
    return 0


# --------------------------------------------------------------------------- #
# Browser commands
# --------------------------------------------------------------------------- #


async def cmd_inspect(args: argparse.Namespace) -> int:
    """Manual selector discovery.

    Two modes share this implementation. Plain ``inspect`` overwrites one
    ``page-elements.json`` per dump, which is what you want while iterating on
    a single screen. ``inspect-auth`` numbers every capture instead, because
    the authentication journey is a sequence of screens you cannot easily get
    back to once you have moved on.
    """
    subdir = getattr(args, "debug_subdir", None)
    numbered = getattr(args, "numbered", False)

    async with app_session(args, debug_subdir=subdir) as (config, ctx):
        # The authentication journey starts on the signed-out home page. The
        # cabinet would only bounce back to it once the session has expired,
        # and would hide the login screens while it has not.
        start_url = args.url or (config.flow.base_url if numbered else None)
        if start_url:
            await ctx.page.goto(start_url, wait_until="domcontentloaded")
        else:
            await _navigate_home(ctx)

        print(
            f"\nInspect mode{' (authentication screens)' if numbered else ''}.\n"
            "  Navigate manually in the Chromium window.\n"
            "  Press ENTER here to dump the visible interactive elements.\n"
            + (
                "  Type a label first to name the capture, e.g. "
                "`idgov-method` then ENTER.\n"
                if numbered
                else ""
            )
            + '  Type "q" then ENTER to quit.\n'
        )

        assert ctx.diagnostics is not None
        while True:
            answer = (await prompt_async("inspect> ")).strip()
            if answer.lower() in {"q", "quit", "exit"}:
                break

            print(f"URL: {ctx.page.url}")
            if numbered:
                shot, path = await ctx.diagnostics.capture_snapshot(
                    ctx.page, answer or "auth"
                )
            else:
                shot = (
                    await ctx.diagnostics.screenshot(ctx.page, "inspect")
                    if args.screenshot
                    else None
                )
                path = await ctx.diagnostics.dump_elements(ctx.page, "page-elements")
            if path is None:
                continue

            elements = await _preview_elements(ctx)
            for element in elements[: args.limit]:
                print(f"  {format_element(element)}")
            if len(elements) > args.limit:
                print(f"  … {len(elements) - args.limit} more")
            print(f"Elements:   {_display_path(path)}")
            if shot is not None:
                print(f"Screenshot: {_display_path(shot)}")
    return 0


async def _preview_elements(ctx: FlowContext) -> list[dict[str, object]]:
    from .browser.diagnostics import collect_interactive_elements

    return await collect_interactive_elements(ctx.page)


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


async def cmd_auth_status(args: argparse.Namespace) -> int:
    """Report whether the persistent profile still has a live session.

    Diagnostic only: it opens the cabinet and looks, and never logs in. Use
    ``ensure-auth`` when you want it fixed.
    """
    async with app_session(args) as (config, ctx):
        await ctx.queue.open()
        authenticated = await ctx.auth.is_authenticated()

        print("\nHSC AUTH STATUS\n")
        if authenticated:
            print("Authenticated: YES")
            print(f"URL: {ctx.page.url}")
        else:
            print("Authenticated: NO")
            print(f"Current URL: {ctx.page.url}")
            print("\nRun `ensure-auth` to sign in, or just run the command you "
                  "actually wanted — it recovers the session on its own.")
        print()
    # An expired session is an observation, not a failure of this command.
    return EXIT_OK


async def cmd_ensure_auth(args: argparse.Namespace) -> int:
    """Run only the authentication guard, then stop on ``/cabinet``."""
    async with app_session(args) as (config, ctx):
        try:
            await ctx.auth.ensure_authenticated()
        except (SelectorNotConfigured, ConfigError) as exc:
            return _config_error(exc)
        except HscMonitorError as exc:
            print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
            return EXIT_RUNTIME

        print("\nHSC AUTH STATUS\n")
        print("Authenticated: YES")
        print(f"URL: {ctx.page.url}")
        print("\nStopped after reaching the cabinet. Nothing else was clicked.\n")
    return EXIT_OK


async def run_ensure_auth_debug_provider(
    config: AppConfig,
    ctx: FlowContext,
    *,
    prompt: Callable[[str], Awaitable[str]] = prompt_async,
) -> int:
    """One controlled experiment: does the КНЕДП have to be chosen by hand?

    Everything is automated except the provider dropdown, which the operator
    works through the page's own UI. That is the single variable — the same
    key, the same password, the same submit, the same observer. If this run
    reaches signer information and ``ensure-auth`` does not, the wrapper's
    click handlers are the difference; if it resets the same way, the provider
    is not the cause and the search moves on.
    """
    provider = config.flow.authentication.require_key_provider()

    print("\nPROVIDER A/B DIAGNOSTIC\n")
    print("This run is identical to `ensure-auth` except for one thing:")
    print("  select_key_provider() is NOT called — you pick the provider.")
    print(f"\nProvider to select: {provider}")
    print("\nThe browser window will stop on the ID.GOV.UA file-key screen.\n")

    try:
        await ctx.auth.ensure_authenticated(manual_provider=prompt)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        print(
            "The manual provider selection did NOT change the outcome. "
            "Compare this failure with the one from `ensure-auth`: if they "
            "match, the dropdown wrapper is not the cause.\n",
            file=sys.stderr,
        )
        return EXIT_RUNTIME

    print("\nHSC AUTH STATUS\n")
    print("Authenticated: YES")
    print(f"URL: {ctx.page.url}")
    print(
        "\nThe journey completed with the provider chosen by hand. That is the "
        "answer: the native <select> value alone is not enough, and "
        "select_key_provider() has to drive the visible dropdown instead.\n"
    )
    return EXIT_OK


async def cmd_ensure_auth_debug_provider(args: argparse.Namespace) -> int:
    async with app_session(args) as (config, ctx):
        return await run_ensure_auth_debug_provider(config, ctx)


async def run_ensure_auth_debug_password(
    config: AppConfig,
    ctx: FlowContext,
    *,
    prompt: Callable[[str], Awaitable[str]] = prompt_async,
) -> int:
    """One controlled experiment: does the password have to be *typed*?

    The provider run came back identical, so that variable is spent. This one
    changes only how the password reaches the field: ``IDGOV_SIGNING_KEY_PASSWORD`` is
    never filled and ``fill(secret=True)`` is never called — you type it, and
    everything else (key, provider, submit, observers) stays automated.

    The value is never read back. The field is checked for presence with a
    browser-side boolean, so this process never holds the password at all.
    """
    print("\nPASSWORD A/B DIAGNOSTIC\n")
    print("This run is identical to `ensure-auth` except for one thing:")
    print("  the password is typed by you, not filled by Playwright.")
    print("\nIDGOV_SIGNING_KEY_PASSWORD is not entered into the page by this command.")
    print("Nothing you type is read, logged or saved — only whether the field")
    print("is empty, decided inside the browser.\n")

    try:
        await ctx.auth.ensure_authenticated(manual_password=prompt)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        print(
            "A hand-typed password did NOT change the outcome. Compare this "
            "failure with the `ensure-auth` one: if they match, how the "
            "password is entered is not the cause either.\n",
            file=sys.stderr,
        )
        return EXIT_RUNTIME

    print("\nHSC AUTH STATUS\n")
    print("Authenticated: YES")
    print(f"URL: {ctx.page.url}")
    print(
        "\nThe journey completed with the password typed by hand. That is the "
        "answer: fill() is not delivering it the way the page expects, and the "
        "production step needs to type into the field instead.\n"
    )
    return EXIT_OK


async def cmd_ensure_auth_debug_password(args: argparse.Namespace) -> int:
    async with app_session(args) as (config, ctx):
        return await run_ensure_auth_debug_password(config, ctx)


async def run_ensure_auth_debug_native_ax(config: AppConfig, ctx: FlowContext) -> int:
    """Open the real macOS dialog and dump what is actually in it.

    Two heuristics have now failed against this dialog — the focused element is
    reported as nothing, and the roles we expected are not where we expected
    them. So this stops guessing: it walks the process's real accessibility
    tree, assuming no sheet index, no child index and no role, and writes down
    what it finds.

    It selects no file. Nothing is typed, nothing is pasted, Return is never
    pressed, and the dialog is deliberately left open so the state in the
    artifact is the state on screen.
    """
    selector = ctx.login.file_selector
    if not isinstance(selector, AccessibilityInspector):
        print(
            "\nThis diagnostic needs the native macOS file selector.\n"
            f"authentication.file_selection is "
            f"{config.flow.authentication.file_selection!r} and this platform "
            "has no accessibility inspector.\n",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    provider = config.flow.authentication.require_key_provider()
    login = ctx.login

    print("\nNATIVE ACCESSIBILITY DIAGNOSTIC\n")
    print("Opens the real macOS file dialog and records its hierarchy.")
    print("No file is selected, nothing is typed, Return is never pressed.\n")

    try:
        # The journey up to the dialog, using the ordinary steps.
        await ctx.queue.open()
        if await ctx.auth.is_authenticated():
            print(
                "The session is already authenticated, so the key-file screen "
                "is not reachable.\nSign out (or clear data/browser-profile) "
                "and run this again.\n",
                file=sys.stderr,
            )
            return EXIT_RUNTIME

        await login.accept_service_terms()
        await login.go_to_idgov()
        await login.select_electronic_signature()
        await login.select_file_key()
        await login.select_key_provider(provider)

        logger.info("Opening ID.GOV.UA key file chooser")
        await login.click(LoginPage.KEY_FILE_TRIGGER, step="login.key_file_trigger")

        await selector.wait_for_dialog()
        await selector.send_goto_shortcut()
        logger.info("Sent ⌘⇧G; reading the accessibility hierarchy")

        elements = await selector.describe_hierarchy()
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME

    summary = summarize_hierarchy(elements)
    path = None
    if ctx.diagnostics is not None:
        path = ctx.diagnostics.write_artifact(
            "native-ax",
            {
                "process": config.flow.authentication.browser_process,
                "note": (
                    "Accessibility metadata only. No page content (AXWebArea "
                    "subtrees are not descended into), no secure field values."
                ),
                "summary": summary,
                "elements": elements,
            },
        )

    print(f"windows: {summary['windows']}")
    print(f"elements inspected: {summary['elements']}")
    print(f"deepest level reached: {summary['max_depth_seen']}")
    print("roles found:")
    for role, count in summary["roles"].items():
        print(f"  {role}: {count}")
    print(f"\nWritten to: {path}" if path else "\nNo artifact was written.")
    print(
        "\nThe macOS dialog has been left open on purpose — nothing was "
        "selected.\nClose it by hand when you have sent the file.\n"
    )
    return EXIT_OK


async def run_ensure_auth_debug_native_file_only(
    config: AppConfig,
    ctx: FlowContext,
    *,
    prompt: Callable[[str], Awaitable[str]] = prompt_async,
) -> int:
    """Do the native key selection and then stop, before the password.

    The A/B this settles: the site now shows the key as loaded, and only
    afterwards does the attempt reset. That leaves two candidates — the file
    arriving through the native panel, or the password and submit that follow —
    and they can only be told apart by doing one without the other.

    So this runs the production path up to and including the key being loaded,
    using the same LoginPage and the same MacOSFileSelector, and hands the
    browser over. If a human then types the password and gets signer
    information, the automated file selection is exonerated and the problem is
    downstream; if it resets anyway, the reverse.

    The password is not read, not filled, and not submitted.
    """
    provider = config.flow.authentication.require_key_provider()
    key_path = config.secrets.require_key_path()
    login = ctx.login
    timeouts = config.flow.timeouts

    print("\nNATIVE FILE SELECTION A/B DIAGNOSTIC\n")
    print("Runs the production journey as far as the key being loaded, then stops.")
    print("The password is never read, typed or submitted by this command.\n")

    try:
        await ctx.queue.open()
        if await ctx.auth.is_authenticated():
            print(
                "The session is already authenticated, so the key-file screen "
                "is not reachable.\nSign out (or clear data/browser-profile) "
                "and run this again.\n",
                file=sys.stderr,
            )
            return EXIT_RUNTIME

        # Production steps, unaltered — that is the point of the experiment.
        await login.accept_service_terms()
        await login.go_to_idgov()
        await login.select_electronic_signature()
        await login.select_file_key()
        await login.select_key_provider(provider)
        await login.choose_key_file(key_path)
        await login.wait_for_key_loaded(timeouts.authentication)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME

    print("\nNative key selection completed.")
    print(f"Key loaded in ID.GOV.UA: {key_path.name}\n")
    print("Now manually:")
    print("  1. enter the key password;")
    print('  2. click "Продовжити";')
    print("  3. observe whether signer information appears or the form resets.\n")

    # The browser closes when this returns, so it does not return until the
    # experiment is over.
    await prompt("Press ENTER here when you have finished, to close the browser: ")
    return EXIT_OK


async def cmd_ensure_auth_debug_native_file_only(args: argparse.Namespace) -> int:
    async with app_session(args) as (config, ctx):
        return await run_ensure_auth_debug_native_file_only(config, ctx)


async def cmd_ensure_auth_debug_native_ax(args: argparse.Namespace) -> int:
    async with app_session(args) as (config, ctx):
        return await run_ensure_auth_debug_native_ax(config, ctx)


async def cmd_screenshot(args: argparse.Namespace) -> int:
    async with app_session(args) as (config, ctx):
        if args.url:
            await ctx.page.goto(args.url, wait_until="domcontentloaded")
        else:
            await _navigate_home(ctx)

        if args.wait:
            await prompt_async("Navigate to the screen you want, then press ENTER: ")

        assert ctx.diagnostics is not None
        await ctx.queue.wait_stable()
        print(f"URL: {ctx.page.url}")
        await ctx.diagnostics.screenshot(ctx.page, args.name)
        await ctx.diagnostics.dump_elements(ctx.page, f"{args.name}-elements")
    return 0


async def run_test_step(
    config: AppConfig,
    ctx: FlowContext,
    *,
    selector: str,
    value: str | None = None,
    click: bool = False,
    manual_prepare: bool = False,
    wait: bool = True,
    url: str | None = None,
    timeout: int | None = None,
    prompt: Callable[[str], Awaitable[str]] = prompt_async,
) -> int:
    """Reach the target's screen, then validate (and optionally click) it.

    Separated from :func:`cmd_test_step` so the preparation and click policy can
    be tested without a browser.
    """
    key = selector.strip()

    # Fail on an unusable target before navigating anywhere.
    spec = config.selectors.require(key)
    if spec.is_dynamic:
        if not value:
            print(
                f"FAIL: {key} is DYNAMIC — pass the runtime text with "
                f'--value "Назва центру"'
            )
            return 1
        spec = spec.resolved(value)

    engine = FlowEngine(ctx, auto=True, pause_after_step=False)
    plan = config.flow.plan_for(key)

    if manual_prepare:
        start_url = url or config.flow.start_url_for(key)
        if url or ctx.page.url in {"about:blank", ""}:
            await engine.goto(start_url)
        if wait:
            await prompt("Navigate to the screen containing this element, then press ENTER: ")
    else:
        await engine.prepare(
            plan.prerequisites, start_url=url or config.flow.start_url_for(key)
        )
        if not plan.has_prerequisites and key not in config.flow.steps:
            print(
                f"\nNote: no prerequisites are configured for {key}. Add an entry under "
                "`steps:` in config/flow.yaml if it needs navigation first, or pass "
                "--manual-prepare to navigate by hand."
            )

    page_object = ctx.page_object_for(key)
    locator = build_locator(ctx.page, spec)

    print(f"\nSelector: {key}")
    print(f"Locator:  {spec.describe()}")
    print(f"URL:      {ctx.page.url}\n")

    indices = await page_object._wait_for_matches(
        locator, spec, timeout or config.flow.timeouts.default_locator
    )
    total = await locator.count()

    if not indices:
        print(f"FAIL: selector matched 0 elements (DOM matches: {total})")
        if spec.visible and total:
            print("      All matches are hidden — add `visible: false` if that is expected.")
        return 1

    candidates = await page_object.describe_candidates(locator, indices)
    # `visible: false` selectors (the hidden file input) are counted in the
    # DOM, so do not claim the matches were visible.
    kind = "visible element" if spec.visible else "element"
    if len(indices) == 1:
        print(f"PASS: selector matched exactly one {kind}")
        print(f"      {candidates[0]}")
    elif spec.nth is not None:
        print(f"PASS: selector matched {len(indices)} {kind}s; "
              f"nth={spec.nth} is configured")
        for line in candidates:
            print(f"      {line}")
    elif spec.multiple:
        print(f"PASS: selector matched {len(indices)} {kind}s "
              "(multiple: true is configured)")
        for line in candidates:
            print(f"      {line}")
    else:
        print(f"FAIL: selector matched {len(indices)} elements")
        for line in candidates:
            print(f"      {line}")
        print("\n      Narrow the selector, or set `nth:` deliberately.")
        return 1

    target = locator.nth(indices[spec.nth or 0])
    with suppress(Exception):  # highlighting is best effort
        await target.highlight()

    if ctx.diagnostics is not None:
        await ctx.diagnostics.screenshot(ctx.page, f"test-step-{key}")

    # Only ever reached after the target validated successfully.
    if click:
        print("\nClicking (--click was passed)…")
        url_before = ctx.page.url
        await target.click()
        await page_object.wait_stable()
        print(f"URL after click: {ctx.page.url}")
        if url_before == ctx.page.url:
            print("(URL unchanged — check the screenshot to see what happened)")
        if ctx.diagnostics is not None:
            await ctx.diagnostics.screenshot(ctx.page, f"test-step-{key}-after-click")
    else:
        print("\nNot clicking. Pass --click to actually interact.")
    return 0


async def cmd_test_step(args: argparse.Namespace) -> int:
    """Resolve one selector and report how many visible elements it matched."""
    async with app_session(args) as (config, ctx):
        if args.service_center:
            ctx.current_service_center = find_service_center(
                config.service_centers, args.service_center
            )
        return await run_test_step(
            config,
            ctx,
            selector=args.selector,
            value=args.value,
            click=args.click,
            manual_prepare=args.manual_prepare,
            wait=not args.no_wait,
            url=args.url,
            timeout=args.timeout,
        )


# --------------------------------------------------------------------------- #
# check-center
# --------------------------------------------------------------------------- #

#: Exit codes. Availability is an observation, never a process failure.
#:
#: 3 and 4 belong to the headless path and are defined in
#: EXIT_OK is imported from :mod:`.api.headless_monitor` rather than repeated
#: here, so a scheduled run and other CLI commands never disagree.
EXIT_RUNTIME = 1
EXIT_CONFIG = 2


def _display_path(path: Path) -> str:
    """Repo-relative when possible, so the printed path is copy-pasteable."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _resolve_center(config: AppConfig, service_center_id: str) -> ServiceCenter:
    """Everything that can be decided before a browser is launched.

    Raises :class:`ConfigError` for an unknown ID and
    :class:`SelectorNotConfigured` while the search box is still a TODO.
    """
    center = find_service_center(config.service_centers, service_center_id)
    config.selectors.require(DepartmentPage.SEARCH)
    return center


def _config_error(exc: HscMonitorError) -> int:
    print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
    return EXIT_CONFIG


def _print_availability(center: ServiceCenter, availability: DepartmentAvailability) -> None:
    print("\nSERVICE CENTER CHECK\n")
    print(f"ID:         {center.id}")
    print(f"Name:       {center.name}")
    print(f"Found:      {'yes' if availability.found else 'no'}")
    print(f"Disabled:   {str(availability.disabled).lower()}")
    print(f"Available:  {'YES' if availability.available else 'NO'}")
    if availability.available:
        # The card, not an appointment: an enabled centre can still have no free
        # date, and a free date can still have no free time.
        print(
            f"\n(That is the centre's button being enabled, not a free appointment.\n"
            f" For real dates and times: check-availability --center {center.id})"
        )
    if availability.full_text:
        print("\nFull text:")
        print(availability.full_text)
        if center.full_name and availability.full_text != center.full_name:
            # Not an error: the address is display text, the ID is the identity.
            print(f"\n(Configured full_name differs: {center.full_name})")


async def run_check_center(
    config: AppConfig,
    ctx: FlowContext,
    *,
    service_center_id: str,
    click: bool = False,
    url: str | None = None,
) -> int:
    """Reach the service-centre screen, search one centre and report its state.

    Nothing is clicked on that screen unless ``click`` is set *and* the centre's
    button is enabled. Separated from :func:`cmd_check_center` so the whole
    policy is testable without a browser.
    """
    try:
        center = _resolve_center(config, service_center_id)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)

    if not center.enabled:
        print(
            f"Note: {center.id} is disabled in config/service_centers.yaml — "
            "checking it anyway because you asked for it by ID."
        )

    ctx.current_service_center = center
    key = DepartmentPage.SEARCH
    engine = FlowEngine(ctx, auto=True, pause_after_step=False)

    try:
        await engine.prepare(
            config.flow.plan_for(key).prerequisites,
            start_url=url or config.flow.start_url_for(key),
        )
        # The last prerequisite is the category card, and HSC draws the
        # service-centre screen after it, behind a spinner. Wait for that
        # screen rather than letting the search below race it.
        await ctx.department.wait_until_ready()
        await ctx.department.search_department(center.search_term)
        availability = await ctx.department.get_department_availability(
            center.id, name=center.name
        )
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME

    _print_availability(center, availability)

    if not availability.found:
        print(f"\nSearching for {center.search_term!r} did not put this centre on screen.")
        labels = await ctx.department.candidate_labels(center.search_term)
        if labels:
            print("Buttons that did match the search term:")
            for label in labels[:20]:
                print(f"  - {label}")
        else:
            print(
                "Nothing matched it at all. If the site's search filters by name "
                "rather than by ID, put the searchable text in `name:` in "
                "config/service_centers.yaml — the ID stays the identity."
            )
        return EXIT_RUNTIME

    if not click:
        print("\nNot clicking. Pass --click to select it and inspect the next screen.")
        return EXIT_OK

    if not availability.available:
        print(
            f"\nNot clicking: service centre {center.id} is disabled right now, so "
            "there is no next screen to inspect. Re-run when it is available."
        )
        return EXIT_OK

    print(f"\nClicking service center {center.id}...")
    try:
        await ctx.department.select_department(center.id)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME

    print("\nURL after click:")
    print(ctx.page.url)

    if ctx.diagnostics is not None:
        label = f"check-center-{center.id}-after-click"
        shot = await ctx.diagnostics.screenshot(ctx.page, label)
        # A uniquely named dump: the generic page-elements.json is overwritten
        # by every `inspect` round and would not survive long enough to be read.
        dump = await ctx.diagnostics.dump_elements(ctx.page, f"{label}-elements")
        if shot is not None:
            print("\nScreenshot:")
            print(_display_path(shot))
        if dump is not None:
            print("\nElements:")
            print(_display_path(dump))

    print("\nStopped after service-center selection.")
    print("No date or time was selected.")
    return EXIT_OK


async def run_check_availability(
    config: AppConfig,
    ctx: FlowContext,
    *,
    centers: Sequence[str] = (),
    url: str | None = None,
) -> int:
    """Scan 1–5 centres for real appointment availability and print it.

    Reads only. The scan stops at the list of free times — see
    :mod:`..flow.availability` for the boundary and why it exists.
    """
    try:
        chosen = centers_to_scan(config.service_centers, centers)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)

    print(f"\nScanning {len(chosen)} service centre(s): "
          f"{', '.join(c.id or c.name for c in chosen)}")

    scanner = AvailabilityScanner(ctx, start_url=url)
    try:
        results = await scanner.scan(chosen)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME

    print(render_availability(results))
    print("\nNothing was booked: no time was selected and no form was submitted.")
    return EXIT_OK


async def cmd_check_availability(args: argparse.Namespace) -> int:
    # Which centres to scan is answerable without a browser, and "six centres"
    # or "none enabled" should not cost a browser launch to find out.
    try:
        centers_to_scan(load_config(args).service_centers, args.centers or ())
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)

    async with app_session(args) as (config, ctx):
        return await run_check_availability(
            config, ctx, centers=args.centers or (), url=args.url
        )


async def cmd_check_center(args: argparse.Namespace) -> int:
    # An unknown ID or an unconfigured search box is answerable without a
    # browser, so do not open one just to print a configuration error.
    try:
        _resolve_center(load_config(args), args.service_center_id)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)

    async with app_session(args) as (config, ctx):
        return await run_check_center(
            config,
            ctx,
            service_center_id=args.service_center_id,
            click=args.click,
            url=args.url,
        )


# --------------------------------------------------------------------------- #
# api-probe / api-observe  (diagnostic experiments — see hsc_queue_monitor.api)
# --------------------------------------------------------------------------- #


async def run_api_probe(
    config: AppConfig,
    ctx: FlowContext,
    *,
    url: str | None = None,
    sequence: bool = False,
    items: int = DEFAULT_ITEMS,
    fetch: Fetch = http_get,
) -> int:
    """Authenticate, then call the HSC JSON API directly with those cookies.

    The whole experiment: reach ``/cabinet`` through the ordinary
    :class:`~..flow.auth.AuthManager`, stop there — no queue or menu is clicked —
    copy the browser's HSC cookies into an isolated ``requests.Session``, and GET
    one measured endpoint.

    Read-only in every direction. Nothing is booked, and no cookie is ever
    written back into the browser: if the API rejects the copy, the working UI
    session is untouched.
    """
    try:
        targets = (
            [(r.name, resolve_url(require_read_only(r).path)) for r in MEASURED_REQUESTS]
            if sequence
            else [("url", resolve_url(url))]
        )
    except ApiProbeError as exc:
        return _config_error(exc)

    # The single authentication path, before anything else happens.
    try:
        await ctx.auth.ensure_authenticated()
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME

    print("\nAPI PROBE\n")
    print(f"Authenticated: YES\nBrowser URL: {ctx.page.url}")

    raw = await read_browser_cookies(ctx.page)
    cookies = hsc_cookies(raw)
    if not cookies:
        print(
            "\nNo hsc.gov.ua cookies were found in the browser context, so there "
            "is nothing to copy and the experiment cannot run.\n",
            file=sys.stderr,
        )
        return EXIT_RUNTIME

    names = describe_cookies(cookies)
    # Names only. A value never reaches a log, a print or an artifact.
    logger.info(
        "Exported %d HSC cookies:\n  %s",
        len(names),
        "\n  ".join(info.name for info in names),
    )

    session = build_session(cookies, user_agent=await read_user_agent(ctx.page))
    outcomes: list[ProbeOutcome] = []

    for label, target in targets:
        if sequence:
            print(f"\n--- {label} ---")
        # requests is blocking; the browser stays responsive on the event loop.
        outcome = await asyncio.to_thread(perform, session, target, fetch=fetch)
        outcomes.append(outcome)
        print(render_outcome(outcome, items=items))

        if sequence and not outcome.ok:
            # One request is enough to learn that the sequence cannot continue.
            # Repeating it is how a diagnostic turns into a retry loop.
            print(
                f"Stopping the sequence at {label}: the request did not return "
                "JSON, and nothing is retried.\n"
            )
            break

    print(
        "Nothing was booked, reserved or submitted, and no cookie was written "
        "back into the browser.\n"
    )
    # A status code is an observation, whatever it is. Never reaching the server
    # is not: nothing at all was measured.
    return EXIT_OK if all(outcome.responded for outcome in outcomes) else EXIT_RUNTIME


async def cmd_api_probe(args: argparse.Namespace) -> int:
    # An off-site URL is answerable without a browser, and must be refused
    # before an authenticated session exists to be misdirected.
    if not args.sequence:
        try:
            resolve_url(args.url)
        except ApiProbeError as exc:
            return _config_error(exc)

    async with app_session(args) as (config, ctx):
        return await run_api_probe(
            config, ctx, url=args.url, sequence=args.sequence, items=args.items
        )


async def run_api_observe(
    config: AppConfig,
    ctx: FlowContext,
    *,
    prompt: Callable[[str], Awaitable[str]] = prompt_async,
) -> int:
    """Log the page's own HSC ``/api/`` calls while a human clicks through.

    This is how the measured-endpoint list grows without guessing. The command
    itself clicks nothing after authentication: it attaches a passive listener
    and waits.
    """
    try:
        await ctx.auth.ensure_authenticated()
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME

    observer = ApiObserver(
        ctx.page, on_record=lambda record: print(f"  {record.describe()}")
    )
    observer.start()
    try:
        print("\nAPI NETWORK OBSERVER\n")
        print("Authenticated. Nothing else has been clicked.\n")
        print("Now drive the wizard yourself in the Chromium window — category A,")
        print("centre 3242, a date — and watch the calls appear here.\n")
        print("Recorded: method, path, non-sensitive query values, status, type.")
        print("Never recorded: headers, cookies, tokens, request or response bodies.\n")
        await prompt("Press ENTER here when you are done observing: ")
    finally:
        observer.stop()

    print()
    print(observer.render())
    print(
        "\nCopy the lines you want to reproduce into MEASURED_REQUESTS in "
        "src/hsc_queue_monitor/api/endpoints.py — measured, never guessed.\n"
    )
    return EXIT_OK


async def cmd_api_observe(args: argparse.Namespace) -> int:
    async with app_session(args) as (config, ctx):
        return await run_api_observe(config, ctx)


async def _bootstrap_queue_session(config: AppConfig, ctx: FlowContext) -> QueueBootstrap:
    """One navigation to the queue page, with the cookie state either side of it.

    Exactly one extra thing happens to the browser: ``goto``. No wizard control
    is clicked and no preparation chain is replayed — whether the navigation
    *alone* mints the queue session is the question being asked.
    """
    url = config.flow.queue_url
    before = wizard_fingerprint(hsc_cookies(await read_browser_cookies(ctx.page)))

    logger.info("Queue bootstrap: opening %s (no wizard control is clicked)", url)
    await ctx.page.goto(
        url, wait_until="domcontentloaded", timeout=config.flow.timeouts.navigation
    )
    # The project's own settle: lenient networkidle, no sleep, no selector — the
    # point is not to enter the wizard.
    await ctx.queue.wait_stable()

    after = wizard_fingerprint(hsc_cookies(await read_browser_cookies(ctx.page)))
    return QueueBootstrap(url=url, final_url=ctx.page.url, before=before, after=after)


def _print_availability_header(bootstrap: QueueBootstrap | None) -> None:
    print("\nAPI AVAILABILITY\n")
    if bootstrap is not None:
        print("\n".join(bootstrap.render()))
        print()


async def run_api_availability(
    config: AppConfig,
    ctx: FlowContext,
    *,
    center: str,
    max_dates: int = 0,
    open_queue: bool = False,
    slot_interval: float | None = None,
    fetch: Fetch | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Read one centre's availability through the API instead of the wizard.

    Live validation of the measured endpoints, run beside the UI scanner rather
    than in place of it: ``check-availability``, ``monitor`` and
    :class:`~.flow.availability.AvailabilityScanner` are untouched.

    The journey is authenticate → stop at ``/cabinet`` → bridge the cookies →
    departments → days → slots. No wizard control is clicked, no date or time is
    selected, and nothing is submitted.

    ``open_queue`` adds exactly one navigation, to the queue page, between the
    cabinet and the cookie bridge — the opt-in experiment for "does the API need
    a session that only the queue page creates?". The bridge is built *after*
    it, so the session carries whatever that navigation produced; nothing is
    ever synced into an already-built jar.

    Exit codes differ from ``api-probe`` on purpose: this command exists to
    *obtain* availability, so a refusal or an unreadable schema is a failure to
    do its job (``EXIT_RUNTIME``) even though it is a perfectly good observation.
    """
    try:
        centre = find_service_center(config.service_centers, center)
        interval = (
            config.app.api.slot_request_interval_seconds
            if slot_interval is None
            else validate_slot_interval(slot_interval, "--slot-interval")
        )
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)

    try:
        await ctx.auth.ensure_authenticated()
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME

    # Before the session exists, so the cookies it copies are the ones the
    # navigation left behind.
    bootstrap = await _bootstrap_queue_session(config, ctx) if open_queue else None

    try:
        client, cookies = await client_for(
            ctx.page, timeout=config.app.api.timeout, retry=config.app.api.retry, fetch=fetch
        )
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME

    # Names only. A value never reaches a log, a print or an artifact.
    logger.info(
        "Exported %d HSC cookies:\n  %s",
        len(cookies),
        "\n  ".join(info.name for info in cookies),
    )

    try:
        # One thread for the whole sequence: the calls are blocking, share a
        # cookie jar, and must stay in order.
        scan = await asyncio.to_thread(
            scan_centre,
            client,
            centre.id,
            max_dates=max_dates,
            slot_interval=interval,
            sleep=sleep,
            clock=clock,
        )
    except ApiSchemaUnknown as exc:
        _print_availability_header(bootstrap)
        print(render_schema_stop(exc))
        return EXIT_RUNTIME
    except ApiRequestFailed as exc:
        _print_availability_header(bootstrap)
        print(f"{exc.call.label}: {exc.call.target}")
        print(render_outcome(exc.call.outcome))
        _explain_no_content(exc, bootstrap)
        return EXIT_RUNTIME
    except HscMonitorError as exc:
        _print_availability_header(bootstrap)
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME
    finally:
        _close_client(client)

    print(render_api_availability(scan, bootstrap=bootstrap))
    if scan.schema_stop is not None:
        # The partial results above are real; this says why the rest is missing.
        print(render_schema_stop(scan.schema_stop))
    print(
        "\nRead only: no date or time was selected, nothing was booked, and the "
        "browser session was not modified.\n"
    )
    # A refused date is an observation about the API, and the dates that were
    # read are still worth reporting — the run did its job.
    return EXIT_OK


def _close_client(client: HscApiClient) -> None:
    """Drop the HTTP session. Cookies live only as long as the process needs them."""
    with suppress(Exception):
        client.close()


def _explain_no_content(failure: ApiRequestFailed, bootstrap: QueueBootstrap | None) -> None:
    """Say what an empty departments response means for the experiment.

    Nothing is retried, no other endpoint is tried, and no click is added: the
    result is reported and the run ends.
    """
    if failure.call.label != LABEL_DEPARTMENTS or failure.call.outcome.kind != KIND_NO_CONTENT:
        return
    if bootstrap is not None:
        print("Queue session bootstrap did not make departments available.")
        if bootstrap.worked:
            print(
                "The queue session cookie exists and the endpoint still returned "
                "nothing, so the wizard state it needs is not created by opening "
                "the page alone."
            )
    else:
        print(
            "Try `--open-queue`: it adds one navigation to the queue page (no "
            "wizard control is clicked) to test whether that is what creates the "
            "session this endpoint needs."
        )
    print()


class BrowserSessionProvider:
    """Turns a browser session into an HTTP one, then closes the browser.

    The entire browser lifetime is inside :meth:`create_api_session`: launch,
    authenticate, one navigation to the queue page (no wizard control is ever
    clicked), copy the cookies, close. What comes back holds a
    ``requests.Session`` and no Playwright object at all, which is what lets the
    monitor poll for hours with no Chromium running.

    The context manager does the closing, so the persistent profile is flushed
    the way it always is. Nothing kills a process, and nothing touches the
    profile directory.
    """

    def __init__(self, config: AppConfig, *, fetch: Fetch | None = None) -> None:
        self.config = config
        self.fetch = fetch
        #: Counted so the lifecycle is observable in tests and in logs.
        self.sessions_created = 0

    async def create_api_session(self) -> ApiSession:
        logger.info("Opening browser for HSC authentication/bootstrap")
        async with self._browser() as ctx:
            await ctx.auth.ensure_authenticated()
            bootstrap = await _bootstrap_queue_session(self.config, ctx)
            # After the bootstrap, so the session carries what it minted.
            client, cookies = await client_for(
                ctx.page,
                timeout=self.config.app.api.timeout,
                retry=self.config.app.api.retry,
                fetch=self.fetch,
            )
            logger.info("API session created")

        logger.info("Closing browser; monitor will continue over HTTP only")
        self.sessions_created += 1
        return ApiSession(client=client, cookies=tuple(cookies), bootstrap=bootstrap)

    def restore_api_session(self, persisted: PersistedSession) -> ApiSession:
        """The same client, rebuilt from a stored jar. Opens nothing.

        Whether those cookies still work is not decided here — the next ordinary
        departments call decides it.
        """
        api = self.config.app.api
        session = session_from_cookies(
            persisted.cookies, user_agent=persisted.user_agent
        )
        client = HscApiClient(
            session, timeout=api.timeout, retry=api.retry, fetch=self.fetch
        )
        return ApiSession(client=client, cookies=tuple(describe_cookies(persisted.cookies)))

    @asynccontextmanager
    async def _browser(self) -> AsyncIterator[FlowContext]:
        """The browser, open only for as long as the hand-over takes."""
        paths = self.config.paths
        async with BrowserManager(
            paths.profile_dir,
            headless=self.config.app.headless,
            default_timeout=self.config.flow.timeouts.default_locator,
        ) as browser:
            page = await browser.page()
            yield FlowContext(
                config=self.config,
                page=page,
                diagnostics=Diagnostics(paths.debug_dir, enabled=True),
            )


def build_session_store(config: AppConfig) -> SessionStore | None:
    """A MongoDB store when it is configured, or ``None`` when it is not.

    Persistence is opt-in: without ``HSC_MONGODB_URI`` the monitor behaves
    exactly as it did before, opening a browser once per process. With a URI but
    no key it refuses to start, because writing a session somewhere it cannot be
    encrypted is not a lesser version of the feature — it is a different and
    worse one.
    """
    secrets = config.secrets
    if not secrets.mongodb_uri:
        logger.info("Session persistence is not configured; using memory only")
        return None

    uri, key = secrets.require_persistence()
    return MongoSessionStore(
        uri,
        SessionCipher(key),
        # Names from the committed config; credentials from the environment.
        database=config.app.mongodb.database,
        collection=config.app.mongodb.session_collection,
    )


def build_notifier(config: AppConfig) -> OutboundTelegramNotifier | None:
    """The Telegram transport when it is configured, or ``None`` when it is not.

    Optional, and all-or-nothing: with neither setting the monitor behaves
    exactly as it did before notifications existed, and with only one of them it
    refuses to start rather than run half a feature.
    """
    secrets = config.secrets
    secrets.require_telegram()
    if not config.app.telegram.enabled or not secrets.telegram_configured:
        return None

    # A count, never the list: those ids name people.
    logger.info(
        "Telegram notifications enabled for %d recipient(s)", len(secrets.telegram_users)
    )
    return OutboundTelegramNotifier(secrets.telegram_bot_token, secrets.telegram_users)


def availability_snapshot_store(
    session_store: SessionStore,
) -> AvailabilitySnapshotStore:
    """The last complete availability, in a third document beside the other two.

    Same connection again. Three documents with three lifetimes — a session that
    expires in fifteen minutes, a state that sticks until a human acts, and a
    snapshot that changes whenever HSC does — and no reason to merge any of them.
    """
    if isinstance(session_store, MongoSessionStore):
        return MongoAvailabilitySnapshotStore(session_store.collection)
    return NullAvailabilitySnapshotStore()


def monitor_state_store(session_store: SessionStore) -> MonitorStateStore:
    """The monitor's own state, in a second document beside the session.

    Same connection, same collection, different ``_id`` — the two have different
    lifetimes and different secrecy, so they are never merged into one document,
    but they have no reason to open two clients.
    """
    if isinstance(session_store, MongoSessionStore):
        return MongoMonitorStateStore(session_store.collection)
    return NullMonitorStateStore()


async def run_refresh_session(
    config: AppConfig,
    *,
    provider: ApiSessionProvider | None = None,
    store: SessionStore | None = None,
) -> int:
    """Local only: authenticate, mint the queue session, store it, close up.

    The other half of the split. This is the one command that needs the
    MasterKey, the browser and a human nearby; ``monitor-once`` needs none of
    them and cannot do any of it. Persistence is mandatory here, because a
    refreshed session that goes nowhere is not a refresh of anything.

    It reads no availability at all: no departments, no days, no slots.
    """
    try:
        session_store = store or build_session_store(config)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_PERSISTENCE

    if session_store is None:
        return _config_error(
            ConfigError(
                "refresh-session writes the session to MongoDB, so "
                "HSC_MONGODB_URI and HSC_SESSION_ENCRYPTION_KEY must both be "
                "set in .env.\nWithout them there is nowhere for the headless "
                "monitor to read the session from."
            )
        )

    print("\nHSC SESSION REFRESH\n")
    try:
        session = await (provider or BrowserSessionProvider(config)).create_api_session()
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME
    finally:
        # Whatever happened, the browser is already closed: the provider owns it
        # from end to end.
        pass

    print("Authentication: OK")

    bootstrap = session.bootstrap
    if bootstrap is None or not bootstrap.worked:
        print(
            f"Queue bootstrap: FAILED\n\n{WIZARD_COOKIE} was not created by "
            f"opening {config.flow.queue_url}, so this session cannot read the "
            "API.\nNothing was written to MongoDB.\n",
            file=sys.stderr,
        )
        _close_client(session.client)
        session_store.close()
        return EXIT_RUNTIME

    print("Queue bootstrap: OK")
    print("Session persistence: MongoDB")

    cookies = cookies_from_jar(session.client.session)
    # Both writes happen inside this block, against the one Mongo client, and
    # the client is closed only after both. Closing between them is exactly the
    # bug this shape exists to prevent: the session lands, the state write hits
    # a closed client, and the next scheduled run stays gated behind a stale
    # AUTH_REQUIRED that nothing ever cleared.
    try:
        try:
            session_store.save(
                PersistedSession(
                    cookies=cookies,
                    user_agent=str(session.client.session.headers.get("User-Agent", "")),
                    queue_session_expires_at=queue_expiry(cookies),
                )
            )
        except HscMonitorError as exc:
            print(f"Session saved: FAILED\n\n{exc}\n", file=sys.stderr)
            return EXIT_PERSISTENCE

        print("Session saved: OK")

        # Only now, and only on the same open client. Clearing the flag before
        # the session was safely stored would let the next scheduled run resume
        # against a session that does not exist.
        states = monitor_state_store(session_store)
        previous = None
        with suppress(HscMonitorError):
            previous = states.load()
        event = record(states, previous, MonitorStatus.READY, now=datetime.now(UTC))
    finally:
        _close_client(session.client)
        session_store.close()

    if event is None:
        # The state document is unchanged, which means a persisted
        # AUTH_REQUIRED is still there — and that is the safe outcome. Saying
        # "ready" here would be a lie the next scheduled run would disprove.
        print(
            "\nSESSION REFRESH INCOMPLETE\n\n"
            "The HSC session was refreshed and saved, but monitor state could\n"
            "not be changed to READY.\n\n"
            "Headless monitoring remains paused. Re-run this command once the\n"
            "database is reachable.\n",
            file=sys.stderr,
        )
        return EXIT_PERSISTENCE

    if event.changed:
        print(f"Monitor state: {event.describe()}")

    print("\nBrowser closed.")
    print("Session is ready for headless monitoring.\n")
    return EXIT_OK


async def cmd_refresh_session(args: argparse.Namespace) -> int:
    # No app_session: the provider opens and closes the browser itself.
    return await run_refresh_session(load_config(args))


def run_monitor_once(
    config: AppConfig,
    *,
    centers: Sequence[str] = (),
    slot_interval: float | None = None,
    max_dates: int = 0,
    store: SessionStore | None = None,
    state_store: MonitorStateStore | None = None,
    snapshots: AvailabilitySnapshotStore | None = None,
    fetch: Fetch | None = None,
    notifier: OutboundNotifier | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """One headless scan, for GitHub Actions. Never opens a browser.

    Synchronous on purpose: there is no event loop worth starting for a handful
    of sequential HTTP reads, and nothing here awaits a browser.

    All operational outcomes return exit code 0. Operational failures (auth
    required, rate limited, service unavailable, etc.) are represented through
    persisted monitor state, logs and Telegram notifications, not exit codes.
    Configuration errors still return non-zero.
    """
    try:
        chosen = centers_to_scan(config.service_centers, centers)
        pacing = (
            config.app.api.slot_request_interval_seconds
            if slot_interval is None
            else validate_slot_interval(slot_interval, "--slot-interval")
        )
        session_store = store or build_session_store(config)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_PERSISTENCE

    if session_store is None:
        return _config_error(
            ConfigError(
                "monitor-once reads its session from MongoDB, so HSC_MONGODB_URI "
                "and HSC_SESSION_ENCRYPTION_KEY must both be set.\nIt cannot "
                "authenticate: that is what `refresh-session` is for, and it has "
                "to run locally."
            )
        )

    states = state_store or monitor_state_store(session_store)
    stored_availability = snapshots or availability_snapshot_store(session_store)

    try:
        outbound = notifier or build_notifier(config)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)

    api = config.app.api
    try:
        try:
            scan = run_headless_scan(
                session_store,
                [centre.id for centre in chosen],
                state_store=states,
                snapshots=stored_availability,
                timeout=api.timeout,
                retry=api.retry,
                slot_interval=pacing,
                max_dates=max_dates,
                fetch=fetch,
                emit=emit,
            )
        except Exception:
            logger.exception("Unexpected error during monitor-once scan")
            scan = None
            outbound_for_error = outbound
        else:
            outbound_for_error = None

        if scan is not None:
            # Everything worth telling anyone about is already persisted by now: the
            # session, the monitor state and the availability snapshot. A message that
            # fails to send changes none of them.
            NotificationDispatcher(outbound).notify_scan(scan)
        else:
            # Unexpected exception: best-effort error notification
            if outbound_for_error:
                try:
                    NotificationDispatcher(outbound_for_error).notify_unexpected_error()
                except Exception as notify_exc:
                    logger.exception("Failed to send error notification: %s", notify_exc)
    finally:
        session_store.close()

    # Always return 0 for operational outcomes. Configuration errors already
    # returned non-zero above.
    return EXIT_OK


def run_init_config(
    config: AppConfig,
    *,
    output: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    store: SessionStore | None = None,
    fetch: Fetch | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Discover the service centres and write them into service_centers.yaml.

    Configuration discovery, and nothing else: one departments call, no days, no
    slots, no browser. It reads its session the way ``monitor-once`` does, which
    is also why it keeps that session fresh — the response's cookies are written
    back through the same persister.
    """
    try:
        session_store = store or build_session_store(config)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_PERSISTENCE

    if session_store is None:
        return _config_error(
            ConfigError(
                "init-config reads the catalogue with the session stored in "
                "MongoDB, so HSC_MONGODB_URI and HSC_SESSION_ENCRYPTION_KEY must "
                "both be set.\nIt cannot authenticate: run `refresh-session` "
                "locally first."
            )
        )

    api = config.app.api
    target = output or config.paths.service_centers_path
    try:
        return run_config_init(
            session_store,
            output=target,
            dry_run=dry_run,
            force=force,
            timeout=api.timeout,
            retry=api.retry,
            fetch=fetch,
            emit=emit,
        )
    finally:
        session_store.close()


def cmd_init_config(args: argparse.Namespace) -> int:
    return run_init_config(
        load_config(args),
        output=Path(args.output) if args.output else None,
        dry_run=args.dry_run,
        force=args.force,
    )


def run_telegram_test(
    config: AppConfig,
    *,
    notifier: OutboundTelegramNotifier | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Prove the bot can reach its recipients, and nothing else at all.

    Transport only: no database, no HSC request, no browser, no session. When
    this fails, the thing that failed is Telegram.
    """
    try:
        bot = notifier or build_notifier(config)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)

    if bot is None:
        print(
            "\nTELEGRAM TEST FAILED\n\n"
            "Telegram notifications are not configured.\n"
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_USERS in .env, then run this "
            "again.\n",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    report = send_test_message(bot, emit=emit)
    return EXIT_OK if report.ok else EXIT_RUNTIME


def cmd_telegram_test(args: argparse.Namespace) -> int:
    return run_telegram_test(load_config(args))


def cmd_monitor_once(args: argparse.Namespace) -> int:
    return run_monitor_once(
        load_config(args),
        centers=args.centers or (),
        slot_interval=args.slot_interval,
    )


async def run_api_monitor(
    config: AppConfig,
    *,
    centers: Sequence[str] = (),
    interval: float | None = None,
    slot_interval: float | None = None,
    once: bool = False,
    max_dates: int = 0,
    provider: ApiSessionProvider | None = None,
    session_store: SessionStore | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    slot_sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = datetime.now,
) -> int:
    """Poll the API for 1–5 centres and print what changed. Dry run only.

    Nothing is notified, persisted or booked: this prints to stdout and that is
    all it does. The browser monitor, ``check-availability`` and the Telegram
    notifier are untouched and unaware of it.
    """
    try:
        chosen = centers_to_scan(config.service_centers, centers)
        scan_interval = (
            config.app.api.monitor_interval_seconds
            if interval is None
            else validate_monitor_interval(interval, "--interval")
        )
        pacing = (
            config.app.api.slot_request_interval_seconds
            if slot_interval is None
            else validate_slot_interval(slot_interval, "--slot-interval")
        )
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)

    try:
        store = session_store or build_session_store(config)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        # A database that will not connect is not a reason to refuse to monitor;
        # it is a reason to monitor without persistence.
        logger.warning("Session persistence is unavailable: %s", exc)
        store = None

    if scan_interval < ADVISED_MIN_MONITOR_INTERVAL_SECONDS:
        logger.warning(
            "A %.0fs interval is below the advised %.0fs — HSC rate-limits, and a "
            "429 costs a whole cycle.",
            scan_interval,
            ADVISED_MIN_MONITOR_INTERVAL_SECONDS,
        )

    monitor = ApiMonitor(
        provider or BrowserSessionProvider(config),
        [centre.id for centre in chosen],
        interval=scan_interval,
        slot_interval=pacing,
        max_dates=max_dates,
        store=store,
        sleep=sleep,
        clock=clock,
        slot_sleep=slot_sleep,
        now=now,
    )

    print("\nAPI MONITOR (dry run)\n")
    print(f"Centres:  {', '.join(centre.id for centre in chosen)}")
    print(f"Interval: {scan_interval:.0f}s between scan starts")
    print(f"Pacing:   {pacing:.1f}s between slot requests")
    print(f"Session:  {'persisted (encrypted)' if store is not None else 'in memory only'}")
    print("Nothing is booked and nothing is notified.\n")

    try:
        await monitor.run(once=once)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # A deliberate stop is not a crash: no traceback, no partial line.
        print("\nMonitor stopped.")
        return EXIT_OK
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return EXIT_RUNTIME

    if not once:  # pragma: no cover - only reachable if run() ever returns
        print("\nMonitor stopped.")
    return EXIT_OK


async def cmd_api_monitor(args: argparse.Namespace) -> int:
    """Deliberately *not* wrapped in ``app_session``.

    Every other browser command holds Chromium open for its whole run. This one
    must not: the browser belongs to :meth:`BrowserSessionProvider.create_api_session`
    and is closed again before the first poll.
    """
    config = load_config(args)
    return await run_api_monitor(
        config,
        centers=args.centers or (),
        interval=args.interval,
        slot_interval=args.slot_interval,
        once=args.once,
    )


async def cmd_api_availability(args: argparse.Namespace) -> int:
    # An unknown centre is answerable without a browser.
    try:
        find_service_center(load_config(args).service_centers, args.center)
    except (SelectorNotConfigured, ConfigError) as exc:
        return _config_error(exc)

    async with app_session(args) as (config, ctx):
        return await run_api_availability(
            config,
            ctx,
            center=args.center,
            max_dates=args.max_dates,
            open_queue=args.open_queue,
            slot_interval=args.slot_interval,
        )


async def cmd_flow(args: argparse.Namespace) -> int:
    async with app_session(args) as (config, ctx):
        engine = FlowEngine(
            ctx,
            auto=args.auto,
            pause_after_step=True if args.pause else None,
        )
        if args.service_center:
            ctx.current_service_center = find_service_center(
                config.service_centers, args.service_center
            )
        try:
            await engine.run(from_step=getattr(args, "from"), include_login=not args.no_login)
        finally:
            if not args.auto:
                await prompt_async("\nPress ENTER to close the browser: ")
    return 0


async def cmd_monitor(args: argparse.Namespace) -> int:
    async with app_session(args) as (config, ctx):
        notifiers: list[Notifier] = [ConsoleNotifier(dry_run=args.dry_run)]

        if args.dry_run:
            logger.info("Dry run: Telegram will not be contacted and state is not updated.")
        elif config.secrets.telegram_configured:
            # The older notifier speaks to one chat; the first configured
            # recipient is the one it gets.
            notifiers.append(
                TelegramNotifier(
                    config.secrets.telegram_bot_token,
                    str(config.secrets.telegram_users[0]),
                )
            )
            logger.info("Telegram notifications enabled.")
        else:
            logger.info("Telegram is not configured; using console notifications only.")

        state = StateStore(
            config.paths.state_path,
            cooldown_seconds=config.app.browser_monitor.notify_cooldown_seconds,
        ).load()

        monitor = Monitor(ctx, notifiers, state, dry_run=args.dry_run)
        logger.info(
            "Poll interval: %ds ±%ds",
            config.app.browser_monitor.poll_interval_seconds,
            config.app.browser_monitor.poll_jitter_seconds,
        )
        try:
            await monitor.run(once=args.once)
        except KeyboardInterrupt:  # pragma: no cover - interactive
            logger.info("Stopped by user.")
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hsc_queue_monitor.cli",
        description="HSC Parser: availability monitor for the HSC electronic queue.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--config-dir", help="override the config/ directory")
    parser.add_argument("--data-dir", help="override the data/ directory")
    parser.add_argument(
        "--pwdebug",
        action="store_true",
        help="run with the Playwright Inspector (sets PWDEBUG=1)",
    )
    headless = parser.add_mutually_exclusive_group()
    headless.add_argument(
        "--headless", dest="headless", action="store_true", default=None,
        help="force headless (not recommended: challenges need a visible window)",
    )
    headless.add_argument(
        "--headed", dest="headless", action="store_false", help="force a visible window"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_auth_status = sub.add_parser(
        "auth-status",
        help="report whether the persistent profile still has a live session",
        description=(
            "Opens the cabinet and reports whether the session is authenticated. "
            "Diagnostic only — it never logs in."
        ),
    )
    p_auth_status.set_defaults(func=cmd_auth_status, needs_browser=True)

    p_ensure_auth = sub.add_parser(
        "ensure-auth",
        help="authenticate if the session has expired, then stop at /cabinet",
        description=(
            "Runs only the authentication guard: if the session is live it "
            "returns immediately, otherwise it walks the ID.GOV.UA MasterKey "
            "journey and verifies the cabinet. Nothing else is clicked."
        ),
    )
    p_ensure_auth.set_defaults(func=cmd_ensure_auth, needs_browser=True)

    p_debug_provider = sub.add_parser(
        "ensure-auth-debug-provider",
        help="A/B test: authenticate, but pick the КНЕДП by hand",
        description=(
            "Temporary diagnostic. Runs the normal journey but stops before "
            "the provider dropdown and waits for you to choose it through the "
            "page's own UI — select_key_provider() is never called. Everything "
            "after that (upload, password, submit, observers) is automated and "
            "unchanged, so the manual selection is the only variable. "
            "`ensure-auth` is not affected."
        ),
    )
    p_debug_provider.set_defaults(
        func=cmd_ensure_auth_debug_provider, needs_browser=True
    )

    p_debug_password = sub.add_parser(
        "ensure-auth-debug-password",
        help="A/B test: authenticate, but type the password by hand",
        description=(
            "Temporary diagnostic. Runs the normal journey — including "
            "automatic provider selection and key upload — then stops before "
            "the password field and waits for you to type it in the browser. "
            "IDGOV_SIGNING_KEY_PASSWORD is never filled and the typed value is never "
            "read: only a browser-side boolean says whether the field is "
            "empty. Everything after that is automated and unchanged. "
            "`ensure-auth` is not affected."
        ),
    )
    p_debug_password.set_defaults(
        func=cmd_ensure_auth_debug_password, needs_browser=True
    )

    p_debug_native_file = sub.add_parser(
        "ensure-auth-debug-native-file-only",
        help="A/B test: select the key natively, then stop before the password",
        description=(
            "Temporary diagnostic. Runs the production journey — provider, "
            "native macOS file selection, key-loaded check — and stops there, "
            "leaving the browser open so you can type the password by hand and "
            "see whether the reset still happens. IDGOV_SIGNING_KEY_PASSWORD is never "
            "read, filled or submitted. `ensure-auth` is not affected."
        ),
    )
    p_debug_native_file.set_defaults(
        func=cmd_ensure_auth_debug_native_file_only, needs_browser=True
    )

    p_debug_native_ax = sub.add_parser(
        "ensure-auth-debug-native-ax",
        help="A/B test: dump the real macOS accessibility hierarchy",
        description=(
            "Temporary diagnostic. Runs the journey to the key-file screen, "
            "opens the real macOS Open dialog, sends ⌘⇧G, and then dumps the "
            "browser process's actual accessibility tree to "
            "data/debug/native-ax-<timestamp>.json. It selects no file, types "
            "nothing, never presses Return, and leaves the dialog open. "
            "`ensure-auth` is not affected."
        ),
    )
    p_debug_native_ax.set_defaults(
        func=cmd_ensure_auth_debug_native_ax, needs_browser=True
    )

    p_inspect = sub.add_parser("inspect", help="open the site and dump visible elements")
    p_inspect.add_argument("--url", help="start at this URL instead of the queue page")
    p_inspect.add_argument("--limit", type=int, default=40, help="elements printed per dump")
    p_inspect.add_argument(
        "--screenshot", action="store_true", help="also save a screenshot per dump"
    )
    p_inspect.set_defaults(func=cmd_inspect, needs_browser=True)

    p_inspect_auth = sub.add_parser(
        "inspect-auth",
        help="discover the login / ID.GOV.UA selectors, one screen per capture",
        description=(
            "Same as `inspect`, but every capture is written to a uniquely "
            "numbered pair of files under data/debug/auth/ so the screens of "
            "the authentication journey do not overwrite each other. Walk the "
            "journey by hand and press ENTER on each screen; type a label "
            "first to name that capture."
        ),
    )
    p_inspect_auth.add_argument("--url", help="start here instead of the HSC home page")
    p_inspect_auth.add_argument(
        "--limit", type=int, default=40, help="elements printed per dump"
    )
    p_inspect_auth.set_defaults(
        func=cmd_inspect,
        needs_browser=True,
        debug_subdir="auth",
        numbered=True,
        screenshot=True,
    )

    p_shot = sub.add_parser("screenshot", help="save a screenshot + element dump")
    p_shot.add_argument("--url", help="navigate here first")
    p_shot.add_argument("--name", default="screenshot", help="artifact name")
    p_shot.add_argument(
        "-w", "--wait", action="store_true", help="wait for ENTER before capturing"
    )
    p_shot.set_defaults(func=cmd_screenshot, needs_browser=True)

    p_test = sub.add_parser(
        "test-step",
        help="validate one selector, running its prerequisites first",
        description=(
            "Executes the prerequisite chain configured under `steps:` in "
            "flow.yaml, stops before touching the target, then validates it."
        ),
    )
    p_test.add_argument("selector", help="dotted selector key, e.g. login.key_file")
    p_test.add_argument("--click", action="store_true",
                        help="click the target after it validates")
    p_test.add_argument("--value", help="runtime text for a DYNAMIC target selector")
    p_test.add_argument(
        "--service-center",
        help="service centre ID (or configured name) for a DYNAMIC prerequisite",
    )
    p_test.add_argument("--url", help="start here instead of the configured start_url")
    p_test.add_argument("--timeout", type=int, help="resolution timeout in ms")
    p_test.add_argument(
        "--manual-prepare",
        action="store_true",
        help="do not run prerequisites; navigate by hand and press ENTER",
    )
    p_test.add_argument(
        "--no-wait",
        action="store_true",
        help="with --manual-prepare, skip the ENTER prompt",
    )
    p_test.set_defaults(func=cmd_test_step, needs_browser=True)

    p_check = sub.add_parser(
        "check-center",
        help="check whether one service centre is currently available",
        description=(
            "Runs the prerequisite chain configured for department.search, types "
            "the service centre ID into the search box and reports whether the "
            "centre's button is enabled. Nothing is clicked without --click."
        ),
    )
    p_check.add_argument(
        "service_center_id",
        metavar="SERVICE_CENTER_ID",
        help="service centre ID from config/service_centers.yaml, e.g. 3242",
    )
    p_check.add_argument(
        "--click",
        action="store_true",
        help="select the centre when it is available, then stop on the next screen",
    )
    p_check.add_argument("--url", help="start here instead of the configured start_url")
    p_check.set_defaults(func=cmd_check_center, needs_browser=True)

    p_avail = sub.add_parser(
        "check-availability",
        help="scan 1-5 service centres for free dates and times",
        description=(
            "For each configured centre: opens the wizard, reads every enabled "
            "date and the free times on it, and prints the result. Reads only — "
            "no time is ever selected and nothing is booked or submitted."
        ),
    )
    p_avail.add_argument(
        "--center",
        dest="centers",
        action="append",
        metavar="SERVICE_CENTER_ID",
        help=(
            "scan this centre instead of the enabled ones in "
            "config/service_centers.yaml; repeat for up to 5"
        ),
    )
    p_avail.add_argument("--url", help="start here instead of the configured start_url")
    p_avail.set_defaults(func=cmd_check_availability, needs_browser=True)

    p_api = sub.add_parser(
        "api-probe",
        help="DIAGNOSTIC: call the HSC JSON API directly with the browser session",
        description=(
            "Experiment, not production. Authenticates with the normal "
            "AuthManager, stops at /cabinet without clicking the queue, copies "
            "the browser's hsc.gov.ua cookies into an isolated requests.Session "
            "and issues one GET. Reads only: nothing is booked, no cookie value "
            "is ever printed, and no cookie is written back into the browser. "
            "Only URLs under https://eqn.hsc.gov.ua are accepted."
        ),
    )
    api_target = p_api.add_mutually_exclusive_group()
    api_target.add_argument(
        "--url",
        metavar="PATH",
        help=(
            "path (or full eqn.hsc.gov.ua URL) to GET; defaults to the measured "
            "/api/v2/equeue/departments?serviceId=47"
        ),
    )
    api_target.add_argument(
        "--sequence",
        action="store_true",
        help=(
            "run every measured request in api/endpoints.py in order, stopping "
            "at the first one that does not return JSON"
        ),
    )
    p_api.add_argument(
        "--items",
        type=int,
        default=DEFAULT_ITEMS,
        help=f"JSON records to print per response (default {DEFAULT_ITEMS})",
    )
    p_api.set_defaults(func=cmd_api_probe, needs_browser=True)

    p_api_observe = sub.add_parser(
        "api-observe",
        help="DIAGNOSTIC: log the HSC /api/ calls the page makes while you click",
        description=(
            "Authenticates, then attaches a passive listener and waits. Every "
            "eqn.hsc.gov.ua /api/ response is reported as method, path, status "
            "and content type, with sensitive query values redacted. Headers, "
            "cookies, tokens and bodies are never recorded. Use it to discover "
            "the real date/time endpoints instead of guessing them."
        ),
    )
    p_api_observe.set_defaults(func=cmd_api_observe, needs_browser=True)

    p_api_avail = sub.add_parser(
        "api-availability",
        help="DIAGNOSTIC: read one centre's free dates and times through the API",
        description=(
            "Live validation of the measured endpoints, beside the UI scanner "
            "rather than instead of it. Authenticates, stops at /cabinet, "
            "bridges the browser cookies into one requests.Session and walks "
            "departments -> days -> slots for a single centre, resolving the "
            "visible centre number to the API's internal department id from the "
            "response itself. GET only: no wizard click, no date or time "
            "selection, no booking. `check-availability` and `monitor` are "
            "unaffected."
        ),
    )
    p_api_avail.add_argument(
        "--center",
        required=True,
        metavar="SERVICE_CENTER_ID",
        help="service centre ID from config/service_centers.yaml, e.g. 3242",
    )
    p_api_avail.add_argument(
        "--open-queue",
        action="store_true",
        help=(
            "before bridging the cookies, navigate once to the queue page to see "
            "whether that is what creates the API's session (no wizard control "
            "is clicked)"
        ),
    )
    p_api_avail.add_argument(
        "--max-dates",
        type=int,
        default=0,
        help="query at most this many of the returned dates (0 = every one)",
    )
    p_api_avail.add_argument(
        "--slot-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "minimum seconds between two dates' slots requests "
            "(default: api.slot_request_interval_seconds in flow.yaml; 0 disables "
            "pacing). Each date is still requested exactly once."
        ),
    )
    p_api_avail.set_defaults(func=cmd_api_availability, needs_browser=True)

    p_api_monitor = sub.add_parser(
        "api-monitor",
        help="DIAGNOSTIC: poll 1-5 centres through the API and print changes",
        description=(
            "Dry run only. Authenticates once, opens the queue page once to mint "
            "the API session, then polls departments -> days -> slots for the "
            "chosen centres and prints what changed since the previous scan. "
            "Nothing is booked, nothing is sent to Telegram and nothing is "
            "written to disk. A centre whose read was refused or incomplete "
            "keeps its previous availability instead of being reported as empty. "
            "`monitor` and `check-availability` are unaffected."
        ),
    )
    p_api_monitor.add_argument(
        "--center",
        dest="centers",
        action="append",
        metavar="SERVICE_CENTER_ID",
        help=(
            "watch this centre instead of the enabled ones in "
            "config/service_centers.yaml; repeat for up to 5"
        ),
    )
    p_api_monitor.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "seconds between scan starts (default: api.monitor_interval_seconds "
            "in flow.yaml)"
        ),
    )
    p_api_monitor.add_argument(
        "--slot-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="seconds between slot requests (default: api.slot_request_interval_seconds)",
    )
    p_api_monitor.add_argument(
        "--once", action="store_true", help="run a single scan and exit"
    )
    p_api_monitor.set_defaults(func=cmd_api_monitor, needs_browser=True)

    p_refresh = sub.add_parser(
        "refresh-session",
        help="LOCAL: authenticate, mint the queue session and store it encrypted",
        description=(
            "The browser half of the split, and the only command that needs the "
            "signing key. Opens Chromium, authenticates through ID.GOV.UA's "
            "electronic-signature flow, navigates once to the queue page, copies "
            "the cookies into an HTTP session, writes it to MongoDB encrypted "
            "and closes the browser. It reads no availability at all — no "
            "departments, no days, no slots — and books nothing. Run it whenever "
            "a scheduled `monitor-once` reports AUTH REQUIRED."
        ),
    )
    p_refresh.set_defaults(func=cmd_refresh_session, needs_browser=True)

    p_once = sub.add_parser(
        "monitor-once",
        help="HEADLESS: one availability scan from the stored session, no browser",
        description=(
            "The half that runs in GitHub Actions. Loads the encrypted session "
            "from MongoDB, reads departments -> days -> slots once, writes the "
            "refreshed cookies back and exits. It cannot open a browser and "
            "cannot authenticate: a missing, expired or refused session exits "
            "with code 3 (AUTH REQUIRED) and asks for `refresh-session` to be "
            "run locally. Exit codes: 0 scanned, 2 configuration, 3 session "
            "refresh required, 4 persistence unreadable."
        ),
    )
    p_once.add_argument(
        "--center",
        dest="centers",
        action="append",
        metavar="SERVICE_CENTER_ID",
        help=(
            "scan this centre instead of the enabled ones in "
            "config/service_centers.yaml; repeat for up to 5"
        ),
    )
    p_once.add_argument(
        "--slot-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="seconds between slot requests (default: api.slot_request_interval_seconds)",
    )
    p_once.set_defaults(func=cmd_monitor_once, needs_browser=False)

    p_init = sub.add_parser(
        "init-config",
        help="discover HSC's service centres and write config/service_centers.yaml",
        description=(
            "Configuration discovery, using the stored session and one request: "
            "GET /api/v2/equeue/departments?serviceId=47. It calls no other "
            "endpoint, opens no browser and books nothing. Discovered centres "
            "are added disabled — HSC returns centres across the whole country — "
            "and centres you already enabled stay enabled. A centre HSC no "
            "longer returns is retained, never silently deleted. Nothing is "
            "written unless the whole catalogue was read."
        ),
    )
    p_init.add_argument(
        "--output",
        metavar="PATH",
        help="write here instead of <config-dir>/service_centers.yaml",
    )
    p_init.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch, parse and print the summary without touching the file",
    )
    p_init.add_argument(
        "--force",
        action="store_true",
        help=(
            "rebuild the discovered entries from the API alone, which also "
            "resets `enabled` to false; centres HSC did not return are still kept"
        ),
    )
    p_init.set_defaults(func=cmd_init_config, needs_browser=False)

    p_telegram = sub.add_parser(
        "telegram-test",
        help="send one test message to every configured Telegram recipient",
        description=(
            "Transport check, and nothing else: it sends one message to each id "
            "in TELEGRAM_USERS and reports what happened. No database, no HSC "
            "request, no browser and no session are involved, so when this fails "
            "the thing that failed is Telegram. Exits non-zero if any recipient "
            "did not receive the message — a 403 usually means that person has "
            "not opened the bot and pressed Start."
        ),
    )
    p_telegram.set_defaults(func=cmd_telegram_test, needs_browser=False)

    p_flow = sub.add_parser("flow", help="run the configured flow step by step")
    p_flow.add_argument("--auto", action="store_true", help="do not wait for ENTER")
    p_flow.add_argument("--from", help="start at this step, e.g. category.category_a")
    p_flow.add_argument("--no-login", action="store_true", help="skip the login step")
    p_flow.add_argument("--pause", action="store_true", help="pause after every step")
    p_flow.add_argument(
        "--service-center", help="service centre ID for the department step, e.g. 3242"
    )
    p_flow.set_defaults(func=cmd_flow, needs_browser=True)

    p_monitor = sub.add_parser("monitor", help="poll for available slots")
    p_monitor.add_argument(
        "--dry-run", action="store_true", help="print notifications instead of sending them"
    )
    p_monitor.add_argument("--once", action="store_true", help="run a single cycle and exit")
    p_monitor.set_defaults(func=cmd_monitor, needs_browser=True)

    p_sel = sub.add_parser("selectors", help="show configured and TODO selectors")
    p_sel.set_defaults(func=cmd_selectors, needs_browser=False)

    p_steps = sub.add_parser("steps", help="list flow steps")
    p_steps.set_defaults(func=cmd_steps, needs_browser=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Keep step output interleaved correctly with log/error output when the
    # session is piped or tee'd to a file.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    args = build_parser().parse_args(argv)

    if args.pwdebug:
        # Must be set before Playwright starts.
        os.environ["PWDEBUG"] = "1"

    try:
        if args.needs_browser:
            exit_code: int = asyncio.run(args.func(args))
            return exit_code
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except (LocatorNotFound, LocatorAmbiguous) as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return 1
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
