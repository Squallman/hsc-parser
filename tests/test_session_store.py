"""Encrypted MongoDB persistence for the HSC HTTP session.

No database and no network: the collection is a small in-memory fake that
behaves the way ``replace_one(..., upsert=True)`` does, which is the only
MongoDB behaviour this feature depends on.

The security properties are tested as hard as the feature, because a stored
session *is* a credential:

* what reaches the document is ciphertext — the plaintext cookie value is
  asserted absent from the whole BSON-shaped document;
* a wrong key or an edited payload is refused, never decrypted to something
  plausible;
* the URI's credentials and the encryption key never reach a log line.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
from cryptography.fernet import Fernet

from hsc_queue_monitor.api.probe import WIZARD_COOKIE
from hsc_queue_monitor.api.session_store import (
    DOCUMENT_ID,
    SCHEMA_VERSION,
    MongoSessionStore,
    NullSessionStore,
    PersistedSession,
    SessionCipher,
    SessionStoreError,
    cookies_from_jar,
    jar_fingerprint,
    queue_expiry,
    redact_mongo_uri,
    session_from_cookies,
)

URI = "mongodb+srv://hsc_user:sup3r-secret-pw@cluster0.example.mongodb.net/?retryWrites=true"
KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()

ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.NEVER-STORE-ME-IN-PLAINTEXT"
EQUEUE_VALUE = "queue-session-NEVER-STORE-ME-IN-PLAINTEXT"
USER_AGENT = "Mozilla/5.0 (Macintosh) TestChrome/131.0.0.0"

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def cookie(name: str, value: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": "eqn.hsc.gov.ua",
        "path": "/",
        "secure": True,
        "expires": None,
        **extra,
    }


COOKIES = (
    cookie("__Secure-auth.access-token", ACCESS_TOKEN),
    cookie(WIZARD_COOKIE, EQUEUE_VALUE, expires=(NOW + timedelta(seconds=900)).timestamp()),
)


class FakeCollection:
    """``find_one`` / ``replace_one(upsert=True)`` / ``delete_one``, in memory."""

    def __init__(self, *, fails: Exception | None = None) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.replacements = 0
        self.updates: list[dict[str, Any]] = []
        self.fails = fails

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        if self.fails is not None:
            raise self.fails
        found = self.documents.get(str(query["_id"]))
        return dict(found) if found else None

    def replace_one(
        self, query: dict[str, Any], document: dict[str, Any], upsert: bool = False
    ) -> None:
        if self.fails is not None:
            raise self.fails
        assert upsert is True, "the session must be written with upsert semantics"
        self.replacements += 1
        self.updates.append(dict(document))
        # Whole-document replacement, exactly like the real thing.
        self.documents[str(query["_id"])] = dict(document)

    def delete_one(self, query: dict[str, Any]) -> None:
        if self.fails is not None:
            raise self.fails
        self.documents.pop(str(query["_id"]), None)

    @property
    def document(self) -> dict[str, Any]:
        return self.documents[DOCUMENT_ID]


class FakeMongoClient:
    def __init__(self, collection: FakeCollection) -> None:
        self._collection = collection
        self.closed = False
        self.uris: list[str] = []

    def __call__(self, uri: str) -> FakeMongoClient:
        self.uris.append(uri)
        return self

    def __getitem__(self, _name: str) -> FakeMongoClient:
        return self

    def close(self) -> None:
        self.closed = True

    def __getattr__(self, _name: str) -> Any:  # pragma: no cover - not reached
        raise AttributeError(_name)


class FakeDatabase(dict):  # type: ignore[type-arg]
    pass


def build_store(
    *, key: str = KEY, collection: FakeCollection | None = None, now: datetime = NOW
) -> tuple[MongoSessionStore, FakeCollection]:
    fake = collection or FakeCollection()

    class Client:
        def __init__(self, uri: str) -> None:
            self.uri = uri
            self.closed = False

        def __getitem__(self, _name: str) -> Any:
            return {"sessions": fake}

        def close(self) -> None:
            self.closed = True

    store = MongoSessionStore(
        URI,
        SessionCipher(key),
        client_factory=Client,
        now=lambda: now,
    )
    return store, fake


# --------------------------------------------------------------------------- #
# Encryption
# --------------------------------------------------------------------------- #


def test_the_cookie_jar_survives_a_round_trip():
    store, _fake = build_store()
    store.save(PersistedSession(cookies=COOKIES, user_agent=USER_AGENT))

    restored = store.load()

    assert restored is not None
    assert [c["name"] for c in restored.cookies] == [c["name"] for c in COOKIES]
    assert [c["value"] for c in restored.cookies] == [c["value"] for c in COOKIES]
    assert restored.user_agent == USER_AGENT


def test_the_stored_document_holds_no_plaintext_cookie_value():
    store, fake = build_store()
    store.save(PersistedSession(cookies=COOKIES, user_agent=USER_AGENT))

    document = fake.document
    # The whole document, as it would be serialised into BSON.
    rendered = json.dumps(document, default=str)
    for secret in (ACCESS_TOKEN, EQUEUE_VALUE):
        assert secret not in rendered
    # Nor are the cookie names themselves keys in the document.
    assert set(document) == {
        "_id",
        "version",
        "cookies_encrypted",
        "created_at",
        "updated_at",
        "queue_session_expires_at",
    }
    assert document["_id"] == DOCUMENT_ID
    assert document["version"] == SCHEMA_VERSION
    assert isinstance(document["cookies_encrypted"], str)


def test_the_key_is_never_stored_beside_what_it_unlocks():
    store, fake = build_store()
    store.save(PersistedSession(cookies=COOKIES))

    assert KEY not in json.dumps(fake.document, default=str)


def test_a_wrong_key_fails_instead_of_decrypting():
    store, fake = build_store()
    store.save(PersistedSession(cookies=COOKIES))

    other, _ = build_store(key=OTHER_KEY, collection=fake)
    with pytest.raises(SessionStoreError, match="different HSC_SESSION_ENCRYPTION_KEY"):
        other.load()


def test_a_tampered_payload_is_rejected():
    store, fake = build_store()
    store.save(PersistedSession(cookies=COOKIES))

    sealed = fake.document["cookies_encrypted"]
    fake.documents[DOCUMENT_ID]["cookies_encrypted"] = sealed[:-4] + "AAAA"

    with pytest.raises(SessionStoreError):
        store.load()


def test_an_unusable_key_is_refused_clearly():
    with pytest.raises(SessionStoreError, match="not a valid Fernet key"):
        SessionCipher("not-a-key")


def test_a_document_from_another_schema_version_is_ignored():
    store, fake = build_store()
    store.save(PersistedSession(cookies=COOKIES))
    fake.documents[DOCUMENT_ID]["version"] = SCHEMA_VERSION + 1

    assert store.load() is None


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #


def test_saving_replaces_the_whole_document_atomically():
    store, fake = build_store()

    store.save(PersistedSession(cookies=COOKIES))
    store.save(PersistedSession(cookies=COOKIES[:1]))

    assert fake.replacements == 2
    assert len(fake.documents) == 1  # one logical session, no history
    # Each write carried the complete payload; nothing was patched field by field.
    assert all("cookies_encrypted" in update for update in fake.updates)


def test_created_at_is_carried_forward_and_updated_at_moves():
    later = NOW + timedelta(minutes=5)
    store, fake = build_store()
    store.save(PersistedSession(cookies=COOKIES))
    first = store.load()

    assert first is not None
    moved, _ = build_store(collection=fake, now=later)
    moved.save(PersistedSession(cookies=COOKIES, created_at=first.created_at))

    second = moved.load()
    assert second is not None
    assert second.created_at == first.created_at
    assert second.updated_at == later


def test_the_queue_expiry_comes_from_the_cookies_own_metadata():
    expires_at = queue_expiry(COOKIES)

    assert expires_at is not None
    assert expires_at == datetime.fromtimestamp(COOKIES[1]["expires"], UTC)
    # No JWT is decoded to find it.
    assert queue_expiry([cookie(WIZARD_COOKIE, "x")]) is None
    assert queue_expiry([cookie("other", "x")]) is None


def test_expiry_decides_only_whether_a_session_is_worth_trying():
    fresh = PersistedSession(
        cookies=COOKIES, queue_session_expires_at=NOW + timedelta(seconds=60)
    )
    stale = PersistedSession(
        cookies=COOKIES, queue_session_expires_at=NOW - timedelta(seconds=1)
    )
    unknown = PersistedSession(cookies=COOKIES)

    assert not fresh.expired(now=NOW)
    assert stale.expired(now=NOW)
    assert not unknown.expired(now=NOW)  # unknown means "try it and see"


def test_the_age_reads_like_a_log_line():
    session = PersistedSession(cookies=(), updated_at=NOW - timedelta(seconds=252))
    assert session.age(now=NOW) == "4m12s"
    assert PersistedSession(cookies=()).age(now=NOW) == "unknown"


def test_deleting_removes_the_one_document():
    store, fake = build_store()
    store.save(PersistedSession(cookies=COOKIES))

    store.delete()

    assert fake.documents == {}
    assert store.load() is None


def test_a_missing_document_is_not_an_error():
    store, _fake = build_store()
    assert store.load() is None


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


def test_a_database_failure_becomes_a_session_store_error():
    from pymongo.errors import ServerSelectionTimeoutError

    store, _fake = build_store(
        collection=FakeCollection(fails=ServerSelectionTimeoutError("no primary"))
    )

    with pytest.raises(SessionStoreError):
        store.load()
    with pytest.raises(SessionStoreError):
        store.save(PersistedSession(cookies=COOKIES))
    with pytest.raises(SessionStoreError):
        store.delete()


def test_a_failure_message_never_carries_the_uri_or_the_key():
    from pymongo.errors import ServerSelectionTimeoutError

    store, _fake = build_store(
        collection=FakeCollection(fails=ServerSelectionTimeoutError(URI))
    )

    with pytest.raises(SessionStoreError) as excinfo:
        store.save(PersistedSession(cookies=COOKIES))

    message = str(excinfo.value)
    assert "sup3r-secret-pw" not in message
    assert KEY not in message


def test_connecting_is_logged_without_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    build_store()

    assert "sup3r-secret-pw" not in caplog.text
    assert "hsc_user" not in caplog.text
    assert "cluster0.example.mongodb.net" in caplog.text


def test_a_uri_is_redacted_down_to_its_host():
    assert redact_mongo_uri(URI) == "mongodb+srv://***@cluster0.example.mongodb.net/"
    assert "pw" not in redact_mongo_uri("mongodb://u:pw@localhost:27017/db")
    assert redact_mongo_uri("mongodb://localhost:27017") == "mongodb://localhost:27017"


# --------------------------------------------------------------------------- #
# Jar round-trip
# --------------------------------------------------------------------------- #


def test_a_restored_jar_reconstructs_the_same_session_state():
    original = session_from_cookies(COOKIES, user_agent=USER_AGENT)
    snapshot = cookies_from_jar(original)

    rebuilt = session_from_cookies(snapshot, user_agent=USER_AGENT)

    assert {c.name: c.value for c in rebuilt.cookies} == {
        c.name: c.value for c in original.cookies
    }
    assert jar_fingerprint(cookies_from_jar(rebuilt)) == jar_fingerprint(snapshot)
    assert rebuilt.headers["User-Agent"] == USER_AGENT
    assert rebuilt.headers["Referer"] == "https://eqn.hsc.gov.ua/"
    # The scope survives, so the cookies are sent to the same place.
    jar = {c.name: c for c in rebuilt.cookies}
    assert jar[WIZARD_COOKIE].domain == "eqn.hsc.gov.ua"
    assert jar[WIZARD_COOKIE].path == "/"
    assert jar[WIZARD_COOKIE].secure is True


def test_a_fingerprint_changes_only_when_a_value_changes():
    first = jar_fingerprint(COOKIES)
    same = jar_fingerprint(tuple(reversed(COOKIES)))  # order is not identity
    changed = jar_fingerprint((COOKIES[0], cookie(WIZARD_COOKIE, "something-else")))

    assert first == same
    assert first != changed
    # And it is one-way.
    assert EQUEUE_VALUE not in first


def test_only_cookie_names_are_printable():
    session = PersistedSession(cookies=COOKIES)
    assert session.names == ("__Secure-auth.access-token", WIZARD_COOKIE)
    assert ACCESS_TOKEN not in " ".join(session.names)


def test_nothing_but_cookies_is_persisted():
    """No key file, no password, no headers, no browser profile."""
    jar = requests.Session()
    jar.headers["Authorization"] = "Bearer NEVER-PERSIST-ME"
    jar.cookies.set("a", "b", domain="eqn.hsc.gov.ua", path="/")

    stored = cookies_from_jar(jar)

    assert [set(entry) for entry in stored] == [
        {"name", "value", "domain", "path", "secure", "expires"}
    ]
    assert "NEVER-PERSIST-ME" not in json.dumps(stored, default=str)


def test_the_null_store_remembers_nothing():
    store = NullSessionStore()

    store.save(PersistedSession(cookies=COOKIES))
    store.close()

    assert store.load() is None
    assert store.saves == 1
