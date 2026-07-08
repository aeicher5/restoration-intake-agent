"""Escalation workflow over the audit store: open → acknowledged → resolved.

Escalation used to end at a flag (`escalate_to_human=True`) — the trail stopped
exactly where the human began. This module turns the flag into a workflow:
every escalated request gets a state, and every human action is recorded the
same way the pipeline records its own steps.

No new storage. Workflow events are appended to the *same* append-only JSONL
store as request records, one JSON object per line, distinguished by
`"kind": "escalation_event"`:

    {"kind": "escalation_event", "event": "escalation_opened",
     "request_id": "req_…", "at": "<UTC ISO>",
     "reasons": ["life_safety", …], "life_safety": true}
    {"kind": "escalation_event", "event": "escalation_acknowledged",
     "request_id": "req_…", "at": "<UTC ISO>"}
    {"kind": "escalation_event", "event": "escalation_resolved",
     "request_id": "req_…", "at": "<UTC ISO>",
     "resolution": "confirmed" | "corrected",
     "request_type": "<human-decided type>", "original_type": "<pipeline type>",
     "note": "<free text, may be empty>"}

A request's workflow state is *derived* by folding its events in file order —
there is no separate state table, so the store remains a full replayable
history and the Postgres swap later maps these lines onto an events table
unchanged. Records that were escalated before this module existed (or seeded
by demo.py, which appends straight to the store) have no `escalation_opened`
line; they derive as implicitly open, with `opened_at` falling back to the
request's `received_at` — the queue works over an existing store with no
migration.

The store passed in must provide `append(mapping)` and `read_raw() -> list`
(every parsed line, oldest first) — web.py's AuditStore does. This module is
pure stdlib and imports neither web.py nor agent.py; run `python3
escalations.py` for the offline selftest (no network, no store file).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

log = logging.getLogger("intake.escalations")
notify_log = logging.getLogger("intake.notify")

EVENT_KIND = "escalation_event"
EVENT_OPENED = "escalation_opened"
EVENT_ACKNOWLEDGED = "escalation_acknowledged"
EVENT_RESOLVED = "escalation_resolved"
EVENT_TYPES = (EVENT_OPENED, EVENT_ACKNOWLEDGED, EVENT_RESOLVED)

STATE_OPEN = "open"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_RESOLVED = "resolved"

RESOLUTION_CONFIRMED = "confirmed"  # human kept the pipeline's type
RESOLUTION_CORRECTED = "corrected"  # human decided a different type

# Why a request escalated, derived deterministically from its audit trail.
# Closed vocabulary — declared in HANDOFF-E.md for downstream consumers.
REASON_LIFE_SAFETY = "life_safety"
REASON_HAZARD_SCREEN = "hazard_screen"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_REREAD_FAILED = "reread_failed"
REASON_REPLY_REVIEW_FAILED = "reply_review_failed"
REASON_UNSPECIFIED = "unspecified"


class WorkflowError(RuntimeError):
    """An action that the state machine refuses (already resolved, unknown
    request, …). `code` is a stable machine-readable name; the message is
    human-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def is_workflow_event(line: Mapping[str, Any]) -> bool:
    """True for escalation-event lines in the store; False for request records."""
    return line.get("kind") == EVENT_KIND


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trail_step(record: Mapping[str, Any], name: str) -> dict[str, Any]:
    return next((s for s in record.get("audit", []) if s.get("step") == name), {})


def record_life_safety(record: Mapping[str, Any]) -> bool:
    """The record's deterministic life-safety flag. Analysis.to_dict() puts it
    in extras; the finalized step carries it too — accept either, so the
    derivation survives record-shape drift."""
    extras = record.get("extras") or {}
    return bool(extras.get("life_safety") or _trail_step(record, "finalized").get("life_safety"))


