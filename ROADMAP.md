# Roadmap

These are the things deliberately not built yet. Each entry follows
[playbook/BRIEF-TEMPLATE.md](playbook/BRIEF-TEMPLATE.md): a mission, concrete
deliverables, and done-criteria you can actually verify, so whoever picks one
up doesn't have to reconstruct the intent first. The order matters: the
event-store spine unlocks the other three.

One piece of shared context. The design decisions in
[ARCHITECTURE.md](ARCHITECTURE.md) still apply to all of these (staged
workflow, code owns the hard schema, everything audited, Haiku-default
economics). The briefs extend that design; none of them should pivot away
from it.

---

## 1. Event-store spine: Postgres `audit_events` as the system of record

> ⚠️ This one is serial and design-first, not a parallel lane. It cuts across
> every file boundary the parallel motion relies on (`agent.py`'s Analysis
> contract, the web store, eval fixtures, deploy config), and no ownership
> matrix makes that conflict-free. Run it as a short design doc, a review
> stop-gate, then a serial implementation behind the existing store seam.

**Mission:** make a Postgres `audit_events` table the system of record for
the append-only audit trail, with request summaries maintained as a
projection over it. The JSONL file drops back to being a zero-dependency dev
fallback.

- Schema: `audit_events` (append-only: event id, request id, step name,
  recorded values as JSONB, timestamps) plus a `requests` projection (type,
  confidence, routing, status) maintained from the events.
- A Postgres-backed store implementing the existing `AuditStore` read/append
  interface. The routes and templates must not change. Store selection is one
  env knob: `INTAKE_STORE=jsonl|postgres`.
- Backfill: a migration script that replays an existing `audit_log.jsonl`
  into the events table, idempotently.
- DEPLOY.md gets a managed-Postgres section (the Railway plugin) and loses
  the replica caveat, since `numReplicas` can exceed 1 once the store is
  shared.

**Done =** both eval suites and the selftest are green against both stores;
`/admin` renders byte-identically from either store on the seeded demo data;
the backfill replays the demo JSONL and a second run is a no-op; docs
updated.

## 2. Multi-tenancy

**Mission:** one deployment serves many restoration companies, isolated from
each other.

- A tenant dimension on requests, audit events, and config. Per-tenant
  confidence thresholds, service areas, and safety-term lists; the term lists
  are ops data someone curates, not code.
- Tenant-scoped admin views and stats. Intake binds to a tenant by hostname
  or path prefix.
- Depends on the event-store spine: tenancy should be a column and an index,
  not a fork of the JSONL file.

**Done =** two demo tenants run end-to-end with disjoint admin views, stats,
and thresholds; a request submitted to tenant A can't be reached from tenant
B's admin; the suites are green per tenant.

## 3. PII and retention

**Mission:** customer text is PII, so treat it that way.

- Retention knobs: raw text is redacted after N days, while the derived
  fields, hashes, and decision trail survive. Reply text follows the same
  clock.
- Right-to-forget: an admin deletion action by request id that redacts the
  content and writes a `pii_deleted` audit event. The deletion itself is
  audited, and the shape of the trail is preserved.
- A log hygiene pass: raw customer text comes out of INFO-level logs;
  token-usage and decision logging are unaffected.
- DEPLOY.md gets a data-handling section: what's stored where, for how long,
  and how to purge it.

**Done =** the redaction job is proven against the seeded store (raw text
gone, decisions intact, admin still renders); the deletion action leaves a
tombstone event; the suites are green with redaction enabled.

## 4. SSO and roles

**Mission:** replace `ADMIN_TOKEN` with real identities, so human actions
have authors.

- OIDC behind the reverse proxy. The ADMIN_TOKEN cookie gate either gets
  removed or stays as a documented break-glass path.
- Two roles to start: **dispatcher** (read the admin, act on escalations) and
  **admin** (config and retention actions). Checks at the route level, deny
  by default.
- Every human action — resolving an escalation, deleting PII, changing
  config — records who did it in its audit event. This is what makes the
  escalation workflow's actions attributable, which ARCHITECTURE.md lists as
  an open gap.

**Done =** role checks are enforced on every admin route, verified by an
unauthorized-role probe per route; a fake-IdP end-to-end smoke test passes;
human actions carry identity in the trail; the token path is removed or
explicitly documented as break-glass.
