"""Command line interface.

python -m hsc_queue_monitor.cli login
python -m hsc_queue_monitor.cli departments --service-id 47
python -m hsc_queue_monitor.cli inspect
python -m hsc_queue_monitor.cli monitor
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .api import ApiError, EndpointNotDiscoveredError, HscApiClient
from .browser import BrowserSession
from .config import Settings, describe
from .logging_setup import setup_logging
from .models import MonitorState
from .monitor import QueueMonitor
from .network_logger import NetworkLogger
from .notifier import build_notifier

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH_REQUIRED = 2


# ---------------------------------------------------------------- arguments
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hsc-queue-monitor",
        description=(
            "Detect (never book) available appointment slots in the Ukrainian HSC "
            "electronic queue using a real, persistent Chromium session."
        ),
    )
    _add_common_flags(parser, default=None)

    # The same global flags are accepted before *and* after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    _add_common_flags(common, default=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser(
        "login", parents=[common], help="Open the browser and authenticate manually"
    )
    _add_headed_flags(login, default_headed=True)
    login.add_argument(
        "--keep-open",
        action="store_true",
        help="Keep the window open after login until you close it or press Ctrl+C",
    )

    departments = sub.add_parser(
        "departments", parents=[common], help="List departments for a service id"
    )
    _add_headed_flags(departments, default_headed=True)
    departments.add_argument("--service-id", type=int, default=None)
    departments.add_argument("--json", action="store_true", help="Print raw JSON records")

    inspect = sub.add_parser(
        "inspect",
        parents=[common],
        help="Capture /api/v2/equeue/ traffic while you navigate manually",
    )
    _add_headed_flags(inspect, default_headed=True)
    inspect.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Stop after N seconds (default: until the window is closed / Ctrl+C)",
    )

    monitor = sub.add_parser("monitor", parents=[common], help="Poll for availability and notify")
    _add_headed_flags(monitor, default_headed=False)
    monitor.add_argument("--service-id", type=int, default=None)
    monitor.add_argument(
        "--department-ids", default=None, help="Comma-separated ids (default: all)"
    )
    monitor.add_argument("--interval", type=float, default=None, help="Polling interval in seconds")
    monitor.add_argument("--date-from", default=None, help="Earliest date to care about")
    monitor.add_argument("--date-to", default=None, help="Latest date to care about")
    monitor.add_argument(
        "--inspect-network",
        action="store_true",
        help="Also record API traffic to data/network-events.jsonl",
    )
    monitor.add_argument("--once", action="store_true", help="Run a single cycle and exit")

    return parser


def _add_common_flags(parser: argparse.ArgumentParser, *, default: Any) -> None:
    parser.add_argument("--log-level", default=default, help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--profile-dir", type=Path, default=default, help="Playwright profile dir")
    parser.add_argument(
        "--data-dir", type=Path, default=default, help="Directory for state/captures"
    )
    parser.add_argument("--env-file", type=Path, default=default, help="Path to a .env file")


def _add_headed_flags(parser: argparse.ArgumentParser, *, default_headed: bool) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        default=None,
        help="Run with a visible window" + (" (default)" if default_headed else ""),
    )
    group.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=None,
        help="Run without a window" + ("" if default_headed else " (default)"),
    )
    parser.set_defaults(_default_headless=not default_headed)


def settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env(env_file=args.env_file)

    headless = args.headless
    if headless is None:
        headless = getattr(args, "_default_headless", settings.headless)

    department_ids = None
    raw_departments = getattr(args, "department_ids", None)
    if raw_departments:
        department_ids = tuple(
            int(chunk) for chunk in raw_departments.replace(";", ",").split(",") if chunk.strip()
        )

    settings = settings.with_overrides(
        log_level=args.log_level.upper() if args.log_level else None,
        profile_dir=args.profile_dir,
        data_dir=args.data_dir,
        headless=headless,
        service_id=getattr(args, "service_id", None),
        department_ids=department_ids,
        poll_interval_seconds=getattr(args, "interval", None),
        date_from=getattr(args, "date_from", None),
        date_to=getattr(args, "date_to", None),
        inspect_network=getattr(args, "inspect_network", None) or None,
    )
    if args.data_dir is not None and args.profile_dir is None:
        settings = settings.with_overrides(profile_dir=args.data_dir / "browser-profile")
    return settings


# ------------------------------------------------------------------ helpers
def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _handle() -> None:
        if not stop.is_set():
            logger.info("Shutdown requested, finishing up…")
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, _handle)


# ----------------------------------------------------------------- commands
async def cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    session = BrowserSession(settings)
    try:
        await session.start()
        authenticated = await session.ensure_authenticated()
        if not authenticated:
            print("Authentication was not completed.", flush=True)
            return EXIT_AUTH_REQUIRED
        print(
            "\nAuthenticated. The session lives in the persistent profile:\n"
            f"  {settings.profile_dir}\n"
            "Never commit or share that directory.\n",
            flush=True,
        )
        if args.keep_open:
            logger.info("Keeping the browser open (Ctrl+C or close the window to exit)")
            await session.wait_until_closed(stop)
        return EXIT_OK
    finally:
        await session.close()


async def cmd_departments(settings: Settings, args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    session = BrowserSession(settings)
    try:
        await session.start()
        if not await session.ensure_authenticated():
            print("Not authenticated. Run `login` first.", flush=True)
            return EXIT_AUTH_REQUIRED

        client = HscApiClient(session.page, settings)
        try:
            departments = await client.get_departments(settings.service_id)
        except ApiError as exc:
            logger.error("Failed to load departments: %s", exc)
            return EXIT_ERROR

        if args.json:
            print(
                json.dumps([d.raw for d in departments], ensure_ascii=False, indent=2), flush=True
            )
        else:
            print(f"\nDepartments for serviceId={settings.service_id}: {len(departments)}\n")
            for department in departments:
                print(f"  {department.describe()}")
            print("", flush=True)
        return EXIT_OK
    finally:
        await session.close()


async def cmd_inspect(settings: Settings, args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    settings.ensure_directories()
    session = BrowserSession(settings)
    net_logger = NetworkLogger(settings.network_events_file)
    try:
        await session.start()
        net_logger.attach(session.context)
        await session.ensure_authenticated()

        print(
            "\n"
            "==================================================================\n"
            " NETWORK INSPECTION MODE\n"
            "------------------------------------------------------------------\n"
            " Walk the booking flow by hand in the open window:\n"
            "   1. pick the service\n"
            "   2. pick a department\n"
            "   3. open the date selection\n"
            "   4. look at the available dates\n"
            "   5. open the time slots\n"
            " Do NOT confirm a booking — only navigate.\n"
            f" Captured calls -> {settings.network_events_file}\n"
            " Sensitive headers/tokens are redacted before writing.\n"
            " Press Ctrl+C (or close the window) when you are done.\n"
            "==================================================================\n",
            flush=True,
        )

        if args.timeout:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session.wait_until_closed(stop), timeout=args.timeout)
        else:
            await session.wait_until_closed(stop)

        await net_logger.detach()
        print("\n" + net_logger.summary() + "\n", flush=True)
        return EXIT_OK
    finally:
        await net_logger.detach()
        await session.close()


async def cmd_monitor(settings: Settings, args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    settings.ensure_directories()
    session = BrowserSession(settings)
    net_logger = NetworkLogger(settings.network_events_file) if settings.inspect_network else None
    state = MonitorState.load(settings.state_file)
    notifier = build_notifier(settings)
    monitor: QueueMonitor | None = None
    try:
        await session.start()
        if net_logger is not None:
            net_logger.attach(session.context)
        if not await session.ensure_authenticated():
            print("Not authenticated. Run `login` first.", flush=True)
            return EXIT_AUTH_REQUIRED

        client = HscApiClient(session.page, settings)
        monitor = QueueMonitor(
            client,
            notifier,
            settings,
            state,
            stop_event=stop,
            reauthenticate=session.ensure_authenticated,
        )
        logger.info("Configuration: %s", describe(settings))

        if args.once:
            try:
                department_ids = await monitor.resolve_departments()
                events = await monitor.check_once(department_ids)
            except EndpointNotDiscoveredError as exc:
                logger.error("%s", exc)
                return EXIT_ERROR
            except ApiError as exc:
                logger.error("Check failed: %s", exc)
                return EXIT_ERROR
            for event in events:
                await notifier.notify(event)
            return EXIT_OK

        await monitor.run()
        return EXIT_OK
    finally:
        if monitor is not None:
            monitor.shutdown()
        else:
            with contextlib.suppress(OSError):
                state.save(settings.state_file)
        if net_logger is not None:
            await net_logger.detach()
        await notifier.close()
        await session.close()


COMMANDS = {
    "login": cmd_login,
    "departments": cmd_departments,
    "inspect": cmd_inspect,
    "monitor": cmd_monitor,
}


async def run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    setup_logging(settings.log_level)
    logger.debug("Settings: %s", describe(settings))
    handler = COMMANDS[args.command]
    return await handler(settings, args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
