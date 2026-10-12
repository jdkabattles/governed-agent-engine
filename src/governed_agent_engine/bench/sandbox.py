"""Run untrusted generated code against hidden tests in a constrained subprocess.

Isolation (best effort, stdlib only, works on Linux and Termux): isolated interpreter (-I),
empty environment, temp working dir, wall-clock timeout, and on POSIX rlimits for CPU time,
address space and file size, plus a blocklist of obviously dangerous modules checked before
execution. This is NOT a security boundary against a determined attacker; for that use
Judge0 (``Judge0Client``), which runs code in an isolated, network-less container.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN = re.compile(
    r"\b(import\s+(os|subprocess|socket|shutil|ctypes|multiprocessing|urllib|http|requests)"
    r"|from\s+(os|subprocess|socket|shutil|ctypes|multiprocessing|urllib|http|requests)\b"
    r"|__import__|eval\(|exec\(|open\()"
)
_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SandboxResult:
    passed: bool
    status: str  # passed | failed | timeout | forbidden | no_code
    detail: str = ""


def extract_code(text: str) -> str:
    blocks = _FENCE.findall(text)
    if blocks:
        return str(max(blocks, key=len)).strip()
    return text.strip() if "def " in text else ""


# Applied inside the child before untrusted code runs (preexec_fn is not thread-safe).
_PRELUDE = """\
try:
    import resource as _r
    _r.setrlimit(_r.RLIMIT_CPU, (5, 5))
    _r.setrlimit(_r.RLIMIT_AS, (512 * 2**20, 512 * 2**20))
    _r.setrlimit(_r.RLIMIT_FSIZE, (2**20, 2**20))
    del _r
except Exception:
    pass
"""


def run_tests(code: str, tests: str, timeout_s: float = 10.0) -> SandboxResult:
    if not code:
        return SandboxResult(False, "no_code")
    if FORBIDDEN.search(code):
        return SandboxResult(False, "forbidden", FORBIDDEN.search(code).group(0))  # type: ignore[union-attr]
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "t.py"
        script.write_text(
            _PRELUDE + code + "\n\n" + tests + "\nprint('ALL_TESTS_PASSED')\n", encoding="utf-8"
        )
        try:
            proc = subprocess.run(  # noqa: S603
                [sys.executable, "-I", str(script)],
                cwd=tmp,
                env={},
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(False, "timeout")
    if proc.returncode == 0 and "ALL_TESTS_PASSED" in proc.stdout:
        return SandboxResult(True, "passed")
    return SandboxResult(False, "failed", (proc.stderr or proc.stdout)[-300:])
