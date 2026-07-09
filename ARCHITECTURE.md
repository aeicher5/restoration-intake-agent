# ARCHITECTURE — restoration-services intake agent

## What this is

An AI work-agent platform for a restoration-services company (water damage,
fire/smoke, mold, storm, biohazard cleanup, reconstruction). A customer writes
in free text — *"my basement is flooded, the carpet is soaked, and I'm worried
about mold"* — and the system handles the request end to end: **intake →
analyze/classify → respond → track**. Greenfield by design: the platform owns
its own data layer.

The shape is a **staged workflow, not an open-ended agent**. The task is
bounded, every stage is inspectable, and human escalation is the safety valve.

## The pipeline

```
free-text request (web form · CLI · JSON API)
      │
      ▼
1  deterministic validation gate          code-owned: length bounds, channel
      │                                   whitelist; bad input → rejected with
      │                                   a reference id, no LLM spend — the
      │                                   rejected trail is persisted too
      ▼
2  deterministic hazard screen            word-boundary term match (nuclear,
      │                                   radioactive, chemical, sewage,
      │  match ──────────────────────►    asbestos, hazmat, …) short-circuits:
      │                                   biohazard_cleanup, confidence 1.0,
      │                                   escalate_to_human — no model call
      ▼
3  deterministic life-safety screen       active fire / gas leak / carbon
      │                                   monoxide / 911 phrasing → sets
      │  (match: flag + force human       life_safety, forces human review —
      │   review; pipeline continues)     never decided by a model; past-tense
      │                                   damage must not trip it
      ▼
4  LLM classification — Haiku             structured JSON against a closed
      │  failure/refusal → Sonnet         8-type enum; the model cannot invent
      │  transient errors → retry         types. Retry w/ exponential backoff
      │  total failure → fail-safe stub   + jitter, transient errors only
      ▼
5  confidence gate (per-class floors)     model-reported confidence, clamped
      │  below its bar → reread on Opus   by code. Effective bar = max(global
      │  still below → human review       0.70, class floor): biohazard_cleanup
      │                                   and fire_smoke_damage reads need
      │                                   0.85 — judged against the class the
      │                                   read reports; the finalized step
      │                                   records the bar + its source
      ▼
6  respond — reviewed customer reply      draft (Haiku) → critic (Haiku
      │                                   checklist: no cost promises, no
      │  reject → one revision →          guarantees; life-safety replies must
      │  reject again → fallback to       open with the 911 line — that opener
      │  dispatcher note + human          is also checked deterministically) —
      │                                   skipped on deterministic-hazard and
      │                                   failed-classification paths
      ▼
7  audit trail                            JSON step records: received,
      │                                   hazard_screen, life_safety_screen,
      │                                   classified (attempts, token usage,
      │                                   retries), reply_drafted /
      │                                   reply_reviewed / customer_reply,
      │                                   finalized — every request, every
      ▼                                   decision, with the why
8  admin surface + dispatcher queue       /admin table + stats strip over the
                                          trail; /admin/queue works the open
                                          escalations (see the workflow
                                          section below); per-request detail;
                                          optional ADMIN_TOKEN gate
```

Provider failures never crash the pipeline; the worst case for any request is
`unknown` + escalate-to-human, with the failure chain recorded in the trail.
Both safety screens are deterministic on purpose — see the third design
principle — and both cost zero API calls.

## Design principles (locked in the original build, kept tonight)

- **Code owns the hard schema; the LLM owns soft judgment.** Request IDs,
  the type enum, validation bounds, thresholds, escalation rules — code.
  Reading messy human text — the model.
- **Model economics:** Haiku by default; deterministic code instead of an LLM
  call wherever possible (both safety screens and validation answer in 0.0s
  for free); Opus only for low-confidence rereads. A fully analyzed request is
  ~3 Haiku calls (classify + reply draft + critic ≈ 1.4k in / 130 out tokens,
  ≈ $0.002 at list rates); deterministic paths stay at zero. Cost scales with
  ambiguity, not volume.
