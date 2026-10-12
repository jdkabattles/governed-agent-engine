from __future__ import annotations

import json
from pathlib import Path

import pytest

from governed_agent_engine import cli
from governed_agent_engine.bench import graders, items, plan, sandbox
from governed_agent_engine.bench.capabilities import (
    Capabilities,
    DimScore,
    EndpointCapability,
    build_capabilities,
    load_capabilities,
    write_capabilities,
)
from governed_agent_engine.bench.cli_bench import bench
from governed_agent_engine.bench.runner import (
    CallRecord,
    RunResult,
    Target,
    percentile,
    run_target,
    summarize,
)
from governed_agent_engine.cognition.base import ChatMessage, Completion
from governed_agent_engine.core.errors import (
    AuthError,
    ConfigError,
    ProviderError,
    RateLimitedError,
)

from .conftest import FakeHttp, oai

CODE = {
    "code.roman": "def roman_to_int(s):\n"
    "    v={'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}\n"
    "    t=0\n    for i,c in enumerate(s):\n        x=v[c]\n"
    "        t+= -x if i+1<len(s) and v[s[i+1]]>x else x\n    return t\n",
    "code.merge_intervals": "def merge_intervals(iv):\n    out=[]\n"
    "    for a,b in sorted(iv):\n        if out and a<=out[-1][1]:\n"
    "            out[-1][1]=max(out[-1][1],b)\n        else:\n            out.append([a,b])\n"
    "    return out\n",
    "code.rpn": "def rpn_eval(t):\n    st=[]\n    for x in t:\n"
    "        if x in '+-*/' and len(x)==1:\n"
    "            b=st.pop();a=st.pop()\n"
    "            st.append(a+b if x=='+' else a-b if x=='-' else a*b if x=='*' else int(a/b))\n"
    "        else:\n            st.append(int(x))\n    return st[0]\n",
    "code.topk": "import re\nfrom collections import Counter\ndef top_k_words(text,k):\n"
    "    c=Counter(w.lower() for w in re.findall('[A-Za-z]+',text))\n"
    "    return sorted(c.items(),key=lambda p:(-p[1],p[0]))[:k]\n",
    "code.balanced": "def is_balanced(s):\n    m={')':'(',']':'[','}':'{'}\n    st=[]\n"
    "    for c in s:\n        if c in '([{':\n            st.append(c)\n        elif c in m:\n"
    "            if not st or st.pop()!=m[c]:\n                return False\n    return not st\n",
    "code.run_length": "import re\ndef rle_decode(s):\n"
    "    return ''.join(c*int(n or 1) for n,c in re.findall('([0-9]*)([a-zA-Z])',s))\n",
}


def reference(item: items.Item) -> str:
    d = item.dimension
    if d == "coding":
        return f"```python\n{CODE[item.item_id]}```"
    if d == "reasoning":
        return f"Work.\nANSWER: {item.expected}"
    if d == "structured":
        n = int(str(item.params["steps"]))
        steps = [{"index": i, "description": f"s{i}"} for i in range(n)]
        return json.dumps({"summary": "x", "steps": steps})
    if d == "honesty":
        return (
            "Einstein; it removes a prefix." if item.params["exists"] else "No such thing exists."
        )
    if d == "long_context":
        return item.expected
    kind = item.params.get("kind")
    return {
        "bullets": "- a\n- b\n- c",
        "lowercase": "a pale rock in the sky.",
        "word_count": "The sea is wide and very blue.",
        "fruits": "apple,banana,cherry,date",
    }.get(str(kind), item.expected)


@pytest.mark.parametrize("item", items.ALL_ITEMS, ids=lambda i: i.item_id)
def test_reference_answers_score_full_and_junk_scores_zero(item: items.Item) -> None:
    assert graders.grade(item, reference(item))[0] == 1.0
    junk = "I think it's banana." if item.dimension != "honesty" else "It returns 42 widgets."
    if item.dimension == "honesty" and item.params["exists"]:
        junk = "There is no such thing."
    assert graders.grade(item, junk)[0] == 0.0


def test_suite_shape_and_determinism() -> None:
    dims = {i.dimension for i in items.ALL_ITEMS}
    assert dims == set(items.DIMENSIONS)
    assert len({i.item_id for i in items.ALL_ITEMS}) == len(items.ALL_ITEMS)
    for d in items.DIMENSIONS:
        assert 3 <= sum(i.dimension == d for i in items.ALL_ITEMS) <= 10
    assert items.haystack(5000, "N.", 0.5) == items.haystack(5000, "N.", 0.5)
    assert "N." in items.haystack(5000, "N.", 0.5)
    assert len(items.by_ids(items.SUBSET_IDS)) == len(items.SUBSET_IDS)


