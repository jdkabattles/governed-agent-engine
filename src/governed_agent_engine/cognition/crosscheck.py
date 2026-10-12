"""Cross-checks that turn single-model weaknesses into system strengths.

All functions are planning/verification helpers: they call free cognition and local or Judge0
verification only, never execute side-effecting actions.

(a) ``complete_json``      validate -> local repair -> retry on a *different* model
(b) ``generate_verified_code``  best coder first, verify with hidden tests, fall back on failure
(c) ``honesty_guard``      a second model audits an answer for unsupported/nonexistent claims
    ``majority_vote``      k independent models answer exact-answer tasks; majority wins
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from governed_agent_engine.bench.graders import abstains
from governed_agent_engine.bench.sandbox import SandboxResult, extract_code, run_tests
from governed_agent_engine.cognition.base import ChatMessage
from governed_agent_engine.cognition.judge0 import Judge0Client
from governed_agent_engine.cognition.strategy import CognitionStrategy
from governed_agent_engine.core.errors import AllEndpointsFailedError
from governed_agent_engine.core.models import Task

Validator = Callable[[object], str | None]  # None = valid, else reason
Verifier = Callable[[str, str], SandboxResult]  # (code, tests) -> result


@dataclass
class CrossCheckLog:
    steps: list[dict[str, str]] = field(default_factory=list)

    def add(self, endpoint: str, outcome: str, detail: str = "") -> None:
        self.steps.append({"endpoint": endpoint, "outcome": outcome, "detail": detail[:200]})


# ---------------------------------------------------------------- (a) JSON
def repair_json(text: str) -> object:
    """Best-effort local repair: strip fences/prose, take the outermost {...} or [...],
    drop trailing commas. Raises ValueError if still not JSON."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(t.rfind("}"), t.rfind("]"))
        if end > start:
            t = t[start : end + 1]
    t = re.sub(r",\s*([}\]])", r"\1", t)
    return json.loads(t)


def complete_json(
    strategy: CognitionStrategy,
    task: Task,
    chain: Sequence[str],
    messages: list[ChatMessage],
    validator: Validator,
    max_models: int = 3,
) -> tuple[Any, str, CrossCheckLog]:
    """Return (object, endpoint, log). Each failing model is excluded from the next try and
    the next model is told what was wrong."""
    log = CrossCheckLog()
    tried: list[str] = []
    msgs = list(messages)
    for _ in range(max_models):
        try:
            res = strategy.complete(task, chain, msgs, exclude=tried)
        except AllEndpointsFailedError:
            break
        ep = res.completion.endpoint
        tried.append(ep)
        try:
            obj = json.loads(res.completion.text)
            how = "valid_raw"
        except ValueError:
            try:
                obj = repair_json(res.completion.text)
                how = "valid_repaired"
            except ValueError as exc:
                log.add(ep, "invalid_json", str(exc))
                msgs = [*messages, ChatMessage("user", "Previous answer was not valid JSON. "
                                               "Output ONLY the JSON object.")]  # fmt: skip
                continue
        err = validator(obj)
        if err is None:
            log.add(ep, how)
            return obj, ep, log
        log.add(ep, "schema_violation", err)
        msgs = [*messages, ChatMessage("user", f"Previous answer violated the schema ({err}). "
                                       "Output ONLY a corrected JSON object.")]  # fmt: skip
    raise AllEndpointsFailedError(())


# ---------------------------------------------------------------- (b) code
def judge0_verifier(client: Judge0Client) -> Verifier:
    def verify(code: str, tests: str) -> SandboxResult:
        run = client.run(code + "\n\n" + tests + "\nprint('ALL_TESTS_PASSED')\n")
        ok = run.ok and "ALL_TESTS_PASSED" in run.stdout
        return SandboxResult(ok, "passed" if ok else "failed", run.status + run.stderr[-200:])

    return verify


