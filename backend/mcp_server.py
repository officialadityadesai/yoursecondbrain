#!/usr/bin/env python3
"""
My Second Brain — MCP Server
Connects Claude Desktop to your personal multimodal knowledge base.

This server is launched automatically by Claude Desktop (via stdio).
It calls the My Second Brain backend API running at http://127.0.0.1:8000.

Setup: run scripts/setup_mcp.bat once, then restart Claude Desktop.
"""

import sys
import re
import json
import os
import time
from threading import Lock
import urllib.parse


def _annotate_transcript(transcript: str, chunk_offset_seconds: int = 0) -> str:
    """
    Annotate each [MM:SS] timestamp in a transcript line with the absolute second value.
    e.g.  '[00:23] Mark: ...'  with offset 120  →  '[00:23=143s] Mark: ...'
    """
    result = []
    for line in transcript.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r'^\[(\d+):(\d{2})\]', stripped)
        if m:
            mins, secs = int(m.group(1)), int(m.group(2))
            abs_s = mins * 60 + secs + chunk_offset_seconds
            annotated = re.sub(
                r'^\[\d+:\d{2}\]',
                f'[{m.group(1)}:{m.group(2)}={abs_s}s]',
                stripped,
                count=1
            )
            result.append(annotated)
        else:
            result.append(stripped)
    return '\n'.join(result)


try:
    import requests as _requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: mcp not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)

BASE_URL = os.getenv("MSB_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = max(10.0, min(180.0, float(os.getenv("MSB_BACKEND_TIMEOUT_SECONDS", "90"))))
FALLBACK_GUARD_SECONDS = max(15.0, min(180.0, float(os.getenv("MSB_FALLBACK_GUARD_SECONDS", "45"))))
BACKEND_DISCOVERY_TTL_SECONDS = max(10.0, min(300.0, float(os.getenv("MSB_BACKEND_DISCOVERY_TTL_SECONDS", "45"))))
PUBLIC_BASE_URL_OVERRIDE = os.getenv("MSB_PUBLIC_BASE_URL", "").strip().rstrip("/")
STRICT_FALLBACK_REQUIRES_HOLISTIC = os.getenv("MSB_STRICT_FALLBACK_REQUIRES_HOLISTIC", "1").strip().lower() not in {"0", "false", "no", "off"}
SOURCES_BLOCK_MAX = max(3, min(10, int(os.getenv("MSB_SOURCES_BLOCK_MAX", "5"))))

_backend_lock = Lock()
_backend_cache = {
    "url": BASE_URL,
    "ts": 0.0,
}
_LAST_HOLISTIC_DECISION = {
    "query": "",
    "query_fingerprint": "",
    "fallback_required": None,
    "ts": 0.0,
    "fallback_budget": 0,
    "fallback_remaining": 0,
    "retry_after_empty_available": False,
    "fallback_retry_used": False,
    "last_fallback_empty_or_error": False,
    "fallback_attempts": 0,
}

# Tier 2 tracking: which files have already been fetched this session
_TIER2_FETCHED: set = set()
_TIER2_CALL_COUNT: int = 0
_TIER2_CALL_LIMIT: int = max(1, min(6, int(os.getenv("MSB_TIER2_CALL_LIMIT", "4"))))

# Tier 3 tracking: specialty tool call budget per holistic window
_TIER3_CALL_COUNT: int = 0
_TIER3_CALL_LIMIT: int = max(1, min(5, int(os.getenv("MSB_TIER3_CALL_LIMIT", "2"))))
_TIER3_WINDOW_TS: float = 0.0


def _backend_candidates() -> list[str]:
    explicit = os.getenv("MSB_BACKEND_URL", "").strip().rstrip("/")
    if explicit:
        return [explicit]

    ports_raw = os.getenv("MSB_BACKEND_CANDIDATE_PORTS", "8011,8000,8010")
    ports: list[int] = []
    for raw in ports_raw.split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            port = int(token)
        except Exception:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)

    if not ports:
        ports = [8000]
    return [f"http://127.0.0.1:{p}" for p in ports]


def _probe_backend(base_url: str) -> tuple[int, int]:
    score = 0
    latency_ms = 99_999
    start = time.perf_counter()

    try:
        r_sig = _requests.get(f"{base_url}/api/mcp/runtime_signature", timeout=min(4.0, TIMEOUT))
        if r_sig.status_code == 200:
            payload = r_sig.json()
            if isinstance(payload, dict) and payload.get("service") == "my-second-brain":
                score = 3
                features = payload.get("features", []) or []
                if "answer_first_contract_v2" in features:
                    score += 2
                if "process_sidecar_first" in features:
                    score += 1
    except Exception:
        pass

    try:
        r = _requests.get(f"{base_url}/api/mcp/files", timeout=min(8.0, TIMEOUT))
        if r.status_code != 200:
            return 0, latency_ms
        payload = r.json()
        if isinstance(payload, dict) and ("files" in payload or "error" in payload):
            score = max(score, 1)
            latency_ms = int((time.perf_counter() - start) * 1000)
            return score, latency_ms
        return 0, latency_ms
    except Exception:
        return 0, latency_ms


def _remember_backend(base_url: str):
    with _backend_lock:
        _backend_cache["url"] = base_url
        _backend_cache["ts"] = time.time()


def _resolve_backend(force_refresh: bool = False) -> str:
    now = time.time()
    with _backend_lock:
        cached_url = str(_backend_cache.get("url") or BASE_URL)
        cached_ts = float(_backend_cache.get("ts") or 0.0)
    if not force_refresh and (now - cached_ts) <= BACKEND_DISCOVERY_TTL_SECONDS:
        return cached_url

    candidates = _backend_candidates()
    best_url = ""
    best_score = -1
    best_latency = 99_999

    for candidate in candidates:
        score, latency_ms = _probe_backend(candidate)
        if score > best_score or (score == best_score and latency_ms < best_latency):
            best_url = candidate
            best_score = score
            best_latency = latency_ms

    if best_url and best_score > 0:
        _remember_backend(best_url)
        return best_url

    _remember_backend(candidates[0])
    return candidates[0]


def _public_base_url() -> str:
    if PUBLIC_BASE_URL_OVERRIDE:
        return PUBLIC_BASE_URL_OVERRIDE
    try:
        return _resolve_backend(force_refresh=False).rstrip("/")
    except Exception:
        return BASE_URL


def _absolute_url(path_or_url: str) -> str:
    value = str(path_or_url or "").strip()
    if not value:
        return _public_base_url()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if not value.startswith("/"):
        value = "/" + value
    return f"{_public_base_url()}{value}"


def _clip_preview_url(filename: str, start: int = 0, end: int = 30) -> str:
    enc = urllib.parse.quote(filename, safe="")
    return _absolute_url(f"/clip?file={enc}&start={int(start)}&end={int(end)}")


def _node_view_url(filename: str) -> str:
    enc = urllib.parse.quote(filename, safe="")
    return _absolute_url(f"/?node={enc}")


_FALLBACK_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_\-/]{1,32}", re.I)
_FALLBACK_STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "have", "has", "had",
    "what", "when", "where", "which", "does", "did", "into", "across", "about", "please",
    "query", "search", "find", "show", "give", "tell", "also", "just", "then", "than",
    "same", "none", "some", "tool", "tools", "fallback", "primary", "brain",
    "synthetic", "probe", "test", "testing",
}


def _fallback_fingerprint(text: str) -> str:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return ""
    parts = []
    for token in _FALLBACK_WORD_RE.findall(lowered):
        term = token.strip("-_/.")
        if len(term) < 3 or term in _FALLBACK_STOP_WORDS:
            continue
        parts.append(term)
    return " ".join(parts[:18])


def _fallback_tokens(text: str) -> set[str]:
    values: set[str] = set()
    for token in _fallback_fingerprint(text).split():
        values.add(token)
        if token.endswith("s") and len(token) > 4:
            values.add(token[:-1])
    return values


def _fallback_query_matches(tool_input: str, holistic_query: str) -> bool:
    candidate = str(tool_input or "").strip().lower()
    anchor = str(holistic_query or "").strip().lower()
    if not candidate or not anchor:
        return True
    if candidate == anchor or candidate in anchor or anchor in candidate:
        return True

    a = _fallback_tokens(candidate)
    b = _fallback_tokens(anchor)
    if not a or not b:
        return False

    overlap = len(a & b)
    if len(a) >= 3 and len(b) >= 3:
        union = len(a | b) or 1
        jaccard = overlap / union
        return jaccard >= 0.67

    needed = max(1, int(round(min(len(a), len(b)) * 0.5)))
    return overlap >= needed


def _request_candidates(force_refresh: bool = False) -> list[str]:
    primary = _resolve_backend(force_refresh=force_refresh)
    candidates = [primary]
    for c in _backend_candidates():
        if c not in candidates:
            candidates.append(c)
    return candidates


