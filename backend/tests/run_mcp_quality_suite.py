import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

import mcp_server

SOURCES_RE = re.compile(r"---\s*\n\*\*Sources\*\*[\s\S]*\n---\s*$")
SOURCE_LINE_RE = re.compile(r"^\s*[\-•]\s+", re.MULTILINE)


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text or "") / 4))


def has_sources_block(text: str) -> bool:
    return bool(SOURCES_RE.search((text or "").rstrip()))


def extract_sources_count(text: str) -> int:
    if not text:
        return 0
    block_match = SOURCES_RE.search(text.rstrip())
    if not block_match:
        return 0
    block = block_match.group(0)
    lines = SOURCE_LINE_RE.findall(block)
    return len(lines)


def run_holistic_suite(questions: list[dict]) -> dict:
    rows = []
    token_values = []
    latency_values = []
    sources_present = 0
    one_source_count = 0
    broad_count = 0
    broad_ok = 0

    for q in questions:
        query = q.get("query", "")
        qid = q.get("id", "unknown")
        qtype = q.get("type", "fact")
        min_sources = int(q.get("min_sources", 1) or 1)

        start = time.perf_counter()
        out = mcp_server.holistic_search(query)
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        tok = estimate_tokens(out)
        src_ok = has_sources_block(out)
        src_count = extract_sources_count(out)

        if src_ok:
            sources_present += 1
        if src_count <= 1:
            one_source_count += 1

        if qtype == "broad":
            broad_count += 1
            if src_count >= min_sources:
                broad_ok += 1

        token_values.append(tok)
        latency_values.append(elapsed_ms)
        rows.append(
            {
                "id": qid,
                "type": qtype,
                "query": query,
                "tokens_est": tok,
                "latency_ms": elapsed_ms,
                "sources_present": src_ok,
                "source_count": src_count,
                "min_sources_required": min_sources,
            }
        )

    count = len(rows) or 1
    summary = {
        "queries": len(rows),
        "sources_present": sources_present,
        "sources_rate": round(sources_present / count, 4),
        "tokens_avg": round(statistics.mean(token_values), 2) if token_values else 0,
        "tokens_median": round(statistics.median(token_values), 2) if token_values else 0,
        "tokens_p95": sorted(token_values)[max(0, int(len(token_values) * 0.95) - 1)] if token_values else 0,
        "tokens_max": max(token_values) if token_values else 0,
        "latency_avg_ms": round(statistics.mean(latency_values), 2) if latency_values else 0,
        "latency_p95_ms": sorted(latency_values)[max(0, int(len(latency_values) * 0.95) - 1)] if latency_values else 0,
        "latency_max_ms": max(latency_values) if latency_values else 0,
        "one_source_rate": round(one_source_count / count, 4),
        "broad_queries": broad_count,
        "broad_multi_source_rate": round((broad_ok / broad_count), 4) if broad_count else 1.0,
    }
    return {"summary": summary, "rows": rows}


def run_tool_sources_regression() -> dict:
    checks = [
        ("search_brain", lambda: mcp_server.search_brain("workflow automation", top_k=6)),
        ("keyword_search", lambda: mcp_server.keyword_search("rate")),
        ("list_knowledge", lambda: mcp_server.list_knowledge()),
        ("get_topics", lambda: mcp_server.get_topics()),
        ("search_entities", lambda: mcp_server.search_entities("Dan")),
        ("get_file_content_not_found", lambda: mcp_server.get_file_content("definitely_not_a_real_file_12345.md")),
        ("get_connections_not_found", lambda: mcp_server.get_connections("definitely_not_a_real_file_12345.md")),
        ("get_video_clip_not_found", lambda: mcp_server.get_video_clip("definitely_not_a_real_video_12345.mp4", "pricing")),
    ]

    rows = []
    passed = 0
    for name, fn in checks:
        start = time.perf_counter()
        text = fn()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        ok = has_sources_block(text)
        if ok:
            passed += 1
        rows.append(
            {
                "tool": name,
                "sources_present": ok,
                "tokens_est": estimate_tokens(text),
                "latency_ms": elapsed_ms,
            }
        )

    return {
        "summary": {
            "checks": len(checks),
            "passed": passed,
            "pass_rate": round(passed / len(checks), 4) if checks else 1.0,
        },
        "rows": rows,
    }


def evaluate_gates(holistic_summary: dict, tool_summary: dict, args: argparse.Namespace) -> dict:
    gates = {
        "sources_rate": holistic_summary["sources_rate"] >= args.min_sources_rate,
        "tokens_avg": holistic_summary["tokens_avg"] <= args.max_avg_tokens,
        "tokens_p95": holistic_summary["tokens_p95"] <= args.max_p95_tokens,
        "latency_p95": holistic_summary["latency_p95_ms"] <= args.max_p95_latency_ms,
        "broad_multi_source_rate": holistic_summary["broad_multi_source_rate"] >= args.min_broad_multi_source_rate,
        "fallback_sources": tool_summary["pass_rate"] >= args.min_tool_sources_rate,
    }
    return gates


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MCP quality suite for holistic retrieval and sources enforcement.")
    parser.add_argument("--questions", default=str(Path(__file__).with_name("mcp_benchmark_questions.json")), help="Path to question fixture JSON")
    parser.add_argument("--min-sources-rate", type=float, default=1.0)
    parser.add_argument("--max-avg-tokens", type=float, default=18000)
    parser.add_argument("--max-p95-tokens", type=float, default=24000)
    parser.add_argument("--max-p95-latency-ms", type=int, default=3500)
    parser.add_argument("--min-broad-multi-source-rate", type=float, default=0.75)
    parser.add_argument("--min-tool-sources-rate", type=float, default=1.0)
    args = parser.parse_args()

    questions_path = Path(args.questions)
    if not questions_path.is_file():
        print(json.dumps({"error": f"Question file not found: {questions_path}"}, indent=2))
        return 2

    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    holistic = run_holistic_suite(questions)
    tools = run_tool_sources_regression()

    gates = evaluate_gates(holistic["summary"], tools["summary"], args)
    passed = all(gates.values())

    result = {
        "holistic": holistic["summary"],
        "tools": tools["summary"],
        "gates": gates,
        "status": "PASS" if passed else "FAIL",
    }
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
