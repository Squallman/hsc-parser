"""Encrypted persistence for the HSC HTTP session.

The queue session was measured with ``Max-Age=900`` and is *refreshed* by API
responses — every departments, days and slots call rewrote
``__Host-next.equeue-session``. So a monitor that restarts inside that window
should not have to open Chromium again: it can pick the jar back up and carry on.

What is stored is the cookie jar and nothing else, and it is stored encrypted.
Cookie values are authentication material, so:

* the payload is serialised, then sealed whole with Fernet (AES-CBC +
  HMAC — authenticated, so tampering is detected rather than decrypted);
* the key comes only from ``HSC_SESSION_ENCRYPTION_KEY``, never from the
  database, never from a file in the repository;
* no plaintext cookie value is ever written to MongoDB, logged, or put in an
  error message.

The MasterKey password, the ``.dat`` file, the browser profile and any
``Authorization`` header are not persisted here and never will be: none of them
is a cookie, and none of them is needed to replay a session.

Nothing in this module knows about the monitor, and :class:`HscApiClient` knows
nothing about MongoDB — the two meet only in the monitor, which owns both.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests
from cryptography.fernet import Fernet, InvalidToken
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from ..logging_config import sanitize
from ..models import HscMonitorError
from .probe import BROWSER_HEADERS, WIZARD_COOKIE

logger = logging.getLogger(__name__)

#: One logical session, one document. There is no history to keep: an old jar is
#: not merely useless, it is a liability.
DOCUMENT_ID = "hsc-api-session"

#: Bumped only if the stored shape changes in a way an older reader cannot
#: handle. A document with an unknown version is ignored, never guessed at.
SCHEMA_VERSION = 1

DEFAULT_DATABASE = "hsc_queue_monitor"
DEFAULT_COLLECTION = "sessions"

#: The cookie fields that are enough to rebuild a ``requests`` jar. Anything
#: else ``requests`` fills in with its own defaults.
COOKIE_FIELDS = ("name", "value", "domain", "path", "secure", "expires")


class SessionStoreError(HscMonitorError):
    """Persistence failed. Never fatal to a monitor that already has a session."""


# --------------------------------------------------------------------------- #
# What is stored
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PersistedSession:
    """One HTTP session, as it survives a restart.

    ``cookies`` holds values — it is the in-memory form, sealed before it ever
    reaches the database. ``user_agent`` is stored with it because the cookies
    were minted for that browser identity and replaying them under a different
    one would be a different client; it is not a secret and is not sensitive.
    """

    cookies: tuple[Mapping[str, Any], ...] = ()
    user_agent: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: From the queue cookie's own expiry metadata. Informational: an unexpired
    #: timestamp is not proof the server still honours the session — only a
    #: response is.
    queue_session_expires_at: datetime | None = None

    @property
    def names(self) -> tuple[str, ...]:
        """Cookie names. The only part of a cookie that may be printed."""
        return tuple(str(cookie.get("name", "")) for cookie in self.cookies)

    def expired(self, *, now: datetime | None = None) -> bool:
        """Whether the queue cookie's own metadata says it is already dead."""
        if self.queue_session_expires_at is None:
            return False  # unknown: try it, and let the API be the judge
        moment = now or datetime.now(UTC)
        return self.queue_session_expires_at <= moment

    def age(self, *, now: datetime | None = None) -> str:
        """``4m12s`` since the last write, for a log line."""
        if self.updated_at is None:
            return "unknown"
        seconds = int(((now or datetime.now(UTC)) - self.updated_at).total_seconds())
        seconds = max(seconds, 0)
        return f"{seconds // 60}m{seconds % 60:02d}s"


def cookies_from_jar(session: requests.Session) -> tuple[Mapping[str, Any], ...]:
    """The jar as plain data, keeping what it takes to rebuild it."""
    return tuple(
        {
            "name": cookie.name,
            "value": cookie.value or "",
            "domain": cookie.domain or "",
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
            "expires": cookie.expires,
        }
        for cookie in session.cookies
    )


def mongo_payload(session: PersistedSession) -> dict[str, Any]:
    """The exact plaintext object that gets sealed with Fernet before storage.

    Shared with the ``refresh-session --dump-session`` diagnostic (see
    :mod:`.session_dump`) so the two can never drift apart: the dump always
    shows what was actually encrypted, because it is built from a call to
    this same function.
    """
    return {
        "cookies": [dict(cookie) for cookie in session.cookies],
        "user_agent": session.user_agent,
    }


def session_from_cookies(
    cookies: Iterable[Mapping[str, Any]], *, user_agent: str = ""
) -> requests.Session:
    """A ``requests.Session`` carrying a restored jar and the browser's shape."""
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    if user_agent:
        session.headers["User-Agent"] = user_agent

    for cookie in cookies:
        session.cookies.set(
            str(cookie["name"]),
            str(cookie.get("value") or ""),
            domain=str(cookie.get("domain") or ""),
            path=str(cookie.get("path") or "/"),
            secure=bool(cookie.get("secure", False)),
            expires=cookie.get("expires"),
        )
    return session


