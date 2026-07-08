"""Seed the admin view: five archetypal requests through the real pipeline.

Each result is persisted to the same audit store the web layer uses, so
http://localhost:8080/admin is fully populated the first time you open it.

    pip install -r requirements.txt     # the demo reuses the web layer's wiring
    python3 demo.py                     # live API: a few Haiku calls + one Opus
                                        # reread — fractions of a cent

The five cases, chosen to light up every path in the pipeline:

    1. canonical flood            one clean Haiku read, auto-routed
    2. nuclear-yard hazard        the fixed regression: resolved by the
                                  deterministic hazard screen, zero model calls
    3. ambiguous low-signal       low-confidence read -> Opus reread -> human
                                  review (the full escalation chain)
    4. too-short garbage          rejected by deterministic validation before
                                  any model call; persisted with status
                                  "rejected" so /admin shows rejects too
    5. active fire                classifies at high confidence AND trips the
                                  deterministic life-safety screen: flagged,
                                  forced human review, reply opens with 911

Append-only: running the demo twice adds five more rows, same as real traffic.
"""

import sys

try:
    import web  # noqa: F401 — wires settings, agent, and store at import time
except SystemExit as exc:
    raise SystemExit(
        f"demo startup failed: {exc}\n"
        "The demo hits the live API — create .env with ANTHROPIC_API_KEY=<your key> "
        "(see README quickstart)."
    )
except ImportError as exc:
    raise SystemExit(
        f"missing web-layer dependency ({exc.name}); run: pip install -r requirements.txt"
    )

from agent import ValidationError
from web import AGENT, STORE, WEB_PORT, make_record

CASES = [
    ("canonical flood", "web",
     "Hi, our basement flooded overnight after the storm and there's standing "
     "water everywhere. The carpet is soaked through and we're worried about "
     "mold. We're in Cedar Park and need someone out as soon as possible."),
    ("nuclear-yard hazard (fixed regression)", "email",
     "Nuclear material has spilled all over our yard and we don't know what "
     "to do. Is this something you can clean up?"),
    ("ambiguous low-signal", "phone",
     "Something feels wrong with our house lately and there is a strange "
     "smell in a couple of rooms. Can someone just come take a look?"),
    ("too-short garbage", "web", "help"),
    ("active fire", "phone",
     "Our house is actively on fire right now, there are flames coming out "
     "of the kitchen and smoke everywhere. What do we do??"),
]


def main() -> int:
    print(f"running {len(CASES)} requests through the live pipeline "
          f"(store: {STORE.path})\n")
    for label, channel, text in CASES:
        try:
            analysis = AGENT.handle(text, channel=channel)
        except ValidationError as exc:
            STORE.append(make_record(exc.to_dict()))
            print(f"  rejected      {exc.request_id}  ({label}): {exc}")
            continue
        record = make_record(analysis.to_dict())
        STORE.append(record)
        routing = "human review" if record["escalate_to_human"] else "auto-routed"
        print(f"  {record['request_type']:<18}{record['request_id']}  "
              f"conf={record['confidence']:.2f}  {routing}  ({label})")

    print(f"\n{STORE.count()} records in the store. Next:\n"
          f"    python3 web.py\n"
          f"    open http://localhost:{WEB_PORT}/admin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
