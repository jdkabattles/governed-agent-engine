# Agent instructions (for Grok Bot, Cursor, Overseer or any coding agent)

* $0 only. Never add a paid service, paid model or card-required trial. Config enforces `cost_usd: 0.0`.
* Runtime deps: stdlib + PyYAML only. Do not add packages with native/Rust extensions (Termux
  must install without a compiler). See `docs/decisions/ADR-001-language.md`.
* Never commit secrets. Keys only via env vars (`OPENROUTER_API_KEY`, `GEMINI_API_KEY`).
* Never edit `config/governance.yaml` autonomously (`modify_governance` is always refused).
* Grok Bot routine contract: `docs/architecture.md` → "Grok Bot routine contract".
* Before pushing: `ruff check . && ruff format --check . && mypy && pytest`.
* Unit tests never touch the internet; use `tests/conftest.py::FakeHttp` and injected clocks.
