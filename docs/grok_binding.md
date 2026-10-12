# .grok binding (the owner's binding spec, 2026-10-11)

Native code is **primary and executable** (`core/identity.py`, `config/doctrine.yaml`).
The `.grok` files are the **read-only constitutional mirror and audit source of truth**. Startup
binds them and fails fast with a named error code; the runtime exposes one `AgentIdentity` per
agent (always the native object). Implementation: `core/grok_binding.py` (identical in both repos).

## Files

| file | content |
|---|---|
| `config/identity.grok`, `config/doctrine.grok`, `config/directive.grok` | shared spec (unchanged layout; see [grok_format.md](grok_format.md)) |
| `config/agents/{overseer,grok,acquirer,analyst}.grok` | per-agent package in the expanded schema |
| `migrations/*.grok` | migration records signed off by Owner (not part of the fingerprint) |

Expanded package schema:

```
FORMAT: "grok/1"
AGENT: "<bot>"
IDENTITY: {NAME, TYPE, AUTHORITY_LEVEL, HUMAN_AUTHORITY{NAME, AUTHORITY_LEVEL},
           OVERSEER_AUTHORITY{NAME, AUTHORITY_LEVEL}, DESCRIPTION}
DOCTRINE: {STATEMENT, RULES{TRUTH_PRIORITY low|medium|high|highest, EVIDENCE_REQUIRED,
           LAZINESS_PENALTY low|medium|high|maximum, FAILURE_ANALYSIS_REQUIRED,
           IMPROVEMENT_LOOP optional|recommended|mandatory}, OPERATIONAL_REQUIREMENTS[]}
DIRECTIVE: "<freeform>"
RESPONSE_WINDOW: {CLARIFICATION_REQUIRED: true, CLARIFY_WITH: ["Owner", "Overseer Agent"]}
```

(Written in block style; JSON-like braces above are notation only.) `agents/grok.grok` carries
the owner's Executor Agent directive verbatim and must equal `config/directive.grok`.
All four packages carry the owner's directive verbatim (`DIRECTIVE_SOURCE: "owner"`):
grok (same as `config/directive.grok`), acquirer (+ `ACQUISITION`,
[acquisition_agent.md](acquisition_agent.md)), overseer (+ `GOVERNANCE`) and analyst
(+ `COGNITION`) . RESPONSE_WINDOW adds
`CLARIFY_OWNER_ON` / `CLARIFY_OVERSEER_ON` topic lists and a verbatim `TEXT`; Overseer clarifies
only with Owner (`CLARIFY_WITH: ["Owner"]`), the others with Owner then Overseer.

## Error codes

| code | raised when |
|---|---|
| `IDENTITY_MISMATCH` | package/`identity.grok` field differs from native identity; package missing/unknown/malformed |
| `DOCTRINE_MISMATCH` | statement or a rule differs between doctrine.grok, the packages and native `config/doctrine.yaml` |
| `DIRECTIVE_MISMATCH` | DIRECTIVE contradicts native name/type/authority (agent_name, agent_type, authority_level, Owner 100, Overseer Agent 50), omits the doctrine or "Never override Owner.", or hierarchy Owner > Overseer > agent fails |
| `GOVERNANCE_MISMATCH` / `COGNITION_MISMATCH` | overseer.grok GOVERNANCE / analyst.grok COGNITION differs from `core/scopes.py` |
| `ACQUISITION_MISMATCH` | acquirer.grok ACQUISITION differs from the native policy (`core/acquisition.py`) |
| `CLARIFICATION_RULE_MISSING` | RESPONSE_WINDOW does not require clarification with Owner, then Overseer Agent |
| `NON_COMPLIANT` | audit status of an agent with any finding (blocks execution: startup refuses) |
| `DOCTRINE_DRIFT` | audit: a package or native doctrine drifted from doctrine.grok |
| `UNAUTHORIZED_GROK_CHANGE` | charter files changed while running without a matching signed migration |

Native mapping: TRUTH_PRIORITY high/highest ⇔ `truth_priority: 1`; EVIDENCE_REQUIRED and
FAILURE_ANALYSIS_REQUIRED equal the native booleans; LAZINESS_PENALTY level ⇒ native factor ≤
low 0.9 / medium 0.75 / high 0.5 / maximum 0.25; IMPROVEMENT_LOOP mandatory ⇔
`improvement_loop: true`. So every enforced native rule is derivable from doctrine.grok.
The LAZINESS_PENALTY thresholds (0.9 / 0.75 / 0.5 / 0.25) were **approved by Owner on
2026-10-11**.

## Overseer Agent audit

`example-executor-backend --root . audit` / `gae audit` runs every check without stopping at the first,
writes `artifacts/evidence/audits/audit-<UTC>.json` (also written automatically at every orchestrator start) (auditor `overseer`, charter fingerprint,
per-agent `COMPLIANT`/`NON_COMPLIANT` with findings, global findings) and exits 0 when
compliant, 6 otherwise.

## Drift check and migration path

The orchestrator re-computes the charter fingerprint (identities/, doctrine/, config/*.grok,
config/agents/*.grok) before submit, replay, approve, block and every run cycle. A change halts
with `UNAUTHORIZED_GROK_CHANGE` unless `migrations/` contains a record:

```
FORMAT: "grok/1"
KIND: "grok_migration"
FROM_FINGERPRINT: "<running sha256>"
TO_FINGERPRINT: "<new sha256>"
APPROVED_BY: "owner"
SIGNED_OFF: true
REASON: "<why>"
DATE: "<YYYY-MM-DD>"
```

and the new files bind cleanly; then the new fingerprint is adopted. Get fingerprints from
`audit` (charter_fingerprint) before and after editing. The record is an attestation that
Owner commits, not a cryptographic signature; protect `migrations/` with branch protection.
"Never override Overseer Agent" stays bounded: Overseer cannot override Owner or approve
human-approval actions.
