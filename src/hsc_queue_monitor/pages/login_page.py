"""The signed-out HSC page and the ID.GOV.UA MasterKey journey.

Rules that must not be relaxed:

* the .dat file is read from ``IDGOV_SIGNING_KEY_PATH`` and never copied into the repo;
* the password is passed straight to ``fill()`` and never logged;
* the key provider (КНЕДП) is selected *before* the .dat is uploaded — it is
  what decides how ID.GOV.UA interprets the file;
* a CAPTCHA or anti-bot interstitial stops automation and waits for a human;
* the web-signature component is never emulated — if ID.GOV.UA says it is
  missing, authentication stops and says so.

The journey is a sequence of explicit steps, one method per screen, so a broken
selector names the screen it belongs to instead of failing as one opaque click.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

from playwright.async_api import FileChooser, Locator, Page

from ..browser.auth_observer import AuthObserver
from ..browser.diagnostics import AuthArtifacts, Diagnostics
from ..browser.native_files import NativeFileSelector
from ..config import SelectorRegistry
from ..models import (
    AuthenticationFailed,
    AuthenticationProcessingTimeout,
    AuthIntermediateScreenReached,
    ChallengeDetected,
    FlowError,
    HscMonitorError,
    LocatorSpec,
    NativeFileDialogError,
    SignatureExtensionUnavailable,
)
from .base_page import BasePage

logger = logging.getLogger(__name__)

#: How often URL / enabled-state conditions are re-checked. Matches BasePage.
_POLL_INTERVAL_MS = 250

#: Consecutive polls the key form must be back, unchanged and clickable before
#: the attempt is called reset. Three polls (~0.75s) is long enough that a
#: mid-render frame cannot trigger it, short enough to beat any callback.
_STABLE_POLLS_FORM_RETURNED = 3

#: A screen we have never seen has to hold still for longer before automation
#: declares it a destination rather than a step on the way to one.
_STABLE_POLLS_NEW_SCREEN = 8

#: The identity provider the HSC login button hands over to.
IDGOV_HOST_SUFFIX = "id.gov.ua"

#: Everything needed to reason about the КНЕДП dropdown, read in one round trip:
#: that it really is a ``<select>``, what it offers, and what is chosen now.
_SELECT_STATE_JS = """
(el) => ({
  tag: el.tagName.toLowerCase(),
  options: Array.from(el.options || []).map(
    (option) => (option.textContent || '').replace(/\\s+/g, ' ').trim()
  ),
  selected: Array.from(el.selectedOptions || []).map(
    (option) => (option.textContent || '').replace(/\\s+/g, ' ').trim()
  ),
})
"""


#: Asks the operator something and returns whatever they typed. Injected so a
#: page object never reads stdin itself, and so tests can answer instantly.
type Prompt = Callable[[str], Awaitable[str]]

#: Answers "is there a password in there?" and nothing else. The comparison
#: happens in the browser so the value never crosses into this process, where
#: it could end up in a traceback, a repr or a log record by accident.
_PASSWORD_PRESENT_JS = "(el) => (el.value || '').length > 0"

#: Identifies the input a file chooser was opened for, so the key can never be
#: handed to the certificates input that shares the screen with it.
_CHOOSER_TARGET_JS = """
(el, selector) => ({ id: el.id || '', matches: el.matches(selector) })
"""


def host_of(url: str) -> str:
    """Hostname of a URL, lowercased. Empty for ``about:blank`` and friends."""
    return (urlsplit(url).hostname or "").lower()


def is_idgov_url(url: str) -> bool:
    host = host_of(url)
    return host == IDGOV_HOST_SUFFIX or host.endswith(f".{IDGOV_HOST_SUFFIX}")


class LoginPage(BasePage):
    # HSC, signed out
    TERMS = "login.terms"
    IDGOV = "login.idgov"
    # ID.GOV.UA
    ELECTRONIC_SIGNATURE = "login.electronic_signature"
    FILE_TAB = "login.file_tab"
    PROVIDER = "login.provider"
    KEY_FILE_TRIGGER = "login.key_file_trigger"
    KEY_FILE = "login.key_file"
    KEY_LOADED = "login.key_loaded"
    PROCESSING = "login.processing"
    PASSWORD = "login.password"
    SUBMIT = "login.submit"
    USER_DATA_ACCEPT = "login.user_data_accept"
    USER_DATA_SCREEN = "login.user_data_screen"
    SIGNATURE_UNAVAILABLE = "login.signature_unavailable"
    AUTH_ERROR = "login.auth_error"
    # HSC, signed in
    AUTHENTICATED = "login.authenticated_marker"
    CHALLENGE = "login.challenge"

    def __init__(
        self,
        page: Page,
        selectors: SelectorRegistry,
        *,
        diagnostics: Diagnostics | None = None,
        default_timeout: int = 15_000,
        navigation_timeout: int = 30_000,
        file_selector: NativeFileSelector | None = None,
    ) -> None:
        super().__init__(
            page, selectors, diagnostics=diagnostics, default_timeout=default_timeout
        )
        self.navigation_timeout = navigation_timeout
        #: Answers the operating system's Open dialog. ``None`` means this
        #: machine has no implementation, so Playwright's intercepted file
        #: chooser is used instead — see :meth:`choose_key_file`.
        self.file_selector = file_selector

    # -------------------------------------------------------------- state ---

    @property
    def has_authenticated_marker(self) -> bool:
        """Whether ``login.authenticated_marker`` is configured at all."""
        return self.optional_spec(self.AUTHENTICATED) is not None

    async def authenticated_marker_visible(self, *, timeout: int = 3_000) -> bool:
        """Whether the cabinet-only marker is on screen.

        Never raises: an absent marker is an answer ("no"), not a failure. The
        URL check that goes with it lives in :class:`~..flow.auth.AuthManager`,
        which is what decides whether the session is actually usable.
        """
        marker = self.optional_spec(self.AUTHENTICATED)
        if marker is None:
            return False
        return await self.is_present(marker, timeout=timeout)

    async def challenge_present(self) -> bool:
        marker = self.optional_spec(self.CHALLENGE)
        if marker is None:
            return False
        return await self.is_present(marker, timeout=1_500)

    async def signature_component_unavailable(self) -> bool:
        marker = self.optional_spec(self.SIGNATURE_UNAVAILABLE)
        if marker is None:
            return False
        return await self.is_present(marker, timeout=1_500)

    async def require_signature_component(self, screen: str) -> None:
        """Stop the journey if ID.GOV.UA reports a missing signing component.

        Deliberately has no fallback: switching to another authentication
        method behind the user's back would hide a problem they have to fix in
        the browser profile anyway.
        """
        if not await self.signature_component_unavailable():
            return

        detail = ""
        texts = await self.texts(self.SIGNATURE_UNAVAILABLE)
        if texts:
            detail = texts[0]

        if self.diagnostics is not None:
            await self.diagnostics.screenshot(self.page, f"auth-signature-unavailable-{screen}")
            await self.diagnostics.dump_elements(
                self.page, f"auth-signature-unavailable-{screen}-elements"
            )
        raise SignatureExtensionUnavailable(detail)

    # ------------------------------------------------------- HSC, signed out --

    async def open_entry_page(self, url: str) -> None:
        """Open the public HSC page the signed-out journey starts from."""
        logger.info("Opening %s", url)
        await self.page.goto(
            url, wait_until="domcontentloaded", timeout=self.navigation_timeout
        )
        await self.wait_stable()

    async def accept_service_terms(self) -> None:
        """Tick "Я ознайомлений…" — but only when it has not been ticked yet.

        The checkbox is a custom control whose state is not readable directly.
        The ID.GOV.UA button being disabled *is* that state, so it is used both
        as the condition and as the confirmation. Clicking unconditionally
        would toggle an already-accepted box back off.
        """
        button = await self.resolve(self.IDGOV)
        if await button.is_enabled():
            logger.info("Service terms are already accepted.")
            return

        logger.info("Accepting the service terms.")
        await self.click(self.TERMS, step="login.terms")
        await self._wait_until_enabled(
            button,
            self.IDGOV,
            hint=f"Check that {self.TERMS} really ticks the consent checkbox.",
        )

    async def go_to_idgov(self) -> None:
        """Hand over to ID.GOV.UA and wait for the browser to actually get there."""
        logger.info("Continuing to ID.GOV.UA.")
        await self.click(self.IDGOV, step="login.idgov")
        await self._wait_for_url(
            is_idgov_url, timeout=self.navigation_timeout, what="the ID.GOV.UA login page"
        )
        logger.info("ID.GOV.UA login page reached.")

    # -------------------------------------------------------- ID.GOV.UA -----

    async def select_electronic_signature(self) -> None:
        """Choose "Електронного підпису" on the authentication-method screen."""
        await self.require_signature_component("method")
        logger.info("Selecting the electronic-signature authentication method.")
        await self.click(self.ELECTRONIC_SIGNATURE, step="login.electronic_signature")

    async def select_file_key(self) -> None:
        """Choose "Файловий" — a key file rather than a hardware token."""
        await self.require_signature_component("signature")
        logger.info("Selecting the file-based key medium.")
        await self.click(self.FILE_TAB, step="login.file_tab")

    async def select_key_provider(self, provider: str) -> None:
        """Choose the КНЕДП that issued the key, by its visible label.

        ID.GOV.UA pre-selects «КНЕДП ДПС» and interprets the uploaded .dat
        according to whatever is chosen here, so this must happen *before* the
        file is uploaded, never after.

        The option is picked with ``select_option()`` — the dropdown is never
        clicked open, and an option is never addressed by index: the site is
        free to reorder its provider list, and the name is the only thing about
        it that identifies the right one.
        """
        await self.require_signature_component("provider")

        select = await self.resolve(self.PROVIDER)
        tag, options, _selected = await self._select_state(select)

        if tag != "select":
            raise FlowError(
                f"{self.PROVIDER} resolved to a <{tag}>, not a <select>.\n"
                "The key provider is chosen with select_option(), so the "
                "selector must point at the dropdown element itself.\n"
                "Re-check it with:  python -m hsc_queue_monitor.cli test-step "
                f"{self.PROVIDER}"
            )

        if provider not in options:
            raise FlowError(
                f"The key provider {provider!r} is not offered by "
                f"{self.PROVIDER}.\n"
                f"{self._describe_options(options)}\n"
                "Set authentication.key_provider in config/flow.yaml to the "
                "option text exactly as ID.GOV.UA shows it."
            )

        logger.info("Selecting key provider: %s", provider)
        await self.select_label(self.PROVIDER, provider, step="login.provider")

        _tag, _options, selected = await self._select_state(select)
        if selected != [provider]:
            raise FlowError(
                f"{self.PROVIDER} still shows {selected or ['(nothing)']} after "
                f"selecting {provider!r}.\n"
                "The key was NOT uploaded: a provider the page did not accept "
                "would make it read the .dat file the wrong way."
            )

    async def confirm_manual_provider(self, provider: str, prompt: Prompt) -> None:
        """Diagnostic A/B step: let a *person* work the КНЕДП dropdown.

        ID.GOV.UA wraps ``#CAsServersSelect`` in jquery.nice-select, so the
        native element the automation drives is not the control a user touches.
        ``select_option()`` demonstrably leaves the right value in the DOM — the
        open question is whether the wrapper's own handlers, which a real click
        would fire, are what the signing code actually reads.

        This method answers that by changing exactly one variable: a human
        picks the provider, everything else stays automated. It is never used
        by ``ensure-auth`` — :meth:`select_key_provider` remains the production
        path and is deliberately not called here.
        """
        await self.require_signature_component("provider")

        select = await self.resolve(self.PROVIDER)
        tag, options, selected = await self._select_state(select)
        logger.info("Current provider before the manual step: %s", selected or ["(none)"])

        logger.warning(
            "\n"
            "================================================================\n"
            " MANUAL ACTION REQUIRED:\n"
            " Open the visible КНЕДП dropdown in the browser and manually select:\n"
            f" {provider}\n"
            "\n"
            " Use the dropdown the page shows, not the developer tools.\n"
            " Nothing is being clicked for you — that is the point of this run.\n"
            "================================================================"
        )
        await prompt("Press ENTER here when done: ")

        tag, options, selected = await self._select_state(select)
        if tag != "select":  # pragma: no cover - same guard as the automated path
            raise FlowError(f"{self.PROVIDER} resolved to a <{tag}>, not a <select>.")

        if selected != [provider]:
            raise FlowError(
                f"{self.PROVIDER} reads {selected or ['(nothing)']}, not "
                f"{provider!r}.\n"
                f"{self._describe_options(options)}\n"
                "The manual selection did not take, so the run stops here "
                "rather than repeating the previous attempt with a different "
                "provider than intended."
            )

        logger.info("Key provider confirmed from the page: %s", provider)

    async def _select_state(self, locator: Locator) -> tuple[str, list[str], list[str]]:
        """``(tag, option labels, selected labels)`` of a resolved dropdown."""
        state = await locator.evaluate(_SELECT_STATE_JS)
        if not isinstance(state, dict):  # pragma: no cover - defensive
            raise FlowError(f"{self.PROVIDER} could not be inspected: got {state!r}")
        return (
            str(state.get("tag", "")),
            [str(option) for option in state.get("options") or []],
            [str(option) for option in state.get("selected") or []],
        )

    @staticmethod
    def _describe_options(options: list[str]) -> str:
        if not options:
            return "The dropdown is empty — the page may not have finished loading."
        listing = "\n".join(f"  - {option}" for option in options[:30])
        more = "" if len(options) <= 30 else f"\n  … and {len(options) - 30} more"
        return f"It offers:\n{listing}{more}"

    async def submit_master_key(
        self,
        key_path: Path,
        password: str,
        *,
        key_load_timeout_ms: int = 120_000,
        observer: AuthObserver | None = None,
        manual_password: Prompt | None = None,
    ) -> None:
        """Upload the .dat, wait for it to be *accepted*, then type and submit.

        ``key_path`` must already exist — validate it with
        ``Settings.require_key_path()`` before calling.

        The file goes through the page's own upload control (see
        :meth:`choose_key_file`), not into the input behind it.

        Handing a file to the page is not the same as ID.GOV.UA having read
        it: the page loads and parses the key asynchronously, and typing into a
        password field that is still bound to no key produces a submit that
        does nothing at all. Each step therefore waits for the state the
        previous one is supposed to have produced.
        """
        if not key_path.is_file():
            raise FlowError(f"MasterKey file not found: {key_path}")

        await self.require_signature_component("file")

        await self.choose_key_file(key_path)
        await self.wait_for_key_loaded(key_load_timeout_ms)

        if manual_password is None:
            await self.fill(self.PASSWORD, password, secret=True, step="login.password")
        else:
            # Diagnostic mode only. Nothing is filled: the operator's keystrokes
            # are the variable being tested, so IDGOV_SIGNING_KEY_PASSWORD stays unused.
            await self.confirm_manual_password(manual_password)

        await self.submit_authentication(observer)

    async def choose_key_file(self, key_path: Path) -> None:
        """Hand the .dat over the way ID.GOV.UA actually accepts it.

        Three mechanisms have been tried against the live site, and only the
        last one authenticates:

        1. ``set_input_files()`` on ``#PKeyFileInput`` — key read, form resets;
        2. clicking the visible control and answering Playwright's intercepted
           ``FileChooser`` — identical result;
        3. clicking the visible control and choosing the file in the operating
           system's own Open dialog — proceeds to signer information.

        So the click is the same in both branches; what differs is who answers
        the dialog. With a :class:`NativeFileSelector` the browser is left to
        open its real picker and the OS is driven — critically *without* arming
        ``expect_file_chooser()``, because listening for the chooser is what
        makes Playwright intercept it and suppress the native panel.
        """
        if self.file_selector is not None:
            await self._choose_key_file_natively(key_path, self.file_selector)
            return

        logger.info("Opening ID.GOV.UA key file chooser")
        try:
            async with self.page.expect_file_chooser(
                timeout=self.default_timeout
            ) as chooser_info:
                await self.click(self.KEY_FILE_TRIGGER, step="login.key_file_trigger")
            chooser = await chooser_info.value
        except HscMonitorError:
            # A selector problem already explains itself; don't relabel it as
            # "the page never asked for a file".
            raise
        except Exception as exc:
            artifacts = await self.capture_snapshot("auth-no-file-chooser")
            raise FlowError(
                f"Clicking {self.KEY_FILE_TRIGGER} did not make ID.GOV.UA ask "
                "for a file.\n"
                "Nothing was uploaded. The file is deliberately NOT written "
                "into the input directly as a fallback: that is the exact "
                "behaviour this step exists to avoid, and it fails later, at "
                "the point where nothing explains it any more.\n"
                "Check that the selector still matches the visible "
                "«оберіть його на своєму носієві» control.\n"
                f"{artifacts}"
            ) from exc

        logger.info("ID.GOV.UA requested key file selection")
        await self._verify_chooser_target(chooser)

        logger.info("Selecting key file (%s)", key_path.name)
        await chooser.set_files(str(key_path))

    async def _choose_key_file_natively(
        self, key_path: Path, selector: NativeFileSelector
    ) -> None:
        """Click the site's control, then answer the OS dialog it opens.

        No ``expect_file_chooser()`` anywhere near this: arming that listener
        would make Playwright swallow the chooser, and the native panel this
        depends on would never appear.
        """
        logger.info("Opening ID.GOV.UA key file chooser")
        await self.click(self.KEY_FILE_TRIGGER, step="login.key_file_trigger")

        logger.info("Selecting key file (%s)", key_path.name)
        try:
            await selector.select_file(key_path)
        except NativeFileDialogError as exc:
            # Deliberately no in-page fallback: the whole point of this step is
            # that the other two mechanisms do not work.
            artifacts = await self.capture_snapshot("auth-native-file-dialog")
            raise FlowError(
                f"The key file could not be selected in the system dialog.\n\n"
                f"{exc}\n\n"
                "Nothing was uploaded, and the file was NOT written into the "
                "input as a fallback — ID.GOV.UA does not accept keys that "
                "arrive that way, so it would only fail later and less "
                "clearly.\n"
                f"{artifacts}"
            ) from exc

    async def _verify_chooser_target(self, chooser: FileChooser) -> None:
        """Refuse a chooser that belongs to any input but the private key one.

        The screen carries ``#ChoosePKCertsInput`` as well, and a key sent to
        the certificates input would be accepted by the widget and then quietly
        do nothing useful.
        """
        spec = self.spec(self.KEY_FILE)
        if spec.strategy != "css" or not spec.value:  # pragma: no cover - config guard
            logger.warning(
                "%s is not a CSS selector, so the file chooser could not be "
                "checked against it.",
                self.KEY_FILE,
            )
            return

        try:
            element = chooser.element
            state = await element.evaluate(_CHOOSER_TARGET_JS, spec.value)
        except Exception:  # pragma: no cover - handle unavailable
            logger.warning("The file chooser did not expose its input; not verified.")
            return

        if not isinstance(state, dict) or not state.get("matches"):
            element_id = ""
            if isinstance(state, dict):
                element_id = str(state.get("id") or "")
            artifacts = await self.capture_snapshot("auth-wrong-file-input")
            raise FlowError(
                "ID.GOV.UA opened a file chooser for "
                f"{('#' + element_id) if element_id else 'an unidentified input'}, "
                f"not for {spec.value}.\n"
                "Nothing was uploaded. The screen also holds "
                "#ChoosePKCertsInput (certificates), and the private key must "
                "never be handed to it.\n"
                f"Check what {self.KEY_FILE_TRIGGER} actually activates.\n"
                f"{artifacts}"
            )

        logger.debug("File chooser belongs to %s", spec.value)

    async def confirm_manual_password(self, prompt: Prompt) -> None:
        """Diagnostic A/B step: let a *person* type the MasterKey password.

        ``IDGOV_SIGNING_KEY_PASSWORD`` is not filled and :meth:`BasePage.fill` is not
        called — whether the value arrives through ``fill()`` or through a
        keyboard is the single variable this run changes.

        The field is checked for *presence only*, and the check runs in the
        browser: :data:`_PASSWORD_PRESENT_JS` returns a boolean, so the typed
        value never enters this process and cannot reach a log, an exception or
        an artifact by any route.
        """
        field = await self.resolve(self.PASSWORD)

        logger.warning(
            "\n"
            "================================================================\n"
            " MANUAL ACTION REQUIRED:\n"
            "\n"
            " Enter the MasterKey password manually in the browser's\n"
            ' "Пароль" field.\n'
            "\n"
            ' Do NOT press "Продовжити" — this run clicks it for you.\n'
            "\n"
            " Nothing is typed for you, and nothing you type is read back.\n"
            "================================================================"
        )
        await prompt(
            "Press ENTER in this terminal when the password has been entered: "
        )

        if not bool(await field.evaluate(_PASSWORD_PRESENT_JS)):
            raise FlowError(
                f"{self.PASSWORD} is still empty, so nothing was submitted.\n"
                "Type the password into the browser's «Пароль» field first, "
                "then press ENTER."
            )

        # Deliberately says nothing about the value — only that there is one.
        logger.info("A password is present in the field (its value is never read)")

    async def wait_for_key_loaded(self, timeout_ms: int) -> None:
        """Block until the file-key UI shows that the .dat was accepted.

        A condition poll, never a sleep. Nothing downstream — not the password,
        not the submit — may run until this is satisfied, so a key ID.GOV.UA
        never read fails here with the screen saved, instead of two minutes
        later as an unexplained callback timeout.
        """
        if await self.is_present(self.KEY_LOADED, timeout=timeout_ms):
            logger.info("MasterKey file loaded")
            return

        artifacts = await self.capture_snapshot("auth-key-not-loaded")
        raise FlowError(
            f"ID.GOV.UA did not accept the MasterKey file within "
            f"{timeout_ms // 1000}s: {self.KEY_LOADED} never appeared.\n"
            "The file was handed to the upload input, but the page never "
            "reported a loaded key, so the password was NOT typed and nothing "
            "was submitted.\n"
            "Usual causes: the .dat is not a key this provider issued, the "
            "wrong КНЕДП is configured in authentication.key_provider, or the "
            "web-signature component failed to read the file.\n"
            f"{artifacts}"
        )

    async def submit_authentication(self, observer: AuthObserver | None = None) -> None:
        """Click "Продовжити" once it is really clickable, and prove it landed.

        "The selector resolved" is not readiness: ID.GOV.UA renders the button
        before it is usable. Visibility and the enabled state are both waited
        for, and the click is followed by an explicit completion line — a click
        that returned is evidence about the click, never about the session.
        """
        submit = await self.resolve(self.SUBMIT)
        await self._wait_until_visible(submit, self.SUBMIT)
        await self._wait_until_enabled(
            submit,
            self.SUBMIT,
            hint=(
                "The key and password are entered, so the site should have "
                "enabled it. Check the artifacts for what it is waiting for."
            ),
        )

        if observer is not None:
            # The baseline the post-submit watcher compares against: this is
            # what the key form looks like when nothing has happened yet.
            observer.phase = "submit"
            await observer.capture_text()

        url_before = self.page.url
        logger.info("Submitting authentication")
        await self.click(self.SUBMIT, step="login.submit")

        logger.info(
            "login.submit click completed (url: %s -> %s, submit now: %s)",
            url_before,
            self.page.url,
            await self._enabled_state(submit),
        )

    @staticmethod
    async def _enabled_state(locator: Locator) -> str:
        """``enabled`` / ``disabled`` / ``gone`` — never raises.

        Read only to describe what the click did; the page is allowed to
        navigate out from under it.
        """
        try:
            return "enabled" if await locator.is_enabled() else "disabled"
        except Exception:  # pragma: no cover - element detached by navigation
            return "gone"

    # ------------------------------------------------------------ journey ---

    async def authenticate_with_master_key(
        self,
        key_path: Path,
        password: str,
        provider: str,
        *,
        returns_to: str,
        challenge_timeout_ms: int = 600_000,
        callback_timeout_ms: int = 120_000,
        key_load_timeout_ms: int = 120_000,
        manual_provider: Prompt | None = None,
        manual_password: Prompt | None = None,
    ) -> None:
        """Walk the whole signed-out journey once, ending back on ``returns_to``.

        ``provider`` is the КНЕДП from ``authentication.key_provider``; it is
        passed in rather than read here, so this page object never has to know
        which configuration file anything came from.

        ``manual_provider`` and ``manual_password`` are the diagnostic A/B
        switches and default to off. Each hands exactly one step to the
        operator — the dropdown, or the password field — and leaves every other
        step automated, so a run differs from production in one variable only.

        ``returns_to`` is the host the callback must land on. Nothing here
        verifies the *cabinet* — that is the caller's job, because "we are back
        on the HSC site" and "we have a working session" are different claims.
        """
        if await self.challenge_present():
            await self.wait_for_manual_challenge(challenge_timeout_ms)

        await self.accept_service_terms()
        await self.go_to_idgov()

        # Watching starts here, not at the click: whether changing the provider
        # kicks off its own request is a question about this phase, and the
        # only way to answer it is to have recorded it.
        observer = AuthObserver(self.page)
        observer.start()
        try:
            await self.select_electronic_signature()
            await self.select_file_key()

            # Before the upload, always: the provider decides how the .dat is read.
            observer.phase = "provider"
            if manual_provider is None:
                await self.select_key_provider(provider)
            else:
                # Diagnostic mode only. select_key_provider() is not called —
                # substituting it is the entire experiment.
                await self.confirm_manual_provider(provider, manual_provider)

            observer.phase = "upload"
            await self.submit_master_key(
                key_path,
                password,
                key_load_timeout_ms=key_load_timeout_ms,
                observer=observer,
                manual_password=manual_password,
            )

            await self._wait_for_callback(returns_to, callback_timeout_ms, observer)
        finally:
            logger.debug("ID.GOV.UA phase recorded: %s", observer.summary())
            observer.stop()

        logger.info("ID.GOV.UA authentication completed")

    async def _wait_for_callback(
        self, host: str, timeout_ms: int, observer: AuthObserver
    ) -> None:
        """Watch for whichever post-submit outcome happens first.

        A. the browser is handed back to *host* — the callback landed;
        B. ID.GOV.UA settles on a screen that is not the key form;
        C. a configured error marker appears — fail with what it says;
        D. the key form is back, with the overlay gone — the attempt was reset;
        E. the overlay never clears — processing timed out;
        F. the «Перевірте дані» user-data confirmation screen appears — a known
           continuation of a *successful* signature. It is confirmed exactly
           once, and the loop goes back to waiting for the callback rather than
           calling the screen a destination. Because it is known, it is never
           counted towards B.

        Only the host is compared for A: the callback carries query parameters
        that are none of our business and must never be matched on.

        **The overlay is the whole point of this loop.** ID.GOV.UA reads the
        key with the form still mounted underneath a dimmer, so "the password
        field exists and «Продовжити» is enabled" is true *during* processing
        and says nothing about the outcome. Nothing is classified while
        ``login.processing`` is visible; the screen is only judged once it has
        gone. Every branch is a condition poll, never a sleep.
        """
        logger.info("Waiting for the ID.GOV.UA callback to return to %s", host)
        processing = self.optional_spec(self.PROCESSING)
        error = self.optional_spec(self.AUTH_ERROR)
        deadline = time.monotonic() + timeout_ms / 1000

        # The form as it looked at the moment of the click. Without a
        # configured overlay marker this is the only evidence that anything
        # happened at all, so it stays as the fallback.
        previous_text = observer.last_text
        processing_started = False
        processing_visible = False
        screen_changed = False
        form_back_polls = 0
        other_screen_polls = 0
        # The user-data screen is confirmed once and only once. One that is
        # still there afterwards is a callback that did not arrive, not an
        # invitation to click again.
        user_data_confirmed = False

        while True:
            url = self.page.url

            if host_of(url) == host:  # A — a callback outranks everything
                await self.wait_stable()
                if processing_started:
                    logger.info("ID.GOV.UA key processing completed")
                logger.info("Authentication callback received")
                return

            text = await observer.capture_text()
            if text is not None and text != previous_text:
                previous_text = text
                screen_changed = True
                form_back_polls = other_screen_polls = 0

            # timeout=0 is a single non-blocking probe: this loop must keep
            # watching the URL, not stall on a marker that is not there.
            busy = processing is not None and await self.is_present(processing, timeout=0)

            if busy:
                if not processing_started:
                    processing_started = True
                    observer.enter_phase("processing")
                    logger.info("ID.GOV.UA is reading the private key")
                    logger.info("Waiting for key processing to complete")
                processing_visible = True
                form_back_polls = other_screen_polls = 0

                if time.monotonic() >= deadline:  # E
                    await self._fail_on_processing_timeout(timeout_ms, observer)

                # Nothing below may run while the overlay is up: the form under
                # it is not an answer, it is scenery.
                await asyncio.sleep(_POLL_INTERVAL_MS / 1000)
                continue

            if processing_visible:
                processing_visible = False
                logger.info("ID.GOV.UA key processing completed")

            if (
                error is not None
                and is_idgov_url(url)
                and await self.is_present(error, timeout=0)
            ):  # C
                await self._fail_on_idgov_error(error, observer)

            if await self._user_data_screen_present():  # F
                # A known screen, so neither of the verdicts below applies to
                # it: it is not the key form coming back, and it is not an
                # unknown destination to stop on.
                form_back_polls = other_screen_polls = 0
                if not user_data_confirmed:
                    user_data_confirmed = True
                    await self._confirm_user_data(observer)
            elif await self._key_form_present():
                other_screen_polls = 0
                # Enabled *and* unobscured: the site handed the form back
                # rather than still working behind it.
                if self._reset_is_possible(
                    processing, processing_started, screen_changed
                ) and await self._submit_is_enabled():
                    form_back_polls += 1
                    if form_back_polls >= _STABLE_POLLS_FORM_RETURNED:  # D
                        await self._fail_on_form_reset(observer)
            else:
                screen_changed = True
                form_back_polls = 0
                other_screen_polls += 1
                if is_idgov_url(url) and other_screen_polls >= _STABLE_POLLS_NEW_SCREEN:
                    await self._stop_on_intermediate_screen(observer)  # B

            if time.monotonic() >= deadline:
                await self._fail_on_callback_timeout(
                    url, host, timeout_ms, observer, user_data_confirmed
                )

            await asyncio.sleep(_POLL_INTERVAL_MS / 1000)

    @staticmethod
    def _reset_is_possible(
        processing: LocatorSpec | None, processing_started: bool, screen_changed: bool
    ) -> bool:
        """Whether a returned key form is allowed to mean "reset" yet.

        With the overlay marker configured, processing must have been *seen*
        first: a form that never went busy is a submit that did nothing, and
        calling that a rejection is the false positive this exists to prevent.
        Without the marker there is only the weaker "something re-rendered"
        signal, which is better than nothing but never as good.
        """
        return processing_started if processing is not None else screen_changed

    async def _key_form_present(self) -> bool:
        """Whether the file-key form — all three controls — is on screen.

        The combination is the identity of that screen: the password field
        alone appears on other screens, and a loaded key alone does not mean
        the form is still being offered.
        """
        for key in (self.KEY_LOADED, self.PASSWORD, self.SUBMIT):
            if not await self.is_present(key, timeout=0):
                return False
        return True

    async def _submit_is_enabled(self) -> bool:
        try:
            return await (await self.resolve(self.SUBMIT, timeout=0)).is_enabled()
        except Exception:  # pragma: no cover - mid-render
            return False

    # ------------------------------------------------- user-data screen -----

    async def _user_data_screen_present(self) -> bool:
        """Whether ID.GOV.UA is showing the «Перевірте дані» confirmation.

        Recognised by ``#btnAcceptUserDataAgreement``: an id is identity, and it
        is also the only control this screen is ever clicked on. The heading is
        a second, weaker signal — the site owns its wording — so a disagreement
        between the two is noted and the button still decides.
        """
        if not await self.is_present(self.USER_DATA_ACCEPT, timeout=0):
            return False

        heading = self.optional_spec(self.USER_DATA_SCREEN)
        if heading is not None and not await self.is_present(heading, timeout=0):
            logger.debug(
                "%s is on screen without %s; going by the button.",
                self.USER_DATA_ACCEPT,
                self.USER_DATA_SCREEN,
            )
        return True

    async def _confirm_user_data(self, observer: AuthObserver) -> None:
        """Click «Продовжити» on the user-data screen. Exactly that button.

        Its neighbour ``#btnResetUserDataAgreement`` («Відмовитись») abandons
        the authentication, and both this screen and the file-key form carry a
        «Продовжити», so nothing here may fall back to :attr:`SUBMIT`, to a
        generic button locator, or to a position.

        The screen shows the name, tax number and address read out of the
        certificate. None of it is read here and none of it is logged: the log
        identifies the screen, not the person on it.
        """
        logger.info(
            "ID.GOV.UA accepted the key and reached the user-data confirmation screen"
        )
        logger.info("Confirming ID.GOV.UA user data")
        observer.enter_phase("user-data")

        accept = await self.resolve(self.USER_DATA_ACCEPT)
        await self._wait_until_visible(accept, self.USER_DATA_ACCEPT)
        await self._wait_until_enabled(
            accept,
            self.USER_DATA_ACCEPT,
            hint=(
                "The screen was reached with a key ID.GOV.UA had already "
                "accepted, so the confirm button should be usable. Check the "
                "artifacts for what it is waiting for."
            ),
        )
        await self.click(self.USER_DATA_ACCEPT, step="login.user_data_accept")

    # ------------------------------------------------------- outcomes -------

    async def _fail_on_form_reset(self, observer: AuthObserver) -> NoReturn:
        """C: the site processed the submission and put the same form back.

        No cause is inferred. "Wrong password", "wrong provider" and "bad key"
        all look identical from here, and guessing in the message is how a user
        ends up debugging the wrong thing.
        """
        logger.info("Key form returned after processing")
        artifacts = await self._capture_auth(observer, outcome="form-reset")
        logger.info("Post-submit observations: %s", observer.summary())
        raise AuthenticationFailed(
            "ID.GOV.UA processed the key submission but returned to the "
            "file-key form instead of proceeding to signer information.\n"
            "The reading overlay has gone and «Продовжити» is usable again, so "
            "this is the finished state, not work in progress. The callback "
            "was not waited out — nothing was going to arrive.\n"
            "Why it was reset is not guessed here; the artifacts below are the "
            "evidence:\n\n"
            f"Diagnostics:\n{artifacts.describe()}"
        )

    async def _fail_on_processing_timeout(
        self, timeout_ms: int, observer: AuthObserver
    ) -> NoReturn:
        """E: the overlay never cleared, so the key was never finished with.

        Explicitly not a rejection: reading stalled, which is a different
        problem from a key the site refused.
        """
        artifacts = await self._capture_auth(observer, outcome="processing-timeout")
        logger.info("Post-submit observations: %s", observer.summary())
        raise AuthenticationProcessingTimeout(
            f"ID.GOV.UA was still reading the private key after "
            f"{timeout_ms // 1000}s — «{self.PROCESSING}» never went away.\n"
            "The submission was never resolved either way, so nothing is "
            "concluded about the key itself. Raise timeouts.authentication in "
            "config/flow.yaml if signing is simply slow on this machine.\n\n"
            f"Diagnostics:\n{artifacts.describe()}"
        )

    async def _stop_on_intermediate_screen(self, observer: AuthObserver) -> NoReturn:
        """B: further than before, but somewhere with no configured steps."""
        logger.info("ID.GOV.UA advanced to an intermediate authentication screen")
        artifacts = await self._capture_auth(observer, outcome="intermediate-screen")
        logger.info("Post-submit observations: %s", observer.summary())
        raise AuthIntermediateScreenReached(
            "ID.GOV.UA accepted the key submission and moved on to a screen "
            f"this project has no steps for ({self.page.url}).\n"
            "This is further than the file-key form, so the submission itself "
            "worked. Nothing on the new screen was clicked — the element dump "
            "below describes it so the next step can be added deliberately.\n\n"
            f"Diagnostics:\n{artifacts.describe()}"
        )

    async def _fail_on_idgov_error(
        self, error: LocatorSpec, observer: AuthObserver
    ) -> NoReturn:
        """D: ID.GOV.UA said no. Report what it said, once, without retrying."""
        texts = await self.texts(error)
        reported = texts[0] if texts else "(the message could not be read)"
        artifacts = await self._capture_auth(observer, outcome="rejected")
        raise AuthenticationFailed(
            f"ID.GOV.UA rejected the sign-in and stayed on {self.page.url}\n"
            f"It reported: {reported}\n"
            "The callback was not waited out — this is a refusal, not a slow "
            "response.\n\n"
            f"Diagnostics:\n{artifacts.describe()}"
        )

    async def _fail_on_callback_timeout(
        self,
        url: str,
        host: str,
        timeout_ms: int,
        observer: AuthObserver,
        user_data_confirmed: bool = False,
    ) -> NoReturn:
        """Time ran out without any recognisable outcome."""
        artifacts = await self._capture_auth(observer, outcome="callback-timeout")
        logger.info("Post-submit observations: %s", observer.summary())
        if user_data_confirmed:
            # Everything up to and including the signature worked, so the
            # message must not read like a key that was refused.
            detail = (
                "The key was accepted and the user-data confirmation screen was "
                "confirmed, but ID.GOV.UA never handed the browser back "
                "afterwards."
            )
        elif is_idgov_url(url):
            detail = (
                "ID.GOV.UA never handed the browser back and never returned to "
                "the key form either, so it is showing something in between."
            )
        else:
            detail = "The browser is on neither ID.GOV.UA nor the HSC site."
        raise FlowError(
            f"Timed out after {timeout_ms // 1000}s waiting for the "
            f"authentication callback to {host}.\n"
            f"The browser is on: {url}\n"
            f"{detail}\n\n"
            f"Diagnostics:\n{artifacts.describe()}"
        )

    async def _capture_auth(self, observer: AuthObserver, *, outcome: str) -> AuthArtifacts:
        if self.diagnostics is None:
            return AuthArtifacts()
        return await self.diagnostics.capture_post_submit(
            self.page, observer, outcome=outcome
        )


    # ------------------------------------------------------------ waiting ---

    async def _wait_for_url(
        self, predicate: Callable[[str], bool], *, timeout: int, what: str
    ) -> None:
        """Poll ``page.url`` until *predicate* accepts it.

        A condition poll, never a fixed sleep: it returns the moment the
        transition happens and fails loudly if it never does.
        """
        deadline = time.monotonic() + timeout / 1000
        while True:
            url = self.page.url
            if predicate(url):
                await self.wait_stable()
                return
            if time.monotonic() >= deadline:
                raise FlowError(
                    f"Timed out after {timeout // 1000}s waiting for {what}.\n"
                    f"The browser is on: {url}"
                )
            await asyncio.sleep(_POLL_INTERVAL_MS / 1000)

    async def _wait_until_visible(self, locator: Locator, key: str) -> None:
        """Poll until the element is actually on screen.

        Resolution only proves the element matched; a control that is still
        being rendered can be clicked into the void.
        """
        deadline = time.monotonic() + self.default_timeout / 1000
        while True:
            if await locator.is_visible():
                return
            if time.monotonic() >= deadline:
                raise FlowError(
                    f"{key} resolved but never became visible within "
                    f"{self.default_timeout // 1000}s, so it was not clicked."
                )
            await asyncio.sleep(_POLL_INTERVAL_MS / 1000)

    async def _wait_until_enabled(self, locator: Locator, key: str, *, hint: str) -> None:
        """Poll until the element stops being disabled. Never force-clicks."""
        deadline = time.monotonic() + self.default_timeout / 1000
        while True:
            if await locator.is_enabled():
                return
            if time.monotonic() >= deadline:
                raise FlowError(
                    f"{key} is still disabled after "
                    f"{self.default_timeout // 1000}s. {hint}"
                )
            await asyncio.sleep(_POLL_INTERVAL_MS / 1000)

    # ---------------------------------------------------------- challenge ---

    async def wait_for_manual_challenge(self, timeout_ms: int) -> None:
        """Hand control to the user. Never solves anything automatically."""
        marker = self.optional_spec(self.CHALLENGE)
        if marker is None:  # pragma: no cover - guarded by callers
            return

        logger.warning(
            "\n"
            "================================================================\n"
            " A CAPTCHA / browser challenge is on screen.\n"
            " Please solve it MANUALLY in the open Chromium window.\n"
            " Automation is paused and will continue once the page moves on.\n"
            "================================================================"
        )

        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            if not await self.is_present(marker, timeout=1_000):
                logger.info("Challenge cleared; resuming.")
                return

        raise ChallengeDetected(
            "The challenge was still on screen after "
            f"{timeout_ms // 1000}s. Re-run once it is solved — the persistent "
            "profile keeps the session."
        )