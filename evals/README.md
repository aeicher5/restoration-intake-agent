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

## The promotion surface: versioned prompts and per-class thresholds

Everything the pipeline's judgment depends on — the three system prompts and the confidence
thresholds — lives in `agent.PROMPT_CONFIG`, a frozen structure with an explicit `version`
string and a content `fingerprint()`. Every audit trail's `classified` step records both, so
any request can be traced to the exact artifact that read it. **Changing any of it means
bumping the version, and the CI promotion gate re-runs this suite before the change lands**
(see `.github/workflows/`).

Confidence thresholds are per-class-aware (config `1.2.0`):

| class | bar | why |
|---|---|---|
| `biohazard_cleanup` | **0.85** | a wrong read near hazmat sends a crew in with the wrong protective posture; the deterministic hazard screen catches *obvious* phrasing at confidence 1.0, this floor guards the subtler model-read calls |
| `fire_smoke_damage` | **0.85** | fire routing sits next to the life-safety tier; a shaky fire read gets a second opinion before passing as routine intake |
| everything else | 0.70 (global) | cheap to misroute — a water/mold/storm mix-up costs a phone call |

Per-class values are *floors*: the effective bar is `max(global, class floor)`, so raising the
global via `INTAKE_CONFIDENCE_THRESHOLD` tightens everything and a class entry can never
silently loosen below it. The bar is judged against the class each read actually reports (an
Opus reread that lands on a different class is judged against *that* class's bar), and the
`finalized` audit step records the effective threshold and its source
(`class:fire_smoke_damage` vs `global`) for every request.

The suite pins this two ways: the `threshold` category cases probe borderline safety-class
phrasing live, and a **suite-wide invariant** checks every analyzed case — a final read whose
confidence sits below the bar its own `finalized` step recorded must have been escalated.
Selftest (`python3 agent.py --selftest`) pins the exact shipped floors offline.

## Closing the loop: corrections from resolved escalations

When a human resolves an escalated request and corrects the classifier's read, that's a
labeled example — exactly what this suite is made of. `ingest_corrections.py` harvests them:

```bash
python3 evals/ingest_corrections.py             # reads audit_log.jsonl at the repo root
python3 evals/ingest_corrections.py --log PATH  # or any JSONL audit log
python3 evals/ingest_corrections.py --selftest  # offline check on a synthetic log
```

It scans the log for `escalation_resolved` events (the escalation workstream owns that event
shape, so consumption is generic — top-level records or nested audit steps, several
correction-key spellings, nested `resolution` objects) and every resolved event carrying a
corrected type becomes a candidate in **`evals/corrections.json`**: the corrected label, the
original request text, provenance (who resolved it, when, what the model originally read),
and a ready-to-edit `proposed_case` in this suite's schema.

**`corrections.json` is a review queue, not a golden set.** Nothing is ever auto-merged into
`extended_cases.json` — the script refuses to write that file by construction. A human
reviews each candidate (corrections can be wrong too), edits the proposed case, copies it in
by hand, and marks the candidate `merged` or `rejected`. Re-runs are idempotent and never
overwrite a candidate a human has touched. `request_type` alone never counts as a correction
(that's the model's original read); resolutions correcting to `unknown` are skipped by the
same rule — a fail-safe value is not a label.

## Results — live run, 2026-07-08 (post-promotion-pass)

Config: primary `claude-haiku-4-5`, fallback `claude-sonnet-5`, escalation `claude-opus-4-8`,
prompt config **1.2.0** (global threshold 0.70; per-class floors biohazard_cleanup 0.85,
fire_smoke_damage 0.85 — see the promotion-surface section above). Run after the promotion
pass landed the versioned config surface, the per-class floors, and the two `threshold`
cases: 19 cases. Single-run snapshot; individual reads vary a little between runs (e.g. the
ambiguous case has landed on `mold_remediation` or `general_inquiry` on different runs — both
inside its accepted set, and it escalates to a human either way).

| case | category | status | expected | got | conf | escalated | life | reply | latency |
|---|---|---|---|---|---|---|---|---|---|
| regression-nuclear-yard | regression | PASS | biohazard_cleanup + escalate | biohazard_cleanup | 1.00 | yes | no | skipped | 0.0s |
| hazard-chemical-solvent | hazard | PASS | biohazard_cleanup (or human) | biohazard_cleanup | 1.00 | yes | no | skipped | 0.0s |
| validation-whitespace-only | validation | PASS | rejected | rejected | — | — | — | — | 0.0s |
| validation-overlong | validation | PASS | rejected | rejected | — | — | — | — | 0.0s |
| garbage-gibberish | robustness | PASS | any + escalate | general_inquiry | 0.10 | yes | no | drafted | 6.3s |
| multi-hazard-flood-smoke | multi-hazard | PASS | water_damage / fire_smoke_damage | water_damage | 0.72 | yes | no | fallback | 11.0s |
| multi-hazard-storm-roof-rain | multi-hazard | PASS | storm_damage / water_damage | storm_damage | 0.95 | no | no | drafted | 4.2s |
| urgency-active-fire | urgency | PASS | fire_smoke_damage + escalate + life-safety | fire_smoke_damage | 0.99 | yes | yes | drafted | 6.1s |
| life-safety-gas-leak-co | urgency | PASS | any + escalate + life-safety | general_inquiry | 0.35 | yes | yes | drafted | 6.1s |
| life-safety-negative-past-fire | urgency | PASS | fire_smoke_damage | fire_smoke_damage | 0.95 | no | no | drafted | 3.2s |
| ambiguous-low-signal | ambiguity | PASS | mold_remediation / general_inquiry / unknown (or human) | mold_remediation | 0.40 | yes | no | drafted | 6.9s |
| narrative-rambling-slow-leak | narrative | PASS | water_damage / mold_remediation | water_damage | 0.98 | no | no | drafted | 7.2s |
| out-of-scope-lawn-care | out-of-scope | PASS | general_inquiry (or human) | general_inquiry | 0.95 | no | no | drafted | 3.4s |
| brief-canonical-flood-mold | multi-hazard | PASS | water_damage / mold_remediation | water_damage | 0.95 | no | no | drafted | 3.1s |
| temporal-old-flood-rebuild | temporal | PASS | reconstruction | reconstruction | 0.95 | no | no | drafted | 3.3s |
| robustness-instruction-injection | robustness | PASS | water_damage | water_damage | 0.98 | no | no | drafted | 3.7s |
| language-spanish-flood | language | PASS | water_damage | water_damage | 0.98 | no | no | drafted | 4.6s |
| threshold-borderline-smoke-odor | threshold | PASS | fire_smoke_damage / general_inquiry / unknown (or human) | general_inquiry | 0.72 | no | no | drafted | 7.1s |
| threshold-borderline-unknown-substance | threshold | PASS | biohazard_cleanup / general_inquiry / unknown (or human) | biohazard_cleanup | 0.85 | no | no | drafted | 3.3s |

**Summary:** expectations met 19/19 (PASS 19, FAIL 0, XFAIL 0, XPASS 0) — suite green.
Over the 17 analyzed cases: avg confidence 0.81, escalated to human 7/17, life-safety flags 2,
escalation-model (Opus) rereads 3, avg latency 4.7s. Customer replies: drafted 14, fallback 1,
skipped 2 (the hazard-screen cases — that path stays zero-API-call by design). Metrics are
stable vs. the product-pass run (avg conf 0.83 → 0.81, same three rereads, same fallback
case); every `classified` step in every trail now also carries the prompt-config version and
fingerprint (`1.2.0`), which is the point of this pass. The two new `threshold` cases both
landed on the *pass* side of their bars this run — and one landed **exactly on it**: the
unknown-substance case read `biohazard_cleanup` at precisely 0.85, the class floor, so it
passed unescalated (at 0.84 it would have taken the Opus reread). The smoke-odor case read
`general_inquiry` 0.72, judged against the global 0.70 since that's the class it reported.
Runs where a safety-class read lands under its floor take the reread/escalate chain instead —
that mechanic is pinned deterministically in selftest, and the suite-wide invariant (a final
read below the bar its own `finalized` step records must be escalated) would redden this
suite if the pipeline ever let one slip.

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
- **Intended behavior, decided at review: pricing questions escalate.** The built-in suite's
  pricing case (*"can you give me a rough estimate for what mold remediation typically costs?"*)
  classifies `general_inquiry` at 0.95 and, since the reply review gate landed, escalates to a
  human (built-in escalations moved 2/12 → 3/12). The critic refuses to approve any draft that
  promises costs, so the request falls back to the dispatcher note and routes to a person — on
  the one case whose honest answer *is* a price, that's the checklist working, not a regression.
  Pricing answers stay with humans until a real quoting flow exists; teaching the responder to
  decline pricing questions gracefully is a known one-line prompt change, deliberately deferred.
