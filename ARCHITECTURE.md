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
5  confidence gate (threshold 0.70)       model-reported confidence, clamped
      │  below → reread on Opus           by code; a low read gets one reread
      │  still below → human review       on the escalation model; still low
      │                                   or reread failure → human
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
8  admin surface                          /admin table + stats strip over the
                                          trail, escalated rows flagged;
                                          per-request step-by-step detail;
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

## Components

| Path | What it is |
|---|---|
| `agent.py` | The whole pipeline + config, offline `--selftest` (fake client), live `--evals`, stdlib dev server (`--serve`) |
| `web.py`, `templates/` | FastAPI layer: `/` customer intake (per-IP rate limit), `/admin` audit table + stats strip (optional `ADMIN_TOKEN` gate), `/admin/{id}` detail, `/health`. Owns persistence via an append-only JSONL audit store (the pipeline itself is stateless); renders new pipeline steps/fields generically — no hardcoded step list |
| `evals/` | 17-case extended suite + standalone runner (xfail semantics, offline `--check`) — see `evals/README.md` for live results |
| `Dockerfile` | python-slim, non-root; runs `--selftest` by default |
| `.github/workflows/ci.yml` | Offline CI on every push: selftest, eval `--check`, web import smoke — zero secrets |
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
- **Evals in CI** — `--selftest` on every commit (offline, free);
  `evals/run_extended.py` on merge and on any prompt/model change. The xfail
  mechanism lets known bugs ride in CI without masking new ones.

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
- The admin *list* view has no life-safety badge yet (the detail view flags
  it); cosmetic, one template change.