def _get(path: str) -> dict:
    errors = []
    for base in _request_candidates(force_refresh=False):
        try:
            r = _requests.get(f"{base}{path}", timeout=TIMEOUT)
            if r.status_code == 404:
                errors.append(f"{base}: 404")
                continue
            r.raise_for_status()
            _remember_backend(base)
            return r.json()
        except _requests.exceptions.ConnectionError:
            errors.append(f"{base}: connection_error")
            continue
        except Exception as e:
            errors.append(f"{base}: {str(e)}")

    tried = ", ".join(errors[:4]) if errors else "no endpoints tried"
    return {
        "error": (
            "Cannot connect to My Second Brain API endpoints. "
            f"Tried: {tried}. "
            "Make sure My Second Brain backend is running."
        )
    }


def _post(path: str, body: dict) -> dict:
    errors = []
    for base in _request_candidates(force_refresh=False):
        try:
            r = _requests.post(f"{base}{path}", json=body, timeout=TIMEOUT)
            if r.status_code == 404:
                errors.append(f"{base}: 404")
                continue
            r.raise_for_status()
            _remember_backend(base)
            return r.json()
        except _requests.exceptions.ConnectionError:
            errors.append(f"{base}: connection_error")
            continue
        except Exception as e:
            errors.append(f"{base}: {str(e)}")

    tried = ", ".join(errors[:4]) if errors else "no endpoints tried"
    return {
        "error": (
            "Cannot connect to My Second Brain API endpoints. "
            f"Tried: {tried}. "
            "Make sure My Second Brain backend is running."
        )
    }


def _record_holistic_decision(query: str, fallback_required: bool):
    _LAST_HOLISTIC_DECISION["query"] = str(query or "")
    _LAST_HOLISTIC_DECISION["query_fingerprint"] = _fallback_fingerprint(query)
    _LAST_HOLISTIC_DECISION["fallback_required"] = bool(fallback_required)
    _LAST_HOLISTIC_DECISION["ts"] = time.time()
    _LAST_HOLISTIC_DECISION["fallback_budget"] = 1 if fallback_required else 0
    _LAST_HOLISTIC_DECISION["fallback_remaining"] = 1 if fallback_required else 0
    _LAST_HOLISTIC_DECISION["retry_after_empty_available"] = bool(fallback_required)
    _LAST_HOLISTIC_DECISION["fallback_retry_used"] = False
    _LAST_HOLISTIC_DECISION["last_fallback_empty_or_error"] = False
    _LAST_HOLISTIC_DECISION["fallback_attempts"] = 0
    _reset_tier_state()


def _fallback_window_active() -> bool:
    fallback_required = _LAST_HOLISTIC_DECISION.get("fallback_required")
    ts = float(_LAST_HOLISTIC_DECISION.get("ts") or 0.0)
    if fallback_required is None:
        return False
    if (time.time() - ts) > FALLBACK_GUARD_SECONDS:
        return False
    return True


def _remaining_fallback_calls() -> int:
    if not _fallback_window_active():
        return 0 if STRICT_FALLBACK_REQUIRES_HOLISTIC else -1
    if not bool(_LAST_HOLISTIC_DECISION.get("fallback_required")):
        return 0
    remaining = int(_LAST_HOLISTIC_DECISION.get("fallback_remaining", 0) or 0)
    if bool(_LAST_HOLISTIC_DECISION.get("last_fallback_empty_or_error")) and bool(
        _LAST_HOLISTIC_DECISION.get("retry_after_empty_available")
    ):
        remaining += 1
    return max(0, remaining)


def _acquire_fallback_slot(tool_input: str = "") -> tuple[bool, str, int]:
    if not _fallback_window_active():
        if STRICT_FALLBACK_REQUIRES_HOLISTIC:
            return False, "guard_window_inactive", 0
        return True, "guard_window_inactive", -1

    if not bool(_LAST_HOLISTIC_DECISION.get("fallback_required")):
        return False, "holistic_marked_no_fallback", 0

    last_query = str(_LAST_HOLISTIC_DECISION.get("query") or "")
    if tool_input and last_query and not _fallback_query_matches(tool_input, last_query):
        return False, "fallback_query_mismatch", _remaining_fallback_calls()

    remaining = int(_LAST_HOLISTIC_DECISION.get("fallback_remaining", 0) or 0)
    if remaining > 0:
        _LAST_HOLISTIC_DECISION["fallback_remaining"] = remaining - 1
        _LAST_HOLISTIC_DECISION["fallback_attempts"] = int(_LAST_HOLISTIC_DECISION.get("fallback_attempts", 0) or 0) + 1
        return True, "primary_fallback_budget", _remaining_fallback_calls()

    if bool(_LAST_HOLISTIC_DECISION.get("last_fallback_empty_or_error")) and bool(
        _LAST_HOLISTIC_DECISION.get("retry_after_empty_available")
    ):
        _LAST_HOLISTIC_DECISION["retry_after_empty_available"] = False
        _LAST_HOLISTIC_DECISION["fallback_retry_used"] = True
        _LAST_HOLISTIC_DECISION["last_fallback_empty_or_error"] = False
        _LAST_HOLISTIC_DECISION["fallback_attempts"] = int(_LAST_HOLISTIC_DECISION.get("fallback_attempts", 0) or 0) + 1
        return True, "retry_after_empty_or_error", _remaining_fallback_calls()

    if bool(_LAST_HOLISTIC_DECISION.get("fallback_retry_used")):
        return False, "fallback_retry_already_used", 0
    return False, "fallback_budget_exhausted", 0


def _mark_fallback_outcome(empty_or_error: bool):
    if not _fallback_window_active():
        return
    _LAST_HOLISTIC_DECISION["last_fallback_empty_or_error"] = bool(empty_or_error)
    if empty_or_error:
        if bool(_LAST_HOLISTIC_DECISION.get("fallback_retry_used")):
            _LAST_HOLISTIC_DECISION["retry_after_empty_available"] = False
        else:
            _LAST_HOLISTIC_DECISION["retry_after_empty_available"] = True
    else:
        _LAST_HOLISTIC_DECISION["retry_after_empty_available"] = False


def _reset_tier_state():
    """Reset Tier 2 and Tier 3 state when a new holistic_search starts."""
    global _TIER2_FETCHED, _TIER2_CALL_COUNT, _TIER3_CALL_COUNT, _TIER3_WINDOW_TS
    _TIER2_FETCHED = set()
    _TIER2_CALL_COUNT = 0
    _TIER3_CALL_COUNT = 0
    _TIER3_WINDOW_TS = time.time()


def _acquire_tier2_slot(filename: str) -> tuple[bool, str]:
    """Check if a Tier 2 get_file_content call is permitted.
    Returns (allowed, reason).
    """
    global _TIER2_CALL_COUNT
    if filename in _TIER2_FETCHED:
        return False, f"file_already_fetched_in_tier2: {filename}"
    if _TIER2_CALL_COUNT >= _TIER2_CALL_LIMIT:
        return False, f"tier2_call_limit_reached ({_TIER2_CALL_LIMIT} max per holistic window)"
    return True, "ok"


def _record_tier2_fetch(filename: str):
    global _TIER2_CALL_COUNT
    _TIER2_FETCHED.add(filename)
    _TIER2_CALL_COUNT += 1


def _acquire_tier3_slot() -> tuple[bool, str]:
    """Check if a Tier 3 specialty tool call is permitted.
    Returns (allowed, reason).
    """
    global _TIER3_CALL_COUNT
    if _TIER3_CALL_COUNT >= _TIER3_CALL_LIMIT:
        return False, f"tier3_call_limit_reached ({_TIER3_CALL_LIMIT} max per holistic window)"
    # Also respect the holistic window — Tier 3 only allowed after holistic ran
    if _LAST_HOLISTIC_DECISION.get("fallback_required") is None:
        return False, "holistic_search_not_yet_called"
    if (time.time() - float(_TIER3_WINDOW_TS or 0.0)) > (FALLBACK_GUARD_SECONDS * 4):
        return False, "tier3_window_expired"
    return True, "ok"


def _record_tier3_call():
    global _TIER3_CALL_COUNT
    _TIER3_CALL_COUNT += 1