- **Escalation protects against uncertainty, not miscalibration.** The
  confidence gate catches "I'm not sure"; it cannot catch confidently-wrong.
  That is why safety-critical routing (hazmat) sits in the deterministic tier
  ahead of the model — over-triggering costs one human review; the reverse
  costs a crew walking into a hazmat scene.
- **Auditability is a requirement, not a feature.** Every step records what
  happened and why; the admin view is just a renderer over that trail.

## The escalation workflow: state folded from events

"Escalate to a human" stopped being a terminal flag. Escalated requests carry
a workflow state — **open → acknowledged → resolved** — and the state is not
stored anywhere: it is **derived by folding workflow events over the same
append-only store** the pipeline writes. Event lines share the file with
request records, discriminated by `kind: "escalation_event"`; `AuditStore`'s
record readers filter them out, so nothing that reads requests had to change.

- `escalation_opened` is appended when an escalated record is stored (with
  machine-derived reasons: `life_safety`, `hazard_screen`, `low_confidence`,
  …). Life-safety openings fire a pluggable notification hook
  (`escalations.NOTIFIERS`; ships with a structured log-line channel — SMS or
  pager callables register alongside it).
- `/admin/queue` is the dispatcher surface: unresolved escalations,
  life-safety pinned first, acknowledge/resolve actions on each card.
- Resolving records the human's decision — `confirmed` or `corrected`, the
  final type, and a note — **into the same audit trail**, so the record of a
  request now runs from `received` through the model's reasoning to the
  human's disposition. Escalated records with no events yet derive as
  implicitly open: pre-workflow stores need no migration.

## The promotion flywheel: eval-gated change, human-reviewed corrections

Everything the pipeline's judgment depends on — the three system prompts and
the confidence thresholds — is one versioned artifact (`agent.PROMPT_CONFIG`,
currently `1.2.0`, with a content fingerprint). Every trail's `classified`
step records the version + fingerprint, so any request traces to the exact
artifact that read it; `/health` reports it.