def escalation_reasons(record: Mapping[str, Any]) -> "list[str]":
    """Why this record escalated, read off its own audit trail. A request can
    escalate for several reasons at once (life-safety phrasing AND a low-
    confidence read); all that apply are listed, strongest first."""
    reasons: list[str] = []
    if record_life_safety(record):
        reasons.append(REASON_LIFE_SAFETY)
    if _trail_step(record, "hazard_screen").get("triggered"):
        reasons.append(REASON_HAZARD_SCREEN)
    action = _trail_step(record, "finalized").get("action")
    if action == "escalated_to_human":
        reasons.append(REASON_LOW_CONFIDENCE)
    elif action == "escalation_failed":
        reasons.append(REASON_REREAD_FAILED)
    if _trail_step(record, "customer_reply").get("source") == "fallback_dispatcher_note":
        reasons.append(REASON_REPLY_REVIEW_FAILED)
    if not reasons:
        reasons.append(REASON_UNSPECIFIED)
    return reasons


# ------------------------------------------------------- life-safety notification

def _log_notifier(payload: Mapping[str, Any]) -> None:
    """Default channel: one structured WARNING line on the `intake.notify`
    logger — grep for LIFE_SAFETY_ESCALATION, or point a log shipper at it."""
    notify_log.warning("LIFE_SAFETY_ESCALATION %s",
                       json.dumps(payload, separators=(",", ":"), sort_keys=True))


# EXTENSION POINT — life-safety notification channels.
#
# A channel is `name: callable(payload_dict)`. On every life-safety escalation
# each channel fires once; the names that succeeded are recorded on the
# escalation_opened event as `notified`. One failing channel never blocks the
# others or the customer's request (failures are logged and skipped).
#
# To page a human for real, register a channel here — e.g.
#     NOTIFIERS["sms"] = lambda p: twilio_client.messages.create(...)
#     NOTIFIERS["pager"] = lambda p: pagerduty.enqueue(...)
# The payload is deliberately pager-sized: ids, type, matched terms, and a
# bounded snippet — never the full trail, never any secret.
NOTIFIERS: "dict[str, Callable[[Mapping[str, Any]], None]]" = {
    "log": _log_notifier,
}

_SNIPPET_CHARS = 160


def notify_life_safety(record: Mapping[str, Any],
                       opened_event: Mapping[str, Any]) -> "list[str]":
    """Fan a life-safety escalation out to every registered channel; returns
    the channel names that fired (for the opened event's `notified` field)."""
    text = str(record.get("raw_text", ""))
    payload = {
        "alert": "life_safety_escalation",
        "request_id": record.get("request_id", ""),
        "received_at": record.get("received_at", ""),
        "channel": record.get("channel", ""),
        "request_type": record.get("request_type", ""),
        "confidence": record.get("confidence", 0.0),
        "matched_terms": _trail_step(record, "life_safety_screen").get("matched_terms", []),
        "reasons": list(opened_event.get("reasons", [])),
        "text_snippet": text[:_SNIPPET_CHARS] + ("…" if len(text) > _SNIPPET_CHARS else ""),
        "queue": "/admin/queue",
    }
    fired: list[str] = []
    for name, notifier in NOTIFIERS.items():
        try:
            notifier(payload)
            fired.append(name)
        except Exception:
            log.exception("life-safety notifier %r failed for %s",
                          name, payload["request_id"])
    return fired


