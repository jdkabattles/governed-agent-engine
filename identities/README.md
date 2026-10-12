# identities/ — the multi-agent system (+ free model endpoints)

One file per principal. The **code is the source of truth** (`core/identity.py`); these files
restate each bot for humans and other tools and are verified at load by `core/charter.py`.
Any mismatch (name, id, type, authority, display name), a missing or extra file, a non-human
that does not forbid every `approve:<action>`, or an escalation path that does not go strictly
upward to Owner stops the orchestrator. Their SHA-256 is recorded at start; changing them
while it runs halts `submit`/`replay` until restart.

| file | bot | former name | type | authority | agent id |
|---|---|---|---|---|---|
| `owner.yaml` | Owner | – | human | 100 (final) | – |
| `overseer.yaml` | Overseer Agent | – | overseer | 50 (exactly, hard-coded) | 00000000-0000-4000-8000-000000000050 |
| `grok.yaml` | Executor Agent | – | executor | 10 | 00000000-0000-4000-8000-000000000010 |
| `acquirer.yaml` | Acquisition Agent | – | acquisition | 5 | 00000000-0000-4000-8000-000000000005 |
| `analyst.yaml` | Analyst Agent | – | cognition | 3 | 00000000-0000-4000-8000-000000000003 |
| (none) | free model endpoints | – | cognition | 1 (`cognition:<endpoint>`) | – |

Rules (code-enforced): only humans hold authority >= 50, except the overseer at exactly 50.
Top-level tasks need Owner; the overseer may issue them only if `governance.yaml` sets
`overseer_may_issue_top_level: true`. Only a human approves human-approval actions
(send_message, delete, purchase, publish, trade, modify_governance), never the overseer.
The overseer and Owner can block (`block <entry> --reason ... --as overseer`), with a decision
record in `artifacts/evidence/decisions/`. Bots may create child tasks within
`max_recursion_depth` with a provenance chain to a human-issued root.

`issued_by` accepts the bot name (`overseer`) or its agent id.

`config/identity.grok` restates this table in the `.grok` format (docs/grok_format.md) and
must match it exactly. "Never override Overseer Agent" is bounded: Overseer cannot override
Owner and can never approve human-approval actions.

