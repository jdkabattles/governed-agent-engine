from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from governed_agent_engine import cli
from governed_agent_engine.cognition.health import HealthTracker
from governed_agent_engine.cognition.planner import build_messages, parse_plan
from governed_agent_engine.cognition.registry import build_clients, build_limiters
from governed_agent_engine.cognition.strategy import CognitionStrategy
from governed_agent_engine.config.loader import AppConfig
from governed_agent_engine.core import identity
from governed_agent_engine.core.doctrine import (
    DOCTRINE_STATEMENT,
    Doctrine,
    load_doctrine,
    parse_doctrine,
)
from governed_agent_engine.core.errors import (
    ConfigError,
    DoctrineError,
    GovernanceError,
    GovernanceTamperError,
    ValidationError,
)
from governed_agent_engine.core.grok_binding import audit as audit
from governed_agent_engine.core.models import (
    BacklogStatus,
    Executor,
    GovernanceConfig,
    GrokResult,
    Heartbeat,
    Plan,
    PlanStep,
    StepKind,
)
from governed_agent_engine.orchestrator import main as orch_main
from governed_agent_engine.orchestrator.doctrine_gate import (
    check_execution_evidence,
    check_plan,
)
from governed_agent_engine.orchestrator.governance import validate_task
from governed_agent_engine.orchestrator.main import Orchestrator

from .conftest import GOOD_PLAN, T0, FakeClock, FakeHttp, Mono, make_task, oai

G, A, E, V, IMP = (StepKind.GATHER, StepKind.ANALYZE, StepKind.EXECUTE, StepKind.VERIFY,
                 StepKind.IMPROVE)  # fmt: skip


