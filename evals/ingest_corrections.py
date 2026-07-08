#!/usr/bin/env python3
"""Ingest human-resolved escalations into candidate eval cases.

When a human resolves an escalated request and corrects the classifier's
read, that correction is exactly the labeled data the eval suite wants. This
script reads `escalation_resolved` events from an audit log (JSONL, one JSON
record per line — the web layer's append-only store) and turns every resolved
event that carries a corrected request type into a *candidate* case in
`evals/corrections.json`.

Candidates are a review queue, not eval cases:

  - HUMAN REVIEW REQUIRED. Nothing here is ever auto-merged into
    extended_cases.json (or any golden set). This script never writes that
    file — a human reviews a candidate, edits its `proposed_case`, and copies
    it in by hand. Corrections can be wrong too; the label of record is the
    reviewed one.
  - Re-runs are idempotent and additive: a candidate id already present in
    corrections.json is left exactly as the human last touched it (status
    edits included); only genuinely new resolutions are appended.

The escalation-workflow lane owns the `escalation_resolved` event shape and
it may still evolve, so consumption is deliberately generic: any JSON object
in the log (top-level record or nested step) whose `step`/`event`/`type` is
"escalation_resolved" counts as a resolved event, and the corrected type is
any correction-signaling key (corrected_type, corrected_request_type,
resolved_type, final_type, ... — nested objects are searched too) whose value
normalizes to a real request type. `request_type` alone never counts: that is
the model's original read, not a human correction. Events with no usable
corrected type are reported and skipped; ones missing the original request
text are queued as incomplete rather than silently dropped.

Usage:
    python3 evals/ingest_corrections.py                   # read ../audit_log.jsonl
    python3 evals/ingest_corrections.py --log PATH        # read a specific log
    python3 evals/ingest_corrections.py --out PATH        # write elsewhere (default evals/corrections.json)
    python3 evals/ingest_corrections.py --selftest        # offline check on a synthetic log, no files touched

Zero network calls, zero API spend: this is pure log parsing.
"""

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402  (path bootstrap above must run first)

log = logging.getLogger("evals.ingest")

DEFAULT_LOG_PATH = Path(__file__).resolve().parents[1] / "audit_log.jsonl"
DEFAULT_OUT_PATH = Path(__file__).resolve().parent / "corrections.json"
CASES_PATH = Path(__file__).resolve().parent / "extended_cases.json"

RESOLVED_MARKER = "escalation_resolved"

# Human-corrected labels must be real, classifiable types. UNKNOWN is the
# pipeline's own fail-safe value — "corrected to unknown" is not a label.
VALID_LABELS = frozenset(t.value for t in agent.CLASSIFIABLE_TYPES)

# Keys that signal "a human corrected the type" (exact, lowercase). Fuzzy
# fallback: any key containing "correct". `request_type` deliberately never
# matches — that's the model's original read, present on many events.
CORRECTED_TYPE_KEYS = frozenset({
    "corrected_type", "corrected_request_type", "correct_type", "correction",
    "resolved_type", "final_type", "human_type", "reviewed_type",
})
TEXT_KEYS = ("text", "raw_text", "request_text", "customer_text", "original_text")

OUTPUT_NOTE = (
    "Candidate eval cases from human-resolved escalations. HUMAN REVIEW REQUIRED — "
    "candidates are never auto-merged into extended_cases.json or any golden set. "
    "Review a candidate, edit its proposed_case, copy it into extended_cases.json by "
    "hand, and set its status here to 'merged' (or 'rejected'). Entries you have "
    "touched are never overwritten by re-runs of ingest_corrections.py."
)


# ---------------------------------------------------------------- log walking

