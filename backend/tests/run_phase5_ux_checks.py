import argparse
import json
import re
from pathlib import Path

import mcp_server


SOURCES_RE = re.compile(r"---\s*\n\*\*Sources\*\*[\s\S]*\n---\s*$")
VIEW_LINK_RE = re.compile(r"\[View(?:\s+\"[^\"]+\")?\]\(([^)]+)\)")
WATCH_LINK_RE = re.compile(r"\[Watch clip\]\(([^)]+)\)")


def has_sources_block(text: str) -> bool:
    return bool(SOURCES_RE.search((text or "").rstrip()))


def check_instruction_contract() -> dict:
    source_text = Path(mcp_server.__file__).read_text(encoding="utf-8")
    required_markers = [
        "PRIMARY RETRIEVAL PATH",
        "ANTI-THRASH POLICY",
        "FALLBACK_REQUIRED=yes",
        "best_effort_disclaimer",
        "copy_sources_block_verbatim",
        "comprehensive",
        "answer_first=yes",
        "limitation_sentence_max=1",
        "fallback_budget=1",
        "fallback_retry_rule=allow_one_extra_only_if_first_fallback_empty_or_error",
        "ANSWER PACKET",
    ]

    checks = {marker: (marker in source_text) for marker in required_markers}
    return {
        "checks": checks,
        "pass_rate": round(sum(1 for ok in checks.values() if ok) / len(checks), 4) if checks else 1.0,
    }


def check_tool_roles() -> dict:
    role_checks = {
        "holistic_primary_doc": "[PRIMARY]" in (mcp_server.holistic_search.__doc__ or ""),
        "search_brain_fallback_doc": "[FALLBACK]" in (mcp_server.search_brain.__doc__ or ""),
        "keyword_fallback_doc": "[FALLBACK]" in (mcp_server.keyword_search.__doc__ or ""),
        "topics_specialty_doc": "[SPECIALTY]" in (mcp_server.get_topics.__doc__ or ""),
        "entities_specialty_doc": "[SPECIALTY]" in (mcp_server.search_entities.__doc__ or ""),
        "file_content_specialty_doc": "[SPECIALTY]" in (mcp_server.get_file_content.__doc__ or ""),
    }
    return {
        "checks": role_checks,
        "pass_rate": round(sum(1 for ok in role_checks.values() if ok) / len(role_checks), 4),
    }


def run_synthetic_best_effort_case() -> dict:
    original_post = mcp_server._post

    def _mock_post(path: str, body: dict) -> dict:
        if path != "/api/mcp/holistic_search":
            return original_post(path, body)

        return {
            "semantic_results": [],
            "full_files": [],
            "keyword_hits": [],
            "connected_files": [],
            "evidence_files": [
                {
                    "source_file": "synthetic_pricing.md",
                    "source_type": "text",
                    "confidence": 71,
                    "confidence_reason": "synthetic uncertainty fixture",
                    "topics": ["pricing"],
                    "match_signals": ["semantic", "keyword"],
                    "matched_keywords": ["rate"],
                    "upload_context": "",
                    "evidence_snippets": [
                        "Pricing notes mention $70/hour in one passage and $90/hour in another."
                    ],
                    "truncated": False,
                }
            ],
            "claim_verification": [
                {
                    "subject": "dan",
                    "status": "conflicting",
                    "evidence": [
                        {
                            "source_file": "synthetic_pricing.md",
                            "confidence": 71,
                            "quote": "Dan appears with both $70/hour and $90/hour references.",
                            "attribution": "mentioned_other_speaker",
                            "speaker_label": "Host",
                        }
                    ],
                }
            ],
            "claim_adjudication": {
                "policy": {
                    "requires_uncertainty": True,
                    "can_answer_directly": False,
                    "must_not_guess": True,
                },
                "counts": {
                    "supported": 0,
                    "conflicting": 1,
                    "weak_support": 0,
                    "insufficient_evidence": 0,
                    "total": 1,
                },
                "supported": [],
                "conflicting": [
                    {
                        "subject": "dan",
                        "observed_values": ["$70/hour", "$90/hour"],
                        "uncertainty": "Conflicting evidence across passages.",
                    }
                ],
                "weak_support": [],
                "insufficient_evidence": [],
            },
            "retrieval_meta": {
                "rows_scanned": 1,
                "semantic_rows": 0,
                "query_intent": "factual",
                "selected_files": 1,
                "claim_validation_enabled": True,
                "claim_status_counts": {
                    "supported": 0,
                    "conflicting": 1,
                    "weak_support": 0,
                    "insufficient_evidence": 0,
                },
            },
        }

    mcp_server._post = _mock_post
    try:
        text = mcp_server.holistic_search("synthetic uncertainty probe")
    finally:
        mcp_server._post = original_post

    return {
        "tool": "holistic_search_synthetic",
        "has_orchestration_contract": "ORCHESTRATION CONTRACT" in text,
        "has_answer_packet": "ANSWER PACKET" in text,
        "has_answer_policy": "ANSWER POLICY" in text,
        "best_effort_disclaimer": "best_effort_disclaimer=" in text,
        "uncertainty_flag": "use_uncertainty_language=yes" in text,
        "limitation_one_sentence": "limitation_one_sentence:" in text,
        "do_not_guess": "do_not_guess=yes" in text,
        "has_sources_block": has_sources_block(text),
    }


