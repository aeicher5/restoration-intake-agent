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
- **`known_failing` (xfail semantics)** — a case that documents a real, currently-unfixed bug is
  marked `known_failing: true` with a one-line `diagnosis` from an observed live run. It reports
  as `XFAIL` and keeps the suite green; once fixed it reports `XPASS` with a loud reminder to flip
  the flag. The suite only goes red (`exit 1`) on *unexpected* failures.

## Results — live run, 2026-07-08 (post-hardening-merge)

Config: primary `claude-haiku-4-5`, fallback `claude-sonnet-5`, escalation `claude-opus-4-8`,
confidence threshold 0.70 (all defaults). Run after the hardening branch merged (deterministic
hazard screen in place). Single-run snapshot; individual reads vary a little between runs
(e.g. the ambiguous case has landed on `mold_remediation` or `general_inquiry` on different
runs — both inside its accepted set, and it escalates to a human either way).

| case | category | status | expected | got | conf | escalated | latency |
|---|---|---|---|---|---|---|---|
| regression-nuclear-yard | regression | PASS | biohazard_cleanup + escalate | biohazard_cleanup | 1.00 | yes | 0.0s |
| hazard-chemical-solvent | hazard | PASS | biohazard_cleanup (or human) | biohazard_cleanup | 1.00 | yes | 0.0s |
| validation-whitespace-only | validation | PASS | rejected | rejected | — | — | 0.0s |
| validation-overlong | validation | PASS | rejected | rejected | — | — | 0.0s |
| garbage-gibberish | robustness | PASS | any + escalate | general_inquiry | 0.05 | yes | 4.2s |
| multi-hazard-flood-smoke | multi-hazard | PASS | water_damage / fire_smoke_damage | water_damage | 0.72 | no | 1.7s |
| multi-hazard-storm-roof-rain | multi-hazard | PASS | storm_damage / water_damage | storm_damage | 0.95 | no | 1.2s |
| urgency-active-fire | urgency | PASS | fire_smoke_damage | fire_smoke_damage | 0.99 | no | 2.4s |
| ambiguous-low-signal | ambiguity | PASS | mold_remediation / general_inquiry / unknown (or human) | general_inquiry | 0.35 | yes | 4.9s |
| narrative-rambling-slow-leak | narrative | PASS | water_damage / mold_remediation | water_damage | 0.95 | no | 1.9s |
| out-of-scope-lawn-care | out-of-scope | PASS | general_inquiry (or human) | general_inquiry | 0.98 | no | 1.2s |
| brief-canonical-flood-mold | multi-hazard | PASS | water_damage / mold_remediation | water_damage | 0.85 | no | 2.1s |
| temporal-old-flood-rebuild | temporal | PASS | reconstruction | reconstruction | 0.95 | no | 1.2s |
| robustness-instruction-injection | robustness | PASS | water_damage | water_damage | 0.99 | no | 1.0s |
| language-spanish-flood | language | PASS | water_damage | water_damage | 0.99 | no | 1.0s |

**Summary:** expectations met 15/15 (PASS 15, FAIL 0, XFAIL 0, XPASS 0) — suite green.
Over the 13 analyzed cases: avg confidence 0.83, escalated to human 4/13, escalation-model
(Opus) rereads 2, avg latency 1.7s. Escalations rose from 2/13 pre-fix to 4/13 because
hazard-screened requests always route to a human — by design. Both hazard cases now resolve
deterministically: confidence 1.00, escalated, 0.0s, zero API calls.

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
- **Product gap, not asserted by any eval:** an actively-burning-house request classifies
  correctly (`fire_smoke_damage`, 0.99) but nothing in the pipeline flags life safety or says
  "call 911 first" — there is no urgency tier in the schema. Left as a documented backlog item so
  the eval doesn't invent requirements the system never had.
