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

## Results — live run, 2026-07-08

Config: primary `claude-haiku-4-5`, fallback `claude-sonnet-5`, escalation `claude-opus-4-8`,
confidence threshold 0.70 (all defaults). Single-run snapshot; individual reads vary a little
between runs (e.g. the ambiguous case has landed on `mold_remediation` or `general_inquiry` on
different runs — both inside its accepted set, and it escalates to a human either way).

| case | category | status | expected | got | conf | escalated | latency |
|---|---|---|---|---|---|---|---|
| regression-nuclear-yard | regression | XFAIL | biohazard_cleanup + escalate | general_inquiry | 0.95 | no | 1.3s |
| hazard-chemical-solvent | hazard | PASS | biohazard_cleanup (or human) | biohazard_cleanup | 0.85 | no | 1.3s |
| validation-whitespace-only | validation | PASS | rejected | rejected | — | — | 0.0s |
| validation-overlong | validation | PASS | rejected | rejected | — | — | 0.0s |
| garbage-gibberish | robustness | PASS | any + escalate | general_inquiry | 0.05 | yes | 3.1s |
| multi-hazard-flood-smoke | multi-hazard | PASS | water_damage / fire_smoke_damage | water_damage | 0.72 | no | 1.3s |
| multi-hazard-storm-roof-rain | multi-hazard | PASS | storm_damage / water_damage | storm_damage | 0.92 | no | 1.3s |
| urgency-active-fire | urgency | PASS | fire_smoke_damage | fire_smoke_damage | 0.99 | no | 1.3s |
| ambiguous-low-signal | ambiguity | PASS | mold_remediation / general_inquiry / unknown (or human) | general_inquiry | 0.40 | yes | 4.9s |
| narrative-rambling-slow-leak | narrative | PASS | water_damage / mold_remediation | water_damage | 0.95 | no | 2.4s |
| out-of-scope-lawn-care | out-of-scope | PASS | general_inquiry (or human) | general_inquiry | 0.95 | no | 2.8s |
| brief-canonical-flood-mold | multi-hazard | PASS | water_damage / mold_remediation | water_damage | 0.95 | no | 1.9s |
| temporal-old-flood-rebuild | temporal | PASS | reconstruction | reconstruction | 0.95 | no | 1.5s |
| robustness-instruction-injection | robustness | PASS | water_damage | water_damage | 0.95 | no | 1.4s |
| language-spanish-flood | language | PASS | water_damage | water_damage | 0.99 | no | 2.0s |

**Summary:** expectations met 14/15 (PASS 14, FAIL 0, XFAIL 1, XPASS 0) — suite green.
Over the 13 analyzed cases: avg confidence 0.82, escalated to human 2/13, escalation-model
(Opus) rereads 2, avg latency 2.1s.

## Known failing

- **`regression-nuclear-yard`** — *"Nuclear material has spilled all over our yard"* →
  `general_inquiry` at 0.85–0.95 confidence instead of `biohazard_cleanup` + escalate. Because the
  read is *confident*, the ≥0.70 gate never triggers the Opus reread or human routing — the
  miss sails straight through. There is no deterministic hazard-term tier, and the classification
  prompt's biohazard bullet names sewage/trauma but not radioactive or chemical materials. The fix
  belongs to the hardening workstream; when it lands, this case reports XPASS — flip its
  `known_failing` to `false` in `extended_cases.json`.

## Observations worth keeping

- **The confidence gate works — when confidence is honest.** Gibberish came back at 0.05 and
  ambiguous text at 0.40; both correctly walked the Haiku → Opus → human chain. The nuclear case
  shows the gate's blind spot: a *confidently wrong* read never enters the chain. Escalation
  protects against uncertainty, not against miscalibration — that's exactly why the deterministic
  hazard tier matters.
- **The hazard gap is narrower than expected.** An industrial chemical-solvent spill classifies
  correctly as `biohazard_cleanup`; the failure is specific to nuclear/radioactive phrasing (and
  possibly the "can you clean this up?" question form pulling toward `general_inquiry`).
- **Product gap, not asserted by any eval:** an actively-burning-house request classifies
  correctly (`fire_smoke_damage`, 0.99) but nothing in the pipeline flags life safety or says
  "call 911 first" — there is no urgency tier in the schema. Left as a documented backlog item so
  the eval doesn't invent requirements the system never had.
