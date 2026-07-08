"""Web layer over the intake agent: customer intake + company admin view.

Two server-rendered surfaces on one FastAPI app (templates/ holds the HTML):

    GET  /                customer-facing free-text intake form
    POST /                runs the pipeline, renders the analysis result
    GET  /admin           company-facing: all processed requests, newest first,
                          escalated rows flagged
    GET  /admin/{id}      full audit detail for one request: every step, values,
                          model attempts, confidence, escalation path
    GET  /health          JSON liveness + masked config summary

agent.py is imported, never modified. It returns each request's audit trail on
the Analysis object but persists nothing, so this module adds the missing
piece: AuditStore, an append-only JSONL file (audit_log.jsonl next to this
file; override with INTAKE_AUDIT_LOG). Reads scan the whole file — fine at MVP
volume; swap the store for Postgres without touching the routes when volume
arrives.

Usage:
    pip install -r requirements.txt
    python3 web.py                          # http://localhost:8080
    INTAKE_WEB_PORT=9000 python3 web.py     # pick another port
    uvicorn web:app --reload --port 8080    # dev auto-reload
"""

import json
import logging
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agent import (
    ConfigError,
    IntakeAgent,
    Settings,
    ValidationError,
    load_env,
)

log = logging.getLogger("intake.web")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_AUDIT_LOG = BASE_DIR / "audit_log.jsonl"
WEB_PORT = int(os.environ.get("INTAKE_WEB_PORT", "8080"))

# USD per 1M tokens (input, output) — platform.claude.com/docs/en/pricing,
# checked 2026-07-08. claude-sonnet-5 has introductory pricing ($2/$10) through
# 2026-08-31; the standard rate is hardcoded so estimates stay valid after it
# lapses. Models missing from this table are counted but reported as unpriced.
MODEL_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
}


# ------------------------------------------------------------------ audit store

class AuditStore:
    """Append-only JSONL persistence for processed requests.

    One JSON object per line, written after every pipeline run. Corrupt lines
    are skipped on read (with a warning) rather than poisoning the admin view.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[dict[str, Any]] = []
        for line_num, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("skipping corrupt audit line %s:%d", self.path, line_num)
        return records

    def list_newest_first(self) -> list[dict[str, Any]]:
        return list(reversed(self._read_all()))

    def get(self, request_id: str) -> "dict[str, Any] | None":
        return next((r for r in self._read_all() if r.get("request_id") == request_id), None)

    def count(self) -> int:
        return len(self._read_all())


# Result fields the web layer knows by name. Anything else the pipeline adds
# to Analysis.to_dict() (customer_reply, critic verdicts, ...) rides along in
# record["extras"] and renders generically — new fields need no changes here.
KNOWN_RESULT_FIELDS = frozenset({
    "request_id", "request_type", "confidence", "escalate_to_human", "notes", "audit",
})


def make_record(analysis_dict: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten an Analysis.to_dict() into the stored record shape.

    Summary fields the admin table needs live at the top level; the full audit
    trail rides along for the detail view; unknown result fields are preserved
    under 'extras'. Metadata comes from the trail's 'received' step
    defensively — if the pipeline's steps evolve, the record degrades to
    blanks instead of crashing the web layer.
    """
    received = next((s for s in analysis_dict.get("audit", []) if s.get("step") == "received"), {})
    request_meta = received.get("request", {})
    record = {
        "request_id": analysis_dict.get("request_id", ""),
        "received_at": request_meta.get("received_at", ""),
        "channel": request_meta.get("channel", ""),
        "raw_text": request_meta.get("raw_text", ""),
        "request_type": analysis_dict.get("request_type", "unknown"),
        "confidence": analysis_dict.get("confidence", 0.0),
        "escalate_to_human": analysis_dict.get("escalate_to_human", False),
        "notes": analysis_dict.get("notes", ""),
    }
    extras = {k: v for k, v in analysis_dict.items() if k not in KNOWN_RESULT_FIELDS}
    if extras:
        record["extras"] = extras
    record["audit"] = analysis_dict.get("audit", [])
    return record


# Steps whose names look safety-related get a visual flag in the detail view:
# a neutral marker when the screen ran clean, a loud one when it tripped
# (any truthy trigger-ish value among the step's recorded fields).
SAFETY_STEP_RE = re.compile(r"hazard|life[_-]?safety|urgen(t|cy)|emergency|911", re.IGNORECASE)
SAFETY_TRIGGER_KEYS = ("triggered", "matched_terms", "matched", "flagged", "detected", "indicators")