Changing the artifact is gated: `.github/workflows/promotion-gate.yml`
triggers on prompt/eval/config paths, always runs the offline checks, and
runs the **live extended suite** when the repository has an API-key secret
(skipping loudly when it doesn't). Prompts don't change without the evals
passing on the change — the regression gate the eval suite always wanted
to be.

The loop closes from production: `evals/ingest_corrections.py` harvests
`escalation_resolved` events where a human corrected the model's read and
turns each into a candidate eval case in `evals/corrections.json` — original
text, corrected label, provenance, and a ready-to-edit `proposed_case`.
**Human review is the gate by construction**: the script refuses to write
`extended_cases.json`, re-runs never overwrite a reviewed candidate, and a
correction only enters the suite when a person copies it in. Dispatcher
corrections become tomorrow's regression cases; the promotion gate then
holds every future prompt change to them.

## Components

| Path | What it is |
|---|---|
| `agent.py` | The whole pipeline + config, offline `--selftest` (fake client), live `--evals`, stdlib dev server (`--serve`) |
| `web.py`, `templates/` | FastAPI layer: `/` customer intake (per-IP rate limit), `/admin` audit table + stats strip, `/admin/queue` dispatcher queue, `/admin/{id}` detail, `/health` (all admin views behind the optional `ADMIN_TOKEN` gate). Owns persistence via an append-only JSONL audit store (the pipeline itself is stateless); renders new pipeline steps/fields generically — no hardcoded step list |
| `escalations.py` | The workflow state machine: event shapes, state folding, escalation reasons, notification hook. Stdlib-only, self-testing (`python3 escalations.py`) |
| `evals/` | 19-case extended suite + standalone runner (xfail semantics, offline `--check`) + `ingest_corrections.py` (resolved escalations → candidate cases) — see `evals/README.md` |
| `Dockerfile` | python-slim, non-root; runs `--selftest` by default |
| `.github/workflows/` | `ci.yml`: offline suite on every push (selftests incl. escalations + ingest, eval check, web import) — zero secrets. `promotion-gate.yml`: live eval suite on prompt/eval changes |
| `railway.toml`, `runtime.txt`, `DEPLOY.md` | Config-as-code deploy (Railway; Render fallback) with env-var table and verification checklist |

## What changes at production scale

MVP hosting is deliberately lightweight (Railway/Heroku-class: one container,
one process). The seams below are where the system grows without a rewrite:

- **Storage** — the JSONL audit store is an adapter with a read/append
  interface; swap it for **Postgres** (a `requests` table + an `audit_events`
  table) without touching routes or pipeline. This is the first real change
  to make.
- **Load** — intake is synchronous today (~1–5s per request, LLM-bound). A
  **queue** between the web layer and the pipeline absorbs storm-driven
  spikes — which is exactly when a restoration company's volume arrives — and
  makes retries/backpressure explicit.
- **Hosting** — container is ready; GCP-class (Cloud Run + Cloud SQL + a task
  queue) when volume justifies it. `INTAKE_BIND_HOST` exists for running
  behind a reverse proxy / published ports.
- **Auth & tenancy** — `/admin` supports an optional `ADMIN_TOKEN` gate
  (constant-time compare, HttpOnly cookie, token redacted from logs); unset
  means open, for localhost only. The cookie stores the raw token — fine for
  MVP over TLS; a signed cookie, then SSO behind a proxy, is the upgrade path.
  `POST /` has a per-IP token-bucket rate limit (in-memory, per-process — it
  resets on restart and isn't shared across replicas; revisit alongside the
  Postgres swap). Multi-tenancy = a company dimension on requests, audit
  events, and config (per-tenant thresholds and service areas).
- **Observability** — the eval metrics (accuracy, avg confidence, escalation
  rate, latency) become production dashboards; alert on escalation-rate and
  confidence drift, which is how prompt or model regressions surface.
- **Evals in CI** — ✅ real as of wave 3: the offline suite runs on every
  push, and the promotion gate re-runs the live suite on any prompt/config/
  eval change (see the promotion-flywheel section). The xfail mechanism lets
  known bugs ride in CI without masking new ones.

### Gaps ledger

Wave 1 shipped with three documented gaps. All three were closed by the
wave-2 product pass the same evening (records kept, nuclear-case style):

- ~~Rejected requests aren't persisted~~ — **closed 2026-07-08 (wave 2).**
  `ValidationError` now carries the finished trail (`received` →
  `validated[rejected]` → `rejected`) and a `to_dict()`; the web layer stores
  it, so `/admin` shows rejects like any other request.
- ~~No generated customer-facing reply~~ — **closed 2026-07-08 (wave 2).**
  The respond stage drafts a reply and passes it through a critic checklist
  (no cost promises, no guarantees, 911 opener on life-safety) with one
  revision, falling back to the dispatcher note + human review if the critic
  still refuses — the fallback was exercised live on the first run.
- ~~No life-safety urgency tier~~ — **closed 2026-07-08 (wave 2).** A
  deterministic screen ahead of classification sets `life_safety`, forces
  human review, and requires drafted replies to open with the 911 line;
  past-tense fire phrasing verifiably does not trip it.

Still open, deliberately (from the wave-2 handoffs):

- Both safety screens are **English-only** — the Spanish eval case covers
  classification, not the screens. Extend the term lists per-language when
  real traffic warrants.
- Reply *tone* is judged by an LLM critic; the only deterministic reply check
  is the 911 opener. No LLM-judge eval for reply quality yet — the suite
  asserts the deterministic invariants and reply-source counts only.
- Pricing questions escalate to a human by design (the critic refuses cost
  promises — see `evals/README.md`); a graceful decline-pricing line in the
  responder prompt, or a real quoting flow, is the follow-up.
- ~~The admin *list* view has no life-safety badge~~ — **closed 2026-07-08
  (wave 3, escalation lane):** unresolved life-safety escalations badge on
  the admin table and pin to the top of `/admin/queue`.
- Escalation acknowledge/resolve actions carry no user identity yet
  (deliberate at MVP — there are no identities until SSO lands; see
  `ROADMAP.md` §4, which makes actions attributable).
