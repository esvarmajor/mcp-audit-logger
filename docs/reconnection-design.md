# Downstream Reconnection Design

## Problem

`DownstreamProxy` holds a single long-lived `ClientSession`. If the downstream
process crashes or the TCP connection drops, the next `call_tool` raises an
exception, the caller gets a logged error row, and every subsequent call fails
the same way until the proxy is restarted. There's no self-healing.

## Constraints

1. **anyio cancel-scope ownership.** The `stdio_client` and
   `streamable_http_client` context managers create cancel scopes that must be
   entered *and* exited by the same task. The initial implementation hit a
   `RuntimeError: Attempted to exit cancel scope in a different task` when the
   proxy lifecycle was split across tasks. Any reconnection design must ensure
   a single task owns the entire `start → run → stop` cycle.

2. **No blocking callers.** `call_tool` is called from inside the MCP SDK's
   session handler. Blocking it indefinitely during reconnect would stall the
   upstream client's entire request.

3. **Audit rows still required.** A call that fails during a reconnect window
   must still land an audit row with `success=False`.

4. **Backoff must not spiral.** Tight reconnect loops under a dead downstream
   would burn CPU and spam logs.

## Proposed Design

### Supervisor task

Extract session lifecycle into a **supervisor task** that lives for the entire
lifespan of the proxy and owns all anyio cancel scopes. Callers interact
through an `anyio.Event` (session-ready gate) and a shared state enum.

```
                ┌─────────────────────────────────────┐
                │          DownstreamProxy             │
                │                                      │
  call_tool ──► │  await _ready.wait()  ──► _session  │
                │                                      │
                │  _supervisor_task (background)       │
                │    loop:                             │
                │      _start()                        │
                │      set _ready                      │
                │      await _failed.wait()            │
                │      clear _ready                    │
                │      await sleep(backoff)            │
                └─────────────────────────────────────┘
```

### State machine

```
DISCONNECTED  →  CONNECTING  →  CONNECTED
     ▲                               │
     └───────── RECONNECTING ────────┘
                     ▲
                     │  exception in call_tool or health check
```

### New `DownstreamProxy` interface

```python
class DownstreamProxy:
    async def start(self) -> None:
        """Start the supervisor. Non-blocking after first connection."""

    async def stop(self) -> None:
        """Cancel supervisor, close transport. Idempotent."""

    async def call_tool(self, name, arguments) -> Any:
        """Forward a call. Waits up to CALL_TIMEOUT for a live session.
        Raises DownstreamUnavailableError if no session within the window."""

    async def list_tools(self) -> list[Tool]:
        """Same availability contract as call_tool."""
```

### Backoff schedule

| Attempt | Wait |
|---------|------|
| 1       | 1 s  |
| 2       | 2 s  |
| 3       | 4 s  |
| 4+      | 30 s |

Cap at 30 s. Reset to 1 s on first successful call after reconnect.

### Call timeout during reconnect

`call_tool` waits up to **5 s** for `_ready` to be set. If the supervisor
hasn't re-established the session in time, raise `DownstreamUnavailableError`.
The caller in `server._proxied_call` catches this and returns an `isError=True`
result with the audit row logged.

### anyio cancel-scope safety

The supervisor task does:

```python
async def _supervisor(self) -> None:
    while not self._stopping:
        async with AsyncExitStack() as stack:
            try:
                # All cancel scopes entered here; same task exits them.
                read, write = await stack.enter_async_context(transport_ctx)
                session = await stack.enter_async_context(ClientSession(...))
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._failed.wait()          # blocks until health failure
            except* Exception as eg:
                log.warning("downstream.disconnected", ...)
            finally:
                self._ready = anyio.Event()        # reset for next attempt
                self._session = None
        await anyio.sleep(self._next_backoff())
```

`start()` launches `_supervisor` as a task via `anyio.create_task_group`, which
is started **inside** `session_mgr.run()` so everything shares the same anyio
backend event loop and task scope (same fix that resolved the original bug).

### Health detection

Two triggers reset `_ready` and fire `_failed`:

1. **Exception in `call_tool`.** Any `Exception` from `session.call_tool` is
   caught; the session is considered dead.
2. **Periodic ping (optional).** A secondary task inside the supervisor loop
   sends `tools/list` every 30 s. A timeout or exception marks the session
   dead. This catches silent TCP drops where calls happen infrequently.

### What this does NOT cover

- Multiple simultaneous in-flight calls during reconnect: each one gets a
  "reconnecting" error. This is intentional — do not queue calls.
- stdio process restart: the supervisor re-spawns via `StdioServerParameters`
  exactly as if it were the first start.

## Files to change

| File | Change |
|------|--------|
| `proxy.py` | Rewrite `DownstreamProxy` with supervisor task |
| `server.py` | Catch `DownstreamUnavailableError` in `_proxied_call` |
| `__main__.py` | Pass task group handle into `proxy.start()` (or restructure lifespan to use `tg.start_soon`) |
| `tests/test_proxy.py` | New test file: supervisor restart, backoff, call-timeout during reconnect |

## Open questions

1. Should the health-ping task be enabled by default or opt-in via
   `AUDIT_HEALTH_PING_INTERVAL`? Default-on is safer; default-off avoids
   surprise `tools/list` calls on downstream servers that rate-limit.
2. For HTTP downstream, `streamable_http_client` creates a new HTTP session per
   invocation; reconnect is just re-entering the context. Should HTTP get
   a shorter initial backoff (500 ms)?