def step_safety_flag(name: str, recorded: Mapping[str, Any]) -> "str | None":
    """None for ordinary steps; 'clear' / 'tripped' for safety-screen steps."""
    if not SAFETY_STEP_RE.search(name):
        return None
    tripped = any(recorded.get(key) for key in SAFETY_TRIGGER_KEYS)
    return "tripped" if tripped else "clear"


def iter_usage(node: Any) -> "list[tuple[str | None, int, int]]":
    """Collect (model, input_tokens, output_tokens) for every usage blob
    anywhere in a record's audit trail.

    Walks the structure generically rather than naming steps, so token usage
    recorded by future pipeline stages (reply drafts, critic reviews, ...)
    is priced with no changes here. The model is whatever model-ish key sits
    beside the usage blob.
    """
    found: list[tuple[str | None, int, int]] = []
    if isinstance(node, dict):
        usage = node.get("usage")
        if isinstance(usage, dict) and ("input_tokens" in usage or "output_tokens" in usage):
            model = node.get("model") or node.get("escalation_model") or node.get("model_used")
            found.append((model,
                          int(usage.get("input_tokens") or 0),
                          int(usage.get("output_tokens") or 0)))
        for value in node.values():
            found.extend(iter_usage(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(iter_usage(item))
    return found


def request_cost(record: Mapping[str, Any]) -> dict[str, Any]:
    """Price one request's model calls against MODEL_PRICES_PER_MTOK."""
    usd = 0.0
    tokens_in = tokens_out = 0
    unpriced: set[str] = set()
    for model, tin, tout in iter_usage(record.get("audit", [])):
        tokens_in += tin
        tokens_out += tout
        prices = MODEL_PRICES_PER_MTOK.get(model or "")
        if prices is None:
            if model:
                unpriced.add(model)
            continue
        usd += tin * prices[0] / 1e6 + tout * prices[1] / 1e6
    return {"usd": usd, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "unpriced": unpriced}


def escalation_path(record: Mapping[str, Any]) -> str:
    """One-line, human-readable routing path reconstructed from the audit trail:
    which models were attempted, whether each succeeded, and where the request
    ended up (auto-accepted, resolved on reread, or human review)."""
    audit = record.get("audit", [])
    classified = next((s for s in audit if s.get("step") == "classified"), {})
    finalized = next((s for s in audit if s.get("step") == "finalized"), {})

    hops = [
        f"{attempt.get('model', '?')} {'failed' if 'error' in attempt else 'ok'}"
        for attempt in classified.get("attempts", [])
    ]

    action = finalized.get("action")
    reread_model = finalized.get("escalation_model", "escalation model")
    if action == "passed":
        hops.append("auto-accepted")
    elif action == "escalated_resolved":
        hops.extend([f"low confidence, reread on {reread_model} ok", "auto-accepted"])
    elif action == "escalated_to_human":
        hops.extend([f"low confidence, reread on {reread_model} still low", "human review"])
    elif action == "escalation_failed":
        hops.extend([f"low confidence, reread on {reread_model} failed", "human review"])
    elif record.get("escalate_to_human"):
        hops.append("human review")
    return "  →  ".join(hops)


# ------------------------------------------------------------------ app wiring

# Fail fast at import, same posture as agent.py: a missing/invalid environment
# should stop the server before it takes traffic. ConfigError's message names
# exactly what's missing.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)
load_env()
try:
    SETTINGS = Settings.from_env()
except ConfigError as exc:
    raise SystemExit(f"web.py startup failed: {exc}") from exc
AGENT = IntakeAgent(SETTINGS)  # constructs the SDK client; no network call yet
STORE = AuditStore(Path(os.environ.get("INTAKE_AUDIT_LOG", DEFAULT_AUDIT_LOG)))

app = FastAPI(title="Restoration Intake — Web", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _format_timestamp(value: Any) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM:SS UTC' (pipeline stamps are UTC)."""
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return str(value)


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _format_usd(value: Any) -> str:
    """Auto-compact dollars: $0 / $0.0142 (four decimals under $1) / $1.27."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if amount == 0:
        return "$0"
    if amount < 1:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


templates.env.filters["ts"] = _format_timestamp
templates.env.filters["pretty"] = _pretty_json
templates.env.filters["usd"] = _format_usd


# ---------------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse)
def intake_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html",
        {"active": "intake", "result": None, "error": None, "text": ""},
    )


@app.post("/", response_class=HTMLResponse)
def intake_submit(request: Request, text: str = Form("")) -> HTMLResponse:
    try:
        analysis = AGENT.handle(text, channel="web")
    except ValidationError as exc:
        # Bad input, rejected deterministically before any model call. The
        # partial trail is discarded inside handle(), so rejections don't
        # appear in /admin — gap noted in HANDOFF-A.md.
        return templates.TemplateResponse(
            request, "index.html",
            {"active": "intake", "result": None, "error": str(exc),
             "error_request_id": exc.request_id, "text": text},
            status_code=400,
        )
    except Exception:
        # Provider failures already degrade inside agent.py (fail-safe stub,
        # escalated); reaching here means a genuine bug. Log it, keep the
        # customer page intact.
        log.exception("pipeline failed unexpectedly")
        return templates.TemplateResponse(
            request, "index.html",
            {"active": "intake", "result": None,
             "error": "We hit an internal error and could not process this. "
                      "Please call us directly.",
             "text": text},
            status_code=500,
        )

    record = make_record(analysis.to_dict())
    STORE.append(record)
    return templates.TemplateResponse(
        request, "index.html",
        {"active": "intake", "result": record, "error": None, "text": ""},
    )


def build_stats(records: "list[dict[str, Any]]") -> dict[str, Any]:
    """Aggregate the stats strip from the audit store, attaching each record's
    cost as record['cost_usd'] for the per-row column on the way through."""
    total = len(records)
    escalated = sum(1 for r in records if r.get("escalate_to_human"))
    # A reread happened iff the finalized step carries token usage — the only
    # call whose usage is recorded there is the escalation-model reread.
    rereads = sum(
        1 for r in records
        if any(s.get("step") == "finalized" and "usage" in s for s in r.get("audit", []))
    )

    cost_total = 0.0
    tokens_in = tokens_out = 0
    unpriced: set[str] = set()
    type_counts: dict[str, int] = {}
    for record in records:
        cost = request_cost(record)
        record["cost_usd"] = cost["usd"]
        cost_total += cost["usd"]
        tokens_in += cost["tokens_in"]
        tokens_out += cost["tokens_out"]
        unpriced |= cost["unpriced"]
        label = str(record.get("request_type", "unknown")).replace("_", " ")
        type_counts[label] = type_counts.get(label, 0) + 1

    return {
        "total": total,
        "escalated": escalated,
        "escalation_rate": (escalated / total) if total else 0.0,
        "rereads": rereads,
        "avg_confidence": (sum(r.get("confidence", 0.0) for r in records) / total) if total else 0.0,
        "cost_total": cost_total,
        "cost_avg": (cost_total / total) if total else 0.0,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "unpriced": sorted(unpriced),
        "type_mix": sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0])),
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_list(request: Request) -> HTMLResponse:
    records = STORE.list_newest_first()
    return templates.TemplateResponse(
        request, "admin.html",
        {"active": "admin", "records": records, "stats": build_stats(records)},
    )


@app.get("/admin/{request_id}", response_class=HTMLResponse)
def admin_detail(request: Request, request_id: str) -> HTMLResponse:
    record = STORE.get(request_id)
    if record is None:
        return templates.TemplateResponse(
            request, "detail.html",
            {"active": "admin", "record": None, "request_id": request_id},
            status_code=404,
        )
    # Key must not collide with a dict method name (e.g. "values"): Jinja's
    # attribute lookup finds the method before the item and rendering breaks.
    steps = []
    for step in record.get("audit", []):
        name = step.get("step", "?")
        recorded = {k: v for k, v in step.items() if k not in ("step", "at")}
        steps.append({
            "name": name,
            "at": step.get("at", ""),
            "recorded": recorded,
            "safety": step_safety_flag(name, recorded),
        })
    return templates.TemplateResponse(
        request, "detail.html",
        {"active": "admin", "record": record, "request_id": request_id,
         "steps": steps, "path": escalation_path(record)},
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "config": SETTINGS.summary(), "stored_requests": STORE.count()}


# ------------------------------------------------------------------ entrypoint

def main() -> int:
    import uvicorn

    log.info("web -> http://localhost:%d  (admin at /admin)", WEB_PORT)
    uvicorn.run(app, host="127.0.0.1", port=WEB_PORT, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