def good_plan(**kw: object) -> Plan:
    p = parse_plan(make_task(), GOOD_PLAN, "e", "m", T0)
    return replace(p, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ identity
def test_identities() -> None:
    assert identity.HUMAN_OWNER.authority_level == 100
    assert identity.HUMAN_OWNER.is_human
    assert identity.EXECUTOR_GROK.authority_level == 10
    c = identity.resolve("cognition:groq_gpt_oss_120b")
    assert (c.type, c.authority_level) == (identity.AgentType.COGNITION, 1)
    assert identity.resolve("grok") is identity.EXECUTOR_GROK
    assert identity.resolve("") is identity.UNKNOWN
    assert identity.resolve("mallory") is identity.UNKNOWN
    assert identity.resolve("cognition:") is identity.UNKNOWN
    with pytest.raises(ValidationError, match="range"):
        identity.AgentIdentity("x", identity.AgentType.HUMAN, 101)
    with pytest.raises(ValidationError, match="human authority"):
        identity.AgentIdentity("x", identity.AgentType.EXECUTOR, 60)


def test_top_level_requires_human(config: AppConfig) -> None:
    gov = config.governance
    validate_task(make_task(), gov)
    for who in ("grok", "cognition:llm7", "", "mallory"):
        with pytest.raises(GovernanceError, match="human issuer"):
            validate_task(make_task(issued_by=who), gov)
    with pytest.raises(GovernanceError, match="empty provenance"):
        validate_task(make_task(provenance=["x"]), gov)
    low = GovernanceConfig.from_dict({"process": {"min_top_level_authority": 50}})
    assert low.process.min_top_level_authority == 50
    with pytest.raises(ValueError, match="min_top_level_authority"):
        GovernanceConfig.from_dict({"process": {"min_top_level_authority": 10}})


def test_child_provenance(config: AppConfig) -> None:
    gov = config.governance
    root_t = make_task(task_id="r")
    child = make_task(task_id="c", depth=1, parent_task_id="r", provenance=["r"],
                      issued_by="cognition:ovh")  # fmt: skip
    tasks = {"r": root_t, "c": child}
    validate_task(child, gov, tasks.get)
    grand = make_task(task_id="g", depth=2, parent_task_id="c", provenance=["r", "c"],
                      issued_by="grok")  # fmt: skip
    validate_task(grand, gov, tasks.get)
    bad = [
        (replace(child, issued_by="mallory"), "unknown issuer"),
        (replace(child, provenance=("x",)), "every ancestor"),
        (replace(child, provenance=()), "every ancestor"),
        (replace(grand, task_id="r"), "cycle"),
    ]
    for t, msg in bad:
        with pytest.raises(GovernanceError, match=msg):
            validate_task(t, gov, tasks.get)
    with pytest.raises(GovernanceError, match="not found"):
        validate_task(replace(child, parent_task_id="zz", provenance=("zz",)), gov, tasks.get)
    with pytest.raises(GovernanceError, match="recorded chain"):
        validate_task(replace(grand, provenance=("q", "c")), gov, tasks.get)
    rogue = {"r": replace(root_t, issued_by="grok"), "c": child}
    with pytest.raises(GovernanceError, match="provenance root"):
        validate_task(grand, gov, rogue.get)


# ------------------------------------------------------------------ doctrine config
def test_doctrine_loader(root: Path, config: AppConfig) -> None:
    d = config.doctrine
    assert d.statement == DOCTRINE_STATEMENT
    assert (d.truth_priority, d.evidence_required, d.improvement_loop) == (1, True, True)
    assert d.fingerprint
    assert DOCTRINE_STATEMENT in d.system_prompt()
    with pytest.raises(ConfigError, match="verbatim"):
        parse_doctrine({"statement": "Be nice."})
    with pytest.raises(ValueError, match="laziness_penalty"):
        parse_doctrine({"statement": DOCTRINE_STATEMENT, "laziness_penalty": 2})
    with pytest.raises(ValueError, match="unknown"):
        parse_doctrine({"statement": DOCTRINE_STATEMENT, "lazy_ok": True})
    p = root / "config" / "doctrine.yaml"
    p.write_text("statement: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_doctrine(p)
    p.write_text("statement: nope\n")
    with pytest.raises(ConfigError, match="verbatim"):
        load_doctrine(p)
    with pytest.raises(ConfigError, match="missing"):
        load_doctrine(root / "nope.yaml")


def test_doctrine_in_system_prompt() -> None:
    sys_msg = build_messages(make_task(), Doctrine())[0]
    assert sys_msg.role == "system" and DOCTRINE_STATEMENT in sys_msg.content  # noqa: PT018


# ------------------------------------------------------------------ plan gate
def test_good_plan_passes() -> None:
    assert check_plan(good_plan(), Doctrine()) == []


@pytest.mark.parametrize(
    ("steps", "needle"),
    [
        ((), "min_steps"),
        (((A, "Look at the facts given", False, ()), (E, "Make the change now", True, ("x",)),
          (V, "Check the change carefully", True, ()), (IMP, "Apply lessons learned", False, ())),
         "no gather"),
        (((E, "Make the change now", True, ("x",)), (G, "Collect the facts first", False, ()),
          (V, "Check the change carefully", True, ()), (IMP, "Apply lessons learned", False, ())),
         "execute step before"),
        (((G, "Collect the facts first", False, ()), (E, "Make the change now", True, ("x",)),
          (IMP, "Apply lessons learned", False, ())), "no verify"),
        (((G, "Collect the facts first", False, ()), (E, "Make the change now", False, ("x",)),
          (V, "Check the change carefully", True, ()), (IMP, "Apply lessons learned", False, ())),
         "must set evidence_required"),
        (((G, "Collect the facts first", False, ()), (E, "Make the change now", True, ()),
          (V, "Check the change carefully", True, ()), (IMP, "Apply lessons learned", False, ())),
         "failure_modes"),
        (((G, "Collect the facts first", False, ()), (E, "Make the change now", True, ("x",)),
          (V, "Check, then improve and improve again", True, ())), "kind=improve"),
        (((G, "Collect the facts first", False, ()), (IMP, "Apply lessons learned", False, ()),
          (V, "Check the change carefully", True, ())), "must follow a verify"),
        (((G, "Collect the facts first", False, ()), (G, "short", False, ()),
          (G, "Collect the facts first", False, ())), "too short"),
        (((G, "Collect the facts first", False, ()), (G, "Collect the facts first", False, ()),
          (V, "Check the change carefully", True, ())), "duplicates"),
        (((None, "Collect the facts first", False, ()), (G, "Gather more relevant facts", False,
          ()), (V, "Check the change carefully", True, ())), "no kind"),
    ],
)  # fmt: skip
def test_plan_violations(steps: tuple[tuple[object, ...], ...], needle: str) -> None:
    ps = tuple(PlanStep(i, str(d), k, bool(ev), tuple(fm))  # type: ignore[arg-type]
               for i, (k, d, ev, fm) in enumerate(steps))  # fmt: skip
    v = check_plan(good_plan(steps=ps), Doctrine())
    assert any(needle in x for x in v), v


def test_substring_improve_is_not_enough() -> None:
    """The word 'improve' in descriptions must not satisfy the improvement loop."""
    p = good_plan()
    gamed = tuple(replace(s, kind=V if s.kind is IMP else s.kind,
                          description=s.description + " to improve") for s in p.steps)  # fmt: skip
    assert any("kind=improve" in x for x in check_plan(replace(p, steps=gamed), Doctrine()))


def test_plan_misc_rules() -> None:
    p = good_plan(summary=" ")
    assert "laziness: empty summary" in check_plan(p, Doctrine())
    bad_idx = good_plan(steps=tuple(replace(s, index=s.index + 1) for s in good_plan().steps))
    assert any("indices" in x for x in check_plan(bad_idx, Doctrine()))
    lax = Doctrine(evidence_required=False, failure_analysis_required=False,
                   improvement_loop=False, min_steps=1)  # fmt: skip
    one = good_plan(steps=(PlanStep(0, "Collect the facts first", G),))
    assert check_plan(one, lax) == []


def test_parse_plan_json_and_legacy() -> None:
    p = parse_plan(make_task(), "```json\n" + GOOD_PLAN + "\n```", "e", "m", T0)
    assert [s.kind for s in p.steps] == [G, E, V, IMP]
    assert p.steps[1].failure_modes == ("inputs incomplete",)
    odd = json.dumps({"summary": "s", "steps": [{"kind": "dance", "description": "x",
                                                 "failure_modes": "no"}]})  # fmt: skip
    q = parse_plan(make_task(), odd, "e", "m", T0)
    assert q.steps[0].kind is None and q.steps[0].failure_modes == ()  # noqa: PT018
    assert parse_plan(make_task(), '{"steps": [1]}', "e", "m", T0).steps == ()
    legacy = parse_plan(make_task(), "Sum\n1. a", "e", "m", T0)
    assert legacy.steps[0].kind is None


def test_execution_evidence(root: Path) -> None:
    p = good_plan()
    (root / "artifacts/evidence/s1.txt").write_text("proof")
    (root / "artifacts/evidence/empty.txt").write_text("")
    ok = {"evidence": {"1": "artifacts/evidence/s1.txt", "2": "artifacts/evidence/s1.txt"}}
    assert check_execution_evidence(p, ok, root, ("artifacts",)) == []
    v = check_execution_evidence(
        p, {"evidence": {"1": "../etc/passwd", "2": "artifacts/evidence/empty.txt"}},
        root, ("artifacts",),
    )  # fmt: skip
    assert any("outside" in x for x in v) and any("missing or empty" in x for x in v)  # noqa: PT018
    assert len(check_execution_evidence(p, {}, root, ("artifacts",))) == 2


# ------------------------------------------------------------------ orchestrator wiring
LAZY = json.dumps(
    {"summary": "do it", "steps": [{"index": 0, "kind": "execute", "description": "Just do it"}]}
)


def _orch(config: AppConfig, clock: FakeClock, h: FakeHttp) -> Orchestrator:
    en = config.enabled_endpoints()
    m = Mono()
    s = CognitionStrategy(build_clients(en, h, {}), en, build_limiters(en, m),
                          HealthTracker(clock=m), config.governance)  # fmt: skip
    return Orchestrator(config, s, clock=clock)


def test_rejected_plan_retries_next_model(root: Path, config: AppConfig, clock: FakeClock) -> None:
    h = FakeHttp()
    calls: list[str] = []

    def handler(method: str, url: str, headers: dict[str, str], body: object) -> object:
        calls.append(url)
        return oai(LAZY if len(calls) == 1 else GOOD_PLAN)

    h.handle("POST", "https://", handler)  # type: ignore[arg-type]
    o = _orch(config, clock, h)
    out = o.submit(make_task(type="research"))
    assert out.decision.executor is Executor.COGNITION
    assert len(calls) == 2
    rej = list((root / "artifacts/evidence/doctrine").glob("t1.0.*.rejected.json"))
    assert len(rej) == 1
    data = json.loads(rej[0].read_text())
    assert data["kind"] == "doctrine_rejection" and data["violations"]  # noqa: PT018
    assert o.strategy.penalties[data["endpoint"]] == 0.5
    assert o.metrics.counters["doctrine.rejected"] == 1


def test_all_models_lazy(root: Path, config: AppConfig, clock: FakeClock) -> None:
    h = FakeHttp()
    h.add("POST", "https://", oai(LAZY))
    o = _orch(config, clock, h)
    with pytest.raises(DoctrineError) as info:
        o.submit(make_task(type="research"))
    assert 2 <= len(info.value.rejections) <= 4  # max_models, limited by reachable endpoints
    assert o.backlog.counts() == {}
    inbox_task = make_task(task_id="x", type="research").to_dict()
    (root / "artifacts/inbox/x.json").write_text(json.dumps(inbox_task))
    rep = o.run_cycle()
    assert rep.intake_rejected == 1


def test_evidence_of_truth_on_replay(root: Path, config: AppConfig, clock: FakeClock) -> None:
    o = _orch(config, clock, FakeHttp())
    h2 = FakeHttp()
    h2.add("POST", "https://", oai())
    o = _orch(config, clock, h2)
    entry = o.submit(make_task(type="research")).entry
    (root / "artifacts/evidence/heartbeat.json").write_text(json.dumps(Heartbeat(T0).to_dict()))
    res = GrokResult(entry.entry_id, "t1", "done", T0, outputs={"evidence": {}})
    (root / f"artifacts/evidence/{entry.entry_id}.json").write_text(json.dumps(res.to_dict()))
    o.replay()
    assert o.backlog.get(entry.entry_id).status is BacklogStatus.FAILED
    chk = json.loads((root / f"artifacts/evidence/{entry.entry_id}.evidence_check.json")
                     .read_text())  # fmt: skip
    assert len(chk["violations"]) == 2
    # with evidence -> DONE
    (root / "artifacts/evidence/p.txt").write_text("proof")
    e2 = o.submit(make_task(task_id="t2", type="research")).entry
    good = {"evidence": {"1": "artifacts/evidence/p.txt", "2": "artifacts/evidence/p.txt"}}
    (root / f"artifacts/evidence/{e2.entry_id}.json").write_text(
        json.dumps(GrokResult(e2.entry_id, "t2", "done", T0, outputs=good).to_dict())
    )
    o.replay()
    assert o.backlog.get(e2.entry_id).status is BacklogStatus.DONE
    # plan file gone -> failed
    e3 = o.submit(make_task(task_id="t3", type="research")).entry
    (root / "artifacts" / str(e3.plan_path)).unlink()
    (root / f"artifacts/evidence/{e3.entry_id}.json").write_text(
        json.dumps(GrokResult(e3.entry_id, "t3", "done", T0, outputs=good).to_dict())
    )
    o.replay()
    assert o.backlog.get(e3.entry_id).status is BacklogStatus.FAILED


def test_child_task_via_orchestrator(root: Path, config: AppConfig, clock: FakeClock) -> None:
    h = FakeHttp()
    h.add("POST", "https://", oai())
    o = _orch(config, clock, h)
    o.submit(make_task(task_id="r", type="research"))
    o.submit(make_task(task_id="c", type="research", depth=1, parent_task_id="r",
                       provenance=["r"], issued_by="cognition:ovh"))  # fmt: skip
    with pytest.raises(GovernanceError, match="not found"):
        o.submit(make_task(task_id="d", depth=1, parent_task_id="nope", provenance=["nope"],
                           issued_by="grok"))  # fmt: skip


def test_doctrine_tamper(root: Path, config: AppConfig, clock: FakeClock) -> None:
    o = _orch(config, clock, FakeHttp())
    p = root / "config" / "doctrine.yaml"
    p.write_text(p.read_text() + "\n# edit\n")
    with pytest.raises(GovernanceTamperError, match="doctrine"):
        o.run_cycle()


def test_cli_issuer(root: Path, monkeypatch: pytest.MonkeyPatch,
                    capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    h = FakeHttp()
    h.add("POST", "https://", oai())
    real = orch_main.build
    monkeypatch.setattr(orch_main, "build", lambda r: real(r, http=h, env={}))
    t = make_task(type="research").to_dict()
    del t["issued_by"]
    assert cli.main(["--root", str(root), "submit", "--json", json.dumps({"task": t})]) == 0
    capsys.readouterr()
    t["task_id"] = "t9"
    assert cli.main(["--root", str(root), "submit", "--as", "grok", "--json",
                     json.dumps(t)]) == 2  # fmt: skip
    capsys.readouterr()
    h.routes.clear()
    h.add("POST", "https://", oai(LAZY))
    t["task_id"] = "t10"
    assert cli.main(["--root", str(root), "submit", "--json", json.dumps(t)]) == 5
    assert json.loads(capsys.readouterr().out)["error"] == "doctrine"


ACQ_INPUTS = {
    "agent": "acquirer", "target": "https://example.org", "output_schema": {"type": "object"},
    "evidence_plan": "raw pages", "tool": "in_repo_scraper",
    "failure_modes": dict.fromkeys(("rate_limits", "bans", "bad_data", "coverage"), "ok"),
}  # fmt: skip


def test_acquisition_run_artifacts_enforced(
    root: Path, config: AppConfig, clock: FakeClock
) -> None:
    h = FakeHttp()
    h.add("POST", "https://", oai())
    o = _orch(config, clock, h)
    ev = root / "artifacts/evidence"
    (ev / "p.txt").write_text("proof")
    for n in ("data.jsonl", "logs.txt", "acquisition_report.md"):
        (ev / n).write_text("x")
    (ev / "logs.empty").write_text("")
    full = ["artifacts/evidence/data.jsonl", "artifacts/evidence/logs.txt",
            "artifacts/evidence/acquisition_report.md"]  # fmt: skip
    cases = {"ok": full, "short": [*full[:1], "artifacts/evidence/logs.empty"], "none": None}
    entries = {}
    for name, arts in cases.items():
        e = o.submit(make_task(task_id=name, type="research", inputs=ACQ_INPUTS)).entry
        outputs: dict[str, object] = {
            "evidence": {"1": "artifacts/evidence/p.txt", "2": "artifacts/evidence/p.txt"}
        }
        if arts is not None:
            outputs["artifacts"] = arts
        res = GrokResult(e.entry_id, name, "done", T0, outputs=outputs)
        (ev / f"{e.entry_id}.json").write_text(json.dumps(res.to_dict()))
        entries[name] = e
    o.replay()
    assert o.backlog.get(entries["ok"].entry_id).status is BacklogStatus.DONE
    for bad in ("short", "none"):
        assert o.backlog.get(entries[bad].entry_id).status is BacklogStatus.FAILED
        chk = json.loads((ev / f"{entries[bad].entry_id}.evidence_check.json").read_text())
        assert all(v.startswith("ACQUISITION_NON_COMPLIANT") for v in chk["violations"])
    short = json.loads((ev / f"{entries['short'].entry_id}.evidence_check.json").read_text())
    assert any("missing or empty" in v for v in short["violations"])
    assert any("acquisition_report" in v for v in short["violations"])
    assert o.metrics.counters["acquisition.noncompliant"] == 2
    led = [
        json.loads(x) for x in (ev / "routing" / "routing_records.jsonl").read_text().splitlines()
    ]
    by = {r["task_id"]: r for r in led}
    assert {r["agent"] for r in led} == {"acquirer"}
    assert (by["ok"]["laziness_detected"], by["ok"]["final_score"]) == (False, 1.0)
    assert (by["short"]["laziness_detected"], by["short"]["final_score"]) == (True, 0.5)
    assert o.metrics.counters["routing.penalty.acquirer"] == 2

    # agent selection: 2 lazy in the last 10 -> acquirer blocked; tasks held, not dropped
    held = o.submit(make_task(task_id="held", type="research", inputs=ACQ_INPUTS)).entry
    assert o.backlog.get(held.entry_id).status is BacklogStatus.BLOCKED_AGENT
    info = json.loads((ev / f"{held.entry_id}.blocked_agent.json").read_text())
    assert info["agent"] == "acquirer"
    assert "unblock" in info["reason"]
    sel_led = [
        json.loads(x) for x in (ev / "routing/selection_records.jsonl").read_text().splitlines()
    ]
    assert sel_led[-1]["eligibility"] == "blocked"
    assert sel_led[-1]["routing_score"] == 0.25
    assert all(r["agent_selected"] != "overseer" for r in sel_led)
    assert sel_led[-1]["block_report"] is not None
    reports = sorted((ev / "blocks").glob("acquirer-*.json"))
    assert len(reports) == 1
    assert reports[0].with_suffix(".md").is_file()
    rep = json.loads(reports[0].read_text())
    assert rep["how"]["lazy_runs"][0]["checks_fired"] == ["ACQUISITION_NON_COMPLIANT"]
    assert rep["where"]["evidence_paths"]
    notes = list((root / "artifacts/outbox/notifications").glob("*.json"))
    assert json.loads(notes[0].read_text())["to"] == "owner"
    ov = o.blocks_overview()
    assert ov["states"]["acquisition"]["blocked"] is True
    assert [h["task_id"] for h in ov["held"]] == ["held"]
    assert len(ov["reports"]) == 1
    assert audit(root, config.doctrine, T0, root / "artifacts").compliant
    # honest failure is not lazy: no run record, grok stays eligible
    hon = o.submit(make_task(task_id="honest")).entry
    assert o.backlog.get(hon.entry_id).status is not BacklogStatus.BLOCKED_AGENT
    hres = GrokResult(hon.entry_id, "honest", "failed", T0, outputs={})
    (ev / f"{hon.entry_id}.json").write_text(json.dumps(hres.to_dict()))
    o.replay()
    assert o.backlog.get(hon.entry_id).status is BacklogStatus.FAILED
    runs = [json.loads(x) for x in (ev / "routing/routing_records.jsonl").read_text().splitlines()]
    assert "honest" not in {r["task_id"] for r in runs}
    assert o.backlog.get(held.entry_id).status is BacklogStatus.BLOCKED_AGENT  # still held
    # Owner-signed unblock resets the window -> released to pending on the next replay
    ub = root / "migrations" / "unblocks"
    ub.mkdir(parents=True, exist_ok=True)
    (ub / "2026-10-11-acquirer.grok").write_text(
        'FORMAT: "grok/1"\nKIND: "agent_unblock"\nAGENT: "acquirer"\n'
        f'RESET_AFTER_HASH: "{runs[-1]["hash"]}"\nAPPROVED_BY: "owner"\nSIGNED_OFF: true\n'
        'REASON: "test unblock"\nDATE: "2026-10-11"\n'
    )
    o.replay()
    assert o.backlog.get(held.entry_id).status is BacklogStatus.PENDING
    assert o.metrics.counters["agent_selection.released"] == 1


COG_INPUTS = {
    "agent": "analyst", "objective": "summarise", "input_artifacts": ["artifacts/evidence/d.json"],
    "output_schema": {"type": "object"},
}  # fmt: skip


def test_cognition_scope_and_startup_audit(root: Path, config: AppConfig, clock: FakeClock) -> None:
    h = FakeHttp()
    h.add("POST", "https://", oai())
    o = _orch(config, clock, h)
    audits = list((root / "artifacts/evidence/audits").glob("audit-*.json"))
    assert audits
    assert json.loads(audits[0].read_text())["compliant"] is True
    assert o.startup_audit in audits
    with pytest.raises(GovernanceError, match="COGNITION_NON_COMPLIANT"):
        o.submit(make_task(task_id="bad", type="research", inputs={"agent": "analyst"}))
    with pytest.raises(GovernanceError, match="GOVERNANCE_NON_COMPLIANT"):
        o.submit(make_task(task_id="hh", type="research", inputs={"agent": "overseer"}))
    ev = root / "artifacts/evidence"
    (ev / "p.txt").write_text("proof")
    e = o.submit(make_task(task_id="c", type="research", inputs=COG_INPUTS)).entry
    outputs = {"evidence": {"1": "artifacts/evidence/p.txt", "2": "artifacts/evidence/p.txt"}}
    res = GrokResult(e.entry_id, "c", "done", T0, outputs=outputs)
    (ev / f"{e.entry_id}.json").write_text(json.dumps(res.to_dict()))
    o.replay()
    assert o.backlog.get(e.entry_id).status is BacklogStatus.FAILED
    chk = json.loads((ev / f"{e.entry_id}.evidence_check.json").read_text())
    assert any(v.startswith("COGNITION_NON_COMPLIANT") for v in chk["violations"])
    assert o.metrics.counters["cognition.noncompliant"] == 1
