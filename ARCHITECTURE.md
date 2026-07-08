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
      │                                   a reference id, no LLM spend
      ▼
2  deterministic hazard screen            word-boundary term match (nuclear,
      │                                   radioactive, chemical, sewage,
      │  match ──────────────────────►    asbestos, hazmat, …) short-circuits:
      │                                   biohazard_cleanup, confidence 1.0,
      │                                   escalate_to_human — no model call
      ▼
3  LLM classification — Haiku             structured JSON against a closed
      │  failure/refusal → Sonnet         8-type enum; the model cannot invent
      │  transient errors → retry         types. Retry w/ exponential backoff
      │  total failure → fail-safe stub   + jitter, transient errors only
      ▼
4  confidence gate (threshold 0.70)       model-reported confidence, clamped
      │  below → reread on Opus           by code; a low read gets one reread
      │  still below → human review       on the escalation model; still low
      │                                   or reread failure → human
      ▼
5  audit trail                            JSON step records: received,
      │                                   hazard_screen, classified (attempts,
      │                                   token usage, retries, escalation
      │                                   path), finalized — every request,
      ▼                                   every decision, with the why
6  admin surface                          /admin table over the trail,
                                          escalated rows flagged; per-request
                                          step-by-step detail view
```

Provider failures never crash the pipeline; the worst case for any request is
`unknown` + escalate-to-human, with the failure chain recorded in the trail.

## Design principles (locked in the original build, kept tonight)

- **Code owns the hard schema; the LLM owns soft judgment.** Request IDs,
  the type enum, validation bounds, thresholds, escalation rules — code.
  Reading messy human text — the model.
- **Model economics:** Haiku by default; deterministic code instead of an LLM
  call wherever possible (the hazard screen answers in 0.0s for free); Opus
  only for low-confidence rereads. Cost scales with ambiguity, not volume.
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
| `web.py`, `templates/` | FastAPI layer: `/` customer intake, `/admin` audit table, `/admin/{id}` detail, `/health`. Owns persistence via an append-only JSONL audit store (the pipeline itself is stateless) |
| `evals/` | 15-case extended suite + standalone runner (xfail semantics, offline `--check`) — see `evals/README.md` for live results |
| `Dockerfile` | python-slim, non-root; runs `--selftest` by default |

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
- **Auth & tenancy** — `/admin` is unauthenticated localhost-MVP today; it
  needs SSO behind a proxy before real exposure. Multi-tenancy = a company
  dimension on requests, audit events, and config (per-tenant thresholds and
  service areas).
- **Observability** — the eval metrics (accuracy, avg confidence, escalation
  rate, latency) become production dashboards; alert on escalation-rate and
  confidence drift, which is how prompt or model regressions surface.
- **Evals in CI** — `--selftest` on every commit (offline, free);
  `evals/run_extended.py` on merge and on any prompt/model change. The xfail
  mechanism lets known bugs ride in CI without masking new ones.

### Known gaps, deliberately deferred (from the build's handoffs)

- Rejected requests aren't persisted to the trail (the pipeline raises before
  returning one) — audit-of-rejects needs a small `agent.py` change.
- No generated customer-facing reply yet — the "respond" stage renders the
  dispatcher note; a true reply is a new pipeline step with its own review
  gate.
- No life-safety urgency tier: an actively-burning-house request classifies
  correctly, but nothing says "call 911 first." Schema change, documented in
  `evals/README.md`.
