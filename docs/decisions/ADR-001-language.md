# ADR-001: Implementation language and dependency policy

* Status: accepted (2026-10-10)
* Hard requirements: $0; one-command download + install + run on Termux; no compile failures
  on the phone (no Rust-built dependencies); same quality bar as example-executor-backend
  (ruff, mypy --strict, ≥85% coverage, CI on 3.11–3.13).

## Options considered

| Option | Termux install | Dev velocity / ecosystem | Risk |
|---|---|---|---|
| **A. Python, Pydantic v2 + httpx** (what example-executor-backend uses) | `pip install pydantic` **fails**: pydantic-core is Rust/maturin; PyPI has no Android wheels; Termux main repo has no `python-pydantic` package | best | **Rejected**: verified failure (see evidence below) |
| **B. Python, stdlib-leaning core** (dataclasses + small validator, urllib, PyYAML) | `pkg install python git` + `pip install -e .`; PyYAML installs from sdist with its pure-Python fallback, no compiler needed | very good; reuses example-executor-backend patterns and tests | low |
| C. Go single static binary (GOOS=android/linux arm64) | download one file; zero runtime deps | good, but rewrite of every proven module; cross-language schemas | medium (release pipeline, binary signing, no reuse) |
| D. Mixed (Go CLI + Python cognition) | two toolchains on the phone or two artifacts | worst | high |

## Evidence gathered (2026-10-10)

1. **Termux package index** (`packages.termux.dev/apt/termux-main`, aarch64): `python` is
   3.14.6; packages named `python-pydantic*` or `python-yaml` do **not** exist; `rust` and
   `maturin` do exist (so pydantic is possible only by compiling Rust on-device).
2. **PyPI** `pydantic-core` 2.50.0 ships manylinux/musllinux aarch64 wheels but **no Android
   wheels**; pip on Termux (`sys.platform == "android"` on 3.14) cannot use them.
3. **Emulated Termux** (official `termux/termux-docker:aarch64` rootfs run under
   `proot -q qemu-aarch64` on the build box): `pip install pydantic` →
   `Failed to build 'pydantic-core' ... maturin` (log:
   an emulated Termux aarch64 log, not included here). In the same
   environment `pip install pyyaml` → 6.0.3, `__with_libyaml__ = False` (pure-Python, OK).
4. Nous Research's Overseer Agent solves the same problem by building Android wheels in CI and
   shipping a signed APT package (`pkg install overseer-agent`, wheels tagged
   `android_24_arm64_v8a`), i.e. they also avoid compiling native deps on the device. That
   infrastructure is out of scope for a $0 single-maintainer repo; avoiding native deps
   altogether gets the same reliability.

## Decision

**Option B.** Python ≥ 3.11, stdlib core:

* models: `dataclasses(frozen=True, slots=True)` + explicit `from_dict` validation
  (`core/validate.py`), strict unknown-field rejection, tz-aware datetimes;
* HTTP: `urllib.request` behind an `HttpClient` protocol (tests inject fakes; one test uses a
  loopback `http.server`);
* config: YAML via **PyYAML (the only runtime dependency)**;
* JSON Schemas hand-maintained in `schemas/` and checked against model output in tests
  (`jsonschema` is a dev-only dependency).

Revisit (→ Option C) if: on-device startup time or memory becomes a problem, or Termux drops
Python, or we need a no-Python distribution.

## Consequences

* + Install on Termux is `pkg install python git` + a pure-Python `pip install`; verified in
  emulation.
* + Most example-executor-backend logic (router, usage state, backlog, governance, rate limits,
  adapters) ported with little change.
* − No Pydantic conveniences; validation code is hand-written (and 100% covered by tests).
* − No async HTTP; the agent is a sequential loop, which matches its design anyway.
