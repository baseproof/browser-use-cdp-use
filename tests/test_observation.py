"""Real loopback WebSocket tests for observation, not site behavior."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, asdict
from http import HTTPStatus
import json
import os
import threading
import unittest
import warnings

import websockets
from websockets.asyncio.server import serve

from cdp_use import CDPClient, observe_clients
from cdp_use.observation import ClientIdentity, TransportObservation


@asynccontextmanager
async def endpoint(handler, **kwargs):
    async with serve(handler, "127.0.0.1", 0, **kwargs) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def echo(socket):
    async for raw in socket:
        message = json.loads(raw)
        await socket.send(json.dumps({"id": message["id"], "result": {"ok": True}}))


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_wire_text_and_binary_receive_and_immutable_events(self):
        for binary in (False, True):
            with self.subTest(binary=binary):
                events = []
                outgoing = []
                incoming = []

                async def handler(socket):
                    async for raw in socket:
                        outgoing.append(raw)
                        request = json.loads(raw)
                        response = (
                            '{ "id": %d, "result": { "text": "✓" } }' % request["id"]
                        )
                        if binary:
                            response = response.encode()
                        incoming.append(response)
                        await socket.send(response)

                async with endpoint(handler) as url:
                    with observe_clients(lambda identity: events.append):
                        client = CDPClient(url)
                    async with client:
                        self.assertEqual(
                            await client.send_raw(
                                "Example.action", {"text": "é"}, "session-1"
                            ),
                            {"text": "✓"},
                        )
                    sent = next(e for e in events if e.kind == "send")
                    received = next(e for e in events if e.kind == "receive")
                    self.assertEqual(sent.raw, outgoing[0])
                    self.assertEqual(received.raw, incoming[0])
                    self.assertEqual(
                        sent.raw,
                        json.dumps(
                            {
                                "id": 1,
                                "method": "Example.action",
                                "params": {"text": "é"},
                                "sessionId": "session-1",
                            }
                        ),
                    )
                    self.assertEqual(
                        (sent.request_id, sent.method, sent.session_id),
                        (1, "Example.action", "session-1"),
                    )
                    completed = next(e for e in events if e.kind == "sent")
                    self.assertIsNone(completed.raw)
                    self.assertEqual(completed.request_id, sent.request_id)
                    self.assertEqual(
                        [e.sequence for e in events], list(range(1, len(events) + 1))
                    )
                    self.assertEqual(events[0].kind, "connection_start")
                    self.assertEqual(events[1].kind, "connection_ready")
                    self.assertEqual(events[-1].kind, "connection_closed")
                    self.assertEqual(len({e.connection_id for e in events}), 1)
                    self.assertTrue(all(e.process_id == os.getpid() for e in events))
                    self.assertTrue(
                        all(
                            e.thread_id == e.owner_thread_id == threading.get_ident()
                            for e in events
                        )
                    )
                    self.assertTrue(
                        all(
                            e.loop_id
                            == e.owner_loop_id
                            == id(asyncio.get_running_loop())
                            for e in events
                        )
                    )
                    self.assertTrue(
                        all(e.wall_time_ns > 0 and e.monotonic_ns > 0 for e in events)
                    )
                    self.assertTrue(all(url not in repr(asdict(e)) for e in events))
                    with self.assertRaises(FrozenInstanceError):
                        sent.raw = "modified"
                    health = client.observation_health
                    self.assertTrue(health.enabled)
                    self.assertEqual(health.emitted, health.delivered)
                    self.assertEqual(health.observer_errors, 0)

    async def test_unobserved_existing_constructor_and_command_contract(self):
        async with endpoint(echo) as url:
            client = CDPClient(url, None, 1024 * 1024)
            async with client:
                self.assertEqual(await client.send_raw("Example.action"), {"ok": True})
            self.assertFalse(client.observation_health.enabled)
            self.assertEqual(client.observation_health.emitted, 0)

    async def test_idless_reply_is_observed_without_invented_attribution(self):
        raw_error = (
            '{"error":{"code":-32700,"message":"JSON: invalid token at position 0"}}'
        )
        events = []

        async def handler(socket):
            message = json.loads(await socket.recv())
            await socket.send(raw_error)
            await socket.send(json.dumps({"id": message["id"], "result": {"ok": True}}))
            await socket.wait_closed()

        async with endpoint(handler) as url:
            async with CDPClient(
                url, observer_factory=lambda identity: events.append
            ) as client:
                self.assertEqual(await client.send_raw("Example.action"), {"ok": True})
        event = next(e for e in events if e.raw == raw_error)
        self.assertEqual(event.kind, "receive")
        self.assertIsNone(event.request_id)
        self.assertIsNone(event.method)
        self.assertIsNone(event.session_id)
        self.assertEqual(event.pending_count, 1)

    async def test_malformed_message_precedes_original_decode_failure(self):
        events = []
        raw_error = b"{broken JSON"

        async def handler(socket):
            await socket.recv()
            await socket.send(raw_error)
            await socket.wait_closed()

        async with endpoint(handler) as url:
            async with CDPClient(
                url, observer_factory=lambda identity: events.append
            ) as client:
                with self.assertRaises(json.JSONDecodeError):
                    await client.send_raw("Example.action")
        receive = next(e for e in events if e.kind == "receive")
        error = next(e for e in events if e.kind == "receive_error")
        self.assertEqual(receive.raw, raw_error)
        self.assertLess(receive.sequence, error.sequence)
        self.assertEqual(error.exception_type, "JSONDecodeError")
        self.assertIsNone(error.raw)

    async def test_observer_failure_preserves_original_transport_error(self):
        original_error = {"code": -32000, "message": "controlled protocol failure"}

        def observer(event):
            raise ValueError("diagnostic failure must not replace protocol error")

        async def handler(socket):
            request = json.loads(await socket.recv())
            await socket.send(
                json.dumps({"id": request["id"], "error": original_error})
            )
            await socket.wait_closed()

        async with endpoint(handler) as url:
            async with CDPClient(
                url, observer_factory=lambda identity: observer
            ) as client:
                with self.assertRaises(RuntimeError) as caught:
                    await client.send_raw("Example.action")
                self.assertEqual(caught.exception.args, (original_error,))
            self.assertGreater(client.observation_health.observer_errors, 0)
            self.assertEqual(client.observation_health.delivered, 0)

    async def test_factory_failures_are_visible_without_preventing_commands(self):
        for error_type in (ValueError, asyncio.CancelledError):
            with self.subTest(error_type=error_type):

                def factory(identity):
                    raise error_type("not retained")

                async with endpoint(echo) as url:
                    async with CDPClient(url, observer_factory=factory) as client:
                        self.assertEqual(
                            await client.send_raw("Example.action"), {"ok": True}
                        )
                    health = client.observation_health
                    self.assertFalse(health.enabled)
                    self.assertEqual(health.factory_errors, 1)
                    self.assertEqual(health.last_error_type, error_type.__name__)
                    self.assertNotIn("not retained", repr(health))

    async def test_observer_cancelled_error_does_not_cancel_browser_work(self):
        def observer(event):
            raise asyncio.CancelledError("diagnostic cancellation")

        async with endpoint(echo) as url:
            async with CDPClient(
                url, observer_factory=lambda identity: observer
            ) as client:
                self.assertEqual(await client.send_raw("Example.action"), {"ok": True})
            self.assertEqual(
                client.observation_health.last_error_type, "CancelledError"
            )
            self.assertGreater(client.observation_health.observer_errors, 0)

    async def test_async_callback_and_factory_results_are_closed_not_leaked(self):
        async def coroutine():
            return None

        async with endpoint(echo) as url:
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                async with CDPClient(
                    url, observer_factory=lambda identity: coroutine()
                ) as broken_factory:
                    self.assertEqual(
                        await broken_factory.send_raw("Example.action"), {"ok": True}
                    )
                async with CDPClient(
                    url, observer_factory=lambda identity: lambda event: coroutine()
                ) as broken_observer:
                    self.assertEqual(
                        await broken_observer.send_raw("Example.action"), {"ok": True}
                    )
            self.assertEqual(broken_factory.observation_health.factory_errors, 1)
            self.assertGreater(broken_observer.observation_health.observer_errors, 0)
            self.assertFalse(
                [w for w in recorded if "was never awaited" in str(w.message)]
            )

    async def test_nested_scopes_explicit_none_and_existing_client_binding(self):
        first, second, explicit = [], [], []
        async with endpoint(echo) as url:
            with observe_clients(lambda identity: first.append):
                original = CDPClient(url)
                with observe_clients(lambda identity: second.append):
                    nested = CDPClient(url)
                    disabled = CDPClient(url, observer_factory=None)
                selected = CDPClient(
                    url, observer_factory=lambda identity: explicit.append
                )
                with observe_clients(None):
                    scoped_disabled = CDPClient(url)
            outside = CDPClient(url)
            for client in (
                original,
                nested,
                disabled,
                selected,
                scoped_disabled,
                outside,
            ):
                async with client:
                    self.assertEqual(
                        await client.send_raw("Example.action"), {"ok": True}
                    )
        self.assertEqual(len({e.client_id for e in first}), 1)
        self.assertEqual(len({e.client_id for e in second}), 1)
        self.assertEqual(len({e.client_id for e in explicit}), 1)
        self.assertEqual(
            len({first[0].client_id, second[0].client_id, explicit[0].client_id}), 3
        )
        self.assertFalse(disabled.observation_health.enabled)
        self.assertFalse(scoped_disabled.observation_health.enabled)
        self.assertFalse(outside.observation_health.enabled)

    async def test_connection_restart_has_new_identity_and_preserves_request_counter(
        self,
    ):
        events, identities = [], []

        def factory(identity):
            identities.append(identity)
            return events.append

        async with endpoint(echo) as url:
            client = CDPClient(url, observer_factory=factory)
            for _ in range(2):
                async with client:
                    self.assertEqual(
                        await client.send_raw("Example.action"), {"ok": True}
                    )
        self.assertEqual(len(identities), 1)
        self.assertIsInstance(identities[0], ClientIdentity)
        starts = [e for e in events if e.kind == "connection_start"]
        self.assertEqual(len(starts), 2)
        self.assertNotEqual(starts[0].connection_id, starts[1].connection_id)
        self.assertEqual([e.sequence for e in starts], [1, 1])
        self.assertEqual([e.request_id for e in events if e.kind == "send"], [1, 2])
        self.assertEqual(len([e for e in events if e.kind == "connection_closed"]), 2)

    async def test_failed_start_retains_original_handshake_failure(self):
        events = []

        def refuse(connection, request):
            return connection.respond(
                HTTPStatus.SERVICE_UNAVAILABLE, "controlled refusal"
            )

        async with endpoint(echo, process_request=refuse) as url:
            client = CDPClient(url, observer_factory=lambda identity: events.append)
            with self.assertRaises(websockets.exceptions.InvalidStatus):
                await client.start()
            await client.stop()
        self.assertEqual(
            [e.kind for e in events], ["connection_start", "connection_error"]
        )
        self.assertEqual(events[-1].exception_type, "InvalidStatus")
        self.assertTrue(all(e.raw is None for e in events))

    async def test_cancellation_is_observed_without_claiming_browser_cancellation(self):
        events = []
        received, release = asyncio.Event(), asyncio.Event()
        reply_seen = asyncio.Event()

        def observer(event):
            events.append(event)
            if event.kind == "receive":
                reply_seen.set()

        async def handler(socket):
            request = json.loads(await socket.recv())
            received.set()
            await release.wait()
            await socket.send(json.dumps({"id": request["id"], "result": {"ok": True}}))
            await socket.wait_closed()

        async with endpoint(handler) as url:
            async with CDPClient(
                url, observer_factory=lambda identity: observer
            ) as client:
                task = asyncio.create_task(client.send_raw("Example.action"))
                await asyncio.wait_for(received.wait(), 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                cancelled = next(e for e in events if e.kind == "request_cancelled")
                self.assertEqual(cancelled.request_id, 1)
                self.assertEqual(cancelled.pending_count, 1)
                self.assertIn(1, client.pending_requests)
                self.assertTrue(client.pending_requests[1].cancelled())
                release.set()
                await asyncio.wait_for(reply_seen.wait(), 3)
                self.assertTrue(any(e.kind == "receive" for e in events))
                self.assertNotIn(1, client.pending_requests)
        self.assertTrue(any(e.kind == "receive_cancelled" for e in events))

    async def test_real_closed_socket_send_error_keeps_original_exception(self):
        events = []
        closed = asyncio.Event()

        async def handler(socket):
            await socket.close()
            closed.set()

        async with endpoint(handler) as url:
            client = CDPClient(url, observer_factory=lambda identity: events.append)
            await client.start()
            await asyncio.wait_for(closed.wait(), 3)
            await client.ws.wait_closed()
            if client._message_handler_task is not None:
                await client._message_handler_task
            with self.assertRaises(websockets.exceptions.ConnectionClosed):
                await client.send_raw("Example.action")
            pending = list(client.pending_requests.values())
            await client.stop()
            for future in pending:
                if future.done() and not future.cancelled():
                    future.exception()
        failed = next(e for e in events if e.kind == "send_error")
        self.assertEqual(failed.request_id, 1)
        self.assertIsNone(failed.raw)
        self.assertTrue(failed.exception_type.startswith("ConnectionClosed"))
        self.assertFalse(any(e.kind == "sent" for e in events))

    async def test_concurrent_threads_have_independent_client_scopes(self):
        barrier = threading.Barrier(2)

        def worker(label):
            async def run():
                events = []
                async with endpoint(echo) as url:
                    with observe_clients(lambda identity: events.append):
                        client = CDPClient(url)
                    await asyncio.to_thread(barrier.wait, 3)
                    async with client:
                        result = await client.send_raw(
                            "Example.action", {"owner": label}
                        )
                        self.assertEqual(result, {"ok": True})
                return label, events

            return asyncio.run(run())

        a, b = await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(worker, "a"), asyncio.to_thread(worker, "b")
            ),
            10,
        )
        self.assertNotEqual(a[1][0].client_id, b[1][0].client_id)
        self.assertNotEqual(a[1][0].owner_thread_id, b[1][0].owner_thread_id)
        self.assertNotEqual(a[1][0].owner_loop_id, b[1][0].owner_loop_id)
        for label, events in (a, b):
            requests = [json.loads(e.raw) for e in events if e.kind == "send"]
            self.assertEqual([r["params"]["owner"] for r in requests], [label])
            self.assertEqual(len({e.client_id for e in events}), 1)
            self.assertTrue(all(e.thread_id == e.owner_thread_id for e in events))
            self.assertTrue(all(e.loop_id == e.owner_loop_id for e in events))

    def test_raw_payload_is_excluded_from_default_representation(self):
        payload = "credential-sentinel-not-for-logs"
        event = TransportObservation(
            "client", "connection", 1, 1, 1, "receive", raw=payload
        )
        self.assertEqual(event.raw, payload)
        self.assertNotIn(payload, repr(event))

    async def test_serialized_payload_is_released_while_waiting_for_response(self):
        sent = asyncio.Event()
        reply = asyncio.Event()

        def observe(event):
            if event.kind == "sent":
                sent.set()

        async def handler(socket):
            request = json.loads(await socket.recv())
            await reply.wait()
            await socket.send(json.dumps({"id": request["id"], "result": {}}))
            await socket.wait_closed()

        async with endpoint(handler) as url:
            async with CDPClient(
                url, observer_factory=lambda identity: observe
            ) as client:
                task = asyncio.create_task(
                    client.send_raw("Example.action", {"large": "x" * 100_000})
                )
                try:
                    await asyncio.wait_for(sent.wait(), 3)
                    frame = task.get_coro().cr_frame
                    self.assertIsNotNone(frame)
                    self.assertNotIn("raw", frame.f_locals)
                    self.assertFalse(task.done())
                finally:
                    reply.set()
                self.assertEqual(await asyncio.wait_for(task, 3), {})

    async def test_async_task_inherits_scope_without_changing_existing_binding(self):
        first, second = [], []
        async with endpoint(echo) as url:
            with observe_clients(lambda identity: first.append):
                first_client = CDPClient(url)

                async def child():
                    with observe_clients(lambda identity: second.append):
                        async with CDPClient(url) as client:
                            return await client.send_raw("Example.action")

                task = asyncio.create_task(child())
            async with first_client:
                self.assertEqual(
                    await first_client.send_raw("Example.action"), {"ok": True}
                )
            self.assertEqual(await task, {"ok": True})
        self.assertNotEqual(first[0].client_id, second[0].client_id)
        self.assertEqual(len([e for e in first if e.kind == "send"]), 1)
        self.assertEqual(len([e for e in second if e.kind == "send"]), 1)


if __name__ == "__main__":
    unittest.main()
