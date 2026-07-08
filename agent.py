"""Intake agent for a restoration-services company.

Takes an inbound service request (free text), analyzes it, and emits a
structured result: request id (code), request type (code), confidence (llm).

Build stages (from the project plan):
  1. [x] Foundation — env loading, config, data layer, pipeline scaffold (no tools, no LLM)
  2. [x] Deterministic code / tool calls (asserts) — normalization, input validation,
         service-area lookup tool, invariant asserts at the code<->LLM seam, selftest
  3. [ ] Auditability — JSON of the values at each step (structure baked in at stage 1)
  4. [x] LLM calls — client.messages.create with output_config.format (structured JSON):
         primary_model classifies, fallback_model retries on failure/refusal, total
         failure degrades to the same fail-safe stub used before this stage
  5. [x] Hardening — confidence-threshold escalation: a read below threshold gets
         one reread on escalation_model; still-low or a failed reread routes to
         a human. Plus --evals: a labeled set scored against the real pipeline.
  6. [x] UX — simple web page (:3000) + JSON API (:8000)

Volume target is MVP: single-request, synchronous, stdlib (+ the `anthropic` SDK) for now.

Usage:
    python3 agent.py                     # run a built-in sample request through the pipeline
    python3 agent.py <request text...>   # run your own request text
    python3 agent.py --selftest          # assert-based checks (deterministic layer, LLM
                                          # classification + escalation via a fake client,
                                          # web handlers — no network calls)
    python3 agent.py --evals             # labeled eval set against the real pipeline (network
                                          # calls; reports accuracy, confidence, escalation rate)
    python3 agent.py --serve             # web UI on http://localhost:3000, JSON API on :8000
"""

import json
import logging
import os
import re
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, MutableMapping

import anthropic

log = logging.getLogger("intake")

DEFAULT_PRIMARY_MODEL = "claude-haiku-4-5"     # cheap/fast classifier
DEFAULT_FALLBACK_MODEL = "claude-sonnet-5"     # retries a failed/refused primary-model call
DEFAULT_ESCALATION_MODEL = "claude-opus-4-8"   # low-confidence rereads (stage 5)
DEFAULT_CONFIDENCE_THRESHOLD = 0.70            # below this -> opus reread / human (stage 5)

# Deterministic validation bounds (stage 2). Tune once real volume data arrives.
MIN_TEXT_CHARS = 10
MAX_TEXT_CHARS = 8_000
ALLOWED_CHANNELS = frozenset({"web", "email", "phone"})

# Dev servers (stage 6). Localhost only — put a real reverse proxy in front later.
UI_PORT = 3000
API_PORT = 8000

SAMPLE_REQUEST = (
    "Hi, our basement flooded overnight after the storm and there's standing "
    "water everywhere. We're in Cedar Park and need someone out ASAP."
)


# --------------------------------------------------------------------------- config

class ConfigError(RuntimeError):
    """Raised when the environment/config is unusable. Fail fast at startup."""


class ValidationError(RuntimeError):
    """Raised when an inbound request fails deterministic validation (stage 2).

    Distinct from the literal `assert`s in the pipeline: ValidationError means
    bad *input* (reject the request); a failed assert means a *bug* or a
    malformed model reading crossing the code<->LLM seam.
    """

    def __init__(self, message: str, request_id: "str | None" = None):
        super().__init__(message)
        self.request_id = request_id


class ClassificationError(RuntimeError):
    """Raised internally when one model attempt in the classify chain doesn't
    produce a usable result (refusal, empty content, bad JSON). Caught by
    `_classify`, which moves on to the next model in the chain."""


