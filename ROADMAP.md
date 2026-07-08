# ROADMAP — deliberately not built yet

What the system needs next, written as **ready-to-run briefs** in the shape of
[playbook/BRIEF-TEMPLATE.md](playbook/BRIEF-TEMPLATE.md): each entry has a
mission, concrete deliverables, and verifiable done-criteria, so a future wave
can pick one up without re-deriving intent. Ordering matters — the event-store
spine unlocks the other three.

Shared context for every brief below: the decided-things list in
[ARCHITECTURE.md](ARCHITECTURE.md) still binds (staged workflow, code owns the
hard schema, auditability is a requirement, Haiku-default economics). Extend,
don't pivot.

---

## 1. Event-store spine — Postgres `audit_events` as system of record

> ⚠️ **Serial, design-first — NOT a parallel lane.** This cuts across every
> file boundary the parallel motion relies on (`agent.py`'s Analysis contract,
> the web store, eval fixtures, deploy config). No ownership matrix makes that
> conflict-free. Run it as: short design doc → review stop-gate → serial
> implementation behind the existing store seam.

**Mission:** the append-only audit trail becomes a Postgres `audit_events`
table — the system of record — with request summaries as a projection over
it; the JSONL file demotes to a zero-dependency dev fallback.

- Schema: `audit_events` (append-only: event id, request id, step name,
  recorded values as JSONB, stamps) + a `requests` projection (type,
  confidence, routing, status) maintained from events.
- A Postgres-backed store implementing the **existing `AuditStore` read/append
  interface** — the routes and templates must not change. Store selection via
  one env knob (`INTAKE_STORE=jsonl|postgres`).
- Backfill: a migration script that replays an existing `audit_log.jsonl`
  into the events table, idempotently.
- DEPLOY.md: managed-Postgres section (Railway plugin), replica note removed
  (`numReplicas` can exceed 1 once the store is shared).

**Done =** both eval suites and selftest green against both stores · `/admin`
renders byte-identically from either store on the seeded demo data · backfill
replays the demo JSONL and a re-run is a no-op · docs updated.

## 2. Multi-tenancy

**Mission:** one deployment serves many restoration companies, isolated.

- Tenant dimension on requests, audit events, and config; per-tenant
  confidence thresholds, service areas, and safety-term lists (the term
  lists are curated ops data, not code).
- Tenant-scoped admin views and stats; intake bound to a tenant by hostname
  or path prefix.
- Depends on the event-store spine (tenancy is a column + an index, not a
  fork of the JSONL file).

**Done =** two demo tenants run end-to-end with disjoint admin views, stats,
and thresholds; a request submitted to tenant A is unreachable from tenant
B's admin; suites green per tenant.

## 3. PII and retention

**Mission:** customer text is PII; treat it like it.

- Retention knobs: raw text redacts after N days (derived fields, hashes,
  and the decision trail survive); reply text follows the same clock.
- Right-to-forget: an admin deletion action by request id that redacts
  content and writes a `pii_deleted` audit event — the deletion itself is
  audited, the trail's shape is preserved.
- Log hygiene pass: raw customer text out of INFO-level logs; token-usage and
  decision logging unaffected.
- DEPLOY.md: data-handling section (what is stored where, for how long, how
  to purge).

**Done =** redaction job proven against the seeded store (raw text gone,
decisions intact, admin still renders) · deletion action leaves a tombstone
event · suites green with redaction enabled.

## 4. SSO and roles

**Mission:** `ADMIN_TOKEN` grows up — humans get identities, actions get
authors.

- OIDC behind the reverse proxy (the ADMIN_TOKEN cookie gate demotes to a
  documented break-glass path or is removed).
- Two roles to start: **dispatcher** (read admin, act on escalations) and
  **admin** (config, retention actions). Route-level checks; deny by default.
- Every human action (escalation resolution, deletion, config change) records
  *who* in its audit event — the prerequisite for the escalation workflow's
  actions being attributable.

**Done =** role checks enforced on every admin route (verified by an
unauthorized-role probe per route) · a fake-IdP end-to-end smoke passes ·
human actions carry identity in the trail · token path removed or explicitly
break-glass-documented.
