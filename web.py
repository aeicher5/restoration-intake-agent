"""Web layer over the intake agent: customer intake + company admin view.

Two server-rendered surfaces on one FastAPI app (templates/ holds the HTML):

    GET  /                customer-facing free-text intake form
    POST /                runs the pipeline, renders the analysis result
                          (per-IP token-bucket rate limit; 429 + Retry-After)
    GET  /admin           company-facing: all processed requests, newest first,
                          escalated rows flagged
    GET  /admin/{id}      full audit detail for one request: every step, values,
                          model attempts, confidence, escalation path
    GET  /health          JSON liveness + masked config summary

Both /admin views are open on localhost by default; set ADMIN_TOKEN to require
a token (?token=... once, then a cookie). The token value is never logged —
uvicorn's access line is redacted before it is emitted.

agent.py is imported, never modified. It returns each request's audit trail on
the Analysis object but persists nothing, so this module adds the missing
piece: AuditStore, an append-only JSONL file (audit_log.jsonl next to this
file; override with INTAKE_AUDIT_LOG). Reads scan the whole file — fine at MVP
volume; swap the store for Postgres without touching the routes when volume
arrives.

Usage:
    pip install -r requirements.txt
    python3 web.py                          # http://localhost:8080
    PORT=9000 python3 web.py                # pick another port (INTAKE_WEB_PORT
                                            # also honored; PORT wins)
    ADMIN_TOKEN=... python3 web.py          # gate /admin views behind the token
    uvicorn web:app --reload --port 8080    # dev auto-reload
"""

import hmac
import json
import logging
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from agent import (
    ConfigError,
    IntakeAgent,
    RequestType,
    Settings,
    ValidationError,
    load_env,
)
from escalations import EscalationWorkflow, WorkflowError, is_workflow_event

log = logging.getLogger("intake.web")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_AUDIT_LOG = BASE_DIR / "audit_log.jsonl"
# PORT is the deploy-platform convention and wins; INTAKE_WEB_PORT remains as
# the documented local override from earlier revisions.
WEB_PORT = int(os.environ.get("PORT", os.environ.get("INTAKE_WEB_PORT", "8080")))

# Unset (the default) leaves /admin open — the original localhost posture.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
ADMIN_TOKEN_COOKIE = "intake_admin_token"

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

    The same file also carries escalation-workflow event lines (see
    escalations.py), appended through the same interface. The record-reading
    methods below filter them out; read_raw() exposes every line for the
    workflow to fold.
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

    def read_raw(self) -> list[dict[str, Any]]:
        """Every stored line, oldest first — request records and workflow
        events alike. The escalation workflow derives state from this."""
        return self._read_all()

    def _records(self) -> list[dict[str, Any]]:
        return [line for line in self._read_all() if not is_workflow_event(line)]

    def list_newest_first(self) -> list[dict[str, Any]]:
        return list(reversed(self._records()))

    def get(self, request_id: str) -> "dict[str, Any] | None":
        return next((r for r in self._records() if r.get("request_id") == request_id), None)

    def count(self) -> int:
        return len(self._records())


# ----------------------------------------------------------------- rate limit