def load_env(path: "Path | None" = None,
             env: "MutableMapping[str, str]" = os.environ) -> dict[str, str]:
    """Load KEY=VALUE pairs from .env (next to this file) into the environment.

    Real environment variables win; the file only fills gaps. A missing file is
    a warning, not an error — required keys are enforced by Settings.from_env().
    """
    env_path = path or Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        log.warning("no env file at %s; relying on process environment", env_path)
        return {}

    loaded: dict[str, str] = {}
    for line_num, raw in enumerate(env_path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{env_path}:{line_num}: expected KEY=VALUE, got {line!r}")
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        env.setdefault(key, value.strip().strip("'\""))
        loaded[key] = env[key]
    log.info("loaded %d env vars from %s", len(loaded), env_path)
    return loaded


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    primary_model: str = DEFAULT_PRIMARY_MODEL
    fallback_model: str = DEFAULT_FALLBACK_MODEL
    escalation_model: str = DEFAULT_ESCALATION_MODEL
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        missing = [key for key in ("ANTHROPIC_API_KEY",) if not env.get(key)]
        if missing:
            raise ConfigError(f"missing required env vars: {', '.join(missing)}")

        raw_threshold = env.get("INTAKE_CONFIDENCE_THRESHOLD", str(DEFAULT_CONFIDENCE_THRESHOLD))
        try:
            threshold = float(raw_threshold)
        except ValueError as exc:
            raise ConfigError(f"INTAKE_CONFIDENCE_THRESHOLD must be a float, got {raw_threshold!r}") from exc
        if not 0.0 <= threshold <= 1.0:
            raise ConfigError(f"INTAKE_CONFIDENCE_THRESHOLD must be in [0, 1], got {threshold}")

        return cls(
            anthropic_api_key=env["ANTHROPIC_API_KEY"],
            primary_model=env.get("INTAKE_PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL),
            fallback_model=env.get("INTAKE_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL),
            escalation_model=env.get("INTAKE_ESCALATION_MODEL", DEFAULT_ESCALATION_MODEL),
            confidence_threshold=threshold,
        )

    def summary(self) -> dict[str, Any]:
        """Loggable view of the config — never exposes the API key."""
        return {
            "anthropic_api_key": f"…{self.anthropic_api_key[-4:]}",
            "primary_model": self.primary_model,
            "fallback_model": self.fallback_model,
            "escalation_model": self.escalation_model,
            "confidence_threshold": self.confidence_threshold,
        }


# ----------------------------------------------------------------------- data layer

class RequestType(str, Enum):
    """Closed taxonomy the classifier must pick from (code-owned, not LLM-invented)."""

    WATER_DAMAGE = "water_damage"
    FIRE_SMOKE_DAMAGE = "fire_smoke_damage"
    MOLD_REMEDIATION = "mold_remediation"
    STORM_DAMAGE = "storm_damage"
    BIOHAZARD_CLEANUP = "biohazard_cleanup"
    RECONSTRUCTION = "reconstruction"
    GENERAL_INQUIRY = "general_inquiry"
    UNKNOWN = "unknown"  # pre-classification / fail-safe value — never LLM-chosen, see below


@dataclass(frozen=True)
class ServiceRequest:
    """An inbound request. request_id is assigned by code, never by the LLM."""

    request_id: str
    raw_text: str
    channel: str
    received_at: str

    @classmethod
    def new(cls, raw_text: str, channel: str = "web") -> "ServiceRequest":
        return cls(
            request_id=f"req_{uuid.uuid4().hex[:12]}",
            raw_text=raw_text,
            channel=channel,
            received_at=datetime.now(timezone.utc).isoformat(),
        )


@dataclass
class Analysis:
    """The output schema: request id (code), request type (code), confidence (llm).

    `audit` is the stage-3 data layer: a JSON-able record of the values at each
    pipeline step, appended as the request moves through.
    """

    request_id: str
    request_type: RequestType
    confidence: float
    escalate_to_human: bool
    notes: str
    audit: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["request_type"] = self.request_type.value
        return data


# ---------------------------------------------------------- deterministic code / tools

def normalize_text(text: str) -> str:
    """Collapse whitespace runs to single spaces and trim the ends."""
    return " ".join(text.split())


# MVP service-area table for the Austin metro. Deterministic stand-in for a real
# coverage API — swap the table for the API call without changing callers.
SERVICE_AREA_CITIES = frozenset({
    "austin", "round rock", "cedar park", "pflugerville", "georgetown",
    "leander", "san marcos", "kyle", "buda", "lakeway", "bee cave",
    "hutto", "manor", "dripping springs", "west lake hills",
})
SERVICE_AREA_ZIP_PREFIXES = ("786", "787")
ZIP_RE = re.compile(r"\b(\d{5})\b")


def lookup_service_area(location: str) -> dict[str, Any]:
    """Tool: deterministic service-area lookup by city name or 5-digit ZIP.

    This is the external-lookup tool stage 4's LLM will call with the location
    it extracts from the request text.
    """
    query = normalize_text(location).lower()
    in_area = query in SERVICE_AREA_CITIES or (
        query.isdigit() and len(query) == 5 and query.startswith(SERVICE_AREA_ZIP_PREFIXES)
    )
    return {"location": query, "in_service_area": in_area}


def scan_service_area_mentions(text: str) -> dict[str, Any]:
    """Deterministic pre-scan for known cities/ZIPs mentioned in free text.

    Best-effort only: `in_service_area` is True when a known mention is found
    and None (unknown) otherwise — never False, because proper location
    extraction is the stage-4 LLM's job.
    """
    lowered = normalize_text(text).lower()
    cities = sorted(city for city in SERVICE_AREA_CITIES if city in lowered)
    zips = sorted(z for z in ZIP_RE.findall(lowered) if z.startswith(SERVICE_AREA_ZIP_PREFIXES))
    mentions = cities + zips
    return {"mentions": mentions, "in_service_area": True if mentions else None}


# ------------------------------------------------------------------- llm classification

# The LLM only ever picks from the real categories — UNKNOWN is reserved for our
# own fail-safe path (total classification failure), never an LLM choice.
CLASSIFIABLE_TYPES = [t for t in RequestType if t is not RequestType.UNKNOWN]

CLASSIFICATION_SYSTEM_PROMPT = """You are the intake classifier for a restoration services \
company serving the Austin, TX metro area. Read one inbound customer request and classify \
it into exactly one category.

Categories:
- water_damage: flooding, burst pipes, leaks, standing water
- fire_smoke_damage: fire damage, smoke damage, soot
- mold_remediation: mold, mildew, musty odor, moisture-driven growth
- storm_damage: wind, hail, fallen trees, weather-driven roof or structure damage
- biohazard_cleanup: sewage backup, trauma or crime scenes, hazardous contamination
- reconstruction: rebuilding or repair work following prior damage, no active emergency
- general_inquiry: pricing, scheduling, or other questions that don't describe active damage

Pick the single closest category, even if the request could plausibly fit more than one. \
Report a calibrated confidence in that pick from 0.0 (a guess) to 1.0 (unambiguous) — this \
is a probability estimate, not enthusiasm. Add a one-sentence note explaining the pick, \
written for a dispatcher who has not read the raw request."""

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "request_type": {"type": "string", "enum": [t.value for t in CLASSIFIABLE_TYPES]},
        "confidence": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["request_type", "confidence", "notes"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------------- pipeline

class IntakeAgent:
    """Workflow-shaped agent: fixed steps, code-owned control flow."""

    def __init__(self, settings: Settings, client: "anthropic.Anthropic | None" = None):
        self.settings = settings
        # Injectable for tests (selftest passes a fake — see below); real runs
        # construct the actual SDK client, which makes no network call itself.
        self._client = client if client is not None else anthropic.Anthropic(
            api_key=settings.anthropic_api_key
        )

    def handle(self, raw_text: str, channel: str = "web") -> Analysis:
        trail: list[dict[str, Any]] = []

        request = ServiceRequest.new(raw_text=normalize_text(raw_text), channel=channel)
        self._record(trail, "received", request=asdict(request), raw_chars=len(raw_text))

        checks = self._validate(request)
        self._record(trail, "validated", status="passed", checks=checks)

        area = scan_service_area_mentions(request.raw_text)
        self._record(trail, "service_area", **area)

        analysis, classify_detail = self._classify(request)
        self._assert_invariants(request, analysis)
        self._record(
            trail,
            "classified",
            request_type=analysis.request_type.value,
            confidence=analysis.confidence,
            **classify_detail,
        )

        analysis, finalize_detail = self._finalize(request, analysis)
        self._assert_invariants(request, analysis)
        self._record(
            trail,
            "finalized",
            request_type=analysis.request_type.value,
            confidence=analysis.confidence,
            escalate_to_human=analysis.escalate_to_human,
            confidence_threshold=self.settings.confidence_threshold,
            **finalize_detail,
        )

        analysis.audit = trail
        log.info(
            "%s -> %s (confidence=%.2f, escalate=%s)",
            analysis.request_id, analysis.request_type.value,
            analysis.confidence, analysis.escalate_to_human,
        )
        return analysis

    @staticmethod
    def _record(trail: "list[dict[str, Any]]", step: str, **values: Any) -> None:
        trail.append({"step": step, "at": datetime.now(timezone.utc).isoformat(), **values})

    def _validate(self, request: ServiceRequest) -> list[str]:
        """Stage 2 — deterministic input validation.

        Collects every problem, then rejects loudly with one ValidationError.
        Returns the list of checks applied, for the audit trail.
        """
        problems: list[str] = []
        if not request.raw_text:
            problems.append("text is empty")
        elif len(request.raw_text) < MIN_TEXT_CHARS:
            problems.append(f"text too short ({len(request.raw_text)} < {MIN_TEXT_CHARS} chars)")
        if len(request.raw_text) > MAX_TEXT_CHARS:
            problems.append(f"text too long ({len(request.raw_text)} > {MAX_TEXT_CHARS} chars)")
        if request.channel not in ALLOWED_CHANNELS:
            problems.append(f"unknown channel {request.channel!r} (allowed: {sorted(ALLOWED_CHANNELS)})")

        if problems:
            raise ValidationError("; ".join(problems), request_id=request.request_id)
        return ["non_empty", "length_bounds", "channel"]

    @staticmethod
    def _assert_invariants(request: ServiceRequest, analysis: Analysis) -> None:
        """Stage 2 — invariants at the code<->LLM seam, as literal asserts.

        These guard against bugs and malformed model readings (stage 4 output
        passes through here), not against user input — input problems raise
        ValidationError in _validate. Don't run production with `python -O`.
        """
        assert analysis.request_id == request.request_id, (
            f"request_id mutated in pipeline: {analysis.request_id!r} != {request.request_id!r}"
        )
        assert isinstance(analysis.request_type, RequestType), (
            f"request_type escaped the taxonomy: {analysis.request_type!r}"
        )
        assert 0.0 <= analysis.confidence <= 1.0, (
            f"confidence out of range [0, 1]: {analysis.confidence!r}"
        )
        assert isinstance(analysis.escalate_to_human, bool), (
            f"escalate_to_human must be bool, got {type(analysis.escalate_to_human).__name__}"
        )

    def _classify_with_model(self, request: ServiceRequest, model: str) -> tuple[Analysis, dict[str, Any]]:
        """One classification attempt against `model`. Raises ClassificationError
        (or lets an SDK/parse exception propagate) on any failure — the caller
        decides whether to fall back to the next model."""
        response = self._client.messages.create(
            model=model,
            max_tokens=512,
            system=CLASSIFICATION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": request.raw_text}],
            output_config={"format": {"type": "json_schema", "schema": CLASSIFICATION_SCHEMA}},
        )

        # Check stop_reason before reading content — a refusal can carry empty content.
        if response.stop_reason == "refusal":
            raise ClassificationError(f"{model} refused the classification request")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ClassificationError(f"{model} returned no text block (stop_reason={response.stop_reason})")

        data = json.loads(text)  # output_config.format guarantees valid JSON when present
        confidence = float(data["confidence"])
        clamped = min(max(confidence, 0.0), 1.0)
        if clamped != confidence:
            log.warning("%s returned out-of-range confidence %.4f, clamped to %.2f",
                        model, confidence, clamped)

        analysis = Analysis(
            request_id=request.request_id,
            request_type=RequestType(data["request_type"]),
            confidence=clamped,
            escalate_to_human=False,  # successful automated read; stage 5 adds threshold policy
            notes=str(data["notes"]),
        )
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return analysis, usage

    def _classify(self, request: ServiceRequest) -> tuple[Analysis, dict[str, Any]]:
        """Stage 4 — LLM classification. Tries primary_model, then falls back to
        fallback_model on any failure (rate limit, refusal, malformed output, ...).
        If both fail, degrades to the same fail-safe stub this method returned
        before stage 4 existed: unknown type, zero confidence, escalate on.
        """
        attempts: list[dict[str, Any]] = []
        for model in (self.settings.primary_model, self.settings.fallback_model):
            try:
                analysis, usage = self._classify_with_model(request, model)
            except Exception as exc:
                log.warning("classification via %s failed for %s: %s: %s",
                            model, request.request_id, type(exc).__name__, exc)
                attempts.append({"model": model, "error": f"{type(exc).__name__}: {exc}"})
                continue
            attempts.append({"model": model, "usage": usage})
            return analysis, {"model_used": model, "attempts": attempts}

        log.error("all classification attempts failed for %s; routing to human review",
                   request.request_id)
        stub = Analysis(
            request_id=request.request_id,
            request_type=RequestType.UNKNOWN,
            confidence=0.0,
            escalate_to_human=True,
            notes="classification unavailable — all models failed; routed to human review",
        )
        return stub, {"model_used": None, "attempts": attempts}

    def _finalize(self, request: ServiceRequest, analysis: Analysis) -> tuple[Analysis, dict[str, Any]]:
        """Stage 5 — confidence-threshold escalation.

        A read at or above confidence_threshold passes through untouched. Below
        threshold, get one reread from escalation_model (opus) — a second,
        stronger opinion. If that reread is itself still below threshold, or the
        reread attempt fails outright, route to a human — but keep whichever
        automated read we have (the reread if we got one, otherwise the
        original), rather than discarding it.
        """
        threshold = self.settings.confidence_threshold
        if analysis.confidence >= threshold:
            return analysis, {"action": "passed"}

        log.info("%s below confidence threshold (%.2f < %.2f); escalating to %s",
                  request.request_id, analysis.confidence, threshold, self.settings.escalation_model)
        try:
            reread, usage = self._classify_with_model(request, self.settings.escalation_model)
        except Exception as exc:
            log.warning("escalation reread via %s failed for %s: %s: %s",
                        self.settings.escalation_model, request.request_id, type(exc).__name__, exc)
            analysis.escalate_to_human = True
            analysis.notes = f"{analysis.notes} (escalation reread failed — routed to human review)"
            return analysis, {
                "action": "escalation_failed",
                "escalation_model": self.settings.escalation_model,
                "error": f"{type(exc).__name__}: {exc}",
            }

        if reread.confidence < threshold:
            log.info("%s escalation reread still below threshold (%.2f < %.2f); routing to human",
                      request.request_id, reread.confidence, threshold)
            reread.escalate_to_human = True
            return reread, {
                "action": "escalated_to_human",
                "escalation_model": self.settings.escalation_model,
                "usage": usage,
            }

        log.info("%s escalation reread resolved: %s at %.2f",
                  request.request_id, reread.request_type.value, reread.confidence)
        return reread, {
            "action": "escalated_resolved",
            "escalation_model": self.settings.escalation_model,
            "usage": usage,
        }


# --------------------------------------------------------------------------- web ui

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Restoration Services Intake</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; background: #f4f4f2; color: #1e2226; }
  main { max-width: 720px; margin: 3rem auto; padding: 0 1rem; }
  h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
  p.sub { margin: 0 0 1.5rem; color: #5b6570; font-size: .9rem; }
  textarea { width: 100%; min-height: 8rem; padding: .75rem; font: inherit;
             border: 1px solid #c6ccd2; border-radius: 8px; resize: vertical; }
  button { margin-top: .75rem; padding: .6rem 1.4rem; font: inherit; font-weight: 600;
           color: #fff; background: #1c6e45; border: 0; border-radius: 8px; cursor: pointer; }
  button:disabled { opacity: .5; cursor: wait; }
  #result { margin-top: 1.5rem; }
  .card { background: #fff; border: 1px solid #dde1e5; border-radius: 10px; padding: 1rem 1.25rem; }
  .card.error { border-color: #cf3f3f; background: #fdf3f3; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: .35rem 1rem; margin: 0; }
  dt { color: #5b6570; }
  dd { margin: 0; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  .badge { display: inline-block; padding: .1rem .5rem; border-radius: 999px; font-size: .8rem; }
  .badge.yes { background: #fde8cd; color: #8a4b00; }
  .badge.no { background: #d9f2e4; color: #14532d; }
  details { margin-top: .75rem; }
  pre { overflow-x: auto; background: #f6f8fa; padding: .75rem; border-radius: 8px; font-size: .8rem; }
  footer { margin-top: 2rem; color: #8a939c; font-size: .8rem; }
</style>
</head>
<body>
<main>
  <h1>Restoration Services Intake</h1>
  <p class="sub">Describe the problem; the agent assigns a request id, type, and confidence.</p>
  <textarea id="text" placeholder="e.g. Our basement flooded overnight after the storm — standing water everywhere. Cedar Park, need help ASAP."></textarea>
  <br>
  <button id="go">Analyze request</button>
  <div id="result"></div>
  <footer>Classification runs live against Claude (haiku, falling back to sonnet on failure).
  Low-confidence reads get a second opinion from a stronger model before being routed
  to a human — see the audit trail below for exactly what happened on each request.</footer>
</main>
<script>
const API = "http://" + location.hostname + ":__API_PORT__/analyze";
const el = (id) => document.getElementById(id);

el("go").addEventListener("click", analyze);
el("text").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") analyze();
});

async function analyze() {
  const btn = el("go");
  btn.disabled = true;
  try {
    const res = await fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: el("text").value, channel: "web" }),
    });
    const data = await res.json();
    el("result").innerHTML = res.ok
      ? renderAnalysis(data)
      : renderError(data.error, data.request_id);
  } catch (err) {
    el("result").innerHTML = renderError(
      "API unreachable on :__API_PORT__ — is `python3 agent.py --serve` running? (" + err + ")");
  } finally {
    btn.disabled = false;
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderError(message, requestId) {
  return '<div class="card error"><strong>Rejected.</strong> ' + esc(message) +
    (requestId ? "<br><small>request id: " + esc(requestId) + "</small>" : "") + "</div>";
}

function renderAnalysis(a) {
  const badge = a.escalate_to_human
    ? '<span class="badge yes">yes — human review</span>'
    : '<span class="badge no">no</span>';
  return '<div class="card"><dl>' +
    "<dt>request id</dt><dd>" + esc(a.request_id) + "</dd>" +
    "<dt>type</dt><dd>" + esc(a.request_type) + "</dd>" +
    "<dt>confidence</dt><dd>" + Number(a.confidence).toFixed(2) + "</dd>" +
    "<dt>escalate</dt><dd>" + badge + "</dd>" +
    "<dt>notes</dt><dd>" + esc(a.notes) + "</dd>" +
    "</dl><details><summary>audit trail</summary><pre>" +
    esc(JSON.stringify(a.audit, null, 2)) + "</pre></details></div>";
}
</script>
</body>
</html>
"""


class _JsonHandler(BaseHTTPRequestHandler):
    """Shared plumbing: JSON responses, permissive CORS (local dev), our logging."""

    server_version = "IntakeAgent/0.4"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # CORS preflight for the :3000 -> :8000 fetch
        self.send_response(204)
        self._send_cors()
        self.end_headers()


class ApiHandler(_JsonHandler):
    """JSON API (:8000) — POST /analyze, GET /health."""

    agent: "IntakeAgent | None" = None  # injected by serve()/selftest()

    def do_GET(self) -> None:
        assert self.agent is not None, "serve() must inject the agent first"
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "config": self.agent.settings.summary()})
        elif self.path == "/":
            self._send_json(200, {"service": "intake-api",
                                  "endpoints": ["POST /analyze", "GET /health"]})
        else:
            self._send_json(404, {"error": f"no such endpoint: {self.path}"})

    def do_POST(self) -> None:
        assert self.agent is not None, "serve() must inject the agent first"
        if self.path != "/analyze":
            self._send_json(404, {"error": f"no such endpoint: {self.path}"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"")
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "body must be valid JSON"})
            return
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            self._send_json(400, {"error": 'body must be a JSON object with a string "text" field'})
            return

        try:
            analysis = self.agent.handle(body["text"], channel=body.get("channel", "web"))
        except ValidationError as exc:
            self._send_json(400, {"request_id": exc.request_id, "error": str(exc)})
            return
        self._send_json(200, analysis.to_dict())


class UiHandler(_JsonHandler):
    """Static UI (:3000) — serves the intake page."""

    api_port: int = API_PORT  # substituted into the page so the fetch targets the API

    def do_GET(self) -> None:
        if self.path not in ("/", "/index.html"):
            self._send_json(404, {"error": "not found"})
            return
        body = INDEX_HTML.replace("__API_PORT__", str(self.api_port)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(settings: Settings, ui_port: int = UI_PORT, api_port: int = API_PORT) -> int:
    """Run both dev servers (localhost only): UI on :3000, JSON API on :8000."""
    ApiHandler.agent = IntakeAgent(settings)
    UiHandler.api_port = api_port

    try:
        api_server = ThreadingHTTPServer(("127.0.0.1", api_port), ApiHandler)
    except OSError as exc:
        log.error("cannot bind API port %d (already in use?): %s", api_port, exc)
        return 1
    try:
        ui_server = ThreadingHTTPServer(("127.0.0.1", ui_port), UiHandler)
    except OSError as exc:
        log.error("cannot bind UI port %d (already in use?): %s", ui_port, exc)
        api_server.server_close()
        return 1

    for name, server in (("api", api_server), ("ui", ui_server)):
        threading.Thread(target=server.serve_forever, name=f"{name}-server", daemon=True).start()
    log.info("UI  -> http://localhost:%d", ui_port)
    log.info("API -> http://localhost:%d  (POST /analyze, GET /health)", api_port)
    log.info("Ctrl-C to stop")

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log.info("shutting down")
        api_server.shutdown()
        ui_server.shutdown()
        api_server.server_close()
        ui_server.server_close()
    return 0


# ------------------------------------------------------------------------- selftest

def selftest() -> int:
    """Assert-based checks: deterministic layer, LLM classification (via a fake
    client), and web handlers. No network calls and no real .env.

    Lives in this file because the project rule is "only edit agent.py"; move to
    a proper test module when that constraint lifts.
    """
    import tempfile
    import urllib.error
    import urllib.request

    # load_env: parsing, precedence, malformed lines
    env: dict[str, str] = {"KEEP": "process"}
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text("# comment\n\nA=1\nexport B='two'\nKEEP=file\n")
        load_env(env_file, env)
        assert env["A"] == "1" and env["B"] == "two", env
        assert env["KEEP"] == "process", "process environment must win over the file"
        env_file.write_text("not a pair\n")
        try:
            load_env(env_file, env)
            raise AssertionError("malformed .env line must raise ConfigError")
        except ConfigError:
            pass

    # Settings: required key, defaults, threshold parsing and bounds
    for bad_env in ({}, {"ANTHROPIC_API_KEY": ""}):
        try:
            Settings.from_env(bad_env)
            raise AssertionError(f"missing key must raise ConfigError: {bad_env}")
        except ConfigError:
            pass
    settings = Settings.from_env({"ANTHROPIC_API_KEY": "test-key"})
    assert settings.primary_model == DEFAULT_PRIMARY_MODEL
    assert settings.fallback_model == DEFAULT_FALLBACK_MODEL
    assert settings.escalation_model == DEFAULT_ESCALATION_MODEL
    assert settings.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD
    for bad_threshold in ("nope", "1.5", "-0.1"):
        try:
            Settings.from_env({"ANTHROPIC_API_KEY": "k", "INTAKE_CONFIDENCE_THRESHOLD": bad_threshold})
            raise AssertionError(f"bad threshold must raise ConfigError: {bad_threshold}")
        except ConfigError:
            pass

    # normalization
    assert normalize_text("  water \n\n in   basement ") == "water in basement"

    # service-area tool
    assert lookup_service_area("Cedar Park")["in_service_area"] is True
    assert lookup_service_area("78613")["in_service_area"] is True
    assert lookup_service_area("Dallas")["in_service_area"] is False
    assert lookup_service_area("75201")["in_service_area"] is False
    scan = scan_service_area_mentions(SAMPLE_REQUEST)
    assert scan["mentions"] == ["cedar park"] and scan["in_service_area"] is True, scan
    assert scan_service_area_mentions("no location mentioned here")["in_service_area"] is None

    # --- LLM classification (stage 4), exercised via a fake client — no network ---

    class _FakeTextBlock:
        type = "text"

        def __init__(self, text: str):
            self.text = text

    class _FakeUsage:
        def __init__(self, input_tokens: int = 42, output_tokens: int = 17):
            self.input_tokens = input_tokens
            self.output_tokens = output_tokens

    class _FakeResponse:
        def __init__(self, text: "str | None" = None, stop_reason: str = "end_turn"):
            self.content = [_FakeTextBlock(text)] if text is not None else []
            self.stop_reason = stop_reason
            self.usage = _FakeUsage()

    class _FakeMessages:
        def __init__(self, handler):
            self._handler = handler

        def create(self, **kwargs):
            return self._handler(**kwargs)

    class _FakeClient:
        def __init__(self, handler):
            self.messages = _FakeMessages(handler)

    def _json_response(request_type: str, confidence: float, notes: str = "test notes") -> _FakeResponse:
        return _FakeResponse(json.dumps(
            {"request_type": request_type, "confidence": confidence, "notes": notes}))

    # success on the primary model
    calls: list[str] = []

    def primary_ok(**kwargs):
        calls.append(kwargs["model"])
        return _json_response("water_damage", 0.87, "Explicit flooding and standing water reported.")

    agent_ok = IntakeAgent(settings, client=_FakeClient(primary_ok))
    analysis = agent_ok.handle(SAMPLE_REQUEST)
    assert analysis.request_type is RequestType.WATER_DAMAGE
    assert analysis.confidence == 0.87
    assert analysis.escalate_to_human is False
    assert analysis.notes == "Explicit flooding and standing water reported."
    assert calls == [settings.primary_model], calls
    classified_step = next(s for s in analysis.audit if s["step"] == "classified")
    assert classified_step["model_used"] == settings.primary_model, classified_step

    # primary model fails -> falls back to the secondary model
    calls = []

    def primary_fails_then_ok(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == settings.primary_model:
            raise RuntimeError("simulated rate limit")
        # confidence kept >= settings.confidence_threshold so this stays a pure
        # test of the classify fallback chain — stage-5 escalation has its own tests
        return _json_response("mold_remediation", 0.85, "Musty odor and visible growth reported.")

    agent_fallback = IntakeAgent(settings, client=_FakeClient(primary_fails_then_ok))
    analysis = agent_fallback.handle(SAMPLE_REQUEST)
    assert analysis.request_type is RequestType.MOLD_REMEDIATION
    assert analysis.escalate_to_human is False
    assert calls == [settings.primary_model, settings.fallback_model], calls
    classified_step = next(s for s in analysis.audit if s["step"] == "classified")
    assert classified_step["model_used"] == settings.fallback_model, classified_step
    assert len(classified_step["attempts"]) == 2 and "error" in classified_step["attempts"][0]

    # both models fail -> fail-safe stub, escalated (same posture as pre-stage-4)
    def always_fails(**kwargs):
        raise RuntimeError("simulated outage")

    agent_down = IntakeAgent(settings, client=_FakeClient(always_fails))
    analysis = agent_down.handle(SAMPLE_REQUEST)
    assert analysis.request_type is RequestType.UNKNOWN
    assert analysis.confidence == 0.0
    assert analysis.escalate_to_human is True
    classified_step = next(s for s in analysis.audit if s["step"] == "classified")
    assert classified_step["model_used"] is None
    assert len(classified_step["attempts"]) == 2

    # a refusal is treated as a failed attempt, not a crash on empty content
    def refuses(**kwargs):
        return _FakeResponse(text=None, stop_reason="refusal")

    agent_refused = IntakeAgent(settings, client=_FakeClient(refuses))
    analysis = agent_refused.handle(SAMPLE_REQUEST)
    assert analysis.request_type is RequestType.UNKNOWN
    assert analysis.escalate_to_human is True

    # out-of-range confidence from the model is clamped, not trusted verbatim
    def overconfident(**kwargs):
        return _json_response("storm_damage", 1.4, "Overconfident test double.")

    agent_clamped = IntakeAgent(settings, client=_FakeClient(overconfident))
    analysis = agent_clamped.handle(SAMPLE_REQUEST)
    assert analysis.confidence == 1.0, analysis.confidence

    # --- Stage 5: confidence-threshold escalation, also via a fake client ---

    # confidence already >= threshold -> passes straight through, no escalation call
    calls = []

    def high_confidence(**kwargs):
        calls.append(kwargs["model"])
        return _json_response("water_damage", 0.9, "High-confidence primary read.")

    agent_high = IntakeAgent(settings, client=_FakeClient(high_confidence))
    analysis = agent_high.handle(SAMPLE_REQUEST)
    assert analysis.confidence == 0.9 and analysis.escalate_to_human is False
    assert calls == [settings.primary_model], calls  # escalation_model never invoked
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["action"] == "passed", finalized_step

    # low primary confidence -> escalation reread on escalation_model resolves it
    calls = []

    def low_then_resolved(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == settings.escalation_model:
            return _json_response("mold_remediation", 0.92, "Escalated read: clear mold case.")
        return _json_response("mold_remediation", 0.4, "Uncertain primary read.")

    agent_escalated_ok = IntakeAgent(settings, client=_FakeClient(low_then_resolved))
    analysis = agent_escalated_ok.handle(SAMPLE_REQUEST)
    assert analysis.confidence == 0.92 and analysis.escalate_to_human is False
    assert calls == [settings.primary_model, settings.escalation_model], calls
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["action"] == "escalated_resolved", finalized_step

    # low primary confidence -> escalation reread still low -> routed to human,
    # but keeps the (better) escalated read rather than the original guess
    def low_then_still_low(**kwargs):
        if kwargs["model"] == settings.escalation_model:
            return _json_response("general_inquiry", 0.5, "Still uncertain even after escalation.")
        return _json_response("general_inquiry", 0.3, "Uncertain primary read.")

    agent_escalated_human = IntakeAgent(settings, client=_FakeClient(low_then_still_low))
    analysis = agent_escalated_human.handle(SAMPLE_REQUEST)
    assert analysis.confidence == 0.5 and analysis.escalate_to_human is True
    assert analysis.request_type is RequestType.GENERAL_INQUIRY
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["action"] == "escalated_to_human", finalized_step

    # low primary confidence -> the escalation call itself fails -> routed to
    # human, original (primary) read preserved rather than dropped
    def low_then_escalation_fails(**kwargs):
        if kwargs["model"] == settings.escalation_model:
            raise RuntimeError("simulated opus outage")
        return _json_response("general_inquiry", 0.3, "Uncertain primary read.")

    agent_escalation_failed = IntakeAgent(settings, client=_FakeClient(low_then_escalation_fails))
    analysis = agent_escalation_failed.handle(SAMPLE_REQUEST)
    assert analysis.confidence == 0.3 and analysis.escalate_to_human is True
    assert analysis.request_type is RequestType.GENERAL_INQUIRY
    assert "routed to human review" in analysis.notes
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["action"] == "escalation_failed", finalized_step

    json.dumps(analysis.to_dict())  # output must stay JSON-serializable

    # pipeline rejections: every bad input fails loudly, still carrying an id
    # (validation runs before classify, so any agent — including agent_ok — works)
    rejects = [
        ("", "web"),                            # empty
        ("help", "web"),                        # too short
        ("x" * (MAX_TEXT_CHARS + 1), "web"),    # too long
        (SAMPLE_REQUEST, "fax"),                # unknown channel
    ]
    for raw, channel in rejects:
        try:
            agent_ok.handle(raw, channel=channel)
            raise AssertionError(f"must reject raw={raw[:20]!r} channel={channel!r}")
        except ValidationError as exc:
            assert exc.request_id, "rejections must still carry a request_id"

    # web layer: real HTTP round-trips on ephemeral ports (no fixed-port collisions)
    ApiHandler.agent = agent_ok
    api_server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    ui_server = ThreadingHTTPServer(("127.0.0.1", 0), UiHandler)
    for server in (api_server, ui_server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        api = f"http://127.0.0.1:{api_server.server_address[1]}"
        ui = f"http://127.0.0.1:{ui_server.server_address[1]}"

        req = urllib.request.Request(
            f"{api}/analyze",
            data=json.dumps({"text": SAMPLE_REQUEST}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as res:
            payload = json.loads(res.read())
            assert res.status == 200, res.status
        assert payload["request_type"] == "water_damage" and payload["escalate_to_human"] is False
        assert payload["audit"][-1]["step"] == "finalized", payload["audit"]

        bad = urllib.request.Request(
            f"{api}/analyze",
            data=json.dumps({"text": "help"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(bad)
            raise AssertionError("short text must return HTTP 400")
        except urllib.error.HTTPError as err:
            assert err.code == 400 and "error" in json.loads(err.read())

        with urllib.request.urlopen(f"{api}/health") as res:
            assert res.status == 200 and json.loads(res.read())["status"] == "ok"

        with urllib.request.urlopen(f"{ui}/") as res:
            page = res.read().decode()
            assert res.status == 200, res.status
        assert "<textarea" in page and f":{API_PORT}/analyze" in page
    finally:
        for server in (api_server, ui_server):
            server.shutdown()
            server.server_close()

    print("selftest: all checks passed (deterministic layer, LLM classification, "
          "confidence escalation, web handlers)")
    return 0


# ------------------------------------------------------------------------- evals

# Hand-labeled cases, one dominant signal each (deliberately avoids cross-category
# trigger words like "storm" in a water-damage case) so accuracy isn't noisy from
# genuinely ambiguous inputs. Add a regression case here whenever a real
# misclassification turns up.
EVAL_CASES: list[tuple[str, RequestType]] = [
    ("A pipe burst under the kitchen sink an hour ago and water is pooling across the kitchen floor.",
     RequestType.WATER_DAMAGE),
    ("Our water heater is leaking and there's about an inch of standing water in the utility closet.",
     RequestType.WATER_DAMAGE),
    ("Grease fire in the kitchen last night, whole house smells like smoke and there's soot on the ceiling.",
     RequestType.FIRE_SMOKE_DAMAGE),
    ("We had an electrical fire in the garage, everything in there is charred and smells like smoke.",
     RequestType.FIRE_SMOKE_DAMAGE),
    ("There's black mold growing behind the bathroom tile and a musty smell that won't go away.",
     RequestType.MOLD_REMEDIATION),
    ("We noticed mildew spreading across the basement ceiling, no water actively leaking, just a "
     "persistent musty smell for weeks.",
     RequestType.MOLD_REMEDIATION),
    ("A large tree fell on our roof during last night's windstorm and punched a hole through the shingles.",
     RequestType.STORM_DAMAGE),
    ("Hail damaged our siding and gutters pretty badly in yesterday's storm, no leaks yet though.",
     RequestType.STORM_DAMAGE),
    ("Our sewer line backed up into the basement, raw sewage everywhere, we have small kids at home.",
     RequestType.BIOHAZARD_CLEANUP),
    ("We need help cleaning and disinfecting a room after an unattended death — this is a biohazard situation.",
     RequestType.BIOHAZARD_CLEANUP),
    ("We finished mold remediation last month and now need the drywall and flooring rebuilt in that room.",
     RequestType.RECONSTRUCTION),
    ("Can you give me a rough estimate for what mold remediation typically costs before I commit to anything?",
     RequestType.GENERAL_INQUIRY),
]


def evals() -> int:
    """Run the labeled eval set against the real pipeline (real network calls —
    needs a valid ANTHROPIC_API_KEY). Reports per-case accuracy, confidence,
    escalation rate, and latency.

    Distinct from --selftest: selftest checks that the code is wired correctly
    (fake client, no network, deterministic pass/fail). evals checks whether the
    model actually classifies well — expected to shift as prompts or models
    change, and this is where to add a case when a real misclassification
    turns up.
    """
    import time

    try:
        load_env()
        settings = Settings.from_env()
    except ConfigError as exc:
        log.error("startup failed: %s", exc)
        return 1
    log.info("config: %s", settings.summary())
    print(f"running {len(EVAL_CASES)} eval cases against the live API...\n")

    agent = IntakeAgent(settings)
    results: list[dict[str, Any]] = []
    for text, expected in EVAL_CASES:
        started = time.monotonic()
        try:
            analysis = agent.handle(text)
        except ValidationError as exc:
            print(f"ERROR  expected={expected.value:<18} validation failed: {exc}")
            results.append({"expected": expected.value, "correct": False,
                             "confidence": 0.0, "escalated": True,
                             "seconds": time.monotonic() - started})
            continue

        elapsed = time.monotonic() - started
        correct = analysis.request_type is expected
        results.append({"expected": expected.value, "correct": correct,
                         "confidence": analysis.confidence,
                         "escalated": analysis.escalate_to_human, "seconds": elapsed})
        mark = "PASS" if correct else "FAIL"
        print(f"{mark}  expected={expected.value:<18} got={analysis.request_type.value:<18} "
              f"conf={analysis.confidence:.2f} escalate={analysis.escalate_to_human} "
              f"({elapsed:.1f}s)  {text[:60]}")

    n = len(results)
    passed = sum(r["correct"] for r in results)
    escalated = sum(r["escalated"] for r in results)
    avg_conf = sum(r["confidence"] for r in results) / n
    avg_seconds = sum(r["seconds"] for r in results) / n
    print()
    print(f"accuracy: {passed}/{n} ({passed / n:.0%})")
    print(f"avg confidence: {avg_conf:.2f}   escalated: {escalated}/{n}   avg latency: {avg_seconds:.1f}s")

    return 0 if passed == n else 1


# ------------------------------------------------------------------------ entrypoint

def main(argv: "list[str] | None" = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,  # keep stdout clean: it carries only the result JSON
        format="%(levelname)s %(name)s: %(message)s",
    )

    args = argv if argv is not None else sys.argv[1:]
    if args == ["--selftest"]:
        return selftest()
    if args == ["--evals"]:
        return evals()
    if args and args[0].startswith("--") and args != ["--serve"]:
        log.error("unknown usage: %r (flags: --selftest, --evals, --serve)", " ".join(args))
        return 64

    try:
        load_env()
        settings = Settings.from_env()
    except ConfigError as exc:
        log.error("startup failed: %s", exc)
        return 1
    log.info("config: %s", settings.summary())

    if args == ["--serve"]:
        return serve(settings)

    raw_text = " ".join(args) if args else SAMPLE_REQUEST

    agent = IntakeAgent(settings)
    try:
        analysis = agent.handle(raw_text)
    except ValidationError as exc:
        log.error("request rejected: %s", exc)
        print(json.dumps({"request_id": exc.request_id, "error": str(exc)}, indent=2))
        return 2

    print(json.dumps(analysis.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
