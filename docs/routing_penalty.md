# Agent-level routing penalty

Code:
`core/routing_penalty.py` (byte-identical in both repos), wired through `core/grok_binding.py`
and the orchestrator's evidence reconciliation.

## Two penalties, no double counting

| | per-cognition-MODEL penalty (existing) | agent-level ROUTING penalty (this) |
|---|---|---|
| who | a free model endpoint (Groq, Gemini, ...) | grok, acquirer, analyst |
| when | a model's PLAN fails the doctrine gate, before the backlog | a reconciled run lacks required artifacts / evidence |
| value | `doctrine.yaml laziness_penalty` (0.5) on that model's fallback score | the agent's LAZINESS_PENALTY from `.grok` |
| effect | demotes the model in the cognition fallback chain | `ROUTING_RECORD` in the ledger; `current_scores()` |

A plan rejected by the doctrine gate never reaches the backlog, so it can never also produce an
agent routing record. A run that reaches reconciliation is only scored at the agent level.
Each failure is penalised once, at the layer that caused it.

## Rules
* Levels: low 0.90, medium 0.75, high 0.50 (shared baseline `high` for grok/acquirer/analyst).
  `maximum` 0.25 is Overseer-only: used by the overseer's audit evaluator, `enters_routing: false`, never
  in the routing table, never propagated. A routed agent set to `maximum` fails startup.
* Per-agent `DOCTRINE_OVERRIDE.RULES.LAZINESS_PENALTY` may only tighten. Looser or unknown
  values stop startup with `PENALTY_MISMATCH`.
* `routing_score = base_score * penalty` if lazy, else `base_score`. Rounded to 6 decimal places.
  Spec examples (tested): 1.0->0.5, 0.8->0.4, 0.6->0.3.
* Laziness detected = the existing reconciliation checks returned violations: missing evidence for
  `evidence_required` steps, acquisition data/logs/report (acquirer), cognition reasoning
  log/summary/refinement notes (analyst). Missing failure_modes / improve steps are caught earlier
  by the doctrine gate and charged to the model, not the agent (see above).
* Agent attribution: acquisition-scoped -> acquirer, cognition-scoped -> analyst, else grok.

## ROUTING_RECORD ledger
`artifacts/evidence/routing/routing_records.jsonl` (plus `evidence_path`; agent selection: `docs/agent_selection.md`), one JSON object per line with the spec fields
(`agent, laziness_detected, penalty_level, penalty_value, base_score, final_score,
doctrinal_source, timestamp`) plus `task_id`, `prev_hash` and `hash`. The genesis `prev_hash` is 64 zeros, and
`hash = sha256(canonical JSON of the record without hash)`. Editing, deleting or reordering a
line breaks the chain. This is tamper-evident, not tamper-proof: an attacker with write access
could rewrite the whole chain, so the ledger is also committed with the artifacts.

## Audit and drift
* `audit` (and the startup audit) checks the penalties resolve from `.grok`, none looser, and the
  ledger is complete, consistently scored and unbroken. Findings mark the audit NON_COMPLIANT:
  * `PENALTY_MISMATCH`: invalid/looser config, incomplete record, inconsistent final_score, or
    a non-routed agent (e.g. overseer) in the ledger.
  * `UNAUTHORIZED_PENALTY_CHANGE`: broken hash chain or a record whose penalty differs from
    the approved `.grok` table.
* Drift: `DriftMonitor` reloads before each cycle/replay. When the charter changed without a
  Owner-signed migration and the penalty table moved, it raises `PENALTY_DRIFT_DETECTED`
  (routing stops, orchestrator halts; the next audit records it for Overseer). A matching migration
  reloads the table.
