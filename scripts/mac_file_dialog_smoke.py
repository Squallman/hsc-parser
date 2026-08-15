#!/usr/bin/env python3
"""Drive one already-open macOS Open panel with the production selector.

A standalone local smoke test. It imports the real
:class:`~hsc_queue_monitor.browser.native_files.MacOSFileSelector` — the same
code authentication uses — and points it at a harmless file, so the sequence

    PathTextField AXValue  →  targeted Return  →  panel closes

can be validated before the real .dat key is anywhere near it. Nothing here
touches Playwright, ID.GOV.UA, or the key.

``--inspect-file-row`` is the diagnostic half. Go to Folder turned out to be
directory navigation only: given a full file path it navigates to the parent
and selects nothing. So that mode navigates to the *directory*, finds the
filename in the list, and describes the cell and row around it — what they are,
what they advertise, and whether their selection state can even be set. It
selects nothing and presses nothing, because how to select a row is exactly the
question it exists to answer.

Usage
-----

1. Open any application's file-open dialog by hand. A browser works:
   open ``data:text/html,<input type=file>`` and click the button. So does
   TextEdit's File ▸ Open.
2. Leave the dialog open.
3. In another terminal:

    python scripts/mac_file_dialog_smoke.py --file ~/Desktop/anything.txt

   or, with nothing to lose, let it make its own throwaway file:

    python scripts/mac_file_dialog_smoke.py

``--delay`` waits before starting, in case running this from a terminal moves
the dialog out of the way. ``--dry-run`` finds and describes the panel without
selecting anything.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hsc_queue_monitor.browser.macos_ax import (
    AxApi,
    ancestors_of,
    find_where,
    role_of,
    search_roots,
    value_of,
)
from hsc_queue_monitor.browser.native_files import MacOSFileSelector
from hsc_queue_monitor.logging_config import setup_logging
from hsc_queue_monitor.models import HscMonitorError

#: What the file list wraps a filename in, innermost first.
ROW_ANCESTOR_ROLES = ("AXCell", "AXRow")
#: What holds the rows.
CONTAINER_ROLES = ("AXOutline", "AXTable")

#: Read for the report. Metadata only — and read, never written.
ELEMENT_ATTRIBUTES = (
    "AXSubrole",
    "AXIdentifier",
    "AXValue",
    "AXEnabled",
    "AXFocused",
    "AXSelected",
    "AXIndex",
)
CONTAINER_ATTRIBUTES = (
    "AXSelectedRows",
    "AXSelectedCells",
    "AXRows",
    "AXVisibleRows",
)


@dataclass(slots=True)
class ElementReport:
    """One element, described. Nothing here writes or acts."""

    label: str
    role: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    settable: dict[str, bool] = field(default_factory=dict)
    attribute_names: list[str] = field(default_factory=list)
    action_names: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"  {self.label}: {self.role or '?'}"]
        for name, value in self.attributes.items():
            settable = self.settable.get(name)
            suffix = "" if settable is None else f"  (settable={settable})"
            lines.append(f"      {name}: {value}{suffix}")
        lines.append(f"      actions:    {', '.join(self.action_names) or '(none)'}")
        lines.append(f"      attributes: {', '.join(self.attribute_names) or '(none)'}")
        return "\n".join(lines)


@dataclass(slots=True)
class FileRowReport:
    """What the panel shows for one filename, and how it is wrapped."""

    basename: str
    found: bool = False
    elements: list[ElementReport] = field(default_factory=list)

    def render(self) -> str:
        if not self.found:
            return (
                f"  No element in the panel has AXValue == {self.basename!r}.\n"
                "  The navigation may not have reached the directory, or the "
                "list is not showing filenames as values."
            )
        return "\n\n".join(element.render() for element in self.elements)


def describe(api: AxApi, element: Any, label: str, attributes: tuple[str, ...]) -> ElementReport:
    """Read an element's metadata. Reads only — no actions, no writes."""
    names = api.attribute_names(element)
    report = ElementReport(
        label=label,
        role=role_of(api, element),
        attribute_names=names,
        action_names=api.action_names(element),
    )
    for name in attributes:
        if name not in names:
            continue
        raw = value_of(api, element) if name == "AXValue" else api.attribute_value(element, name)
        if isinstance(raw, list):
            rendered = f"{len(raw)} element(s)"
        else:
            rendered = "nil" if raw is None else str(raw)
        report.attributes[name] = rendered
        report.settable[name] = api.is_settable(element, name)
    return report


