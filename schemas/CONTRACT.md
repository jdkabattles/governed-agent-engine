# Handoff contract v2 (shared by example-executor-backend and governed-agent-engine)

Both repos read and write the same JSON for the Grok Bot handoff, so either orchestrator can
drain the other's backlog and Grok Bot needs one parser. The golden files in
`schemas/contract/` are **byte-identical in both repos** and each repo's test suite
(`tests/test_contract.py`) parses and round-trips every one of them.

| document | path | v2 shape (writers) |
|---|---|---|
| Task | inside backlog entries / `artifacts/inbox/*.json` | `task_id, type, action, inputs, requires_grok, priority, human_approval, sensitive, depth, parent_task_id, issued_by, provenance` |
| Plan | `artifacts/plans/<entry_id>.plan.json` | `task_id, endpoint, model, summary, steps[{index, description, kind, evidence_required, failure_modes}], raw_text, created_at` |
| BacklogEntry | `artifacts/backlog/<entry_id>.json` | `schema_version: 2, entry_id, task, plan_path, created_at, status, requires_human_approval, executor, status_history[{status, at}]` |
| GrokResult (evidence) | `artifacts/evidence/<entry_id>.json` | `schema_version, entry_id, task_id, status: done|failed, summary, outputs, started_at, finished_at` |
| Heartbeat | `artifacts/evidence/heartbeat.json` | `schema_version, agent, ts, note` |

Evidence-of-truth: plan steps with `evidence_required: true` must be backed by
`GrokResult.outputs.evidence = {"<step index>": "<repo-relative path under artifacts/>"}`
(non-empty file), otherwise a `done` result is recorded as `failed`.

Readers accept contract v1 (example-executor-backend 0.1): `Plan.provider` (now `endpoint`),
`status_history` as `[status, at]` pairs, `PlanStep.action` (ignored), missing
`BacklogEntry.executor` (= `grok`), missing identity fields (= unknown issuer, depth 0).

JSON Schemas: example-executor-backend generates `schemas/*.schema.json` from its Pydantic models
(`scripts/export_schemas.py --check` in CI); governed-agent-engine maintains equivalent
hand-written schemas validated against model output in its tests. Change the contract in both
repos together, regenerate the golden files, and copy them verbatim.

## Status `blocked_agent` (2026-10-11)

Agent selection (`docs/agent_selection.md`) holds a task whose agent is blocked as
`blocked_agent`. The entry stays in the backlog and is never dispatched. It moves to `pending` /
`awaiting_approval` when the agent is eligible again (Owner-signed unblock), or to `rejected`.
Readers that predate this status must treat it as not executable.
