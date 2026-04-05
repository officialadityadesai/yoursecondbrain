import argparse
import json
import statistics
from pathlib import Path

import main


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = max(0, int(len(ordered) * 0.95) - 1)
    return ordered[idx]


def _load_questions(path: Path, max_questions: int) -> list[dict]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []

    selected = [q for q in data if isinstance(q, dict) and q.get("query")]
    if max_questions > 0:
        selected = selected[:max_questions]
    return selected


def run_holistic_pass(questions: list[dict]) -> dict:
    rows = []
    for q in questions:
        query = str(q.get("query") or "")
        if not query:
            continue

        result = main.mcp_holistic_search(main.MCPHolisticRequest(query=query))
        meta = result.get("retrieval_meta", {}) or {}

        rows.append(
            {
                "query": query,
                "sidecar_mode": bool(meta.get("sidecar_mode", False)),
                "rows_scanned": int(meta.get("rows_scanned", 0) or 0),
                "candidate_files": int(meta.get("candidate_files", 0) or 0),
                "sidecar_files_loaded": int(meta.get("sidecar_files_loaded", 0) or 0),
                "embedding_cache_hit": bool(meta.get("embedding_cache_hit", False)),
            }
        )

    total = len(rows) or 1
    summary = {
        "queries": len(rows),
        "sidecar_mode_rate": round(sum(1 for r in rows if r["sidecar_mode"]) / total, 4),
        "avg_rows_scanned": round(statistics.mean([r["rows_scanned"] for r in rows]), 2) if rows else 0.0,
        "p95_rows_scanned": _p95([r["rows_scanned"] for r in rows]),
        "avg_candidate_files": round(statistics.mean([r["candidate_files"] for r in rows]), 2) if rows else 0.0,
        "avg_sidecar_files_loaded": round(statistics.mean([r["sidecar_files_loaded"] for r in rows]), 2) if rows else 0.0,
        "embedding_cache_hit_rate": round(sum(1 for r in rows if r["embedding_cache_hit"]) / total, 4),
    }
    return {"summary": summary, "rows": rows}


def run_keyword_checks() -> dict:
    checks = [
        "rate",
        "pricing",
        "automation",
        "dan",
    ]
    rows = []
    for keyword in checks:
        result = main.mcp_keyword_search(main.MCPKeywordSearchRequest(keyword=keyword, max_results=12))
        meta = result.get("retrieval_meta", {}) or {}
        rows.append(
            {
                "keyword": keyword,
                "scan_mode": str(meta.get("scan_mode", "")),
                "rows_scanned": int(meta.get("rows_scanned", 0) or 0),
                "candidate_files": int(meta.get("candidate_files", 0) or 0),
            }
        )

    total = len(rows) or 1
    summary = {
        "checks": len(rows),
        "sidecar_scan_mode_rate": round(sum(1 for r in rows if r["scan_mode"] == "sidecar_index") / total, 4),
        "max_rows_scanned": max([r["rows_scanned"] for r in rows], default=0),
        "avg_candidate_files": round(statistics.mean([r["candidate_files"] for r in rows]), 2) if rows else 0.0,
    }
    return {"summary": summary, "rows": rows}


def run_endpoint_meta_checks() -> dict:
    topics = main.mcp_get_topics()
    entities = main.mcp_get_entities()
    entity_search = main.mcp_entity_search(main.MCPEntitySearchRequest(query="dan", entity_types=[]))

    rows = [
        {
            "endpoint": "topics",
            "sidecar_meta": (topics.get("retrieval_meta", {}) or {}).get("scan_mode") == "sidecar_index",
        },
        {
            "endpoint": "entities",
            "sidecar_meta": (entities.get("retrieval_meta", {}) or {}).get("scan_mode") == "sidecar_index",
        },
        {
            "endpoint": "entity_search",
            "sidecar_meta": (entity_search.get("retrieval_meta", {}) or {}).get("scan_mode") == "sidecar_index",
        },
    ]

    total = len(rows) or 1
    summary = {
        "checks": len(rows),
        "sidecar_endpoint_meta_rate": round(sum(1 for r in rows if r["sidecar_meta"]) / total, 4),
    }
    return {"summary": summary, "rows": rows}


def main_cli() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 scale-path checks (sidecar retrieval + bounded scans).")
    parser.add_argument("--questions", default=str(Path(__file__).with_name("mcp_benchmark_questions.json")))
    parser.add_argument("--max-questions", type=int, default=20)
    parser.add_argument("--min-sidecar-mode-rate", type=float, default=1.0)
    parser.add_argument("--max-p95-rows-scanned", type=int, default=9000)
    parser.add_argument("--min-second-pass-embed-cache-hit-rate", type=float, default=0.9)
    parser.add_argument("--min-keyword-sidecar-rate", type=float, default=1.0)
    parser.add_argument("--min-endpoint-sidecar-rate", type=float, default=1.0)
    args = parser.parse_args()

    questions = _load_questions(Path(args.questions), args.max_questions)
    if not questions:
        print(json.dumps({"error": f"No valid questions found in fixture: {args.questions}"}, indent=2))
        return 2

    first_pass = run_holistic_pass(questions)
    second_pass = run_holistic_pass(questions)
    keyword = run_keyword_checks()
    endpoint_meta = run_endpoint_meta_checks()

    first = first_pass["summary"]
    second = second_pass["summary"]
    ksum = keyword["summary"]
    esum = endpoint_meta["summary"]

    gates = {
        "sidecar_mode_rate": first["sidecar_mode_rate"] >= args.min_sidecar_mode_rate,
        "p95_rows_scanned": first["p95_rows_scanned"] <= args.max_p95_rows_scanned,
        "second_pass_embed_cache_hit_rate": second["embedding_cache_hit_rate"] >= args.min_second_pass_embed_cache_hit_rate,
        "keyword_sidecar_rate": ksum["sidecar_scan_mode_rate"] >= args.min_keyword_sidecar_rate,
        "endpoint_sidecar_rate": esum["sidecar_endpoint_meta_rate"] >= args.min_endpoint_sidecar_rate,
    }

    payload = {
        "holistic_first_pass": first,
        "holistic_second_pass": second,
        "keyword": ksum,
        "endpoint_meta": esum,
        "gates": gates,
        "status": "PASS" if all(gates.values()) else "FAIL",
    }
    print(json.dumps(payload, indent=2))
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main_cli())