class EscalationWorkflow:
    """The state machine, stateless itself: every read folds the store's event
    lines over its request records, so state can never disagree with the trail."""

    def __init__(self, store: Any):
        self.store = store

    # ------------------------------------------------------------- reading

    def _scan(self) -> "tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]":
        """One pass over the store: request records and events, both by id,
        both in file (= chronological) order. Unknown-id events are kept —
        they still derive a state if the record line was lost."""
        records: dict[str, dict[str, Any]] = {}
        events: dict[str, list[dict[str, Any]]] = {}
        for line in self.store.read_raw():
            rid = line.get("request_id")
            if not rid:
                continue
            if is_workflow_event(line):
                events.setdefault(rid, []).append(line)
            else:
                records.setdefault(rid, line)
        return records, events

    @staticmethod
    def derive_state(record: Mapping[str, Any],
                     events: "list[dict[str, Any]]") -> "dict[str, Any] | None":
        """Fold one request's events into its current workflow state.

        None when the request was never escalated (nothing to work). An
        escalated record with no events is implicitly open — see module
        docstring. Out-of-order or duplicate events (crash replays, races)
        degrade to the furthest state reached rather than corrupting it.
        """
        if not events and not record.get("escalate_to_human"):
            return None
        state: dict[str, Any] = {
            "state": STATE_OPEN,
            "opened_at": record.get("received_at", ""),
            "implicit_open": True,
            "acknowledged_at": None,
            "resolved_at": None,
            "reasons": [],
            "notified": [],
            "resolution": None,
            "life_safety": record_life_safety(record),
        }
        for event in events:
            kind = event.get("event")
            at = event.get("at", "")
            if kind == EVENT_OPENED:
                state["opened_at"] = at
                state["implicit_open"] = False
                state["reasons"] = list(event.get("reasons", []))
                state["notified"] = list(event.get("notified", []))
            elif kind == EVENT_ACKNOWLEDGED and state["state"] == STATE_OPEN:
                state["state"] = STATE_ACKNOWLEDGED
                state["acknowledged_at"] = at
            elif kind == EVENT_RESOLVED and state["state"] != STATE_RESOLVED:
                state["state"] = STATE_RESOLVED
                state["resolved_at"] = at
                state["resolution"] = {
                    "resolution": event.get("resolution"),
                    "request_type": event.get("request_type"),
                    "original_type": event.get("original_type"),
                    "note": event.get("note", ""),
                }
        if not state["reasons"]:
            state["reasons"] = escalation_reasons(record)
        return state

    def state_of(self, request_id: str) -> "dict[str, Any] | None":
        """Derived state for one request; None if unknown or never escalated."""
        records, events = self._scan()
        record = records.get(request_id)
        if record is None:
            return None
        return self.derive_state(record, events.get(request_id, []))

    def states_by_request(self) -> "dict[str, dict[str, Any]]":
        """request_id -> derived state, for every escalated request. The admin
        table paints its workflow-state chips from this in one call."""
        records, events = self._scan()
        states: dict[str, dict[str, Any]] = {}
        for rid, record in records.items():
            state = self.derive_state(record, events.get(rid, []))
            if state is not None:
                states[rid] = state
        return states

    def queue_entries(self) -> "list[dict[str, Any]]":
        """The dispatcher work queue: every unresolved escalation as
        {"record": …, "wf": …} — life-safety first, then newest first."""
        records, events = self._scan()
        entries: list[dict[str, Any]] = []
        for rid, record in records.items():
            state = self.derive_state(record, events.get(rid, []))
            if state is None or state["state"] == STATE_RESOLVED:
                continue
            entries.append({"record": record, "wf": state})
        entries.sort(key=lambda e: (e["wf"]["life_safety"], e["wf"]["opened_at"]),
                     reverse=True)
        return entries

    # ------------------------------------------------------------- writing

    def open_for(self, record: Mapping[str, Any]) -> "dict[str, Any] | None":
        """Open the workflow for a just-stored escalated record (no-op with a
        warning if it isn't escalated — callers shouldn't gate, the state
        machine owns its own rules)."""
        if not record.get("escalate_to_human"):
            log.warning("open_for called on non-escalated %s; ignoring",
                        record.get("request_id"))
            return None
        event = {
            "kind": EVENT_KIND,
            "event": EVENT_OPENED,
            "request_id": record.get("request_id", ""),
            "at": _now_iso(),
            "reasons": escalation_reasons(record),
            "life_safety": record_life_safety(record),
        }
        if event["life_safety"]:
            # Notify before appending so the opened event records which
            # channels actually fired.
            event["notified"] = notify_life_safety(record, event)
        self.store.append(event)
        log.info("escalation opened for %s (%s)", event["request_id"],
                 ", ".join(event["reasons"]))
        return event

    def _guarded(self, request_id: str) -> "tuple[dict[str, Any], dict[str, Any]]":
        """Common ack/resolve preamble: the record and its current state, or a
        WorkflowError naming exactly why the action can't apply."""
        records, events = self._scan()
        record = records.get(request_id)
        if record is None:
            raise WorkflowError("not_found", f"no request {request_id!r} in the store")
        state = self.derive_state(record, events.get(request_id, []))
        if state is None:
            raise WorkflowError("not_escalated",
                                f"request {request_id!r} was never escalated")
        return record, state

    def acknowledge(self, request_id: str) -> dict[str, Any]:
        """open → acknowledged: a human has seen it and owns it."""
        _, state = self._guarded(request_id)
        if state["state"] == STATE_RESOLVED:
            raise WorkflowError("already_resolved",
                                f"escalation for {request_id!r} is already resolved")
        if state["state"] == STATE_ACKNOWLEDGED:
            raise WorkflowError("already_acknowledged",
                                f"escalation for {request_id!r} is already acknowledged")
        event = {
            "kind": EVENT_KIND,
            "event": EVENT_ACKNOWLEDGED,
            "request_id": request_id,
            "at": _now_iso(),
        }
        self.store.append(event)
        log.info("escalation acknowledged for %s", request_id)
        return event

    def resolve(self, request_id: str, decided_type: str, note: str = "") -> dict[str, Any]:
        """→ resolved: the human's decision, recorded in the trail.

        `decided_type` is what the human says the request actually is; equal to
        the pipeline's type it confirms the read, different it corrects it.
        Acknowledging first is not required — resolving straight from open is a
        legal (and common) dispatcher move. Type-vocabulary validation is the
        caller's job: web.py checks against the RequestType enum, keeping this
        module import-free of agent.py.
        """
        record, state = self._guarded(request_id)
        if state["state"] == STATE_RESOLVED:
            raise WorkflowError("already_resolved",
                                f"escalation for {request_id!r} is already resolved")
        original = str(record.get("request_type", "unknown"))
        event = {
            "kind": EVENT_KIND,
            "event": EVENT_RESOLVED,
            "request_id": request_id,
            "at": _now_iso(),
            "resolution": (RESOLUTION_CONFIRMED if decided_type == original
                           else RESOLUTION_CORRECTED),
            "request_type": decided_type,
            "original_type": original,
            "note": note.strip(),
        }
        self.store.append(event)
        log.info("escalation resolved for %s: %s %s", request_id,
                 event["resolution"], decided_type)
        return event