def run_fallback_budget_case() -> dict:
    original_post = mcp_server._post

    def _mock_post(path: str, body: dict) -> dict:
        if path == "/api/mcp/holistic_search":
            return {"error": "synthetic fallback budget failure"}
        if path == "/api/mcp/search":
            return {"results": []}
        if path == "/api/mcp/keyword_search":
            return {"results": [], "total_files_matched": 0}
        return original_post(path, body)

    mcp_server._post = _mock_post
    try:
        primary = mcp_server.holistic_search("synthetic fallback budget probe")
        first = mcp_server.search_brain("synthetic fallback budget probe", top_k=3)
        second = mcp_server.keyword_search("synthetic fallback budget probe")
        third = mcp_server.search_brain("synthetic fallback budget probe", top_k=3)
    finally:
        mcp_server._post = original_post

    return {
        "tool": "fallback_budget_case",
        "primary_requires_fallback": "FALLBACK_REQUIRED=yes" in primary,
        "first_used_slot": "fallback_outcome=empty_or_error" in first and "FALLBACK_TOOL_CALL_BLOCKED=yes" not in first,
        "second_used_retry": "fallback_outcome=empty_or_error" in second and "FALLBACK_TOOL_CALL_BLOCKED=yes" not in second,
        "third_blocked": "FALLBACK_TOOL_CALL_BLOCKED=yes" in third,
        "third_block_reason": ("fallback_retry_already_used" in third) or ("fallback_budget_exhausted" in third),
        "has_sources_block": all(has_sources_block(t) for t in [primary, first, second, third]),
    }


def run_multimodal_link_case() -> dict:
    original_post = mcp_server._post

    def _mock_post(path: str, body: dict) -> dict:
        if path != "/api/mcp/holistic_search":
            return original_post(path, body)

        return {
            "semantic_results": [],
            "full_files": [],
            "keyword_hits": [],
            "connected_files": [],
            "evidence_files": [
                {
                    "source_file": "team-whiteboard.png",
                    "source_type": "image",
                    "confidence": 87,
                    "confidence_reason": "synthetic visual evidence",
                    "topics": ["workflow"],
                    "match_signals": ["semantic"],
                    "matched_keywords": ["diagram"],
                    "upload_context": "meeting snapshot",
                    "evidence_snippets": ["Whiteboard screenshot showing handoff sequence."],
                    "truncated": False,
                },
                {
                    "source_file": "ops-review.mp4",
                    "source_type": "video",
                    "confidence": 82,
                    "confidence_reason": "synthetic clip evidence",
                    "topics": ["process"],
                    "match_signals": ["semantic", "topic-neighbor"],
                    "matched_keywords": ["handoff"],
                    "upload_context": "operations review",
                    "evidence_snippets": ["Speaker explains monthly payment approval and handoff."],
                    "truncated": False,
                },
            ],
            "claim_verification": [],
            "claim_adjudication": {
                "policy": {
                    "requires_uncertainty": False,
                    "can_answer_directly": True,
                    "must_not_guess": False,
                },
                "counts": {
                    "supported": 0,
                    "conflicting": 0,
                    "weak_support": 0,
                    "insufficient_evidence": 0,
                    "total": 0,
                },
                "supported": [],
                "conflicting": [],
                "weak_support": [],
                "insufficient_evidence": [],
            },
            "retrieval_meta": {
                "rows_scanned": 2,
                "semantic_rows": 0,
                "query_intent": "process",
                "selected_files": 2,
                "process_query": True,
                "process_steps": 2,
                "process_source_files": 2,
                "answer_first_contract": True,
                "limitation_sentence_max": 1,
            },
        }

    mcp_server._post = _mock_post
    try:
        text = mcp_server.holistic_search("show visual process evidence")
    finally:
        mcp_server._post = original_post

    view_links = VIEW_LINK_RE.findall(text or "")
    watch_links = WATCH_LINK_RE.findall(text or "")

    return {
        "tool": "multimodal_link_case",
        "has_sources_block": has_sources_block(text),
        "has_view_link": bool(view_links),
        "has_watch_link": bool(watch_links),
        "has_multimodal_contract_marker": "must_include_view_or_watch_links_when_visual_question=yes" in text,
        "view_link_shape_ok": all(link.startswith("http://") or link.startswith("https://") or link.startswith("/") for link in view_links),
        "watch_link_shape_ok": all(link.startswith("http://") or link.startswith("https://") or link.startswith("/") for link in watch_links),
    }


