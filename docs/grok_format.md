# The `.grok` spec format

`config/identity.grok`, `config/doctrine.grok` and `config/directive.grok` hold the owner's
language-agnostic spec. `.grok` is a **strict subset of YAML 1.2**: every `.grok` file is valid
YAML and any YAML loader returns the same value, but the grammar is small enough to parse
without a YAML library in any language. Reference parser: `core/grok_format.py` (stdlib only,
byte-identical in example-executor-backend and governed-agent-engine).

## Grammar

```
file     := line*                       UTF-8, LF line endings, no tabs, no BOM
line     := blank | comment | entry | item
comment  := ws* "#" any*                (also allowed after a value, preceded by a space)
entry    := indent KEY ":" ( " " scalar )? ws* comment?
item     := indent "- " scalar ws* comment?
KEY      := [A-Za-z][A-Za-z0-9_]*
scalar   := "null" | "true" | "false" | int | dqstring
int      := "-"? ( "0" | [1-9][0-9]* )
dqstring := '"' ( char except '"' and '\' | '\' ( '"' | '\' | 'n' | 't' ) )* '"'
indent   := exactly two spaces per nesting level
```

* An `entry` with no scalar opens a nested block at exactly one more indent level: either a
  mapping (entries) or a sequence (items), never both, never empty.
* The top level is a mapping. Duplicate keys are errors.
* Not allowed: plain (unquoted) strings, single quotes, flow style (`{}` `[]`), anchors,
  aliases, tags, multi-line or block scalars (`|` `>`), floats, keys other than `KEY`.
* Every file starts with `FORMAT: "grok/1"`.
* the owner's wording is kept verbatim; bare words like `high` are written `"high"` because
  `.grok` only allows double-quoted strings, and multi-line text uses `\n` escapes.

## Consistency (blocks startup)

`core/charter.py` checks, on every start:

| file | must match |
|---|---|
| `identity.grok` | every bot in `core/identity.py` / `identities/*.yaml`: NAME, DISPLAY_NAME, FORMER_NAME, ID, TYPE, AUTHORITY; FINAL_AUTHORITY = owner; COGNITION_ENDPOINT_AUTHORITY = 1; BOUNDS (overseer cannot override owner; only owner approves the six human-approval actions) |
| `doctrine.grok` | STATEMENT verbatim; RULES exactly the owner's (TRUTH_PRIORITY high, EVIDENCE_REQUIRED true, LAZINESS_PENALTY high, FAILURE_ANALYSIS_REQUIRED true, IMPROVEMENT_LOOP mandatory); OPERATIONAL_REQUIREMENTS a non-empty list of his sentences, whose "at least N steps" must equal `doctrine/doctrine.yaml` min_plan_steps |
| `directive.grok` | ISSUED_BY owner; BOUNDS fixed (cannot change authority or approve human-approval actions); STATUS `pending` or `active`; when `active`, DIRECTIVE and RESPONSE_WINDOW are non-empty strings (the owner's text verbatim, line breaks as `\n`) and DIRECTIVE quotes the doctrine statement verbatim |

The three files are part of the charter fingerprint: changing them while the orchestrator
runs halts `submit`, `replay`, `approve` and `block` until restart.

## "Never override Overseer Agent" is bounded

Bots below the overseer (Executor Agent, Acquisition Agent, Analyst Agent, free model
endpoints) must not override Overseer Agent's review, flag or block decisions. That rule is
bounded: **Overseer cannot override Owner, and can never approve human-approval actions**
(send_message, delete, purchase, publish, trade, modify_governance), whatever any `.grok`,
YAML or governance file says. The code enforces it (`identity.can_approve`,
`governance.authorize_approval`).

Binding to the runtime, per-agent packages, error codes, audit and migrations: [grok_binding.md](grok_binding.md).