def jar_fingerprint(cookies: Sequence[Mapping[str, Any]]) -> str:
    """A one-way digest of the whole jar, for "has anything changed?".

    Never reversible, never logged as anything but a comparison result.
    """
    material = json.dumps(
        [[str(c.get("name")), str(c.get("value"))] for c in sorted(
            cookies, key=lambda c: str(c.get("name"))
        )],
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def queue_expiry(cookies: Iterable[Mapping[str, Any]]) -> datetime | None:
    """The queue cookie's expiry, straight from the jar's own metadata.

    Nothing is decoded to find this: if the cookie carries no expiry, the answer
    is "unknown", which is a perfectly good answer.
    """
    for cookie in cookies:
        if cookie.get("name") != WIZARD_COOKIE:
            continue
        expires = cookie.get("expires")
        if isinstance(expires, int | float):
            return datetime.fromtimestamp(float(expires), UTC)
        return None
    return None


# --------------------------------------------------------------------------- #
# Encryption
# --------------------------------------------------------------------------- #


class SessionCipher:
    """Authenticated encryption for the serialised jar. No custom crypto.

    Fernet is used as-is: a tampered or foreign-key payload raises rather than
    decrypting to something plausible, which is exactly the property a stored
    credential needs.
    """

    def __init__(self, key: str | bytes) -> None:
        material = key.encode("utf-8") if isinstance(key, str) else key
        try:
            self._fernet = Fernet(material)
        except (ValueError, TypeError) as exc:
            raise SessionStoreError(
                "HSC_SESSION_ENCRYPTION_KEY is not a valid Fernet key.\n"
                "Generate one with:\n"
                "  python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"\n'
                "and put it in .env. Never commit it."
            ) from exc

    def encrypt(self, payload: Mapping[str, Any]) -> str:
        return self._fernet.encrypt(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")

    def decrypt(self, token: str | bytes) -> dict[str, Any]:
        material = token.encode("ascii") if isinstance(token, str) else token
        try:
            plain = self._fernet.decrypt(material)
        except InvalidToken as exc:
            # Wrong key, or someone edited the document. Either way this is not
            # a session we may use, and the reason is not guessable from here.
            raise SessionStoreError(
                "The stored HSC session could not be decrypted: it was written "
                "with a different HSC_SESSION_ENCRYPTION_KEY, or it has been "
                "modified. It will not be used."
            ) from exc
        decoded: Any = json.loads(plain)
        if not isinstance(decoded, dict):  # pragma: no cover - we wrote it
            raise SessionStoreError("The stored HSC session is not an object.")
        return decoded


#: ``scheme://user:password@host`` anywhere in a message. PyMongo puts the URI
#: into some of its errors, and a diagnostic is not worth a leaked password.
_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)[^/@\s]+@")

#: Long enough to say what went wrong, short enough not to become a body dump.
MAX_DETAIL_CHARS = 200


def safe_detail(error: Exception) -> str:
    """``InvalidOperation: Cannot use MongoClient after close``, scrubbed.

    The class alone is not a diagnostic — it took a live run to work out which
    ``InvalidOperation`` it was — so the message is kept, passed through the
    project redactor and stripped of any embedded credentials.
    """
    message = " ".join(str(error).split())
    if not message:
        return type(error).__name__
    message = _CREDENTIALS.sub(r"\g<scheme>***@", str(sanitize(message)))
    if len(message) > MAX_DETAIL_CHARS:
        message = f"{message[:MAX_DETAIL_CHARS]}…"
    return f"{type(error).__name__}: {message}"


def redact_mongo_uri(uri: str) -> str:
    """``mongodb+srv://user:pw@host/db`` -> ``mongodb+srv://***@host/db``."""
    try:
        parts = urlsplit(uri)
    except ValueError:  # pragma: no cover - defensive
        return "(unparseable URI)"
    if not parts.netloc:
        return "(uri)"
    host = parts.netloc.rsplit("@", 1)[-1]
    netloc = f"***@{host}" if "@" in parts.netloc else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


class SessionStore(Protocol):
    """Where a session sleeps between runs."""

    def load(self) -> PersistedSession | None: ...

    def save(self, session: PersistedSession) -> None: ...

    def delete(self) -> None: ...

    def close(self) -> None: ...


@dataclass
class NullSessionStore:
    """The store you get when MongoDB is not configured. Remembers nothing."""

    saves: int = field(default=0)

    def load(self) -> PersistedSession | None:
        return None

    def save(self, session: PersistedSession) -> None:
        self.saves += 1

    def delete(self) -> None:
        return None

    def close(self) -> None:
        return None


class MongoSessionStore:
    """One encrypted document in MongoDB, replaced whole on every save.

    Whole-document ``replace_one(..., upsert=True)`` is the atomicity story: a
    crash leaves either the previous complete jar or the new complete jar, never
    a half-updated one. Individual cookies are never touched.
    """

    def __init__(
        self,
        uri: str,
        cipher: SessionCipher,
        *,
        database: str = DEFAULT_DATABASE,
        collection: str = DEFAULT_COLLECTION,
        document_id: str = DOCUMENT_ID,
        client_factory: Callable[[str], Any] = MongoClient,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.uri = uri
        self.cipher = cipher
        self.database = database
        self.collection_name = collection
        self.document_id = document_id
        self._now = now
        try:
            self._client = client_factory(uri)
        except PyMongoError as exc:
            raise SessionStoreError(
                f"Could not connect to MongoDB at {redact_mongo_uri(uri)}: "
                f"{type(exc).__name__}"
            ) from exc
        logger.info("Session persistence: MongoDB at %s", redact_mongo_uri(uri))

    @property
    def collection(self) -> Any:
        """The collection both documents live in — the session, and the state.

        Public so the monitor-state store can share this connection instead of
        opening a second one to the same cluster with the same credentials.
        """
        return self._client[self.database][self.collection_name]

    # ---------------------------------------------------------------- read --

    def load(self) -> PersistedSession | None:
        try:
            document = self.collection.find_one({"_id": self.document_id})
        except PyMongoError as exc:
            raise SessionStoreError(
                f"Could not read the stored HSC session: {type(exc).__name__}"
            ) from exc

        if not document:
            return None
        if document.get("version") != SCHEMA_VERSION:
            logger.warning(
                "Ignoring a stored HSC session written by schema version %r",
                document.get("version"),
            )
            return None

        payload = self.cipher.decrypt(document["cookies_encrypted"])
        cookies = tuple(payload.get("cookies") or ())
        return PersistedSession(
            cookies=cookies,
            user_agent=str(payload.get("user_agent") or ""),
            created_at=_as_utc(document.get("created_at")),
            updated_at=_as_utc(document.get("updated_at")),
            queue_session_expires_at=_as_utc(document.get("queue_session_expires_at")),
        )

    # --------------------------------------------------------------- write --

    def save(self, session: PersistedSession) -> None:
        moment = self._now()
        document = {
            "_id": self.document_id,
            "version": SCHEMA_VERSION,
            # Everything sensitive lives inside this one sealed string.
            "cookies_encrypted": self.cipher.encrypt(mongo_payload(session)),
            "created_at": session.created_at or moment,
            "updated_at": moment,
            "queue_session_expires_at": session.queue_session_expires_at,
        }
        try:
            self.collection.replace_one(
                {"_id": self.document_id}, document, upsert=True
            )
        except PyMongoError as exc:
            raise SessionStoreError(
                f"Could not persist the HSC session: {type(exc).__name__}"
            ) from exc

    def delete(self) -> None:
        try:
            self.collection.delete_one({"_id": self.document_id})
        except PyMongoError as exc:
            raise SessionStoreError(
                f"Could not delete the stored HSC session: {type(exc).__name__}"
            ) from exc

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - best effort teardown
            logger.debug("Could not close the MongoDB client")


class SessionPersister:
    """Writes the jar back when — and only when — HSC changed it.

    Attached to :attr:`~.client.HscApiClient.on_response`, so it sees the jar
    after every API call. Both the long-running monitor and the single headless
    scan use this one implementation, because "when is it safe to write" is a
    policy that must not have two answers.

    A write failure is never allowed to matter to the caller: the fingerprint is
    deliberately *not* recorded, so the next response tries again and the current
    state reaches the database as soon as it can.
    """

    def __init__(
        self,
        store: SessionStore,
        *,
        created_at: datetime | None = None,
        fingerprint: str = "",
    ) -> None:
        self.store = store
        self.created_at = created_at
        self._last = fingerprint
        self.degraded = False
        self.writes = 0

    def adopt(self, *, created_at: datetime | None = None, fingerprint: str = "") -> None:
        """Start tracking a different session — after a refresh, say."""
        self.created_at = created_at
        self._last = fingerprint

    def __call__(self, session: requests.Session) -> None:
        cookies = cookies_from_jar(session)
        digest = jar_fingerprint(cookies)
        if digest == self._last:
            return  # nothing moved, nothing to write

        try:
            self.store.save(
                PersistedSession(
                    cookies=cookies,
                    user_agent=str(session.headers.get("User-Agent", "")),
                    created_at=self.created_at,
                    queue_session_expires_at=queue_expiry(cookies),
                )
            )
        except HscMonitorError as exc:
            self.degraded = True
            logger.warning(
                "Could not persist the HSC session: %s",
                str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
            )
            return

        self._last = digest
        self.degraded = False
        self.writes += 1
        logger.info("Persisted refreshed HSC session")


def _as_utc(value: Any) -> datetime | None:
    """MongoDB hands back naive UTC datetimes; make that explicit."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