def generate_verified_code(
    strategy: CognitionStrategy,
    task: Task,
    chain: Sequence[str],
    messages: list[ChatMessage],
    tests: str,
    verifier: Verifier = run_tests,
    max_models: int = 3,
) -> tuple[str, str, CrossCheckLog]:
    """Generate with the best measured coder (repo_maintain weights favour coding), verify,
    and fall back to the next model on failure. Returns (code, endpoint, log)."""
    log = CrossCheckLog()
    tried: list[str] = []
    for _ in range(max_models):
        try:
            res = strategy.complete(task, chain, messages, exclude=tried)
        except AllEndpointsFailedError:
            break
        ep = res.completion.endpoint
        tried.append(ep)
        code = extract_code(res.completion.text)
        result = verifier(code, tests)
        log.add(ep, result.status, result.detail)
        if result.passed:
            return code, ep, log
    raise AllEndpointsFailedError(())


# ---------------------------------------------------------------- (c) honesty
GUARD_PROMPT = (
    "You are a strict fact-checker. Question:\n{q}\n\nAnswer given by another assistant:\n{a}\n\n"
    "Does the answer describe things (APIs, papers, people, standards) that do not actually "
    "exist, or state facts you cannot confirm? Reply on the first line with exactly one of: "
    "VERDICT: SUPPORTED, VERDICT: UNSUPPORTED, VERDICT: UNSURE. Then one short sentence."
)
_VERDICT = re.compile(r"VERDICT:\s*(SUPPORTED|UNSUPPORTED|UNSURE)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class GuardResult:
    verdict: str  # SUPPORTED | UNSUPPORTED | UNSURE | ABSTAINED
    checker: str
    note: str

    @property
    def flagged(self) -> bool:
        return self.verdict in ("UNSUPPORTED", "UNSURE")


def honesty_guard(
    strategy: CognitionStrategy,
    task: Task,
    chain: Sequence[str],
    question: str,
    answer: str,
    answered_by: str,
) -> GuardResult:
    """A *different* model audits the answer. An answer that already abstains needs no audit."""
    if abstains(answer):
        return GuardResult("ABSTAINED", "", "answer already signals uncertainty")
    res = strategy.complete(
        task, chain, [ChatMessage("user", GUARD_PROMPT.format(q=question, a=answer))],
        exclude=[answered_by],
    )  # fmt: skip
    m = _VERDICT.search(res.completion.text)
    verdict = m.group(1).upper() if m else "UNSURE"  # unparseable audit = treat as uncertain
    return GuardResult(verdict, res.completion.endpoint, res.completion.text[:300])


_ANSWER = re.compile(r"ANSWER:\s*\**\s*([^\s*]+)", re.IGNORECASE)


def extract_answer(text: str) -> str | None:
    found = _ANSWER.findall(text.replace(",", ""))
    return found[-1].strip(".") if found else None


@dataclass(frozen=True, slots=True)
class VoteResult:
    answer: str | None  # None when no strict majority
    agreement: float
    votes: dict[str, str]


def majority_vote(
    strategy: CognitionStrategy,
    task: Task,
    chain: Sequence[str],
    messages: list[ChatMessage],
    k: int = 3,
    extract: Callable[[str], str | None] = extract_answer,
) -> VoteResult:
    """Ask up to k distinct models (best-ranked first); strict majority of k wins."""
    votes: dict[str, str] = {}
    tried: list[str] = []
    while len(tried) < k:
        try:
            res = strategy.complete(task, chain, messages, exclude=tried)
        except AllEndpointsFailedError:
            break
        tried.append(res.completion.endpoint)
        a = extract(res.completion.text)
        if a is not None:
            votes[res.completion.endpoint] = a
    if not votes:
        return VoteResult(None, 0.0, votes)
    top, count = Counter(votes.values()).most_common(1)[0]
    winner = top if count > k / 2 else None
    return VoteResult(winner, round(count / k, 3), votes)