mcp = FastMCP(
    "My Second Brain",
    instructions=(
        "You are the user's Second Brain — a warm, thorough, and insightful personal assistant with access to everything they've stored. "
        "Respond in your natural Claude voice: engaged, thoughtful, and genuinely helpful. Answers should feel rich and complete, not clipped or robotic. "
        "Ground every claim in what the tools returned, but reason freely over that evidence — make connections, surface insights, and explain context. "
        "Exact label or keyword matching is NOT required: use semantic reasoning to bridge what was asked and what was found. "
        "If the best match is close but not exact, provide it confidently and note the difference in one sentence.\n\n"

        "<three_tier_retrieval>\n"
        "MANDATORY RULE: The VERY FIRST tool call for ANY user question MUST be holistic_search(query).\n"
        "Never call search_brain, keyword_search, get_file_content, or any other tool before holistic_search.\n"
        "This rule has no exceptions. If you feel tempted to call a different tool first, call holistic_search instead.\n\n"

        "TIER 1 — HOLISTIC SCAN (ALWAYS FIRST, ALWAYS)\n"
        "Call holistic_search(query) for every user question, every time, without exception.\n"
        "Read the full output — it contains EVIDENCE FILES with ranked snippets, TIER 2 CANDIDATES, and FALLBACK_REQUIRED status.\n"
        "If FALLBACK_REQUIRED=no and evidence is present: STOP and write your answer. Do not call any more tools.\n"
        "Tier 1 alone is sufficient for the vast majority of questions.\n\n"

        "TIER 2 — TARGETED FILE DEEPENING (ONLY IF NEEDED)\n"
        "Proceed to Tier 2 ONLY if ALL of the following are true:\n"
        "  a) The holistic_search evidence is incomplete or key details are missing, AND\n"
        "  b) At least one file in EVIDENCE FILES has 'excerpt truncated' OR confidence >= 80%\n"
        "When Tier 2 is needed:\n"
        "  - Call get_file_content ONLY on files listed in 'TIER 2 CANDIDATES' from the holistic output\n"
        "  - Call it on at most 4 files total — pick the highest-confidence ones from TIER 2 CANDIDATES\n"
        "  - Do NOT call get_file_content on the same file twice in one answer\n"
        "  - Do NOT call get_file_content on every file — only the ones flagged for deepening\n"
        "After 4 Tier 2 calls, STOP and write your answer regardless. Do NOT tell the user you hit a limit — just answer with what you have.\n\n"

        "TIER 3 — ULTRA-SPECIFIC GRAPH QUERIES (EXPLORATION ONLY)\n"
        "Proceed to Tier 3 ONLY if the user explicitly asks about:\n"
        "  - Connections between files ('what files are related to X')\n"
        "  - Specific named entities or relationships ('who is connected to Dan')\n"
        "  - Topic browsing ('what topics do I have on X')\n"
        "Tier 3 tools: get_connections, search_entities, get_topics\n"
        "Maximum 2 Tier 3 calls per question. Do NOT use Tier 3 to answer a question already addressed in Tier 1/2.\n\n"

        "FALLBACK SEARCH (ONLY WHEN EXPLICITLY REQUIRED)\n"
        "Call search_brain or keyword_search ONLY when holistic_search output shows FALLBACK_REQUIRED=yes.\n"
        "Maximum 1 fallback call. Do not chain search_brain and keyword_search together.\n\n"

        "HARD STOP RULE\n"
        "After 4 total tool calls, write your answer with what you have. Do not search for more.\n"
        "A focused answer from 2-3 tool calls is better than an exhaustive answer from 10.\n"
        "</three_tier_retrieval>\n\n"

        "<link_policy>\n"
        "MANDATORY — applies to every response that returns a file:\n"
        "1. For IMAGE files: ALWAYS embed the [View] link immediately in your answer body. Never ask 'would you like me to show it?' — just show it. The link IS the file view.\n"
        "2. For VIDEO files: ALWAYS embed the [Watch clip] link immediately in your answer body without asking.\n"
        "3. For ALL other file types (PDF, TEXT, DOCX, etc.): include the [View] link inline when the user asked to find, locate, show, or retrieve a file.\n"
        "4. Do NOT say 'I cannot display' or 'I cannot access' when a [View] link is present in the tool output — clicking the link opens it in the knowledge graph.\n"
        "5. Semantic match is sufficient — if the user asks for X and the closest match is tagged or described as something related, treat it as the answer. Provide the link immediately and note the semantic difference in one short sentence (e.g. 'This is tagged as Y rather than X, but it appears to be the closest match').\n"
        "6. Never ask for confirmation before providing a link. If a relevant file exists, link it immediately.\n"
        "7. Never suggest the user look elsewhere (Google Drive, camera roll, etc.) when a plausible match exists in the brain — show what you have first.\n"
        "</link_policy>\n\n"

        "<answer_quality>\n"
        "Write in your natural Claude voice — warm, engaged, and thorough. Do not be terse or robotic.\n"
        "Open with the direct answer or the most useful finding, then elaborate naturally.\n"
        "Go deep: explain context, surface connections between pieces of evidence, and help the user understand what they have stored.\n"
        "Use semantic reasoning — if the user asks about X and the evidence covers something closely related, bridge the gap rather than declaring 'not found'.\n"
        "When a match is approximate, note it briefly in one sentence, then still provide the full result and link.\n"
        "For broad questions: write richly — explore multiple angles, quote key evidence, explain what it means.\n"
        "For factual questions: give exact figures and quotes from the evidence; do not estimate.\n"
        "For process/how-to questions: walk through the steps clearly, quoting the source material.\n"
        "Always attribute claims to their source file. If evidence conflicts, say so explicitly.\n"
        "Do NOT end with 'Would you like me to help with anything else?' or similar filler — end with substance.\n"
        "</answer_quality>\n\n"

        "<mandatory_format>\n"
        "SOURCES BLOCK — NON-NEGOTIABLE RULE:\n"
        "Every single response that uses tool output MUST end with the Sources block.\n"
        "The Sources block appears at the bottom of every tool result, starting with '---' and '**Sources**' and ending with '---'.\n"
        "Copy it verbatim — do not modify, summarise, reorder, or skip it.\n"
        "This applies whether you used 1 tool or 4 tools. Use the Sources block from the LAST tool you called.\n"
        "If you called get_file_content, use the Sources block from that output.\n"
        "If you called holistic_search only, use the Sources block from that output.\n"
        "NEVER omit the Sources block. NEVER write a response without it ending in '---'.\n"
        "Before sending: check your last line. If it is not '---', you have forgotten the Sources block — add it now.\n"
        "</mandatory_format>"
    ),
)


def _extract_exact_quotes_with_context(content: str, query: str = "") -> list[str]:
    """
    Extract verbatim passages containing numbers, dollar amounts, rates, or
    query-relevant named entities. Returns ±300 char windows so Claude sees
    exact figures with full speaker attribution.
    """
    import re as _re

    # Build query-aware name patterns (e.g. "dan", "ruby" from the query)
    query_names = set()
    if query:
        # Possessives: "dan's" → "dan"
        for m in _re.findall(r"\b(\w+)(?:'s|'s)\b", query, _re.I):
            if len(m) >= 2:
                query_names.add(m.lower())
        # Missing-apostrophe possessives: "rubys" → "ruby"
        for m in _re.findall(r'\b(\w{2,})s\b', query, _re.I):
            bare = m.lower()
            if bare not in {'this', 'does', 'was', 'has', 'his', 'its'}:
                query_names.add(bare)
        # Capitalised words
        for m in _re.findall(r'\b[A-Z][a-z]+\b', query):
            query_names.add(m.lower())
        # All 3+ char words that aren't stopwords as fallback name candidates
        _qstop = {'what','where','when','which','their','there','about','tell','show','find',
                   'give','from','with','this','that','have','does','were','been','they','them',
                   'check','please','search','second','brain','hourly','rates','hour','rate',
                   'are','and','the','for','not','but','can','how','also','just','all','any',
                   'get','got','let','may','our','own','say','she','too','try','use','who',
                   'why','yet','its','had','has','his','her','him'}
        for w in query.lower().split():
            w = w.strip('?.,!"\':;()[] ')
            if len(w) >= 3 and w not in _qstop:
                query_names.add(w)

    # Patterns that indicate a factual claim worth quoting
    number_pattern = _re.compile(
        r'(\$\d+(?:\.\d+)?'
        r'|\b\d{2,6}\s*(?:an hour|per hour|/hr|/hour|bucks|dollars|per month|a month|per year|a year|percent|%)'
        r'|\brate\s+(?:is|of|at|was|would be|could be)\s+\$?\d+'
        r'|\bcharges?\s+\$?\d+'
        r'|\bcosts?\s+\$?\d+'
        r'|\bpaid?\s+\$?\d+'
        r'|\b\d+\s*(?:per cent|%))',
        _re.I
    )

    hits = []
    seen_positions = []

    # Pass 1: Find all number/rate mentions
    for m in number_pattern.finditer(content):
        start = m.start()
        if any(abs(start - p) < 200 for p in seen_positions):
            continue
        seen_positions.append(start)
        snippet_start = max(0, start - 300)
        snippet_end   = min(len(content), start + 300)
        snippet = content[snippet_start:snippet_end].strip()
        snippet = _re.sub(r'\s+', ' ', snippet)
        hits.append(f'"{snippet}"')

    # Pass 2: Find passages where query names appear near numbers
    if query_names:
        name_pattern = _re.compile(
            r'\b(' + '|'.join(_re.escape(n) for n in query_names) + r')\b',
            _re.I
        )
        for m in name_pattern.finditer(content):
            start = m.start()
            if any(abs(start - p) < 200 for p in seen_positions):
                continue
            # Only include if there's a number nearby (within ±400 chars)
            window_start = max(0, start - 400)
            window_end = min(len(content), start + 400)
            window = content[window_start:window_end]
            if _re.search(r'\$\d+|\b\d{2,6}\b', window):
                seen_positions.append(start)
                snippet_start = max(0, start - 300)
                snippet_end   = min(len(content), start + 300)
                snippet = content[snippet_start:snippet_end].strip()
                snippet = _re.sub(r'\s+', ' ', snippet)
                hits.append(f'"{snippet}"')

    # Deduplicate overlapping snippets (check if >40% of content overlaps)
    unique_hits = []
    for h in hits:
        h_words = set(h.lower().split())
        is_dup = False
        for existing in unique_hits:
            existing_words = set(existing.lower().split())
            overlap = len(h_words & existing_words)
            if overlap > 0.4 * min(len(h_words), len(existing_words)):
                is_dup = True
                break
        if not is_dup:
            unique_hits.append(h)
        if len(unique_hits) >= 6:
            break

    return unique_hits


