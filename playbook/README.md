# The parallel-agent build playbook

A repeatable motion for building software with **N coding agents working in
parallel git worktrees, coordinated by one human-driven orchestrator session**.
Extracted from a real build: a one-hour live session extended into a
production-shaped system in one evening by three parallel agents, then
hardened by a second wave. The repo this playbook ships in is the worked
example; [BRIEFS.md](BRIEFS.md) is the actual (sanitized) sprint document that
drove it.

## The core idea: coordination by construction

Parallel agents fail in two ways: they collide (edit the same file) or they
block (wait on each other). Both disappear if the seams are drawn so that
**no two sessions ever own the same file**:

- Git merges file-by-file, so disjoint file sets make merge conflicts
  *structurally impossible*, not merely unlikely.
- No agent waits on another. Anything an agent needs outside its lane goes in
  a handoff note; the orchestrator routes it. The agent moves on.
- The matrix forces clean interfaces. A lane that needs something from a file
  it doesn't own must consume that file's public surface (or adapt around it)
  instead of editing it — which is the discipline you wanted anyway.

The ownership matrix is the whole coordination mechanism. No locks, no
turn-taking, no shared task queue, no inter-agent chat.

## Drawing the matrix

1. Slice by **artifact**, not by feature: core logic · surfaces over it ·
   validation of it · packaging/infra around it. Features cut across files;
   artifacts *are* files.
2. Give every file exactly one owner. The orchestrator owns the connective
   tissue: top-level docs, merges, the gate, and anything two lanes would
   otherwise fight over.
3. Write down **cross-lane contracts** where lanes share a data shape, and
   make them directional: *upstream changes are strictly additive; downstream
   renders generically and hardcodes nothing.* Then either side can move
   without waiting on the other.
4. If two lanes need to edit one file, the matrix is wrong — split the file
   or merge the lanes.

## Wave structure

Run the build as short waves with a human review between each:

- **Wave 1 — build.** Parallel lanes build the system: core hardening,
  validation/evals, surfaces. Merge, gate, document.
- **Polish round — serial.** The orchestrator alone executes the review
  findings that are small and cross-cutting (a demo seeder, doc structure, a
  security fix). Not everything deserves a lane.
- **Wave 2 — remediation.** Review of the merged whole produces the next
  parallel wave: close product gaps, operational gaps, CI/deploy. Same rules.

Each wave ends at a **stop-gate**: the human reviews, decides, and explicitly
starts the next wave. Agents get a timebox and a hard rule: report done or cut
scope; a lane that isn't gate-green by the deadline is cut from the merge and
becomes a documented gap, not a delay.

## Briefs

Every agent gets the same shared context block plus one workstream section —
see [BRIEF-TEMPLATE.md](BRIEF-TEMPLATE.md). The parts that matter most:

- **The decided-things list** ("do not re-litigate these decisions") — locked
  design decisions with their rationale. Without it, every agent re-designs
  the system from scratch.
- **Ownership + hard rules** — own files only, commit small on your own
  branch, no secrets ever, the ship gate is sacred, the timebox.
- **Done criteria** — verifiable, not vibes: "both routes work locally
  against a real run", "selftest green including new cases".
- **A handoff file** (`HANDOFF-<lane>.md`, gitignored) — what was built, how
  to run it, gaps, and needs-outside-my-lane. Handoffs are where the good
  coordination happens: a downstream lane can *predict the contract* an
  upstream fix must meet, and the upstream lane can flag behavior shifts the
  downstream lane should expect — all without either touching the other's
  files.

## Merge order

Merge in **dependency direction**: logic first, then the validation that
proves it, then the surfaces that render it, then infra that packages it.
(In the worked example: hardening → evals → web, so the eval suite that
encodes the bug-fix contract lands on top of the fix it validates.)

Expect zero conflicts — that's what the matrix buys. Record any that appear;
they mean the matrix was violated or drawn wrong.

Small post-merge adjustments requested in handoffs ("flip this eval flag once
the fix lands", "gitignore my runtime file") are **fix-forwards**: the
orchestrator does them on main, attributed to the requesting lane. Anything
big goes back to the owning branch.

## The ship gate

Nothing publishes until, on the merged tree, in order of increasing cost:

1. Offline self-checks green (no network, no key — these run everywhere).
2. Live eval suites green — including the regression cases that encode every
   bug the build claims to have fixed.
3. Packaging: container builds and its default smoke command passes.
4. Surface smoke: every user-facing route exercised against a real run.
5. Secret sweep across **all history** (`git log --all --full-history -- .env`,
   `git grep` for key prefixes across `git rev-list --all`) — plus tracked-file
   and author-identity review if the repo is going public.

## What stays human

The matrix and the briefs (seam design is the leverage point), the
decided-things list, review of every handoff, merge order and merge decisions,
the gate verdicts, cut-scope calls at the timebox, secret/identity hygiene,
and the ship decision itself. The agents write the code; the human decides
what gets built and when it's done.

## Running it

```bash
# 1. baseline: clean repo, offline checks green, baseline commit
# 2. create the lanes (edit LANES in the script):
./playbook/setup-worktrees.sh
# 3. open one terminal per worktree, launch an agent session in each,
#    paste its brief (BRIEF-TEMPLATE.md), and let them run
# 4. merge in dependency order, run the gate, document, ship
```
