# governed-agent-engine

[![ci](https://github.com/jdkabattles/governed-agent-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/jdkabattles/governed-agent-engine/actions/workflows/ci.yml)

A small, **governed multi-agent engine** that installs on an Android phone (Termux) with one
command and runs at **$0**. The core is Python stdlib plus PyYAML: no Rust, no compiler.

* **Orchestrator loop:** inbox → normalize → validate (governance) → route → execute or plan →
  backlog → replay → evidence.
* **Governance framework (APCK):** Artifacts / Process / Constraints / Knowledge. Hard-coded
  human-approval actions, recursion limits, and halting if governance changes while running.
* **Doctrine gate mechanism:** every plan from any model is checked (minimum steps, typed step
  kinds, evidence, failure modes, a mandatory improve step) before it reaches the backlog.
* **`.grok` spec format + binding / audit / migrations:** a strict YAML subset any language can
  parse. Agent packages are bound to native code at startup, an `audit` command writes
  evidence, and drift detection only accepts owner-signed migration records.
* **Agent selection + routing penalty:** a rolling window of runs per agent, hash-chained routing
  logs, sticky blocks with block reports, and owner-signed unblock records.
* **Free cognition adapters:** Pollinations, LLM7, OVH, AI Horde, OpenRouter free, Gemini, and
  Groq. Health tracking and rate limits, plus cross-checks (JSON repair, code verification,
  honesty audit, majority vote).
* **Executor transport:** a git/file queue (backlog out, evidence and heartbeat back). It works
  with any executor that can read and write files, such as a scheduled Grok Bot routine.

Everything personal is an **example** you replace: the doctrine statement, the four example
agents (owner, overseer, executor, acquirer, analyst), their directives, and the capability
scores (neutral 0.5 until you run `gae bench`).

## Install

### Termux (Android)

```bash
curl -fsSL https://raw.githubusercontent.com/jdkabattles/governed-agent-engine/main/termux/install.sh | bash
```

or

```bash
git clone https://github.com/jdkabattles/governed-agent-engine && cd governed-agent-engine && bash termux/install.sh
```

The installer runs `pkg install python git` (prebuilt packages only), creates a venv, runs
`pip install -e .`, puts a `gae` launcher on `PATH` and validates the config.
Overrides: `GAE_DIR`, `GAE_REPO_URL`, `GAE_BRANCH`, `GAE_RUN_ONCE=1`.

### Linux / macOS

```bash
git clone https://github.com/jdkabattles/governed-agent-engine && cd governed-agent-engine
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

## Use

```bash
gae validate                     # load + validate all config
gae status                       # executor state in plain words, backlog counts, blocks
gae example --out task.json      # write a valid example task (--type research|market_watch|...)
gae submit examples/task.json    # or --json '{...}' or stdin
gae run --once                   # one cycle
gae audit                        # overseer audit -> artifacts/evidence/audits/
gae blocks                       # agent blocks, scores, held tasks
gae smoke                        # LIVE ping of enabled endpoints (opt-in, uses network)
gae bench --write-capabilities   # LIVE capability benchmark -> config/capabilities.yaml
```

Bad input prints one line (`gae: error: ...`) and exits non-zero. `GAE_DEBUG=1` shows a
traceback. Optional free keys come from the environment only: `OPENROUTER_API_KEY`,
`GEMINI_API_KEY`, `GROQ_API_KEY`.

## Make it yours

1. **Doctrine:** set your statement in `core/doctrine.py` `DOCTRINE_STATEMENT`,
   `config/doctrine.yaml`, `doctrine/doctrine.yaml`, `config/doctrine.grok` and every
   `config/agents/*.grok`. The binding keeps all copies identical (startup fails otherwise).
2. **Agents:** identities are fixed in `core/identity.py` and restated in `identities/*.yaml`,
   `config/identity.grok` and `config/agents/*.grok`. Edit all of them together; see
   `docs/grok_binding.md`.
3. **Directives:** replace `DIRECTIVE` / `RESPONSE_WINDOW` in `config/directive.grok` and the
   agent packages. Required structure: `docs/grok_binding.md`.
4. **Capabilities:** `gae bench --write-capabilities` measures your providers.
5. After the first deployment, record every change to these files as an owner-signed
   `migrations/*.grok` record (drift protection).

## Using the engine from a private deployment

Keep your own doctrine, agents and history in a private repo and consume this engine as a
dependency:

```bash
pip install "governed-agent-engine @ git+https://github.com/jdkabattles/governed-agent-engine@v0.1.0"
gae --root /path/to/private-config-repo status
```

`--root` (or `GAE_ROOT`) points the engine at a directory holding your `config/`,
`identities/`, `doctrine/`, `migrations/` and `artifacts/`. The doctrine statement and
identity table are still pinned in code (`core/doctrine.py`, `core/identity.py`). A private
deployment that changes them carries a small overlay of those two modules until they become
configurable. See `docs/private_deployment.md`.

## Docs

`docs/architecture.md` · `docs/governance.md` · `docs/doctrine.md` · `docs/grok_format.md` ·
`docs/grok_binding.md` · `docs/routing_penalty.md` · `docs/agent_selection.md` ·
`docs/decisions/ADR-001-language.md` · `schemas/CONTRACT.md`

## License

MIT © 2026 Jonathan D Battles. See `LICENSE`. Borrowed ideas and licence notes: `docs/references.md`.