def _build_sources_block(seen_files: dict, video_clip_urls: dict) -> list[str]:
    """Build the sources block. Claude MUST reproduce this at the end of its response."""
    top_sources = sorted(seen_files.items(), key=lambda x: -x[1]["conf"])[:SOURCES_BLOCK_MAX]
    source_lines = []
    if not top_sources:
        source_lines.append("• No sources found for this query.")
    else:
        for fn, meta in top_sources:
            stype = meta["type"]
            conf  = meta["conf"]
            # All file types get a View link — not just images and videos
            if stype == "VIDEO":
                clip_url = _absolute_url(video_clip_urls.get(fn, _clip_preview_url(fn, 0, 30)))
                suffix = f" — [Watch clip]({clip_url})"
            else:
                suffix = f" — [View]({_node_view_url(fn)})"
            source_lines.append(f"• {fn} [{stype}] ({conf}% match){suffix}")

    lines = ["", "", "---", "**Sources**"]
    lines.extend(source_lines)
    lines.append("---")
    return lines


def _has_terminal_sources_block(text: str) -> bool:
    if not isinstance(text, str):
        return False
    return bool(re.search(r"---\s*\n\*\*Sources\*\*[\s\S]*\n---\s*$", text.rstrip()))


def _finalize_tool_output(text: str, seen_files: dict | None = None, video_clip_urls: dict | None = None) -> str:
    body = text or ""
    if _has_terminal_sources_block(body):
        return body
    safe_seen = seen_files or {}
    safe_video = video_clip_urls or {}
    lines = [
        body.rstrip("\n"),
        "",
        "COPY THE SOURCES BLOCK BELOW VERBATIM AT THE END OF YOUR FINAL ANSWER.",
    ]
    lines.extend(_build_sources_block(safe_seen, safe_video))
    result = "\n".join(lines)
    if _has_terminal_sources_block(result):
        return result
    return body.rstrip("\n") + "\n\n---\n**Sources**\n• No sources found for this query.\n---"


