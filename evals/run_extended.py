#!/usr/bin/env python3
"""Extended eval runner for the restoration-intake agent.

Runs the labeled cases in evals/extended_cases.json against the real pipeline
(imports agent.py from the repo root — never edits it) and scores each case
against its expectations. Complements the built-in `python3 agent.py --evals`
suite: the built-ins are one-dominant-signal cases; this suite deliberately
probes the seams — hazard materials, multi-hazard, ambiguity, garbage input,
long narratives, out-of-scope and non-English requests.

Statuses (pytest-style expected-failure semantics):
  PASS   expectations met
  FAIL   expectations not met            -> suite is red (exit 1)
  XFAIL  known_failing case still fails  -> expected, suite stays green
  XPASS  known_failing case now passes   -> loud notice to flip its flag, still green

Usage:
    python3 evals/run_extended.py             # full live run (needs ANTHROPIC_API_KEY)
    python3 evals/run_extended.py --check     # offline: validate cases + scorer self-test
    python3 evals/run_extended.py --only id1,id2   # live run of a subset, by case id
    python3 evals/run_extended.py --markdown  # also print a results table in Markdown
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402  (path bootstrap above must run first)

log = logging.getLogger("evals.extended")

CASES_PATH = Path(__file__).resolve().parent / "extended_cases.json"

VALID_OUTCOMES = frozenset({"analyzed", "rejected"})
VALID_TYPES = frozenset(t.value for t in agent.RequestType)
CASE_KEYS = frozenset({"id", "category", "why", "text", "repeat_text",
                       "expect", "known_failing", "diagnosis"})
EXPECT_KEYS = frozenset({"outcome", "types", "escalate", "pass_if_escalated"})


# ------------------------------------------------------------------- case loading

def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path}: top-level 'cases' must be a non-empty list")
    return cases


def validate_cases(cases: list[dict[str, Any]]) -> list[str]:
    """Return a list of schema problems (empty = clean). Catches typos and
    contradictory expectations before any API spend."""
    problems: list[str] = []
    seen_ids: set[str] = set()

    for i, case in enumerate(cases):
        where = f"case[{i}] ({case.get('id', '?')})"
        unknown = set(case) - CASE_KEYS
        if unknown:
            problems.append(f"{where}: unknown keys {sorted(unknown)}")

        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            problems.append(f"{where}: 'id' must be a non-empty string")
        elif case_id in seen_ids:
            problems.append(f"{where}: duplicate id {case_id!r}")
        else:
            seen_ids.add(case_id)

        if not isinstance(case.get("text"), str) or not case["text"]:
            problems.append(f"{where}: 'text' must be a non-empty string")
        repeat = case.get("repeat_text", 1)
        if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1:
            problems.append(f"{where}: 'repeat_text' must be a positive int")

        expect = case.get("expect")
        if not isinstance(expect, dict):
            problems.append(f"{where}: 'expect' must be an object")
            continue
        unknown = set(expect) - EXPECT_KEYS
        if unknown:
            problems.append(f"{where}: unknown expect keys {sorted(unknown)}")

        outcome = expect.get("outcome")
        if outcome not in VALID_OUTCOMES:
            problems.append(f"{where}: expect.outcome must be one of {sorted(VALID_OUTCOMES)}")
        if outcome == "rejected" and (set(expect) & {"types", "escalate", "pass_if_escalated"}):
            problems.append(f"{where}: a 'rejected' case cannot also expect types/escalation")

        types = expect.get("types", [])
        if not isinstance(types, list) or not set(types) <= VALID_TYPES:
            problems.append(f"{where}: expect.types must be a list drawn from {sorted(VALID_TYPES)}")
        if "escalate" in expect and not isinstance(expect["escalate"], bool):
            problems.append(f"{where}: expect.escalate must be a bool")
        if "pass_if_escalated" in expect and expect.get("pass_if_escalated") is not True:
            problems.append(f"{where}: pass_if_escalated is either absent or literally true")
        if "pass_if_escalated" in expect and "escalate" in expect:
            problems.append(f"{where}: pass_if_escalated conflicts with an explicit escalate "
                            "expectation — pick one")

        known_failing = case.get("known_failing", False)
        if not isinstance(known_failing, bool):
            problems.append(f"{where}: known_failing must be a bool")
        diagnosis = case.get("diagnosis")
        if known_failing and (not isinstance(diagnosis, str) or not diagnosis.strip()):
            problems.append(f"{where}: known_failing cases need a one-line 'diagnosis'")

    return problems


def case_text(case: dict[str, Any]) -> str:
    return case["text"] * case.get("repeat_text", 1)


# ----------------------------------------------------------------------- scoring

def judge(case: dict[str, Any], outcome: str,
          analysis: "agent.Analysis | None") -> tuple[bool, str]:
    """Score one observed result against the case's expectations.

    Pure function of (case, outcome, analysis) so --check can exercise it
    offline with synthetic Analysis objects.
    """
    expect = case["expect"]
    if outcome == "error":
        return False, "pipeline raised an unexpected exception"
    if outcome != expect["outcome"]:
        return False, f"expected outcome={expect['outcome']}, got {outcome}"
    if outcome == "rejected":
        return True, "rejected by deterministic validation, as expected"

    assert analysis is not None  # outcome == "analyzed" guarantees this
    got_type = analysis.request_type.value
    escalated = analysis.escalate_to_human

    if expect.get("pass_if_escalated") and escalated:
        return True, f"routed to human (acceptable for this case); read was {got_type}"

    types = expect.get("types", [])
    if types and got_type not in types:
        return False, f"type {got_type} not in accepted {types}"
    if "escalate" in expect and escalated is not expect["escalate"]:
        return False, f"escalate_to_human={escalated}, expected {expect['escalate']}"
    return True, f"classified {got_type}" + (" and escalated" if escalated else "")


def status_of(passed: bool, known_failing: bool) -> str:
    if passed:
        return "XPASS" if known_failing else "PASS"
    return "XFAIL" if known_failing else "FAIL"


# ----------------------------------------------------------------------- live run

def run_live(cases: list[dict[str, Any]], markdown: bool) -> int:
    try:
        agent.load_env()
        settings = agent.Settings.from_env()
    except agent.ConfigError as exc:
        log.error("startup failed: %s", exc)
        return 1
    log.info("config: %s", settings.summary())
    print(f"running {len(cases)} extended eval cases against the live API...\n")

    intake = agent.IntakeAgent(settings)
    rows: list[dict[str, Any]] = []
    for case in cases:
        started = time.monotonic()
        analysis: "agent.Analysis | None" = None
        detail = ""
        try:
            analysis = intake.handle(case_text(case))
            outcome = "analyzed"
        except agent.ValidationError as exc:
            outcome, detail = "rejected", str(exc)
        except Exception as exc:  # never let one case kill the suite
            outcome, detail = "error", f"{type(exc).__name__}: {exc}"
            log.error("case %s crashed the pipeline: %s", case["id"], detail)
        elapsed = time.monotonic() - started

        passed, reason = judge(case, outcome, analysis)
        status = status_of(passed, case.get("known_failing", False))
        rows.append({
            "case": case, "status": status, "outcome": outcome, "reason": reason,
            "analysis": analysis, "seconds": elapsed, "detail": detail,
        })
        got = analysis.request_type.value if analysis else outcome
        conf = f"{analysis.confidence:.2f}" if analysis else "   -"
        esc = str(analysis.escalate_to_human) if analysis else "-"
        print(f"{status:<5}  {case['id']:<32} got={got:<18} conf={conf} "
              f"escalate={esc:<5} ({elapsed:.1f}s)  {reason}")

    print()
    return summarize(rows, markdown)


def summarize(rows: list[dict[str, Any]], markdown: bool) -> int:
    n = len(rows)
    by_status = {s: [r for r in rows if r["status"] == s]
                 for s in ("PASS", "FAIL", "XFAIL", "XPASS")}
    met = len(by_status["PASS"]) + len(by_status["XPASS"])

    print(f"expectations met: {met}/{n}   "
          f"(PASS {len(by_status['PASS'])}, FAIL {len(by_status['FAIL'])}, "
          f"XFAIL {len(by_status['XFAIL'])}, XPASS {len(by_status['XPASS'])})")
    analyzed = [r for r in rows if r["analysis"] is not None]
    if analyzed:
        avg_conf = sum(r["analysis"].confidence for r in analyzed) / len(analyzed)
        escalated = sum(r["analysis"].escalate_to_human for r in analyzed)
        avg_secs = sum(r["seconds"] for r in analyzed) / len(analyzed)
        rereads = sum(
            1 for r in analyzed
            for step in r["analysis"].audit
            if step["step"] == "finalized" and step.get("action") != "passed"
        )
        print(f"analyzed cases: {len(analyzed)}   avg confidence: {avg_conf:.2f}   "
              f"escalated to human: {escalated}/{len(analyzed)}   "
              f"escalation-model rereads: {rereads}   avg latency: {avg_secs:.1f}s")

    for r in by_status["FAIL"]:
        print(f"  FAIL  {r['case']['id']}: {r['reason']}")
    for r in by_status["XFAIL"]:
        print(f"  XFAIL {r['case']['id']} (known failing): {r['case']['diagnosis']}")
    for r in by_status["XPASS"]:
        print(f"  XPASS {r['case']['id']}: now passes — flip its known_failing to false "
              "in extended_cases.json")

    if markdown:
        print("\n" + results_markdown(rows))

    green = not by_status["FAIL"]
    print(f"\nsuite: {'green' if green else 'RED'}")
    return 0 if green else 1


def results_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| case | category | status | expected | got | conf | escalated | latency |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        case, analysis = r["case"], r["analysis"]
        expect = case["expect"]
        if expect["outcome"] == "rejected":
            expected = "rejected"
        else:
            expected = " / ".join(expect.get("types") or ["any"])
            if expect.get("escalate") is True:
                expected += " + escalate"
            if expect.get("pass_if_escalated"):
                expected += " (or human)"
        got = analysis.request_type.value if analysis else r["outcome"]
        conf = f"{analysis.confidence:.2f}" if analysis else "—"
        esc = ("yes" if analysis.escalate_to_human else "no") if analysis else "—"
        lines.append(f"| {case['id']} | {case['category']} | {r['status']} | {expected} "
                     f"| {got} | {conf} | {esc} | {r['seconds']:.1f}s |")
    return "\n".join(lines)


# ------------------------------------------------------------------ offline check

def check(cases: list[dict[str, Any]]) -> int:
    """Offline gate: cases file is schema-clean and the scorer behaves. No network."""
    problems = validate_cases(cases)
    for p in problems:
        print(f"SCHEMA  {p}")
    if problems:
        print(f"\ncheck: {len(problems)} problem(s) in {CASES_PATH.name}")
        return 1

    def synthetic(request_type: agent.RequestType, confidence: float,
                  escalate: bool) -> agent.Analysis:
        return agent.Analysis(request_id="req_selftest", request_type=request_type,
                              confidence=confidence, escalate_to_human=escalate,
                              notes="synthetic scorer-check analysis")

    water = synthetic(agent.RequestType.WATER_DAMAGE, 0.9, False)
    storm_escalated = synthetic(agent.RequestType.STORM_DAMAGE, 0.4, True)

    scenarios = [  # (expect, outcome, analysis, should_pass)
        ({"outcome": "analyzed", "types": ["water_damage"]}, "analyzed", water, True),
        ({"outcome": "analyzed", "types": ["mold_remediation"]}, "analyzed", water, False),
        ({"outcome": "analyzed", "types": ["water_damage"], "escalate": False}, "analyzed", water, True),
        ({"outcome": "analyzed", "escalate": True}, "analyzed", water, False),
        ({"outcome": "analyzed", "escalate": True}, "analyzed", storm_escalated, True),
        ({"outcome": "analyzed", "types": ["general_inquiry"], "pass_if_escalated": True},
         "analyzed", storm_escalated, True),
        ({"outcome": "analyzed", "types": ["general_inquiry"], "pass_if_escalated": True},
         "analyzed", water, False),
        ({"outcome": "analyzed", "types": []}, "analyzed", water, True),
        ({"outcome": "rejected"}, "rejected", None, True),
        ({"outcome": "rejected"}, "analyzed", water, False),
        ({"outcome": "analyzed", "types": ["water_damage"]}, "rejected", None, False),
        ({"outcome": "analyzed", "types": ["water_damage"]}, "error", None, False),
    ]
    for i, (expect, outcome, analysis, should_pass) in enumerate(scenarios):
        got_pass, reason = judge({"expect": expect}, outcome, analysis)
        assert got_pass is should_pass, \
            f"scorer scenario {i} ({expect} / {outcome}): got {got_pass}, want {should_pass} — {reason}"

    # Length-bound cases must actually sit on the right side of the validator's bounds.
    for case in cases:
        text = agent.normalize_text(case_text(case))
        if case["expect"]["outcome"] == "rejected":
            assert not agent.MIN_TEXT_CHARS <= len(text) <= agent.MAX_TEXT_CHARS, \
                f"{case['id']}: expected rejection but text length {len(text)} is within bounds"
        else:
            assert agent.MIN_TEXT_CHARS <= len(text) <= agent.MAX_TEXT_CHARS, \
                f"{case['id']}: text length {len(text)} would be rejected before classification"

    known = [c["id"] for c in cases if c.get("known_failing")]
    print(f"check: {len(cases)} cases schema-clean, {len(scenarios)} scorer scenarios pass, "
          f"length bounds verified")
    print(f"check: known_failing = {known if known else 'none'}")
    return 0


# -------------------------------------------------------------------- entrypoint

def main(argv: "list[str] | None" = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="offline: validate the cases file and self-test the scorer")
    parser.add_argument("--only", metavar="IDS",
                        help="comma-separated case ids to run (live)")
    parser.add_argument("--markdown", action="store_true",
                        help="also print the results table as Markdown (for evals/README.md)")
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.check:
        return check(cases)

    problems = validate_cases(cases)
    if problems:
        for p in problems:
            print(f"SCHEMA  {p}", file=sys.stderr)
        return 1

    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = wanted - {c["id"] for c in cases}
        if unknown:
            log.error("no such case id(s): %s", ", ".join(sorted(unknown)))
            return 64
        cases = [c for c in cases if c["id"] in wanted]

    return run_live(cases, markdown=args.markdown)


if __name__ == "__main__":
    sys.exit(main())
