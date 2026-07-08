# Agent brief template

One brief per agent session. Every agent gets the **same** shared block
(sections 1–3) and exactly **one** workstream section (4). Keep the whole
brief short enough to paste into a session opener; link out for depth.

---

## 1. Context — do not re-litigate these decisions

> The product story in a paragraph: who it's for, what it does end to end.

**Decided (with the why — agents extend, they don't pivot):**
- <decision> — <one-line rationale>
- <decision> — <one-line rationale>
- **Known bugs being handled this wave:** <bug> — owned by workstream <X>;
  workstream <Y> encodes it as a regression case and does NOT fix it.

## 2. Hard rules (identical for every session)

- **Stay in your lane.** The ownership matrix is absolute — never edit a file
  owned by another session. Needs outside your lane go in `HANDOFF-<X>.md`
  (gitignored); then move on. The orchestrator routes them.
- Commit small, clear messages, on your own branch only.
- **No secrets ever.** `.env` is gitignored; never commit it or echo it into any file.
- **Ship gate:** <the offline check command> green and the eval suites green.
  Nothing merges that breaks them.
- Timebox: report done — or cut scope and say what was cut — within <N> hours.
  A lane that isn't gate-green by <deadline> is cut from the merge.

## 3. Ownership matrix

| Owner | Branch | Files (exclusive) |
|---|---|---|
| Agent A | `feature/<a>` | <files> |
| Agent B | `feature/<b>` | <files> |
| Agent C | `feature/<c>` | <files> |
| Orchestrator | `main` | top-level docs, `.gitignore`, all merges |

**Cross-lane contracts (directional, so nobody waits):**
- <upstream lane>'s <shared shape> changes are **strictly additive**.
- <downstream lane> consumes it **generically** — no hardcoded lists of the
  other lane's internals.

## 4. Your workstream — Agent <X>: <name> (branch `feature/<x>`)

> Mission in one sentence.

- <deliverable 1 — concrete, with the interface it must expose or consume>
- <deliverable 2>
- <what to do about things you find but don't own: document, don't fix>

**Done =** <verifiable criteria — commands that must pass, routes that must
work against a real run>. Then commit and write `HANDOFF-<X>.md`:
what you built · how to run it · gaps · needs-outside-my-lane · anything the
other lanes or the orchestrator must know (behavior shifts, interface
dependencies, post-merge steps you're requesting).