def test_grader_edges() -> None:
    js = items.STRUCTURED[0]
    ok = json.loads(reference(js))
    assert graders.grade_structured(js, f"```json\n{json.dumps(ok)}\n```") == (0.5, "fenced")
    assert graders.grade_structured(js, "nope")[1] == "invalid_json"
    bad = [
        [], {"summary": "x"}, {"summary": "", "steps": []}, {"summary": "x", "steps": {}},
        {"summary": "x", "steps": [{"index": 0}]},
        {"summary": "x", "steps": [{"index": 1, "description": "a"}]},
        {"summary": "x", "steps": [{"index": 0, "description": " "}]},
    ]  # fmt: skip
    for b in bad:
        assert graders.validate_plan_json(b, None) is not None
    assert graders.validate_plan_json(ok, 9) == "step_count:3"
    m = items.REASONING[0]
    assert graders.grade_reasoning(m, "")[1] == "empty"
    assert graders.grade_reasoning(m, "ANSWER: 1")[1] == "wrong:1"
    assert graders.grade_reasoning(m, "403")[1] == "no_answer_line"
    weird = items.Item("x", "instruction", "p", params={"kind": "??"})
    assert graders.grade_instruction(weird, "a")[1] == "unknown_kind"
    real = items.HONESTY[-1]
    assert graders.grade_honesty(real, "Newton")[1] == "wrong"


def test_sandbox() -> None:
    assert sandbox.extract_code("def f(): pass") == "def f(): pass"
    assert sandbox.extract_code("prose only") == ""
    assert sandbox.run_tests("", "").status == "no_code"
    assert sandbox.run_tests("import os\n", "").status == "forbidden"
    assert sandbox.run_tests("def f():\n    return 1\n", "assert f()==2").status == "failed"
    assert sandbox.run_tests("def f():\n    while True: pass\n", "f()", 2).status in (
        "timeout",
        "failed",
    )


class Scripted:
    def __init__(self, outs: list[str | Exception]) -> None:
        self.name, self.model, self.outs = "s", "m", outs

    def complete(self, messages: list[ChatMessage]) -> Completion:
        o = self.outs.pop(0) if len(self.outs) > 1 else self.outs[0]
        if isinstance(o, Exception):
            raise o
        return Completion("s", "served", o)


def _run(
    outs: list[str | Exception], its: tuple[items.Item, ...], tmp: Path, **kw: object
) -> RunResult:
    r = RunResult()
    t = Target("s", Scripted(outs), its, 1.0)
    run_target(t, r, tmp, clock=lambda: 0.0, sleep=lambda s: None, **kw)  # type: ignore[arg-type]
    return r


def test_runner_outcomes_retry_quota_resume(tmp_path: Path) -> None:
    its = items.by_ids(("math.0", "math.1", "math.2", "math.3", "math.4"))
    rl = RateLimitedError("s", "429")
    r = _run([ProviderError("s", "HTTP 503 busy"), "ANSWER: 403", rl, rl, rl, rl, rl, rl],
             its, tmp_path)  # fmt: skip
    outs = [x.outcome for x in r.records]
    assert outs[0] == "ok"  # 503 retried then succeeded
    assert outs[1] == "rate_limited" and outs[2] == "rate_limited"  # noqa: PT018
    assert outs[3:] == ["skipped_quota", "skipped_quota"]
    raw = (tmp_path / "raw" / "s.jsonl").read_text().splitlines()
    assert len(raw) == 5
    r2 = _run(["ANSWER: 15"], its, tmp_path, resume=True)
    assert next(x.item_id for x in r2.records) == "math.0"  # restored, not re-asked
    assert sum(x.ok for x in r2.records) >= 2
    for exc, want in [
        (AuthError("s", "no"), "auth_error"),
        (ProviderError("s", "read timed out"), "timeout"),
        (ProviderError("s", "boom"), "error"),
    ]:
        rr = _run([exc], its[:1], tmp_path / want)
        assert rr.records[0].outcome == want


def test_summarize_and_percentile() -> None:
    assert percentile([], 0.5) == 0.0
    assert percentile([1, 2, 3, 4], 0.5) == 2.5

    def rec(i: str, d: str, ok: bool, score: float, outcome: str = "ok") -> CallRecord:
        return CallRecord("e", "m", "served" if ok else "", i, d, ok, outcome, 100.0, score,
                          "", "", "t")  # fmt: skip

    s = summarize([
        rec("a", "coding", True, 1.0), rec("b", "coding", False, 0.0, "timeout"),
        rec("c", "latency", True, 1.0), rec("d", "honesty", False, 0, "skipped_quota"),
    ])  # fmt: skip
    e = s["e"]
    assert e["dimensions"] == {"coding": {"score": 1.0, "n": 1}}  # failures -> error_rate
    assert e["calls"] == 3 and e["skipped_quota"] == 1  # noqa: PT018
    assert e["timeout_rate"] == pytest.approx(0.333, abs=1e-3)