@mcp.tool()
def holistic_search(query: str) -> str:
    """⭐ ALWAYS CALL THIS FIRST — for every single user question, no exceptions.
    This is the primary and most powerful retrieval tool. It searches across all files,
    topics, entities, and connections simultaneously and returns ranked evidence with snippets.
    Call this BEFORE search_brain, keyword_search, get_file_content, or any other tool.
    Do NOT call holistic_search more than once per question.
    After reading the output: if FALLBACK_REQUIRED=no, answer immediately from the evidence — do not call more tools."""
    data = _post("/api/mcp/holistic_search", {"query": query})
    if "error" in data:
        _record_holistic_decision(query, True)
        error_lines = [
            "MODE: primary_holistic",
            f"QUERY: {query}",
            "FALLBACK_REQUIRED=yes",
            "",
            "ORCHESTRATION CONTRACT",
            "answer_first=yes",
            "limitation_style=one_sentence_if_needed",
            "limitation_sentence_max=1",
            "fallback_budget=1",
            "remaining_fallback_calls=1",
            "fallback_retry_rule=allow_one_extra_only_if_first_fallback_empty_or_error",
            "",
            "RESPONSE REQUIREMENTS",
            "style=comprehensive_professional_structured",
            "required_bullets=4-8",
            "copy_sources_block_verbatim=yes",
            "extra_tools_allowed=one_fallback_only",
            "final_self_check=end_with_sources_block_verbatim",
            "",
            f"Error: {data['error']}",
            "",
            "ANSWER POLICY",
            "verified_facts=0, uncertain_facts=1",
            "use_uncertainty_language=yes",
            "best_effort_disclaimer=Primary retrieval failed; use one fallback tool and keep uncertainty explicit.",
            "do_not_guess=yes",
            "",
            "CLAIM ADJUDICATION",
            "supported=0, conflicting=0, weak_support=0, insufficient_evidence=1",
            "",
            "VERIFIED FACT SHEET",
            "supported_fact: none",
        ]
        return _finalize_tool_output("\n".join(error_lines))

    sem    = data.get("semantic_results", [])
    fulls  = data.get("full_files", [])
    kwhits = data.get("keyword_hits", [])
    conn   = data.get("connected_files", [])
    evidence_files = data.get("evidence_files", [])
    process_steps = data.get("process_steps", [])
    claim_checks = data.get("claim_verification", [])
    claim_adjudication = data.get("claim_adjudication", {})
    retrieval_meta = data.get("retrieval_meta", {})

    if not evidence_files and not sem and not kwhits and not conn and not claim_checks:
        _record_holistic_decision(query, True)
        no_result_lines = [
            "MODE: primary_holistic",
            f"QUERY: {query}",
            "FALLBACK_REQUIRED=yes",
            "",
            "ORCHESTRATION CONTRACT",
            "answer_first=yes",
            "limitation_style=one_sentence_if_needed",
            "limitation_sentence_max=1",
            "fallback_budget=1",
            "remaining_fallback_calls=1",
            "fallback_retry_rule=allow_one_extra_only_if_first_fallback_empty_or_error",
            "",
            "RESPONSE REQUIREMENTS",
            "style=comprehensive_professional_structured",
            "required_bullets=4-8",
            "copy_sources_block_verbatim=yes",
            "extra_tools_allowed=one_fallback_only",
            "final_self_check=end_with_sources_block_verbatim",
            "",
            "EVIDENCE FILES",
            "No relevant evidence found in the current knowledge base.",
            "",
            "ANSWER POLICY",
            "verified_facts=0, uncertain_facts=1",
            "verified_source=claim_adjudication.supported",
            "inferred_source=evidence_files",
            "use_uncertainty_language=yes",
            "best_effort_disclaimer=No supporting evidence found; ask the user to refine the query or ingest relevant files.",
            "do_not_guess=yes",
            "",
            "CLAIM ADJUDICATION",
            "supported=0, conflicting=0, weak_support=0, insufficient_evidence=1",
            "",
            "VERIFIED FACT SHEET",
            "supported_fact: none",
        ]
        return _finalize_tool_output("\n".join(no_result_lines))

    _record_holistic_decision(query, False)

    lines = [
        "MODE: primary_holistic",
        f"QUERY: {query}",
        "FALLBACK_REQUIRED=no",
        "",
        "ORCHESTRATION CONTRACT",
        "answer_first=yes",
        "limitation_style=one_sentence_if_needed",
        "limitation_sentence_max=1",
        "fallback_budget=1",
        "remaining_fallback_calls=0",
        "fallback_retry_rule=allow_one_extra_only_if_first_fallback_empty_or_error",
        "",
        "RESPONSE REQUIREMENTS",
        "style=comprehensive_professional_structured",
        "depth=multifaceted_if_broad",
        "numeric_claims=quote_exact_evidence_only",
        "answer_opening=1-2_sentences_then_evidence_bullets",
        "copy_sources_block_verbatim=yes",
        "do_not_be_casual=yes",
        "extra_tools_allowed=no_unless_user_explicitly_requests_full_text_or_holistic_has_no_usable_evidence",
        "final_self_check=end_with_sources_block_verbatim",
    ]

    if retrieval_meta:
        lines.append("\nRETRIEVAL META")
        rows_scanned = retrieval_meta.get("rows_scanned")
        semantic_rows = retrieval_meta.get("semantic_rows")
        if rows_scanned is not None and semantic_rows is not None:
            lines.append(f"rows_scanned={rows_scanned}, semantic_rows={semantic_rows}")
        if retrieval_meta.get("compact_mode"):
            lines.append("compact_mode=enabled")
        if retrieval_meta.get("answer_first_contract"):
            lines.append("answer_first_contract=enabled")
        limitation_sentence_max = retrieval_meta.get("limitation_sentence_max")
        if limitation_sentence_max is not None:
            lines.append(f"limitation_sentence_max={limitation_sentence_max}")
        intent = retrieval_meta.get("query_intent")
        if intent:
            lines.append(f"query_intent={intent}")
            if str(intent).lower() == "broad":
                lines.append("required_bullets=6-12")
            elif str(intent).lower() == "factual":
                lines.append("required_bullets=4-8")
            else:
                lines.append("required_bullets=5-9")
        selected_files = retrieval_meta.get("selected_files")
        if selected_files is not None:
            lines.append(f"selected_files={selected_files}")
        if retrieval_meta.get("process_query"):
            lines.append("process_query=yes")
            lines.append(f"process_steps={int(retrieval_meta.get('process_steps', 0) or 0)}")
            lines.append(f"process_source_files={int(retrieval_meta.get('process_source_files', 0) or 0)}")
            lines.append("answer_mode=process_synthesis")
            lines.append("must_synthesize_across_files=yes")
            if int(retrieval_meta.get("process_steps", 0) or 0) > 0:
                lines.append("do_not_claim_process_missing_when_process_trace_present=yes")
        if retrieval_meta.get("process_semantic_backfill_used"):
            lines.append("process_semantic_backfill=enabled")
        if retrieval_meta.get("claim_validation_enabled"):
            counts = retrieval_meta.get("claim_status_counts", {}) or {}
            lines.append(
                "claim_validation=enabled "
                f"(supported={counts.get('supported', 0)}, "
                f"conflicting={counts.get('conflicting', 0)}, "
                f"weak={counts.get('weak_support', 0)}, "
                f"insufficient={counts.get('insufficient_evidence', 0)})"
            )

    policy = claim_adjudication.get("policy", {}) or {}
    counts = claim_adjudication.get("counts", {}) or {}
    supported = claim_adjudication.get("supported", []) or []
    conflicting = claim_adjudication.get("conflicting", []) or []
    weak_support = claim_adjudication.get("weak_support", []) or []
    insufficient = claim_adjudication.get("insufficient_evidence", []) or []

    lines.append("\nANSWER PACKET")
    lines.append("answer_opening_instruction=Start with direct answer in 1-2 sentences before bullets")
    lines.append(
        f"supported_claims={counts.get('supported', 0)}, "
        f"uncertain_claims={counts.get('conflicting', 0) + counts.get('weak_support', 0) + counts.get('insufficient_evidence', 0)}"
    )
    if supported:
        for row in supported[:4]:
            subject = row.get("subject", "?")
            value = row.get("recommended_value", "") or ", ".join((row.get("observed_values", []) or [])[:2])
            if value:
                lines.append(f"direct_fact: {subject} = {value}")
    else:
        lines.append("direct_fact: none")

    if policy.get("requires_uncertainty"):
        concise_limit = "Evidence has uncertainty; include one concise limitation sentence and avoid guessing."
        for row in (conflicting + weak_support + insufficient)[:1]:
            row_note = str(row.get("uncertainty", "") or "").strip()
            if row_note:
                concise_limit = row_note
                break
        lines.append(f"limitation_one_sentence: {concise_limit}")
    else:
        lines.append("limitation_one_sentence: none")

    has_multimodal_links = any(
        str(ev.get("source_type", "")).upper() in {"IMAGE", "VIDEO"}
        for ev in evidence_files
    )
    if has_multimodal_links:
        lines.append("multimodal_links_present=yes")
        lines.append("must_include_view_or_watch_links_when_visual_question=yes")
        lines.append("do_not_claim_cannot_access_media_when_links_present=yes")
        lines.append("do_not_ask_confirmation_before_sharing_link=yes — embed the link directly in your answer, never ask 'would you like me to show it?'")

    if process_steps:
        lines.append("\nPROCESS TRACE")
        lines.append("process_query_deep_mode=enabled")
        lines.append("process_answer_instruction=Build a clear step-by-step monthly payment flow using evidence from multiple files")
        for i, step in enumerate(process_steps[:8], 1):
            step_file = step.get("source_file", "?")
            step_chunk = int(step.get("chunk_index", -1) or -1)
            step_stage = step.get("stage", "process_detail")
            step_conf = int(step.get("confidence", 0) or 0)
            lines.append(f"[{i}] {step_file} chunk={step_chunk} stage={step_stage} {step_conf}%")
            snippet = " ".join(str(step.get("snippet", "") or "").split())
            if snippet:
                lines.append(f"  - {snippet[:380]}")

    if evidence_files:
        lines.append("\nEVIDENCE FILES")
        for i, ev in enumerate(evidence_files, 1):
            fname = ev.get("source_file", "?")
            stype = str(ev.get("source_type", "?")).upper()
            conf = int(ev.get("confidence", 0) or 0)
            lines.append(f"\n[{i}] {fname} [{stype}] {conf}%")

            reason = ev.get("confidence_reason", "")
            if reason:
                lines.append(f"  confidence_reason: {reason}")

            topics = ev.get("topics", [])
            if topics:
                lines.append(f"  topics: {', '.join(topics[:6])}")

            signals = ev.get("match_signals", [])
            if signals:
                lines.append(f"  signals: {', '.join(signals)}")

            matched_keywords = ev.get("matched_keywords", [])
            if matched_keywords:
                lines.append(f"  matched_keywords: {', '.join(matched_keywords[:8])}")

            ctx = ev.get("upload_context", "")
            if ctx:
                lines.append(f"  label: {ctx}")

            if ev.get("truncated"):
                lines.append("  note: excerpt is truncated; call get_file_content only if user explicitly asks for full-file wording")

            if stype == "IMAGE":
                lines.append(f"  📸 [View \"{fname}\"]({_node_view_url(fname)})")
            elif stype == "VIDEO":
                lines.append(f"  video: get_video_clip('{fname}', topic)")
            else:
                lines.append(f"  📄 [View \"{fname}\"]({_node_view_url(fname)})")

            for snippet in (ev.get("evidence_snippets", []) or [])[:3]:
                snippet_text = " ".join(str(snippet).split()).strip()
                if snippet_text:
                    lines.append(f"  - {snippet_text[:420]}")
    else:
        lines.append("\nEVIDENCE FILES")
        for i, r in enumerate(sem[:8], 1):
            stype = str(r.get("source_type", "?")).upper()
            fname = r.get("source_file", "?")
            conf = int(r.get("confidence", 0) or 0)
            lines.append(f"\n[{i}] {fname} [{stype}] {conf}%")
            content = " ".join(str(r.get("content", "")).split())
            if content:
                lines.append(f"  - {content[:420]}")

    if claim_checks or claim_adjudication:
        lines.append("\nCLAIM ADJUDICATION")

        lines.append("\nANSWER POLICY")
        lines.append(
            f"verified_facts={counts.get('supported', 0)}, "
            f"uncertain_facts={counts.get('conflicting', 0) + counts.get('weak_support', 0) + counts.get('insufficient_evidence', 0)}"
        )
        lines.append("verified_source=claim_adjudication.supported")
        lines.append("inferred_source=evidence_files")

        if policy.get("requires_uncertainty"):
            lines.append("use_uncertainty_language=yes")
            lines.append(
                "best_effort_disclaimer=Some claims are weak/conflicting/insufficient; answer with explicit uncertainty."
            )
        if policy.get("must_not_guess"):
            lines.append("do_not_guess=yes")

        for row in supported[:6]:
            subject = row.get("subject", "?")
            value = row.get("recommended_value", "") or ", ".join((row.get("observed_values", []) or [])[:2])
            direct_count = int(row.get("direct_evidence_count", 0) or 0)
            lines.append(f"\n{subject}: supported")
            if value:
                lines.append(f"  value: {value}")
            lines.append(f"  direct_evidence: {direct_count}")

        for row in conflicting[:6]:
            subject = row.get("subject", "?")
            observed = row.get("observed_values", []) or []
            lines.append(f"\n{subject}: conflicting")
            if observed:
                lines.append(f"  observed: {', '.join(observed[:6])}")
            note = row.get("uncertainty", "")
            if note:
                lines.append(f"  uncertainty: {note}")

        for row in weak_support[:6]:
            subject = row.get("subject", "?")
            observed = row.get("observed_values", []) or []
            lines.append(f"\n{subject}: weak_support")
            if observed:
                lines.append(f"  observed: {', '.join(observed[:6])}")
            note = row.get("uncertainty", "")
            if note:
                lines.append(f"  uncertainty: {note}")

        for row in insufficient[:6]:
            subject = row.get("subject", "?")
            lines.append(f"\n{subject}: insufficient_evidence")
            note = row.get("uncertainty", "")
            if note:
                lines.append(f"  uncertainty: {note}")

        lines.append("\nVERIFIED FACT SHEET")
        if supported:
            for row in supported[:6]:
                subject = row.get("subject", "?")
                value = row.get("recommended_value", "")
                evidence_count = int(row.get("evidence_count", 0) or 0)
                lines.append(f"  supported_fact: {subject} = {value or '[value not set]'} (evidence={evidence_count})")
        else:
            lines.append("  supported_fact: none")

        if conflicting:
            for row in conflicting[:6]:
                subject = row.get("subject", "?")
                observed = row.get("observed_values", []) or []
                lines.append(f"  conflicting_fact: {subject} -> {', '.join(observed[:6]) if observed else 'none'}")
        if weak_support:
            for row in weak_support[:6]:
                subject = row.get("subject", "?")
                observed = row.get("observed_values", []) or []
                lines.append(f"  weak_fact: {subject} -> {', '.join(observed[:6]) if observed else 'none'}")
        if insufficient:
            for row in insufficient[:6]:
                subject = row.get("subject", "?")
                lines.append(f"  insufficient_fact: {subject}")

        for item in claim_checks[:8]:
            subject = item.get("subject", "?")
            status = item.get("status", "unknown")
            lines.append(f"\n{subject} evidence ({status})")
            for ev in item.get("evidence", [])[:2]:
                src = ev.get("source_file", "?")
                conf = ev.get("confidence", 0)
                attribution = ev.get("attribution", "unknown")
                speaker = ev.get("speaker_label", "")
                quote = " ".join(str(ev.get("quote", "") or "").split())
                prefix = attribution
                if speaker:
                    prefix = f"{attribution}:{speaker}"
                if quote:
                    lines.append(f"  - {src} ({conf}%, {prefix}): {quote[:240]}")

    # Tier 2 candidates block — tell Claude exactly which files to deepen into
    tier2_recs = data.get("tier2_recommendations", [])
    if tier2_recs:
        lines.append("\nTIER 2 CANDIDATES")
        lines.append("If you need more detail, call get_file_content on these files ONLY (in order listed):")
        for _i, _rec in enumerate(tier2_recs, 1):
            _rfname = _rec.get("source_file", "?")
            _rconf = int(_rec.get("confidence", 0) or 0)
            _rwhy = _rec.get("why_check", "")
            _rtrunc = " [TRUNCATED]" if _rec.get("truncated") else ""
            lines.append(f"  [{_i}] {_rfname} ({_rconf}%){_rtrunc} — {_rwhy}")
        lines.append(f"  Max 2 get_file_content calls. Do not fetch files not listed here.")
    else:
        lines.append("\nTIER 2 CANDIDATES")
        lines.append("  No Tier 2 deepening needed — holistic evidence is sufficient.")

    lines.append("\nMANDATORY FINAL STEP")
    lines.append("Copy the Sources block below verbatim at the very end of your final answer.")
    lines.append("Do not call additional retrieval tools when FALLBACK_REQUIRED=no.")

    # Sources block — cite only files that directly answer the query.
    seen_files  = {}
    video_clip_urls = {}

    if evidence_files:
        _candidates = []
        for r in evidence_files:
            fn = r.get("source_file", "?")
            conf = int(r.get("confidence", 0) or 0) or 50
            stype = str(r.get("source_type", "text")).upper()
            snippets = r.get("evidence_snippets", []) or []
            signals = r.get("match_signals", []) or []
            _candidates.append({"fn": fn, "conf": conf, "stype": stype, "snippets": snippets, "signals": signals})

        _candidates.sort(key=lambda x: -x["conf"])

        # Detect visual (media-retrieval) vs informational query intent
        _visual_words = {"photo", "picture", "image", "screenshot", "video", "clip", "recording",
                         "birthday", "party", "event", "scene", "watch", "visual"}
        _query_words = set(w.strip("?.,!").lower() for w in query.split())
        _has_visual = bool(_query_words & _visual_words)

        # Check if any media files exist in results
        _media_files = [c for c in _candidates if c["stype"] in ("IMAGE", "VIDEO")]
        _has_media_results = bool(_media_files)

        top_conf = _candidates[0]["conf"] if _candidates else 0

        for cand in _candidates:
            fn, conf, stype, snippets, signals = cand["fn"], cand["conf"], cand["stype"], cand["snippets"], cand["signals"]

            # VISUAL QUERY: if media files exist, only cite media files — PDFs/text are not the answer
            if _has_visual and _has_media_results:
                _include = stype in ("IMAGE", "VIDEO")
            else:
                # INFORMATIONAL QUERY: include top result, plus any file within 15 points that
                # has real evidence snippets (not just broad sidecar matches)
                _include = (conf == top_conf)
                if not _include and (top_conf - conf) <= 15 and snippets:
                    _include = True
                # Never include note files unless they are top result
                if fn.startswith("note_") and fn.endswith(".md") and conf != top_conf:
                    _include = False

            if _include:
                if fn not in seen_files or conf > seen_files[fn]["conf"]:
                    seen_files[fn] = {"conf": conf, "type": stype}
            if stype == "VIDEO" and fn not in video_clip_urls:
                video_clip_urls[fn] = _clip_preview_url(fn, 0, 30)
    else:
        kwhit_files = {r.get("source_file", "") for r in kwhits}
        for lst in (sem, fulls, kwhits, conn):
            for r in lst:
                fn   = r.get("source_file", "?")
                conf = r.get("confidence", 0)
                if conf == 0 and fn in kwhit_files:
                    conf = 85
                if conf == 0:
                    conf = 50
                stype = str(r.get("source_type", "text")).upper()
                if fn not in seen_files or conf > seen_files[fn]["conf"]:
                    seen_files[fn] = {"conf": conf, "type": stype}
                if stype == "VIDEO" and fn not in video_clip_urls:
                    ts_s = r.get("timestamp_start")
                    ts_e = r.get("timestamp_end")
                    if ts_s is not None and ts_e is not None:
                        video_clip_urls[fn] = _clip_preview_url(fn, int(ts_s), int(ts_e))
                    else:
                        video_clip_urls[fn] = _clip_preview_url(fn, 0, 30)

    return _finalize_tool_output("\n".join(lines), seen_files, video_clip_urls)


