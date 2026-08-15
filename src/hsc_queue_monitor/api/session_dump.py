"""The `--dump-session` diagnostic for `refresh-session`.

Everything :mod:`.session_store` refuses to do on purpose — write a session in
the clear, anywhere — this module does, deliberately and only on request. It
exists to answer one question during development: does anything get lost on
the way from

    browser cookies -> requests.Session -> plaintext Mongo payload

and answering it needs the plaintext side by side. Nothing here is reached
unless a caller passes ``--dump-session``; without it, this module is not
imported into the run at all.

The file this writes grants whoever holds it the authenticated session, for as
long as that session remains valid — the same as stealing the cookies from the
browser itself. It is opt-in, refuses to silently overwrite a previous dump,
and is written with owner-only permissions where the platform supports it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from ..models import HscMonitorError

logger = logging.getLogger(__name__)

#: Bumped only if the dump's shape changes in a way a reader built against an
#: older version could misinterpret.
DUMP_VERSION = 1

#: Printed by the caller before a byte is written, so the risk is seen even by
#: someone only skimming the terminal.
SESSION_DUMP_WARNING = (
    "WARNING: session dump contains authentication credentials/cookies. "
    "Anyone who reads this file can use it as the authenticated session for "
    "as long as that session remains valid."
)

#: Exactly the fields Playwright's ``BrowserContext.cookies()`` can return.
#: Copied through when present, never invented when absent.
_BROWSER_COOKIE_FIELDS = (
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
)


class SessionDumpError(HscMonitorError):
    """The diagnostic dump could not be written."""


class SessionDumpExists(SessionDumpError):
    """Refused to overwrite an existing dump without ``--overwrite-session-dump``."""


# --------------------------------------------------------------------------- #
# Building the dump
# --------------------------------------------------------------------------- #


def dump_requests_cookies(session: requests.Session) -> list[dict[str, Any]]:
    """The live cookie jar, every attribute ``http.cookiejar.Cookie`` carries.

    Deliberately not limited to :data:`.session_store.COOKIE_FIELDS` — that
    tuple is what the Mongo schema needs to rebuild a jar; this is what the
    jar actually holds, including ``rest`` (HttpOnly, SameSite and anything
    else a ``Set-Cookie`` header carried that ``requests`` did not have a
    named slot for).
    """
    return [
        {
            "name": cookie.name,
            "value": cookie.value or "",
            "domain": cookie.domain or "",
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
            "expires": cookie.expires,
            "rest": dict(getattr(cookie, "_rest", {}) or {}),
        }
        for cookie in session.cookies
    ]


def _dump_browser_cookie(cookie: Mapping[str, Any]) -> dict[str, Any]:
    return {field: cookie[field] for field in _BROWSER_COOKIE_FIELDS if field in cookie}


def build_session_dump(
    *,
    session: requests.Session,
    mongo_session_payload: Mapping[str, Any],
    browser_cookies: Sequence[Mapping[str, Any]] | None = None,
    source: str = "refresh-session",
    now: datetime | None = None,
) -> dict[str, Any]:
    """The full diagnostic object, ready for ``json.dump``.

    ``browser_cookies`` is the raw Playwright cookie dump from the same
    authenticated browser context that produced ``session`` — omitted
    entirely when the caller has none to offer, rather than written as an
    empty list that could be misread as "the browser had no cookies".

    ``mongo_session_payload`` must be the same object
    :func:`.session_store.mongo_payload` builds for
    :meth:`~.session_store.MongoSessionStore.save`, passed in rather than
    recomputed here, so the dumped payload can never drift from what was
    actually encrypted and written.
    """
    moment = now or datetime.now(UTC)
    dump: dict[str, Any] = {
        "dump_version": DUMP_VERSION,
        "created_at": moment.isoformat(),
        "source": source,
        "user_agent": str(session.headers.get("User-Agent", "")),
        "cookies": dump_requests_cookies(session),
        "headers": dict(session.headers),
    }
    if browser_cookies is not None:
        dump["browser_cookies"] = [_dump_browser_cookie(c) for c in browser_cookies]
    dump["mongo_session_payload"] = dict(mongo_session_payload)
    return dump


# --------------------------------------------------------------------------- #
# Writing the dump
# --------------------------------------------------------------------------- #


def write_session_dump(path: Path, dump: Mapping[str, Any], *, overwrite: bool = False) -> None:
    """Pretty-printed UTF-8 JSON, owner-only where the platform supports it.

    Refuses to replace an existing file unless ``overwrite`` is set. The file
    is created with mode 0600 up front — not chmod'd afterwards — so there is
    no window where a more permissive default mode is briefly on disk; a
    best-effort chmod follows only to tighten a permissive umask.

    Never logs or prints anything from ``dump`` — the caller owns the
    before-write warning, and this function's only output is the file itself.
    """
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise SessionDumpExists(
            f"{path} already exists. Pass --overwrite-session-dump to replace "
            "it, or choose a different --dump-session path."
        ) from exc
    except OSError as exc:
        raise SessionDumpError(
            f"Could not create {path}: {type(exc).__name__}: {exc}"
        ) from exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dump, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except OSError as exc:
        raise SessionDumpError(
            f"Could not write {path}: {type(exc).__name__}: {exc}"
        ) from exc

    with suppress(OSError):  # best effort: tightens a permissive umask
        os.chmod(path, 0o600)
