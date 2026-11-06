# Client-bound transport observation

`observe_clients(factory)` binds a synchronous observer factory for clients
constructed in that context. Each client captures its factory once. Existing
clients keep their observer when scopes change; nested scopes reset normally.
Task inheritance follows `ContextVar` rules. Threads must explicitly propagate
context or pass the keyword-only `CDPClient(..., observer_factory=factory)`.
Explicit `None` disables observation, including inside an enabled scope.

The factory receives `ClientIdentity(client_id)` and returns a synchronous
callable accepting a frozen `TransportObservation`. No URL, request headers or
service-specific identity is passed implicitly. The caller binds its own opaque
execution/session context in the factory. Every transport start gets a new
connection ID and a sequence beginning at one, including a repeated start of
the same client. Replacement clients have different client IDs.

Each event also carries the process ID, the thread/loop IDs captured at that
connection's start, and the actual emitting thread/loop IDs. These are
process-local diagnostics and may be reused after a thread or loop ends; scope
them by connection ID, never treat them as execution authority. No loop object
is retained, and observation does not enforce or repair cross-loop use. A loop
ID is explicitly absent when no loop is running. Sequence is the per-client
emission order; wall and monotonic timestamps do not establish distributed-clock
ordering.

`send` contains the exact serialized string supplied to `ws.send`, after actual
request ID allocation. `sent` means that await completed; it does not establish
browser success. `send_error` retains only the original exception's type and
does not claim a provider/protocol response. Those two events carry the same
actual request ID without retaining the payload twice.

`receive` contains the exact `str` or `bytes` returned by `ws.recv`, before JSON
decoding or existing dispatch. These are decoded WebSocket messages, not TCP or
WebSocket framing bytes. A malformed or ID-less reply therefore remains
observable. The observer must not invent a request ID for it. Consumers can
correlate only an actual response ID on the same connection and must account
for their own bounded capture loss. `pending_count` is the actual client's
pending-future count at observation time, not a list of guilty commands.

Lifecycle kinds are `connection_start`, `connection_ready`, `connection_error`
and `connection_closed`. Closure is observed once per connection after a
received `ConnectionClosed` or a successful `ws.close`. `receive_error` records
the original receive/parse/handler failure, while `receive_cancelled` records
receive-task cancellation. Neither asserts physical socket closure.
`request_cancelled` records cancellation of the caller waiting for a response;
it does not remove the client's existing pending entry or claim that the browser
cancelled the command. Browser-use's original cancellation and timeout policy
remains in charge.

Observers must return promptly and must not perform hashing, storage, network
I/O or blocking work. A bounded, nonblocking queue is the intended adapter.
The library does not preempt a deliberately slow synchronous callback. Raw
messages can contain credentials or page content: raw capture, redaction,
access, retention, payload limits, loss counters and export belong to the
consumer. Nothing is logged or persisted automatically by this observation API.
The default event representation excludes the raw payload; explicitly reading
or serializing `raw` remains an access to sensitive content.

Optional factory/callback failures are isolated only at that diagnostic
boundary, including a callback explicitly raising `CancelledError`. They do not
replace original transport results, exceptions or real task cancellation.
`client.observation_health` exposes a frozen counter snapshot: enabled state,
factory errors, observer errors, emitted and delivered callbacks, and bounded
exception type. Exception text and raw payloads are absent from health data.
Async factories/callbacks are unsupported; accidental coroutine results are
closed without being awaited and count as diagnostic errors.

This adds no command allowlist, site policy, retry, response repair or browser
task. `tests/test_observation.py` exercises real loopback WebSocket connections
for wire identity, malformed/ID-less messages, original failures, cancellation,
client reuse, nested scopes and concurrent loops. These tests prove the
observation mechanism; they do not establish live-site behavior or qualify a
consumer's final image.