@mcp.tool()
def get_video_clip(file: str, topic: str) -> str:
    """[SPECIALTY] Trim and return a specific video clip for a topic from a video file.
    Use ONLY when the user asks to watch, see, or play a specific part of a video.
    Only call this after holistic_search has identified a relevant video file.
    Do NOT call this speculatively — only when video evidence is directly needed for the answer."""
    data = _post("/api/mcp/find_clip", {"file": file, "topic": topic})

    if "error" in data:
        return _finalize_tool_output("\n".join([
            "MODE: specialty_video_clip",
            f"Could not generate clip: {data['error']}",
        ]))

    clip_url = _absolute_url(data.get("clip_url", "")) if data.get("clip_url") else ""
    start    = data.get("start", 0)
    end      = data.get("end", 0)
    duration = data.get("duration_seconds", end - start)
    matched  = data.get("matched_lines", [])

    lines = [
        "MODE: specialty_video_clip",
        f"[Watch clip]({clip_url})",
        f"Clip: {start}s–{end}s ({duration}s)",
    ]
    if matched:
        lines.append("Matched lines:")
        for m in matched[:4]:
            lines.append(f"  {m}")
    return _finalize_tool_output(
        "\n".join(lines),
        seen_files={file: {"conf": 100, "type": "VIDEO"}},
        video_clip_urls={file: clip_url} if clip_url else None,
    )


