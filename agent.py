"""Intake agent for a restoration-services company.

Takes an inbound service request (free text), analyzes it, and emits a
structured result: request id (code), request type (code), confidence (llm).

Build stages (from the project plan):
  1. [x] Foundation — env loading, config, data layer, pipeline scaffold (no tools, no LLM)
  2. [x] Deterministic code / tool calls (asserts) — normalization, input validation,
         service-area lookup tool, invariant asserts at the code<->LLM seam, selftest
  3. [x] Auditability — JSON of the values at each step (structure baked in at stage 1)
  4. [x] LLM calls — client.messages.create with output_config.format (structured JSON):
         primary_model classifies, fallback_model retries on failure/refusal, total
         failure degrades to the same fail-safe stub used before this stage
  5. [x] Hardening — confidence-threshold escalation: a read below threshold gets
         one reread on escalation_model; still-low or a failed reread routes to
         a human. Plus --evals: a labeled set scored against the real pipeline.
         Plus (packaging pass): a deterministic hazard-material screen ahead of
         the LLM (hazmat phrasing routes straight to biohazard_cleanup + human
         review — never model-dependent), retry with exponential backoff on
         transient provider errors, and a Dockerfile that runs --selftest by
         default.
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

import hashlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

import anthropic

log = logging.getLogger("intake")

DEFAULT_PRIMARY_MODEL = "claude-haiku-4-5"     # cheap/fast classifier
DEFAULT_FALLBACK_MODEL = "claude-sonnet-5"     # retries a failed/refused primary-model call
DEFAULT_ESCALATION_MODEL = "claude-opus-4-8"   # low-confidence rereads (stage 5)
DEFAULT_CONFIDENCE_THRESHOLD = 0.70            # below this -> opus reread / human (stage 5)
DEFAULT_MAX_RETRIES = 2                        # transient-error retries per model call
RETRY_BASE_SECONDS = 0.5                       # first backoff delay; doubles per retry
RETRY_CAP_SECONDS = 8.0                        # backoff ceiling

# Provider failures worth retrying in place. Anything else fails the attempt
# immediately: auth/config errors won't heal on retry, and refusals/malformed
# output are handled by the model fallback chain instead.
TRANSIENT_API_ERRORS: "tuple[type[Exception], ...]" = (
    anthropic.APIConnectionError,   # network trouble; includes APITimeoutError
    anthropic.RateLimitError,       # 429
    anthropic.InternalServerError,  # 5xx, including 529 overloaded
)

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

    Rejections are persistable, not vanished: `audit` carries the finished
    trail (received -> validated[status=rejected] -> rejected) and `to_dict()`
    returns a store-ready record with status "rejected" — the rejected-request
    counterpart of Analysis.to_dict().
    """

    def __init__(self, message: str, request_id: "str | None" = None,
                 audit: "list[dict[str, Any]] | None" = None):
        super().__init__(message)
        self.request_id = request_id
        self.audit: list[dict[str, Any]] = audit if audit is not None else []

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "status": "rejected",
                "error": str(self), "audit": self.audit}


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
    max_retries: int = DEFAULT_MAX_RETRIES

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

        raw_retries = env.get("INTAKE_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))
        try:
            max_retries = int(raw_retries)
        except ValueError as exc:
            raise ConfigError(f"INTAKE_MAX_RETRIES must be an int, got {raw_retries!r}") from exc
        if not 0 <= max_retries <= 8:
            raise ConfigError(f"INTAKE_MAX_RETRIES must be in [0, 8], got {max_retries}")

        return cls(
            anthropic_api_key=env["ANTHROPIC_API_KEY"],
            primary_model=env.get("INTAKE_PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL),
            fallback_model=env.get("INTAKE_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL),
            escalation_model=env.get("INTAKE_ESCALATION_MODEL", DEFAULT_ESCALATION_MODEL),
            confidence_threshold=threshold,
            max_retries=max_retries,
        )

    def summary(self) -> dict[str, Any]:
        """Loggable view of the config — reports key presence only, never any
        part of the secret, even masked (this reaches /health endpoints)."""
        return {
            "anthropic_api_key": "set" if self.anthropic_api_key else "missing",
            "primary_model": self.primary_model,
            "fallback_model": self.fallback_model,
            "escalation_model": self.escalation_model,
            "confidence_threshold": self.confidence_threshold,
            "max_retries": self.max_retries,
            "prompt_config": PROMPT_CONFIG.summary(),
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

    `life_safety` is set by the deterministic life-safety screen (never by the
    LLM); True always implies escalate_to_human=True.

    `customer_reply` is the reviewed customer-facing reply text; None when the
    reply stage was skipped (deterministic hazard path, failed classification).
    On review-gate fallback it carries the dispatcher note (`notes`) and the
    request routes to a human — see the `customer_reply` audit step's `source`.
    """

    request_id: str
    request_type: RequestType
    confidence: float
    escalate_to_human: bool
    notes: str
    life_safety: bool = False
    customer_reply: "str | None" = None
    audit: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["request_type"] = self.request_type.value
        data["status"] = "analyzed"  # counterpart of ValidationError's "rejected"
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


# Hazardous-material vocabulary for the deterministic screen below, label ->
# word-boundary pattern (matched against normalized lowercase text). The label
# is what the audit trail reports. Deliberately biased toward over-triggering:
# a false positive costs one human review, a false negative sends a crew into
# a hazmat scene. Bare "toxic"/"contaminated" stay off the list — they'd hijack
# routine mold/flood-water phrasings that the taxonomy already handles.
HAZARD_TERM_PATTERNS: "dict[str, re.Pattern[str]]" = {
    label: re.compile(pattern)
    for label, pattern in {
        "nuclear": r"\bnuclear\b",
        "radioactive": r"\bradioactiv\w*\b",
        "radiation": r"\bradiation\b",
        "chemical": r"\bchemicals?\b",
        "sewage": r"\bsewage\b",
        "sewer": r"\bsewers?\b",
        "asbestos": r"\basbestos\b",
        "biohazard": r"\bbio-?hazard\w*\b",
        "hazmat": r"\bhaz-?mat\b",
        "hazardous": r"\bhazardous\b",
        "toxic": r"\btoxic\s+(?:waste|spill|sludge|fumes)\b",
    }.items()
}


def scan_hazard_terms(text: str) -> list[str]:
    """Deterministic screen for hazardous-material phrasing (stage-5 hardening).

    Runs ahead of the LLM call: any match short-circuits classification to
    BIOHAZARD_CLEANUP with escalate_to_human=True. Code owns this decision,
    not the LLM — hazmat routing must never depend on a model reading.
    Returns the sorted matched labels (empty when clean) for the audit trail.
    """
    lowered = normalize_text(text).lower()
    return sorted(label for label, pattern in HAZARD_TERM_PATTERNS.items()
                  if pattern.search(lowered))


# Life-safety vocabulary for the urgency screen, label -> word-boundary pattern
# (matched against normalized lowercase text), same shape as the hazard screen
# above. Active-emergency phrasing only: past-tense damage reports ("we had a
# fire", "smells like smoke") are routine intake and must stay quiet, but a
# false positive on an active read still only costs one human review — bias
# toward over-triggering within the present-tense phrasings.
LIFE_SAFETY_TERM_PATTERNS: "dict[str, re.Pattern[str]]" = {
    label: re.compile(pattern)
    for label, pattern in {
        "active_fire": r"\bon fire\b|\bflames?\b|\bburning\b|\bfire is spreading\b",
        "active_smoke": r"\bsmoke is\b|\bfilling with smoke\b|\bfull of smoke\b",
        "gas_leak": r"\bsmell\w* gas\b|\bgas (?:is )?leak\w*\b|\bgas smell\b|\bgas odou?r\b",
        "carbon_monoxide": r"\bcarbon monoxide\b|\bco (?:alarm|detector)s?\b",
        "emergency_911": r"\b911\b",
    }.items()
}


def scan_life_safety_terms(text: str) -> list[str]:
    """Deterministic life-safety screen (product pass): active fire, gas leak,
    carbon monoxide, smoke-right-now, 911 phrasing.

    Runs ahead of the LLM call and never depends on one: a match sets the
    analysis's `life_safety` flag and forces escalate_to_human=True, while
    classification still proceeds normally (the request type is a separate
    question from "someone may be in danger right now"). A burning-house
    request must never depend on a model call to say "call 911".
    Returns the sorted matched labels (empty when clean) for the audit trail.
    """
    lowered = normalize_text(text).lower()
    return sorted(label for label, pattern in LIFE_SAFETY_TERM_PATTERNS.items()
                  if pattern.search(lowered))


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
- biohazard_cleanup: sewage backup, trauma or crime scenes, hazardous-material \
contamination or spills (chemical, radioactive or nuclear, asbestos, other toxic substances)
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


# ------------------------------------------------------------- llm customer reply

# The exact opener a life-safety reply must start with. Enforced in code by
# _review_reply, never by the model critic — same doctrine as the screens: the
# "call 911" line must never depend on a model call.
LIFE_SAFETY_REPLY_OPENER = "If this is an emergency, call 911 first."

RESPONDER_SYSTEM_PROMPT = """You draft one short reply to a customer of a restoration-services \
company (water, fire/smoke, mold, storm, biohazard, reconstruction) who has just submitted an \
intake request. You receive the request and the intake analysis as JSON.

Hard rules:
- 2 to 4 sentences, plain respectful tone, no marketing language.
- Acknowledge the situation; say what happens next in general terms only.
- NEVER state or promise pricing, quotes, or costs.
- NEVER promise arrival, completion, or response times.
- NEVER admit fault or liability on behalf of the company.
- If life_safety is true, the reply MUST open with exactly: "If this is an emergency, call 911 \
first."
- If escalate_to_human is true, say a team member will personally review the request and reach \
out.
- If reviewer_feedback_on_previous_draft is present, fix every issue it lists.

Return only the reply text — no preamble, no quotation marks, no JSON."""

REPLY_CRITIC_SYSTEM_PROMPT = """You review one drafted customer reply for a restoration-services \
company against a hard checklist. You receive the customer request, the intake analysis, and the \
draft reply as JSON. Approve only if ALL checks pass:

1. No pricing, quote, or cost language of any kind.
2. No promised arrival, completion, or response times ("within the hour", "tomorrow", ...).
3. No admission of fault or liability for the company.
4. If life_safety is true: the reply opens with exactly "If this is an emergency, call 911 \
first."
5. Plain, respectful, professional tone; no marketing hype; at most 4 sentences.

Set approved=true only when every check passes; otherwise approved=false with one short reason \
per failed check."""

REPLY_CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approved", "reasons"],
    "additionalProperties": False,
}


# ------------------------------------------- versioned prompt/config surface

@dataclass(frozen=True)
class PromptConfig:
    """The promotion artifact: every prompt and threshold the pipeline's
    behavior depends on, under one explicit version string.

    This is the surface the CI promotion gate keys on — change anything here
    (or the literals it aggregates) and the eval suite must pass before the
    change promotes. The version says what humans intended; `fingerprint()`
    says what the content actually is, so the audit trail can prove which
    exact prompt/threshold set classified a given request even if someone
    edits a prompt without bumping the version.

    `confidence_threshold` is the versioned *default*; Settings may override
    it at runtime via INTAKE_CONFIDENCE_THRESHOLD (the audit trail records
    the effective value on every request either way).

    `class_confidence_thresholds` are per-class floors on top of the global
    threshold, keyed by RequestType value. Rationale: a model read that
    routes a crew *toward* a safety-relevant class carries asymmetric
    misroute cost — sending people expecting a routine job into what is
    actually hazmat/fire territory (or vice versa) is far more expensive
    than one extra Opus reread or human review. So those reads must clear a
    higher bar before they pass unescalated. Floors only ever tighten: the
    effective threshold for a class is max(global, class floor) — see
    effective_confidence_threshold(). Classes not listed use the global
    fallback (0.70 by default).
    """

    version: str
    classification_system_prompt: str
    responder_system_prompt: str
    reply_critic_system_prompt: str
    confidence_threshold: float
    class_confidence_thresholds: "Mapping[str, float]" = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Stable 12-hex-char content hash of the whole surface."""
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def summary(self) -> dict[str, Any]:
        """Loggable view (reaches /health): version + fingerprint + thresholds,
        never the prompt text itself."""
        return {
            "version": self.version,
            "fingerprint": self.fingerprint(),
            "confidence_threshold": self.confidence_threshold,
            "class_confidence_thresholds": dict(self.class_confidence_thresholds),
        }


# Version history — bump on ANY change to a prompt or threshold in this
# surface (the CI promotion gate runs the eval suite on every such change):
#   1.0.0  original one-hour build: classification prompt, global 0.70 threshold
#   1.1.0  post-nuclear-regression hardening + product pass: biohazard bullet
#          names chemical/radioactive/nuclear/asbestos explicitly; responder
#          and reply-critic prompts added behind the review gate
#   1.2.0  per-class confidence floors: biohazard_cleanup and fire_smoke_damage
#          reads must clear 0.85 (vs the 0.70 global) before passing unescalated
PROMPT_CONFIG = PromptConfig(
    version="1.2.0",
    classification_system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
    responder_system_prompt=RESPONDER_SYSTEM_PROMPT,
    reply_critic_system_prompt=REPLY_CRITIC_SYSTEM_PROMPT,
    confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
    # Default floors, chosen against misroute cost, not model behavior:
    #   biohazard_cleanup 0.85 — a wrong read in either direction near hazmat
    #       means a crew walks in with the wrong protective posture. (Obvious
    #       hazmat phrasing never gets this far — the deterministic screen
    #       short-circuits it at confidence 1.0; this floor guards the
    #       *model-read* biohazard calls on subtler phrasing.)
    #   fire_smoke_damage 0.85 — fire routing sits next to the life-safety
    #       tier; a shaky fire read deserves a second opinion before it
    #       passes as routine intake. (Active-fire phrasing is already
    #       force-escalated by the deterministic life-safety screen.)
    # Everything else: cheap to misroute (a water/mold/storm mix-up costs a
    # phone call), so the global 0.70 fallback stands.
    class_confidence_thresholds={
        RequestType.BIOHAZARD_CLEANUP.value: 0.85,
        RequestType.FIRE_SMOKE_DAMAGE.value: 0.85,
    },
)


def effective_confidence_threshold(
    request_type: RequestType,
    global_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    config: "PromptConfig | None" = None,
) -> "tuple[float, str]":
    """The confidence bar a read of `request_type` must clear, and why.

    Returns (threshold, source) where source is "global" or
    "class:<request_type>". Per-class values are floors: they apply only when
    they *raise* the bar above the global threshold (which INTAKE_CONFIDENCE_
    THRESHOLD may have moved at runtime) — so operators can tighten globally
    without a class entry silently loosening below it. The threshold is
    evaluated against the type each read actually reports: an escalation
    reread that lands on a different class is judged against that class's bar.
    """
    cfg = config if config is not None else PROMPT_CONFIG
    class_floor = cfg.class_confidence_thresholds.get(request_type.value)
    if class_floor is not None and class_floor > global_threshold:
        return class_floor, f"class:{request_type.value}"
    return global_threshold, "global"


# ------------------------------------------------------------------------- pipeline

class IntakeAgent:
    """Workflow-shaped agent: fixed steps, code-owned control flow."""

    def __init__(self, settings: Settings, client: "anthropic.Anthropic | None" = None,
                 sleep: "Callable[[float], None]" = time.sleep):
        self.settings = settings
        # Injectable for tests (selftest passes a fake — see below); real runs
        # construct the actual SDK client, which makes no network call itself.
        # SDK-internal retries are off: _create_with_retry owns the retry
        # policy, so every attempt is visible in our logs and audit trail.
        self._client = client if client is not None else anthropic.Anthropic(
            api_key=settings.anthropic_api_key, max_retries=0,
        )
        self._sleep = sleep  # injectable so selftest can count backoffs without waiting

    def handle(self, raw_text: str, channel: str = "web") -> Analysis:
        trail: list[dict[str, Any]] = []

        request = ServiceRequest.new(raw_text=normalize_text(raw_text), channel=channel)
        self._record(trail, "received", request=asdict(request), raw_chars=len(raw_text))

        try:
            checks = self._validate(request)
        except ValidationError as exc:
            # A rejection is persisted, not vanished: finish the trail with
            # status rejected and hand it to the caller on the exception.
            self._record(trail, "validated", status="rejected", problems=str(exc))
            self._record(trail, "rejected", reason=str(exc))
            exc.audit = trail
            log.info("%s rejected: %s", request.request_id, exc)
            raise
        self._record(trail, "validated", status="passed", checks=checks)

        area = scan_service_area_mentions(request.raw_text)
        self._record(trail, "service_area", **area)

        life_safety_terms = scan_life_safety_terms(request.raw_text)
        self._record(trail, "life_safety_screen", matched_terms=life_safety_terms,
                     triggered=bool(life_safety_terms))

        hazard_terms = scan_hazard_terms(request.raw_text)
        self._record(trail, "hazard_screen", matched_terms=hazard_terms,
                     triggered=bool(hazard_terms))

        if hazard_terms:
            # Deterministic tier: hazmat phrasing never reaches the LLM. The
            # 1.0 confidence also guarantees _finalize spends no escalation
            # reread — the human flag is already set and stays set.
            analysis = Analysis(
                request_id=request.request_id,
                request_type=RequestType.BIOHAZARD_CLEANUP,
                confidence=1.0,
                escalate_to_human=True,
                notes=(f"Hazardous-material terms detected ({', '.join(hazard_terms)}); "
                       "routed to biohazard cleanup and flagged for human review "
                       "before dispatch."),
            )
            classify_detail: dict[str, Any] = {"classifier": "hazard_term_screen",
                                               "matched_terms": hazard_terms}
        else:
            analysis, classify_detail = self._classify(request)
            classify_detail = {"classifier": "llm", **classify_detail}
        self._assert_invariants(request, analysis)
        self._record(
            trail,
            "classified",
            request_type=analysis.request_type.value,
            confidence=analysis.confidence,
            # Which exact prompt/threshold artifact made this read — on every
            # classified step, deterministic and LLM paths alike.
            prompt_config_version=PROMPT_CONFIG.version,
            prompt_config_fingerprint=PROMPT_CONFIG.fingerprint(),
            **classify_detail,
        )

        analysis, finalize_detail = self._finalize(request, analysis)
        if life_safety_terms:
            # Code-owned enforcement, applied to whatever object leaves the
            # confidence gate (a reread builds a fresh Analysis): a life-safety
            # request always reaches a human, whatever the model said.
            analysis.life_safety = True
            analysis.escalate_to_human = True

        # Respond stage: a reviewed customer-facing reply. Runs only after a
        # successful LLM classification — the deterministic hazard path keeps
        # its zero-API-call guarantee, and with no read there is nothing safe
        # to say beyond the dispatcher note (those requests are already
        # escalated to a human).
        if classify_detail.get("classifier") == "llm" and classify_detail.get("model_used"):
            self._respond(request, analysis, trail)
        else:
            reason = ("classification_unavailable" if classify_detail.get("classifier") == "llm"
                      else "deterministic_hazard_path")
            self._record(trail, "customer_reply", source="skipped", reason=reason, reply=None)

        self._assert_invariants(request, analysis)
        self._record(
            trail,
            "finalized",
            request_type=analysis.request_type.value,
            confidence=analysis.confidence,
            escalate_to_human=analysis.escalate_to_human,
            life_safety=analysis.life_safety,
            # finalize_detail carries confidence_threshold + threshold_source:
            # the effective (per-class aware) bar the returned read was gated on.
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
        assert analysis.life_safety in (True, False), (
            f"life_safety must be bool, got {analysis.life_safety!r}"
        )
        assert not analysis.life_safety or analysis.escalate_to_human, (
            "life_safety=True must always carry escalate_to_human=True"
        )

    def _create_with_retry(self, **kwargs: Any) -> "tuple[Any, int]":
        """messages.create with exponential backoff on transient provider errors.

        Returns (response, retries_used) so the audit trail records how hard the
        call had to work. Non-transient errors (auth, bad request, refusal-shaped
        SDK errors, ...) raise immediately, and the last transient error raises
        once retries are exhausted — either way the caller's model fallback
        chain takes over. Never sleeps on the final failure.
        """
        retries = self.settings.max_retries
        for attempt in range(retries + 1):
            try:
                return self._client.messages.create(**kwargs), attempt
            except TRANSIENT_API_ERRORS as exc:
                if attempt == retries:
                    raise
                delay = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * 2 ** attempt)
                delay *= 0.5 + random.random() / 2  # jitter to 50-100% of target
                log.warning("transient API error via %s (%s: %s); retry %d/%d in %.1fs",
                            kwargs.get("model"), type(exc).__name__, exc,
                            attempt + 1, retries, delay)
                self._sleep(delay)
        raise AssertionError("unreachable: retry loop either returns or raises")

    def _classify_with_model(self, request: ServiceRequest, model: str) -> tuple[Analysis, dict[str, Any]]:
        """One classification attempt against `model` (transient provider errors
        are retried in place — see _create_with_retry). Raises ClassificationError
        (or lets an SDK/parse exception propagate) on any failure — the caller
        decides whether to fall back to the next model."""
        response, retries_used = self._create_with_retry(
            model=model,
            max_tokens=512,
            system=PROMPT_CONFIG.classification_system_prompt,
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
        detail = {
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "retries": retries_used,
        }
        return analysis, detail

    def _classify(self, request: ServiceRequest) -> tuple[Analysis, dict[str, Any]]:
        """Stage 4 — LLM classification. Tries primary_model, then falls back to
        fallback_model on any failure (rate limit, refusal, malformed output, ...).
        If both fail, degrades to the same fail-safe stub this method returned
        before stage 4 existed: unknown type, zero confidence, escalate on.
        """
        attempts: list[dict[str, Any]] = []
        for model in (self.settings.primary_model, self.settings.fallback_model):
            try:
                analysis, detail = self._classify_with_model(request, model)
            except Exception as exc:
                log.warning("classification via %s failed for %s: %s: %s",
                            model, request.request_id, type(exc).__name__, exc)
                attempts.append({"model": model, "error": f"{type(exc).__name__}: {exc}"})
                continue
            attempts.append({"model": model, **detail})
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
        """Stage 5 — confidence-threshold escalation, per-class aware.

        A read at or above its effective threshold — the global default, or a
        higher per-class floor for safety-relevant classes (see
        effective_confidence_threshold) — passes through untouched. Below it,
        get one reread from escalation_model (opus) — a second, stronger
        opinion, judged against the bar for whatever class *it* reports. If
        that reread is itself still below its threshold, or the reread attempt
        fails outright, route to a human — but keep whichever automated read
        we have (the reread if we got one, otherwise the original), rather
        than discarding it. The detail always records the threshold that
        gated the returned read, and where it came from.
        """
        threshold, source = effective_confidence_threshold(
            analysis.request_type, self.settings.confidence_threshold)
        if analysis.confidence >= threshold:
            return analysis, {"action": "passed",
                              "confidence_threshold": threshold, "threshold_source": source}

        log.info("%s below confidence threshold (%.2f < %.2f [%s]); escalating to %s",
                  request.request_id, analysis.confidence, threshold, source,
                  self.settings.escalation_model)
        try:
            reread, reread_detail = self._classify_with_model(request, self.settings.escalation_model)
        except Exception as exc:
            log.warning("escalation reread via %s failed for %s: %s: %s",
                        self.settings.escalation_model, request.request_id, type(exc).__name__, exc)
            analysis.escalate_to_human = True
            analysis.notes = f"{analysis.notes} (escalation reread failed — routed to human review)"
            return analysis, {
                "action": "escalation_failed",
                "escalation_model": self.settings.escalation_model,
                "error": f"{type(exc).__name__}: {exc}",
                "confidence_threshold": threshold,
                "threshold_source": source,
            }

        # The reread may land on a different class than the original read did;
        # it is judged against the bar for the class it actually reports.
        threshold, source = effective_confidence_threshold(
            reread.request_type, self.settings.confidence_threshold)
        if reread.confidence < threshold:
            log.info("%s escalation reread still below threshold (%.2f < %.2f [%s]); routing to human",
                      request.request_id, reread.confidence, threshold, source)
            reread.escalate_to_human = True
            return reread, {
                "action": "escalated_to_human",
                "escalation_model": self.settings.escalation_model,
                "confidence_threshold": threshold,
                "threshold_source": source,
                **reread_detail,
            }

        log.info("%s escalation reread resolved: %s at %.2f (threshold %.2f [%s])",
                  request.request_id, reread.request_type.value, reread.confidence,
                  threshold, source)
        return reread, {
            "action": "escalated_resolved",
            "escalation_model": self.settings.escalation_model,
            "confidence_threshold": threshold,
            "threshold_source": source,
            **reread_detail,
        }

    def _respond(self, request: ServiceRequest, analysis: Analysis,
                 trail: "list[dict[str, Any]]") -> None:
        """Respond stage — customer reply behind a review gate.

        The responder drafts a short customer-facing reply (primary model);
        the review gate checks it against the hard checklist (no pricing or
        timeline promises, no liability admissions, life-safety replies open
        with the 911 line, plain respectful tone). One revision loop max; an
        unreviewable or twice-rejected draft falls back to the dispatcher note
        and the request routes to a human. Every draft, every verdict (with
        reasons), and the final reply are recorded as audit steps. A reply
        failure never crashes the pipeline.
        """
        reasons: list[str] = []
        revision = 0
        for revision in (0, 1):
            try:
                draft, draft_detail = self._draft_reply(request, analysis, reasons)
            except Exception as exc:
                log.warning("reply draft failed for %s: %s: %s",
                            request.request_id, type(exc).__name__, exc)
                self._record(trail, "reply_drafted", revision=revision, reply=None,
                             error=f"{type(exc).__name__}: {exc}")
                reasons = ["draft unavailable — responder call failed"]
                break
            self._record(trail, "reply_drafted", revision=revision, reply=draft, **draft_detail)

            try:
                approved, reasons, review_detail = self._review_reply(request, analysis, draft)
            except Exception as exc:
                log.warning("reply review failed for %s: %s: %s",
                            request.request_id, type(exc).__name__, exc)
                self._record(trail, "reply_reviewed", revision=revision, approved=False,
                             reasons=["review unavailable — an unreviewed reply is never sent"],
                             error=f"{type(exc).__name__}: {exc}")
                reasons = ["review unavailable — an unreviewed reply is never sent"]
                break
            self._record(trail, "reply_reviewed", revision=revision, approved=approved,
                         reasons=reasons, **review_detail)

            if approved:
                analysis.customer_reply = draft
                self._record(trail, "customer_reply", source="drafted",
                             revisions_used=revision, reply=draft)
                return

        analysis.customer_reply = analysis.notes
        analysis.escalate_to_human = True
        self._record(trail, "customer_reply", source="fallback_dispatcher_note",
                     revisions_used=revision, reasons=reasons, reply=analysis.notes)

    def _draft_reply(self, request: ServiceRequest, analysis: Analysis,
                     feedback: "list[str]") -> "tuple[str, dict[str, Any]]":
        """One responder attempt on the primary model. Raises on any failure —
        _respond decides what a failed draft means."""
        directives: dict[str, Any] = {
            "customer_request": request.raw_text,
            "request_type": analysis.request_type.value,
            "life_safety": analysis.life_safety,
            "escalate_to_human": analysis.escalate_to_human,
            "dispatcher_note": analysis.notes,
        }
        if feedback:
            directives["reviewer_feedback_on_previous_draft"] = feedback
        response, retries_used = self._create_with_retry(
            model=self.settings.primary_model,
            max_tokens=300,
            system=PROMPT_CONFIG.responder_system_prompt,
            messages=[{"role": "user", "content": json.dumps(directives, ensure_ascii=False)}],
        )
        if response.stop_reason == "refusal":
            raise ClassificationError(f"{self.settings.primary_model} refused the reply draft")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None or not text.strip():
            raise ClassificationError(
                f"{self.settings.primary_model} returned no reply text "
                f"(stop_reason={response.stop_reason})")
        detail = {
            "model_used": self.settings.primary_model,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "retries": retries_used,
        }
        return text.strip(), detail

    def _review_reply(self, request: ServiceRequest, analysis: Analysis,
                      draft: str) -> "tuple[bool, list[str], dict[str, Any]]":
        """The review gate for one draft: (approved, reasons, audit detail).

        The life-safety 911 opener is checked deterministically in code first —
        never model-dependent; a miss rejects without spending a critic call.
        The model critic then judges the soft checklist items. Raises on a
        failed critic call — _respond treats that as unreviewable.
        """
        if analysis.life_safety and not draft.startswith(LIFE_SAFETY_REPLY_OPENER):
            return False, [f'life-safety reply must open with "{LIFE_SAFETY_REPLY_OPENER}"'], \
                {"checker": "deterministic_opener_check"}

        payload = {
            "customer_request": request.raw_text,
            "request_type": analysis.request_type.value,
            "life_safety": analysis.life_safety,
            "escalate_to_human": analysis.escalate_to_human,
            "draft_reply": draft,
        }
        response, retries_used = self._create_with_retry(
            model=self.settings.primary_model,
            max_tokens=300,
            system=PROMPT_CONFIG.reply_critic_system_prompt,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            output_config={"format": {"type": "json_schema", "schema": REPLY_CRITIC_SCHEMA}},
        )
        if response.stop_reason == "refusal":
            raise ClassificationError(f"{self.settings.primary_model} refused the reply review")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ClassificationError(
                f"{self.settings.primary_model} returned no review verdict "
                f"(stop_reason={response.stop_reason})")
        data = json.loads(text)
        approved = bool(data["approved"])
        reasons = [str(reason) for reason in data["reasons"]]
        if not approved and not reasons:
            reasons = ["rejected by the critic without stated reasons"]
        detail = {
            "checker": "critic_llm",
            "model_used": self.settings.primary_model,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "retries": retries_used,
        }
        return approved, reasons, detail


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
            self._send_json(400, exc.to_dict())  # status rejected + the finished trail
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
    """Run both dev servers: UI on :3000, JSON API on :8000.

    Binds 127.0.0.1 by default (local dev). Set INTAKE_BIND_HOST=0.0.0.0 when
    the servers must be reachable across a network namespace boundary — e.g.
    published ports on a Docker container. Still no auth: keep a reverse proxy
    in front of anything non-local.
    """
    host = os.environ.get("INTAKE_BIND_HOST", "127.0.0.1")
    ApiHandler.agent = IntakeAgent(settings)
    UiHandler.api_port = api_port

    try:
        api_server = ThreadingHTTPServer((host, api_port), ApiHandler)
    except OSError as exc:
        log.error("cannot bind API %s:%d (already in use?): %s", host, api_port, exc)
        return 1
    try:
        ui_server = ThreadingHTTPServer((host, ui_port), UiHandler)
    except OSError as exc:
        log.error("cannot bind UI %s:%d (already in use?): %s", host, ui_port, exc)
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
    assert settings.max_retries == DEFAULT_MAX_RETRIES
    summary = settings.summary()
    assert summary["anthropic_api_key"] == "set", "summary must report key presence only"
    assert "test-key" not in json.dumps(summary), "summary must never contain the key"
    for bad_threshold in ("nope", "1.5", "-0.1"):
        try:
            Settings.from_env({"ANTHROPIC_API_KEY": "k", "INTAKE_CONFIDENCE_THRESHOLD": bad_threshold})
            raise AssertionError(f"bad threshold must raise ConfigError: {bad_threshold}")
        except ConfigError:
            pass
    for bad_retries in ("nope", "2.5", "-1", "99"):
        try:
            Settings.from_env({"ANTHROPIC_API_KEY": "k", "INTAKE_MAX_RETRIES": bad_retries})
            raise AssertionError(f"bad retries must raise ConfigError: {bad_retries}")
        except ConfigError:
            pass

    # versioned prompt/config surface (the promotion artifact): explicit
    # version, content fingerprint, and the public module-level names staying
    # exactly aliased to it — backward compatibility is load-bearing (web.py,
    # the eval runners, and this selftest all consume the module-level names).
    assert re.fullmatch(r"\d+\.\d+\.\d+", PROMPT_CONFIG.version), PROMPT_CONFIG.version
    assert PROMPT_CONFIG.classification_system_prompt == CLASSIFICATION_SYSTEM_PROMPT
    assert PROMPT_CONFIG.responder_system_prompt == RESPONDER_SYSTEM_PROMPT
    assert PROMPT_CONFIG.reply_critic_system_prompt == REPLY_CRITIC_SYSTEM_PROMPT
    assert PROMPT_CONFIG.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD
    assert settings.confidence_threshold == PROMPT_CONFIG.confidence_threshold, \
        "default Settings must inherit the versioned threshold"
    fingerprint = PROMPT_CONFIG.fingerprint()
    assert re.fullmatch(r"[0-9a-f]{12}", fingerprint), fingerprint
    assert fingerprint == PROMPT_CONFIG.fingerprint(), "fingerprint must be deterministic"
    assert replace(PROMPT_CONFIG, confidence_threshold=0.71).fingerprint() != fingerprint, \
        "fingerprint must change when the surface's content changes"
    assert replace(PROMPT_CONFIG, version="9.9.9").fingerprint() != fingerprint, \
        "fingerprint covers the version string too"
    assert summary["prompt_config"] == PROMPT_CONFIG.summary(), summary
    assert PROMPT_CONFIG.classification_system_prompt not in json.dumps(summary), \
        "summaries carry version + fingerprint, never prompt text"

    # per-class confidence floors (config 1.2.0): the documented defaults —
    # safety-relevant classes demand more confidence, everything else falls
    # back to the global 0.70. These asserts PIN the shipped values; changing
    # them means bumping PROMPT_CONFIG.version and re-passing the eval gate.
    assert set(PROMPT_CONFIG.class_confidence_thresholds) == \
        {RequestType.BIOHAZARD_CLEANUP.value, RequestType.FIRE_SMOKE_DAMAGE.value}, \
        PROMPT_CONFIG.class_confidence_thresholds
    assert all(DEFAULT_CONFIDENCE_THRESHOLD < floor <= 1.0
               for floor in PROMPT_CONFIG.class_confidence_thresholds.values()), \
        "class entries are floors — they must sit above the global default"
    assert effective_confidence_threshold(RequestType.BIOHAZARD_CLEANUP, 0.70) == \
        (0.85, "class:biohazard_cleanup")
    assert effective_confidence_threshold(RequestType.FIRE_SMOKE_DAMAGE, 0.70) == \
        (0.85, "class:fire_smoke_damage")
    assert effective_confidence_threshold(RequestType.WATER_DAMAGE, 0.70) == (0.70, "global")
    assert effective_confidence_threshold(RequestType.UNKNOWN, 0.70) == (0.70, "global")
    # a globally-raised threshold is never undercut by a class entry
    assert effective_confidence_threshold(RequestType.BIOHAZARD_CLEANUP, 0.95) == (0.95, "global")
    assert replace(PROMPT_CONFIG, class_confidence_thresholds={}).fingerprint() != fingerprint, \
        "fingerprint covers the per-class floors"

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

    # hazard-material screen: the deterministic tier ahead of the LLM.
    # First case is the live-bug regression phrasing (previously misclassified).
    assert scan_hazard_terms("Nuclear material has spilled all over our yard!") == ["nuclear"]
    assert scan_hazard_terms("worried about ASBESTOS in the ceiling tiles") == ["asbestos"]
    assert scan_hazard_terms("sewer line backed up, raw sewage everywhere") == ["sewage", "sewer"]
    assert scan_hazard_terms("radioactive waste — possible radiation exposure") == ["radiation", "radioactive"]
    assert scan_hazard_terms("cleaning chemicals spilled in the flooded garage") == ["chemical"]
    assert scan_hazard_terms("this is a bio-hazard situation, hazmat gear needed") == ["biohazard", "hazmat"]
    assert scan_hazard_terms("someone dumped toxic waste by our fence") == ["toxic"]
    assert scan_hazard_terms(SAMPLE_REQUEST) == []
    assert scan_hazard_terms("black mold behind the tile and a musty smell") == []  # mold-tier, not hazmat
    assert scan_hazard_terms("the source of the leak is unclear to us") == []       # no substring tricks

    # life-safety screen: active-emergency phrasing, deterministic (product pass).
    # Past-tense fire/smoke damage is routine intake and must NOT trigger — the
    # built-in eval texts below are the canonical negatives.
    assert scan_life_safety_terms("Our house is ON FIRE right now!") == ["active_fire"]
    assert scan_life_safety_terms("there are flames coming out of the kitchen") == ["active_fire"]
    assert scan_life_safety_terms("the attic is still burning a little") == ["active_fire"]
    assert scan_life_safety_terms("we smell gas near the water heater") == ["gas_leak"]
    assert scan_life_safety_terms("I think the gas is leaking behind the stove") == ["gas_leak"]
    assert scan_life_safety_terms("our carbon monoxide detector keeps going off") == ["carbon_monoxide"]
    assert scan_life_safety_terms("the CO alarm went off twice tonight") == ["carbon_monoxide"]
    assert scan_life_safety_terms("smoke is filling the hallway, should we call 911?") == \
        ["active_smoke", "emergency_911"]
    assert scan_life_safety_terms(SAMPLE_REQUEST) == []
    assert scan_life_safety_terms("Grease fire in the kitchen last night, whole house "
                                  "smells like smoke and there's soot on the ceiling.") == []
    assert scan_life_safety_terms("We had an electrical fire in the garage, everything "
                                  "in there is charred and smells like smoke.") == []
    assert scan_life_safety_terms("the flamespray coating co. redid our deck") == []  # no substring tricks

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

    # Reply-stage plumbing for the classification-focused tests: the product
    # pass added a responder + critic after classification, so classification
    # fakes are wrapped to answer those calls with a well-formed draft (911
    # opener included, so life-safety paths approve) and an approving verdict.
    # Reply calls stay invisible to each test's `calls` list — those asserts
    # remain exact statements about the classification chain; the reply flow
    # has its own dedicated tests below.
    canned_reply = (f"{LIFE_SAFETY_REPLY_OPENER} Thank you for reaching out — our team "
                    "has your request and will be in touch shortly.")

    def _reply_aware(classify_handler):
        def routed(**kwargs):
            if kwargs.get("system") == RESPONDER_SYSTEM_PROMPT:
                return _FakeResponse(canned_reply)
            if kwargs.get("system") == REPLY_CRITIC_SYSTEM_PROMPT:
                return _FakeResponse(json.dumps({"approved": True, "reasons": []}))
            return classify_handler(**kwargs)
        return routed

    # success on the primary model
    calls: list[str] = []

    def primary_ok(**kwargs):
        calls.append(kwargs["model"])
        return _json_response("water_damage", 0.87, "Explicit flooding and standing water reported.")

    agent_ok = IntakeAgent(settings, client=_FakeClient(_reply_aware(primary_ok)))
    analysis = agent_ok.handle(SAMPLE_REQUEST)
    assert analysis.request_type is RequestType.WATER_DAMAGE
    assert analysis.confidence == 0.87
    assert analysis.escalate_to_human is False
    assert analysis.notes == "Explicit flooding and standing water reported."
    assert calls == [settings.primary_model], calls
    classified_step = next(s for s in analysis.audit if s["step"] == "classified")
    assert classified_step["model_used"] == settings.primary_model, classified_step
    assert classified_step["classifier"] == "llm", classified_step
    assert classified_step["prompt_config_version"] == PROMPT_CONFIG.version, classified_step
    assert classified_step["prompt_config_fingerprint"] == PROMPT_CONFIG.fingerprint(), classified_step
    hazard_step = next(s for s in analysis.audit if s["step"] == "hazard_screen")
    assert hazard_step["triggered"] is False and hazard_step["matched_terms"] == [], hazard_step
    assert analysis.to_dict()["status"] == "analyzed"

    # hazmat phrasing (the live-bug regression case): deterministic screen
    # classifies + escalates and the LLM is never consulted
    calls = []

    def llm_must_not_be_called(**kwargs):
        calls.append(kwargs["model"])
        return _json_response("water_damage", 0.99, "This read must never be used.")

    agent_hazard = IntakeAgent(settings, client=_FakeClient(llm_must_not_be_called))
    analysis = agent_hazard.handle("Nuclear material has spilled all over our yard, please help us!")
    assert analysis.request_type is RequestType.BIOHAZARD_CLEANUP
    assert analysis.confidence == 1.0
    assert analysis.escalate_to_human is True
    assert "nuclear" in analysis.notes
    assert calls == [], f"hazard-screened request must not reach the LLM: {calls}"
    hazard_step = next(s for s in analysis.audit if s["step"] == "hazard_screen")
    assert hazard_step["triggered"] is True and hazard_step["matched_terms"] == ["nuclear"], hazard_step
    classified_step = next(s for s in analysis.audit if s["step"] == "classified")
    assert classified_step["classifier"] == "hazard_term_screen", classified_step
    assert classified_step["prompt_config_version"] == PROMPT_CONFIG.version, \
        "every classified step carries the config version — deterministic paths too"
    assert classified_step["prompt_config_fingerprint"] == PROMPT_CONFIG.fingerprint(), classified_step
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["escalate_to_human"] is True, finalized_step
    assert analysis.customer_reply is None, "hazard path must make zero LLM calls, so no draft"
    reply_step = next(s for s in analysis.audit if s["step"] == "customer_reply")
    assert reply_step["source"] == "skipped", reply_step
    assert reply_step["reason"] == "deterministic_hazard_path", reply_step
    json.dumps(analysis.to_dict())  # deterministic path must stay JSON-serializable too

    # --- Life-safety screen (product pass): flag + forced human escalation, ---
    # --- while the LLM still classifies the request type normally           ---

    calls = []

    def fire_read(**kwargs):
        calls.append(kwargs["model"])
        return _json_response("fire_smoke_damage", 0.97, "Active structure fire reported.")

    agent_fire = IntakeAgent(settings, client=_FakeClient(_reply_aware(fire_read)))
    analysis = agent_fire.handle("Our house is on fire right now, flames in the kitchen!")
    assert analysis.life_safety is True
    assert analysis.escalate_to_human is True, "life safety must force human escalation"
    assert analysis.request_type is RequestType.FIRE_SMOKE_DAMAGE, "LLM classification must still run"
    assert calls == [settings.primary_model], calls
    ls_step = next(s for s in analysis.audit if s["step"] == "life_safety_screen")
    assert ls_step["triggered"] is True and "active_fire" in ls_step["matched_terms"], ls_step
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["life_safety"] is True and finalized_step["escalate_to_human"] is True
    assert analysis.to_dict()["life_safety"] is True
    json.dumps(analysis.to_dict())

    # no life-safety phrasing -> flag stays False, nothing forced, step still recorded
    analysis = agent_ok.handle(SAMPLE_REQUEST)
    assert analysis.life_safety is False and analysis.escalate_to_human is False
    ls_step = next(s for s in analysis.audit if s["step"] == "life_safety_screen")
    assert ls_step["triggered"] is False and ls_step["matched_terms"] == [], ls_step
    assert analysis.to_dict()["life_safety"] is False

    # the flag survives a low-confidence escalation reread (which builds a fresh
    # Analysis) — enforcement applies to whatever object leaves the pipeline
    def low_fire_then_resolved(**kwargs):
        if kwargs["model"] == settings.escalation_model:
            return _json_response("fire_smoke_damage", 0.93, "Escalated read: active fire.")
        return _json_response("fire_smoke_damage", 0.4, "Uncertain primary read.")

    agent_fire_low = IntakeAgent(settings, client=_FakeClient(_reply_aware(low_fire_then_resolved)))
    analysis = agent_fire_low.handle("Smoke is everywhere upstairs and we can smell gas!")
    assert analysis.life_safety is True and analysis.escalate_to_human is True
    assert analysis.confidence == 0.93, "reread result must be kept, flag applied on top"
    ls_step = next(s for s in analysis.audit if s["step"] == "life_safety_screen")
    assert ls_step["matched_terms"] == ["active_smoke", "gas_leak"], ls_step

    # life-safety + hazmat co-trigger: hazard short-circuit owns classification
    # (zero LLM calls), and the life-safety flag is still set on the result
    calls = []

    def must_not_be_used(**kwargs):
        calls.append(kwargs["model"])
        return _json_response("water_damage", 0.99, "This read must never be used.")

    agent_both = IntakeAgent(settings, client=_FakeClient(must_not_be_used))
    analysis = agent_both.handle("There's a gas leak and chemical fumes are filling the basement")
    assert analysis.request_type is RequestType.BIOHAZARD_CLEANUP
    assert analysis.life_safety is True and analysis.escalate_to_human is True
    assert calls == [], f"hazard+life-safety request must not reach the LLM: {calls}"
    assert analysis.customer_reply is None, "deterministic path must stay zero-API-call"

    # --- Customer reply with a review gate (product pass): responder drafts, ---
    # --- critic reviews against the hard checklist, one revision loop max    ---

    # clean path: draft approved first pass; draft, verdict, and final reply all
    # recorded; the whole flow runs on the primary model
    calls = []

    def reply_flow(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["system"] == RESPONDER_SYSTEM_PROMPT:
            return _FakeResponse("Thank you for reaching out — our team is reviewing "
                                 "your request and will contact you shortly.")
        if kwargs["system"] == REPLY_CRITIC_SYSTEM_PROMPT:
            return _FakeResponse(json.dumps({"approved": True, "reasons": []}))
        return _json_response("water_damage", 0.9, "Flooding reported.")

    agent_reply = IntakeAgent(settings, client=_FakeClient(reply_flow))
    analysis = agent_reply.handle(SAMPLE_REQUEST)
    assert analysis.customer_reply == ("Thank you for reaching out — our team is reviewing "
                                       "your request and will contact you shortly.")
    assert analysis.escalate_to_human is False
    assert calls == [settings.primary_model] * 3, calls  # classify, draft, critique
    drafted_steps = [s for s in analysis.audit if s["step"] == "reply_drafted"]
    reviewed_steps = [s for s in analysis.audit if s["step"] == "reply_reviewed"]
    reply_step = next(s for s in analysis.audit if s["step"] == "customer_reply")
    assert len(drafted_steps) == 1 and drafted_steps[0]["revision"] == 0
    assert drafted_steps[0]["reply"] == analysis.customer_reply
    assert len(reviewed_steps) == 1 and reviewed_steps[0]["approved"] is True
    assert reviewed_steps[0]["reasons"] == []
    assert reply_step["source"] == "drafted" and reply_step["revisions_used"] == 0
    assert reply_step["reply"] == analysis.customer_reply
    assert analysis.audit[-1]["step"] == "finalized", "finalized stays the terminal step"
    assert analysis.to_dict()["customer_reply"] == analysis.customer_reply
    json.dumps(analysis.to_dict())

    # rejected once with reasons -> revision prompt carries the critic's
    # feedback -> second draft approved
    responder_prompts = []

    def reply_revise_flow(**kwargs):
        if kwargs["system"] == RESPONDER_SYSTEM_PROMPT:
            responder_prompts.append(kwargs["messages"][0]["content"])
            if len(responder_prompts) == 1:
                return _FakeResponse("We will be there tomorrow and it will cost $500.")
            return _FakeResponse("Thank you for reaching out — our team will contact you shortly.")
        if kwargs["system"] == REPLY_CRITIC_SYSTEM_PROMPT:
            if "$500" in kwargs["messages"][0]["content"]:
                return _FakeResponse(json.dumps(
                    {"approved": False, "reasons": ["promises a price and a timeline"]}))
            return _FakeResponse(json.dumps({"approved": True, "reasons": []}))
        return _json_response("water_damage", 0.9, "Flooding reported.")

    agent_revise = IntakeAgent(settings, client=_FakeClient(reply_revise_flow))
    analysis = agent_revise.handle(SAMPLE_REQUEST)
    assert analysis.customer_reply == "Thank you for reaching out — our team will contact you shortly."
    assert analysis.escalate_to_human is False
    assert len(responder_prompts) == 2
    assert "promises a price and a timeline" in responder_prompts[1], \
        "revision prompt must carry the critic's reasons"
    reviewed_steps = [s for s in analysis.audit if s["step"] == "reply_reviewed"]
    assert [s["approved"] for s in reviewed_steps] == [False, True], reviewed_steps
    assert reviewed_steps[0]["reasons"] == ["promises a price and a timeline"]
    reply_step = next(s for s in analysis.audit if s["step"] == "customer_reply")
    assert reply_step["source"] == "drafted" and reply_step["revisions_used"] == 1

    # rejected twice -> dispatcher-note fallback + human escalation; exactly one
    # revision is ever attempted
    def reply_never_good(**kwargs):
        if kwargs["system"] == RESPONDER_SYSTEM_PROMPT:
            return _FakeResponse("We guarantee arrival within the hour; this was our fault.")
        if kwargs["system"] == REPLY_CRITIC_SYSTEM_PROMPT:
            return _FakeResponse(json.dumps({"approved": False, "reasons": ["liability admission"]}))
        return _json_response("water_damage", 0.9, "Flooding reported; dispatcher note text.")

    agent_reply_fallback = IntakeAgent(settings, client=_FakeClient(reply_never_good))
    analysis = agent_reply_fallback.handle(SAMPLE_REQUEST)
    assert analysis.escalate_to_human is True, "an unapprovable reply must route to a human"
    assert analysis.customer_reply == analysis.notes, "fallback is the dispatcher note"
    assert len([s for s in analysis.audit if s["step"] == "reply_drafted"]) == 2
    reply_step = next(s for s in analysis.audit if s["step"] == "customer_reply")
    assert reply_step["source"] == "fallback_dispatcher_note", reply_step
    assert reply_step["reasons"] == ["liability admission"], reply_step
    json.dumps(analysis.to_dict())

    # the 911 opener on life-safety replies is enforced by CODE: even a critic
    # that approves a draft missing the opener cannot ship it
    def reply_missing_opener(**kwargs):
        if kwargs["system"] == RESPONDER_SYSTEM_PROMPT:
            return _FakeResponse("Our crew is being dispatched — hang tight.")
        if kwargs["system"] == REPLY_CRITIC_SYSTEM_PROMPT:
            return _FakeResponse(json.dumps({"approved": True, "reasons": []}))
        return _json_response("fire_smoke_damage", 0.97, "Active structure fire reported.")

    agent_no_opener = IntakeAgent(settings, client=_FakeClient(reply_missing_opener))
    analysis = agent_no_opener.handle("Our house is on fire right now!")
    assert analysis.life_safety is True
    assert analysis.customer_reply == analysis.notes and analysis.escalate_to_human is True
    reviewed_steps = [s for s in analysis.audit if s["step"] == "reply_reviewed"]
    assert reviewed_steps and all(s["approved"] is False for s in reviewed_steps), reviewed_steps
    assert any("911" in reason for s in reviewed_steps for reason in s["reasons"]), reviewed_steps

    # life-safety reply that does open correctly is approved and kept
    def reply_with_opener(**kwargs):
        if kwargs["system"] == RESPONDER_SYSTEM_PROMPT:
            return _FakeResponse(f"{LIFE_SAFETY_REPLY_OPENER} Our team has been alerted "
                                 "and a person is reviewing your request right now.")
        if kwargs["system"] == REPLY_CRITIC_SYSTEM_PROMPT:
            return _FakeResponse(json.dumps({"approved": True, "reasons": []}))
        return _json_response("fire_smoke_damage", 0.97, "Active structure fire reported.")

    agent_opener = IntakeAgent(settings, client=_FakeClient(reply_with_opener))
    analysis = agent_opener.handle("Flames are coming out of the attic right now!")
    assert analysis.life_safety is True and analysis.escalate_to_human is True
    assert analysis.customer_reply.startswith(LIFE_SAFETY_REPLY_OPENER)
    reply_step = next(s for s in analysis.audit if s["step"] == "customer_reply")
    assert reply_step["source"] == "drafted"

    # critic infrastructure failure -> never ship an unreviewed draft: fall back
    # to the dispatcher note and route to a human
    def critic_down(**kwargs):
        if kwargs["system"] == RESPONDER_SYSTEM_PROMPT:
            return _FakeResponse("Thanks — our team will reach out shortly.")
        if kwargs["system"] == REPLY_CRITIC_SYSTEM_PROMPT:
            raise RuntimeError("simulated critic outage")
        return _json_response("water_damage", 0.9, "Flooding reported.")

    agent_critic_down = IntakeAgent(settings, client=_FakeClient(critic_down))
    analysis = agent_critic_down.handle(SAMPLE_REQUEST)
    assert analysis.customer_reply == analysis.notes and analysis.escalate_to_human is True
    assert analysis.customer_reply != "Thanks — our team will reach out shortly."
    reply_step = next(s for s in analysis.audit if s["step"] == "customer_reply")
    assert reply_step["source"] == "fallback_dispatcher_note", reply_step

    # primary model fails -> falls back to the secondary model
    calls = []

    def primary_fails_then_ok(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == settings.primary_model:
            raise RuntimeError("simulated rate limit")
        # confidence kept >= settings.confidence_threshold so this stays a pure
        # test of the classify fallback chain — stage-5 escalation has its own tests
        return _json_response("mold_remediation", 0.85, "Musty odor and visible growth reported.")

    agent_fallback = IntakeAgent(settings, client=_FakeClient(_reply_aware(primary_fails_then_ok)))
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
    assert analysis.customer_reply is None, "no reply drafting without a classification"
    reply_step = next(s for s in analysis.audit if s["step"] == "customer_reply")
    assert reply_step["source"] == "skipped", reply_step
    assert reply_step["reason"] == "classification_unavailable", reply_step

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

    agent_clamped = IntakeAgent(settings, client=_FakeClient(_reply_aware(overconfident)))
    analysis = agent_clamped.handle(SAMPLE_REQUEST)
    assert analysis.confidence == 1.0, analysis.confidence

    # --- Stage 5: confidence-threshold escalation, also via a fake client ---

    # confidence already >= threshold -> passes straight through, no escalation call
    calls = []

    def high_confidence(**kwargs):
        calls.append(kwargs["model"])
        return _json_response("water_damage", 0.9, "High-confidence primary read.")

    agent_high = IntakeAgent(settings, client=_FakeClient(_reply_aware(high_confidence)))
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

    agent_escalated_ok = IntakeAgent(settings, client=_FakeClient(_reply_aware(low_then_resolved)))
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

    agent_escalated_human = IntakeAgent(settings, client=_FakeClient(_reply_aware(low_then_still_low)))
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

    agent_escalation_failed = IntakeAgent(settings,
                                          client=_FakeClient(_reply_aware(low_then_escalation_fails)))
    analysis = agent_escalation_failed.handle(SAMPLE_REQUEST)
    assert analysis.confidence == 0.3 and analysis.escalate_to_human is True
    assert analysis.request_type is RequestType.GENERAL_INQUIRY
    assert "routed to human review" in analysis.notes
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["action"] == "escalation_failed", finalized_step

    json.dumps(analysis.to_dict())  # output must stay JSON-serializable

    # --- Per-class confidence floors (config 1.2.0): reads that route near ---
    # --- safety-relevant classes must clear a higher bar than the global   ---

    # a fire read at 0.78 clears the 0.70 global but NOT the 0.85 fire floor:
    # it must take the Opus reread (resolved here at 0.97)
    calls = []

    def fire_borderline_resolved(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == settings.escalation_model:
            return _json_response("fire_smoke_damage", 0.97, "Escalated read: clear fire damage.")
        return _json_response("fire_smoke_damage", 0.78, "Borderline fire read.")

    agent_fire_floor = IntakeAgent(settings, client=_FakeClient(_reply_aware(fire_borderline_resolved)))
    analysis = agent_fire_floor.handle("Something in the garage smells scorched and there "
                                       "are dark marks up the drywall behind the freezer.")
    assert analysis.request_type is RequestType.FIRE_SMOKE_DAMAGE
    assert analysis.confidence == 0.97 and analysis.escalate_to_human is False
    assert calls == [settings.primary_model, settings.escalation_model], calls
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["action"] == "escalated_resolved", finalized_step
    assert finalized_step["confidence_threshold"] == 0.85, finalized_step
    assert finalized_step["threshold_source"] == "class:fire_smoke_damage", finalized_step

    # the same 0.78 on a cheap-to-misroute class passes at the global bar with
    # no escalation call — the contrast that defines the per-class floor
    calls = []

    def water_borderline(**kwargs):
        calls.append(kwargs["model"])
        return _json_response("water_damage", 0.78, "Borderline water read.")

    agent_water_borderline = IntakeAgent(settings, client=_FakeClient(_reply_aware(water_borderline)))
    analysis = agent_water_borderline.handle(SAMPLE_REQUEST)
    assert analysis.confidence == 0.78 and analysis.escalate_to_human is False
    assert calls == [settings.primary_model], calls
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["action"] == "passed", finalized_step
    assert finalized_step["confidence_threshold"] == DEFAULT_CONFIDENCE_THRESHOLD, finalized_step
    assert finalized_step["threshold_source"] == "global", finalized_step

    # a model-read biohazard call (no hazard phrasing, so the deterministic
    # screen stays out of it) stuck below its floor after the reread routes
    # to a human — and the trail says which bar it failed
    def biohazard_stuck(**kwargs):
        if kwargs["model"] == settings.escalation_model:
            return _json_response("biohazard_cleanup", 0.80, "Still unsure what the substance is.")
        return _json_response("biohazard_cleanup", 0.75, "Possibly a contamination issue.")

    agent_bio_stuck = IntakeAgent(settings, client=_FakeClient(_reply_aware(biohazard_stuck)))
    analysis = agent_bio_stuck.handle("A strange-smelling liquid keeps seeping into our "
                                      "crawlspace from the property next door.")
    assert analysis.request_type is RequestType.BIOHAZARD_CLEANUP
    assert analysis.confidence == 0.80 and analysis.escalate_to_human is True
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["action"] == "escalated_to_human", finalized_step
    assert finalized_step["confidence_threshold"] == 0.85, finalized_step
    assert finalized_step["threshold_source"] == "class:biohazard_cleanup", finalized_step

    # the reread is judged against the bar for the class IT reports: a shaky
    # biohazard read that rereads as water damage passes at the global bar
    def bio_low_then_water(**kwargs):
        if kwargs["model"] == settings.escalation_model:
            return _json_response("water_damage", 0.75, "Groundwater seepage, not a biohazard.")
        return _json_response("biohazard_cleanup", 0.75, "Possibly a contamination issue.")

    agent_cross_class = IntakeAgent(settings, client=_FakeClient(_reply_aware(bio_low_then_water)))
    analysis = agent_cross_class.handle(SAMPLE_REQUEST)
    assert analysis.request_type is RequestType.WATER_DAMAGE
    assert analysis.confidence == 0.75 and analysis.escalate_to_human is False
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["action"] == "escalated_resolved", finalized_step
    assert finalized_step["confidence_threshold"] == DEFAULT_CONFIDENCE_THRESHOLD, finalized_step
    assert finalized_step["threshold_source"] == "global", finalized_step

    # an env-raised global becomes the bar everywhere — class floors tighten,
    # they never loosen
    settings_tight = Settings.from_env({"ANTHROPIC_API_KEY": "test-key",
                                        "INTAKE_CONFIDENCE_THRESHOLD": "0.9"})

    def fire_above_floor_below_global(**kwargs):
        if kwargs["model"] == settings.escalation_model:
            return _json_response("fire_smoke_damage", 0.95, "Escalated read: fire damage.")
        return _json_response("fire_smoke_damage", 0.87, "Above the class floor, below the raised global.")

    agent_tight = IntakeAgent(settings_tight,
                              client=_FakeClient(_reply_aware(fire_above_floor_below_global)))
    analysis = agent_tight.handle(SAMPLE_REQUEST)
    assert analysis.confidence == 0.95 and analysis.escalate_to_human is False
    finalized_step = next(s for s in analysis.audit if s["step"] == "finalized")
    assert finalized_step["action"] == "escalated_resolved", finalized_step
    assert finalized_step["confidence_threshold"] == 0.9, finalized_step
    assert finalized_step["threshold_source"] == "global", finalized_step

    # --- Retry with backoff on transient provider errors (sleep injected) ---

    import httpx  # anthropic's own HTTP dependency; used only to build SDK errors

    def _transient() -> Exception:
        return anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))

    # two transient failures, then success — retried on the SAME model, with backoff
    calls, sleeps = [], []

    def flaky_then_ok(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) < 3:
            raise _transient()
        return _json_response("water_damage", 0.9, "Recovered after retries.")

    agent_flaky = IntakeAgent(settings, client=_FakeClient(_reply_aware(flaky_then_ok)),
                              sleep=sleeps.append)
    analysis = agent_flaky.handle(SAMPLE_REQUEST)
    assert analysis.request_type is RequestType.WATER_DAMAGE and analysis.escalate_to_human is False
    assert calls == [settings.primary_model] * 3, calls
    assert len(sleeps) == 2 and all(0 < s <= RETRY_CAP_SECONDS for s in sleeps), sleeps
    classified_step = next(s for s in analysis.audit if s["step"] == "classified")
    assert classified_step["attempts"][0]["retries"] == 2, classified_step

    # transient errors all the way down -> every model in the chain (primary,
    # fallback, then the 0.0-confidence stub's escalation reread) exhausts its
    # retries in order, and the request lands with a human (never a crash)
    calls, sleeps = [], []

    def always_transient(**kwargs):
        calls.append(kwargs["model"])
        raise _transient()

    agent_outage = IntakeAgent(settings, client=_FakeClient(always_transient), sleep=sleeps.append)
    analysis = agent_outage.handle(SAMPLE_REQUEST)
    assert analysis.request_type is RequestType.UNKNOWN and analysis.escalate_to_human is True
    per_model = settings.max_retries + 1
    assert calls == ([settings.primary_model] * per_model
                     + [settings.fallback_model] * per_model
                     + [settings.escalation_model] * per_model), calls
    assert len(sleeps) == 3 * settings.max_retries, sleeps

    # non-transient errors are NOT retried — no sleeps, straight down the fallback chain
    sleeps = []
    agent_hard_fail = IntakeAgent(settings, client=_FakeClient(always_fails), sleep=sleeps.append)
    analysis = agent_hard_fail.handle(SAMPLE_REQUEST)
    assert analysis.request_type is RequestType.UNKNOWN and analysis.escalate_to_human is True
    assert sleeps == [], sleeps

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
            # product pass: a rejection carries a finished, persistable trail
            # (status rejected) instead of discarding what was recorded
            assert exc.audit and exc.audit[0]["step"] == "received", exc.audit
            validated_step = next(s for s in exc.audit if s["step"] == "validated")
            assert validated_step["status"] == "rejected" and validated_step["problems"]
            rejected_step = next(s for s in exc.audit if s["step"] == "rejected")
            assert rejected_step["reason"] == str(exc)
            record = exc.to_dict()
            assert record["status"] == "rejected" and record["request_id"] == exc.request_id
            assert record["error"] == str(exc) and record["audit"] is exc.audit
            json.dumps(record)  # the rejected record must be store-ready too

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
            body = json.loads(err.read())
            assert err.code == 400 and "error" in body
            assert body["status"] == "rejected", body
            assert body["audit"] and body["audit"][-1]["step"] == "rejected", body

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

    print("selftest: all checks passed (deterministic layer, hazard + life-safety "
          "screens, versioned prompt config, LLM classification, retry/backoff, "
          "per-class confidence escalation, web handlers)")
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
        print(json.dumps(exc.to_dict(), indent=2))
        return 2

    print(json.dumps(analysis.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
