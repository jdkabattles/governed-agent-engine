# Private deployment on top of the engine

The public engine ships **example** doctrine, agents, directives and neutral capability
scores. A real deployment keeps its own in a private repo.

## Layout of a private config repo

```
config/            routing.yaml governance.yaml usage.yaml endpoints.yaml capabilities.yaml
                   doctrine.yaml identity.grok doctrine.grok directive.grok agents/*.grok
identities/        <agent>.yaml (+ README.md)
doctrine/          doctrine.yaml
migrations/        *.grok (owner-signed) and unblocks/*.grok
artifacts/         inbox/ backlog/ plans/ evidence/ logs/ (runtime)
```

Run the engine against it: `gae --root <private repo> run --once` (or `GAE_ROOT=<dir>`).

## What is still pinned in code

* `core/doctrine.py` `DOCTRINE_STATEMENT`
* `core/identity.py` (agent names, ids, types, authority levels)
* the `COGNITION_REQUIREMENTS` / `OVERSIGHT_REQUIREMENTS` / policy constants in
  `core/scopes.py` and `core/acquisition.py`

These are pinned on purpose: config files can't change them, so a tampered config can't
loosen the rules. A private deployment provides its own copies of these modules, either as a
thin fork or by vendoring the engine and replacing those three files. The tests and binding
then verify that config and code agree.

## Install pinned to a tag

```bash
pip install "governed-agent-engine @ git+https://github.com/jdkabattles/governed-agent-engine@v0.1.0"
```