@mcp.tool()
def search_brain(query: str, top_k: int = 15) -> str:
    """[FALLBACK — NEVER CALL FIRST] Use ONLY after holistic_search returns FALLBACK_REQUIRED=yes.
    NEVER call this as your first tool — always call holistic_search first.
    Do NOT call this if holistic_search returned useful evidence (FALLBACK_REQUIRED=no).
    Do NOT use this as a substitute for Tier 2 deepening — use get_file_content for that.
    Maximum 1 call per question. Will be blocked and redirected if holistic_search was not called first."""
    allowed, gate_reason, remaining_calls = _acquire_fallback_slot(query)
    if not allowed:
        if gate_reason == "guard_window_inactive":
            blocked = [
                "MODE: fallback_semantic",
                "FALLBACK_TOOL_CALL_BLOCKED=yes",
                f"fallback_gate_reason={gate_reason}",
                "CRITICAL: You called search_brain without calling holistic_search first.",
                "ACTION REQUIRED: Call holistic_search(query) now. It is always the first tool to call.",
                "Do not answer until you have called holistic_search.",
            ]
        else:
            blocked = [
                "MODE: fallback_semantic",
                "FALLBACK_TOOL_CALL_BLOCKED=yes",
                f"fallback_gate_reason={gate_reason}",
                f"remaining_fallback_calls={remaining_calls}",
                "Reason: fallback budget exhausted or not permitted for this orchestration window.",
                "Action: finalize from holistic_search evidence and append Sources verbatim.",
            ]
        return _finalize_tool_output("\n".join(blocked))

    top_k = max(1, min(top_k, 20))
    data = _post("/api/mcp/search", {"query": query, "top_k": top_k})
    if "error" in data:
        _mark_fallback_outcome(True)
        return _finalize_tool_output("\n".join([
            "MODE: fallback_semantic",
            f"fallback_gate_reason={gate_reason}",
            f"remaining_fallback_calls={_remaining_fallback_calls()}",
            "fallback_outcome=empty_or_error",
            f"Error: {data['error']}",
        ]))
    all_results = data.get("results", [])
    if not all_results:
        _mark_fallback_outcome(True)
        return _finalize_tool_output("\n".join([
            "MODE: fallback_semantic",
            f"fallback_gate_reason={gate_reason}",
            f"remaining_fallback_calls={_remaining_fallback_calls()}",
            "fallback_outcome=empty_or_error",
            f"No results for: '{query}'",
        ]))

    _mark_fallback_outcome(False)

    all_results.sort(key=lambda x: -x.get("confidence", 0))
    lines = [
        "MODE: fallback_semantic",
        f"fallback_gate_reason={gate_reason}",
        f"remaining_fallback_calls={_remaining_fallback_calls()}",
        "fallback_outcome=success",
        f"Search: '{query}' ({len(all_results)} results)",
    ]
    for i, r in enumerate(all_results, 1):
        stype   = r.get("source_type", "?").upper()
        fname   = r.get("source_file", "?")
        conf    = r.get("confidence", 0)
        conf_reason = r.get("confidence_reason", "semantic vector match")
        content = r.get("content", "").strip()
        ctx     = r.get("upload_context", "")
        cidx    = r.get("chunk_index", -1)
        label   = f" chunk {cidx}" if cidx >= 0 else ""

        lines.append(f"\n[{i}] {fname} [{stype}]{label} {conf}%")
        lines.append(f"  confidence_reason: {conf_reason}")
        if ctx:
            lines.append(f"  label: {ctx}")
        if stype == "VIDEO":
            lines.append(f"  → call get_video_clip(file='{fname}', topic=<what user wants>)")
        if stype == "IMAGE":
            lines.append(f"  📸 [View]({_node_view_url(fname)})")
        if content:
            quote_hits = _extract_exact_quotes_with_context(content, query)
            if quote_hits:
                lines.append("  exact_quotes:")
                for quote in quote_hits[:2]:
                    lines.append(f"    {quote}")
            lines.append(" ".join(content.split())[:520])

    seen_files = {}
    video_clip_urls = {}
    for r in all_results:
        fn    = r.get("source_file", "?")
        conf  = r.get("confidence", 0) or 50
        stype = r.get("source_type", "text").upper()
        if fn not in seen_files or conf > seen_files[fn]["conf"]:
            seen_files[fn] = {"conf": conf, "type": stype}
        if stype == "VIDEO" and fn not in video_clip_urls:
            video_clip_urls[fn] = _clip_preview_url(fn, 0, 30)

    return _finalize_tool_output("\n".join(lines), seen_files, video_clip_urls)


@mcp.tool()
def keyword_search(keyword: str) -> str:
    """[FALLBACK — NEVER CALL FIRST] Exact keyword search — use ONLY after holistic_search returns FALLBACK_REQUIRED=yes.
    NEVER call this as your first tool — always call holistic_search first.
    Do NOT call this if holistic_search already returned keyword hits for the same term.
    Maximum 1 call per question. Will be blocked and redirected if holistic_search was not called first."""
    allowed, gate_reason, remaining_calls = _acquire_fallback_slot(keyword)
    if not allowed:
        if gate_reason == "guard_window_inactive":
            blocked = [
                "MODE: fallback_keyword",
                "FALLBACK_TOOL_CALL_BLOCKED=yes",
                f"fallback_gate_reason={gate_reason}",
                "CRITICAL: You called keyword_search without calling holistic_search first.",
                "ACTION REQUIRED: Call holistic_search(query) now. It is always the first tool to call.",
                "Do not answer until you have called holistic_search.",
            ]
        else:
            blocked = [
                "MODE: fallback_keyword",
                "FALLBACK_TOOL_CALL_BLOCKED=yes",
                f"fallback_gate_reason={gate_reason}",
                f"remaining_fallback_calls={remaining_calls}",
                "Reason: fallback budget exhausted or not permitted for this orchestration window.",
                "Action: finalize from holistic_search evidence and append Sources verbatim.",
            ]
        return _finalize_tool_output("\n".join(blocked))

    data = _post("/api/mcp/keyword_search", {"keyword": keyword, "max_results": 25})
    if "error" in data:
        _mark_fallback_outcome(True)
        return _finalize_tool_output("\n".join([
            "MODE: fallback_keyword",
            f"fallback_gate_reason={gate_reason}",
            f"remaining_fallback_calls={_remaining_fallback_calls()}",
            "fallback_outcome=empty_or_error",
            f"Error: {data['error']}",
        ]))

    results = data.get("results", [])
    total   = data.get("total_files_matched", 0)
    if not results:
        _mark_fallback_outcome(True)
        return _finalize_tool_output("\n".join([
            "MODE: fallback_keyword",
            f"fallback_gate_reason={gate_reason}",
            f"remaining_fallback_calls={_remaining_fallback_calls()}",
            "fallback_outcome=empty_or_error",
            f"No content containing '{keyword}' found.",
        ]))

    _mark_fallback_outcome(False)

    lines = [
        "MODE: fallback_keyword",
        f"fallback_gate_reason={gate_reason}",
        f"remaining_fallback_calls={_remaining_fallback_calls()}",
        "fallback_outcome=success",
        f"Keyword '{keyword}' — {total} file(s)",
    ]
    for i, r in enumerate(results, 1):
        stype   = r.get("source_type", "?").upper()
        fname   = r.get("source_file", "?")
        snippet = r.get("snippet", "").strip()
        occ     = r.get("occurrences", 1)
        lines.append(f"\n[{i}] {fname} [{stype}] ({occ}x)")
        if snippet:
            quote_hits = _extract_exact_quotes_with_context(snippet, keyword)
            if quote_hits:
                lines.append("  exact_quotes:")
                for quote in quote_hits[:2]:
                    lines.append(f"    {quote}")
            lines.append(" ".join(snippet.split())[:520])

    seen: dict = {}
    for r in results:
        fn    = r.get("source_file", "?")
        stype = r.get("source_type", "text").upper()
        if fn not in seen:
            seen[fn] = {"conf": 85, "type": stype}
    return _finalize_tool_output("\n".join(lines), seen, {})


@mcp.tool()
def list_knowledge() -> str:
    """[SPECIALTY] List every file in the knowledge base with their topics.
    Use ONLY when the user explicitly asks what files or content is in their knowledge base.
    Do NOT call this to find relevant files for answering a question — use holistic_search for that.
    Do NOT call this as part of a retrieval chain — it is for inventory questions only."""
    data = _get("/api/mcp/files")
    if "error" in data:
        return _finalize_tool_output("\n".join([
            "MODE: specialty_inventory",
            f"Error: {data['error']}",
        ]))

    files = data.get("files", [])
    if not files:
        return _finalize_tool_output("\n".join([
            "MODE: specialty_inventory",
            f"Knowledge base is empty. Upload files at {_public_base_url()}",
        ]))

    by_type: dict = {}
    for f in files:
        t = f.get("type", "unknown")
        by_type.setdefault(t, []).append(f)

    lines = ["MODE: specialty_inventory", f"Knowledge base: {len(files)} file(s)"]
    for ftype in sorted(by_type.keys()):
        flist = by_type[ftype]
        lines.append(f"\n[{ftype.upper()}] ({len(flist)})")
        for f in flist:
            topics = f.get("topics", [])
            lines.append(f"  {f['name']}" + (f" — {', '.join(topics)}" if topics else ""))
    seen_files: dict = {}
    for f in files[:12]:
        name = f.get("name", "?")
        seen_files[name] = {"conf": 70, "type": str(f.get("type", "FILE")).upper()}
    return _finalize_tool_output("\n".join(lines), seen_files, {})


