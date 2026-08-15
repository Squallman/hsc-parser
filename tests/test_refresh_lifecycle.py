"""The MongoDB lifetime of `refresh-session`, and what READY is allowed to mean.

This file exists because of one live failure. The session saved, the client was
closed in a `finally`, and the monitor-state write then hit a closed client:

    Could not persist monitor state READY:
    Could not persist the monitor state: InvalidOperation

and the command *still* printed ``Monitor state: unknown -> READY`` and declared
itself ready. The next scheduled run, reading the state document that had never
been written, stayed gated behind the old AUTH_REQUIRED — correctly, and
confusingly.

Two things are pinned here. The client stays open until **both** documents are
written, and a transition is only ever reported when the write that would have
caused it actually landed.

The fake client below raises ``InvalidOperation`` after ``close()``, exactly as
PyMongo does, so the original bug fails these tests rather than passing them.
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet
from pymongo.errors import InvalidOperation
from test_api_availability import ApiServer
from test_api_monitor import ProviderWithFakeBrowser, monitor_config

from hsc_queue_monitor.api.monitor_state import (
    STATE_DOCUMENT_ID,
    MongoMonitorStateStore,
    MonitorState,
    MonitorStatus,
)
from hsc_queue_monitor.api.session_store import (
    DOCUMENT_ID,
    MongoSessionStore,
    SessionCipher,
    SessionStoreError,
    safe_detail,
)
from hsc_queue_monitor.cli import (
    EXIT_OK,
    EXIT_PERSISTENCE,
    monitor_state_store,
    run_monitor_once,
    run_refresh_session,
)

URI = "mongodb+srv://hsc_user:sup3r-secret-pw@cluster0.example.mongodb.net/"
KEY = Fernet.generate_key().decode()


class ClosedClientCollection:
    """A collection that stops working the moment its client is closed."""

    def __init__(self, client: FakeMongoClient) -> None:
        self.client = client
        self.documents: dict[str, dict[str, Any]] = {}
        #: Every operation, in order, with whether the client was open for it.
        self.operations: list[tuple[str, bool]] = []
        self.refuse_state_writes = False

    def _check(self, operation: str) -> None:
        self.operations.append((operation, not self.client.closed))
        if self.client.closed:
            # PyMongo's own wording, and the one the live run produced.
            raise InvalidOperation("Cannot use MongoClient after close")

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        self._check(f"find_one:{query['_id']}")
        found = self.documents.get(str(query["_id"]))
        return dict(found) if found else None

    def replace_one(
        self, query: dict[str, Any], document: dict[str, Any], upsert: bool = False
    ) -> None:
        self._check(f"replace_one:{query['_id']}")
        assert upsert is True
        if self.refuse_state_writes and query["_id"] == STATE_DOCUMENT_ID:
            raise InvalidOperation("Cannot use MongoClient after close")
        self.documents[str(query["_id"])] = dict(document)

    def delete_one(self, query: dict[str, Any]) -> None:
        self._check(f"delete_one:{query['_id']}")
        self.documents.pop(str(query["_id"]), None)


class FakeMongoClient:
    """One client, one collection, and a close that actually means something."""

    def __init__(self, _uri: str) -> None:
        self.closed = False
        self.closed_after: list[tuple[str, bool]] = []
        self.collection = ClosedClientCollection(self)

    def __getitem__(self, _name: str) -> Any:
        return {"sessions": self.collection}

    def close(self) -> None:
        self.closed = True
        self.closed_after = list(self.collection.operations)


def build_store() -> tuple[MongoSessionStore, ClosedClientCollection, FakeMongoClient]:
    clients: list[FakeMongoClient] = []

    def factory(uri: str) -> FakeMongoClient:
        client = FakeMongoClient(uri)
        clients.append(client)
        return client

    store = MongoSessionStore(URI, SessionCipher(KEY), client_factory=factory)
    return store, clients[0].collection, clients[0]


def refresh(tmp_path: Any, store: MongoSessionStore) -> tuple[int, ProviderWithFakeBrowser]:
    harness = ProviderWithFakeBrowser(tmp_path, fetch=ApiServer())
    code = _run(run_refresh_session(monitor_config(tmp_path), provider=harness, store=store))
    return code, harness


def _run(coroutine: Any) -> int:
    import asyncio

    return int(asyncio.run(coroutine))


# --------------------------------------------------------------------------- #
# The shared resource
# --------------------------------------------------------------------------- #


def test_both_documents_share_one_client():
    store, collection, client = build_store()
    states = monitor_state_store(store)

    assert isinstance(states, MongoMonitorStateStore)
    # Same collection object: one connection, two documents.
    assert store.collection is collection
    assert states._collection is collection
    assert client.closed is False


async def test_both_writes_happen_before_the_client_closes(tmp_path):
    store, collection, client = build_store()
    collection.documents[STATE_DOCUMENT_ID] = _auth_required_document()

    harness = ProviderWithFakeBrowser(tmp_path, fetch=ApiServer())
    code = await run_refresh_session(
        monitor_config(tmp_path), provider=harness, store=store
    )

    assert code == EXIT_OK
    # Every operation ran against an open client — the bug was the opposite.
    assert all(was_open for _op, was_open in collection.operations)
    assert [op for op, _ in collection.operations] == [
        f"replace_one:{DOCUMENT_ID}",
        f"find_one:{STATE_DOCUMENT_ID}",
        f"replace_one:{STATE_DOCUMENT_ID}",
    ]
    # And the close came after both of them.
    assert client.closed
    assert len(client.closed_after) == 3


async def test_nothing_touches_the_collection_after_the_close(tmp_path):
    store, collection, client = build_store()

    await run_refresh_session(
        monitor_config(tmp_path),
        provider=ProviderWithFakeBrowser(tmp_path, fetch=ApiServer()),
        store=store,
    )

    assert client.closed
    assert collection.operations == client.closed_after  # no late operation


async def test_a_successful_refresh_writes_both_documents(tmp_path, capsys):
    store, collection, _client = build_store()
    collection.documents[STATE_DOCUMENT_ID] = _auth_required_document()

    code = await run_refresh_session(
        monitor_config(tmp_path),
        provider=ProviderWithFakeBrowser(tmp_path, fetch=ApiServer()),
        store=store,
    )

    assert code == EXIT_OK
    assert set(collection.documents) == {DOCUMENT_ID, STATE_DOCUMENT_ID}
    assert collection.documents[STATE_DOCUMENT_ID]["status"] == "READY"

    out = capsys.readouterr().out
    assert "Session saved: OK" in out
    assert "Monitor state: AUTH_REQUIRED -> READY" in out
    assert "Session is ready for headless monitoring." in out


# --------------------------------------------------------------------------- #
# When READY cannot be written
# --------------------------------------------------------------------------- #


def _auth_required_document() -> dict[str, Any]:
    return {
        "_id": STATE_DOCUMENT_ID,
        "version": 1,
        "status": MonitorStatus.AUTH_REQUIRED.value,
        "reason": "HTTP 401 Unauthorized",
        "updated_at": None,
        "last_success_at": None,
        "retry_after_at": None,
    }


async def test_a_failed_ready_write_is_not_reported_as_success(tmp_path, capsys):
    store, collection, _client = build_store()
    collection.documents[STATE_DOCUMENT_ID] = _auth_required_document()
    collection.refuse_state_writes = True

    code = await run_refresh_session(
        monitor_config(tmp_path),
        provider=ProviderWithFakeBrowser(tmp_path, fetch=ApiServer()),
        store=store,
    )

    printed = capsys.readouterr()
    assert code == EXIT_PERSISTENCE
    assert "SESSION REFRESH INCOMPLETE" in printed.err
    assert "Headless monitoring remains paused" in printed.err
    # The two lies the live run told, now impossible.
    assert "-> READY" not in printed.out
    assert "Session is ready for headless monitoring." not in printed.out


async def test_a_failed_ready_write_leaves_auth_required_exactly_as_it_was(tmp_path):
    store, collection, _client = build_store()
    collection.documents[STATE_DOCUMENT_ID] = _auth_required_document()
    before = dict(collection.documents[STATE_DOCUMENT_ID])
    collection.refuse_state_writes = True

    await run_refresh_session(
        monitor_config(tmp_path),
        provider=ProviderWithFakeBrowser(tmp_path, fetch=ApiServer()),
        store=store,
    )

    # Sticky, and never deleted in the hope of replacing it.
    assert collection.documents[STATE_DOCUMENT_ID] == before
    # The fresh session is still saved: it is not the thing that failed.
    assert DOCUMENT_ID in collection.documents


async def test_the_next_run_stays_gated_after_a_failed_ready_write(tmp_path):
    store, collection, _client = build_store()
    collection.documents[STATE_DOCUMENT_ID] = _auth_required_document()
    collection.refuse_state_writes = True

    await run_refresh_session(
        monitor_config(tmp_path),
        provider=ProviderWithFakeBrowser(tmp_path, fetch=ApiServer()),
        store=store,
    )

    # A second store over the same documents, as a new process would have.
    second, second_collection, _c = build_store()
    second_collection.documents = collection.documents
    server = ApiServer()

    code = run_monitor_once(
        monitor_config(tmp_path),
        centers=["3242"],
        store=second,
        fetch=server,
        emit=lambda _text: None,
    )

    # monitor-once returns 0, but internally stays gated (AUTH_REQUIRED state)
    assert code == 0
    assert server.requests == []  # no requests made due to gate


# --------------------------------------------------------------------------- #
# End to end: refresh, then monitor
# --------------------------------------------------------------------------- #


def test_a_refreshed_session_lets_the_next_run_through(tmp_path):
    store, collection, _client = build_store()
    collection.documents[STATE_DOCUMENT_ID] = _auth_required_document()

    assert _run(
        run_refresh_session(
            monitor_config(tmp_path),
            provider=ProviderWithFakeBrowser(tmp_path, fetch=ApiServer()),
            store=store,
        )
    ) == EXIT_OK

    # A fresh process: new client, same two documents.
    second, second_collection, _c = build_store()
    second_collection.documents = collection.documents
    server = ApiServer(days=[])

    code = run_monitor_once(
        monitor_config(tmp_path),
        centers=["3242"],
        store=second,
        fetch=server,
        emit=lambda _text: None,
    )

    assert code == EXIT_OK
    assert server.endpoints == ["departments", "days"]  # the gate let it through
    assert second_collection.documents[STATE_DOCUMENT_ID]["status"] == "READY"


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def test_the_error_says_which_invalid_operation_it_was():
    """The class alone cost a live run to interpret; the message is the fix."""
    detail = safe_detail(InvalidOperation("Cannot use MongoClient after close"))

    assert detail == "InvalidOperation: Cannot use MongoClient after close"


def test_the_state_store_reports_the_underlying_message():
    store, collection, client = build_store()
    states = monitor_state_store(store)
    client.close()

    with pytest.raises(SessionStoreError) as excinfo:
        states.save(MonitorState(status=MonitorStatus.READY))

    message = str(excinfo.value)
    assert "InvalidOperation" in message
    assert "Cannot use MongoClient after close" in message
    assert collection.operations[-1] == (f"replace_one:{STATE_DOCUMENT_ID}", False)


def test_credentials_never_survive_into_a_diagnostic():
    detail = safe_detail(InvalidOperation(f"connection failed to {URI}"))

    assert "sup3r-secret-pw" not in detail
    assert "hsc_user" not in detail
    assert "***@cluster0.example.mongodb.net" in detail


def test_a_long_message_is_trimmed_rather_than_dumped():
    detail = safe_detail(InvalidOperation("x" * 500))

    assert len(detail) < 300
    assert detail.endswith("…")


def test_an_empty_message_still_names_the_class():
    assert safe_detail(InvalidOperation()) == "InvalidOperation"
