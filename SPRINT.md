# SPRINT — Evening extension, built multi-agent

## Context — the original brief (read first; do not re-litigate these decisions)

**The original prompt, near-verbatim (from the live one-hour build session; transcription lightly corrected):**
> "I need to build some sort of AI work agent platform. Basically — this business, there's a customer who writes to the company and says, 'hey, my basement's flooded, the carpet's soaked, the drywall's wet, and I'm really just worried about mold.' And my company — what they do is kind of like restoration services. I need a platform to handle this request, analyze it — however you want to do it — and track it."

Confirmed read-back of the requirement: **"an AI work agent platform; the company does restoration services; handle the request, analyze it, end to end — and track it."**
Constraints given live: don't design for scale yet, but keep it healthy — "I want to build off it in the future" · **greenfield** ("we don't have anything right now") — the data layer is ours to own · input is **just the information that comes from the customer** (free text) · customer-facing UX = a simple web page; company-facing = "what does my team see?" → the admin view.

**The product:** an AI work-agent platform for a **restoration-services company** (water damage, fire/smoke, mold, biohazard cleanup). A customer writes in free text — e.g., *"my basement is flooded, the carpet is soaked, and I'm worried about mold"* — and the system **handles the request end to end: intake → analyze/classify → respond → track it.** Greenfield: no existing systems to integrate; the platform owns its own data layer over time.

