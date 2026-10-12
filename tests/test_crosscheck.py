from __future__ import annotations

import pytest

from governed_agent_engine.bench.capabilities import Capabilities, DimScore, EndpointCapability
from governed_agent_engine.bench.graders import validate_plan_json
from governed_agent_engine.bench.sandbox import SandboxResult
from governed_agent_engine.cognition import crosscheck as cc
from governed_agent_engine.cognition.base import ChatMessage, Completion
from governed_agent_engine.cognition.health import HealthTracker
from governed_agent_engine.cognition.judge0 import CodeRun
from governed_agent_engine.cognition.registry import build_limiters
from governed_agent_engine.cognition.strategy import CognitionStrategy, select_endpoints
from governed_agent_engine.config.loader import AppConfig
from governed_agent_engine.core.errors import AllEndpointsFailedError

from .conftest import Mono, make_task

MSGS = [ChatMessage("user", "x")]
CHAIN = ("pollinations", "llm7", "ovh")
GOOD = '{"summary": "s", "steps": [{"index": 0, "description": "d"}]}'


class Fake:
    def __init__(self, name: str, text: str) -> None:
        self.name, self.model, self.text, self.calls = name, "m", text, 0

    def complete(self, messages: list[ChatMessage]) -> Completion:
        self.calls += 1
        return Completion(self.name, self.model, self.text)


def strat(
    config: AppConfig, texts: dict[str, str], caps: Capabilities | None = None
) -> CognitionStrategy:
    m = Mono()
    clients = {n: Fake(n, t) for n, t in texts.items()}
    return CognitionStrategy(clients, config.endpoints, build_limiters(config.endpoints, m),
                             HealthTracker(clock=m), config.governance, caps)  # fmt: skip


def caps(scores: dict[str, float], dim: str = "coding") -> Capabilities:
    return Capabilities("v", "t", {
        n: EndpointCapability(n, "m", {dim: DimScore(s, 8)}, 0.0, 1, 2) for n, s in scores.items()
    })  # fmt: skip


def test_capabilities_reorder_chain(config: AppConfig) -> None:
    c = caps({"ovh": 1.0, "llm7": 0.2})
    picked, _ = select_endpoints(make_task(type="repo_maintain"), CHAIN, config.endpoints,
                                 config.governance, HealthTracker(clock=Mono()), c)  # fmt: skip
    assert picked == ("ovh", "llm7", "pollinations")  # measured first, unmeasured after
    picked, _ = select_endpoints(make_task(), CHAIN, config.endpoints, config.governance,
                                 HealthTracker(clock=Mono()), c, exclude=["ovh"])  # fmt: skip
    assert picked == ("llm7", "pollinations")


def test_shared_rate_group_limiter(config: AppConfig) -> None:
    lim = build_limiters(config.endpoints, Mono())
    assert lim["openrouter"] is lim["openrouter_gemma4_31b"]
    assert lim["llm7"] is not lim["ovh"]


def test_repair_json() -> None:
    assert cc.repair_json('Sure!\n```json\n{"a": [1,2,],}\n```') == {"a": [1, 2]}
    assert cc.repair_json('x {"a": 1} y') == {"a": 1}
    with pytest.raises(ValueError):
        cc.repair_json("nothing")


def test_complete_json_retries_on_other_model(config: AppConfig) -> None:
    def v(o: object) -> str | None:
        return validate_plan_json(o, 1)

    s = strat(config, {"pollinations": "not json", "llm7": '{"summary": "s", "steps": []}',
                       "ovh": f"```json\n{GOOD}\n```"})  # fmt: skip
    _obj, ep, log = cc.complete_json(s, make_task(), CHAIN, MSGS, v)
    assert ep == "ovh"
    assert [x["outcome"] for x in log.steps] == ["invalid_json", "schema_violation",
                                                 "valid_repaired"]  # fmt: skip
    s2 = strat(config, {"pollinations": GOOD})
    assert (
        cc.complete_json(s2, make_task(), ("pollinations",), MSGS, v)[2].steps[0]["outcome"]
        == "valid_raw"
    )
    s3 = strat(config, {"pollinations": "bad"})
    with pytest.raises(AllEndpointsFailedError):
        cc.complete_json(s3, make_task(), ("pollinations",), MSGS, v)


def test_generate_verified_code_falls_back(config: AppConfig) -> None:
    c = caps({"llm7": 0.9, "pollinations": 0.8, "ovh": 0.1})
    s = strat(config, {"llm7": "```python\ndef f():\n    return 1\n```",
                       "pollinations": "```python\ndef f():\n    return 2\n```",
                       "ovh": "no code"}, c)  # fmt: skip
    code, ep, log = cc.generate_verified_code(s, make_task(type="repo_maintain"), CHAIN, MSGS,
                                              "assert f()==2")  # fmt: skip
    assert ep == "pollinations" and "return 2" in code  # noqa: PT018
    assert [x["outcome"] for x in log.steps] == ["failed", "passed"]
    with pytest.raises(AllEndpointsFailedError):
        cc.generate_verified_code(s, make_task(), CHAIN, MSGS, "assert f()==3")


def test_judge0_verifier() -> None:
    class J:
        def __init__(self, run: CodeRun) -> None:
            self.r = run

        def run(self, source: str) -> CodeRun:
            return self.r

    ok = cc.judge0_verifier(J(CodeRun(True, "Accepted", "ALL_TESTS_PASSED\n")))  # type: ignore[arg-type]
    assert ok("c", "t") == SandboxResult(True, "passed", "Accepted")
    bad = cc.judge0_verifier(J(CodeRun(False, "Runtime Error", "", "Trace")))  # type: ignore[arg-type]
    assert not bad("c", "t").passed


def test_honesty_guard(config: AppConfig) -> None:
    s = strat(config, {"pollinations": "x", "llm7": "VERDICT: UNSUPPORTED\nNot real."})
    g = cc.honesty_guard(s, make_task(), CHAIN[:2], "q?", "It takes 3 params.", "pollinations")
    assert g.verdict == "UNSUPPORTED" and g.checker == "llm7" and g.flagged  # noqa: PT018
    assert cc.honesty_guard(s, make_task(), CHAIN, "q", "No such API exists.", "x").verdict == (
        "ABSTAINED"
    )
    s2 = strat(config, {"llm7": "hmm"})
    g2 = cc.honesty_guard(s2, make_task(), ("llm7",), "q", "Answer.", "pollinations")
    assert g2.verdict == "UNSURE"
    s3 = strat(config, {"llm7": "VERDICT: supported"})
    assert not cc.honesty_guard(s3, make_task(), ("llm7",), "q", "A.", "p").flagged


def test_majority_vote(config: AppConfig) -> None:
    s = strat(config, {"pollinations": "ANSWER: 56", "llm7": "ANSWER: 5,6", "ovh": "ANSWER: 55"})
    v = cc.majority_vote(s, make_task(), CHAIN, MSGS)
    assert v.answer == "56" and v.agreement == pytest.approx(0.667, abs=1e-3)  # noqa: PT018
    s2 = strat(config, {"pollinations": "ANSWER: 1", "llm7": "ANSWER: 2", "ovh": "none"})
    assert cc.majority_vote(s2, make_task(), CHAIN, MSGS).answer is None
    s3 = strat(config, {"pollinations": "none"})
    assert cc.majority_vote(s3, make_task(), ("pollinations",), MSGS).votes == {}
    assert cc.extract_answer("ANSWER: 4.\nANSWER: 7.") == "7"