def _walk(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict reachable inside `node` (depth-first), including it."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def iter_records(log_path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield (line_number, record) for each parseable JSON-object line.

    Junk lines are warned about and skipped — an audit log another process is
    appending to must never crash the ingest.
    """
    for line_num, raw in enumerate(log_path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("%s:%d: unparseable line skipped (%s)", log_path.name, line_num, exc)
            continue
        if isinstance(record, dict):
            yield line_num, record


def resolved_events(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every object in `record` marked as an escalation_resolved event."""
    for node in _walk(record):
        marker = node.get("step") or node.get("event") or node.get("type")
        if marker == RESOLVED_MARKER:
            yield node


def normalize_label(value: Any) -> "str | None":
    """'Mold Remediation' -> 'mold_remediation'; None if not a real type."""
    if not isinstance(value, str):
        return None
    label = value.strip().lower().replace(" ", "_").replace("-", "_")
    return label if label in VALID_LABELS else None


def find_corrected_type(event: dict[str, Any]) -> "tuple[str | None, str | None]":
    """(corrected_type, key_it_came_from) — searching nested objects too."""
    for node in _walk(event):
        for key, value in node.items():
            key_lc = str(key).lower()
            if key_lc == "request_type":  # the original model read, never a correction
                continue
            if key_lc in CORRECTED_TYPE_KEYS or "correct" in key_lc:
                label = normalize_label(value)
                if label is not None:
                    return label, str(key)
    return None, None


def find_text(event: dict[str, Any], record: dict[str, Any],
              text_index: "dict[str, str]", request_id: "str | None") -> "str | None":
    """Original request text: from the event, the enclosing record (its
    `received` audit step included), or any other record sharing the id."""
    for source in (event, record):
        for node in _walk(source):
            for key in TEXT_KEYS:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    if request_id is not None:
        return text_index.get(request_id)
    return None


def build_text_index(records: "list[dict[str, Any]]") -> dict[str, str]:
    """request_id -> raw request text, from every record that carries both."""
    index: dict[str, str] = {}
    for record in records:
        request_id = record.get("request_id")
        if not isinstance(request_id, str) or request_id in index:
            continue
        for node in _walk(record):
            for key in TEXT_KEYS:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    index[request_id] = value
                    break
            if request_id in index:
                break
    return index


# ------------------------------------------------------------- candidate building

def candidate_id(event: dict[str, Any], record: dict[str, Any]) -> "tuple[str, str | None]":
    """(stable candidate id, request_id if known). Falls back to a content
    hash so id-less events still dedupe across re-runs."""
    request_id = event.get("request_id") or record.get("request_id")
    if isinstance(request_id, str) and request_id:
        return f"correction-{request_id}", request_id
    digest = hashlib.sha256(
        json.dumps(event, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
    return f"correction-{digest}", None


def build_candidate(event: dict[str, Any], record: dict[str, Any],
                    text_index: "dict[str, str]", log_name: str,
                    now_iso: str) -> "dict[str, Any] | None":
    """One resolved event -> one candidate (or None when there is no usable
    corrected type — the caller reports those)."""
    corrected, corrected_key = find_corrected_type(event)
    if corrected is None:
        return None
    cand_id, request_id = candidate_id(event, record)
    text = find_text(event, record, text_index, request_id)

    original_read = normalize_label(event.get("request_type")) or \
        next((normalize_label(node.get("request_type")) for node in _walk(record)
              if normalize_label(node.get("request_type"))), None)

    candidate: dict[str, Any] = {
        "id": cand_id,
        "status": "pending_review" if text else "incomplete_missing_text",
        "corrected_type": corrected,
        "text": text,
        "source": {
            "request_id": request_id,
            "log": log_name,
            "corrected_type_key": corrected_key,
            "original_model_read": original_read,
            "resolved_at": event.get("resolved_at") or event.get("at"),
            "resolved_by": event.get("resolved_by") or event.get("resolver"),
            "ingested_at": now_iso,
        },
        "proposed_case": {
            "id": cand_id,
            "category": "correction",
            "why": (f"Human-resolved escalation: reviewer corrected the read to "
                    f"{corrected}. Ingested from {log_name}; REVIEW AND EDIT before "
                    "merging into extended_cases.json by hand."),
            "text": text,
            "expect": {"outcome": "analyzed", "types": [corrected]},
            "known_failing": False,
            "diagnosis": None,
        },
    }
    return candidate


# --------------------------------------------------------------------- ingest

def load_existing(out_path: Path) -> dict[str, Any]:
    if not out_path.exists():
        return {"_note": OUTPUT_NOTE, "candidates": []}
    data = json.loads(out_path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        raise ValueError(f"{out_path}: expected an object with a 'candidates' list")
    data["_note"] = OUTPUT_NOTE
    return data


def ingest(log_path: Path, out_path: Path) -> int:
    """Read the log, append new candidates to out_path. Returns exit code."""
    assert out_path.resolve() != CASES_PATH.resolve(), \
        "ingest must never write the golden cases file"
    if not log_path.exists():
        print(f"no audit log at {log_path} — nothing to ingest "
              "(pass --log PATH if it lives elsewhere)")
        return 0

    records = [record for _, record in iter_records(log_path)]
    text_index = build_text_index(records)
    now_iso = datetime.now(timezone.utc).isoformat()

    output = load_existing(out_path)
    known_ids = {c.get("id") for c in output["candidates"]}
    added, seen_resolved, unusable = [], 0, 0

    for record in records:
        for event in resolved_events(record):
            seen_resolved += 1
            candidate = build_candidate(event, record, text_index, log_path.name, now_iso)
            if candidate is None:
                unusable += 1
                log.warning("resolved event with no usable corrected type skipped "
                            "(request_id=%s)", event.get("request_id") or record.get("request_id"))
                continue
            if candidate["id"] in known_ids:  # human-owned once written; never overwrite
                continue
            known_ids.add(candidate["id"])
            added.append(candidate)

    output["candidates"].extend(added)
    output["generated_at"] = now_iso
    output["source_log"] = str(log_path)
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")

    incomplete = sum(1 for c in added if c["status"] == "incomplete_missing_text")
    print(f"escalation_resolved events seen: {seen_resolved}  "
          f"(no corrected type: {unusable})")
    print(f"new candidates written: {len(added)}  (incomplete, missing text: {incomplete})  "
          f"already queued: {seen_resolved - unusable - len(added)}")
    print(f"review queue: {out_path}  ({len(output['candidates'])} total; human review "
          "required — nothing is auto-merged)")
    return 0


# ------------------------------------------------------------------- selftest

def selftest() -> int:
    """Offline check on a synthetic log. No repo files are read or written."""
    import tempfile

    lines = [
        # 1: top-level event, everything inline, one plausible key spelling
        {"event": RESOLVED_MARKER, "request_id": "req_aaa",
         "corrected_request_type": "fire_smoke_damage", "resolved_by": "dispatcher_7",
         "resolved_at": "2026-07-08T20:00:00Z",
         "text": "The unit above ours had a small fire and now everything smells like smoke."},
        # 2: analysis record; the resolution is a nested audit step and the
        #    corrected type is nested one level deeper; text comes from the
        #    received step of the same record
        {"request_id": "req_bbb", "status": "analyzed", "request_type": "general_inquiry",
         "audit": [
             {"step": "received", "request": {"request_id": "req_bbb",
                                              "raw_text": "Something sharp-smelling spilled in our stairwell."}},
             {"step": RESOLVED_MARKER, "at": "2026-07-08T20:05:00Z",
              "resolution": {"corrected_type": "biohazard_cleanup", "request_type": "general_inquiry"}},
         ]},
        # 3: resolved event with no corrected type at all -> skipped
        {"event": RESOLVED_MARKER, "request_id": "req_ccc", "note": "resolved as-is"},
        # 4: 'corrected' value that is not a real type -> skipped
        {"event": RESOLVED_MARKER, "request_id": "req_ddd", "corrected_type": "flooded"},
        # 5: request_type alone must NOT count as a correction -> skipped
        {"event": RESOLVED_MARKER, "request_id": "req_ggg", "request_type": "water_damage",
         "text": "There is water in the basement."},
        # 6: corrected type present but no text anywhere -> queued as incomplete
        {"event": RESOLVED_MARKER, "request_id": "req_eee", "corrected_type": "water_damage"},
        # 7: denormalized label spelling normalizes
        {"event": RESOLVED_MARKER, "request_id": "req_fff", "correction": "Mold Remediation",
         "raw_text": "Musty smell in the crawlspace that never goes away."},
    ]

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "audit_log.jsonl"
        out_path = Path(tmp) / "corrections.json"
        content = "\n".join(json.dumps(line) for line in lines)
        content += "\nthis line is not json and must not crash the ingest\n"
        log_path.write_text(content)

        assert ingest(log_path, out_path) == 0
        queue = json.loads(out_path.read_text())
        by_id = {c["id"]: c for c in queue["candidates"]}
        assert set(by_id) == {"correction-req_aaa", "correction-req_bbb",
                              "correction-req_eee", "correction-req_fff"}, sorted(by_id)

        aaa = by_id["correction-req_aaa"]
        assert aaa["corrected_type"] == "fire_smoke_damage"
        assert aaa["status"] == "pending_review"
        assert aaa["text"].startswith("The unit above ours")
        assert aaa["source"]["resolved_by"] == "dispatcher_7"
        assert aaa["proposed_case"]["expect"] == {"outcome": "analyzed",
                                                  "types": ["fire_smoke_damage"]}

        bbb = by_id["correction-req_bbb"]
        assert bbb["corrected_type"] == "biohazard_cleanup", "nested resolution must be found"
        assert bbb["text"] == "Something sharp-smelling spilled in our stairwell.", \
            "text must come from the record's received step"
        assert bbb["source"]["original_model_read"] == "general_inquiry"

        assert by_id["correction-req_eee"]["status"] == "incomplete_missing_text"
        assert by_id["correction-req_eee"]["text"] is None
        assert by_id["correction-req_fff"]["corrected_type"] == "mold_remediation"
        assert "never auto-merged" in queue["_note"]
        for candidate in queue["candidates"]:
            assert candidate["proposed_case"]["category"] == "correction"
            assert candidate["proposed_case"]["expect"]["types"][0] in VALID_LABELS

        # idempotency: a re-run adds nothing and never touches human edits
        by_id["correction-req_aaa"]["status"] = "merged"  # a human reviewed it
        queue["candidates"] = list(by_id.values())
        out_path.write_text(json.dumps(queue))
        assert ingest(log_path, out_path) == 0
        again = {c["id"]: c for c in json.loads(out_path.read_text())["candidates"]}
        assert len(again) == 4, "re-run must not duplicate candidates"
        assert again["correction-req_aaa"]["status"] == "merged", \
            "re-run must never overwrite a human-touched candidate"

        # a missing log is a no-op, not an error (CI-safe)
        assert ingest(Path(tmp) / "no_such.jsonl", out_path) == 0

    print("ingest selftest: all checks passed (event discovery, nested corrections, "
          "label normalization, request_type never counts, incomplete queueing, "
          "idempotent re-runs, junk-line tolerance)")
    return 0


# ----------------------------------------------------------------- entrypoint

def main(argv: "list[str] | None" = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH,
                        help=f"audit log to read (JSONL; default {DEFAULT_LOG_PATH.name} at repo root)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH,
                        help="review-queue file to append to (default evals/corrections.json)")
    parser.add_argument("--selftest", action="store_true",
                        help="offline check against a synthetic log; touches no repo files")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    return ingest(args.log, args.out)


if __name__ == "__main__":
    sys.exit(main())