def run_fallback_query_isolation_case() -> dict:
    original_post = mcp_server._post

    def _mock_post(path: str, body: dict) -> dict:
        if path == "/api/mcp/holistic_search":
            query = str((body or {}).get("query", ""))
            if "first" in query:
                return {"error": "synthetic failure first query"}
            if "second" in query:
                return {"error": "synthetic failure second query"}
            return {"error": "synthetic failure default"}
        if path == "/api/mcp/search":
            return {"results": []}
        if path == "/api/mcp/keyword_search":
            return {"results": [], "total_files_matched": 0}
        return original_post(path, body)

    mcp_server._post = _mock_post
    try:
        first_primary = mcp_server.holistic_search("first synthetic query")
        first_fallback = mcp_server.search_brain("first synthetic query", top_k=3)

        second_primary = mcp_server.holistic_search("second synthetic query")
        mismatch_after_second = mcp_server.search_brain("first synthetic query", top_k=3)
        second_fallback = mcp_server.search_brain("second synthetic query", top_k=3)
    finally:
        mcp_server._post = original_post

    return {
        "tool": "fallback_query_isolation_case",
        "first_primary_requires_fallback": "FALLBACK_REQUIRED=yes" in first_primary,
        "first_fallback_used": "fallback_outcome=empty_or_error" in first_fallback and "FALLBACK_TOOL_CALL_BLOCKED=yes" not in first_fallback,
        "second_primary_requires_fallback": "FALLBACK_REQUIRED=yes" in second_primary,
        "mismatch_blocked": "FALLBACK_TOOL_CALL_BLOCKED=yes" in mismatch_after_second and "fallback_query_mismatch" in mismatch_after_second,
        "second_query_fallback_allowed": "fallback_outcome=empty_or_error" in second_fallback and "FALLBACK_TOOL_CALL_BLOCKED=yes" not in second_fallback,
        "has_sources_block": all(has_sources_block(t) for t in [first_primary, first_fallback, second_primary, mismatch_after_second, second_fallback]),
    }