async def inspect_file_row(
    selector: MacOSFileSelector, basename: str
) -> FileRowReport:
    """Find *basename* in the open panel and describe how it is presented.

    Matched on an exact AXValue: a prefix would find ``hsc-smoke-test.txt.bak``
    just as happily. Nothing is selected, pressed or written — the point is to
    learn what the row *supports* before anything acts on it.
    """
    report = FileRowReport(basename=basename)
    api = selector.api

    panel = selector.find_open_panel()
    if panel is None:
        return report

    roots = [panel.sheet, *search_roots(api, panel.app)]
    name_element = find_where(
        api,
        roots,
        lambda element: value_of(api, element) == basename,
        max_elements=800,
    )
    if name_element is None:
        return report

    report.found = True
    report.elements.append(
        describe(api, name_element, "filename element", ELEMENT_ATTRIBUTES)
    )

    # Upwards from the filename: the cell, the row, and whatever holds them.
    chain = ancestors_of(api, name_element, max_depth=10)[1:]
    for role in ROW_ANCESTOR_ROLES:
        ancestor = next((e for e in chain if role_of(api, e) == role), None)
        if ancestor is not None:
            report.elements.append(
                describe(api, ancestor, f"{role} ancestor", ELEMENT_ATTRIBUTES)
            )

    container = next(
        (e for e in chain if role_of(api, e) in CONTAINER_ROLES), None
    )
    if container is not None:
        report.elements.append(
            describe(
                api,
                container,
                f"{role_of(api, container)} container",
                ELEMENT_ATTRIBUTES + CONTAINER_ATTRIBUTES,
            )
        )
    return report


async def run_file_row_inspection(
    selector: MacOSFileSelector, target: Path
) -> int:
    """Navigate to the file's directory, then look at how the file is listed.

    The directory, not the file: passing a full path to Go to Folder was
    measured live to navigate to the parent and select nothing, so the panel
    ends up in the right folder either way and the file still has to be picked
    out of the list. What picking it *means* is the open question, and this
    answers it by reading rather than by trying things.
    """
    directory = target.parent
    print(f"\nNavigating to the directory: {directory}")
    print("(the parent — a full path here selects nothing)\n")

    try:
        await selector.navigate_to(directory)
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return 1

    # Re-resolved from scratch inside the inspection: the panel may have been
    # rebuilt while the sheet closed.
    print(f"Looking for: {target.name}\n")
    report = await inspect_file_row(selector, target.name)
    print(report.render())

    print(
        "\nNothing was selected, pressed or written. The Open panel is still "
        "open —\nclose it by hand when you have sent this output.\n"
    )
    return 0 if report.found else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mac_file_dialog_smoke",
        description=(
            "Select a harmless file in an already-open macOS Open panel, using "
            "the production selector. Validates AXValue → AXConfirm → close "
            "without involving the real key."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="file to select (default: a throwaway file created for the run)",
    )
    parser.add_argument("--delay", type=float, default=0.0,
                        help="seconds to wait before starting")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="seconds to wait for the panel and for it to close")
    parser.add_argument("--dry-run", action="store_true",
                        help="find and describe the panel; select nothing")
    parser.add_argument(
        "--inspect-file-row",
        action="store_true",
        help=(
            "navigate to the test file's PARENT DIRECTORY, then describe how "
            "the panel presents that filename — its cell, its row, and what "
            "they support. Selects nothing and presses nothing."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


async def run(args: argparse.Namespace) -> int:
    if args.delay > 0:
        print(f"Waiting {args.delay:g}s — put the Open dialog back on screen.")
        sys.stdout.flush()
        await asyncio.sleep(args.delay)

    timeout_ms = int(args.timeout * 1000)
    selector = MacOSFileSelector(
        appear_timeout_ms=timeout_ms, close_timeout_ms=timeout_ms
    )

    print("\nLooking for a native Open panel…")
    pids = selector.panel_service_pids()
    print(f"Open/Save panel service processes: {pids or '(none)'}")

    panel = selector.find_open_panel()
    if panel is None:
        print(
            "\nNo open panel found. Open a file dialog by hand and try again "
            "(--delay 5 helps).\n",
            file=sys.stderr,
        )
        return 1
    print(f"Panel found: pid {panel.pid} {panel.name}".rstrip())

    if args.dry_run:
        print("\nHierarchy (first 25 elements):\n")
        for row in (await selector.describe_hierarchy())[:25]:
            indent = "  " * int(row["depth"])
            identifier = f" id={row['identifier']!r}" if row["identifier"] else ""
            actions = f" actions={row['actions']}" if row["actions"] else ""
            print(f"{indent}{row['role']}{identifier}{actions}")
        print("\nDry run: nothing was selected.\n")
        return 0

    with tempfile.TemporaryDirectory() as scratch:
        target = args.file
        if target is None:
            target = Path(scratch) / "hsc-smoke-test.txt"
            target.write_text("harmless smoke-test file\n", encoding="utf-8")
        target = target.expanduser().resolve()

        if not target.exists():
            print(f"\n{target} does not exist.\n", file=sys.stderr)
            return 1

        if args.inspect_file_row:
            return await run_file_row_inspection(selector, target)

        print(f"Selecting: {target}")
        try:
            await selector.select_file(target)
        except HscMonitorError as exc:
            print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
            return 1

    print(
        "\nThe panel closed, so AXValue → AXConfirm → close works on this "
        "machine.\nThe application that opened the dialog has now received "
        "that file.\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "darwin":
        print("This smoke test only works on macOS.", file=sys.stderr)
        return 2
    setup_logging(verbose=args.verbose)
    try:
        return asyncio.run(run(args))
    except HscMonitorError as exc:
        print(f"\n{type(exc).__name__}:\n{exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - manual tool
    raise SystemExit(main())