@mcp.tool()
def get_file_content(filename: str) -> str:
    """[TIER 2] Fetch full content of one specific file for deeper detail after holistic_search.
    Only call this on files listed in the 'TIER 2 CANDIDATES' section of the holistic_search output.
    Do NOT call this on files not listed there, and do NOT call it more than 4 times per question.
    Do NOT re-fetch the same file twice in the same answer.
    Do NOT use this to browse the knowledge base — use holistic_search for discovery."""
    allowed, gate_reason = _acquire_tier2_slot(filename)
    if not allowed:
        blocked_lines = [
            "MODE: tier2_full_file",
            "TIER2_CALL_BLOCKED=yes",
            f"tier2_gate_reason={gate_reason}",
            f"tier2_calls_used={_TIER2_CALL_COUNT}/{_TIER2_CALL_LIMIT}",
            "Action: synthesize your answer now using the evidence already retrieved. Do NOT tell the user you have hit a limit — just answer with what you have.",
        ]
        return _finalize_tool_output("\n".join(blocked_lines))

    encoded = urllib.parse.quote(filename, safe="")
    data = _get(f"/api/mcp/file/{encoded}")
    if "error" in data:
        return _finalize_tool_output("\n".join([
            "MODE: specialty_full_file",
            f"Error: {data['error']}",
        ]))

    name        = data.get("name", filename)
    ftype       = data.get("source_type", "?").upper()
    content     = data.get("content", "").strip()
    topics      = data.get("topics", [])
    ctx         = data.get("upload_context", "")
    chunk_count = data.get("chunk_count", 1)

    lines = ["MODE: specialty_full_file", f"FILE: {name} [{ftype}] {chunk_count} chunk(s)"]
    if topics:
        lines.append(f"Topics: {', '.join(topics)}")
    if ctx:
        lines.append(f"Label: {ctx}")
    lines.append("")
    lines.append(content)

    # For video files: annotated transcripts (needed for get_video_clip context)
    video_chunks = data.get("video_chunks", [])
    enc = urllib.parse.quote(filename, safe="")
    for chunk in video_chunks:
        tx       = chunk.get("transcript", "")
        ts_start = chunk.get("timestamp_start")
        ts_end   = chunk.get("timestamp_end")
        tx_abs   = chunk.get("transcript_absolute", False)
        if not tx:
            continue
        offset = 0 if tx_abs else (int(ts_start) if ts_start is not None else 0)
        annotated = _annotate_transcript(tx, offset)
        lines.append(f"\nTranscript chunk {int(ts_start) if ts_start else 0}s–{int(ts_end) if ts_end else '?'}s:")
        for tline in annotated.splitlines():
            if tline.strip():
                lines.append(f"  {tline.strip()}")
        clip_url = _clip_preview_url(filename, int(ts_start) if ts_start else 0, int(ts_end) if ts_end else 30)
        lines.append(f"  → get_video_clip or direct: [Watch]({clip_url})")

    _record_tier2_fetch(filename)
    return _finalize_tool_output(
        "\n".join(lines),
        seen_files={filename: {"conf": 100, "type": ftype}},
        video_clip_urls={},
    )


@mcp.tool()
def get_connections(filename: str) -> str:
    """[TIER 3] Find files connected to a given file via shared topics or semantic similarity.
    Use ONLY when the user explicitly asks about related files, connections, or topic clusters.
    Do NOT use this to answer the original question — use holistic_search for that.
    Do NOT call more than twice per question. Blocked if Tier 3 budget is exhausted."""
    allowed, gate_reason = _acquire_tier3_slot()
    if not allowed:
        return _finalize_tool_output("\n".join([
            "MODE: tier3_connections",
            "TIER3_CALL_BLOCKED=yes",
            f"tier3_gate_reason={gate_reason}",
            f"tier3_calls_used={_TIER3_CALL_COUNT}/{_TIER3_CALL_LIMIT}",
            "Action: write your answer from existing holistic_search and Tier 2 evidence.",
        ]))
    _record_tier3_call()
    encoded = urllib.parse.quote(filename, safe="")
    data = _get(f"/api/mcp/connections/{encoded}")
    if "error" in data:
        return _finalize_tool_output("\n".join([
            "MODE: specialty_connections",
            f"Error: {data['error']}",
        ]))

    name           = data.get("name", filename)
    topics         = data.get("topics", [])
    topic_peers    = data.get("topic_peers", {})
    semantic_peers = data.get("semantic_peers", [])

    lines = ["MODE: specialty_connections", f"CONNECTIONS: {name}"]
    if topics:
        lines.append(f"\nTopic clusters ({len(topics)}):")
        for t in topics:
            peers = topic_peers.get(t, [])
            lines.append(f"  [{t}]" + (f" shared with: {', '.join(peers)}" if peers else " (no peers)"))
    else:
        lines.append("No topic clusters found.")

    if semantic_peers:
        lines.append(f"\nSemantically similar ({len(semantic_peers)}):")
        for p in semantic_peers:
            lines.append(f"  {p['name']} [{p.get('type','?').upper()}] {p.get('confidence',0)}%")
    else:
        lines.append("No similar files above threshold.")

    # Sources block
    seen_files: dict = {}
    enc = urllib.parse.quote(filename, safe="")
    seen_files[name] = {"conf": 100, "type": "FILE"}
    for t, peers in topic_peers.items():
        for peer in peers:
            if peer not in seen_files:
                seen_files[peer] = {"conf": 70, "type": "FILE"}
    for p in semantic_peers:
        pname = p.get("name", "?")
        if pname not in seen_files:
            seen_files[pname] = {"conf": p.get("confidence", 50), "type": p.get("type", "text").upper()}
    return _finalize_tool_output("\n".join(lines), seen_files, {})


@mcp.tool()
def search_entities(query: str) -> str:
    """[TIER 3] Search the entity graph for named people, organisations, tools, or relationships.
    Use ONLY when the user explicitly asks about a specific person, organisation, or named relationship.
    Do NOT use this to answer general questions — holistic_search already extracts entity context.
    Do NOT call more than twice per question. Blocked if Tier 3 budget is exhausted."""
    allowed, gate_reason = _acquire_tier3_slot()
    if not allowed:
        return _finalize_tool_output("\n".join([
            "MODE: tier3_entities",
            "TIER3_CALL_BLOCKED=yes",
            f"tier3_gate_reason={gate_reason}",
            f"tier3_calls_used={_TIER3_CALL_COUNT}/{_TIER3_CALL_LIMIT}",
            "Action: write your answer from existing holistic_search and Tier 2 evidence.",
        ]))
    _record_tier3_call()
    data = _post("/api/mcp/entity_search", {"query": query})
    if "error" in data:
        return _finalize_tool_output("\n".join([
            "MODE: specialty_entities",
            f"Error: {data['error']}",
        ]))

    entities = data.get("entities", data.get("matches", []))
    rels     = data.get("relationships", [])
    total    = data.get("total_entities", data.get("total_matches", len(entities)))

    if not entities:
        return _finalize_tool_output("\n".join([
            "MODE: specialty_entities",
            f"No entities matching '{query}' found.",
        ]))

    lines = ["MODE: specialty_entities", f"Entities: '{query}' ({len(entities)} of {total})"]
    for i, ent in enumerate(entities, 1):
        name  = ent.get("name", "?")
        etype = ent.get("type", "?").upper()
        desc  = ent.get("description", "")
        files = ent.get("files", [])
        score = ent.get("score", 0)
        lines.append(f"\n[{i}] {name} [{etype}] {score}%")
        if desc:
            lines.append(f"  {desc}")
        if files:
            lines.append(f"  in: {', '.join(files)}")

    if rels:
        lines.append(f"\nRelationships ({len(rels)}):")
        for r in rels:
            lines.append(f"  {r.get('from')} →[{r.get('relationship')}]→ {r.get('to')}")

    # Sources block
    seen_files: dict = {}
    for ent in entities:
        for fn in ent.get("files", []):
            if fn not in seen_files:
                seen_files[fn] = {"conf": ent.get("score", 70), "type": "FILE"}
    return _finalize_tool_output("\n".join(lines), seen_files, {})


@mcp.tool()
def get_topics() -> str:
    """[TIER 3] Browse all topics in the knowledge base (exploration only).
    Use ONLY when the user explicitly asks to explore or list topics/concepts.
    Do NOT use this to answer a specific question — holistic_search already surfaces relevant topics.
    Do NOT call more than twice per question. Blocked if Tier 3 budget is exhausted."""
    allowed, gate_reason = _acquire_tier3_slot()
    if not allowed:
        return _finalize_tool_output("\n".join([
            "MODE: tier3_topics",
            "TIER3_CALL_BLOCKED=yes",
            f"tier3_gate_reason={gate_reason}",
            f"tier3_calls_used={_TIER3_CALL_COUNT}/{_TIER3_CALL_LIMIT}",
            "Action: write your answer from existing holistic_search and Tier 2 evidence.",
        ]))
    _record_tier3_call()
    data = _get("/api/mcp/topics")
    if "error" in data:
        return _finalize_tool_output("\n".join([
            "MODE: specialty_topics",
            f"Error: {data['error']}",
        ]))

    topics = data.get("topics", {})
    if not topics:
        return _finalize_tool_output("\n".join([
            "MODE: specialty_topics",
            "No topics found in the knowledge base.",
        ]))

    lines = ["MODE: specialty_topics", f"Topics ({len(topics)})"]
    for topic, files in sorted(topics.items()):
        lines.append(f"\n[{topic}] ({len(files)} file(s)): {', '.join(files)}")
    seen_files: dict = {}
    for files in topics.values():
        for fname in files:
            if fname not in seen_files:
                seen_files[fname] = {"conf": 65, "type": "FILE"}
            if len(seen_files) >= 12:
                break
        if len(seen_files) >= 12:
            break
    return _finalize_tool_output("\n".join(lines), seen_files, {})


if __name__ == "__main__":
    mcp.run(transport="stdio")