# -------------------------------------------------------------------- selftest

def selftest() -> None:
    """Offline check of the state machine: no store file, no network. The web
    layer's CI import smoke covers wiring; this covers the rules."""

    class MemStore:
        def __init__(self):
            self.lines: list[dict[str, Any]] = []

        def append(self, record: Mapping[str, Any]) -> None:
            # Round-trip through JSON, same as the real JSONL store.
            self.lines.append(json.loads(json.dumps(record)))

        def read_raw(self) -> "list[dict[str, Any]]":
            return list(self.lines)

    def rec(rid: str, escalate: bool, life_safety: bool = False,
            request_type: str = "water_damage", action: str = "passed",
            received_at: str = "2026-07-08T18:00:00+00:00") -> dict[str, Any]:
        return {
            "request_id": rid, "received_at": received_at,
            "request_type": request_type, "confidence": 0.9,
            "escalate_to_human": escalate, "notes": "n",
            "extras": {"life_safety": life_safety, "status": "analyzed"},
            "audit": [
                {"step": "life_safety_screen", "at": received_at,
                 "matched_terms": ["active_fire"] if life_safety else [],
                 "triggered": life_safety},
                {"step": "hazard_screen", "at": received_at, "triggered": False},
                {"step": "finalized", "at": received_at, "action": action,
                 "life_safety": life_safety},
            ],
        }

    store = MemStore()
    wf = EscalationWorkflow(store)

    # Not escalated: no state, no queue row, actions refuse.
    store.append(rec("req_auto", escalate=False))
    assert wf.state_of("req_auto") is None
    assert wf.queue_entries() == []
    try:
        wf.acknowledge("req_auto")
        raise AssertionError("ack of non-escalation must refuse")
    except WorkflowError as exc:
        assert exc.code == "not_escalated"
    try:
        wf.acknowledge("req_missing")
        raise AssertionError("ack of unknown id must refuse")
    except WorkflowError as exc:
        assert exc.code == "not_found"

    # Pre-workflow escalated record (demo.py-style): implicitly open, reasons
    # derived from its own trail, resolvable without ever being "opened".
    legacy = rec("req_legacy", escalate=True, action="escalated_to_human",
                 received_at="2026-07-08T17:00:00+00:00")
    store.append(legacy)
    state = wf.state_of("req_legacy")
    assert state["state"] == STATE_OPEN and state["implicit_open"]
    assert state["opened_at"] == "2026-07-08T17:00:00+00:00"
    assert state["reasons"] == [REASON_LOW_CONFIDENCE]

    # Explicitly opened escalation: open → acknowledged → resolved(corrected).
    # Opening a life-safety escalation fans out to every notifier; a broken
    # channel is skipped, never fatal, and only fired channels are recorded.
    fire = rec("req_fire", escalate=True, life_safety=True,
               request_type="fire_smoke_damage",
               received_at="2026-07-08T18:30:00+00:00")
    fire["raw_text"] = "Our kitchen is on fire right now!"
    store.append(fire)
    captured: list[Mapping[str, Any]] = []

    def _boom(payload: Mapping[str, Any]) -> None:
        raise RuntimeError("pager provider down")

    NOTIFIERS["capture"] = captured.append
    NOTIFIERS["broken"] = _boom
    try:
        opened = wf.open_for(fire)
    finally:
        del NOTIFIERS["capture"], NOTIFIERS["broken"]
    assert opened["reasons"][0] == REASON_LIFE_SAFETY and opened["life_safety"]
    assert opened["notified"] == ["log", "capture"], opened["notified"]
    assert captured[0]["request_id"] == "req_fire"
    assert captured[0]["matched_terms"] == ["active_fire"]
    assert "fire right now" in captured[0]["text_snippet"]
    state = wf.state_of("req_fire")
    assert state["state"] == STATE_OPEN and not state["implicit_open"]
    assert state["notified"] == ["log", "capture"]

    # Queue order: life-safety pinned above the (older) legacy row… and above
    # newer non-life-safety rows too.
    newer = rec("req_newer", escalate=True, action="escalated_to_human",
                received_at="2026-07-08T19:00:00+00:00")
    store.append(newer)
    order = [e["record"]["request_id"] for e in wf.queue_entries()]
    assert order == ["req_fire", "req_newer", "req_legacy"], order

    wf.acknowledge("req_fire")
    state = wf.state_of("req_fire")
    assert state["state"] == STATE_ACKNOWLEDGED and state["acknowledged_at"]
    try:
        wf.acknowledge("req_fire")
        raise AssertionError("double ack must refuse")
    except WorkflowError as exc:
        assert exc.code == "already_acknowledged"
    assert len(wf.queue_entries()) == 3  # acknowledged is still open work

    wf.resolve("req_fire", "water_damage", note="actually a burst pipe, no fire")
    state = wf.state_of("req_fire")
    assert state["state"] == STATE_RESOLVED
    assert state["resolution"] == {
        "resolution": RESOLUTION_CORRECTED, "request_type": "water_damage",
        "original_type": "fire_smoke_damage", "note": "actually a burst pipe, no fire",
    }
    assert [e["record"]["request_id"] for e in wf.queue_entries()] == ["req_newer", "req_legacy"]
    for action, code in ((lambda: wf.acknowledge("req_fire"), "already_resolved"),
                         (lambda: wf.resolve("req_fire", "water_damage"), "already_resolved")):
        try:
            action()
            raise AssertionError("action on resolved escalation must refuse")
        except WorkflowError as exc:
            assert exc.code == code

    # Resolving straight from open (no ack) confirms the pipeline's read.
    wf.resolve("req_legacy", "water_damage")
    state = wf.state_of("req_legacy")
    assert state["state"] == STATE_RESOLVED
    assert state["resolution"]["resolution"] == RESOLUTION_CONFIRMED
    assert state["resolution"]["note"] == ""

    # The store now interleaves records and events; record readers must be
    # able to tell them apart with is_workflow_event.
    kinds = [is_workflow_event(line) for line in store.read_raw()]
    assert kinds.count(True) == 4 and kinds.count(False) == 4, kinds

    print("escalations selftest: ok "
          f"({kinds.count(False)} records, {kinds.count(True)} events, "
          "open → acknowledged → resolved verified)")


if __name__ == "__main__":
    selftest()