**Design decisions locked in the original one-hour build (agent.py implements these — extend, don't pivot):**
- **Shape:** a staged **workflow**, not an open-ended agent — the task is bounded, with human escalation as the safety valve. No parallel-agent runtime needed inside the product.
- **Model economics:** **Haiku by default**; deterministic code over LLM wherever possible; code owns the hard schema (request ID, request-type enum), the LLM owns soft judgment (classification, response text). **Confidence via parity re-run**; low confidence → escalate to **Opus**; still low or API failure → **escalate to human**. Never crash on provider errors.
- **Auditability is a core requirement**, not a feature: every step records what happened and why (JSON audit trail) — "always able to point back to why decisions were made."
- **Build progression was staged with stop-gates:** 1 scaffolding → 2 deterministic/tools → 3 auditability → 4 LLM calls → 5 hardening (escalation, confidence, evals). `agent.py` sits at ~stage 5 with `--selftest` (offline, fake client) and `--evals` (12 labeled cases, live API) both green as of the last run. Header comments in `agent.py` track the stages — cross-check them.
- **Known live bug (deliberately left as a regression case):** hazard-material phrasing — *"nuclear material has spilled all over our yard"* — misclassifies instead of `biohazard_cleanup` + escalate. Workstream C fixes it; Workstream B encodes it as an eval.
- **UX intent from the original session:** customer-facing = dead-simple free-text intake page; company-facing = an **admin view over the audit trail**. Hosting philosophy: lightweight (Railway/Heroku-class) for MVP; Postgres/GCP-class at scale — that trade-off belongs in ARCHITECTURE.md.

**Mission tonight:** extend `agent.py` into a production-shaped system in one evening — built by **three Claude Code agents working in parallel git worktrees**, coordinated by a main orchestrator session. The method is part of the deliverable: the final repo documents exactly how the parallel build was run.

**Hard rules for every session:**
- **Stay in your lane.** File ownership matrix below is absolute — never edit a file owned by another agent. If you need something outside your lane, write the need into your `HANDOFF-<X>.md` and move on.
- Commit small, with clear messages, on your own branch only.
- **No secrets ever**: `.env` is gitignored and must never be committed or echoed into any file.
- This repo will be public. No personal/process context anywhere — it's a technical artifact, nothing else.
- **Ship gate:** `python3 agent.py --selftest` green AND the eval suites green. Nothing merges to main that breaks them.
- Timebox: agents report done (or cut scope) within ~2 hours.

## File ownership matrix

| Owner | Files |
|---|---|
| **Agent A** (web) | `web.py`, `templates/*`, `requirements.txt`, `HANDOFF-A.md` |
| **Agent B** (evals) | `evals/*`, `HANDOFF-B.md` |
| **Agent C** (hardening) | `agent.py`, `Dockerfile`, `.dockerignore`, `HANDOFF-C.md` |
| **Main** (orchestrator) | `.gitignore`, `README.md`, `ORCHESTRATION.md`, `ARCHITECTURE.md`, all merges |

---

## PHASE 0 — Orchestrator setup (main session, in this directory)

1. **Git hygiene first — check for a dirty history.** If a `.git` exists: check whether `.env` or any API key string was EVER committed (`git log --all --full-history -- .env`; `git grep -I "sk-" $(git rev-list --all) || true`). **If history contains secrets — or if there's no repo — delete `.git` and `git init` fresh.** A clean history is non-negotiable (repo goes public).
2. Write `.gitignore`: `.env`, `__pycache__/`, `*.pyc`, `.DS_Store`, `HANDOFF-*.md`.
3. Verify baseline: `python3 agent.py --selftest` (offline) must pass. Record the start timestamp (for ORCHESTRATION.md).
4. Baseline commit: `agent.py`, `.gitignore`, `SPRINT.md`.
5. Create the worktrees:
   git worktree add ../aa-web -b feature/web-admin
   git worktree add ../aa-evals -b feature/evals
   git worktree add ../aa-hardening -b feature/hardening
6. Tell the user to open three terminals and launch one Claude Code session per worktree, then STOP and wait. While waiting, draft skeletons of `ORCHESTRATION.md` and `ARCHITECTURE.md` (fill with real data in Phase 2).

---

## WORKSTREAM A — Admin web layer (worktree `../aa-web`, branch `feature/web-admin`)

Build a minimal web layer over the agent, matching the UX intent in the Context section. **Do not edit `agent.py`** — import from it; if its interface doesn't expose what you need, write an adapter inside `web.py` and note the gap in `HANDOFF-A.md`.

- `web.py` (FastAPI + uvicorn; add `requirements.txt`):
  - `/` — customer intake: free-text request form → runs the pipeline → renders result (request type, confidence, escalation status, response).
  - `/admin` — company-facing: table of processed requests read from the audit trail, newest first, escalated rows flagged; row click → full audit detail (every step, values, model calls, confidence, escalation path).
- Keep it clean and dependency-light. Server-rendered HTML is fine; no build tooling.
- Done = both routes work locally against a real run; commit; write `HANDOFF-A.md` (what you built, how to run, any gaps).

## WORKSTREAM B — Eval expansion (worktree `../aa-evals`, branch `feature/evals`)

Extend evaluation coverage. **Do not edit `agent.py`** — build alongside it.

- `evals/extended_cases.json` — ~12–15 labeled cases beyond the built-in suite. MUST include the **nuclear-waste regression case** ("nuclear material has spilled all over our yard" → expected `biohazard_cleanup`, escalate) plus: empty/garbage input, multi-hazard ("flooded basement AND smoke smell"), urgent-life-safety ("house is actively on fire"), ambiguous/low-signal, long rambling narrative, non-restoration request (polite decline expected).
- `evals/run_extended.py` — standalone runner importing from `agent.py`: per-case pass/fail, accuracy, avg confidence, escalation rate, latency. Cases that currently fail get marked `"known_failing": true` with a one-line diagnosis (Agent C is fixing classification in parallel — do not fix it yourself).
- `evals/README.md` — results table from a real run.
- Done = runner works live; commit; `HANDOFF-B.md` (results summary, known-failing list).

## WORKSTREAM C — Hardening + packaging (worktree `../aa-hardening`, branch `feature/hardening`)

You own `agent.py`.

- **Fix the known misclassification root cause** (see Context): hazard-material requests must classify as `biohazard_cleanup` and escalate. Fix at the right layer — deterministic hazard-term tier (nuclear/radioactive/chemical/sewage/asbestos/biohazard) ahead of the LLM call, or a sharpened classification prompt — whichever is more robust. Add matching selftest cases.
- Light resilience pass: API-call retry with backoff, clean failure → escalate-to-human path (never crash on provider errors).
- `Dockerfile` (python-slim) + `.dockerignore`: image runs `--selftest` by default; document `docker run` usage in comments. (Orchestrator may point CMD at the web layer after merge.)
- Done = selftest green including new cases, docker build succeeds; commit; `HANDOFF-C.md`.

---

## PHASE 2 — Merge, gate, document, publish (main session)

1. **Merge order: `feature/hardening` → `feature/evals` → `feature/web-admin`** (logic fix first, then the evals that validate it, then UI on top). Record every conflict (there should be ~none by seam design — that's the point of the matrix).
2. **Gate:** `--selftest` green · built-in evals green · `evals/run_extended.py` green (known-failing cases should now pass — verify the regression case specifically) · `docker build` + smoke run · web `/` and `/admin` manual smoke. Fix-forward small issues in the orchestrator; anything big goes back to the owning branch.
3. **`ORCHESTRATION.md`** — the writeup of THIS session, with real data: the ownership matrix and why the seams sit at file boundaries; the three agent briefs; per-agent wall-clock and commit counts (from git); merge order + rationale + actual conflicts; the gate; **what stayed human** (seam design, briefs, review, merge decisions, ship gate).
4. **`ARCHITECTURE.md`** — the system story, faithful to the Context section: intake → deterministic validation gate → LLM classification (Haiku) → confidence scoring via parity re-run → escalation chain (Haiku → Opus → human) → audit trail → admin surface. Then "what changes at production scale": Postgres for requests/audit, a queue for spikes, hosting (Railway-class MVP → GCP-class), multi-tenancy, observability, where evals sit in CI.
5. **`README.md`** rewrite: what it is (an end-to-end intake agent for a restoration-services company; started as a one-hour live build, extended in an evening by three parallel agents), quickstart (`--selftest` offline / `--evals` live / docker / web), pointers to the two docs.
6. Cleanup: `git rm SPRINT.md` (superseded by ORCHESTRATION.md), remove `HANDOFF-*` (gitignored anyway), remove worktrees.
7. **Publish:** final secret sweep (`git grep -I "sk-" $(git rev-list --all) || true`; confirm `.env` never tracked). Then `gh repo create restoration-intake-agent --public --source=. --push` (or manual remote if `gh` isn't authed). Report the URL + a 5-line summary.
