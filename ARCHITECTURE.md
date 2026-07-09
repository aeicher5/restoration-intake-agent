# Architecture

This is the intake agent for a restoration-services company: water damage, fire and
smoke, mold, storm, biohazard cleanup, reconstruction. A customer describes their
problem in free text ("my basement is flooded, the carpet is soaked, and I'm worried
about mold") and the system handles it end to end: take the request in, figure out
what it is, reply to the customer, and track it until a human closes it out. It's a
greenfield build, so the platform owns its own data layer.

The framing that matters most: this is a staged workflow, not an open-ended agent.
The task is bounded, every stage can be inspected on its own, and the safety valve
is always the same: escalate to a human.

## The pipeline

Requests arrive as free text from the web form, the CLI, or the JSON API, and move
through eight stages:

```
validate → hazard screen → life-safety screen → classify → confidence gate → respond → audit → admin
```

**1. Validation.** Plain code: length bounds and a channel whitelist. Bad input is
rejected with a reference id before anything hits an LLM, and the rejected trail is
persisted too, so rejects show up in the admin view like any other request.

**2. Hazard screen.** A word-boundary term match against words like nuclear,
radioactive, chemical, sewage, asbestos, hazmat. A hit short-circuits the rest of
the pipeline: the request is tagged `biohazard_cleanup` at confidence 1.0 and
escalated to a human. No model call is made.

**3. Life-safety screen.** Also deterministic, this time looking for
active-emergency phrasing: a fire burning right now, a gas leak, carbon monoxide,
mentions of 911. A hit sets a `life_safety` flag and forces human review, but the
pipeline keeps going so the request still gets classified. Past-tense damage ("we
had a fire last month") must not trip this, and there's a test proving it doesn't.

**4. Classification.** Haiku reads the text and returns structured JSON against a
closed enum of eight request types, so the model can't invent a category. Refusals
and hard failures fall over to Sonnet; transient errors are retried with
exponential backoff and jitter. If everything fails, the request gets a fail-safe
stub: type `unknown`, escalated to a human. A provider outage can't crash the
pipeline.

**5. Confidence gate.** The model reports a confidence score and code decides what
it means. The effective bar is max(0.70 global floor, per-class floor), judged
against whichever class the read actually reported; `biohazard_cleanup` and
`fire_smoke_damage` reads need 0.85. Below the bar, the request is reread once on
Opus. Still below, it goes to a human. The finalized step records which bar applied
and where it came from.

**6. Respond.** Haiku drafts a customer reply, and a second Haiku call reviews it
against a checklist: no cost promises, no guarantees, and life-safety replies must
open with the 911 line. (That opener is also checked deterministically in code, not
just by the critic.) If the critic rejects the draft, it gets one revision; if it
rejects again, we fall back to an internal dispatcher note plus human review. This
stage is skipped entirely on the deterministic-hazard and failed-classification
paths.

**7. Audit trail.** Every request writes JSON step records as it goes: `received`,
`hazard_screen`, `life_safety_screen`, `classified` (with attempts, retries, and
token usage), `reply_drafted` / `reply_reviewed` / `customer_reply`, `finalized`.
Every decision is in there, along with why it was made.

**8. Admin.** `/admin` is a table and stats strip rendered over the audit trail,
`/admin/queue` is where dispatchers work open escalations (more on that below), and
`/admin/{id}` shows the full trail for one request. All of it can sit behind an
optional `ADMIN_TOKEN`.

The worst case for any request, whatever breaks, is `unknown` plus
escalate-to-human, with the failure chain recorded in the trail. Both safety
screens are deterministic on purpose (see the design decisions below), and both
cost zero API calls.

## Design decisions

These were set early and held up through the later passes:

- Code owns the hard schema, the model owns the judgment. Request IDs, the type
  enum, validation bounds, thresholds, escalation rules: all code. The model does
  one thing, which is read messy human text.
- Cheap by default. Haiku for everything, deterministic code instead of a model
  call wherever possible (validation and both screens answer instantly, for free),
  Opus only for low-confidence rereads. A fully analyzed request is about three
  Haiku calls (classify, draft, critic; roughly 1.4k input / 130 output tokens,
  around $0.002 at list prices), and deterministic paths cost nothing. So cost
  grows with ambiguity rather than with volume.
- The confidence gate only protects against uncertainty. It catches "I'm not sure";
  it can't catch confidently wrong. That's why hazmat routing sits in the
  deterministic tier ahead of the model. If the screen over-triggers, someone
  spends a minute reviewing a request that didn't need it. If the model
  under-triggers, a crew walks into a hazmat scene.
- Everything is auditable. Each step records what happened and why, and the admin
  pages are nothing more than renderers over that record.

## Escalation workflow

"Escalate to a human" used to be a terminal flag. It's now a small workflow: open →
acknowledged → resolved. The state isn't stored in a column anywhere; it's derived
by folding workflow events over the same append-only store the pipeline writes.
Event lines share the file with request records, tagged `kind:
"escalation_event"`, and `AuditStore`'s request readers filter them out, so nothing
that reads requests had to change.

When an escalated record is stored, an `escalation_opened` event is appended with a
machine-derived reason (`life_safety`, `hazard_screen`, `low_confidence`, and so
on). Life-safety openings also fire a notification hook, `escalations.NOTIFIERS`.
The channel that ships is a structured log line; an SMS or pager callable registers
the same way.

`/admin/queue` is the dispatcher's view: unresolved escalations, life-safety pinned
to the top, acknowledge and resolve actions on each card. Resolving records the
human's decision into the same audit trail: confirmed or corrected, the final type,
and a note. A request's record now runs from `received`, through the model's
reasoning, to the human's disposition.

One nice property: escalated records with no events yet derive as implicitly open,
so stores written before this workflow existed need no migration.

## Prompt versioning and the eval gate

Everything the pipeline's judgment depends on (the three system prompts and the
confidence thresholds) lives in a single versioned artifact: `agent.PROMPT_CONFIG`,
currently `1.2.0`, with a content fingerprint. Every trail's `classified` step
records the version and fingerprint it ran under, so any request can be traced to
the exact artifact that read it, and `/health` reports the current one.

Changing the artifact is gated. `.github/workflows/promotion-gate.yml` triggers on
prompt, eval, and config paths; it always runs the offline checks, and it runs the
live extended suite whenever the repository has an API-key secret (it skips loudly
when it doesn't). The net effect is that prompts don't change unless the evals pass
on the change.

The loop closes from production. `evals/ingest_corrections.py` reads
`escalation_resolved` events where a dispatcher corrected the model's read and
turns each one into a candidate eval case in `evals/corrections.json`: the original
text, the corrected label, provenance, and a `proposed_case` ready to edit. Human
review is the gate by construction. The script refuses to write
`extended_cases.json`, re-runs never overwrite a candidate someone has already
reviewed, and a correction only enters the suite when a person copies it in. Once
it's in, the promotion gate holds every future prompt change to it.

## Components

| Path | What it is |
|---|---|
| `agent.py` | The whole pipeline and its config. Also carries an offline `--selftest` (fake client), a live `--evals` runner, and a stdlib dev server (`--serve`). |
| `web.py`, `templates/` | The FastAPI layer. `/` is customer intake with a per-IP rate limit, `/admin` the audit table and stats strip, `/admin/queue` the dispatcher queue, `/admin/{id}` per-request detail, `/health` version info. Admin views sit behind the optional `ADMIN_TOKEN`. This layer owns persistence (an append-only JSONL audit store; the pipeline itself is stateless) and renders new pipeline steps and fields generically, so there's no hardcoded step list to keep in sync. |
| `escalations.py` | The workflow state machine: event shapes, state folding, escalation reasons, the notification hook. Stdlib only, self-testing (`python3 escalations.py`). |
| `evals/` | The 19-case extended suite, a standalone runner with xfail semantics and an offline `--check`, and `ingest_corrections.py`. Details in `evals/README.md`. |
| `Dockerfile` | python-slim, non-root, runs `--selftest` by default. |
| `.github/workflows/` | `ci.yml` runs the offline suite on every push (selftests including escalations and ingest, the eval check, a web import check) with zero secrets. `promotion-gate.yml` runs the live suite on prompt and eval changes. |
| `railway.toml`, `runtime.txt`, `DEPLOY.md` | Config-as-code deploy for Railway (Render as fallback), with an env-var table and a verification checklist. |

## What changes at production scale

MVP hosting is deliberately small: one container, one process, Railway or Heroku
class. The seams below are where the system grows without a rewrite.

Storage first. The JSONL audit store sits behind a read/append interface, so
swapping it for Postgres (a `requests` table plus an `audit_events` table) touches
neither the routes nor the pipeline. This is the first real change to make.

Then load. Intake is synchronous today, roughly one to five seconds per request and
LLM-bound. A queue between the web layer and the pipeline absorbs spikes, which
matters more here than in most businesses, because a storm is exactly what
generates restoration volume. It also makes retries and backpressure explicit
instead of accidental.

Hosting is ready when the volume is: the container moves to Cloud Run + Cloud SQL +
a task queue without changes, and `INTAKE_BIND_HOST` already exists for running
behind a reverse proxy or published ports.

Auth and tenancy have a defined path. `ADMIN_TOKEN` is a constant-time compare with
an HttpOnly cookie, and the token is redacted from logs; unset means the admin is
open, which is only acceptable on localhost. The cookie stores the raw token, fine
for an MVP over TLS; the upgrade path is a signed cookie, then SSO behind a proxy.
The intake rate limit is a per-IP token bucket held in memory per process, so it
resets on restart and isn't shared across replicas; worth revisiting alongside the
Postgres swap. Multi-tenancy means a company dimension on requests, audit events,
and config, with per-tenant thresholds and service areas.

Observability falls out of the evals. The same metrics (accuracy, average
confidence, escalation rate, latency) become the production dashboards, and alerts
on escalation-rate and confidence drift are how a prompt or model regression would
actually show up.

Evals in CI are already real: the offline suite runs on every push, and the
promotion gate reruns the live suite on any prompt, config, or eval change. The
xfail mechanism lets known bugs ride in CI without masking new ones.

## Known gaps

The first version shipped with three documented gaps. All three were closed by the
product pass later the same day (2026-07-08):

- Rejected requests weren't persisted. Closed: `ValidationError` now carries the
  finished trail (`received` → `validated[rejected]` → `rejected`) and a
  `to_dict()`; the web layer stores it, and `/admin` shows rejects like any other
  request.
- There was no generated customer reply. Closed: the respond stage described above,
  draft plus critic with one revision and a dispatcher-note fallback. The fallback
  got exercised on the very first live run, so at least we know it works.
- There was no life-safety tier. Closed: the deterministic screen ahead of
  classification, forced human review, the required 911 opener, and a test showing
  past-tense fire phrasing doesn't trip it.

Also closed since then: the admin list view now badges unresolved life-safety
escalations, and the queue pins them to the top.

Still open, deliberately:

- Both safety screens are English-only. The Spanish eval case covers
  classification, not the screens; the term lists should grow per language once
  real traffic warrants it.
- Reply tone is judged only by the LLM critic; the one deterministic reply check is
  the 911 opener. There's no LLM-judge eval for reply quality yet, so the suite
  asserts the deterministic invariants and the reply-source counts, nothing more.
- Pricing questions escalate to a human by design, because the critic refuses cost
  promises (see `evals/README.md`). The follow-up is either a graceful
  decline-pricing line in the responder prompt or an actual quoting flow.
- Acknowledge and resolve actions don't record who performed them. Deliberate at
  MVP, since there are no user identities until SSO lands; `ROADMAP.md` §4 is where
  actions become attributable.
