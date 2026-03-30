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

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT  = 20.0


def _get(path: str) -> dict:
    try:
        r = _requests.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except _requests.exceptions.ConnectionError:
        return {"error": "Cannot connect to My Second Brain. Make sure the app is running at http://127.0.0.1:8000."}
    except Exception as e:
        return {"error": f"Request failed: {str(e)}"}


def _post(path: str, body: dict) -> dict:
    try:
        r = _requests.post(f"{BASE_URL}{path}", json=body, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except _requests.exceptions.ConnectionError:
        return {"error": "Cannot connect to My Second Brain. Make sure the app is running at http://127.0.0.1:8000."}
    except Exception as e:
        return {"error": f"Request failed: {str(e)}"}


mcp = FastMCP(
    "My Second Brain",
    instructions=(
        "You are the user's Second Brain — a brilliant analyst who has read everything they saved.\n"
        "Your job: synthesise, not recite. Give answers like a senior advisor who studied all the material.\n\n"

        "RULES (violating any = critical failure):\n\n"

        "R1 IMAGES: You cannot display images. Always include the clickable link from '📸' lines.\n"
        "  Never say 'I can't show the image' without the link.\n\n"

        "R2 VIDEO CLIPS: This is a real, working feature. Never say 'I can't trim videos' or 'use CapCut'.\n"
        "  Workflow: (1) holistic_search → get filename. (2) get_video_clip(file, topic). (3) paste [Watch clip](url).\n"
        "  Always call get_video_clip for any clip request. Never parse timestamps yourself.\n\n"

        "R3 SOURCES: Your response is INCOMPLETE without a sources block at the end. No exceptions.\n"
        "  Copy the '---\\n**Sources**\\n...\\n---' block from the tool output and paste it at the very end.\n"
        "  Only include sources that actually contributed to your answer (1–3 max).\n"
        "  If you used holistic_search, get_video_clip, or any other tool — sources block is mandatory.\n\n"

        "R4 ONE SEARCH: Call holistic_search once. Read all 4 sections before writing. Don't chain more tools.\n\n"

        "TOOLS:\n"
        "holistic_search — use for every question (semantic + keyword + full content + topics in one call)\n"
        "get_video_clip  — use for any clip request; returns ready-made URL, no timestamp math needed\n"
        "search_entities — only for questions about a specific named person or organisation\n"
        "get_connections — only when user asks how files relate to each other\n"
        "list_knowledge  — only when user explicitly asks 'what files do I have?'\n"
        "get_file_content— only when user names a specific file AND holistic_search missed it\n"
        "search_brain / keyword_search — fallback only if holistic_search returns an error\n\n"

        "HOW TO ANSWER:\n\n"

        "1. READ ALL 4 SECTIONS before writing. The answer is often in a transcript, keyword hit,\n"
        "   or connected file — not the top semantic result. Check everything.\n\n"

        "2. FIND THE ACTUAL ANSWER FIRST. Locate the exact quote or passage that directly answers\n"
        "   the question. Lead with it. Never bury the answer after paragraphs of context.\n"
        "   WRONG: 'In this video, Mark Cuban discusses many aspects of entrepreneurship...'\n"
        "   RIGHT: '**Mark Cuban:** \"The best founders obsess over the problem, not the exit.\" [video ~2:14]'\n\n"

        "3. SYNTHESISE ACROSS SOURCES. If multiple files touch the same topic, weave them together.\n"
        "   Show where sources agree or differ. That synthesis is the value.\n\n"

        "4. DEPTH OVER BREVITY. Every bullet must carry specific information — names, quotes, numbers.\n"
        "   BAD: '• Mark Cuban talks about founder mindset'\n"
        "   GOOD: '• **Mark Cuban [~2:14]:** \"The ones who win are obsessed with the problem.\"'\n\n"

        "5. FORMAT: Bold key names/numbers/quotes. Bullet points. No markdown headings (#).\n"
        "   Open with a bolded direct answer. Then evidence bullets. Then one-sentence gap note if needed.\n\n"

        "ACCURACY: Copy numbers, names, prices, dates exactly. Never estimate.\n"
        "Quote before you state: '[source]: \"exact words\"' then your conclusion.\n"
        "If two sources differ on a fact — show both, attributed separately.\n"
        "If a fact isn't in the results — say 'I didn't find [X]', nothing more.\n\n"

        "HONESTY: If it's not in the knowledge base, say so in one sentence. Never fill gaps with guesses."
    ),
)


def _build_sources_block(seen_files: dict, video_clip_urls: dict) -> list[str]:
    """Build the compact sources block shared across tools."""
    top_sources = sorted(seen_files.items(), key=lambda x: -x[1]["conf"])[:3]
    if not top_sources:
        return []
    lines = ["\n---", "**Sources**"]
    for fn, meta in top_sources:
        stype = meta["type"]
        conf  = meta["conf"]
        enc   = urllib.parse.quote(fn)
        suffix = ""
        if stype == "IMAGE":
            suffix = f"  [View](http://127.0.0.1:8000/node/{enc})"
        elif stype == "VIDEO":
            clip_url = video_clip_urls.get(fn, f"http://127.0.0.1:8000/clip?file={enc}&start=0&end=30")
            suffix = f"  [Watch clip]({clip_url})"
        lines.append(f"• {fn} [{stype}]{suffix}")
    lines.append("---")
    return lines


@mcp.tool()
def holistic_search(query: str) -> str:
    """Search the entire knowledge base: semantic + keyword + full content + topic connections."""
    data = _post("/api/mcp/holistic_search", {"query": query})
    if "error" in data:
        return f"Error: {data['error']}"

    sem    = data.get("semantic_results", [])
    fulls  = data.get("full_files", [])
    kwhits = data.get("keyword_hits", [])
    conn   = data.get("connected_files", [])

    if not sem and not kwhits and not conn:
        return f"No results for: '{query}'. Knowledge base may be empty or try rephrasing."

    lines = [f"SEARCH: '{query}'"]

    # Section 1: Semantic results
    lines.append(f"\nSEMANTIC ({len(sem)} results)")
    for i, r in enumerate(sem, 1):
        stype  = r.get("source_type", "?").upper()
        fname  = r.get("source_file", "?")
        conf   = r.get("confidence", 0)
        topics = r.get("topics", [])
        ctx    = r.get("upload_context", "")
        cidx   = r.get("chunk_index", -1)
        chunk_label = f" chunk {cidx}" if cidx >= 0 else ""

        lines.append(f"\n[{i}] {fname} [{stype}]{chunk_label}")
        if topics:
            lines.append(f"  topics: {', '.join(topics)}")
        if ctx:
            lines.append(f"  label: {ctx}")

        # Image link
        if stype == "IMAGE":
            enc = urllib.parse.quote(fname)
            lines.append(f"  📸 [View \"{fname}\"](http://127.0.0.1:8000/node/{enc})")

        # Video: just show filename so get_video_clip can be called; skip full transcript here
        if stype == "VIDEO":
            enc = urllib.parse.quote(fname)
            ts_start = r.get("timestamp_start")
            ts_end   = r.get("timestamp_end")
            if ts_start is not None:
                lines.append(f"  video chunk: {int(ts_start)}s–{int(ts_end)}s  → call get_video_clip(file='{fname}', topic=<what user wants)")
            else:
                lines.append(f"  video → call get_video_clip(file='{fname}', topic=<what user wants>)")

        content = r.get("content", "").strip()
        if content:
            lines.append(content)

    # Section 2: Full file content
    if fulls:
        lines.append(f"\nFULL CONTENT ({len(fulls)} file(s))")
        for f in fulls:
            fname  = f.get("source_file", "?")
            stype  = f.get("source_type", "?").upper()
            nchunk = f.get("chunk_count", 1)
            conf   = f.get("confidence", 0)
            ctx    = f.get("upload_context", "")
            lines.append(f"\nFILE: {fname} [{stype}] {nchunk} chunk(s)")
            if ctx:
                lines.append(f"  label: {ctx}")
            lines.append(f.get("full_content", ""))

    # Section 3: Keyword hits
    if kwhits:
        lines.append(f"\nKEYWORD HITS ({len(kwhits)} file(s))")
        for kw in kwhits:
            fname   = kw.get("source_file", "?")
            stype   = kw.get("source_type", "?").upper()
            matched = ", ".join(kw.get("matched_keywords", []))
            conf    = kw.get("confidence", 0)
            snippet = kw.get("snippet", "").strip()
            lines.append(f"\n{fname} [{stype}] keywords: {matched}")
            if stype == "IMAGE":
                enc = urllib.parse.quote(fname)
                lines.append(f"  📸 [View \"{fname}\"](http://127.0.0.1:8000/node/{enc})")
            elif stype == "VIDEO":
                enc = urllib.parse.quote(fname)
                lines.append(f"  → call get_video_clip(file='{fname}', topic=<what user wants>)")
            if snippet:
                lines.append(snippet)

    # Section 4: Topic-connected files
    if conn:
        lines.append(f"\nTOPIC CONNECTIONS ({len(conn)} file(s))")
        for c in conn:
            fname   = c.get("source_file", "?")
            stype   = c.get("source_type", "?").upper()
            shared  = ", ".join(c.get("shared_topics", []))
            ctx     = c.get("upload_context", "")
            preview = c.get("content_preview", "").strip()
            lines.append(f"\n{fname} [{stype}] topics: {shared}")
            if ctx:
                lines.append(f"  label: {ctx}")
            if stype == "IMAGE":
                enc = urllib.parse.quote(fname)
                lines.append(f"  📸 [View \"{fname}\"](http://127.0.0.1:8000/node/{enc})")
            if preview:
                lines.append(f"  {preview}")

    # Sources block
    seen_files  = {}
    video_clip_urls = {}
    kwhit_files = {r.get("source_file", "") for r in kwhits}

    for lst in (sem, fulls, kwhits, conn):
        for r in lst:
            fn   = r.get("source_file", "?")
            conf = r.get("confidence", 0)
            if conf == 0 and fn in kwhit_files:
                conf = 85
            if conf == 0:
                conf = 50
            stype = r.get("source_type", "text").upper()
            if fn not in seen_files or conf > seen_files[fn]["conf"]:
                seen_files[fn] = {"conf": conf, "type": stype}
            if stype == "VIDEO" and fn not in video_clip_urls:
                ts_s = r.get("timestamp_start")
                ts_e = r.get("timestamp_end")
                enc  = urllib.parse.quote(fn)
                if ts_s is not None and ts_e is not None:
                    video_clip_urls[fn] = f"http://127.0.0.1:8000/clip?file={enc}&start={int(ts_s)}&end={int(ts_e)}"
                else:
                    video_clip_urls[fn] = f"http://127.0.0.1:8000/clip?file={enc}&start=0&end=30"

    lines.extend(_build_sources_block(seen_files, video_clip_urls))
    return "\n".join(lines)


@mcp.tool()
def get_video_clip(file: str, topic: str) -> str:
    """Get a trimmed clip URL for a specific topic from a video. Always use this instead of parsing timestamps."""
    data = _post("/api/mcp/find_clip", {"file": file, "topic": topic})

    if "error" in data:
        return f"Could not generate clip: {data['error']}"

    clip_url = data.get("clip_url", "")
    start    = data.get("start", 0)
    end      = data.get("end", 0)
    duration = data.get("duration_seconds", end - start)
    matched  = data.get("matched_lines", [])

    lines = [
        f"[Watch clip]({clip_url})",
        f"Clip: {start}s–{end}s ({duration}s)",
    ]
    if matched:
        lines.append("Matched lines:")
        for m in matched[:4]:
            lines.append(f"  {m}")
    return "\n".join(lines)


@mcp.tool()
def search_brain(query: str, top_k: int = 15) -> str:
    """FALLBACK ONLY if holistic_search returned an error. Semantic-only search."""
    top_k = max(1, min(top_k, 20))
    data = _post("/api/mcp/search", {"query": query, "top_k": top_k})
    if "error" in data:
        return f"Error: {data['error']}"
    all_results = data.get("results", [])
    if not all_results:
        return f"No results for: '{query}'"

    all_results.sort(key=lambda x: -x.get("confidence", 0))
    lines = [f"Search: '{query}' ({len(all_results)} results)"]
    for i, r in enumerate(all_results, 1):
        stype   = r.get("source_type", "?").upper()
        fname   = r.get("source_file", "?")
        conf    = r.get("confidence", 0)
        content = r.get("content", "").strip()
        ctx     = r.get("upload_context", "")
        cidx    = r.get("chunk_index", -1)
        label   = f" chunk {cidx}" if cidx >= 0 else ""

        lines.append(f"\n[{i}] {fname} [{stype}]{label} {conf}%")
        if ctx:
            lines.append(f"  label: {ctx}")
        if stype == "VIDEO":
            enc = urllib.parse.quote(fname)
            lines.append(f"  → call get_video_clip(file='{fname}', topic=<what user wants>)")
        if stype == "IMAGE":
            enc = urllib.parse.quote(fname)
            lines.append(f"  📸 [View](http://127.0.0.1:8000/node/{enc})")
        if content:
            lines.append(content)

    seen_files = {}
    video_clip_urls = {}
    for r in all_results:
        fn    = r.get("source_file", "?")
        conf  = r.get("confidence", 0) or 50
        stype = r.get("source_type", "text").upper()
        if fn not in seen_files or conf > seen_files[fn]["conf"]:
            seen_files[fn] = {"conf": conf, "type": stype}
        if stype == "VIDEO" and fn not in video_clip_urls:
            enc = urllib.parse.quote(fn)
            video_clip_urls[fn] = f"http://127.0.0.1:8000/clip?file={enc}&start=0&end=30"

    lines.extend(_build_sources_block(seen_files, video_clip_urls))
    return "\n".join(lines)


@mcp.tool()
def keyword_search(keyword: str) -> str:
    """FALLBACK ONLY if holistic_search returned an error. Exact keyword search."""
    data = _post("/api/mcp/keyword_search", {"keyword": keyword, "max_results": 25})
    if "error" in data:
        return f"Error: {data['error']}"

    results = data.get("results", [])
    total   = data.get("total_files_matched", 0)
    if not results:
        return f"No content containing '{keyword}' found."

    lines = [f"Keyword '{keyword}' — {total} file(s)"]
    for i, r in enumerate(results, 1):
        stype   = r.get("source_type", "?").upper()
        fname   = r.get("source_file", "?")
        snippet = r.get("snippet", "").strip()
        occ     = r.get("occurrences", 1)
        lines.append(f"\n[{i}] {fname} [{stype}] ({occ}x)")
        if snippet:
            lines.append(snippet)

    seen: dict = {}
    for r in results:
        fn    = r.get("source_file", "?")
        stype = r.get("source_type", "text").upper()
        if fn not in seen:
            seen[fn] = {"conf": 85, "type": stype}
    lines.extend(_build_sources_block(seen, {}))
    return "\n".join(lines)


@mcp.tool()
def list_knowledge() -> str:
    """List every file in the knowledge base. Use only when user explicitly asks what files they have."""
    data = _get("/api/mcp/files")
    if "error" in data:
        return f"Error: {data['error']}"

    files = data.get("files", [])
    if not files:
        return "Knowledge base is empty. Upload files at http://127.0.0.1:8000"

    by_type: dict = {}
    for f in files:
        t = f.get("type", "unknown")
        by_type.setdefault(t, []).append(f)

    lines = [f"Knowledge base: {len(files)} file(s)"]
    for ftype in sorted(by_type.keys()):
        flist = by_type[ftype]
        lines.append(f"\n[{ftype.upper()}] ({len(flist)})")
        for f in flist:
            topics = f.get("topics", [])
            lines.append(f"  {f['name']}" + (f" — {', '.join(topics)}" if topics else ""))
    return "\n".join(lines)


@mcp.tool()
def get_file_content(filename: str) -> str:
    """Get full content of a specific named file. Use only when holistic_search didn't return enough of it."""
    encoded = urllib.parse.quote(filename, safe="")
    data = _get(f"/api/mcp/file/{encoded}")
    if "error" in data:
        return f"Error: {data['error']}"

    name        = data.get("name", filename)
    ftype       = data.get("source_type", "?").upper()
    content     = data.get("content", "").strip()
    topics      = data.get("topics", [])
    ctx         = data.get("upload_context", "")
    chunk_count = data.get("chunk_count", 1)

    lines = [f"FILE: {name} [{ftype}] {chunk_count} chunk(s)"]
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
        clip_url = f"http://127.0.0.1:8000/clip?file={enc}&start={int(ts_start) if ts_start else 0}&end={int(ts_end) if ts_end else 30}"
        lines.append(f"  → get_video_clip or direct: [Watch]({clip_url})")

    enc_fn = urllib.parse.quote(filename, safe="")
    suffix = f"  [View](http://127.0.0.1:8000/node/{enc_fn})" if ftype == "IMAGE" else ""
    lines.extend(["\n---", "**Sources**", f"• {filename} [{ftype}]{suffix}", "---"])
    return "\n".join(lines)


@mcp.tool()
def get_connections(filename: str) -> str:
    """Show how a specific file connects to the rest of the knowledge base via topics and similarity."""
    encoded = urllib.parse.quote(filename, safe="")
    data = _get(f"/api/mcp/connections/{encoded}")
    if "error" in data:
        return f"Error: {data['error']}"

    name           = data.get("name", filename)
    topics         = data.get("topics", [])
    topic_peers    = data.get("topic_peers", {})
    semantic_peers = data.get("semantic_peers", [])

    lines = [f"CONNECTIONS: {name}"]
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

    return "\n".join(lines)


@mcp.tool()
def search_entities(query: str) -> str:
    """Search the entity graph for people, organisations, tools, and their relationships."""
    data = _post("/api/mcp/entity_search", {"query": query})
    if "error" in data:
        return f"Error: {data['error']}"

    entities = data.get("entities", [])
    rels     = data.get("relationships", [])
    total    = data.get("total_entities", 0)

    if not entities:
        return f"No entities matching '{query}' found."

    lines = [f"Entities: '{query}' ({len(entities)} of {total})"]
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

    return "\n".join(lines)


@mcp.tool()
def get_topics() -> str:
    """Browse the knowledge base by concept/theme topic rather than filename."""
    data = _get("/api/mcp/topics")
    if "error" in data:
        return f"Error: {data['error']}"

    topics = data.get("topics", {})
    if not topics:
        return "No topics found in the knowledge base."

    lines = [f"Topics ({len(topics)})"]
    for topic, files in sorted(topics.items()):
        lines.append(f"\n[{topic}] ({len(files)} file(s)): {', '.join(files)}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
