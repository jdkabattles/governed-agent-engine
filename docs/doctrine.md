# Doctrine & identity

> Example doctrine: do the work completely, ground every claim in evidence, analyse every
> failure, and improve after each run.

That statement lives in `config/doctrine.yaml` and is pinned in code
(`core/doctrine.py: DOCTRINE_STATEMENT`). The loader rejects any rewording, and the running
agent stops if the file changes, the same way it treats `governance.yaml`.

## Knobs (`config/doctrine.yaml`)

| key | meaning | enforced in |
|---|---|---|
| `truth_priority: 1` | truth outranks speed, brevity and convenience. `1` is the only accepted value | system prompt; gate rule "gather before execute" |
| `evidence_required` | execute/verify steps must set `evidence_required`; a verify step must exist | `doctrine_gate.check_plan`, `check_execution_evidence` |
| `laziness_penalty` | each rejected plan multiplies that model's routing utility by this (per session) | `CognitionStrategy.penalize` |
| `failure_analysis_required` | execute steps must list `failure_modes`; every rejection is written as evidence | gate + `artifacts/evidence/doctrine/*.rejected.json` |
| `improvement_loop` | a `kind: improve` step must come after a `kind: verify` step | gate |
| `min_steps`, `min_step_chars` | lazy-plan floor; duplicate steps are also rejected | gate |

## Plan steps

`PlanStep` = `index, description, kind (gather|analyze|execute|verify|improve),
evidence_required, failure_modes`. Cognition models are asked for this JSON shape. Their system
prompt starts with the doctrine statement. Plans in the old numbered-text format still parse,
but their steps have no `kind`, so the gate rejects them.

**Why `kind == "improve"` and not substring matching:** a check like `"improve" in
description` passes for "verify, then improve nothing". Any model learns to sprinkle the word
in, so the check says nothing about whether the plan improves anything. A typed `kind`, placed
after a `verify` step, is a structural claim the gate can check, and a test proves that adding
the word "improve" to the text does not pass (`test_substring_improve_is_not_enough`).

## Enforcement flow

1. `planner.plan_task` asks the best-ranked model (from capability routing).
2. `doctrine_gate.check_plan` runs on the parsed plan. This happens for every plan, whichever
   model wrote it, before the backlog.
3. If the plan breaks the doctrine, the orchestrator writes
   `artifacts/evidence/doctrine/<task>.<attempt>.<endpoint>.rejected.json` (violations, model,
   the full plan, doctrine fingerprint). The model gets the laziness penalty and is excluded,
   and the next model in the fallback chain is asked (up to 4 models).
4. If every model fails, `DoctrineError` is raised and nothing enters the backlog. The CLI
   exits with code 5, and inbox files move to `rejected/`.

**Evidence of truth at execution:** when Grok reports `done`, every `evidence_required` step
of the entry's plan needs `outputs.evidence["<step index>"]` to name a non-empty file under
an allowed artifact root. Otherwise the entry is marked **FAILED**, and
`artifacts/evidence/<entry>.evidence_check.json` records why. Grok Bot's routine contract
gains this field.

## Identity & authority (`core/identity.py`)

| identity | type | authority |
|---|---|---|
| `owner` (HUMAN_OWNER) | human | 100 |
| `grok` (EXECUTOR_GROK) | executor | 10 |
| `cognition:<endpoint>` | cognition | 1 |
| missing / unrecognised | – | 0 |

* `Task.issued_by` names the issuer. `Task.provenance` lists the ancestor task ids, root
  first, and its length must equal `depth`.
* **Top-level tasks** (`depth 0`) need a human issuer with authority ≥
  `governance.process.min_top_level_authority` (default 100, bounded 50–100, so config can
  never let an executor or cognition model start work).
* **Child tasks** can be issued by any known identity, within `max_recursion_depth`. The
  provenance chain must end at `parent_task_id`, match the parent's recorded chain in the
  backlog, and lead back to a root issued by sufficient human authority.
* Identities are code, not config: a YAML edit cannot mint a new high-authority principal.
  A non-human identity is not allowed to hold authority ≥ 50.

### Who is the issuer?
`gae submit` stamps `issued_by: owner` when the task names no issuer: the person at the
terminal is the human (`--as` overrides this). **Inbox files get no default.** `artifacts/inbox/`
can be filled by other processes (e.g. a git-synced Grok queue), so a file without a valid human
`issued_by` is rejected for top-level work.