def check_output_contract() -> dict:
    rows = []

    holistic_queries = [
        "What are Dan and Ruby's hourly rates?",
        "Summarize Dan and Ruby pricing discussions across files with any uncertainty.",
        "Summarize the major themes across my files.",
        "zxqvbnm asdfghjkl 11223344556677889900",
    ]

    best_effort_count = 0
    answer_policy_count = 0
    primary_mode_count = 0
    source_block_count = 0
    response_requirements_count = 0
    answer_packet_count = 0
    orchestration_contract_count = 0

    for query in holistic_queries:
        text = mcp_server.holistic_search(query)
        has_primary_mode = "MODE: primary_holistic" in text
        has_response_requirements = "RESPONSE REQUIREMENTS" in text
        has_answer_policy = "ANSWER POLICY" in text
        has_best_effort = "best_effort_disclaimer=" in text
        has_uncertainty = "use_uncertainty_language=yes" in text
        has_answer_packet = "ANSWER PACKET" in text
        has_orchestration_contract = (
            "ORCHESTRATION CONTRACT" in text
            and "answer_first=yes" in text
            and "limitation_sentence_max=1" in text
            and "fallback_budget=1" in text
        )
        has_sources = has_sources_block(text)

        if has_best_effort:
            best_effort_count += 1
        if has_answer_policy:
            answer_policy_count += 1
        if has_answer_packet:
            answer_packet_count += 1
        if has_orchestration_contract:
            orchestration_contract_count += 1
        if has_response_requirements:
            response_requirements_count += 1
        if has_primary_mode:
            primary_mode_count += 1
        if has_sources:
            source_block_count += 1

        rows.append(
            {
                "tool": "holistic_search",
                "query": query,
                "has_primary_mode": has_primary_mode,
                "has_response_requirements": has_response_requirements,
                "has_answer_policy": has_answer_policy,
                "has_answer_packet": has_answer_packet,
                "has_orchestration_contract": has_orchestration_contract,
                "best_effort_disclaimer": has_best_effort,
                "uncertainty_flag": has_uncertainty,
                "best_effort_implies_uncertainty": (not has_best_effort) or has_uncertainty,
                "has_sources_block": has_sources,
            }
        )

    fallback_cases = [
        ("search_brain", mcp_server.search_brain("workflow automation", top_k=6), "MODE: fallback_semantic"),
        ("keyword_search", mcp_server.keyword_search("rate"), "MODE: fallback_keyword"),
        ("get_topics", mcp_server.get_topics(), "MODE: specialty_topics"),
        ("search_entities", mcp_server.search_entities("Dan"), "MODE: specialty_entities"),
    ]

    fallback_mode_count = 0
    fallback_sources_count = 0
    best_effort_implies_uncertainty_ok = True

    for tool_name, text, mode_marker in fallback_cases:
        has_mode = mode_marker in text
        has_sources = has_sources_block(text)
        if has_mode:
            fallback_mode_count += 1
        if has_sources:
            fallback_sources_count += 1
        rows.append(
            {
                "tool": tool_name,
                "mode_marker": mode_marker,
                "has_mode_marker": has_mode,
                "has_sources_block": has_sources,
            }
        )

    for row in rows:
        if row.get("tool") == "holistic_search" and not row.get("best_effort_implies_uncertainty", True):
            best_effort_implies_uncertainty_ok = False
            break

    synthetic_row = run_synthetic_best_effort_case()
    rows.append(synthetic_row)
    synthetic_best_effort_case = bool(
        synthetic_row.get("has_orchestration_contract")
        and synthetic_row.get("has_answer_packet")
        and synthetic_row.get("has_answer_policy")
        and synthetic_row.get("best_effort_disclaimer")
        and synthetic_row.get("uncertainty_flag")
        and synthetic_row.get("limitation_one_sentence")
        and synthetic_row.get("do_not_guess")
        and synthetic_row.get("has_sources_block")
    )

    # Deterministic fallback-required case
    original_post = mcp_server._post

    def _mock_fallback_error(path: str, body: dict) -> dict:
        if path == "/api/mcp/holistic_search":
            return {"error": "synthetic primary retrieval failure"}
        return original_post(path, body)

    mcp_server._post = _mock_fallback_error
    try:
        fallback_required_text = mcp_server.holistic_search("synthetic fallback required probe")
    finally:
        mcp_server._post = original_post

    fallback_required_case = {
        "tool": "holistic_search_fallback_required",
        "has_fallback_required_yes": "FALLBACK_REQUIRED=yes" in fallback_required_text,
        "has_orchestration_contract": "ORCHESTRATION CONTRACT" in fallback_required_text,
        "has_answer_policy": "ANSWER POLICY" in fallback_required_text,
        "has_sources_block": has_sources_block(fallback_required_text),
    }
    rows.append(fallback_required_case)

    fallback_budget_case = run_fallback_budget_case()
    rows.append(fallback_budget_case)

    multimodal_link_case = run_multimodal_link_case()
    rows.append(multimodal_link_case)

    fallback_query_isolation_case = run_fallback_query_isolation_case()
    rows.append(fallback_query_isolation_case)

    summary = {
        "holistic_queries": len(holistic_queries),
        "primary_mode_rate": round(primary_mode_count / len(holistic_queries), 4),
        "response_requirements_rate": round(response_requirements_count / len(holistic_queries), 4),
        "answer_policy_rate": round(answer_policy_count / len(holistic_queries), 4),
        "answer_packet_rate": round(answer_packet_count / len(holistic_queries), 4),
        "orchestration_contract_rate": round(orchestration_contract_count / len(holistic_queries), 4),
        "best_effort_presence_rate": round(best_effort_count / len(holistic_queries), 4),
        "holistic_sources_rate": round(source_block_count / len(holistic_queries), 4),
        "fallback_cases": len(fallback_cases),
        "fallback_mode_rate": round(fallback_mode_count / len(fallback_cases), 4),
        "fallback_sources_rate": round(fallback_sources_count / len(fallback_cases), 4),
        "best_effort_implies_uncertainty": best_effort_implies_uncertainty_ok,
        "synthetic_best_effort_case": synthetic_best_effort_case,
        "synthetic_fallback_required_case": bool(
            fallback_required_case.get("has_fallback_required_yes")
            and fallback_required_case.get("has_orchestration_contract")
            and fallback_required_case.get("has_answer_policy")
            and fallback_required_case.get("has_sources_block")
        ),
        "fallback_budget_case": bool(
            fallback_budget_case.get("primary_requires_fallback")
            and fallback_budget_case.get("first_used_slot")
            and fallback_budget_case.get("second_used_retry")
            and fallback_budget_case.get("third_blocked")
            and fallback_budget_case.get("third_block_reason")
            and fallback_budget_case.get("has_sources_block")
        ),
        "multimodal_link_case": bool(
            multimodal_link_case.get("has_sources_block")
            and multimodal_link_case.get("has_view_link")
            and multimodal_link_case.get("has_watch_link")
            and multimodal_link_case.get("has_multimodal_contract_marker")
            and multimodal_link_case.get("view_link_shape_ok")
            and multimodal_link_case.get("watch_link_shape_ok")
        ),
        "fallback_query_isolation_case": bool(
            fallback_query_isolation_case.get("first_primary_requires_fallback")
            and fallback_query_isolation_case.get("first_fallback_used")
            and fallback_query_isolation_case.get("second_primary_requires_fallback")
            and fallback_query_isolation_case.get("mismatch_blocked")
            and fallback_query_isolation_case.get("second_query_fallback_allowed")
            and fallback_query_isolation_case.get("has_sources_block")
        ),
    }

    return {"summary": summary, "rows": rows}


