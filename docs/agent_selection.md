# Agent selection


Block reports:. Code: `core/agent_selection.py`, `core/block_report.py` (byte-identical
in both repos). Built on `core/routing_penalty.py` (the run ledger).

## Selection
* Scopes: execution -> Executor Agent, acquisition -> Acquisition Agent, cognition -> ANALYST.
  **Overseer is never selectable.** This is separate from the model-selection router (cognition
  fallback chain); see `docs/routing_penalty.md` for how the two penalties relate.
* Window: the last **N = 10** completed runs of the agent (run ledger records). N can only change
  via `config/doctrine.grok` `AGENT_SELECTION: {WINDOW: n}`, which is a .grok change and so
  needs a Owner-signed migration. The key is absent today, so the default is 10.
* `routing_score = penalty ** lazy_in_window`. The penalty is the agent's .grok LAZINESS_PENALTY,
  the shared `high` = 0.5, so this equals the owner's `0.5 ** lazy`.
  1 lazy -> 0.5 eligible; 2 lazy -> 0.25 **blocked** (threshold 0.50).
* Honest failure (the executor reports `failed`) is not lazy and writes no run record. Lazy means
  the run claimed `done` without the required artifacts or evidence.

## Blocks are sticky (confirmed by Owner,)
After a block, lazy runs ageing out of the window do **not** unblock. Only a Owner-signed
record in `migrations/unblocks/*.grok` unblocks:

```
FORMAT: "grok/1"
KIND: "agent_unblock"
AGENT: "acquirer"
RESET_AFTER_HASH: "<hash of a run record of that agent>"
APPROVED_BY: "owner"
SIGNED_OFF: true
REASON: "..."
DATE: "2026-10-11"
```

The window and the block reset right after the named run record. A hash that is not a run of
that agent is `UNAUTHORIZED_PENALTY_CHANGE`. Selection records list the unblocks applied
(`unblocks_applied`), so a later unblock never rewrites history.

## Held, not dropped
A task whose agent is blocked is created in the backlog with status `blocked_agent` and is never
dispatched. Its reason is in `artifacts/evidence/<entry_id>.blocked_agent.json`. Each replay
re-selects held entries; once the agent is eligible the entry moves to `pending` (or
`awaiting_approval`) and Grok entries are dispatched.

## Records (hash-chained, `artifacts/evidence/routing/`)
* `routing_records.jsonl`: one per completed run (penalty spec), now with `evidence_path`.
* `selection_records.jsonl`: one per decision, with `task_id, scope, agent_selected, base_score,
  laziness_detected, penalty_level, penalty_value, routing_score, eligibility,
  window{size, lazy_count, run_ids}, timestamp, run_log_head, unblocks_applied, block_report`.
* `block_records.jsonl`: one per block, linking the trigger run hash to the report's sha256.

## Block report (every block)
`artifacts/evidence/blocks/<agent>-<UTC stamp>.json` + `.md`, written when the lazy run that
causes the block is reconciled:
* **WHO:** the agent blocked, issuer `overseer/routing-kernel`, approver needed `owner`.
* **WHAT:** score, threshold, lazy count, window run ids, tasks held at block time.
* **WHERE:** repo, commit sha, routing ledger path, evidence paths of the lazy runs.
* **WHEN:** each lazy run and the block, in CT and UTC.
* **HOW:** trigger run hash, the checks that fired per lazy run (ACQUISITION_NON_COMPLIANT /
  COGNITION_NON_COMPLIANT / EVIDENCE_OF_TRUTH), exact missing artifacts, record hashes.
* **WHY:** the doctrine statement and violated operational requirements quoted verbatim from
  `config/doctrine.grok`, LAZINESS_PENALTY, and the specific failure reasons.

The report is hash-linked into the chain (block ledger + `block_report` in selection records).
The audit marks it NON_COMPLIANT (`PENALTY_MISMATCH`) when a block has no report, a report is
missing or altered, or a report doesn't describe that block.

## Notification
Each block also writes `artifacts/outbox/notifications/<agent>-<stamp>.json` (`to: owner,
channel: chat, summary, report paths, delivered: false`). The repos cannot message anyone. A
Grok Bot routine reads the outbox and posts the summary to Owner in chat.

## CLI
`blocks` prints the reports, live per-scope states and held tasks. `status` includes the same
under `blocks`.

## Audit / stop conditions
Before every selection the run, selection and block ledgers are verified. Any finding (tamper,
inconsistent score/window/threshold, routed while blocked, block without report, bad unblock),
penalty drift, or a looser .grok penalty stops routing (`GovernanceError`). The Overseer audit
reports the same findings as NON_COMPLIANT and logs per-scope states (blocks included).
