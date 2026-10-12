# APCK governance

Defined in `config/governance.yaml`, typed as `GovernanceConfig` (`core/models.py`), enforced in
`orchestrator/governance.py`.

| Pillar | Policy object | Enforced rules |
|---|---|---|
| **A**rtifacts | `ArtifactsPolicy` | writes only under `write_roots` (default `artifacts/`); `done` requires an evidence file |
| **P**rocess | `ProcessPolicy` | fixed lifecycle; `allowed_task_types`; human-approval actions only enter the backlog as `awaiting_approval` and only with `human_approval: true` |
| **C**onstraints | `ConstraintsPolicy` | deny-by-default `allowed_actions`; `max_recursion_depth` (sub-tasks must name `parent_task_id`); `max_inputs_bytes`; `cost_ceiling_usd` must be 0.0; `governance_mutable` must be false |
| **K**nowledge | `KnowledgePolicy` | only `allowed_endpoints` may be consulted; `sensitive` tasks never go to `third_party_proxy`/`community` endpoints unless explicitly allowed; cognition output is advisory only |

## Hard-coded, not configurable

* `HUMAN_APPROVAL_ACTIONS = {send_message, delete, purchase, publish, trade, modify_governance}`
  is unioned with config; YAML can add to it, never remove from it.
* `assert_allowed(action, {"auto": True})` refuses every human-approval action. No flag
  disables this.
* `modify_governance` is refused even with `human_approval: true`: governance changes happen
  only by a human editing `config/governance.yaml` in a reviewed commit and restarting.
* A running agent re-fingerprints `governance.yaml` before every cycle and every submit and
  halts if it changed.
* `cost_usd` on every endpoint and `cost_ceiling_usd` must be exactly 0.0 or config won't load.

## Approving a gated entry

Change only `status` from `awaiting_approval` to `approved` in the backlog JSON (append a
`status_history` item), commit, push. Grok Bot picks it up on its next run.


## Authority & doctrine (added 2026-10-11)

P also covers authority: top-level tasks need a human issuer (`process.min_top_level_authority`,
50–100), and child tasks need a provenance chain back to such a root. The doctrine gate runs on
every plan before the backlog. Details: [doctrine.md](doctrine.md).
