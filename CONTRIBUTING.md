# Contributing

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

Rules (also in `AGENTS.md`):

* **$0 only.** No paid services; endpoint configs must have `cost_usd: 0.0`.
* **Runtime deps:** stdlib + PyYAML. No native/Rust extensions (Termux must install without a
  compiler). Dev deps may be anything that runs in CI.
* **No network in unit tests.** Use `FakeHttp` from `tests/conftest.py`; inject clocks.
* **Adding an endpoint:** verify it is genuinely free; add it to `config/endpoints.yaml`
  (OpenAI-compatible endpoints need no code), add it to `knowledge.allowed_endpoints` in
  governance (human-reviewed PR), add tests, run `gae smoke --endpoints <name>`.
* **Changing models:** update `schemas/*.schema.json`; `tests/test_models.py` validates model
  output against them.
* **Governance changes** are human-only PRs (see `docs/governance.md`).
