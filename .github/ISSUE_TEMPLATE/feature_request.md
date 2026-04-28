---
name: Feature request
about: Propose a new audit_* tool, config option, or transport feature
title: "feature: "
labels: enhancement
---

## What problem does this solve?

<!-- Describe the use case. "I'm trying to do X but the proxy doesn't help me Y." -->

## Proposed change

<!-- A new audit_* tool, a new config knob, a new metric series, etc.
Be concrete — what would the input/output look like? -->

## Alternatives considered

<!-- Did you try working around it with audit_search_arguments,
audit_export_jsonl + jq, etc.? What hit a wall? -->

## Out of scope

The project is intentionally narrow. We do **not** plan to add:
- Resources / prompts pass-through (only tool calls).
- Multi-downstream fan-out (one logger, one downstream).
- Non-SQLite storage backends.
- A web UI.

If your request falls into one of these, please open a discussion thread
instead so we can talk about whether it should change.
