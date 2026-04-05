import argparse
import json
import re
from pathlib import Path

import main
import mcp_server


SOURCES_RE = re.compile(r"---\s*\n\*\*Sources\*\*[\s\S]*\n---\s*$")
ALLOWED_STATUSES = {"supported", "conflicting", "weak_support", "insufficient_evidence"}


def has_sources_block(text: str) -> bool:
    return bool(SOURCES_RE.search((text or "").rstrip()))


def default_questions() -> list[dict]:
    return [
        {
            "id": "phase3_q1",
            "query": "What are Dan and Ruby's hourly rates?",
            "expect_claims": True,
        },
        {
            "id": "phase3_q2",
            "query": "Who discussed $70 per hour and what alternatives were mentioned?",
            "expect_claims": True,
        },
        {
            "id": "phase3_q3",
            "query": "Summarize Dan and Ruby pricing discussions across files with any uncertainty.",
            "expect_claims": True,
        },
    ]


def run_backend_checks(questions: list[dict]) -> dict:
    rows = []
    validation_enabled = 0
    has_claims = 0
    adjudication_policy_ok = 0
    valid_status_rows = 0
    uncertainty_rows = 0
    attribution_rows = 0

    for q in questions:
        query = q.get("query", "")
        expect_claims = bool(q.get("expect_claims", True))

        result = main.mcp_holistic_search(main.MCPHolisticRequest(query=query))
        retrieval_meta = result.get("retrieval_meta", {}) or {}
        claim_checks = result.get("claim_verification", []) or []
        adjudication = result.get("claim_adjudication", {}) or {}

        if retrieval_meta.get("claim_validation_enabled"):
            validation_enabled += 1

        if claim_checks:
            has_claims += 1

        policy = adjudication.get("policy", {}) or {}
        if isinstance(policy.get("requires_uncertainty"), bool) and isinstance(policy.get("must_not_guess"), bool):
            adjudication_policy_ok += 1

        status_ok = True
        uncertainty_ok = True
        attribution_ok = True

        for claim in claim_checks:
            status = str(claim.get("status", ""))
            if status not in ALLOWED_STATUSES:
                status_ok = False

            if status != "supported" and not claim.get("uncertainty"):
                uncertainty_ok = False

            for ev in claim.get("evidence", []) or []:
                if "attribution" not in ev:
                    attribution_ok = False
                    break
            if not attribution_ok:
                break

        if status_ok:
            valid_status_rows += 1
        if uncertainty_ok:
            uncertainty_rows += 1
        if attribution_ok:
            attribution_rows += 1

        rows.append(
            {
                "id": q.get("id", "unknown"),
                "query": query,
                "expect_claims": expect_claims,
                "claim_validation_enabled": bool(retrieval_meta.get("claim_validation_enabled", False)),
                "claim_count": len(claim_checks),
                "status_ok": status_ok,
                "uncertainty_ok": uncertainty_ok,
                "attribution_ok": attribution_ok,
                "policy": policy,
                "counts": adjudication.get("counts", {}),
            }
        )

    total = len(rows) or 1
    summary = {
        "queries": len(rows),
        "claim_validation_enabled_rate": round(validation_enabled / total, 4),
        "has_claims_rate": round(has_claims / total, 4),
        "policy_shape_rate": round(adjudication_policy_ok / total, 4),
        "valid_status_rate": round(valid_status_rows / total, 4),
        "uncertainty_for_non_supported_rate": round(uncertainty_rows / total, 4),
        "attribution_field_rate": round(attribution_rows / total, 4),
    }
    return {"summary": summary, "rows": rows}


def run_render_checks(questions: list[dict]) -> dict:
    rows = []
    adjudication_present = 0
    sources_present = 0

    for q in questions:
        query = q.get("query", "")
        text = mcp_server.holistic_search(query)
        has_adj = "CLAIM ADJUDICATION" in (text or "")
        has_src = has_sources_block(text)
        if has_adj:
            adjudication_present += 1
        if has_src:
            sources_present += 1

        rows.append(
            {
                "id": q.get("id", "unknown"),
                "query": query,
                "claim_adjudication_rendered": has_adj,
                "sources_present": has_src,
            }
        )

    total = len(rows) or 1
    summary = {
        "queries": len(rows),
        "claim_adjudication_render_rate": round(adjudication_present / total, 4),
        "sources_block_rate": round(sources_present / total, 4),
    }
    return {"summary": summary, "rows": rows}


def main_cli() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 claim adjudication and rendering checks.")
    parser.add_argument("--questions", default="", help="Optional JSON file with query list")
    parser.add_argument("--min-validation-rate", type=float, default=0.95)
    parser.add_argument("--min-has-claims-rate", type=float, default=0.95)
    parser.add_argument("--min-policy-shape-rate", type=float, default=1.0)
    parser.add_argument("--min-valid-status-rate", type=float, default=1.0)
    parser.add_argument("--min-uncertainty-rate", type=float, default=0.95)
    parser.add_argument("--min-attribution-rate", type=float, default=0.95)
    parser.add_argument("--min-render-rate", type=float, default=0.95)
    parser.add_argument("--min-sources-rate", type=float, default=1.0)
    args = parser.parse_args()

    if args.questions:
        qpath = Path(args.questions)
        if not qpath.is_file():
            print(json.dumps({"error": f"Question fixture not found: {qpath}"}, indent=2))
            return 2
        questions = json.loads(qpath.read_text(encoding="utf-8"))
    else:
        questions = default_questions()

    backend = run_backend_checks(questions)
    render = run_render_checks(questions)

    b = backend["summary"]
    r = render["summary"]

    gates = {
        "claim_validation_enabled_rate": b["claim_validation_enabled_rate"] >= args.min_validation_rate,
        "has_claims_rate": b["has_claims_rate"] >= args.min_has_claims_rate,
        "policy_shape_rate": b["policy_shape_rate"] >= args.min_policy_shape_rate,
        "valid_status_rate": b["valid_status_rate"] >= args.min_valid_status_rate,
        "uncertainty_for_non_supported_rate": b["uncertainty_for_non_supported_rate"] >= args.min_uncertainty_rate,
        "attribution_field_rate": b["attribution_field_rate"] >= args.min_attribution_rate,
        "claim_adjudication_render_rate": r["claim_adjudication_render_rate"] >= args.min_render_rate,
        "sources_block_rate": r["sources_block_rate"] >= args.min_sources_rate,
    }

    payload = {
        "backend": b,
        "render": r,
        "gates": gates,
        "status": "PASS" if all(gates.values()) else "FAIL",
    }
    print(json.dumps(payload, indent=2))
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main_cli())