class RateLimiter:
    """Per-key token bucket: `burst` requests immediately, refilled at
    `per_minute`/60 tokens a second. In-memory and per-process — resets on
    restart, not shared across replicas; swap for a shared store alongside
    the audit store when the deployment grows past one process.
    """

    MAX_KEYS = 4096  # prune idle buckets past this; bounds memory under churn

    def __init__(self, burst: int, per_minute: float):
        self.burst = float(burst)
        self.refill_per_sec = per_minute / 60.0
        self._buckets: "dict[str, tuple[float, float]]" = {}  # key -> (tokens, stamp)
        self._lock = threading.Lock()

    def try_acquire(self, key: str) -> float:
        """0.0 when a token was taken; otherwise seconds until one refills."""
        now = time.monotonic()
        with self._lock:
            tokens, stamp = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - stamp) * self.refill_per_sec)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                if len(self._buckets) > self.MAX_KEYS:
                    self._prune(now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / self.refill_per_sec

    def _prune(self, now: float) -> None:
        """Drop buckets that have refilled to full — indistinguishable from
        never having been seen. Called with the lock held."""
        full = [
            key for key, (tokens, stamp) in self._buckets.items()
            if tokens + (now - stamp) * self.refill_per_sec >= self.burst
        ]
        for key in full:
            del self._buckets[key]


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


# A nested structure whose pretty JSON fits in this many characters starts
# expanded in the detail view; anything bigger starts collapsed behind its
# summary line. Purely presentational.
FIELD_OPEN_JSON_LIMIT = 320


def classify_field(key: str, value: Any) -> dict[str, Any]:
    """Render hint for one recorded audit-step field — decided by the value's
    shape, never its name, so steps future pipeline stages add render with no
    changes here (the generic-rendering contract).

    Strings show verbatim (multiline ones keep their line breaks); numbers,
    booleans and null show JSON-exact; nested dicts/lists show as pretty JSON
    behind a <details> that starts open only when small.
    """
    if isinstance(value, str):
        return {"key": key, "kind": "multiline" if "\n" in value else "text",
                "value": value}
    if isinstance(value, (dict, list)):
        pretty = _pretty_json(value)
        count = len(value)
        unit = ("key" if isinstance(value, dict) else "item") + ("" if count == 1 else "s")
        summary = f"{count} {unit}"
        if isinstance(value, dict) and value:
            preview = ", ".join(list(value)[:3]) + (", …" if count > 3 else "")
            summary += f" — {preview}"
        return {"key": key, "kind": "json", "value": pretty,
                "open": len(pretty) <= FIELD_OPEN_JSON_LIMIT, "summary": summary}
    return {"key": key, "kind": "code", "value": json.dumps(value)}


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


def escalation_path(record: Mapping[str, Any]) -> "list[str]":
    """Human-readable routing hops reconstructed from the audit trail: which
    models were attempted, whether each succeeded, and where the request ended
    up (auto-accepted, resolved on reread, or human review). The detail view
    renders the hops as a chip chain."""
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
    return hops


# ------------------------------------------------------------------ app wiring

# Fail fast at import, same posture as agent.py: a missing/invalid environment
# should stop the server before it takes traffic. ConfigError's message names
# exactly what's missing.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)


class _TokenRedactingFilter(logging.Filter):
    """uvicorn's access line includes the query string, which would leak
    ?token= values into logs. Redact them before the record is emitted.
    Attached at logger level so it survives uvicorn's dictConfig (which
    replaces handlers but leaves logger filters alone)."""

    _TOKEN_RE = re.compile(r"(token=)[^&\s\"']*")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._TOKEN_RE.sub(r"\1[redacted]", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._TOKEN_RE.sub(r"\1[redacted]", record.msg)
        return True


for _logger_name in ("uvicorn.access", "uvicorn.error", "intake.web"):
    logging.getLogger(_logger_name).addFilter(_TokenRedactingFilter())

load_env()
try:
    SETTINGS = Settings.from_env()
except ConfigError as exc:
    raise SystemExit(f"web.py startup failed: {exc}") from exc
AGENT = IntakeAgent(SETTINGS)  # constructs the SDK client; no network call yet
STORE = AuditStore(Path(os.environ.get("INTAKE_AUDIT_LOG", DEFAULT_AUDIT_LOG)))
WORKFLOW = EscalationWorkflow(STORE)  # escalation state machine over the same store

# POST / triggers a model pipeline run, so it is the endpoint worth guarding.
# Defaults are generous for a human, tight for a script; env-tunable for ops.
RATE_LIMITER = RateLimiter(
    burst=int(os.environ.get("INTAKE_RATE_BURST", "5")),
    per_minute=float(os.environ.get("INTAKE_RATE_PER_MINUTE", "6")),
)

app = FastAPI(title="Restoration Intake — Web", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _format_timestamp(value: Any) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM:SS UTC' (pipeline stamps are UTC)."""
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return str(value)


def _relative_age(value: Any) -> str:
    """ISO timestamp -> compact age: 'just now', '4m ago', '3h ago', '2d ago'.

    Server-rendered, so it ages until the next page load — render it next to
    the absolute stamp (e.g. in a title attribute), never instead of it.
    """
    try:
        then = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    now = datetime.now(then.tzinfo or timezone.utc)
    if then.tzinfo is None:  # pipeline stamps are UTC; treat naive ones as such
        then = then.replace(tzinfo=timezone.utc)
    seconds = max(0.0, (now - then).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 48 * 3600:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


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


def _confidence_pct(value: Any) -> int:
    """Confidence 0..1 -> integer percent clamped to [0, 100], for bar widths."""
    try:
        return max(0, min(100, round(float(value) * 100)))
    except (TypeError, ValueError):
        return 0


templates.env.filters["ts"] = _format_timestamp
templates.env.filters["age"] = _relative_age
templates.env.filters["pretty"] = _pretty_json
templates.env.filters["usd"] = _format_usd
templates.env.filters["confpct"] = _confidence_pct


# ------------------------------------------------------------------ admin auth

_ADMIN_DENIED_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Admin — token required</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' \
viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%2315171E'/%3E%3Cpath \
d='M16 6C16 6 9 14.5 9 19a7 7 0 0 0 14 0C23 14.5 16 6 16 6Z' fill='%23E4610A'/%3E%3C/svg%3E">
<style>body{font-family:"IBM Plex Sans",-apple-system,"Segoe UI",sans-serif;
background:#FAF9F6;color:#23262E;display:grid;place-items:center;min-height:100vh;margin:0}
.card{background:#fff;border:1px solid #E7E4DE;border-radius:8px;padding:1.5rem 2rem;
max-width:26rem}h1{font-size:1.05rem;margin:0 0 .4rem;color:#15171E}
p{margin:.3rem 0;color:#5B6472;font-size:.9rem}
code{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;background:#F1EFEA;
padding:.1rem .3rem;border-radius:4px}</style></head>
<body><div class="card"><h1>Admin access requires a token</h1>
<p>This deployment gates the admin views. Open
<code>/admin?token=&lt;ADMIN_TOKEN&gt;</code> once — a cookie keeps you signed
in after that.</p></div></body></html>"""


def _admin_guard(request: Request) -> "Response | None":
    """None = allowed. With ADMIN_TOKEN set, a correct ?token= sets the auth
    cookie and 303-redirects to the same path so the token drops out of the
    URL (and browser history); after that the cookie alone authorizes. A
    wrong or missing credential gets the 403 page. Unset ADMIN_TOKEN keeps
    the open-localhost default. Comparisons are constant-time; the token
    value itself is never logged (see _TokenRedactingFilter)."""
    if not ADMIN_TOKEN:
        return None
    supplied = request.query_params.get("token")
    if supplied is not None:
        if hmac.compare_digest(supplied, ADMIN_TOKEN):
            response = RedirectResponse(request.url.path, status_code=303)
            response.set_cookie(ADMIN_TOKEN_COOKIE, ADMIN_TOKEN, httponly=True,
                                samesite="lax", path="/admin")
            return response
    elif hmac.compare_digest(request.cookies.get(ADMIN_TOKEN_COOKIE, ""), ADMIN_TOKEN):
        return None
    return HTMLResponse(_ADMIN_DENIED_HTML, status_code=403)


# ---------------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse)
def intake_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html",
        {"active": "intake", "result": None, "error": None, "text": ""},
    )


@app.post("/", response_class=HTMLResponse)
def intake_submit(request: Request, text: str = Form("")) -> HTMLResponse:
    client_ip = request.client.host if request.client else "unknown"
    wait = RATE_LIMITER.try_acquire(client_ip)
    if wait > 0:
        retry_after = max(1, math.ceil(wait))
        return templates.TemplateResponse(
            request, "index.html",
            {"active": "intake", "result": None,
             "error": "You're submitting faster than we can take requests in. "
                      f"Please wait about {retry_after} seconds and try again.",
             "text": text},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    try:
        analysis = AGENT.handle(text, channel="web")
    except ValidationError as exc:
        # Bad input, rejected deterministically before any model call. The
        # finished trail rides on the exception (received → validated →
        # rejected), so rejects reach /admin like any other request.
        STORE.append(make_record(exc.to_dict()))
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
    if record.get("escalate_to_human"):
        # A failure here must not cost the customer their confirmation page:
        # an escalated record with no opened event derives as implicitly open,
        # so the queue still picks it up.
        try:
            WORKFLOW.open_for(record)
        except Exception:
            log.exception("failed to open escalation for %s", record.get("request_id"))
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
        # Raw type values (not display labels): the admin template linkifies
        # each chip into a ?type= filter, so the value must round-trip.
        raw_type = str(record.get("request_type", "unknown"))
        type_counts[raw_type] = type_counts.get(raw_type, 0) + 1

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


# Coarse routing buckets behind the /admin filter bar — the same three ways a
# request can leave the pipeline that the table's routing column shows, plus
# "unresolved", which narrows "human" to escalations a dispatcher still owes
# an answer. (value, label) pairs; the empty value passes everything.
ROUTING_FILTERS = [
    ("", "All routing"),
    ("human", "Human review"),
    ("unresolved", "Human review — unresolved"),
    ("auto", "Auto-routed"),
    ("rejected", "Rejected"),
]


def record_routing(record: Mapping[str, Any]) -> str:
    """The bucket a record renders under in the admin table's routing column."""
    if record.get("extras", {}).get("status") == "rejected":
        return "rejected"
    if record.get("escalate_to_human"):
        return "human"
    return "auto"


def filter_records(records: "list[dict[str, Any]]",
                   wf_states: "Mapping[str, dict[str, Any]]",
                   type_: str = "", routing: str = "", q: str = "") -> "list[dict[str, Any]]":
    """Apply the /admin filter-bar selection. Empty values pass everything;
    the text search is a case-insensitive substring over request id, customer
    text, and the dispatcher note."""
    needle = q.strip().lower()
    shown = []
    for record in records:
        if type_ and str(record.get("request_type", "unknown")) != type_:
            continue
        if routing:
            bucket = record_routing(record)
            if routing == "unresolved":
                # An escalated record with no workflow events derives as
                # implicitly open (see intake_submit), so a missing state
                # still counts as unresolved.
                wf = wf_states.get(record.get("request_id"))
                if bucket != "human" or (wf is not None and wf["state"] == "resolved"):
                    continue
            elif bucket != routing:
                continue
        if needle:
            hay = " ".join((
                str(record.get("request_id", "")),
                str(record.get("raw_text", "")),
                str(record.get("notes", "")),
            )).lower()
            if needle not in hay:
                continue
        shown.append(record)
    return shown


@app.get("/admin", response_class=HTMLResponse)
def admin_list(request: Request) -> Response:
    denied = _admin_guard(request)
    if denied is not None:
        return denied
    records = STORE.list_newest_first()
    wf_states = WORKFLOW.states_by_request()
    unresolved = [s for s in wf_states.values() if s["state"] != "resolved"]
    queue_counts = {
        "unresolved": len(unresolved),
        "life_safety": sum(1 for s in unresolved if s["life_safety"]),
    }
    stats = build_stats(records)  # also stamps cost_usd on every record for the table
    params = request.query_params
    filters = {
        "type": params.get("type", ""),
        # Unknown routing codes are dropped rather than reflected.
        "routing": params.get("routing", "") if any(
            params.get("routing", "") == value for value, _ in ROUTING_FILTERS) else "",
        "q": params.get("q", "").strip(),
    }
    filters["active"] = any(filters.values())
    type_options = sorted(
        {str(r.get("request_type", "unknown")) for r in records}
        | ({filters["type"]} if filters["type"] else set())  # keep an unmatched selection visible
    )
    shown = filter_records(records, wf_states,
                           filters["type"], filters["routing"], filters["q"])
    return templates.TemplateResponse(
        request, "admin.html",
        {"active": "admin", "records": shown, "total": len(records),
         "stats": stats, "filters": filters, "type_options": type_options,
         "routing_options": ROUTING_FILTERS,
         "wf_states": wf_states, "queue_counts": queue_counts,
         # nav badge: only admin pages hand these to the template — the
         # customer intake page never sees queue numbers.
         "nav_queue": queue_counts["unresolved"],
         "nav_queue_danger": queue_counts["life_safety"] > 0},
    )


# The human's decision vocabulary when resolving: the same closed enum the
# classifier picks from, minus the pre-classification sentinel — a dispatcher
# resolving a request must land it on a real type.
RESOLVABLE_TYPES = [t.value for t in RequestType if t is not RequestType.UNKNOWN]

# Post-action feedback rendered on the queue page. Actions redirect with
# ?notice=<code> (never free text — codes only, so nothing user-controlled is
# reflected); unknown codes render nothing.
QUEUE_NOTICES = {
    "acknowledged": ("ok", "Acknowledged — it stays in the queue until resolved."),
    "resolved": ("ok", "Resolved — the decision is recorded in the request's audit trail."),
    "already_acknowledged": ("warn", "Already acknowledged, likely by another dispatcher just now."),
    "already_resolved": ("warn", "Already resolved, likely by another dispatcher just now — it has left the queue."),
    "not_found": ("warn", "No such request in the audit store."),
    "not_escalated": ("warn", "That request was never escalated — nothing to work."),
    "bad_type": ("warn", "Resolving needs a valid request type — pick one from the list."),
}


# NOTE: registered before /admin/{request_id}, which would otherwise capture
# the literal path segment "queue" as a request id.
@app.get("/admin/queue", response_class=HTMLResponse)
def admin_queue(request: Request) -> Response:
    denied = _admin_guard(request)
    if denied is not None:
        return denied
    entries = WORKFLOW.queue_entries()
    counts = {
        "open": sum(1 for e in entries if e["wf"]["state"] == "open"),
        "acknowledged": sum(1 for e in entries if e["wf"]["state"] == "acknowledged"),
        "life_safety": sum(1 for e in entries if e["wf"]["life_safety"]),
    }
    return templates.TemplateResponse(
        request, "queue.html",
        {"active": "queue", "entries": entries, "counts": counts,
         "resolvable_types": RESOLVABLE_TYPES,
         "notice": QUEUE_NOTICES.get(request.query_params.get("notice", "")),
         "nav_queue": len(entries),
         "nav_queue_danger": counts["life_safety"] > 0},
    )


def _queue_redirect(notice: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/queue?notice={notice}", status_code=303)


@app.post("/admin/queue/{request_id}/ack")
def admin_queue_ack(request: Request, request_id: str) -> Response:
    denied = _admin_guard(request)
    if denied is not None:
        return denied
    try:
        WORKFLOW.acknowledge(request_id)
    except WorkflowError as exc:
        return _queue_redirect(exc.code)
    return _queue_redirect("acknowledged")


@app.post("/admin/queue/{request_id}/resolve")
def admin_queue_resolve(request: Request, request_id: str,
                        request_type: str = Form(""), note: str = Form("")) -> Response:
    denied = _admin_guard(request)
    if denied is not None:
        return denied
    if request_type not in RESOLVABLE_TYPES:
        return _queue_redirect("bad_type")
    try:
        # The note is free text headed for an append-only audit line; cap it
        # so a runaway paste can't bloat the store (form maxlength is 500).
        WORKFLOW.resolve(request_id, request_type, note=note[:2000])
    except WorkflowError as exc:
        return _queue_redirect(exc.code)
    return _queue_redirect("resolved")


@app.get("/admin/{request_id}", response_class=HTMLResponse)
def admin_detail(request: Request, request_id: str) -> Response:
    denied = _admin_guard(request)
    if denied is not None:
        return denied
    queue = WORKFLOW.queue_entries()
    nav = {"nav_queue": len(queue),
           "nav_queue_danger": any(e["wf"]["life_safety"] for e in queue)}
    record = STORE.get(request_id)
    if record is None:
        return templates.TemplateResponse(
            request, "detail.html",
            {"active": "admin", "record": None, "request_id": request_id, **nav},
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
            "fields": [classify_field(k, v) for k, v in recorded.items()],
            "safety": step_safety_flag(name, recorded),
        })
    # The trail no longer ends where the human begins: escalation-workflow
    # events continue it, rendered by the same generic step renderer.
    for event in WORKFLOW.events_for(request_id):
        name = event.get("event", "escalation_event")
        recorded = {k: v for k, v in event.items()
                    if k not in ("kind", "event", "request_id", "at")}
        steps.append({
            "name": name,
            "at": event.get("at", ""),
            "fields": [classify_field(k, v) for k, v in recorded.items()],
            "safety": step_safety_flag(name, recorded),
        })
    return templates.TemplateResponse(
        request, "detail.html",
        {"active": "admin", "record": record, "request_id": request_id,
         "steps": steps, "path": escalation_path(record),
         "wf": WORKFLOW.state_of(request_id), **nav},
    )


@app.get("/health")
def health() -> dict[str, Any]:
    unresolved = WORKFLOW.queue_entries()
    return {"status": "ok", "config": SETTINGS.summary(),
            "stored_requests": STORE.count(),
            "open_escalations": len(unresolved),
            "life_safety_open": sum(1 for e in unresolved if e["wf"]["life_safety"])}


# ------------------------------------------------------------------ entrypoint

def main() -> int:
    import uvicorn

    log.info("web -> http://localhost:%d  (admin at /admin)", WEB_PORT)
    uvicorn.run(app, host="127.0.0.1", port=WEB_PORT, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
