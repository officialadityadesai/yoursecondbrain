import argparse
import json
import statistics
import re
from pathlib import Path

import main


def _is_process_query(query: str) -> bool:
    q = (query or "").lower()
    return bool(
        re.search(
            r"\b(process|workflow|steps?|sequence|lifecycle|pipeline|handoff|onboarding|how does|how do|what happens)\b",
            q,
        )
    )


def run_checks(questions: list[dict]) -> dict:
    rows = []
    broad_rows = []
    factual_rows = []
    process_rows = []

    for q in questions:
        query = q.get("query", "")
        qtype = q.get("type", "fact")
        req = main.MCPHolisticRequest(query=query)
        result = main.mcp_holistic_search(req)

        meta = result.get("retrieval_meta", {}) or {}
        evidence_files = result.get("evidence_files", []) or []
        signals_per_file = [len(ev.get("match_signals", [])) for ev in evidence_files]

        row = {
            "id": q.get("id"),
            "type": qtype,
            "query": query,
            "intent": meta.get("query_intent"),
            "process_query": bool(meta.get("process_query", False)),
            "process_steps": int(meta.get("process_steps", 0) or 0),
            "selected_files": int(meta.get("selected_files", len(evidence_files)) or 0),
            "evidence_files_len": len(evidence_files),
            "fusion_candidates": int(meta.get("fusion_candidates", 0) or 0),
            "avg_signals_per_file": round(statistics.mean(signals_per_file), 3) if signals_per_file else 0.0,
            "max_signals_per_file": max(signals_per_file) if signals_per_file else 0,
        }
        rows.append(row)
        if qtype == "broad":
            broad_rows.append(row)
        if qtype == "fact":
            factual_rows.append(row)
        if _is_process_query(query):
            process_rows.append(row)

    def _rate(items, predicate):
        if not items:
            return 1.0
        return sum(1 for i in items if predicate(i)) / len(items)

    summary = {
        "queries": len(rows),
        "avg_selected_files": round(statistics.mean([r["selected_files"] for r in rows]), 3) if rows else 0.0,
        "avg_fusion_candidates": round(statistics.mean([r["fusion_candidates"] for r in rows]), 3) if rows else 0.0,
        "broad_queries": len(broad_rows),
        "factual_queries": len(factual_rows),
        "process_queries": len(process_rows),
        "broad_intent_rate": round(_rate(broad_rows, lambda r: r.get("intent") == "broad"), 4),
        "factual_intent_rate": round(_rate(factual_rows, lambda r: r.get("intent") == "factual"), 4),
        "process_mode_detection_rate": round(_rate(process_rows, lambda r: r.get("process_query", False)), 4),
        "process_steps_presence_rate": round(_rate(process_rows, lambda r: r.get("process_steps", 0) > 0), 4),
        "broad_min4_selected_rate": round(_rate(broad_rows, lambda r: r.get("selected_files", 0) >= 4), 4),
        "broad_signal_diversity_rate": round(_rate(broad_rows, lambda r: r.get("avg_signals_per_file", 0.0) >= 1.6), 4),
        "all_have_evidence_rate": round(_rate(rows, lambda r: r.get("evidence_files_len", 0) > 0), 4),
    }

    return {"summary": summary, "rows": rows}


def main_cli() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 retrieval quality checks (direct backend endpoint call).")
    parser.add_argument("--questions", default=str(Path(__file__).with_name("mcp_benchmark_questions.json")))
    parser.add_argument("--min-broad-intent-rate", type=float, default=0.8)
    parser.add_argument("--min-factual-intent-rate", type=float, default=0.55)
    parser.add_argument("--min-process-mode-detection-rate", type=float, default=0.6)
    parser.add_argument("--min-process-steps-presence-rate", type=float, default=0.5)
    parser.add_argument("--min-broad-min4-rate", type=float, default=0.9)
    parser.add_argument("--min-broad-signal-diversity-rate", type=float, default=0.8)
    parser.add_argument("--min-all-evidence-rate", type=float, default=0.98)
    args = parser.parse_args()

    qpath = Path(args.questions)
    if not qpath.is_file():
        print(json.dumps({"error": f"Question fixture not found: {qpath}"}, indent=2))
        return 2

    questions = json.loads(qpath.read_text(encoding="utf-8"))
    result = run_checks(questions)
    s = result["summary"]

    gates = {
        "broad_intent_rate": s["broad_intent_rate"] >= args.min_broad_intent_rate,
        "factual_intent_rate": s["factual_intent_rate"] >= args.min_factual_intent_rate,
        "process_mode_detection_rate": s["process_mode_detection_rate"] >= args.min_process_mode_detection_rate,
        "process_steps_presence_rate": s["process_steps_presence_rate"] >= args.min_process_steps_presence_rate,
        "broad_min4_selected_rate": s["broad_min4_selected_rate"] >= args.min_broad_min4_rate,
        "broad_signal_diversity_rate": s["broad_signal_diversity_rate"] >= args.min_broad_signal_diversity_rate,
        "all_have_evidence_rate": s["all_have_evidence_rate"] >= args.min_all_evidence_rate,
    }

    payload = {
        "summary": s,
        "gates": gates,
        "status": "PASS" if all(gates.values()) else "FAIL",
    }
    print(json.dumps(payload, indent=2))
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main_cli())
