"""Opt-in, client-bound transport observation without changing command outcomes.

Observers are synchronous and must return promptly. A bounded nonblocking queue
is the intended adapter; storage, hashing and network I/O belong outside this
callback. Raw values are decoded WebSocket messages, not TCP/WebSocket frames.
They can contain secrets and are supplied only to the explicitly bound observer.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    client_id: str


ObservationKind = Literal[
    "connection_start",
    "connection_ready",
    "connection_error",
    "connection_closed",
    "send",
    "sent",
    "send_error",
    "receive",
    "receive_error",
    "receive_cancelled",
    "request_cancelled",
]


@dataclass(frozen=True, slots=True)
class TransportObservation:
    client_id: str
    connection_id: str
    sequence: int
    wall_time_ns: int
    monotonic_ns: int
    kind: ObservationKind
    raw: str | bytes | None = field(default=None, repr=False)
    request_id: int | None = None
    method: str | None = None
    session_id: str | None = None
    pending_count: int = 0
    exception_type: str | None = None
    process_id: int | None = None
    owner_thread_id: int | None = None
    owner_loop_id: int | None = None
    thread_id: int | None = None
    loop_id: int | None = None


Observer = Callable[[TransportObservation], None]
ObserverFactory = Callable[[ClientIdentity], Observer]


@dataclass(frozen=True, slots=True)
class ObservationHealth:
    enabled: bool
    factory_errors: int = 0
    observer_errors: int = 0
    emitted: int = 0
    delivered: int = 0
    last_error_type: str | None = None


class _UseContext:
    pass


USE_CONTEXT = _UseContext()
_factory: ContextVar[ObserverFactory | None] = ContextVar(
    "cdp_use_observer_factory", default=None
)


@contextmanager
def observe_clients(factory: ObserverFactory | None) -> Iterator[None]:
    """Bind newly constructed clients; existing clients keep their own observer.

    Task/context propagation follows normal ContextVar rules. Explicit ``None``
    disables observation inside a nested scope. Threads require explicit context
    propagation or the client's ``observer_factory`` constructor argument.
    """
    if factory is not None and (
        not callable(factory) or inspect.iscoroutinefunction(factory)
    ):
        raise TypeError("observer factory must be a synchronous callable or None")
    token = _factory.set(factory)
    try:
        yield
    finally:
        _factory.reset(token)


def _reject_awaitable(value: object) -> None:
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise TypeError("transport observation must be synchronous")


def _running_loop_id() -> int | None:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return None


class _ClientObservation:
    """One client's observation state; records but does not enforce loop ownership."""

    def __init__(self, factory: ObserverFactory):
        self.identity = ClientIdentity(uuid4().hex)
        self.connection_id: str | None = None
        self.sequence = 0
        self.closed = False
        self.owner_thread_id: int | None = None
        self.owner_loop_id: int | None = None
        self.observer: Observer | None = None
        self.factory_errors = 0
        self.observer_errors = 0
        self.emitted = 0
        self.delivered = 0
        self.last_error_type: str | None = None
        # Deliberately limited to the optional diagnostic factory. A diagnostic
        # CancelledError is not a cancellation of any asynchronous client work.
        try:
            observer = factory(self.identity)
            _reject_awaitable(observer)
            if not callable(observer) or inspect.iscoroutinefunction(observer):
                raise TypeError("observer must be a synchronous callable")
            self.observer = observer
        except (Exception, asyncio.CancelledError) as exc:
            self.factory_errors += 1
            self.last_error_type = type(exc).__name__[:128]

    def start(self, pending_count: int) -> None:
        self.connection_id = uuid4().hex
        self.sequence = 0
        self.closed = False
        self.owner_thread_id = threading.get_ident()
        self.owner_loop_id = _running_loop_id()
        self.emit("connection_start", pending_count=pending_count)

    def emit(
        self,
        kind: ObservationKind,
        *,
        raw: str | bytes | None = None,
        request_id: int | None = None,
        method: str | None = None,
        session_id: str | None = None,
        pending_count: int,
        exception: BaseException | None = None,
    ) -> None:
        if self.observer is None or self.connection_id is None:
            return
        if kind == "connection_closed":
            if self.closed:
                return
            self.closed = True
        self.sequence += 1
        self.emitted += 1
        event = TransportObservation(
            client_id=self.identity.client_id,
            connection_id=self.connection_id,
            sequence=self.sequence,
            wall_time_ns=time.time_ns(),
            monotonic_ns=time.monotonic_ns(),
            kind=kind,
            raw=raw,
            request_id=request_id,
            method=method,
            session_id=session_id,
            pending_count=pending_count,
            exception_type=None
            if exception is None
            else type(exception).__name__[:128],
            process_id=os.getpid(),
            owner_thread_id=self.owner_thread_id,
            owner_loop_id=self.owner_loop_id,
            thread_id=threading.get_ident(),
            loop_id=_running_loop_id(),
        )
        # Only the optional callback is isolated. Original transport exceptions
        # and cancellation are observed by the caller and keep their semantics.
        try:
            result = self.observer(event)
            _reject_awaitable(result)
            if result is not None:
                raise TypeError("observer must return None")
        except (Exception, asyncio.CancelledError) as exc:
            self.observer_errors += 1
            self.last_error_type = type(exc).__name__[:128]
        else:
            self.delivered += 1

    @property
    def health(self) -> ObservationHealth:
        return ObservationHealth(
            enabled=self.observer is not None,
            factory_errors=self.factory_errors,
            observer_errors=self.observer_errors,
            emitted=self.emitted,
            delivered=self.delivered,
            last_error_type=self.last_error_type,
        )


def _bind_client(
    factory: ObserverFactory | None | _UseContext,
) -> _ClientObservation | None:
    selected = _factory.get() if isinstance(factory, _UseContext) else factory
    return None if selected is None else _ClientObservation(selected)


__all__ = [
    "ClientIdentity",
    "ObservationHealth",
    "ObservationKind",
    "Observer",
    "ObserverFactory",
    "TransportObservation",
    "observe_clients",
]