def _caps() -> Capabilities:
    return Capabilities(
        "v", "t",
        {
            "a": EndpointCapability("a", "m", {"coding": DimScore(1.0, 6)}, 0.0, 1, 2),
            "b": EndpointCapability("b", "m", {"coding": DimScore(0.5, 6)}, 0.0, 1, 2),
            "c": EndpointCapability("c", "m", {"coding": DimScore(1.0, 1)}, 0.5, 1, 2),
        },
    )  # fmt: skip


def test_capabilities_roundtrip_and_ranking(tmp_path: Path) -> None:
    caps = _caps()
    assert caps.best_for("coding") == ["a", "c", "b"]  # c shrunk by small n
    assert caps.best_for("honesty", ["b", "a"]) == ["a", "b"]
    assert caps.utility("zzz", "custom") is None
    assert caps.utility("a", "repo_maintain") > caps.utility("b", "repo_maintain")  # type: ignore[operator]
    summary = {"a": {"model_requested": "m", "models_served": ["m"], "error_rate": 0.1,
                     "p50_ms": 5, "p95_ms": 9,
                     "dimensions": {"coding": {"score": 1.0, "n": 2}}}}  # fmt: skip
    p = tmp_path / "capabilities.yaml"
    write_capabilities(build_capabilities(summary, "v1", "src"), p)
    loaded = load_capabilities(p)
    assert loaded is not None
    assert loaded.endpoints["a"].dims["coding"] == DimScore(1.0, 2)
    assert load_capabilities(tmp_path / "missing.yaml") is None
    p.write_text("endpoints: {a: {scores: {coding: {score: 7, n: 1}}}}\n")
    with pytest.raises(ConfigError):
        load_capabilities(p)
    p.write_text("endpoints: {}\ntask_type_weights: {research: {honesty: 1}}\n")
    assert "custom" in load_capabilities(p).weights  # type: ignore[union-attr]


def test_plan_budget(root: Path) -> None:
    r = bench(root, dry_run=True)
    assert r["calls"]["openrouter"] <= 40
    assert len(r["plan"]["openrouter"]) == len(items.MINI_IDS)
    assert bench(root, dry_run=True, only_items=["math.0"])["calls"]["ovh"] == 1
    assert all("needle.30k" not in i for i in r["plan"]["ai_horde"])
    from governed_agent_engine.config.loader import load_config

    eps = load_config(root / "config").endpoints
    assert len(plan.items_for("llm7", eps["llm7"], "mini")) == len(items.MINI_IDS)
    assert len(plan.items_for("llm7", eps["llm7"], "subset")) == len(items.SUBSET_IDS)
    with pytest.raises(ValueError, match="budget"):
        bench(root, dry_run=True, max_per_group={"openrouter": 1})


def test_bench_end_to_end_mocked(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = FakeHttp()
    h.add("POST", "https://", oai("ANSWER: 5", model="codestral"))
    res = bench(root, endpoints=["llm7"], mode="mini", http=h, write_caps=True,
                sleep=lambda s: None)  # fmt: skip
    s = res["summary"]["llm7"]
    assert s["dimensions"]["reasoning"] == {"score": 1.0, "n": 1}
    assert s["models_served"] == ["codestral"]
    caps = load_capabilities(root / "config" / "capabilities.yaml")
    assert caps is not None and "llm7" in caps.endpoints  # noqa: PT018
    assert json.loads((Path(res["out"]) / "scores.json").read_text())["meta"]["seed"] == items.SEED
    rc = cli.main(["--root", str(root), "bench", "--dry-run"])
    assert rc == 0
    assert "calls" in json.loads(capsys.readouterr().out)


def test_rebuild_from_raw(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = root / "artifacts" / "benchmarks" / "x"
    (out / "raw").mkdir(parents=True)

    def line(item: str, ok: bool, score: float, outcome: str) -> str:
        ans = {"math.0": "ANSWER: 403", "math.1": "ANSWER: 15"}.get(item, "")
        r = CallRecord("llm7", "m", "m" if ok else "", item, "reasoning", ok, outcome, 10.0,
                       score, "", ans, "t")  # fmt: skip
        return json.dumps(r.to_dict())

    (out / "raw" / "llm7.jsonl").write_text("\n".join([
        line("math.0", False, 0, "timeout"), line("math.0", True, 1, "ok"),
        line("math.1", True, 1, "ok"), line("math.1", False, 0, "skipped_quota"),
        json.dumps({**json.loads(line("math.2", True, 1, "ok")), "suite_version": "old"}),
    ]) + "\n")  # fmt: skip
    rc = cli.main(["--root", str(root), "bench", "--rebuild-from", str(out),
                   "--write-capabilities"])  # fmt: skip
    assert rc == 0
    s = json.loads(capsys.readouterr().out)["summary"]["llm7"]
    assert s["dimensions"]["reasoning"] == {"score": 1.0, "n": 2}
    assert s["error_rate"] == pytest.approx(0.333, abs=1e-3)
    assert load_capabilities(root / "config" / "capabilities.yaml") is not None
    md = (out / "summary.md").read_text()
    assert "| llm7 | m | - | 1.00 (n=2) |" in md