def main_cli() -> int:
    parser = argparse.ArgumentParser(description="Phase 5 MCP UX/tool-orchestration checks.")
    parser.add_argument("--min-instruction-pass-rate", type=float, default=1.0)
    parser.add_argument("--min-tool-role-pass-rate", type=float, default=1.0)
    parser.add_argument("--min-primary-mode-rate", type=float, default=1.0)
    parser.add_argument("--min-response-requirements-rate", type=float, default=1.0)
    parser.add_argument("--min-answer-policy-rate", type=float, default=1.0)
    parser.add_argument("--min-answer-packet-rate", type=float, default=1.0)
    parser.add_argument("--min-orchestration-contract-rate", type=float, default=1.0)
    parser.add_argument("--min-holistic-sources-rate", type=float, default=1.0)
    parser.add_argument("--min-fallback-mode-rate", type=float, default=1.0)
    parser.add_argument("--min-fallback-sources-rate", type=float, default=1.0)
    args = parser.parse_args()

    instruction = check_instruction_contract()
    roles = check_tool_roles()
    outputs = check_output_contract()

    s = outputs["summary"]

    gates = {
        "instruction_contract": instruction["pass_rate"] >= args.min_instruction_pass_rate,
        "tool_roles": roles["pass_rate"] >= args.min_tool_role_pass_rate,
        "primary_mode_rate": s["primary_mode_rate"] >= args.min_primary_mode_rate,
        "response_requirements_rate": s["response_requirements_rate"] >= args.min_response_requirements_rate,
        "answer_policy_rate": s["answer_policy_rate"] >= args.min_answer_policy_rate,
        "answer_packet_rate": s["answer_packet_rate"] >= args.min_answer_packet_rate,
        "orchestration_contract_rate": s["orchestration_contract_rate"] >= args.min_orchestration_contract_rate,
        "holistic_sources_rate": s["holistic_sources_rate"] >= args.min_holistic_sources_rate,
        "fallback_mode_rate": s["fallback_mode_rate"] >= args.min_fallback_mode_rate,
        "fallback_sources_rate": s["fallback_sources_rate"] >= args.min_fallback_sources_rate,
        "best_effort_implies_uncertainty": bool(s["best_effort_implies_uncertainty"]),
        "synthetic_best_effort_case": bool(s["synthetic_best_effort_case"]),
        "synthetic_fallback_required_case": bool(s["synthetic_fallback_required_case"]),
        "fallback_budget_case": bool(s["fallback_budget_case"]),
        "multimodal_link_case": bool(s["multimodal_link_case"]),
        "fallback_query_isolation_case": bool(s["fallback_query_isolation_case"]),
    }

    payload = {
        "instruction": instruction,
        "roles": roles,
        "outputs": s,
        "gates": gates,
        "status": "PASS" if all(gates.values()) else "FAIL",
    }
    print(json.dumps(payload, indent=2))
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main_cli())
