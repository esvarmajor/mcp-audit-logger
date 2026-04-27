# Contributing to mcp-audit-logger

Thanks for considering a contribution! This is a small, intentionally
narrow project — the bar for new code is "does it make the tool a
better auditor?" rather than "does it add a feature."

## Quick start

```bash
git clone https://github.com/esvarmajor/mcp-audit-logger.git
cd mcp-audit-logger
make install      # creates .venv and installs in editable mode
make ci           # ruff + mypy + pytest — must be green before PR
make smoke        # boots the proxy and round-trips an MCP call
```

## What lives where

| Path                              | What you'll find                          |
| --------------------------------- | ----------------------------------------- |
| `src/mcp_audit_logger/server.py`  | Audit-tool definitions and dispatch       |
| `src/mcp_audit_logger/storage.py` | The SQLite store — all `audit_*` SQL      |
| `src/mcp_audit_logger/proxy.py`   | Downstream session lifecycle              |
| `src/mcp_audit_logger/__main__.py`| CLI entry, lifespan, ASGI routing         |
| `src/mcp_audit_logger/metrics.py` | `/metrics` text-format renderer           |
| `tests/`                          | One test file per source module           |
| `scripts/smoke.py`                | End-to-end smoke check (subprocess + SDK) |
| `docs/`                           | Design notes (e.g. reconnection)          |
| `GAMEPLAN.md`                     | Running list of done / open work          |

## Style

- Type-annotate everything — `mypy` runs in CI under
  `--ignore-missing-imports` plus `warn_unused_ignores` /
  `check_untyped_defs`.
- Comments earn their place: explain the *why*, not the *what*. The
  existing modules have a 4-line module docstring at the top — keep
  that pattern.
- Default to fewer lines, fewer abstractions. The whole point of this
  project is that it's small.

## Tests

- Unit tests live alongside their target module under `tests/`. We use
  `pytest` with `pytest-asyncio` in auto mode.
- For SQL changes, add a test in `tests/test_storage.py` that exercises
  the new behavior end-to-end against an on-disk SQLite file (use
  `tmp_path`).
- For new audit tools, also add an in-process server integration test
  in `tests/test_server.py` using `create_connected_server_and_client_session`.
- For routing/wire changes, update `scripts/smoke.py` so the next CI
  run catches the regression.

## Out of scope

Please don't open PRs for these without discussing first:

- Resources / prompts pass-through (this is a *tool-call* auditor).
- Multi-downstream fan-out.
- Non-SQLite storage backends.
- A web UI for browsing `audit.db` (the `audit_*` MCP tools are the UI).

## Submitting a PR

1. Fork, branch, push.
2. `make ci` and `make smoke` must both pass.
3. Update `CHANGELOG.md` under the Unreleased section.
4. Update `README.md` if the change is user-visible.
5. Open the PR with the populated template; link any related issue.

## License

By contributing, you agree your changes are licensed under the
project's MIT license. See [LICENSE](LICENSE).
