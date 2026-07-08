# Extended evals

The built-in suite (`python3 agent.py --evals`, 12 cases) is deliberately clean: one dominant
signal per case, no cross-category trigger words. This suite is the opposite — it probes the
seams: hazard materials, multi-hazard requests, garbage and overlong input, ambiguity, long
rambling narratives, out-of-scope requests, non-English text, and instruction-like content
embedded in customer text.

## Running

```bash
python3 evals/run_extended.py             # full live run (needs ANTHROPIC_API_KEY in .env)
python3 evals/run_extended.py --check     # offline: validate cases + scorer self-test, no network
python3 evals/run_extended.py --only regression-nuclear-yard   # subset by case id
python3 evals/run_extended.py --markdown  # also emit the results table below
```

The runner imports `agent.py` from the repo root and never modifies it.

## How cases are labeled

Cases live in `extended_cases.json` (schema documented in the file's `_schema` header). The
important labeling choices:

- **`types` is a set, not a single answer.** Genuinely multi-hazard requests ("flooded basement
  and a smoke smell") accept any defensible dominant read; single-signal cases accept exactly one.
- **`pass_if_escalated`** — for cases where routing to a human *is* a correct answer (ambiguous,
  out-of-scope, hazard), escalation passes regardless of the type read. A confident wrong read
  still fails.
- **`life_safety`** — asserts the deterministic life-safety screen's flag exactly (true *or*
  false — the negative matters: past-tense fire damage must not trip it). Checked before
  `pass_if_escalated`, so human routing never excuses a missed or spurious screen decision.
  Two invariants are also enforced globally, no per-case label needed: a drafted (non-fallback)
  reply on a life-safety analysis must open with the 911 line, and a live rejection must arrive
  with a persistable trail (`status: rejected` + non-empty audit).
- **`known_failing` (xfail semantics)** — a case that documents a real, currently-unfixed bug is
  marked `known_failing: true` with a one-line `diagnosis` from an observed live run. It reports
  as `XFAIL` and keeps the suite green; once fixed it reports `XPASS` with a loud reminder to flip
  the flag. The suite only goes red (`exit 1`) on *unexpected* failures.

## Results — live run, 2026-07-08 (post-product-pass)

Config: primary `claude-haiku-4-5`, fallback `claude-sonnet-5`, escalation `claude-opus-4-8`,
confidence threshold 0.70 (all defaults). Run after the product pass landed all three
known-gap features (life-safety tier, customer-reply review gate, persisted rejections):
17 cases, and every analyzed case now also runs the responder + critic live. Single-run
snapshot; individual reads vary a little between runs (e.g. the ambiguous case has landed on
`mold_remediation` or `general_inquiry` on different runs — both inside its accepted set, and
it escalates to a human either way).

| case | category | status | expected | got | conf | escalated | life | reply | latency |
|---|---|---|---|---|---|---|---|---|---|
| regression-nuclear-yard | regression | PASS | biohazard_cleanup + escalate | biohazard_cleanup | 1.00 | yes | no | skipped | 0.0s |
| hazard-chemical-solvent | hazard | PASS | biohazard_cleanup (or human) | biohazard_cleanup | 1.00 | yes | no | skipped | 0.0s |
| validation-whitespace-only | validation | PASS | rejected | rejected | — | — | — | — | 0.0s |
| validation-overlong | validation | PASS | rejected | rejected | — | — | — | — | 0.0s |
| garbage-gibberish | robustness | PASS | any + escalate | general_inquiry | 0.05 | yes | no | drafted | 8.1s |
| multi-hazard-flood-smoke | multi-hazard | PASS | water_damage / fire_smoke_damage | water_damage | 0.55 | yes | no | fallback | 11.4s |
| multi-hazard-storm-roof-rain | multi-hazard | PASS | storm_damage / water_damage | storm_damage | 0.92 | no | no | drafted | 3.6s |
| urgency-active-fire | urgency | PASS | fire_smoke_damage + escalate + life-safety | fire_smoke_damage | 0.99 | yes | yes | drafted | 2.7s |
| life-safety-gas-leak-co | urgency | PASS | any + escalate + life-safety | biohazard_cleanup | 0.85 | yes | yes | drafted | 4.3s |
| life-safety-negative-past-fire | urgency | PASS | fire_smoke_damage | fire_smoke_damage | 0.95 | no | no | drafted | 3.7s |
| ambiguous-low-signal | ambiguity | PASS | mold_remediation / general_inquiry / unknown (or human) | mold_remediation | 0.40 | yes | no | drafted | 6.0s |
| narrative-rambling-slow-leak | narrative | PASS | water_damage / mold_remediation | water_damage | 0.95 | no | no | drafted | 3.3s |
| out-of-scope-lawn-care | out-of-scope | PASS | general_inquiry (or human) | general_inquiry | 0.98 | no | no | drafted | 3.3s |
| brief-canonical-flood-mold | multi-hazard | PASS | water_damage / mold_remediation | water_damage | 0.95 | no | no | drafted | 3.1s |
| temporal-old-flood-rebuild | temporal | PASS | reconstruction | reconstruction | 0.95 | no | no | drafted | 3.3s |
| robustness-instruction-injection | robustness | PASS | water_damage | water_damage | 0.99 | no | no | drafted | 3.5s |
| language-spanish-flood | language | PASS | water_damage | water_damage | 0.99 | no | no | drafted | 3.4s |

**Summary:** expectations met 17/17 (PASS 17, FAIL 0, XFAIL 0, XPASS 0) — suite green.
Over the 15 analyzed cases: avg confidence 0.83, escalated to human 7/15, life-safety flags 2,
escalation-model (Opus) rereads 3, avg latency 4.0s. Customer replies: drafted 12, fallback 1,
skipped 2 (the hazard-screen cases — that path stays zero-API-call by design, so nothing is
drafted). Worth naming honestly: avg latency roughly doubled vs. the pre-reply run (1.7s →
4.0s) because every analyzed request now spends two extra primary-model calls on draft +
critique. Escalations rose 4/13 → 7/15: the two life-safety cases force human review, and one
review-gate fallback (`multi-hazard-flood-smoke`: a low-confidence read whose draft the critic
rejected twice) routed to a human with the dispatcher note as its reply — the fallback path
exercised live, and the case still passes its type expectation. Both drafted life-safety
replies opened with the 911 line (the scorer's global invariant); both validation rejects
arrived with a store-ready rejected trail.

## Known failing

None. One case was known-failing when this suite was written; the record is kept for history:

- **`regression-nuclear-yard`** (fixed 2026-07-08 by the hardening workstream, same evening) —
  pre-fix, *"Nuclear material has spilled all over our yard"* → `general_inquiry` at 0.85–0.95
  confidence instead of `biohazard_cleanup` + escalate. Because the read was *confident*, the
  ≥0.70 gate never triggered the Opus reread or human routing — the miss sailed straight through:
  no deterministic hazard-term tier, and the classification prompt's biohazard bullet named
  sewage/trauma but not radioactive or chemical materials. The fix added a deterministic
  hazard-term screen ahead of any LLM call; the case reported XPASS on the post-merge run and its
  `known_failing` flag was flipped to `false`.

## Observations worth keeping

- **The confidence gate works — when confidence is honest.** Gibberish came back at 0.05 and
  ambiguous text at 0.35–0.40; both correctly walked the Haiku → Opus → human chain. The nuclear
  case showed the gate's blind spot: a *confidently wrong* read never enters the chain. Escalation
  protects against uncertainty, not against miscalibration — that's exactly why the deterministic
  hazard tier (now in place) matters.
- **The hazard gap was narrower than expected.** An industrial chemical-solvent spill classified
  correctly as `biohazard_cleanup` even pre-fix; the failure was specific to nuclear/radioactive
  phrasing (and possibly the "can you clean this up?" question form pulling toward
  `general_inquiry`). Post-fix, both resolve at the deterministic tier without an API call.
- **Product gap, closed 2026-07-08 (product pass):** an actively-burning-house request used to
  classify correctly (`fire_smoke_damage`, 0.99) with nothing flagging life safety. The pipeline
  now runs a deterministic life-safety screen (active fire / gas leak / carbon monoxide /
  smoke-right-now / 911 phrasing, word-boundary matched, zero API calls) ahead of classification:
  it sets `life_safety` on the analysis, forces human escalation, and classification still runs
  normally. The urgency cases above assert it; past-tense fire damage must stay unflagged.
