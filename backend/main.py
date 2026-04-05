import os
import re
import glob
import uuid
import subprocess
import concurrent.futures
import lancedb
import numpy as np
import time
import json
import hashlib
import base64
import secrets
import urllib.parse
import threading
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Form
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
import shutil
import requests
import keyring
from ingest import process_file, extract_entities, LLM_MODEL as INGEST_LLM_MODEL
from db import get_table, DB_PATH
from google import genai
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def _entity_backfill():
    """
    Startup background task: entity-tag any files in the DB that were ingested
    before entity extraction was added. Runs once at startup, skips files that
    already have entity data.
    """
    time.sleep(8)  # Let the server fully initialise first
    if not GEMINI_API_KEY:
        return
    print("Entity backfill: scanning for files without entity data...")
    try:
        tbl = get_table()
        all_rows = tbl.search().limit(50000).to_list()

        # Group rows by source file
        file_rows: dict = {}
        for row in all_rows:
            sf = row.get("source_file", "")
            if sf:
                file_rows.setdefault(sf, []).append(row)

        # Only process files that have no entity data on ANY chunk
        to_process = []
        for sf, rows in file_rows.items():
            has_entities = False
            for r in rows:
                rm = json.loads(r["metadata"]) if isinstance(r.get("metadata"), str) else (r.get("metadata") or {})
                if rm.get("entities", {}).get("entities"):
                    has_entities = True
                    break
            if not has_entities:
                to_process.append((sf, rows))

        if not to_process:
            print("Entity backfill: all files already have entity data — nothing to do.")
            return

        print(f"Entity backfill: enriching {len(to_process)} file(s)...")
        updated_any = False
        for sf, rows in to_process:
            try:
                sorted_rows = sorted(rows, key=lambda r: r.get("chunk_index") or 0)
                full_text = "\n\n".join(r.get("content", "") for r in sorted_rows)
                entities = extract_entities(full_text)

                if entities.get("entities"):
                    for row in rows:
                        tbl.delete(f"id = '{row['id']}'")
                    new_rows = []
                    for row in rows:
                        rm = json.loads(row["metadata"]) if isinstance(row.get("metadata"), str) else (row.get("metadata") or {})
                        rm["entities"] = entities
                        new_rows.append({
                            "id": row["id"],
                            "content": row["content"],
                            "vector": row["vector"],
                            "source_type": row["source_type"],
                            "source_file": row["source_file"],
                            "chunk_index": row.get("chunk_index", 0),
                            "metadata": json.dumps(rm),
                        })
                    tbl.add(new_rows)
                    updated_any = True
                    print(f"Entity backfill: {sf} → {len(entities.get('entities', []))} entities")
                else:
                    print(f"Entity backfill: {sf} → no entities found (content may be too sparse)")

                time.sleep(1.5)  # Gentle rate-limit buffer between Gemini calls
            except Exception as e:
                print(f"Entity backfill: failed for {sf}: {e}")

        if updated_any:
            _invalidate_retrieval_caches("entity_backfill")
        print("Entity backfill: complete.")
    except Exception as e:
        print(f"Entity backfill error: {e}")


def _transcript_backfill():
    """
    Startup background task: generate timestamped transcripts for video chunks that
    don't have one yet. Uploads the original source file directly — no ffmpeg needed.
    Transcripts use absolute timestamps (seconds from start of full video).
    """
    time.sleep(20)
    if not GEMINI_API_KEY:
        print("Transcript backfill: no Gemini API key — skipping", flush=True)
        return
    print("Transcript backfill: scanning for video chunks without transcripts...", flush=True)
    try:
        tbl = get_table()
        all_rows = tbl.search().limit(50000).to_list()

        to_transcribe = []
        for row in all_rows:
            if row.get("source_type") != "video":
                continue
            meta = json.loads(row["metadata"]) if isinstance(row.get("metadata"), str) else (row.get("metadata") or {})
            if meta.get("transcript"):
                continue
            to_transcribe.append(row)

        if not to_transcribe:
            print("Transcript backfill: all video chunks already have transcripts — nothing to do.", flush=True)
            return

        print(f"Transcript backfill: generating transcripts for {len(to_transcribe)} video chunk(s)...", flush=True)
        brain_dir = os.path.join(REPO_ROOT, "brain_data")

        # Group by source file so we upload each video only once
        by_file: dict = {}
        for row in to_transcribe:
            sf = row.get("source_file", "")
            by_file.setdefault(sf, []).append(row)

        updated_any = False
        for source_file, rows in by_file.items():
            source_path = os.path.normpath(os.path.join(brain_dir, source_file))
            if not os.path.isfile(source_path):
                print(f"Transcript backfill: source file not found for {source_file} — skipping", flush=True)
                continue
            try:
                # Upload the full source video once and get a transcript with absolute timestamps
                video_file = gemini.files.upload(file=source_path)
                for _ in range(30):
                    video_file = gemini.files.get(name=video_file.name)
                    state = getattr(getattr(video_file, "state", None), "name", None)
                    if state == "ACTIVE":
                        break
                    elif state in ("FAILED", None):
                        raise Exception(f"Gemini file state: {state}")
                    time.sleep(2)

                transcript = ""
                for attempt in range(3):
                    try:
                        resp = gemini.models.generate_content(
                            model=INGEST_LLM_MODEL,
                            contents=[
                                video_file,
                                "Generate a precise timestamped transcript of all spoken content in this video.\n"
                                "Timestamps must be ABSOLUTE (from the very start of the video, 00:00 = video start).\n"
                                "Format each line as: [MM:SS] Name (if known): exact words spoken\n"
                                "Be precise — timestamps will be used to cut exact video clips.\n"
                                "If no speech: [00:00] No spoken content."
                            ]
                        )
                        transcript = resp.text.strip()
                        break
                    except Exception as retry_err:
                        err_str = str(retry_err)
                        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                            if attempt < 2:
                                print(f"Transcript backfill: quota hit, retrying in 40s (attempt {attempt+1}/3)...", flush=True)
                                time.sleep(40)
                            else:
                                raise
                        else:
                            raise
                try:
                    gemini.files.delete(name=video_file.name)
                except Exception:
                    pass

                if not transcript:
                    print(f"Transcript backfill: no transcript returned for {source_file}", flush=True)
                    continue

                # Write transcript to every chunk of this file that needs it
                # Mark as absolute so clip URLs don't need to add chunk offset
                for row in rows:
                    row_meta = json.loads(row["metadata"]) if isinstance(row.get("metadata"), str) else (row.get("metadata") or {})
                    row_meta["transcript"] = transcript
                    row_meta["transcript_absolute"] = True
                    tbl.delete(f"id = '{row['id']}'")
                    tbl.add([{
                        "id": row["id"],
                        "content": row["content"],
                        "vector": row["vector"],
                        "source_type": row["source_type"],
                        "source_file": row["source_file"],
                        "chunk_index": row.get("chunk_index", 0),
                        "metadata": json.dumps(row_meta),
                    }])
                    updated_any = True
                    print(f"Transcript backfill: {source_file} chunk {row.get('chunk_index', 0)} — done", flush=True)

                time.sleep(2)
            except Exception as e:
                print(f"Transcript backfill: failed for {source_file}: {e}", flush=True)

        if updated_any:
            _invalidate_retrieval_caches("transcript_backfill")
        print("Transcript backfill: complete.", flush=True)
    except Exception as e:
        print(f"Transcript backfill error: {e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_entity_backfill, daemon=True).start()
    threading.Thread(target=_transcript_backfill, daemon=True).start()
    threading.Thread(target=_migrate_legacy_brain_dumps, daemon=True).start()
    yield


app = FastAPI(title="My Second Brain API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the brain_data directory to serve images/files
os.makedirs(os.path.join(os.path.dirname(os.path.dirname(__file__)), "brain_data"), exist_ok=True)
app.mount("/brain_data", StaticFiles(directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "brain_data")), name="brain_data")
FRONTEND_DIST_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
FRONTEND_ASSETS_DIR = os.path.join(FRONTEND_DIST_DIR, "assets")
if os.path.isdir(FRONTEND_ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=FRONTEND_ASSETS_DIR), name="assets")
REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
CLIPS_DIR = os.path.join(REPO_ROOT, "brain_data", "clips")
os.makedirs(CLIPS_DIR, exist_ok=True)
PUBLIC_BASE_URL_OVERRIDE = os.getenv("MSB_PUBLIC_BASE_URL", "").strip().rstrip("/")


def _clip_relative_url(filename: str, start: int = 0, end: int = 30) -> str:
    enc = urllib.parse.quote(filename, safe="")
    return f"/clip?file={enc}&start={int(start)}&end={int(end)}"


def _safe_brain_file_path(filename: str) -> str:
    brain_dir = os.path.join(REPO_ROOT, "brain_data")
    # Check brain_data root first
    source_path = os.path.normpath(os.path.join(brain_dir, filename))
    if not source_path.startswith(os.path.normpath(brain_dir) + os.sep):
        return ""
    if os.path.isfile(source_path):
        return source_path
    # Also check brain_data/notes/ subdirectory (for note_*.md files)
    notes_path = os.path.normpath(os.path.join(brain_dir, "notes", filename))
    if notes_path.startswith(os.path.normpath(brain_dir) + os.sep) and os.path.isfile(notes_path):
        return notes_path
    return source_path


@app.get("/api/video-clip")
def serve_video_clip(file: str, start: float = 0, end: float = 30):
    """
    Trim a video to the requested segment and return it as video/mp4.
    Clips are cached in brain_data/clips/ — repeated requests for the same
    segment are instant. If ffmpeg is not installed, the full source video
    is served instead (browser can seek to the timestamp manually).
    """
    brain_dir = os.path.join(REPO_ROOT, "brain_data")
    # Security: resolve and confirm path stays within brain_data/
    source_path = os.path.normpath(os.path.join(brain_dir, file))
    if not source_path.startswith(os.path.normpath(brain_dir) + os.sep):
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not os.path.isfile(source_path):
        raise HTTPException(status_code=404, detail=f"Video not found: {file}")

    # Deterministic clip name so identical requests reuse the cached file
    safe_stem = re.sub(r"[^\w]", "_", os.path.splitext(file)[0])[:40]
    clip_name = f"{safe_stem}_{int(start)}_{int(end)}.mp4"
    clip_path = os.path.join(CLIPS_DIR, clip_name)

    if not os.path.isfile(clip_path):
        if not shutil.which("ffmpeg"):
            # No ffmpeg — serve the full source file; user can seek in the browser
            return FileResponse(source_path, media_type="video/mp4",
                                filename=os.path.basename(file))
        try:
            result = subprocess.run(
                ["ffmpeg", "-y",
                 "-ss", str(start),
                 "-i", source_path,
                 "-t", str(end - start),
                 "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                 "-c:a", "aac",
                 "-avoid_negative_ts", "make_zero",
                 "-map_metadata", "-1",
                 "-movflags", "+faststart",
                 clip_path],
                capture_output=True, timeout=120
            )
            if result.returncode != 0 or not os.path.isfile(clip_path):
                raise HTTPException(status_code=500,
                                    detail=f"ffmpeg failed: {result.stderr.decode()[:200]}")
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Clip trimming timed out")

    return FileResponse(clip_path, media_type="video/mp4",
                        headers={"Content-Disposition": "inline"})


@app.get("/clip")
def clip_preview_page(file: str, start: float = 0, end: float = 30):
    """
    Serves a minimal HTML page with an embedded video player for the requested clip.
    This is the URL Claude should link to — it opens in-browser as a proper video preview.
    If the source video no longer exists, returns a clean not-found page instead.
    """
    brain_dir   = os.path.join(REPO_ROOT, "brain_data")
    source_path = os.path.normpath(os.path.join(brain_dir, file))

    # Security: confirm path stays within brain_data/
    if not source_path.startswith(os.path.normpath(brain_dir) + os.sep):
        return HTMLResponse(_clip_not_found_html(file), status_code=404)

    # File has been deleted — show not-found page
    if not os.path.isfile(source_path):
        return HTMLResponse(_clip_not_found_html(file), status_code=404)

    enc = urllib.parse.quote(file, safe="")
    label = f"{file}  ·  {int(start)}s – {int(end)}s"
    video_src = f"/api/video-clip?file={enc}&start={start}&end={end}"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{label}</title>
  <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    html,body{{height:100%}}
    body{{background:#0d0d0d;color:#eee;font-family:system-ui,sans-serif;
         display:flex;flex-direction:column;align-items:center;justify-content:center;
         padding:20px;gap:12px;overflow:hidden}}
    h1{{font-size:.85rem;color:#888;text-align:center;max-width:680px;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;width:100%}}
    .video-wrap{{width:100%;max-width:680px;max-height:calc(100vh - 90px);
                 display:flex;align-items:center;justify-content:center}}
    video{{width:100%;max-height:calc(100vh - 90px);border-radius:8px;
           box-shadow:0 6px 32px rgba(0,0,0,.8);object-fit:contain}}
    a{{color:#6c8ebf;font-size:.75rem;text-decoration:none;opacity:.7}}
    a:hover{{opacity:1}}
  </style>
</head>
<body>
  <h1>{label}</h1>
  <div class="video-wrap">
    <video src="{video_src}" controls autoplay playsinline preload="auto"></video>
  </div>
  <a href="/">&#8592; Back to My Second Brain</a>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/node/{filename:path}")
def node_preview_page(filename: str):
        """
        Lightweight preview route used by MCP [View] links.
        - Images: render in an HTML preview page.
        - Videos: redirect to /clip preview.
        - Other files: redirect to raw /brain_data path.
        """
        source_path = _safe_brain_file_path(filename)
        if not source_path or not os.path.isfile(source_path):
                return HTMLResponse(_node_not_found_html(filename), status_code=404)

        ext = os.path.splitext(source_path)[1].lower()
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                enc = urllib.parse.quote(filename, safe="")
                safe_label = filename.replace("<", "&lt;").replace(">", "&gt;")
                html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{safe_label}</title>
    <style>
        *{{margin:0;padding:0;box-sizing:border-box}}
        html,body{{height:100%}}
        body{{background:#0d0d0d;color:#eee;font-family:system-ui,sans-serif;
                 display:flex;flex-direction:column;align-items:center;justify-content:center;
                 padding:20px;gap:14px;overflow:hidden}}
        h1{{font-size:.85rem;color:#8f8f8f;text-align:center;max-width:800px;
                white-space:nowrap;overflow:hidden;text-overflow:ellipsis;width:100%}}
        .image-wrap{{width:100%;max-width:980px;max-height:calc(100vh - 100px);
                                 display:flex;align-items:center;justify-content:center}}
        img{{max-width:100%;max-height:calc(100vh - 120px);border-radius:10px;
                 box-shadow:0 8px 34px rgba(0,0,0,.85);object-fit:contain}}
        a{{color:#6c8ebf;font-size:.78rem;text-decoration:none;opacity:.78}}
        a:hover{{opacity:1}}
    </style>
</head>
<body>
    <h1>{safe_label}</h1>
    <div class="image-wrap">
        <img src="/brain_data/{enc}" alt="{safe_label}" />
    </div>
    <a href="/">&#8592; Back to My Second Brain</a>
</body>
</html>"""
                return HTMLResponse(html)

        if ext in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
                return RedirectResponse(_clip_relative_url(filename, 0, 30), status_code=307)

        enc = urllib.parse.quote(filename, safe="")
        return RedirectResponse(f"/brain_data/{enc}", status_code=307)


def _clip_not_found_html(filename: str) -> str:
    safe_name = filename.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>File not found</title>
  <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    html,body{{height:100%;background:#0d0d0d;color:#eee;font-family:system-ui,sans-serif;
              display:flex;align-items:center;justify-content:center}}
    .card{{display:flex;flex-direction:column;align-items:center;gap:16px;
           padding:40px 48px;background:#161616;border:1px solid rgba(255,255,255,.08);
           border-radius:20px;max-width:420px;width:90vw;text-align:center}}
    .icon{{width:52px;height:52px;border-radius:14px;background:rgba(239,68,68,.1);
           display:flex;align-items:center;justify-content:center;margin-bottom:4px}}
    .icon svg{{stroke:#f87171;stroke-width:1.8;fill:none;width:26px;height:26px}}
    h2{{font-size:1.05rem;font-weight:600;color:#fff;margin:0}}
    p{{font-size:.85rem;color:#9ca3af;line-height:1.5;margin:0}}
    .filename{{color:#e5e7eb;font-weight:500;word-break:break-all}}
    a.btn{{display:inline-block;margin-top:4px;padding:9px 24px;border-radius:12px;
           background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);
           color:#e5e7eb;font-size:.82rem;font-weight:500;text-decoration:none;
           transition:background .2s}}
    a.btn:hover{{background:rgba(255,255,255,.13)}}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">
      <svg viewBox="0 0 24 24"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/><line x1="9" y1="17" x2="15" y2="17"/><line x1="12" y1="14" x2="12" y2="20"/></svg>
    </div>
    <h2>File no longer exists</h2>
    <p><span class="filename">{safe_name}</span> has been removed from your knowledge base.</p>
    <a class="btn" href="/">&#8592; Back to Knowledge Base</a>
  </div>
</body>
</html>"""


def _node_not_found_html(filename: str) -> str:
        safe_name = filename.replace("<", "&lt;").replace(">", "&gt;")
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Node not found</title>
    <style>
        *{{margin:0;padding:0;box-sizing:border-box}}
        html,body{{height:100%;background:#0d0d0d;color:#eee;font-family:system-ui,sans-serif;
                            display:flex;align-items:center;justify-content:center}}
        .card{{display:flex;flex-direction:column;align-items:center;gap:16px;
                     padding:26px 30px;border:1px solid #2f2f2f;border-radius:14px;background:#161616;max-width:620px}}
        h1{{font-size:1.05rem;font-weight:650;color:#f2f2f2}}
        p{{font-size:.92rem;color:#ababab;text-align:center;line-height:1.45}}
        code{{background:#1f1f1f;padding:2px 6px;border-radius:6px;color:#d8d8d8;font-family:ui-monospace,monospace}}
        a{{color:#8db4ff;text-decoration:none;font-size:.88rem}}
    </style>
</head>
<body>
    <div class="card">
        <h1>Node file not found</h1>
        <p>The requested file is unavailable:<br><code>{safe_name}</code></p>
        <a href="/">&#8592; Back to My Second Brain</a>
    </div>
</body>
</html>"""


class ChatRequest(BaseModel):
    query: str
    top_k: int = 5
    provider: str = "gemini"

class MCPSearchRequest(BaseModel):
    query: str
    top_k: int = 15

class MCPKeywordSearchRequest(BaseModel):
    keyword: str
    max_results: int = 25

class MCPEntitySearchRequest(BaseModel):
    query: str
    entity_types: list[str] = []

class MCPHolisticRequest(BaseModel):
    query: str

class BrainDumpRequest(BaseModel):
    text: str
    context: str = ""


class NoteSaveRequest(BaseModel):
    note_id: str | None = None
    title: str
    content: str
    context: str = ""
    index_now: bool = False

from threading import Lock, Semaphore

UPLOAD_STATUS = {}
UPLOAD_STATUS_LOCK = Lock()
# Documents are prioritised and can process with slightly higher parallelism,
# while media remains capped to prevent Gemini free-tier retry storms.
DOCUMENT_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}
MAX_DOC_INFLIGHT = max(1, min(6, int(os.getenv("MAX_DOC_INFLIGHT", "4"))))
MAX_MEDIA_INFLIGHT = max(1, min(2, int(os.getenv("MAX_MEDIA_INFLIGHT", "1"))))
DOCUMENT_PROCESSING_SEMAPHORE = Semaphore(MAX_DOC_INFLIGHT)
MEDIA_PROCESSING_SEMAPHORE = Semaphore(MAX_MEDIA_INFLIGHT)
UPLOAD_DEDUPE_LOCK = Lock()
CONTENT_HASH_INDEX: dict[str, str] = {}
CONTENT_HASH_INDEX_READY = False
INFLIGHT_CONTENT_HASHES: dict[str, str] = {}
INFLIGHT_FILENAMES: set[str] = set()
OAUTH_PENDING = {}
OAUTH_PENDING_LOCK = Lock()
NOTES_INDEX_LOCK = Lock()
LEGACY_MIGRATION_LOCK = Lock()
LEGACY_MIGRATION_DONE = False
LEGACY_MIGRATION_RUNNING = False

CLAUDE_OAUTH_CLIENT_ID = os.getenv("CLAUDE_OAUTH_CLIENT_ID", "").strip()
CLAUDE_OAUTH_CLIENT_SECRET = os.getenv("CLAUDE_OAUTH_CLIENT_SECRET", "").strip()
CLAUDE_OAUTH_AUTH_URL = os.getenv("CLAUDE_OAUTH_AUTH_URL", "https://claude.ai/oauth/authorize").strip()
CLAUDE_OAUTH_TOKEN_URL = os.getenv("CLAUDE_OAUTH_TOKEN_URL", "https://api.anthropic.com/oauth/token").strip()
CLAUDE_OAUTH_REDIRECT_URI = os.getenv("CLAUDE_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/api/auth/claude/callback").strip()
CLAUDE_API_BASE = os.getenv("CLAUDE_API_BASE", "https://api.anthropic.com").rstrip("/")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CHAT_MODEL = "gemini-2.0-flash-lite"  # free-tier friendly; same family as ingest LLM
TOKEN_SERVICE_NAME = "my-second-brain"
TOKEN_USERNAME = "claude_oauth_tokens"

TOPIC_NOISE_SUBSTRINGS = (
    "i have identified",
    "as the video file could not be processed",
    "ai video analysis was unavailable",
    "use this node for file-level retrieval",
    "semantic context",
    "conceptual themes and entities",
    "based on the file metadata",
    "typical technical context",
    "system operations",
    "video file:",
)

TOPIC_STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "about",
    "video", "file", "files", "metadata", "context", "semantic", "system", "operations"
}

SIDECAR_CACHE_LOCK = Lock()
SIDECAR_CACHE_VERSION = 0
SIDECAR_CACHE_DATA = None
SIDECAR_CACHE_BUILT_AT = 0.0
SIDECAR_CACHE_BUILT_VERSION = -1

QUERY_EMBED_CACHE_LOCK = Lock()
QUERY_EMBED_CACHE: dict[str, dict] = {}
QUERY_EMBED_GATE_LOCK = Lock()
QUERY_EMBED_LAST_CALL_AT = 0.0
QUERY_EMBED_GLOBAL_COOLDOWN_UNTIL = 0.0

SIDECAR_SCAN_LIMIT = max(8000, min(200000, int(os.getenv("MCP_SIDECAR_SCAN_LIMIT", "100000"))))
SIDECAR_TTL_SECONDS = max(30, min(3600, int(os.getenv("MCP_SIDECAR_TTL_SECONDS", "420"))))
SIDECAR_MAX_TERMS_PER_FILE = max(60, min(800, int(os.getenv("MCP_SIDECAR_MAX_TERMS_PER_FILE", "240"))))
SIDECAR_FILE_BLOB_CHARS = max(1200, min(16000, int(os.getenv("MCP_SIDECAR_FILE_BLOB_CHARS", "5200"))))

QUERY_EMBED_CACHE_TTL_SECONDS = max(60, min(3600, int(os.getenv("MCP_QUERY_EMBED_CACHE_TTL", "900"))))
QUERY_EMBED_CACHE_SIZE = max(32, min(1024, int(os.getenv("MCP_QUERY_EMBED_CACHE_SIZE", "256"))))
QUERY_EMBED_RETRY_ATTEMPTS = max(1, min(6, int(os.getenv("MCP_QUERY_EMBED_RETRY_ATTEMPTS", "2"))))
QUERY_EMBED_TIMEOUT_SECONDS = max(3, min(60, int(os.getenv("MCP_QUERY_EMBED_TIMEOUT_SECONDS", "5"))))
QUERY_EMBED_TOTAL_TIMEOUT_SECONDS = max(8, min(180, int(os.getenv("MCP_QUERY_EMBED_TOTAL_TIMEOUT_SECONDS", "10"))))
QUERY_EMBED_BACKOFF_BASE_SECONDS = max(0.2, min(8.0, float(os.getenv("MCP_QUERY_EMBED_BACKOFF_BASE_SECONDS", "0.8"))))
QUERY_EMBED_BACKOFF_MAX_SECONDS = max(1.0, min(90.0, float(os.getenv("MCP_QUERY_EMBED_BACKOFF_MAX_SECONDS", "6"))))
QUERY_EMBED_MIN_INTERVAL_SECONDS = max(0.0, min(5.0, float(os.getenv("MCP_QUERY_EMBED_MIN_INTERVAL_SECONDS", "0.2"))))

HOLISTIC_CANDIDATE_FILES_LIMIT = max(30, min(1200, int(os.getenv("MCP_HOLISTIC_CANDIDATE_FILES", "260"))))
HOLISTIC_CANDIDATE_ROWS_LIMIT = max(500, min(40000, int(os.getenv("MCP_HOLISTIC_CANDIDATE_ROWS", "7000"))))
KEYWORD_CANDIDATE_FILES_LIMIT = max(6, min(300, int(os.getenv("MCP_KEYWORD_CANDIDATE_FILES", "90"))))

MSB_ROLLOUT_MODE = os.getenv("MSB_ROLLOUT_MODE", "balanced").strip().lower()
if MSB_ROLLOUT_MODE not in {"safe", "balanced", "aggressive"}:
    MSB_ROLLOUT_MODE = "balanced"

# Safe mode favors speed/cost, aggressive favors depth/recall.
ROLLOUT_CANDIDATE_FACTOR = {"safe": 0.82, "balanced": 1.0, "aggressive": 1.18}[MSB_ROLLOUT_MODE]
ROLLOUT_RESPONSE_DEPTH_FACTOR = {"safe": 0.9, "balanced": 1.0, "aggressive": 1.15}[MSB_ROLLOUT_MODE]

_SIDECAR_TOKEN_RE = re.compile(r"[a-z0-9$%][a-z0-9$%_./-]{1,30}")
_SIDE_CAR_STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "about", "are", "was", "were", "been",
    "have", "has", "had", "you", "your", "our", "their", "they", "them", "there", "here", "what", "when",
    "where", "which", "who", "will", "would", "could", "should", "does", "did", "done", "not", "yes",
    "also", "just", "than", "then", "over", "under", "into", "onto", "between", "across", "about", "all",
    "any", "each", "many", "more", "most", "some", "such", "very", "much", "both", "same", "other",
    "file", "files", "chunk", "chunks", "content", "transcript", "video", "image", "document", "metadata",
}


def _rollout_scale_int(value: int, minimum: int, maximum: int, factor: float | None = None) -> int:
    use_factor = factor if factor is not None else ROLLOUT_CANDIDATE_FACTOR
    return max(minimum, min(maximum, int(round(value * use_factor))))


def _invalidate_retrieval_caches(reason: str = ""):
    global SIDECAR_CACHE_VERSION, SIDECAR_CACHE_DATA, SIDECAR_CACHE_BUILT_AT, SIDECAR_CACHE_BUILT_VERSION
    with SIDECAR_CACHE_LOCK:
        SIDECAR_CACHE_VERSION += 1
        SIDECAR_CACHE_DATA = None
        SIDECAR_CACHE_BUILT_AT = 0.0
        SIDECAR_CACHE_BUILT_VERSION = -1


def _sidecar_terms(text: str) -> list[str]:
    if not text:
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _SIDECAR_TOKEN_RE.findall(text.lower()):
        token = raw.strip("._/-")
        if len(token) < 2:
            continue
        if token in _SIDE_CAR_STOP_WORDS:
            continue
        if token.isdigit() and len(token) < 3:
            continue
        if token not in seen:
            seen.add(token)
            tokens.append(token)
            if len(tokens) >= 320:
                break
    return tokens


def _build_sidecar_index() -> dict:
    tbl = get_table()
    rows = tbl.search().limit(SIDECAR_SCAN_LIMIT).to_list()

    rows_by_file: dict[str, list[dict]] = {}
    file_info: dict[str, dict] = {}
    file_terms: dict[str, set[str]] = {}
    file_blob_parts: dict[str, list[str]] = {}
    file_blob_chars: dict[str, int] = {}

    topic_to_files: dict[str, set[str]] = {}
    token_to_files: dict[str, set[str]] = {}
    entity_to_files: dict[str, set[str]] = {}

    entity_map: dict[str, dict] = {}
    relationship_seen: set[tuple[str, str, str]] = set()
    relationships: list[dict] = []

    for row in rows:
        fname = str(row.get("source_file", "") or "")
        if not fname:
            continue

        source_type = str(row.get("source_type", "unknown") or "unknown")
        content = str(row.get("content", "") or "")
        meta = _parse_row_meta(row)
        chunk_index = int(row.get("chunk_index", 0) or 0)
        upload_context = str((meta or {}).get("upload_context", "") or "")

        rows_by_file.setdefault(fname, []).append(
            {
                "id": row.get("id"),
                "source_file": fname,
                "source_type": source_type,
                "chunk_index": chunk_index,
                "content": content,
                "metadata": meta,
            }
        )

        if fname not in file_info:
            file_info[fname] = {
                "source_file": fname,
                "source_type": source_type,
                "display_name": _display_name_for_source(fname, meta),
                "upload_context": upload_context,
                "topics": set(),
            }

        if upload_context and not file_info[fname].get("upload_context"):
            file_info[fname]["upload_context"] = upload_context

        for norm_topic in _sanitize_topics((meta or {}).get("topics", [])):
            display_topic = _topic_display(norm_topic)
            file_info[fname]["topics"].add(display_topic)
            topic_to_files.setdefault(display_topic.lower(), set()).add(fname)

        terms = file_terms.setdefault(fname, set())
        if len(terms) < SIDECAR_MAX_TERMS_PER_FILE:
            term_text = f"{content[:1200]}\n{upload_context}"
            for token in _sidecar_terms(term_text):
                terms.add(token)
                if len(terms) >= SIDECAR_MAX_TERMS_PER_FILE:
                    break

        if fname not in file_blob_parts:
            file_blob_parts[fname] = []
            file_blob_chars[fname] = 0
        remaining = SIDECAR_FILE_BLOB_CHARS - file_blob_chars[fname]
        if remaining > 0:
            compact = " ".join(content.split())[:remaining]
            if compact:
                file_blob_parts[fname].append(compact)
                file_blob_chars[fname] += len(compact) + 1
        if upload_context and file_blob_chars[fname] < SIDECAR_FILE_BLOB_CHARS:
            remaining = SIDECAR_FILE_BLOB_CHARS - file_blob_chars[fname]
            compact_ctx = " ".join(upload_context.split())[:remaining]
            if compact_ctx:
                file_blob_parts[fname].append(compact_ctx)
                file_blob_chars[fname] += len(compact_ctx) + 1

        ents_data = (meta or {}).get("entities", {}) or {}
        for ent in ents_data.get("entities", []):
            name = str(ent.get("name", "") or "").strip()
            if not name:
                continue
            key = name.lower()
            entity_to_files.setdefault(key, set()).add(fname)
            if key not in entity_map:
                entity_map[key] = {
                    "name": name,
                    "type": ent.get("type", "concept"),
                    "description": ent.get("description", ""),
                    "files": set(),
                }
            entity_map[key]["files"].add(fname)

        for rel in ents_data.get("relationships", []):
            frm = str(rel.get("from", "") or "").strip()
            reln = str(rel.get("relationship", "") or "").strip()
            to = str(rel.get("to", "") or "").strip()
            if not frm or not reln or not to:
                continue
            rel_key = (frm.lower(), reln.lower(), to.lower())
            if rel_key in relationship_seen:
                continue
            relationship_seen.add(rel_key)
            relationships.append({"from": frm, "relationship": reln, "to": to, "file": fname})

    for fname, file_rows in rows_by_file.items():
        rows_by_file[fname] = sorted(file_rows, key=lambda r: r.get("chunk_index") or 0)

    for fname, terms in file_terms.items():
        for token in terms:
            token_to_files.setdefault(token, set()).add(fname)

    file_blob_lookup = {
        fname: " ".join(parts).lower()
        for fname, parts in file_blob_parts.items()
    }

    for fname, info in file_info.items():
        info["topics"] = sorted(list(info.get("topics", set())))[:16]

    topics_map: dict[str, set[str]] = {}
    for fname, info in file_info.items():
        for display_topic in info.get("topics", []):
            topics_map.setdefault(display_topic, set()).add(fname)

    entities = []
    for ent in entity_map.values():
        entities.append(
            {
                "name": ent["name"],
                "type": ent["type"],
                "description": ent["description"],
                "files": sorted(list(ent["files"])),
                "file_count": len(ent["files"]),
            }
        )
    entities.sort(key=lambda x: (-x["file_count"], x["name"]))

    return {
        "rows_by_file": rows_by_file,
        "file_info": file_info,
        "topic_to_files": topic_to_files,
        "token_to_files": token_to_files,
        "entity_to_files": entity_to_files,
        "file_blob_lookup": file_blob_lookup,
        "topics_map": {k: sorted(list(v)) for k, v in topics_map.items()},
        "entities": entities,
        "relationships": relationships,
        "rows_loaded": len(rows),
        "files_loaded": len(file_info),
        "scan_limit": SIDECAR_SCAN_LIMIT,
        "truncated": len(rows) >= SIDECAR_SCAN_LIMIT,
        "built_at": _utc_now_iso(),
    }


def _get_sidecar_index(force_rebuild: bool = False) -> dict:
    global SIDECAR_CACHE_DATA, SIDECAR_CACHE_BUILT_AT, SIDECAR_CACHE_BUILT_VERSION
    now = time.time()
    with SIDECAR_CACHE_LOCK:
        if (
            not force_rebuild
            and SIDECAR_CACHE_DATA is not None
            and SIDECAR_CACHE_BUILT_VERSION == SIDECAR_CACHE_VERSION
            and (now - SIDECAR_CACHE_BUILT_AT) <= SIDECAR_TTL_SECONDS
        ):
            return SIDECAR_CACHE_DATA

    rebuilt = _build_sidecar_index()
    with SIDECAR_CACHE_LOCK:
        SIDECAR_CACHE_DATA = rebuilt
        SIDECAR_CACHE_BUILT_AT = time.time()
        SIDECAR_CACHE_BUILT_VERSION = SIDECAR_CACHE_VERSION
        return SIDECAR_CACHE_DATA


def _is_embedding_rate_limited(error_msg: str) -> bool:
    lowered = (error_msg or "").lower()
    return any(token in lowered for token in ("429", "resource_exhausted", "rate limit", "quota", "too many requests"))


def _is_embedding_retryable(error_msg: str) -> bool:
    lowered = (error_msg or "").lower()
    if _is_embedding_rate_limited(error_msg):
        return True
    transient_tokens = (
        "timeout",
        "timed out",
        "deadline",
        "temporarily unavailable",
        "service unavailable",
        "internal error",
        "503",
    )
    return any(token in lowered for token in transient_tokens)


def _embedding_backoff_seconds(query_key: str, attempt_idx: int, rate_limited: bool) -> float:
    base = QUERY_EMBED_BACKOFF_BASE_SECONDS * (2.0 if rate_limited else 1.0)
    exp = base * (2 ** max(0, attempt_idx))
    seed = int(query_key[:8], 16) if query_key else 0
    jitter = 0.85 + ((seed % 31) / 100.0)
    return min(QUERY_EMBED_BACKOFF_MAX_SECONDS, max(0.2, exp * jitter))


def _get_query_embedding_cache_only(query: str) -> tuple[list[float], bool, str]:
    normalized_query = (query or "").strip()
    if not normalized_query:
        return [], False, "empty_query"

    query_key = hashlib.sha256(normalized_query.lower().encode("utf-8")).hexdigest()
    now = time.time()
    with QUERY_EMBED_CACHE_LOCK:
        entry = QUERY_EMBED_CACHE.get(query_key)
        if not entry:
            return [], False, "cache_miss"
        cached_vector = list(entry.get("vector", []) or [])
        if not cached_vector:
            return [], False, "cache_miss"
        age_seconds = now - float(entry.get("ts", 0.0))
        if age_seconds <= QUERY_EMBED_CACHE_TTL_SECONDS:
            return cached_vector, True, "cache_hit"
        return cached_vector, True, "stale_cache"


def _get_query_embedding_cached(query: str) -> tuple[list[float], bool, str]:
    global QUERY_EMBED_LAST_CALL_AT, QUERY_EMBED_GLOBAL_COOLDOWN_UNTIL

    normalized_query = (query or "").strip()
    query_key = hashlib.sha256(normalized_query.lower().encode("utf-8")).hexdigest()
    now = time.time()

    stale_vector: list[float] = []
    with QUERY_EMBED_CACHE_LOCK:
        entry = QUERY_EMBED_CACHE.get(query_key)
        if entry:
            stale_vector = list(entry.get("vector", []) or [])
            if stale_vector and (now - float(entry.get("ts", 0.0))) <= QUERY_EMBED_CACHE_TTL_SECONDS:
                return stale_vector, True, "cache_hit"

    if not normalized_query:
        return [], False, "empty_query"

    last_error = ""
    start_ts = time.time()

    for attempt in range(QUERY_EMBED_RETRY_ATTEMPTS):
        elapsed = time.time() - start_ts
        if elapsed >= QUERY_EMBED_TOTAL_TIMEOUT_SECONDS:
            last_error = "embedding_total_timeout"
            break

        with QUERY_EMBED_GATE_LOCK:
            wait_until = max(
                QUERY_EMBED_GLOBAL_COOLDOWN_UNTIL,
                QUERY_EMBED_LAST_CALL_AT + QUERY_EMBED_MIN_INTERVAL_SECONDS,
            )
        wait_s = wait_until - time.time()
        if wait_s > 0:
            time.sleep(min(wait_s, QUERY_EMBED_BACKOFF_MAX_SECONDS))

        per_attempt_timeout = max(2.0, float(QUERY_EMBED_TIMEOUT_SECONDS))
        try:
            def _embed_once():
                return gemini.models.embed_content(
                    model="gemini-embedding-2-preview",
                    contents=[normalized_query],
                    config=genai.types.EmbedContentConfig(
                        task_type="RETRIEVAL_QUERY",
                        output_dimensionality=1536,
                    ),
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(_embed_once)
                result = future.result(timeout=per_attempt_timeout)

            embeddings = getattr(result, "embeddings", None) or []
            vector = list(getattr(embeddings[0], "values", []) or []) if embeddings else []
            if not vector:
                raise Exception("Embedding returned an empty vector.")

            now = time.time()
            with QUERY_EMBED_GATE_LOCK:
                QUERY_EMBED_LAST_CALL_AT = now
                QUERY_EMBED_GLOBAL_COOLDOWN_UNTIL = max(QUERY_EMBED_GLOBAL_COOLDOWN_UNTIL, now)

            with QUERY_EMBED_CACHE_LOCK:
                QUERY_EMBED_CACHE[query_key] = {"vector": vector, "ts": now}
                if len(QUERY_EMBED_CACHE) > QUERY_EMBED_CACHE_SIZE:
                    overflow = len(QUERY_EMBED_CACHE) - QUERY_EMBED_CACHE_SIZE
                    for stale_key, _ in sorted(QUERY_EMBED_CACHE.items(), key=lambda kv: kv[1].get("ts", 0.0))[:overflow]:
                        QUERY_EMBED_CACHE.pop(stale_key, None)

            return vector, False, "fresh"

        except concurrent.futures.TimeoutError:
            last_error = f"embedding_timeout_after_{int(per_attempt_timeout)}s"
        except Exception as e:
            last_error = str(e)

        rate_limited = _is_embedding_rate_limited(last_error)
        with QUERY_EMBED_GATE_LOCK:
            now = time.time()
            QUERY_EMBED_LAST_CALL_AT = now
            if rate_limited:
                cooldown = _embedding_backoff_seconds(query_key, attempt, True)
                QUERY_EMBED_GLOBAL_COOLDOWN_UNTIL = max(QUERY_EMBED_GLOBAL_COOLDOWN_UNTIL, now + cooldown)

        if attempt >= (QUERY_EMBED_RETRY_ATTEMPTS - 1):
            break
        if not _is_embedding_retryable(last_error):
            break

        sleep_for = _embedding_backoff_seconds(query_key, attempt, rate_limited)
        time.sleep(sleep_for)

    if stale_vector:
        return stale_vector, True, "stale_cache"

    if last_error:
        return [], False, f"unavailable:{last_error[:180]}"
    return [], False, "unavailable"


def _candidate_files_from_sidecar(
    sidecar: dict,
    query: str,
    kw_terms: list[str],
    query_names: list[str],
    sem_results: list[dict],
    intent: dict,
) -> tuple[list[str], dict[str, set[str]]]:
    token_to_files = sidecar.get("token_to_files", {}) or {}
    topic_to_files = sidecar.get("topic_to_files", {}) or {}
    entity_to_files = sidecar.get("entity_to_files", {}) or {}
    file_blob_lookup = sidecar.get("file_blob_lookup", {}) or {}

    candidate_scores: dict[str, float] = {}
    candidate_signals: dict[str, set[str]] = {}

    def _boost(fname: str, score: float, signal: str):
        if not fname:
            return
        candidate_scores[fname] = candidate_scores.get(fname, 0.0) + score
        if signal:
            candidate_signals.setdefault(fname, set()).add(signal)

    sem_files: list[str] = []
    for row in sem_results:
        fname = row.get("source_file", "")
        if fname and fname not in sem_files:
            sem_files.append(fname)

    for idx, fname in enumerate(sem_files):
        _boost(fname, 2.0 - min(1.2, idx * 0.08), "semantic-candidate")

    keyword_hits: dict[str, int] = {}
    for kw in kw_terms[:32]:
        files = token_to_files.get(kw.lower())
        if not files:
            continue
        for fname in files:
            keyword_hits[fname] = keyword_hits.get(fname, 0) + 1
    for fname, hits in keyword_hits.items():
        _boost(fname, min(1.35, 0.25 + hits * 0.12), "sidecar-token")

    q_phrase = " ".join((query or "").lower().split())
    if q_phrase and len(q_phrase) >= 5:
        phrase_hits = 0
        for fname, blob in file_blob_lookup.items():
            if q_phrase in blob:
                _boost(fname, 0.72, "sidecar-phrase")
                phrase_hits += 1
                if phrase_hits >= 120:
                    break

    for name in query_names[:10]:
        files = entity_to_files.get(name.lower())
        if not files:
            continue
        for fname in files:
            _boost(fname, 1.1, "entity-match")

    top_topics: set[str] = set()
    for row in sem_results[:6]:
        for topic in row.get("topics", []):
            top_topics.add(str(topic).lower())

    for topic in top_topics:
        for fname in topic_to_files.get(topic, set()):
            _boost(fname, 0.45, "topic-overlap")

    for word in [w.strip("?.,!\"'()[]{}:;").lower() for w in query.split() if len(w.strip()) >= 4][:12]:
        files = token_to_files.get(word)
        if not files:
            continue
        for fname in files:
            _boost(fname, 0.15, "query-term")

    ranked = sorted(candidate_scores.items(), key=lambda kv: (-kv[1], kv[0]))
    dynamic_limit = HOLISTIC_CANDIDATE_FILES_LIMIT
    if intent.get("is_broad"):
        dynamic_limit = min(1200, max(dynamic_limit, 320))
    elif intent.get("is_factual"):
        dynamic_limit = min(800, max(140, dynamic_limit - 40))
    dynamic_limit = _rollout_scale_int(dynamic_limit, 30, 1200)

    selected = [fname for fname, _ in ranked[:dynamic_limit]]
    selected_set = set(selected)
    for fname in sem_files:
        if fname not in selected_set:
            selected.append(fname)
            selected_set.add(fname)

    if not selected:
        fallback_files = sorted(list((sidecar.get("file_info", {}) or {}).keys()))
        selected = fallback_files[: min(dynamic_limit, 120)]
        for fname in selected:
            candidate_signals.setdefault(fname, set()).add("fallback-fill")

    # Always include image and video files as candidates when query has visual intent,
    # so they are never excluded from rank fusion even if sidecar scoring missed them.
    _vis_words = {
        "photo", "photos", "picture", "pictures", "image", "images", "screenshot",
        "pic", "pics", "video", "videos", "clip", "clips", "recording", "film",
        "watch", "see", "show", "look", "view", "visual", "capture", "shot",
        "selfie", "portrait", "event", "party", "birthday", "gallery", "media",
    }
    _q_words = set(w.strip("?.,!\"'()[]").lower() for w in query.split())
    if _q_words & _vis_words:
        file_info = sidecar.get("file_info", {}) or {}
        selected_set = set(selected)
        for fname, finfo in file_info.items():
            if str(finfo.get("source_type", "")).lower() in ("image", "video"):
                if fname not in selected_set:
                    selected.append(fname)
                    selected_set.add(fname)
                    candidate_signals.setdefault(fname, set()).add("media-visual-intent")

    selected_signals = {fname: candidate_signals.get(fname, set()) for fname in selected}
    return selected, selected_signals

def _normalize_topic(topic: str) -> str:
    if not isinstance(topic, str):
        return ""
    t = topic.strip().lower()
    t = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in t)
    t = " ".join(t.split())
    return t

def _is_noisy_topic(topic: str) -> bool:
    t = topic.lower().strip()
    if not t:
        return True
    if any(noise in t for noise in TOPIC_NOISE_SUBSTRINGS):
        return True
    if len(t) > 56 or len(t.split()) > 6:
        return True
    if ":" in t or "\n" in t:
        return True
    return False

def _sanitize_topics(raw_topics) -> list[str]:
    if not raw_topics:
        return []
    cleaned = []
    for item in raw_topics:
        if not isinstance(item, str):
            continue
        parts = item.replace("\n", ",").split(",")
        for part in parts:
            candidate = part.strip(" -•\t\r\n\"'()[]{}")
            norm = _normalize_topic(candidate)
            if not norm or _is_noisy_topic(norm):
                continue
            if all(w in TOPIC_STOP_WORDS for w in norm.split()):
                continue
            cleaned.append(norm)
    deduped = list(dict.fromkeys(cleaned))
    return deduped[:12]

def _topic_display(norm_topic: str) -> str:
    words = norm_topic.split()
    return " ".join(words[:6]).title()

def _topic_node_id(norm_topic: str) -> str:
    return f"topic::{norm_topic}"


BRAIN_DUMP_PREFIX = "brain_dump_"
BRAIN_DUMP_NUMERIC_RE = re.compile(r"^brain_dump_(\d+)\.txt$", re.IGNORECASE)
NOTE_ID_RE = re.compile(r"^note_[a-f0-9]{10,32}$", re.IGNORECASE)
NOTE_FILENAME_RE = re.compile(r"^note_[a-f0-9]{10,32}\.md$", re.IGNORECASE)
NOTES_SUBDIR = "notes"
NOTES_INDEX_FILENAME = ".notes_index.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _brain_data_dir() -> str:
    path = os.path.join(REPO_ROOT, "brain_data")
    os.makedirs(path, exist_ok=True)
    return path


def _notes_dir() -> str:
    path = os.path.join(_brain_data_dir(), NOTES_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _notes_index_path() -> str:
    return os.path.join(_notes_dir(), NOTES_INDEX_FILENAME)


def _load_notes_index() -> dict:
    path = _notes_index_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        cleaned: dict[str, dict] = {}
        for note_id, value in data.items():
            if isinstance(note_id, str) and isinstance(value, dict):
                cleaned[note_id] = value
        return cleaned
    except Exception:
        return {}


def _save_notes_index(index_data: dict):
    path = _notes_index_path()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _normalize_note_title(title: str) -> str:
    return " ".join((title or "").strip().split())


def _note_title_key(title: str) -> str:
    return _normalize_note_title(title).lower()


def _find_note_title_conflict(index_data: dict, title: str, exclude_note_id: str | None = None) -> str | None:
    wanted = _note_title_key(title)
    if not wanted:
        return None
    for nid, rec in index_data.items():
        if nid == exclude_note_id:
            continue
        if _note_title_key(str(rec.get("title", ""))) == wanted:
            return nid
    return None


def _generate_note_id(index_data: dict) -> str:
    while True:
        candidate = f"note_{uuid.uuid4().hex[:12]}"
        if candidate not in index_data:
            return candidate


def _build_note_source_file(note_id: str) -> str:
    return f"{note_id}.md"


def _build_note_path(note_id: str) -> str:
    return os.path.join(_notes_dir(), _build_note_source_file(note_id))


def _validate_note_id(note_id: str):
    if not isinstance(note_id, str) or not NOTE_ID_RE.match(note_id.strip()):
        raise HTTPException(status_code=400, detail="Invalid note id.")


def _note_content_hash(content: str, context: str) -> str:
    payload = f"{content}\n\n<context>\n{context or ''}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _derive_note_title_from_text(content: str, fallback: str = "Untitled Brain Dump") -> str:
    for raw_line in (content or "").splitlines():
        line = re.sub(r"^#{1,6}\s*", "", raw_line or "").strip()
        if not line:
            continue
        line = " ".join(line.split())
        return line[:96].rstrip()
    return fallback


def _is_brain_dump_source_name(source_name: str) -> bool:
    return isinstance(source_name, str) and source_name.lower().startswith(BRAIN_DUMP_PREFIX)


def _parse_brain_dump_index(source_name: str) -> int | None:
    if not isinstance(source_name, str):
        return None
    m = BRAIN_DUMP_NUMERIC_RE.match(source_name.strip())
    if not m:
        return None
    try:
        idx = int(m.group(1))
        return idx if idx > 0 else None
    except Exception:
        return None


def _build_brain_dump_index_map(source_names: list[str]) -> dict[str, int]:
    # Keep labels unique across document names, including files literally named "1", "2", etc.
    numeric_doc_names = {
        int(name.strip())
        for name in source_names
        if isinstance(name, str) and name.strip().isdigit() and int(name.strip()) > 0
    }
    used_indices = set(numeric_doc_names)
    mapping: dict[str, int] = {}
    legacy_names: list[str] = []

    unique_names = sorted({n for n in source_names if _is_brain_dump_source_name(n)})
    for source_name in unique_names:
        idx = _parse_brain_dump_index(source_name)
        if idx is not None and idx not in used_indices:
            mapping[source_name] = idx
            used_indices.add(idx)
        else:
            legacy_names.append(source_name)

    next_idx = 1
    for source_name in legacy_names:
        while next_idx in used_indices:
            next_idx += 1
        mapping[source_name] = next_idx
        used_indices.add(next_idx)
        next_idx += 1

    return mapping


def _parse_row_meta(row: dict) -> dict:
    try:
        return json.loads(row["metadata"]) if isinstance(row.get("metadata"), str) else (row.get("metadata") or {})
    except Exception:
        return {}


def _display_name_for_source(source_name: str, meta: dict, legacy_map: dict[str, int] | None = None) -> str:
    custom_name = str((meta or {}).get("display_name", "") or "").strip()
    if custom_name:
        return custom_name
    if legacy_map and source_name in legacy_map:
        return str(legacy_map[source_name])
    return source_name


def _is_note_source(source_name: str, meta: dict) -> bool:
    if bool((meta or {}).get("is_brain_dump_note")):
        return True
    return bool(_is_brain_dump_source_name(source_name))


def _resolve_source_file_path(source_file: str) -> str:
    candidate_root = os.path.join(_brain_data_dir(), source_file)
    if os.path.isfile(candidate_root):
        return candidate_root
    candidate_note = os.path.join(_notes_dir(), source_file)
    if os.path.isfile(candidate_note):
        return candidate_note
    return candidate_root


def _delete_rows_for_source_file(source_file: str):
    tbl = get_table()
    rows = tbl.search().limit(50000).to_list()
    ids_to_delete = [r.get("id") for r in rows if r.get("source_file") == source_file and r.get("id")]
    for row_id in ids_to_delete:
        tbl.delete(f"id = '{row_id}'")
    if ids_to_delete:
        _invalidate_retrieval_caches("delete_rows_for_source_file")


def _apply_note_metadata_to_rows(source_file: str, note_id: str, title: str):
    tbl = get_table()
    rows = tbl.search().limit(50000).to_list()
    target_rows = [r for r in rows if r.get("source_file") == source_file and r.get("id")]
    if not target_rows:
        return

    updated = []
    for row in target_rows:
        meta = _parse_row_meta(row)
        meta["is_brain_dump_note"] = True
        meta["note_id"] = note_id
        meta["display_name"] = title
        updated.append(
            {
                "id": row["id"],
                "content": row["content"],
                "vector": row["vector"],
                "source_type": row["source_type"],
                "source_file": row["source_file"],
                "chunk_index": row.get("chunk_index", 0),
                "metadata": json.dumps(meta),
            }
        )

    for row in target_rows:
        tbl.delete(f"id = '{row['id']}'")
    tbl.add(updated)
    _invalidate_retrieval_caches("apply_note_metadata")


def _remove_note_record_by_source_file(source_file: str):
    with NOTES_INDEX_LOCK:
        index_data = _load_notes_index()
        removed = False
        for note_id, rec in list(index_data.items()):
            if str(rec.get("source_file", "")).strip() == source_file:
                index_data.pop(note_id, None)
                removed = True
        if removed:
            _save_notes_index(index_data)


def _rename_note_record_by_source_file(old_source_file: str, new_source_file: str):
    with NOTES_INDEX_LOCK:
        index_data = _load_notes_index()
        changed = False
        for note_id, rec in list(index_data.items()):
            if str(rec.get("source_file", "")).strip() == old_source_file:
                rec["source_file"] = new_source_file
                # Also update title if it matched the old filename
                if rec.get("title", "").strip() == old_source_file:
                    rec["title"] = new_source_file
                changed = True
        if changed:
            _save_notes_index(index_data)


def _migrate_legacy_brain_dumps():
    global LEGACY_MIGRATION_DONE, LEGACY_MIGRATION_RUNNING
    with LEGACY_MIGRATION_LOCK:
        if LEGACY_MIGRATION_DONE or LEGACY_MIGRATION_RUNNING:
            return
        LEGACY_MIGRATION_RUNNING = True

    try:
        legacy_paths = sorted(glob.glob(os.path.join(_brain_data_dir(), "brain_dump_*.txt")))
        if not legacy_paths:
            return

        print(f"Legacy brain dump migration: found {len(legacy_paths)} file(s)")
        for idx, legacy_path in enumerate(legacy_paths, start=1):
            legacy_name = os.path.basename(legacy_path)
            try:
                with open(legacy_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception as ex:
                print(f"Legacy migration: failed reading {legacy_name}: {ex}")
                continue

            with NOTES_INDEX_LOCK:
                index_data = _load_notes_index()
                note_id = _generate_note_id(index_data)
                fallback_title = f"Brain Dump {idx}"
                base_title = _derive_note_title_from_text(content, fallback=fallback_title)
                title = base_title
                suffix = 2
                while _find_note_title_conflict(index_data, title):
                    title = f"{base_title} ({suffix})"
                    suffix += 1

                source_file = _build_note_source_file(note_id)
                note_path = _build_note_path(note_id)
                with open(note_path, "w", encoding="utf-8") as f:
                    f.write(content)

                now = _utc_now_iso()
                content_hash = _note_content_hash(content, "")
                index_data[note_id] = {
                    "note_id": note_id,
                    "title": title,
                    "source_file": source_file,
                    "created_at": now,
                    "updated_at": now,
                    "context": "",
                    "content_hash": content_hash,
                    "indexed_hash": "",
                    "indexed_at": "",
                }
                _save_notes_index(index_data)

            reindex_success = False
            try:
                _delete_rows_for_source_file(legacy_name)
                process_file(note_path, upload_context="", content_hash=None)
                _apply_note_metadata_to_rows(source_file, note_id, title)
                reindex_success = True
            except Exception as ex:
                print(f"Legacy migration: reindex failed for {legacy_name}: {ex}")

            with NOTES_INDEX_LOCK:
                index_data = _load_notes_index()
                rec = index_data.get(note_id)
                if rec:
                    if reindex_success:
                        rec["indexed_hash"] = rec.get("content_hash", "")
                        rec["indexed_at"] = _utc_now_iso()
                    index_data[note_id] = rec
                    _save_notes_index(index_data)

            try:
                os.remove(legacy_path)
            except Exception as ex:
                print(f"Legacy migration: failed to remove {legacy_name}: {ex}")

        print("Legacy brain dump migration complete.")
    finally:
        with LEGACY_MIGRATION_LOCK:
            LEGACY_MIGRATION_RUNNING = False
            LEGACY_MIGRATION_DONE = True

def _ensure_content_hash_index(tbl):
    global CONTENT_HASH_INDEX_READY
    if CONTENT_HASH_INDEX_READY:
        return
    rows = tbl.search().limit(50000).to_list()
    for row in rows:
        content_hash = _parse_row_meta(row).get("content_hash")
        source_file = row.get("source_file")
        if content_hash and source_file and content_hash not in CONTENT_HASH_INDEX:
            CONTENT_HASH_INDEX[content_hash] = source_file
    CONTENT_HASH_INDEX_READY = True

def _release_inflight_upload(filename: str, content_hash: str, mark_complete: bool):
    if INFLIGHT_CONTENT_HASHES.get(content_hash) == filename:
        INFLIGHT_CONTENT_HASHES.pop(content_hash, None)
    INFLIGHT_FILENAMES.discard(filename)
    if mark_complete:
        CONTENT_HASH_INDEX[content_hash] = filename

def _remove_file_from_hash_index(filename: str):
    INFLIGHT_FILENAMES.discard(filename)
    for h, f in list(INFLIGHT_CONTENT_HASHES.items()):
        if f == filename:
            INFLIGHT_CONTENT_HASHES.pop(h, None)
    for h, f in list(CONTENT_HASH_INDEX.items()):
        if f == filename:
            CONTENT_HASH_INDEX.pop(h, None)


def _read_note_content(note_id: str) -> str:
    path = _build_note_path(note_id)
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _update_note_record(note_id: str, updater):
    with NOTES_INDEX_LOCK:
        index_data = _load_notes_index()
        record = index_data.get(note_id)
        if not record:
            return
        updater(record)
        index_data[note_id] = record
        _save_notes_index(index_data)


def _note_to_response(note_id: str, record: dict, include_content: bool = False) -> dict:
    source_file = str(record.get("source_file") or _build_note_source_file(note_id))
    with UPLOAD_STATUS_LOCK:
        upload_status = UPLOAD_STATUS.get(source_file)
    content_hash = str(record.get("content_hash") or "")
    indexed_hash = str(record.get("indexed_hash") or "")
    needs_indexing = bool(content_hash) and content_hash != indexed_hash

    payload = {
        "note_id": note_id,
        "title": str(record.get("title") or "Untitled"),
        "source_file": source_file,
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "context": str(record.get("context") or ""),
        "indexed_at": record.get("indexed_at") or "",
        "needs_indexing": needs_indexing,
        "index_status": upload_status,
    }
    if include_content:
        payload["content"] = _read_note_content(note_id)
    return payload


def _queue_note_index(background_tasks: BackgroundTasks, note_id: str, source_file: str, note_path: str, title: str, context: str, content_hash: str):
    def _process_note_index():
        with UPLOAD_STATUS_LOCK:
            UPLOAD_STATUS[source_file] = {"status": "queued", "error": None, "queue": "document"}

        with DOCUMENT_PROCESSING_SEMAPHORE:
            with UPLOAD_STATUS_LOCK:
                UPLOAD_STATUS[source_file] = {"status": "processing", "error": None, "queue": "document"}
            try:
                process_file(note_path, upload_context=context or "", content_hash=None)
                _apply_note_metadata_to_rows(source_file, note_id, title)
                _invalidate_retrieval_caches("note_index")

                def _mark_indexed(rec):
                    rec["indexed_hash"] = content_hash
                    rec["indexed_at"] = _utc_now_iso()
                    rec["updated_at"] = _utc_now_iso()

                _update_note_record(note_id, _mark_indexed)

                with UPLOAD_STATUS_LOCK:
                    UPLOAD_STATUS[source_file] = {"status": "done", "error": None, "queue": "document"}
            except Exception as ex:
                with UPLOAD_STATUS_LOCK:
                    UPLOAD_STATUS[source_file] = {"status": "failed", "error": str(ex), "queue": "document"}

    background_tasks.add_task(_process_note_index)

def _settings_path() -> str:
    return os.path.join(os.path.dirname(__file__), "app_settings.json")

def _load_settings() -> dict:
    path = _settings_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {"chat_provider": "gemini"}

def _save_settings(data: dict):
    with open(_settings_path(), "w", encoding="utf-8") as f:
        json.dump(data, f)

def _get_chat_provider() -> str:
    provider = _load_settings().get("chat_provider", "gemini")
    return provider if provider in ("gemini", "claude_oauth") else "gemini"

def _set_chat_provider(provider: str):
    data = _load_settings()
    data["chat_provider"] = provider
    _save_settings(data)

def _pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("utf-8").rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).decode("utf-8").rstrip("=")
    return verifier, challenge

def _store_claude_tokens(tokens: dict):
    keyring.set_password(TOKEN_SERVICE_NAME, TOKEN_USERNAME, json.dumps(tokens))

def _get_claude_tokens() -> dict | None:
    raw = keyring.get_password(TOKEN_SERVICE_NAME, TOKEN_USERNAME)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None

def _clear_claude_tokens():
    try:
        keyring.delete_password(TOKEN_SERVICE_NAME, TOKEN_USERNAME)
    except Exception:
        pass

def _exchange_oauth_code(code: str, verifier: str) -> dict:
    if not CLAUDE_OAUTH_CLIENT_ID or not CLAUDE_OAUTH_CLIENT_SECRET:
        raise Exception("Claude OAuth credentials missing. Configure CLAUDE_OAUTH_CLIENT_ID and CLAUDE_OAUTH_CLIENT_SECRET.")
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
        "client_secret": CLAUDE_OAUTH_CLIENT_SECRET,
        "code": code,
        "redirect_uri": CLAUDE_OAUTH_REDIRECT_URI,
        "code_verifier": verifier,
    }
    resp = requests.post(CLAUDE_OAUTH_TOKEN_URL, data=payload, timeout=20)
    if resp.status_code >= 300:
        raise Exception(f"Claude token exchange failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    expires_in = int(data.get("expires_in", 3600))
    return {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "token_type": data.get("token_type", "Bearer"),
        "expires_at": int(time.time()) + max(60, expires_in - 30),
        "scope": data.get("scope"),
    }

def _refresh_oauth_token(refresh_token: str) -> dict:
    payload = {
        "grant_type": "refresh_token",
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
        "client_secret": CLAUDE_OAUTH_CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    resp = requests.post(CLAUDE_OAUTH_TOKEN_URL, data=payload, timeout=20)
    if resp.status_code >= 300:
        raise Exception(f"Claude token refresh failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    expires_in = int(data.get("expires_in", 3600))
    return {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token") or refresh_token,
        "token_type": data.get("token_type", "Bearer"),
        "expires_at": int(time.time()) + max(60, expires_in - 30),
        "scope": data.get("scope"),
    }

def _get_valid_claude_access_token() -> str:
    tokens = _get_claude_tokens()
    if not tokens or not tokens.get("access_token"):
        raise Exception("Claude account is not connected.")
    if int(tokens.get("expires_at", 0)) > int(time.time()) + 15:
        return tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        _clear_claude_tokens()
        raise Exception("Claude session expired. Please reconnect.")
    refreshed = _refresh_oauth_token(refresh_token)
    _store_claude_tokens(refreshed)
    return refreshed["access_token"]

def _claude_connected() -> bool:
    tokens = _get_claude_tokens()
    return bool(tokens and tokens.get("access_token"))

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    provider = req.provider if req.provider in ("gemini", "claude_oauth") else _get_chat_provider()
    if provider == "gemini" and not gemini:
        return {"answer": "Gemini API Key missing.", "sources": []}
    tbl = get_table()

    # 1. Embed query
    try:
        result = gemini.models.embed_content(
            model="gemini-embedding-2-preview",
            contents=[req.query],
            config=genai.types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=1536
            )
        )
        query_vector = result.embeddings[0].values
    except Exception as e:
        error_msg = str(e)
        async def embed_error_stream():
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                msg = "My Second Brain is at capacity (Gemini Quota Reached). Please wait a minute and try again."
            else:
                msg = f"Failed to process your question: {error_msg}"
            yield f"data: {json.dumps({'type': 'error', 'text': msg})}\n\n"
        return StreamingResponse(embed_error_stream(), media_type="text/event-stream")

    # 2. Search LanceDB
    try:
        results = tbl.search(query_vector).limit(req.top_k).to_list()
    except Exception:
        results = []

    if not results:
        async def empty_stream():
            yield f"data: {json.dumps({'type': 'answer', 'text': 'No relevant documents found.'})}\n\n"
        return StreamingResponse(empty_stream(), media_type="text/event-stream")
        
    context_parts = []
    for r in results:
        src = f"[{r['source_type'].upper()}] {r['source_file']}"
        meta = json.loads(r["metadata"]) if isinstance(r.get("metadata"), str) else r.get("metadata", {})
        upload_context_val = (meta or {}).get("upload_context", "")
        contextual_prefix = f"\nUpload Context: {upload_context_val}\n" if upload_context_val else "\n"
        # For video results, include transcript for precise clip generation
        clip_line = ""
        if r["source_type"] == "video":
            ts_start            = (meta or {}).get("timestamp_start")
            ts_end              = (meta or {}).get("timestamp_end")
            transcript          = (meta or {}).get("transcript", "")
            transcript_absolute = (meta or {}).get("transcript_absolute", False)
            if ts_start is not None:
                enc = urllib.parse.quote(r["source_file"], safe="")
                if transcript:
                    if transcript_absolute:
                        clip_line = (
                            f"\nTranscript (timestamps are ABSOLUTE — use directly as clip start/end):\n"
                            f"{transcript}\n"
                            f"Preview URL template: /clip?file={enc}&start=ABSOLUTE_START&end=ABSOLUTE_END\n"
                        )
                    else:
                        clip_line = (
                            f"\nChunk window: {int(ts_start)}s–{int(ts_end)}s in full video\n"
                            f"Transcript (timestamps relative to chunk start = {int(ts_start)}s):\n"
                            f"{transcript}\n"
                            f"To get absolute position: add transcript seconds to {int(ts_start)}.\n"
                            f"Preview URL template: /clip?file={enc}&start=ABSOLUTE_START&end=ABSOLUTE_END\n"
                        )
                else:
                    clip_url = _clip_relative_url(r["source_file"], int(ts_start), int(ts_end))
                    clip_line = f"\nWatch full chunk: {clip_url}\n"
            elif transcript:
                # Whole-file video (no ffmpeg chunking) — ts_start is None but transcript exists.
                # Transcript timestamps are relative to video start = 0, so they are absolute.
                enc = urllib.parse.quote(r["source_file"], safe="")
                clip_line = (
                    f"\nTranscript (timestamps are ABSOLUTE — video start = 00:00):\n"
                    f"{transcript}\n"
                    f"Preview URL template: /clip?file={enc}&start=ABSOLUTE_START&end=ABSOLUTE_END\n"
                )
        context_parts.append(f"{src}{contextual_prefix}{clip_line}{r['content']}")
    context_str = "\n\n---\n\n".join(context_parts)
    # Cap context to avoid token overflow (RESOURCE_EXHAUSTED)
    if len(context_str) > 28000:
        context_str = context_str[:28000] + "\n\n[Context truncated to fit model limits]"
    
    # 3. Create Generator for Streaming Response
    async def chat_generator():
        system_instructions = (
            "You are the 'My Second Brain' assistant. "
            "Use retrieved context and respond clearly. "
            "First give short reasoning, then final answer.\n\n"
            "VIDEO CLIPS — FOLLOW THIS EXACTLY:\n"
            "When context contains a video chunk with a Transcript and a Preview URL template:\n"
            "1. Read ALL transcript lines relevant to the user's question.\n"
            "2. Find the FIRST relevant [MM:SS] — where the relevant speech starts.\n"
            "3. Find the LAST relevant [MM:SS] — where the relevant speech ends.\n"
            "4. Convert both to seconds. If transcript_absolute=False, add chunk's timestamp_start to each.\n"
            "   Example: chunk start = 120s, transcript [00:08]→[00:42] → clip = 128s to 162s\n"
            "5. Subtract 1s from start and add 2s to end for natural lead-in/out.\n"
            "6. Replace ABSOLUTE_START and ABSOLUTE_END in the Preview URL template.\n"
            "7. Always include it as: [Watch clip](URL)\n"
            "Clip length must match the actual speech — NOT a fixed duration.\n"
            "Short quote = short clip. Full answer = longer clip. Never pad with silence.\n"
            "If no transcript is present but a 'Watch full chunk' URL exists, use that instead."
        )
        prompt = f"{system_instructions}\n\nRetrieved Context:\n{context_str}\n\nUser Question: {req.query}"

        citations = []
        for r in results:
            dist = r.get("_distance", 0.5)
            conf = max(30, min(98, int((1 - dist) * 100)))
            meta = _parse_row_meta(r)
            display_name = _display_name_for_source(r["source_file"], meta)
            citations.append({
                "file": display_name,
                "source_file": r["source_file"],
                "confidence": conf,
                "relevant_quote": r["content"][:250],
                "type": r.get("source_type", "text")
            })

        try:
            if provider == "claude_oauth":
                access_token = _get_valid_claude_access_token()
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }
                body = {
                    "model": CLAUDE_MODEL,
                    "max_tokens": 1200,
                    "system": system_instructions,
                    "messages": [{"role": "user", "content": prompt}],
                }
                resp = requests.post(f"{CLAUDE_API_BASE}/v1/messages", headers=headers, json=body, timeout=60)
                if resp.status_code == 401:
                    _clear_claude_tokens()
                    raise Exception("Claude auth expired. Please reconnect your Claude account.")
                if resp.status_code >= 300:
                    raise Exception(f"Claude request failed: {resp.status_code} {resp.text[:300]}")
                data = resp.json()
                text_parts = []
                for part in data.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                answer_text = "\n".join([p for p in text_parts if p]).strip()
                if answer_text:
                    yield f"data: {json.dumps({'type': 'thinking', 'text': 'Using connected Claude account context-aware reasoning...'})}\n\n"
                    yield f"data: {json.dumps({'type': 'answer', 'text': answer_text})}\n\n"
            else:
                current_tag = "thinking"
                stream = gemini.models.generate_content_stream(
                    model=CHAT_MODEL,
                    contents=prompt
                )
                answer_started = False
                for chunk in stream:
                    if not chunk.text:
                        continue
                    text = chunk.text
                    if "[ANSWER]" in text:
                        parts = text.split("[ANSWER]")
                        thinking_part = parts[0].replace("[THOUGHT]", "").strip()
                        if thinking_part:
                            yield f"data: {json.dumps({'type': 'thinking', 'text': thinking_part})}\n\n"
                        current_tag = "answer"
                        answer_started = True
                        if len(parts) > 1 and parts[1].strip():
                            yield f"data: {json.dumps({'type': 'answer', 'text': parts[1].strip()})}\n\n"
                    else:
                        clean_text = text.replace("[THOUGHT]", "").strip() if not answer_started else text
                        if clean_text:
                            yield f"data: {json.dumps({'type': current_tag, 'text': clean_text})}\n\n"

            yield f"data: {json.dumps({'type': 'metadata', 'citations': citations})}\n\n"
            
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                friendly_msg = (
                    "My Second Brain is at capacity (Gemini Quota Reached). "
                    "Please wait ~30-60 seconds and try again.\n\n"
                    f"Details: {error_msg[:300]}"
                )
                yield f"data: {json.dumps({'type': 'error', 'text': friendly_msg})}\n\n"
            elif "Claude auth expired" in error_msg or "reconnect your Claude account" in error_msg:
                yield f"data: {json.dumps({'type': 'error', 'text': error_msg})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'error', 'text': f'Error: {error_msg}'})}\n\n"

    return StreamingResponse(chat_generator(), media_type="text/event-stream")

@app.get("/api/graph")
def get_graph():
    _migrate_legacy_brain_dumps()
    tbl = get_table()
    try:
        # Increased limit to ensure all nodes are retrieved
        data = tbl.search().limit(10000).to_list()
        brain_dump_index_map = _build_brain_dump_index_map([d.get("source_file", "") for d in data])
        nodes = []
        links = {}
        doc_file_index = {}
        doc_node_by_id = {}

        topic_to_docs = {}
        topic_display_name = {}
        doc_vectors = {}

        def add_link(source, target, weight, link_type):
            key = (str(source), str(target), link_type)
            existing = links.get(key)
            if not existing or weight > existing["weight"]:
                links[key] = {
                    "source": source,
                    "target": target,
                    "weight": float(weight),
                    "type": link_type
                }

        # 1. Create Nodes and Topic Links
        for d in data:
            file_key = d["source_file"]
            meta = _parse_row_meta(d)
            if file_key in doc_file_index:
                doc_id = doc_file_index[file_key]
                refreshed_name = _display_name_for_source(file_key, meta, brain_dump_index_map)
                node_ref = doc_node_by_id.get(doc_id)
                if node_ref and refreshed_name and node_ref.get("name") == file_key and refreshed_name != file_key:
                    node_ref["name"] = refreshed_name
            else:
                doc_id = d["id"]
                doc_file_index[file_key] = doc_id
                doc_vectors[doc_id] = d.get("vector")
                display_name = _display_name_for_source(file_key, meta, brain_dump_index_map)
                node_payload = {
                    "id": doc_id,
                    "name": display_name,
                    "group": "document",
                    "val": 4.5,
                    "source_type": d["source_type"],
                    "source_file": file_key,
                    "note_id": meta.get("note_id"),
                    "is_brain_dump_note": _is_note_source(file_key, meta),
                }
                nodes.append(node_payload)
                doc_node_by_id[doc_id] = node_payload
            topics = _sanitize_topics(meta.get("topics", []))
            
            for norm_t in topics:
                if norm_t not in topic_to_docs:
                    topic_to_docs[norm_t] = set()
                    topic_display_name[norm_t] = _topic_display(norm_t)
                    nodes.append({
                        "id": _topic_node_id(norm_t),
                        "name": topic_display_name[norm_t],
                        "group": "topic",
                        "val": 3.2, 
                        "color_seed": sum(ord(c) for c in norm_t) % 100
                    })
                topic_to_docs[norm_t].add(doc_id)
                add_link(doc_id, _topic_node_id(norm_t), 0.9, "topic")

        # 2. Topic-Topic Links (Shared Documents)
        topic_list = list(topic_to_docs.keys())
        for i in range(len(topic_list)):
            for j in range(i + 1, len(topic_list)):
                t1 = topic_list[i]
                t2 = topic_list[j]
                intersection = topic_to_docs[t1].intersection(topic_to_docs[t2])
                if len(intersection) < 2:
                    continue
                union_size = len(topic_to_docs[t1].union(topic_to_docs[t2])) or 1
                jaccard = len(intersection) / union_size
                if jaccard < 0.2:
                    continue
                weight = min(1.0, 0.25 + (0.6 * jaccard) + (0.1 * min(len(intersection), 3)))
                add_link(_topic_node_id(t1), _topic_node_id(t2), weight, "topic_overlap")

        # 2b. Recover missed topic-doc links using semantic similarity to topic anchor docs
        topic_to_anchor_vector = {}
        for topic, doc_ids in topic_to_docs.items():
            if len(doc_ids) < 2:
                continue
            vectors = [doc_vectors.get(doc_id) for doc_id in doc_ids if doc_vectors.get(doc_id) is not None]
            if not vectors:
                continue
            arr = np.array(vectors, dtype=float)
            topic_to_anchor_vector[topic] = np.mean(arr, axis=0)

        if topic_to_anchor_vector and doc_vectors:
            existing_doc_topic = {
                (src, tgt) for (src, tgt, lt) in links.keys()
                if lt == "topic" and src in doc_vectors and isinstance(tgt, str) and tgt.startswith("topic::")
            }
            semantic_threshold = 0.9
            for topic, anchor_vec in topic_to_anchor_vector.items():
                anchor_norm = np.linalg.norm(anchor_vec) or 1e-10
                topic_id = _topic_node_id(topic)
                candidates = []
                for doc_id, vec in doc_vectors.items():
                    if vec is None:
                        continue
                    if (doc_id, topic_id) in existing_doc_topic:
                        continue
                    vec_arr = np.array(vec, dtype=float)
                    sim = float(np.dot(vec_arr, anchor_vec) / ((np.linalg.norm(vec_arr) or 1e-10) * anchor_norm))
                    if sim >= semantic_threshold:
                        candidates.append((doc_id, sim))
                candidates.sort(key=lambda x: x[1], reverse=True)
                for doc_id, sim in candidates[:2]:
                    add_link(doc_id, topic_id, min(1.0, 0.55 + (sim - semantic_threshold)), "topic_semantic")
                     
        # 3. Document-Document Links (Semantic Similarity)
        if len(doc_vectors) > 1:
            try:
                valid_docs = [{"id": doc_id, "vector": vec} for doc_id, vec in doc_vectors.items() if vec is not None]
                if len(valid_docs) < 2:
                    return {"nodes": nodes, "links": list(links.values())}

                vectors = np.array([d["vector"] for d in valid_docs], dtype=float)
                norms = np.linalg.norm(vectors, axis=1, keepdims=True)
                norms[norms == 0] = 1e-10
                normalized = vectors / norms
                sim_matrix = np.dot(normalized, normalized.T)

                threshold = 0.9
                max_neighbors = 3
                doc_neighbors = {i: [] for i in range(len(valid_docs))}
                for i in range(len(valid_docs)):
                    for j in range(i + 1, len(valid_docs)):
                        sim = sim_matrix[i, j]
                        if sim < threshold:
                            continue
                        doc_neighbors[i].append((j, float(sim)))
                        doc_neighbors[j].append((i, float(sim)))

                for i, neighbors in doc_neighbors.items():
                    neighbors.sort(key=lambda x: x[1], reverse=True)
                    for j, sim in neighbors[:max_neighbors]:
                        source_id = valid_docs[i]["id"]
                        target_id = valid_docs[j]["id"]
                        pair = tuple(sorted([source_id, target_id]))
                        add_link(pair[0], pair[1], sim, "semantic")
            except Exception as e:
                print(f"Vector similarity error: {e}")

        # 4. Entity-based Document-Document Links (shared person/org entities)
        try:
            # Map doc_id -> set of person/org entity names from all its chunks
            doc_entity_names: dict = {}
            for d in data:
                file_key = d["source_file"]
                doc_id = doc_file_index.get(file_key)
                if doc_id is None:
                    continue
                if doc_id not in doc_entity_names:
                    doc_entity_names[doc_id] = set()
                row_meta = json.loads(d["metadata"]) if isinstance(d["metadata"], str) else d.get("metadata", {})
                for ent in row_meta.get("entities", {}).get("entities", []):
                    if ent.get("type") in ("person", "organisation"):
                        name = ent.get("name", "").strip().lower()
                        if name:
                            doc_entity_names[doc_id].add(name)

            doc_ids = list(doc_entity_names.keys())
            for i in range(len(doc_ids)):
                for j in range(i + 1, len(doc_ids)):
                    a, b = doc_ids[i], doc_ids[j]
                    shared = doc_entity_names[a].intersection(doc_entity_names[b])
                    if not shared:
                        continue
                    weight = min(1.0, 0.4 + 0.1 * len(shared))
                    pair = tuple(sorted([a, b]))
                    add_link(pair[0], pair[1], weight, "entity")
        except Exception as e:
            print(f"Entity link error: {e}")

        return {"nodes": nodes, "links": list(links.values())}
    except Exception as e:
        print(f"Graph Error: {e}")
        return {"nodes": [], "links": []}

@app.get("/api/files")
def list_files():
    _migrate_legacy_brain_dumps()
    tbl = get_table()
    try:
        # LanceDB search().limit(N) returns N rows. If you have many chunks per file, 
        # this might cut off some files if N is small.
        # Better to query unique source_files if possible, but LanceDB SQL is limited.
        # We'll fetch a larger limit to be safe, or scan.
        docs = tbl.search().limit(10000).to_list()
    except Exception as e:
        print(f"Error listing files: {e}")
        return {"files": []}

    brain_dump_index_map = _build_brain_dump_index_map([d.get("source_file", "") for d in docs])

    index_data = _load_notes_index()
    source_note_map: dict[str, dict] = {}
    for note_id, rec in index_data.items():
        if not isinstance(rec, dict):
            continue
        source_file = str(rec.get("source_file") or _build_note_source_file(note_id))
        source_note_map[source_file] = {
            "note_id": note_id,
            "title": str(rec.get("title") or "").strip(),
        }
        
    files = {}
    for d in docs:
        if d["source_file"] not in files:
            source_name = d["source_file"]
            meta = _parse_row_meta(d)
            linked_note = source_note_map.get(source_name) or {}
            note_id = meta.get("note_id") or linked_note.get("note_id")

            effective_meta = dict(meta)
            if note_id and not effective_meta.get("note_id"):
                effective_meta["note_id"] = note_id
            if linked_note.get("title") and not effective_meta.get("display_name"):
                effective_meta["display_name"] = linked_note.get("title")

            brain_dump_index = brain_dump_index_map.get(source_name)
            display_name = _display_name_for_source(source_name, effective_meta, brain_dump_index_map)
            files[d["source_file"]] = {
                "name": source_name,
                "type": d["source_type"],
                "is_brain_dump": bool(note_id) or _is_note_source(source_name, effective_meta),
                "brain_dump_index": brain_dump_index,
                "display_name": display_name,
                "note_id": note_id,
            }
            
    # Sort alphabetically
    sorted_files = sorted(list(files.values()), key=lambda x: x['name'])
    return {"files": sorted_files}

@app.get("/api/docx-preview/{filename:path}")
def docx_preview(filename: str):
    """Convert a DOCX file to styled HTML for in-browser preview."""
    file_path = os.path.join(REPO_ROOT, "brain_data", filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        import mammoth, base64 as _b64
        def _embed_image(image):
            with image.open() as img_f:
                data = _b64.b64encode(img_f.read()).decode()
            return {"src": f"data:{image.content_type};base64,{data}"}
        with open(file_path, "rb") as f:
            result = mammoth.convert_to_html(f, convert_image=mammoth.images.inline(_embed_image))
        html_body = result.value
    except ImportError:
        raise HTTPException(status_code=500, detail="mammoth not installed. Run: pip install mammoth")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to convert DOCX: {e}")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Georgia', serif;
    font-size: 14px;
    line-height: 1.75;
    color: #1a1a1a;
    background: #ffffff;
    padding: 48px 56px;
    max-width: 860px;
    margin: 0 auto;
  }}
  h1 {{ font-size: 2em; font-weight: 700; margin: 0.8em 0 0.4em; border-bottom: 2px solid #e0e0e0; padding-bottom: 0.2em; }}
  h2 {{ font-size: 1.5em; font-weight: 700; margin: 1em 0 0.4em; }}
  h3 {{ font-size: 1.2em; font-weight: 600; margin: 0.8em 0 0.3em; }}
  h4, h5, h6 {{ font-size: 1em; font-weight: 600; margin: 0.6em 0 0.2em; }}
  p {{ margin-bottom: 0.85em; }}
  ul, ol {{ padding-left: 1.8em; margin-bottom: 0.85em; }}
  li {{ margin-bottom: 0.3em; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1em; font-size: 0.9em; }}
  td, th {{ border: 1px solid #d0d0d0; padding: 8px 12px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; font-weight: 600; }}
  tr:nth-child(even) td {{ background: #fafafa; }}
  strong, b {{ font-weight: 700; }}
  em, i {{ font-style: italic; }}
  img {{ max-width: 100%; height: auto; margin: 0.5em 0; border-radius: 4px; }}
  hr {{ border: none; border-top: 1px solid #e0e0e0; margin: 1.5em 0; }}
  blockquote {{ border-left: 4px solid #d0d0d0; padding-left: 1em; color: #555; margin: 1em 0; font-style: italic; }}
  code {{ background: #f0f0f0; padding: 2px 5px; border-radius: 3px; font-family: monospace; font-size: 0.9em; }}
  pre {{ background: #f0f0f0; padding: 1em; border-radius: 4px; overflow-x: auto; margin-bottom: 0.85em; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
    return HTMLResponse(content=html)


@app.delete("/api/files/{filename}")
def delete_file(filename: str):
    tbl = get_table()
    file_path = _resolve_source_file_path(filename)
    try:
        rows = tbl.search().limit(20000).to_list()
        ids_to_delete = [r.get("id") for r in rows if r.get("source_file") == filename and r.get("id")]
        for row_id in ids_to_delete:
            tbl.delete(f"id = '{row_id}'")
    except Exception as e:
        return {"status": "error", "message": f"Failed deleting from database: {e}"}

    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        return {"status": "error", "message": f"Deleted from database but failed deleting local file: {e}"}

    with UPLOAD_DEDUPE_LOCK:
        _remove_file_from_hash_index(filename)
    _remove_note_record_by_source_file(filename)
    _invalidate_retrieval_caches("delete_file")

    return {"status": "success"}


@app.post("/api/files/{filename}/rename")
def rename_file(filename: str, body: dict):
    """Rename a file/node. Updates the source_file field in all DB rows and renames the file on disk."""
    new_name = (body.get("new_name") or "").strip()
    if not new_name:
        return {"status": "error", "message": "new_name is required"}

    # Prevent path traversal
    if "/" in new_name or "\\" in new_name or new_name.startswith("."):
        return {"status": "error", "message": "Invalid name"}

    # Keep the same file extension
    old_ext = os.path.splitext(filename)[1].lower()
    new_ext = os.path.splitext(new_name)[1].lower()
    if not new_ext:
        new_name = new_name + old_ext
    elif new_ext != old_ext:
        return {"status": "error", "message": f"Cannot change file extension (must keep {old_ext})"}

    if new_name == filename:
        return {"status": "success", "new_name": new_name}

    tbl = get_table()
    BRAIN_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "brain_data")

    # Check new name doesn't already exist
    old_path = _resolve_source_file_path(filename)
    # Try brain_data root first, then notes subdir
    new_path = os.path.join(BRAIN_DATA_DIR, new_name)
    notes_path = os.path.join(BRAIN_DATA_DIR, "notes", new_name)
    if os.path.exists(new_path) or os.path.exists(notes_path):
        return {"status": "error", "message": f"A file named '{new_name}' already exists"}

    # Update all DB rows that reference this file
    try:
        all_rows = tbl.search().limit(20000).to_list()
        rows_to_update = [r for r in all_rows if r.get("source_file") == filename]
        if not rows_to_update:
            return {"status": "error", "message": f"No knowledge base records found for '{filename}'"}

        for row in rows_to_update:
            row_id = row.get("id")
            if not row_id:
                continue
            tbl.delete(f"id = '{row_id}'")
            row["source_file"] = new_name
            # Update display_name inside metadata if it still references the old filename
            try:
                meta = json.loads(row.get("metadata") or "{}")
                if meta.get("display_name", "").strip() == filename:
                    meta["display_name"] = new_name
                    row["metadata"] = json.dumps(meta)
            except Exception:
                pass
            tbl.add([row])
    except Exception as e:
        return {"status": "error", "message": f"Database update failed: {e}"}

    # Rename the physical file
    if old_path and os.path.exists(old_path):
        new_disk_path = os.path.join(os.path.dirname(old_path), new_name)
        try:
            os.rename(old_path, new_disk_path)
        except Exception as e:
            # DB is already updated — roll it back isn't practical; just warn
            _invalidate_retrieval_caches("rename_file")
            return {"status": "partial", "message": f"DB updated but file rename failed: {e}", "new_name": new_name}

    # Update note records if this was a note file
    _rename_note_record_by_source_file(filename, new_name)

    # Update hash index: re-map any hash pointing at the old filename to the new one
    with UPLOAD_DEDUPE_LOCK:
        for h, f in list(CONTENT_HASH_INDEX.items()):
            if f == filename:
                CONTENT_HASH_INDEX[h] = new_name

    _invalidate_retrieval_caches("rename_file")
    return {"status": "success", "new_name": new_name}


@app.post("/api/upload")
async def upload_file(background_tasks: BackgroundTasks, file: UploadFile = File(...), upload_context: str = Form(default="")):
    os.makedirs(os.path.join(os.path.dirname(os.path.dirname(__file__)), "brain_data"), exist_ok=True)
    file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "brain_data", file.filename)
    file_bytes = file.file.read()
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    tbl = get_table()
    safe_name = file.filename.replace("'", "''")
    with UPLOAD_DEDUPE_LOCK:
        _ensure_content_hash_index(tbl)

        duplicate_by_hash = INFLIGHT_CONTENT_HASHES.get(file_hash) or CONTENT_HASH_INDEX.get(file_hash)
        if duplicate_by_hash:
            raise HTTPException(
                status_code=409,
                detail=f"Exact duplicate blocked: this file content already exists as '{duplicate_by_hash}'."
            )

        if file.filename in INFLIGHT_FILENAMES:
            raise HTTPException(status_code=409, detail=f"Duplicate filename blocked: '{file.filename}' is already queued or processing.")

        try:
            same_name_rows = tbl.search().where(f"source_file = '{safe_name}'").limit(1).to_list()
        except Exception:
            fallback_rows = tbl.search().limit(20000).to_list()
            same_name_rows = [r for r in fallback_rows if r.get("source_file") == file.filename][:1]
        if same_name_rows:
            raise HTTPException(status_code=409, detail=f"Duplicate filename blocked: '{file.filename}' already exists. Rename file or delete existing one first.")

        # Reserve duplicate slots before writing file to avoid races in parallel uploads.
        INFLIGHT_CONTENT_HASHES[file_hash] = file.filename
        INFLIGHT_FILENAMES.add(file.filename)

    try:
        with open(file_path, "wb") as buffer:
            buffer.write(file_bytes)
    except Exception:
        with UPLOAD_DEDUPE_LOCK:
            _release_inflight_upload(file.filename, file_hash, mark_complete=False)
        raise

    ext = os.path.splitext(file.filename)[1].lower()
    is_document = ext in DOCUMENT_EXTENSIONS
    selected_semaphore = DOCUMENT_PROCESSING_SEMAPHORE if is_document else MEDIA_PROCESSING_SEMAPHORE
    queue_label = "document" if is_document else "media"
    
    def _process_with_status(path: str, filename: str, context: str, content_hash: str):
        # Mark as queued while waiting for the semaphore slot
        with UPLOAD_STATUS_LOCK:
            UPLOAD_STATUS[filename] = {"status": "queued", "error": None, "queue": queue_label}
        # Acquire slot — blocks until any currently-processing file finishes
        with selected_semaphore:
            with UPLOAD_STATUS_LOCK:
                UPLOAD_STATUS[filename] = {"status": "processing", "error": None, "queue": queue_label}
            try:
                process_file(path, upload_context=context, content_hash=content_hash)
                _invalidate_retrieval_caches("upload_process_file")
                with UPLOAD_STATUS_LOCK:
                    UPLOAD_STATUS[filename] = {"status": "done", "error": None, "queue": queue_label}
                with UPLOAD_DEDUPE_LOCK:
                    _release_inflight_upload(filename, content_hash, mark_complete=True)
            except Exception as e:
                with UPLOAD_STATUS_LOCK:
                    UPLOAD_STATUS[filename] = {"status": "failed", "error": str(e), "queue": queue_label}
                with UPLOAD_DEDUPE_LOCK:
                    _release_inflight_upload(filename, content_hash, mark_complete=False)

    # Queue for background processing (document and media lanes are bounded separately)
    background_tasks.add_task(_process_with_status, file_path, file.filename, upload_context or "", file_hash)
        
    return {"status": "success", "filename": file.filename, "message": "File queued for processing."}


@app.get("/api/notes")
def list_notes():
    _migrate_legacy_brain_dumps()
    with NOTES_INDEX_LOCK:
        index_data = _load_notes_index()
        notes = [_note_to_response(note_id, rec, include_content=False) for note_id, rec in index_data.items()]
    notes.sort(key=lambda n: (n.get("updated_at") or ""), reverse=True)
    return {"notes": notes}


@app.get("/api/notes/{note_id}")
def get_note(note_id: str):
    _migrate_legacy_brain_dumps()
    _validate_note_id(note_id)
    with NOTES_INDEX_LOCK:
        index_data = _load_notes_index()
        record = index_data.get(note_id)
    if not record:
        raise HTTPException(status_code=404, detail="Note not found.")
    return _note_to_response(note_id, record, include_content=True)


@app.post("/api/notes/save")
async def save_note(req: NoteSaveRequest, background_tasks: BackgroundTasks):
    _migrate_legacy_brain_dumps()

    title = _normalize_note_title(req.title)
    if not title:
        raise HTTPException(status_code=400, detail="Title is required.")
    if len(title) > 120:
        raise HTTPException(status_code=400, detail="Title is too long. Keep it under 120 characters.")

    content = req.content or ""
    if len(content) > 300000:
        raise HTTPException(status_code=400, detail="Note is too long. Keep it under 300000 characters.")
    if req.index_now and not content.strip():
        raise HTTPException(status_code=400, detail="Cannot index an empty note. Add content first.")
    context = (req.context or "").strip()
    now = _utc_now_iso()

    with NOTES_INDEX_LOCK:
        index_data = _load_notes_index()
        if req.note_id:
            _validate_note_id(req.note_id)
            note_id = req.note_id
            if note_id not in index_data:
                raise HTTPException(status_code=404, detail="Note not found.")
        else:
            note_id = _generate_note_id(index_data)

        conflict_id = _find_note_title_conflict(index_data, title, exclude_note_id=note_id)
        if conflict_id:
            raise HTTPException(status_code=409, detail="A note with this title already exists. Use a unique title.")

        existing = index_data.get(note_id, {})
        source_file = str(existing.get("source_file") or _build_note_source_file(note_id))
        note_path = _build_note_path(note_id)
        old_title = str(existing.get("title") or "")
        old_indexed_hash = str(existing.get("indexed_hash") or "")
        content_hash = _note_content_hash(content, context)

        try:
            with open(note_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as ex:
            raise HTTPException(status_code=500, detail=f"Failed to save note: {ex}")

        record = {
            "note_id": note_id,
            "title": title,
            "source_file": source_file,
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
            "context": context,
            "content_hash": content_hash,
            "indexed_hash": old_indexed_hash,
            "indexed_at": existing.get("indexed_at") or "",
        }
        index_data[note_id] = record
        _save_notes_index(index_data)

    title_changed = title != old_title
    content_changed = content_hash != old_indexed_hash

    # Title changes do not require re-embedding, but graph/file labels must refresh.
    if title_changed and old_indexed_hash:
        try:
            _apply_note_metadata_to_rows(source_file, note_id, title)
        except Exception as ex:
            print(f"Note title metadata refresh failed for {note_id}: {ex}")

    index_state = "draft_saved"
    if req.index_now:
        if content_changed:
            _queue_note_index(background_tasks, note_id, source_file, note_path, title, context, content_hash)
            index_state = "queued"
        else:
            try:
                _apply_note_metadata_to_rows(source_file, note_id, title)
            except Exception as ex:
                print(f"Note metadata sync skipped for {note_id}: {ex}")
            index_state = "up_to_date"

    return {
        "status": "success",
        "index_state": index_state,
        "note": _note_to_response(note_id, record, include_content=True),
    }


@app.post("/api/notes/{note_id}/index")
async def index_note(note_id: str, background_tasks: BackgroundTasks):
    _migrate_legacy_brain_dumps()
    _validate_note_id(note_id)
    with NOTES_INDEX_LOCK:
        index_data = _load_notes_index()
        record = index_data.get(note_id)
    if not record:
        raise HTTPException(status_code=404, detail="Note not found.")

    source_file = str(record.get("source_file") or _build_note_source_file(note_id))
    note_path = _build_note_path(note_id)
    if not os.path.isfile(note_path):
        raise HTTPException(status_code=404, detail="Note file is missing.")
    if not _read_note_content(note_id).strip():
        raise HTTPException(status_code=400, detail="Cannot index an empty note. Add content first.")

    content_hash = str(record.get("content_hash") or "")
    indexed_hash = str(record.get("indexed_hash") or "")
    title = str(record.get("title") or "Untitled")
    context = str(record.get("context") or "")

    if content_hash and indexed_hash and content_hash == indexed_hash:
        try:
            _apply_note_metadata_to_rows(source_file, note_id, title)
        except Exception as ex:
            print(f"Note metadata sync skipped for {note_id}: {ex}")
        return {
            "status": "up_to_date",
            "note": _note_to_response(note_id, record, include_content=True),
        }

    _queue_note_index(background_tasks, note_id, source_file, note_path, title, context, content_hash)
    return {
        "status": "queued",
        "note": _note_to_response(note_id, record, include_content=True),
    }


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: str):
    _validate_note_id(note_id)
    with NOTES_INDEX_LOCK:
        index_data = _load_notes_index()
        record = index_data.pop(note_id, None)
        if not record:
            raise HTTPException(status_code=404, detail="Note not found.")
        _save_notes_index(index_data)

    source_file = str(record.get("source_file") or _build_note_source_file(note_id))
    note_path = _build_note_path(note_id)

    try:
        if os.path.isfile(note_path):
            os.remove(note_path)
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Failed to delete note file: {ex}")

    try:
        _delete_rows_for_source_file(source_file)
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Failed to delete note from database: {ex}")

    with UPLOAD_DEDUPE_LOCK:
        _remove_file_from_hash_index(source_file)

    return {"status": "success"}


@app.post("/api/brain-dump")
async def create_brain_dump(req: BrainDumpRequest, background_tasks: BackgroundTasks):
    _migrate_legacy_brain_dumps()
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Brain dump text cannot be empty.")
    if len(text) > 120000:
        raise HTTPException(status_code=400, detail="Brain dump is too long. Please keep it under 120000 characters.")

    upload_context = (req.context or "").strip()
    base_title = _derive_note_title_from_text(text, fallback="Brain Dump")
    now = _utc_now_iso()

    with NOTES_INDEX_LOCK:
        index_data = _load_notes_index()
        title = base_title
        suffix = 2
        while _find_note_title_conflict(index_data, title):
            title = f"{base_title} ({suffix})"
            suffix += 1

        note_id = _generate_note_id(index_data)
        filename = _build_note_source_file(note_id)
        file_path = _build_note_path(note_id)
        content_hash = _note_content_hash(text, upload_context)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(text)

        index_data[note_id] = {
            "note_id": note_id,
            "title": title,
            "source_file": filename,
            "created_at": now,
            "updated_at": now,
            "context": upload_context,
            "content_hash": content_hash,
            "indexed_hash": "",
            "indexed_at": "",
        }
        _save_notes_index(index_data)

    _queue_note_index(background_tasks, note_id, filename, file_path, title, upload_context, content_hash)
    return {
        "status": "success",
        "filename": filename,
        "note_id": note_id,
        "title": title,
        "message": "Brain dump queued for processing."
    }

@app.get("/api/upload-status/{filename}")
def get_upload_status(filename: str):
    with UPLOAD_STATUS_LOCK:
        status = UPLOAD_STATUS.get(filename)
    if not status:
        return {"status": "unknown", "error": None}
    return status

@app.get("/api/settings")
def get_settings():
    return {
        "chat_provider": _get_chat_provider(),
        "claude_connected": _claude_connected(),
        "claude_oauth_configured": bool(CLAUDE_OAUTH_CLIENT_ID and CLAUDE_OAUTH_CLIENT_SECRET),
    }

class SettingsUpdateRequest(BaseModel):
    chat_provider: str

@app.post("/api/settings")
def update_settings(req: SettingsUpdateRequest):
    if req.chat_provider not in ("gemini", "claude_oauth"):
        raise HTTPException(status_code=400, detail="Invalid chat provider")
    if req.chat_provider == "claude_oauth" and not _claude_connected():
        raise HTTPException(status_code=400, detail="Claude account is not connected")
    _set_chat_provider(req.chat_provider)
    return {"status": "success", "chat_provider": _get_chat_provider()}

@app.get("/api/auth/claude/start")
def start_claude_auth():
    if not CLAUDE_OAUTH_CLIENT_ID or not CLAUDE_OAUTH_CLIENT_SECRET:
        raise HTTPException(status_code=400, detail="Claude OAuth is not configured on server")
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    with OAUTH_PENDING_LOCK:
        OAUTH_PENDING[state] = {"verifier": verifier, "created_at": int(time.time())}
    params = {
        "response_type": "code",
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
        "redirect_uri": CLAUDE_OAUTH_REDIRECT_URI,
        "scope": "openid profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{CLAUDE_OAUTH_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return {"auth_url": url}

@app.get("/api/auth/claude/callback")
def claude_auth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return HTMLResponse(f"<html><body style='font-family: sans-serif; background:#0a0a0a; color:#ddd;'><h3>Claude connection failed</h3><p>{error}</p><p>You can close this tab.</p></body></html>", status_code=400)
    if not code or not state:
        return HTMLResponse("<html><body style='font-family: sans-serif; background:#0a0a0a; color:#ddd;'><h3>Claude connection failed</h3><p>Missing code/state.</p><p>You can close this tab.</p></body></html>", status_code=400)
    with OAUTH_PENDING_LOCK:
        pending = OAUTH_PENDING.pop(state, None)
    if not pending:
        return HTMLResponse("<html><body style='font-family: sans-serif; background:#0a0a0a; color:#ddd;'><h3>Claude connection failed</h3><p>Session expired. Retry from app.</p><p>You can close this tab.</p></body></html>", status_code=400)
    try:
        tokens = _exchange_oauth_code(code, pending["verifier"])
        if not tokens.get("access_token"):
            raise Exception("No access_token returned")
        _store_claude_tokens(tokens)
        _set_chat_provider("claude_oauth")
    except Exception as e:
        return HTMLResponse(f"<html><body style='font-family: sans-serif; background:#0a0a0a; color:#ddd;'><h3>Claude connection failed</h3><p>{str(e)}</p><p>You can close this tab.</p></body></html>", status_code=400)

    return HTMLResponse("<html><body style='font-family: sans-serif; background:#0a0a0a; color:#ddd;'><h3>Claude connected</h3><p>You can close this tab and return to the app.</p></body></html>")

@app.post("/api/auth/claude/logout")
def claude_logout():
    _clear_claude_tokens()
    if _get_chat_provider() == "claude_oauth":
        _set_chat_provider("gemini")
    return {"status": "success"}

@app.get("/ui_assets/{filename}")
def ui_asset(filename: str):
    allowed = {"Claudeloginbutton.png"}
    if filename not in allowed:
        raise HTTPException(status_code=404, detail="Not found")
    path = os.path.join(REPO_ROOT, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)

@app.get("/api/nodes/{node_id}")
def get_node_details(node_id: str):
    tbl = get_table()
    try:
        print(f"Fetching node details for: {node_id}")
        
        # 1. First try: Exact match on ID (UUID)
        docs = tbl.search().where(f"id = '{node_id}'").limit(1).to_list()
        
        # 2. Second try: Exact match on source_file (for file nodes)
        if not docs:
            docs = tbl.search().where(f"source_file = '{node_id}'").limit(1).to_list()
            
        # 3. Third try (Fallback): Scan all (slow but robust if where clause fails on some types)
        # This is needed because sometimes escaping in where clause is tricky
        if not docs:
             all_data = tbl.search().limit(10000).to_list()
             # Try matching ID
             docs = [d for d in all_data if d['id'] == node_id]
             # Try matching filename
             if not docs:
                docs = [d for d in all_data if d['source_file'] == node_id]

        if docs:
            d = docs[0]
            source_file = d["source_file"]
            meta = _parse_row_meta(d)
            display_name = _display_name_for_source(source_file, meta)

            # Reassemble all chunks for this file (sorted by chunk_index) so the
            # preview shows the full document, not just one chunk.
            try:
                all_rows = tbl.search().limit(20000).to_list()
                file_rows = sorted(
                    [r for r in all_rows if r["source_file"] == source_file],
                    key=lambda r: r.get("chunk_index") or 0
                )
                full_content = "\n\n".join(r["content"] for r in file_rows) if len(file_rows) > 1 else d["content"]
            except Exception:
                file_rows = [d]
                full_content = d["content"]

            # Aggregate entities across all chunks of this file
            agg_entities: dict[str, dict] = {}
            agg_rels: list[dict] = []
            seen_rels: set = set()
            for row in file_rows:
                row_meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row.get("metadata", {})
                for ent in row_meta.get("entities", {}).get("entities", []):
                    name = ent.get("name", "").strip()
                    if name and name not in agg_entities:
                        agg_entities[name] = ent
                for rel in row_meta.get("entities", {}).get("relationships", []):
                    key = f"{rel.get('from')}|{rel.get('relationship')}|{rel.get('to')}"
                    if key not in seen_rels:
                        seen_rels.add(key)
                        agg_rels.append(rel)

            return {
                "type": "document",
                "name": display_name,
                "source_file": source_file,
                "note_id": meta.get("note_id"),
                "content": full_content,
                "topics": [_topic_display(t) for t in _sanitize_topics(meta.get("topics", []))],
                "source_type": d["source_type"],
                "upload_context": meta.get("upload_context", ""),
                "chunk_count": len(file_rows),
                "description": meta.get("description", ""),
                "entities": list(agg_entities.values()),
                "relationships": agg_rels
            }
        
        # 4. Check if it's a topic cluster
        # Only scan if we haven't found a doc
        all_docs = tbl.search().limit(10000).to_list()
        related_docs = []
        topic_norm = _normalize_topic(node_id.replace("topic::", "", 1)) if isinstance(node_id, str) else ""
        for d in all_docs:
            meta = json.loads(d["metadata"]) if isinstance(d["metadata"], str) else d.get("metadata", {})
            topic_norms = _sanitize_topics(meta.get("topics", []))
            if topic_norm and topic_norm in topic_norms:
                related_docs.append(d["source_file"])
                
        if related_docs:
            return {
                "type": "topic",
                "name": _topic_display(topic_norm) if topic_norm else node_id,
                "content": f"Conceptual Intersection: These documents are linked by their shared focus on '{_topic_display(topic_norm) if topic_norm else node_id}'.",
                "related_files": list(set(related_docs)) # dedupe
            }
            
    except Exception as e:
        print(f"Error getting node {node_id}: {e}")
        return {"error": str(e)}
        
    return {"error": "Node empty or not found"}

# ---------------------------------------------------------------------------
# MCP API endpoints — used by backend/mcp_server.py (Claude Desktop bridge)
# ---------------------------------------------------------------------------

@app.post("/api/mcp/search")
def mcp_search(req: MCPSearchRequest):
    """Semantic search for the MCP server. Returns JSON results (non-streaming)."""
    if not gemini:
        return {"error": "Gemini API Key missing. Check your .env file."}

    top_k = max(1, min(req.top_k, 20))

    query_vector: list[float] = []
    embedding_cache_hit = False
    embedding_status = "not_started"

    try:
        query_vector, embedding_cache_hit, embedding_status = _get_query_embedding_cached(req.query)
    except Exception as e:
        embedding_status = f"unavailable:{str(e)[:160]}"
        query_vector = []
        embedding_cache_hit = False

    if not query_vector:
        # Preserve tool usefulness during Gemini throttling by falling back to sidecar keyword search.
        keyword_fallback = mcp_keyword_search(MCPKeywordSearchRequest(keyword=req.query, max_results=top_k))
        if keyword_fallback.get("error"):
            return {"error": f"Embedding unavailable and keyword fallback failed: {keyword_fallback.get('error')}"}

        fallback_results = []
        for row in keyword_fallback.get("results", [])[:top_k]:
            occurrences = int(row.get("occurrences", 0) or 0)
            confidence = max(42, min(88, 48 + min(40, occurrences * 6)))
            fallback_results.append(
                {
                    "content": row.get("snippet", ""),
                    "source_file": row.get("source_file", ""),
                    "display_name": row.get("display_name", row.get("source_file", "")),
                    "source_type": row.get("source_type", "unknown"),
                    "confidence": confidence,
                    "topics": row.get("topics", []),
                    "upload_context": row.get("upload_context", ""),
                    "matched_keywords": row.get("matched_keywords", []),
                    "chunk_index": -1,
                }
            )

        return {
            "results": fallback_results,
            "retrieval_meta": {
                "semantic_used": False,
                "fallback_mode": "keyword_sidecar",
                "embedding_cache_hit": bool(embedding_cache_hit),
                "embedding_status": embedding_status,
                "keyword_candidate_files": (keyword_fallback.get("retrieval_meta", {}) or {}).get("candidate_files", 0),
                "keyword_rows_scanned": (keyword_fallback.get("retrieval_meta", {}) or {}).get("rows_scanned", 0),
            },
        }

    try:
        tbl = get_table()
        rows = tbl.search(query_vector).limit(top_k).to_list()
    except Exception as e:
        return {"error": f"Search failed: {str(e)}"}

    results = []
    for r in rows:
        dist = r.get("_distance", 0.5)
        confidence = max(30, min(98, int((1 - dist) * 100)))
        meta = json.loads(r["metadata"]) if isinstance(r.get("metadata"), str) else r.get("metadata", {})
        display_name = _display_name_for_source(r["source_file"], meta)
        topics = [_topic_display(t) for t in _sanitize_topics((meta or {}).get("topics", []))]
        row: dict = {
            "content": r["content"],
            "source_file": r["source_file"],
            "display_name": display_name,
            "source_type": r["source_type"],
            "confidence": confidence,
            "topics": topics,
            "upload_context": (meta or {}).get("upload_context", ""),
            "chunk_index": r.get("chunk_index", -1),
        }
        # Include scene timestamps and transcript for video results
        if r["source_type"] == "video":
            ts_start            = (meta or {}).get("timestamp_start")
            ts_end              = (meta or {}).get("timestamp_end")
            transcript          = (meta or {}).get("transcript", "")
            transcript_absolute = (meta or {}).get("transcript_absolute", False)
            if ts_start is not None:
                row["timestamp_start"] = ts_start
                row["timestamp_end"]   = ts_end
            if transcript:
                row["transcript"]          = transcript
                row["transcript_absolute"] = transcript_absolute
        results.append(row)

    return {
        "results": results,
        "retrieval_meta": {
            "semantic_used": True,
            "embedding_cache_hit": bool(embedding_cache_hit),
            "embedding_status": embedding_status,
        },
    }


@app.get("/api/mcp/runtime_signature")
def mcp_runtime_signature():
    return {
        "service": "my-second-brain",
        "api_version": "2026-04-04",
        "features": [
            "answer_first_contract_v2",
            "process_sidecar_first",
            "fallback_budget_guard_v2",
            "claim_adjudication_v1",
            "rollout_profile_v1",
        ],
        "rollout_mode": MSB_ROLLOUT_MODE,
        "pid": os.getpid(),
    }


@app.get("/api/mcp/files")
def mcp_list_files():
    """List all files with their types and topics for the MCP server."""
    tbl = get_table()
    try:
        docs = tbl.search().limit(10000).to_list()
    except Exception as e:
        return {"error": f"Failed to list files: {str(e)}"}

    files = {}
    for d in docs:
        fname = d["source_file"]
        meta = json.loads(d["metadata"]) if isinstance(d.get("metadata"), str) else d.get("metadata", {})
        if fname not in files:
            files[fname] = {
                "name": fname,
                "display_name": _display_name_for_source(fname, meta),
                "type": d["source_type"],
                "topics": set(),
            }
        for t in _sanitize_topics((meta or {}).get("topics", [])):
            files[fname]["topics"].add(_topic_display(t))

    result = []
    for f in sorted(files.values(), key=lambda x: x["name"]):
        result.append({"name": f["name"], "display_name": f["display_name"], "type": f["type"], "topics": sorted(list(f["topics"]))})

    return {"files": result}


@app.get("/api/mcp/file/{filename:path}")
def mcp_get_file(filename: str):
    """Return full reassembled content of a single file for the MCP server."""
    filename = urllib.parse.unquote(filename)
    tbl = get_table()
    try:
        all_rows = tbl.search().limit(20000).to_list()
    except Exception as e:
        return {"error": f"Database query failed: {str(e)}"}

    file_rows = sorted(
        [r for r in all_rows if r["source_file"] == filename],
        key=lambda r: r.get("chunk_index") or 0
    )
    if not file_rows:
        return {"error": f"File '{filename}' not found in knowledge base."}

    first = file_rows[0]
    meta = json.loads(first["metadata"]) if isinstance(first.get("metadata"), str) else first.get("metadata", {})
    full_content = "\n\n".join(r["content"] for r in file_rows) if len(file_rows) > 1 else first["content"]
    topics = [_topic_display(t) for t in _sanitize_topics((meta or {}).get("topics", []))]

    response = {
        "name": filename,
        "display_name": _display_name_for_source(filename, meta),
        "source_type": first["source_type"],
        "content": full_content,
        "topics": topics,
        "upload_context": (meta or {}).get("upload_context", ""),
        "chunk_count": len(file_rows),
    }

    # For video files, include transcript + timestamp data so get_file_content can build clip links
    if first["source_type"] == "video":
        chunks = []
        for r in file_rows:
            r_meta = json.loads(r["metadata"]) if isinstance(r.get("metadata"), str) else r.get("metadata", {})
            tx = (r_meta or {}).get("transcript", "")
            ts_start = (r_meta or {}).get("timestamp_start")
            ts_end   = (r_meta or {}).get("timestamp_end")
            tx_abs   = (r_meta or {}).get("transcript_absolute", False)
            if tx:
                chunks.append({
                    "transcript":          tx,
                    "timestamp_start":     ts_start,
                    "timestamp_end":       ts_end,
                    "transcript_absolute": tx_abs,
                })
        if chunks:
            response["video_chunks"] = chunks

    return response


@app.get("/api/mcp/connections/{filename:path}")
def mcp_get_connections(filename: str):
    """Return topic clusters and semantic neighbours for a file (for the MCP server)."""
    filename = urllib.parse.unquote(filename)
    tbl = get_table()
    try:
        all_rows = tbl.search().limit(10000).to_list()
    except Exception as e:
        return {"error": f"Database query failed: {str(e)}"}

    file_rows = [r for r in all_rows if r["source_file"] == filename]
    if not file_rows:
        return {"error": f"File '{filename}' not found in knowledge base."}

    # Collect this file's topics and first available vector
    file_topics: set = set()
    file_vector = None
    for r in file_rows:
        meta = json.loads(r["metadata"]) if isinstance(r.get("metadata"), str) else r.get("metadata", {})
        for t in _sanitize_topics((meta or {}).get("topics", [])):
            file_topics.add(t)
        if file_vector is None and r.get("vector") is not None:
            file_vector = r["vector"]

    # Map each shared topic to other files that also have it
    topic_to_peers: dict = {t: set() for t in file_topics}
    other_file_vecs: dict = {}
    other_file_types: dict = {}
    other_file_display: dict = {}
    for r in all_rows:
        fname = r["source_file"]
        if fname == filename:
            continue
        meta = json.loads(r["metadata"]) if isinstance(r.get("metadata"), str) else r.get("metadata", {})
        for t in _sanitize_topics((meta or {}).get("topics", [])):
            if t in file_topics:
                topic_to_peers[t].add(fname)
        if fname not in other_file_vecs and r.get("vector") is not None:
            other_file_vecs[fname] = r["vector"]
            other_file_types[fname] = r["source_type"]
            other_file_display[fname] = _display_name_for_source(fname, meta)

    # Build topic_peers: display_name -> sorted list of peer filenames
    topic_peers = {}
    for t in file_topics:
        display = _topic_display(t)
        topic_peers[display] = sorted(list(topic_to_peers[t]))

    # Find semantically similar files via cosine similarity
    semantic_peers = []
    if file_vector is not None:
        fv = np.array(file_vector, dtype=float)
        fv_norm = np.linalg.norm(fv) or 1e-10
        for fname, vec in other_file_vecs.items():
            ov = np.array(vec, dtype=float)
            ov_norm = np.linalg.norm(ov) or 1e-10
            sim = float(np.dot(fv, ov) / (fv_norm * ov_norm))
            if sim >= 0.85:
                semantic_peers.append({
                    "name": fname,
                    "display_name": other_file_display.get(fname, fname),
                    "type": other_file_types.get(fname, "unknown"),
                    "confidence": max(0, min(100, int(sim * 100))),
                })
        semantic_peers.sort(key=lambda x: -x["confidence"])
        semantic_peers = semantic_peers[:10]

    return {
        "name": filename,
        "display_name": _display_name_for_source(filename, _parse_row_meta(file_rows[0])),
        "topics": [_topic_display(t) for t in sorted(file_topics)],
        "topic_peers": topic_peers,
        "semantic_peers": semantic_peers,
    }


@app.post("/api/mcp/keyword_search")
def mcp_keyword_search(req: MCPKeywordSearchRequest):
    """Full-text keyword search through all stored content. Finds exact word/phrase matches."""
    keyword_lower = req.keyword.lower().strip()
    if not keyword_lower:
        return {"error": "Keyword cannot be empty."}

    try:
        sidecar = _get_sidecar_index()
    except Exception as e:
        return {"error": f"Sidecar index build failed: {str(e)}"}

    rows_by_file = sidecar.get("rows_by_file", {}) or {}
    file_info = sidecar.get("file_info", {}) or {}
    token_to_files = sidecar.get("token_to_files", {}) or {}
    file_blob_lookup = sidecar.get("file_blob_lookup", {}) or {}

    candidate_scores: dict[str, float] = {}

    def _boost(fname: str, score: float):
        if not fname:
            return
        candidate_scores[fname] = candidate_scores.get(fname, 0.0) + score

    for token in _sidecar_terms(keyword_lower):
        for fname in token_to_files.get(token, set()):
            _boost(fname, 0.5)

    phrase_hits = 0
    for fname, blob in file_blob_lookup.items():
        if keyword_lower in blob:
            _boost(fname, 1.25)
            phrase_hits += 1
            if phrase_hits >= 180:
                break

    if not candidate_scores:
        return {
            "results": [],
            "total_files_matched": 0,
            "retrieval_meta": {
                "scan_mode": "sidecar_index",
                "rows_scanned": 0,
                "candidate_files": 0,
                "sidecar_files": len(file_info),
            },
        }

    max_candidates = max(_rollout_scale_int(KEYWORD_CANDIDATE_FILES_LIMIT, 6, 300), req.max_results * 4)
    ranked_candidates = [
        fname for fname, _ in sorted(candidate_scores.items(), key=lambda kv: (-kv[1], kv[0]))[:max_candidates]
    ]

    results = []
    seen_files = set()
    rows_scanned = 0

    for fname in ranked_candidates:
        file_rows = rows_by_file.get(fname, [])
        if not file_rows:
            continue

        file_occurrences = 0
        snippets: list[str] = []
        matched_terms: set[str] = set()
        upload_ctx = str(file_info.get(fname, {}).get("upload_context", "") or "")

        for row in file_rows:
            rows_scanned += 1
            content = str(row.get("content", "") or "")
            content_lower = content.lower()
            meta = row.get("metadata", {}) or {}
            row_upload_ctx = str(meta.get("upload_context", "") or upload_ctx)
            searchable = f"{content} {row_upload_ctx}".lower()

            if keyword_lower not in searchable:
                continue

            file_occurrences += max(1, content_lower.count(keyword_lower))
            matched_terms.add(keyword_lower)

            if keyword_lower in content_lower:
                search_from = 0
                local_hits = 0
                while local_hits < 2:
                    idx = content_lower.find(keyword_lower, search_from)
                    if idx < 0:
                        break
                    s = max(0, idx - 320)
                    e = min(len(content), idx + 320)
                    snippet = ("..." if s > 0 else "") + content[s:e] + ("..." if e < len(content) else "")
                    snippets.append(_compact_snippet(snippet, max_chars=420))
                    search_from = idx + len(keyword_lower)
                    local_hits += 1
            elif row_upload_ctx and keyword_lower in row_upload_ctx.lower() and not snippets:
                snippets.append(f"[From upload label]: {row_upload_ctx}")

            if len(snippets) >= 5:
                break

        if file_occurrences <= 0:
            continue

        info = file_info.get(fname, {})
        source_type = info.get("source_type")
        if not source_type and file_rows:
            source_type = file_rows[0].get("source_type", "unknown")

        results.append(
            {
                "source_file": fname,
                "display_name": info.get("display_name", fname),
                "source_type": source_type or "unknown",
                "snippet": "\n---\n".join(snippets[:4]),
                "occurrences": file_occurrences,
                "topics": info.get("topics", []),
                "upload_context": upload_ctx,
                "matched_keywords": sorted(list(matched_terms)),
            }
        )
        seen_files.add(fname)
        if len(results) >= req.max_results:
            break

    return {
        "results": results,
        "total_files_matched": len(seen_files),
        "retrieval_meta": {
            "scan_mode": "sidecar_index",
            "rows_scanned": rows_scanned,
            "candidate_files": len(ranked_candidates),
            "sidecar_files": len(file_info),
            "rollout_mode": MSB_ROLLOUT_MODE,
        },
    }


def _extract_holistic_keywords(query: str) -> list[str]:
    kw_set: set[str] = set()
    for m in re.findall(r'\$\d+(?:\.\d+)?', query):
        kw_set.add(m.lower())
        kw_set.add(m.lstrip("$").lower())
    for m in re.findall(r'(?<!\$)\b\d+(?:\.\d+)?\b', query):
        kw_set.add(m.lower())
    for m in re.findall(r'\b[A-Z][a-z]+\b', query):
        kw_set.add(m.lower())
    for m in re.findall(r"\b(\w+)(?:'s|'s)\b", query, re.I):
        if len(m) >= 2:
            kw_set.add(m.lower())
    for m in re.findall(r'\b(\w{2,})s\b', query, re.I):
        bare = m.lower()
        if bare not in {'this', 'does', 'was', 'has', 'his', 'its'}:
            kw_set.add(bare)
    stop_words = {
        'what', 'where', 'when', 'which', 'their', 'there', 'about', 'tell', 'show', 'find',
        'give', 'from', 'with', 'this', 'that', 'have', 'does', 'were', 'been', 'they', 'them',
        'know', 'like', 'make', 'your', 'second', 'brain', 'check', 'please', 'search', 'query',
        'looking', 'need', 'want', 'are', 'and', 'the', 'for', 'not', 'but', 'can', 'how',
        'also', 'just', 'all', 'any', 'get', 'got', 'let', 'may', 'our', 'own', 'say', 'she',
        'too', 'try', 'use', 'who', 'why', 'yet', 'its', 'had', 'has', 'his', 'her', 'him',
    }
    for w in query.lower().split():
        w = w.strip('?.,!"\':;()[] ')
        if len(w) >= 3 and w not in stop_words:
            kw_set.add(w)
    return sorted(list(kw_set))[:14]


def _extract_numeric_tokens(query: str) -> list[str]:
    tokens = re.findall(r'\$\d+(?:\.\d+)?|(?<!\$)\b\d+(?:\.\d+)?\b', query)
    return list(dict.fromkeys([t.lower() for t in tokens]))[:10]


def _extract_name_targets(query: str) -> list[str]:
    candidates: list[str] = []
    stop_words = {
        "what", "who", "when", "where", "why", "how", "which", "and", "or", "the", "a", "an", "is", "are", "was", "were",
        "summarize", "summary", "compare", "list", "show", "tell", "find", "give", "provide", "explain", "discuss", "discussed",
        "across", "files", "file", "with", "from", "about", "any", "uncertainty", "pricing", "price", "rate", "rates",
    }

    def _name_variants(token: str) -> list[str]:
        t = str(token or "").strip().lower()
        if not t:
            return []
        # Canonicalize likely possessive/singularized forms (e.g., rubys -> ruby, dans -> dan).
        if t.endswith("ies") and len(t) > 4:
            return [t[:-3] + "y"]
        if t.endswith("s") and len(t) > 3 and not t.endswith(("ss", "us", "is", "es")):
            return [t[:-1]]
        variants: list[str] = [t]
        seen_local: set[str] = set()
        result: list[str] = []
        for v in variants:
            if v and v not in seen_local:
                seen_local.add(v)
                result.append(v)
        return result

    for m in re.findall(r"\b([A-Z][a-z]{1,})('s)?\b", query):
        token = m[0].lower()
        for variant in _name_variants(token):
            if len(variant) >= 2 and variant not in stop_words:
                candidates.append(variant)
    for m in re.findall(r"\b(\w+)(?:'s|'s)\b", query, re.I):
        token = m.lower()
        for variant in _name_variants(token):
            if len(variant) >= 2 and variant not in stop_words:
                candidates.append(variant)
    return list(dict.fromkeys(candidates))[:10]


def _canonical_numeric_value(raw_value: str) -> str:
    value = (raw_value or "").strip().lower()
    if not value:
        return ""
    amount_match = re.search(r'\$?\d+(?:\.\d+)?', value)
    if not amount_match:
        return re.sub(r'\s+', ' ', value)
    amount = amount_match.group(0)
    if not amount.startswith("$"):
        amount = f"${amount}"

    if re.search(r'an hour|per hour|/hr|/hour', value):
        unit = "/hour"
    elif re.search(r'per month|a month|monthly', value):
        unit = "/month"
    elif re.search(r'per year|a year|yearly|annually', value):
        unit = "/year"
    elif re.search(r'percent|per cent|%', value):
        unit = "%"
    else:
        unit = ""
    return f"{amount}{unit}"


def _normalize_person_token(value: str) -> str:
    return re.sub(r'[^a-z0-9 ]+', ' ', (value or '').lower()).strip()


def _extract_speaker_label(snippet: str) -> str:
    text = " ".join((snippet or "").replace("...", " ").split())
    if not text:
        return ""

    patterns = [
        r'\[\d+:\d{2}(?:=\d+s)?\]\s*([A-Za-z][A-Za-z\'\-.]*(?:\s+[A-Za-z][A-Za-z\'\-.]*){0,3})\s*:',
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})(?:\s*\([^)]{0,40}\))?(?:\s+\|\s*[^0-9:]{1,40})?\s+\d{1,2}:\d{2}\b',
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\s*:\s',
    ]
    noise_tokens = {
        "speaker", "summary", "keywords", "project", "launchpad", "rockit", "file", "transcript"
    }
    for pattern in patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        candidate = " ".join((m.group(1) or "").split()).strip(" -:|")
        normalized = _normalize_person_token(candidate)
        if not normalized:
            continue
        first = normalized.split()[0]
        if first in noise_tokens or normalized.startswith("speaker"):
            continue
        return candidate
    return ""


def _speaker_attribution(snippet: str, subject: str) -> tuple[str, str]:
    speaker = _extract_speaker_label(snippet)
    subject_norm = _normalize_person_token(subject)
    speaker_norm = _normalize_person_token(speaker)
    snippet_norm = _normalize_person_token(snippet)
    subject_head = subject_norm.split()[0] if subject_norm else ""

    if speaker_norm:
        speaker_head = speaker_norm.split()[0]
        if subject_head and (subject_head == speaker_head or subject_norm == speaker_norm):
            return ("direct_speaker", speaker)
        if subject_head and re.search(rf'\b{re.escape(subject_head)}\b', snippet_norm):
            return ("mentioned_other_speaker", speaker)
        return ("other_speaker", speaker)

    if subject_head and re.search(rf'\b{re.escape(subject_head)}\b', snippet_norm):
        return ("mentioned_no_speaker", "")
    return ("unknown", "")


def _claim_uncertainty_message(status: str, subject: str, observed_values: list[str]) -> str:
    if status == "conflicting":
        return (
            f"Conflicting evidence for {subject}: observed {', '.join(observed_values[:4])}. "
            "Do not assert a single definitive value without qualification."
        )
    if status == "weak_support":
        return (
            f"Evidence for {subject} is weak/indirect (speaker attribution is not direct). "
            "Answer with explicit uncertainty."
        )
    if status == "insufficient_evidence":
        return (
            f"Insufficient evidence for {subject} in selected passages. "
            "Do not guess."
        )
    return ""


def _build_claim_adjudication(claims: list[dict]) -> dict:
    supported = []
    conflicting = []
    weak_support = []
    insufficient = []

    for claim in claims:
        row = {
            "subject": claim.get("subject", ""),
            "claim_type": claim.get("claim_type", ""),
            "status": claim.get("status", ""),
            "recommended_value": claim.get("recommended_value", ""),
            "observed_values": claim.get("observed_values", []),
            "evidence_count": int(claim.get("evidence_count", 0) or 0),
            "direct_evidence_count": int(claim.get("direct_evidence_count", 0) or 0),
            "uncertainty": claim.get("uncertainty", ""),
        }
        status = row["status"]
        if status == "supported":
            supported.append(row)
        elif status == "conflicting":
            conflicting.append(row)
        elif status == "weak_support":
            weak_support.append(row)
        else:
            insufficient.append(row)

    counts = {
        "supported": len(supported),
        "conflicting": len(conflicting),
        "weak_support": len(weak_support),
        "insufficient_evidence": len(insufficient),
        "total": len(claims),
    }
    policy = {
        "requires_uncertainty": bool(conflicting or weak_support or insufficient),
        "can_answer_directly": bool(supported) and not bool(conflicting),
        "must_not_guess": bool(conflicting or insufficient),
    }
    return {
        "supported": supported[:10],
        "conflicting": conflicting[:10],
        "weak_support": weak_support[:10],
        "insufficient_evidence": insufficient[:10],
        "counts": counts,
        "policy": policy,
    }


def _requires_claim_validation(query: str, intent: dict) -> bool:
    q = (query or "").lower()
    markers = [
        "rate", "rates", "price", "cost", "dollar", "$", "how much", "hour", "hourly", "per hour",
        "percent", "%", "date", "when", "who", "salary", "budget", "pricing", "fee", "fees", "compensation",
    ]
    return bool(intent.get("is_factual")) or bool(_extract_numeric_tokens(query)) or any(m in q for m in markers)


def _collect_claim_candidate_rows(
    query: str,
    candidate_files: list[str],
    rows_by_file: dict[str, list[dict]],
    query_names: list[str],
    numeric_tokens: list[str],
    max_rows: int = 360,
) -> list[dict]:
    q_lower = (query or "").lower()
    rate_term_re = re.compile(r"\brate|rates|hour|hourly|per hour|/hr|salary|compensation|fee|fees|pricing|price|cost\b", re.I)
    currency_re = re.compile(r"\$\d+(?:\.\d+)?")
    name_patterns = [re.compile(rf"\b{re.escape(name.lower())}(?:'s)?\b", re.I) for name in query_names if name]

    scored_rows: list[tuple[int, int, dict]] = []
    seen_row_keys: set[tuple[str, int, str]] = set()

    file_order = {fname: idx for idx, fname in enumerate(candidate_files)}

    for fname in candidate_files:
        file_rows = rows_by_file.get(fname, [])
        if not file_rows:
            continue
        for row in file_rows[:40]:
            content = str(row.get("content", "") or "")
            if not content:
                continue
            meta = row.get("metadata", {}) or {}
            upload_ctx = str(meta.get("upload_context", "") or "")
            searchable = f"{content} {upload_ctx}".lower()

            name_hit = any(p.search(searchable) for p in name_patterns) if name_patterns else False
            numeric_hit = False
            if numeric_tokens:
                for tok in numeric_tokens:
                    bare = tok.lstrip("$")
                    if tok in searchable or (bare and bare in searchable):
                        numeric_hit = True
                        break

            has_rate_term = bool(rate_term_re.search(searchable))
            has_currency = bool(currency_re.search(searchable))

            score = 0
            if name_hit:
                score += 3
            if numeric_hit:
                score += 2
            if has_rate_term:
                score += 2
            if has_currency:
                score += 1

            if name_hit and (numeric_hit or has_currency or has_rate_term):
                score += 2
            if not score and query_names:
                continue
            if not score and ("$" in q_lower or has_rate_term):
                continue

            row_key = (
                fname,
                int(row.get("chunk_index", -1) or -1),
                content[:80],
            )
            if row_key in seen_row_keys:
                continue
            seen_row_keys.add(row_key)
            scored_rows.append((score, file_order.get(fname, 10_000), row))

    if scored_rows:
        scored_rows.sort(key=lambda item: (-item[0], item[1]))
        return [row for _, _, row in scored_rows[:max_rows]]

    fallback_rows: list[dict] = []
    for fname in candidate_files:
        for row in rows_by_file.get(fname, [])[:14]:
            fallback_rows.append(row)
            if len(fallback_rows) >= max_rows:
                return fallback_rows
    return fallback_rows


def _snippet_around(content: str, start_idx: int, radius: int = 240) -> str:
    s = max(0, start_idx - radius)
    e = min(len(content), start_idx + radius)
    prefix = "..." if s > 0 else ""
    suffix = "..." if e < len(content) else ""
    return (prefix + content[s:e] + suffix).strip()


def _build_claim_verification(query: str, rows: list[dict], confidence_map: dict[str, int]) -> list[dict]:
    claims: list[dict] = []
    names = _extract_name_targets(query)
    numeric_tokens = _extract_numeric_tokens(query)
    q_lower = (query or "").lower()
    is_rate_query = bool(re.search(r'\brate|rates|hour|hourly|per hour|/hr|salary|compensation|fee|fees|pricing|price|cost\b', q_lower))

    value_pattern_general = re.compile(
        r'\$\d+(?:\.\d+)?|\b\d+(?:\.\d+)?\s*(?:an hour|per hour|/hr|/hour|hourly|percent|%)\b',
        re.I,
    )
    value_pattern_rate = re.compile(
        r'\$\d+(?:\.\d+)?\s*(?:an hour|per hour|/hr|/hour|hourly)\b'
        r'|\b\d+(?:\.\d+)?\s*(?:an hour|per hour|/hr|/hour|hourly)\b'
        r'|\brate\s*(?:is|was|at|of|=|:)?\s*\$?\d+(?:\.\d+)?\b'
        r'|\$\d+(?:\.\d+)?\s*(?=\s*(?:rate|rates)\b)',
        re.I,
    )
    rate_term_re = re.compile(r'\brate|rates|hour|hourly|per hour|/hr|salary|compensation|fee|fees|pricing|price|cost\b', re.I)
    hour_term_re = re.compile(r'an hour|per hour|/hr|/hour|hourly', re.I)

    def _numeric_amount(raw_value: str) -> float | None:
        m = re.search(r'\d+(?:\.\d+)?', raw_value or "")
        if not m:
            return None
        try:
            return float(m.group(0))
        except Exception:
            return None

    def _value_priority(raw_value: str) -> tuple[int, int, int, str]:
        lowered = (raw_value or "").lower()
        return (
            0 if "$" in lowered else 1,
            0 if hour_term_re.search(lowered) else 1,
            len(lowered),
            lowered,
        )

    def _is_rate_relevant_match(raw_value: str, text_window: str) -> bool:
        amount = _numeric_amount(raw_value)
        if amount is not None and amount <= 0:
            return False

        if not is_rate_query:
            return True

        raw_lower = (raw_value or "").lower()
        win_lower = (text_window or "").lower()
        has_hour = bool(hour_term_re.search(raw_lower) or hour_term_re.search(win_lower))
        has_rate = bool(rate_term_re.search(win_lower))
        has_currency = "$" in raw_lower

        if has_hour:
            return True
        if has_currency and has_rate:
            return True
        if amount is not None and amount < 10:
            return False
        return False

    def _extract_values(snippet: str) -> list[str]:
        pattern = value_pattern_rate if is_rate_query else value_pattern_general
        values: list[str] = []
        for m in pattern.finditer(snippet or ""):
            raw = m.group(0).strip()
            s = max(0, m.start() - 56)
            e = min(len(snippet), m.end() + 56)
            window = (snippet[s:e] or "")
            if not _is_rate_relevant_match(raw, window):
                continue
            values.append(raw)
        if values:
            deduped = list(dict.fromkeys(values))
            deduped.sort(key=_value_priority)
            return deduped
        if is_rate_query:
            # Salvage path: allow bare currency only when local context contains clear rate terms.
            fallback = []
            for m in re.finditer(r'\$\d+(?:\.\d+)?', snippet or "", re.I):
                s = max(0, m.start() - 48)
                e = min(len(snippet), m.end() + 48)
                window = (snippet[s:e] or "").lower()
                raw = m.group(0).strip()
                if _is_rate_relevant_match(raw, window):
                    fallback.append(raw)
            deduped = list(dict.fromkeys(fallback))
            deduped.sort(key=_value_priority)
            return deduped
        return []

    for name in names:
        name_pat = re.compile(rf"\b{re.escape(name)}(?:'s)?\b", re.I)
        evidence: list[dict] = []
        normalized_to_raw: dict[str, set[str]] = {}
        for row in rows:
            content = row.get("content", "") or ""
            if not content:
                continue
            for m in name_pat.finditer(content):
                snippet = _snippet_around(content, m.start())
                focus_start = max(0, m.start() - 100)
                focus_end = min(len(content), m.end() + 200)
                focused = content[focus_start:focus_end]
                values = _extract_values(focused)
                if not values:
                    values = _extract_values(snippet)
                if not values:
                    continue
                attribution, speaker_label = _speaker_attribution(snippet, name)
                for raw_value in values:
                    normalized = _canonical_numeric_value(raw_value)
                    if not normalized:
                        continue
                    normalized_to_raw.setdefault(normalized, set()).add(raw_value.strip())
                    evidence.append({
                        "source_file": row.get("source_file", ""),
                        "chunk_index": row.get("chunk_index", -1),
                        "confidence": confidence_map.get(row.get("source_file", ""), 0),
                        "quote": snippet,
                        "value": raw_value.strip(),
                        "value_normalized": normalized,
                        "speaker_label": speaker_label,
                        "attribution": attribution,
                    })
                    if len(evidence) >= 8:
                        break
                if len(evidence) >= 8:
                    break
            if len(evidence) >= 8:
                break

        observed_values = [
            sorted(raws, key=_value_priority)[0]
            for raws in normalized_to_raw.values()
            if raws
        ]
        observed_values = sorted(list(dict.fromkeys(observed_values)), key=_value_priority)
        direct_values = {
            ev.get("value_normalized", "")
            for ev in evidence
            if ev.get("attribution") == "direct_speaker" and ev.get("value_normalized")
        }
        status = "insufficient_evidence"
        recommended_value = ""

        if observed_values:
            if len(direct_values) > 1:
                status = "conflicting"
            elif len(direct_values) == 1:
                status = "supported"
                direct_key = next(iter(direct_values))
                direct_raws = sorted(list(normalized_to_raw.get(direct_key, [])), key=_value_priority)
                if direct_raws:
                    recommended_value = direct_raws[0]
            elif len(observed_values) > 1:
                status = "conflicting"
            else:
                status = "weak_support"
                recommended_value = observed_values[0]

        uncertainty = _claim_uncertainty_message(status, name, observed_values)
        direct_count = sum(1 for ev in evidence if ev.get("attribution") == "direct_speaker")
        weak_count = sum(1 for ev in evidence if ev.get("attribution") in {"mentioned_other_speaker", "mentioned_no_speaker", "other_speaker"})

        claims.append({
            "subject": name,
            "claim_type": "numeric_fact",
            "status": status,
            "observed_values": observed_values,
            "recommended_value": recommended_value,
            "uncertainty": uncertainty,
            "evidence_count": len(evidence),
            "direct_evidence_count": direct_count,
            "weak_evidence_count": weak_count,
            "evidence": evidence[:6],
        })

    for token in numeric_tokens:
        matched: list[dict] = []
        observed_norm: set[str] = set()
        for row in rows:
            content = row.get("content", "") or ""
            if not content:
                continue
            idx = content.lower().find(token)
            if idx < 0 and token.startswith("$"):
                idx = content.lower().find(token.lstrip("$"))
            if idx < 0:
                continue
            quote = _snippet_around(content, idx)
            value_guess = re.search(r'\$?\d+(?:\.\d+)?', quote)
            normalized = _canonical_numeric_value(value_guess.group(0) if value_guess else token)
            observed_norm.add(normalized or token)
            matched.append({
                "source_file": row.get("source_file", ""),
                "chunk_index": row.get("chunk_index", -1),
                "confidence": confidence_map.get(row.get("source_file", ""), 0),
                "quote": quote,
                "value": value_guess.group(0) if value_guess else token,
                "value_normalized": normalized,
                "speaker_label": _extract_speaker_label(quote),
                "attribution": "token_match",
            })
            if len(matched) >= 4:
                break

        if not matched:
            status = "insufficient_evidence"
        elif len(observed_norm) > 1:
            status = "conflicting"
        else:
            status = "supported"

        observed_values = sorted([ev.get("value", token) for ev in matched if ev.get("value")])
        uncertainty = _claim_uncertainty_message(status, token, observed_values)
        claims.append({
            "subject": token,
            "claim_type": "explicit_numeric_token",
            "status": status,
            "observed_values": observed_values,
            "recommended_value": observed_values[0] if status == "supported" and observed_values else "",
            "uncertainty": uncertainty,
            "evidence_count": len(matched),
            "direct_evidence_count": 0,
            "weak_evidence_count": len(matched),
            "evidence": matched,
        })

    return claims[:14]


def _compact_snippet(text: str, max_chars: int = 360) -> str:
    compact = " ".join((text or "").split()).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars].rstrip() + "..."


def _append_unique_snippet(snippets: list[str], snippet: str, max_items: int = 4, max_chars: int = 360):
    value = _compact_snippet(snippet, max_chars=max_chars)
    if not value:
        return
    sig = value[:120]
    for existing in snippets:
        if sig and (sig in existing or existing[:120] in value):
            return
    snippets.append(value)
    if len(snippets) > max_items:
        del snippets[max_items:]


def _build_focus_passages(text: str, query_terms: list[str], max_passages: int = 4, radius: int = 220, max_chars: int = 1800) -> str:
    raw = text or ""
    if not raw:
        return ""

    lowered = raw.lower()
    snippets: list[str] = []

    for term in query_terms[:16]:
        term_l = (term or "").lower().strip()
        if not term_l:
            continue
        idx = lowered.find(term_l)
        if idx < 0:
            continue
        start = max(0, idx - radius)
        end = min(len(raw), idx + radius)
        fragment = ("..." if start > 0 else "") + raw[start:end] + ("..." if end < len(raw) else "")
        _append_unique_snippet(snippets, fragment, max_items=max_passages, max_chars=min(420, radius * 2))
        if len(snippets) >= max_passages:
            break

    if not snippets:
        _append_unique_snippet(snippets, raw[:max_chars], max_items=1, max_chars=max_chars)

    joined = "\n---\n".join(snippets)
    if len(joined) <= max_chars:
        return joined
    return joined[:max_chars].rstrip() + "..."


def _sanitize_query_for_retrieval(query: str) -> str:
    """
    Clean copied chat transcripts/tool chatter from user queries while preserving intent.
    """
    raw = str(query or "").strip()
    if not raw:
        return ""

    cleaned = raw.replace("\r", "\n")
    # Remove timestamps like 9:53 PM that are often pasted with chat logs.
    cleaned = re.sub(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", " ", cleaned, flags=re.I)

    # Trim known assistant/tool chatter tails if present in pasted transcript text.
    chatter_cut = re.search(
        r"(?i)\b(i(?:'|’)ll search your second brain|searched available tools|let me try using|let me load the my-second-brain tools|holistic search|keyword search|search brain|get topics)\b",
        cleaned,
    )
    if chatter_cut:
        cleaned = cleaned[:chatter_cut.start()]

    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!?:;-")
    if len(cleaned) >= 12:
        return cleaned
    return raw


def _extract_process_terms(query: str) -> list[str]:
    stop_words = {
        "what", "where", "when", "which", "their", "there", "about", "tell", "show", "find",
        "give", "from", "with", "this", "that", "have", "does", "were", "been", "they", "them",
        "know", "like", "make", "your", "second", "brain", "check", "please", "search", "query",
        "looking", "need", "want", "are", "and", "the", "for", "not", "but", "can", "how",
        "also", "just", "all", "any", "get", "got", "let", "may", "our", "own", "say", "she",
        "too", "try", "use", "who", "why", "yet", "its", "had", "has", "his", "her", "him",
        "process", "workflow", "workflows", "steps", "step", "sequence", "flow", "lifecycle", "pipeline",
        "client", "across", "files", "material", "corpus", "people", "said",
    }

    expansions = {
        "payment": ["payment", "payments", "invoice", "invoicing", "billing", "remittance", "milestone"],
        "pay": ["pay", "paid", "payment", "payments", "compensation", "invoice", "billing"],
        "paying": ["pay", "paying", "paid", "payment", "payments", "compensation", "invoice"],
        "invoice": ["invoice", "invoicing", "billing", "bill", "payment"],
        "proposal": ["proposal", "quote", "scope", "estimate"],
        "approval": ["approval", "approve", "signoff", "sign-off"],
        "onboarding": ["onboarding", "onboard", "kickoff", "intake"],
        "handoff": ["handoff", "handover", "transition", "route", "assign"],
        "monthly": ["monthly", "month", "cadence", "cycle"],
        "collective": ["collective", "youth collective", "members", "member", "cohort"],
        "member": ["member", "members", "collective", "participant", "young people"],
        "members": ["member", "members", "collective", "participant", "young people"],
    }

    priority_terms = {
        "pay", "paid", "paying", "payment", "payments", "billing", "invoice", "invoicing",
        "collective", "member", "members", "youth", "month", "monthly", "cadence", "cycle",
        "process", "workflow", "steps", "timesheet", "compensation",
    }

    tokens: list[str] = []
    for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_\-/]+", (query or "").lower()):
        normalized = token.strip("-_/")
        if len(normalized) < 3 or normalized in stop_words:
            continue
        tokens.append(normalized)

    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        singular = token[:-1] if token.endswith("s") and len(token) > 4 else token
        if singular != token:
            expanded.append(singular)
        for extra in expansions.get(token, []):
            expanded.append(extra)
        for extra in expansions.get(singular, []):
            expanded.append(extra)

    unique_terms = list(dict.fromkeys(expanded))
    unique_terms.sort(key=lambda t: (0 if t in priority_terms else 1, len(t)))
    return unique_terms[:32]


def _process_stage_label(text: str) -> str:
    lowered = (text or "").lower()
    if re.search(r"\b(start|begin|intake|receive|input|capture|submit|request|onboard)\b", lowered):
        return "entry"
    if re.search(r"\b(month|monthly|week|weekly|fortnight|schedule|cadence|cycle|throughout the month|each month)\b", lowered):
        return "cadence"
    if re.search(r"\b(validate|review|check|verify|audit|screen)\b", lowered):
        return "validation"
    if re.search(r"\b(decide|decision|approve|reject|gate|criteria)\b", lowered):
        return "decision"
    if re.search(r"\b(next|then|after|handoff|handover|route|queue|assign|escalate)\b", lowered):
        return "transition"
    if re.search(r"\b(output|deliver|send|store|publish|complete|done|close|result)\b", lowered):
        return "outcome"
    return "process_detail"


def _build_process_steps(
    query: str,
    selected_ranked: list[dict],
    rows_by_file: dict[str, list[dict]],
    rank_score_map: dict[str, float],
    max_files: int = 5,
    max_steps: int = 14,
) -> list[dict]:
    process_terms = _extract_process_terms(query)
    if not process_terms:
        process_terms = [w.strip().lower() for w in query.split() if len(w.strip()) >= 4][:10]

    selected_docs = [rec for rec in selected_ranked if rec.get("source_type") != "video"][:max_files]
    steps: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    stage_order = {
        "entry": 0,
        "cadence": 1,
        "validation": 2,
        "decision": 3,
        "transition": 4,
        "outcome": 5,
        "process_detail": 6,
    }

    per_file_step_cap = max(1, min(4, int(os.getenv("MCP_HOLISTIC_PROCESS_STEPS_PER_FILE", "3"))))

    for rec in selected_docs:
        fname = rec.get("source_file", "")
        if not fname:
            continue
        display_name = rec.get("display_name") or fname
        file_rows = sorted(rows_by_file.get(fname, []), key=lambda r: r.get("chunk_index") or 0)
        if not file_rows:
            continue

        file_steps = 0

        for row in file_rows[:30]:
            content = str(row.get("content", "") or "")
            if not content:
                continue

            meta = row.get("metadata", {}) or {}
            searchable = f"{content} {meta.get('upload_context', '')}".lower()
            if process_terms and not any(term in searchable for term in process_terms):
                continue

            chunk_index = int(row.get("chunk_index", -1) or -1)
            row_key = (fname, chunk_index, content[:80])
            if row_key in seen:
                continue
            seen.add(row_key)

            snippet = _build_focus_passages(
                content,
                process_terms,
                max_passages=1,
                radius=210,
                max_chars=360,
            )
            if not snippet:
                snippet = _compact_snippet(content, max_chars=360)
            if not snippet:
                continue

            score = float(rank_score_map.get(fname, rec.get("score", 0.0)) or 0.0)
            confidence = max(30, min(96, int(28 + score * 74)))

            steps.append(
                {
                    "source_file": fname,
                    "display_name": display_name,
                    "chunk_index": chunk_index,
                    "stage": _process_stage_label(snippet),
                    "confidence": confidence,
                    "snippet": _compact_snippet(snippet, max_chars=360),
                }
            )
            file_steps += 1
            if file_steps >= per_file_step_cap:
                break
            if len(steps) >= max_steps:
                break

    if not steps:
        return []

    deduped: list[dict] = []
    seen_file_stage: set[tuple[str, str]] = set()
    seen_file_any: set[str] = set()

    ranked_candidates = sorted(
        steps,
        key=lambda s: (
            stage_order.get(str(s.get("stage", "process_detail")), 6),
            -int(s.get("confidence", 0) or 0),
            int(s.get("chunk_index", -1) or -1),
            str(s.get("source_file", "")),
        ),
    )

    # Ensure broad process coverage by retaining at least one step per selected file first.
    for rec in selected_docs:
        fname = str(rec.get("source_file", "") or "")
        if not fname:
            continue
        file_steps = [s for s in ranked_candidates if s.get("source_file") == fname]
        if file_steps:
            first = file_steps[0]
            deduped.append(first)
            seen_file_any.add(fname)
            seen_file_stage.add((fname, str(first.get("stage", "process_detail"))))
        if len(deduped) >= max_steps:
            return deduped[:max_steps]

    for step in ranked_candidates:
        fname = str(step.get("source_file", "") or "")
        stage = str(step.get("stage", "process_detail") or "process_detail")
        key = (fname, stage)
        if key in seen_file_stage:
            continue
        seen_file_stage.add(key)
        deduped.append(step)
        if len(deduped) >= max_steps:
            break

    deduped.sort(
        key=lambda s: (
            stage_order.get(str(s.get("stage", "process_detail")), 6),
            str(s.get("source_file", "")),
            int(s.get("chunk_index", -1) or -1),
            -int(s.get("confidence", 0) or 0),
        )
    )
    return deduped[:max_steps]


def _query_intent_profile(query: str) -> dict:
    q = (query or "").lower()
    broad_markers = [
        "summarize", "summary", "overview", "across", "all", "synthesize", "synthesis",
        "unbiased", "multifaceted", "compare", "contrast", "themes", "overall", "big picture",
        "what did people say", "what does the material", "what does the corpus", "major insights",
        "main insights", "common", "biggest unknowns", "open questions", "show evidence about",
        "give me a broad", "broader", "provide a multifaceted", "opportunities vs risks",
    ]
    factual_markers = [
        "what is", "what are", "how much", "rate", "price", "cost", "date", "when", "who",
        "which files mention", "where are references", "find explicit", "show evidence", "mention",
    ]
    process_markers = [
        "process", "workflow", "steps", "step-by-step", "step by step", "how does", "how do",
        "what happens", "sequence", "lifecycle", "pipeline", "handoff", "onboarding", "intake",
        "from start to finish", "end to end",
    ]
    wants_comparison = any(w in q for w in ["compare", "contrast", "versus", "vs", "difference"])
    is_broad = any(marker in q for marker in broad_markers)
    is_factual = (not is_broad) and any(marker in q for marker in factual_markers)
    has_numeric_focus = bool(_extract_numeric_tokens(query)) or bool(re.search(r"\brate|rates|price|cost|dollar|\$|hourly|per hour\b", q))
    is_process = any(marker in q for marker in process_markers) and not has_numeric_focus

    if is_broad:
        weights = {"semantic": 0.48, "keyword": 0.24, "topic": 0.20, "name": 0.08}
        min_files = 5
        label = "broad"
    elif is_factual:
        weights = {"semantic": 0.62, "keyword": 0.28, "topic": 0.04, "name": 0.06}
        min_files = 3
        label = "factual"
    else:
        weights = {"semantic": 0.55, "keyword": 0.25, "topic": 0.12, "name": 0.08}
        min_files = 4
        label = "hybrid"

    if is_process and not is_broad:
        weights["semantic"] = max(0.40, weights["semantic"] - 0.05)
        weights["keyword"] = min(0.34, weights["keyword"] + 0.03)
        weights["topic"] = min(0.30, weights["topic"] + 0.05)
        min_files = max(min_files, 5 if not is_factual else 4)
        if not is_factual:
            label = "process"

    if wants_comparison:
        weights["topic"] = min(0.28, weights["topic"] + 0.06)
        weights["semantic"] = max(0.40, weights["semantic"] - 0.03)

    return {
        "label": label,
        "is_broad": is_broad,
        "is_factual": is_factual,
        "is_process": is_process,
        "wants_comparison": wants_comparison,
        "weights": weights,
        "min_files": min_files,
    }


@app.post("/api/mcp/holistic_search")
def mcp_holistic_search(req: MCPHolisticRequest):
    """
    Single-call multimodal search that combines semantic vector search, keyword exact
    matching, full file content retrieval, and topic graph connections — all in one
    server-side pass. Replaces the need for Claude to make 3-4 sequential tool calls.
    """
    if not gemini:
        return {"error": "Gemini API Key missing."}
    query_raw = req.query.strip()
    query = _sanitize_query_for_retrieval(query_raw)
    if not query:
        return {"error": "query required"}
    intent = _query_intent_profile(query)

    tbl = get_table()
    semantic_limit = _rollout_scale_int(
        max(8, min(40, int(os.getenv("MCP_HOLISTIC_SEMANTIC_LIMIT", "24")))),
        8,
        40,
        factor=ROLLOUT_RESPONSE_DEPTH_FACTOR,
    )
    semantic_chars = _rollout_scale_int(
        max(300, min(1600, int(os.getenv("MCP_HOLISTIC_SEMANTIC_CHARS", "900")))),
        300,
        1600,
        factor=ROLLOUT_RESPONSE_DEPTH_FACTOR,
    )
    full_file_limit = max(0, min(4, int(os.getenv("MCP_HOLISTIC_FULL_FILE_LIMIT", "2"))))
    full_excerpt_chars = _rollout_scale_int(
        max(1200, min(8000, int(os.getenv("MCP_HOLISTIC_FULL_EXCERPT_CHARS", "4200")))),
        1200,
        8000,
        factor=ROLLOUT_RESPONSE_DEPTH_FACTOR,
    )
    keyword_limit = _rollout_scale_int(
        max(3, min(12, int(os.getenv("MCP_HOLISTIC_KEYWORD_LIMIT", "8")))),
        3,
        12,
        factor=ROLLOUT_RESPONSE_DEPTH_FACTOR,
    )
    connected_limit = _rollout_scale_int(
        max(0, min(8, int(os.getenv("MCP_HOLISTIC_CONNECTED_LIMIT", "4")))),
        0,
        8,
        factor=ROLLOUT_RESPONSE_DEPTH_FACTOR,
    )
    evidence_file_limit = _rollout_scale_int(
        max(4, min(14, int(os.getenv("MCP_HOLISTIC_EVIDENCE_FILES", "9")))),
        4,
        14,
        factor=ROLLOUT_RESPONSE_DEPTH_FACTOR,
    )

    if MSB_ROLLOUT_MODE == "safe":
        full_file_limit = max(0, full_file_limit - 1)
    elif MSB_ROLLOUT_MODE == "aggressive":
        full_file_limit = min(4, full_file_limit + 1)

    if intent["is_broad"]:
        semantic_limit = min(40, max(semantic_limit, 28))
        keyword_limit = min(12, max(keyword_limit, 9))
        connected_limit = min(8, max(connected_limit, 5))
        evidence_file_limit = min(14, max(evidence_file_limit, 10))
    elif intent["is_factual"]:
        keyword_limit = min(10, max(keyword_limit, 7))

    if intent.get("is_process"):
        semantic_limit = min(40, max(semantic_limit, 32))
        keyword_limit = min(12, max(keyword_limit, 9))
        connected_limit = min(8, max(connected_limit, 5))
        evidence_file_limit = min(14, max(evidence_file_limit, 10))
        full_file_limit = min(4, max(full_file_limit, 3))
        full_excerpt_chars = min(8000, max(full_excerpt_chars, 5000))

    query_vector: list[float] = []
    embedding_cache_hit = False
    embedding_status = "not_started"

    try:
        sidecar = _get_sidecar_index()
    except Exception as e:
        return {"error": f"Search setup failed: {str(e)}"}

    process_skip_semantic = bool(intent.get("is_process")) and os.getenv("MCP_HOLISTIC_PROCESS_SKIP_SEMANTIC", "1").strip().lower() not in {"0", "false", "no", "off"}
    process_semantic_backfill_enabled = bool(intent.get("is_process")) and os.getenv("MCP_HOLISTIC_PROCESS_SEMANTIC_BACKFILL", "1").strip().lower() not in {"0", "false", "no", "off"}
    process_semantic_backfill_attempted = False
    process_semantic_backfill_used = False

    if process_skip_semantic:
        cached_vector, cached_hit, cached_status = _get_query_embedding_cache_only(query)
        query_vector = cached_vector
        embedding_cache_hit = cached_hit
        if query_vector:
            embedding_status = f"{cached_status}:process_sidecar_first"
        else:
            embedding_status = "skipped:process_sidecar_first"

        if process_semantic_backfill_enabled and not query_vector:
            process_semantic_backfill_attempted = True
            try:
                bf_vector, bf_hit, bf_status = _get_query_embedding_cached(query)
                if bf_vector:
                    query_vector = bf_vector
                    embedding_cache_hit = bool(embedding_cache_hit or bf_hit)
                    embedding_status = f"{bf_status}:process_semantic_backfill"
                    process_semantic_backfill_used = True
            except Exception as e:
                embedding_status = f"{embedding_status}:backfill_unavailable:{str(e)[:120]}"
    else:
        try:
            query_vector, embedding_cache_hit, embedding_status = _get_query_embedding_cached(query)
        except Exception as e:
            embedding_status = f"unavailable:{str(e)[:160]}"
            query_vector = []
            embedding_cache_hit = False

    sidecar_rows_by_file = sidecar.get("rows_by_file", {}) or {}
    if not sidecar_rows_by_file:
        return {"semantic_results": [], "full_files": [], "keyword_hits": [], "connected_files": []}

    # ── Step 2: semantic vector search ───────────────────────────────────
    sem_rows = []
    if query_vector:
        try:
            sem_rows = tbl.search(query_vector).limit(semantic_limit).to_list()
        except Exception:
            sem_rows = []

    sem_results = []
    seen_files: dict = {}   # fname -> best confidence

    for r in sem_rows:
        dist = r.get("_distance", 0.5)
        conf = max(30, min(98, int((1 - dist) * 100)))
        meta = _parse_row_meta(r)
        topics = [_topic_display(t) for t in _sanitize_topics(meta.get("topics", []))]
        fname = r["source_file"]
        row: dict = {
            "content":        _compact_snippet(r.get("content", ""), max_chars=semantic_chars),
            "source_file":    fname,
            "display_name":   _display_name_for_source(fname, meta),
            "source_type":    r["source_type"],
            "confidence":     conf,
            "confidence_reason": f"semantic vector distance {dist:.3f}",
            "topics":         topics,
            "upload_context": meta.get("upload_context", ""),
            "chunk_index":    r.get("chunk_index", -1),
        }
        if r["source_type"] == "video":
            ts_start = meta.get("timestamp_start")
            ts_end   = meta.get("timestamp_end")
            tx       = meta.get("transcript", "")
            tx_abs   = meta.get("transcript_absolute", False)
            if ts_start is not None:
                row["timestamp_start"] = ts_start
                row["timestamp_end"]   = ts_end
            if tx:
                row["transcript"]          = tx
                row["transcript_absolute"] = tx_abs
        sem_results.append(row)
        if fname not in seen_files or conf > seen_files[fname]:
            seen_files[fname] = conf

    kw_terms = _extract_holistic_keywords(query)
    query_names = _extract_name_targets(query)
    candidate_files, candidate_signals = _candidate_files_from_sidecar(
        sidecar,
        query,
        kw_terms,
        query_names,
        sem_results,
        intent,
    )

    rows_by_file: dict[str, list[dict]] = {}
    rows_scanned = 0
    candidate_rows_limit = _rollout_scale_int(HOLISTIC_CANDIDATE_ROWS_LIMIT, 500, 40000)
    if intent["is_broad"]:
        per_file_candidate_cap = 48
    elif intent.get("is_process"):
        per_file_candidate_cap = 56
    else:
        per_file_candidate_cap = 28
    per_file_candidate_cap = _rollout_scale_int(per_file_candidate_cap, 16, 80)
    for fname in candidate_files:
        if rows_scanned >= candidate_rows_limit:
            break
        file_rows = sidecar_rows_by_file.get(fname, [])
        if not file_rows:
            continue
        remaining = candidate_rows_limit - rows_scanned
        take_n = min(remaining, per_file_candidate_cap)
        bounded = file_rows[:take_n]
        if not bounded:
            continue
        rows_by_file[fname] = bounded
        rows_scanned += len(bounded)

    for row in sem_results:
        fname = row.get("source_file", "")
        if fname and fname not in rows_by_file:
            fallback_rows = sidecar_rows_by_file.get(fname, [])
            if fallback_rows:
                rows_by_file[fname] = fallback_rows[:8]

    if not rows_by_file and sem_results:
        for row in sem_results:
            fname = row.get("source_file", "")
            if not fname:
                continue
            rows_by_file.setdefault(fname, []).append(
                {
                    "source_file": fname,
                    "source_type": row.get("source_type", "unknown"),
                    "chunk_index": row.get("chunk_index", 0),
                    "content": row.get("content", ""),
                    "metadata": {
                        "upload_context": row.get("upload_context", ""),
                        "topics": row.get("topics", []),
                    },
                }
            )

    # ── Step 3: full file content for top non-video semantic results ──────
    top_doc_files = []
    for r in sem_results:
        fn = r["source_file"]
        if r["source_type"] != "video" and fn not in top_doc_files:
            top_doc_files.append(fn)
        if len(top_doc_files) >= full_file_limit:
            break

    full_files = []
    for fname in top_doc_files:
        file_rows = sorted(rows_by_file.get(fname, []), key=lambda r: r.get("chunk_index") or 0)
        if not file_rows:
            continue
        raw_full_content = "\n\n---\n\n".join(r.get("content", "") for r in file_rows)
        full_excerpt = _build_focus_passages(
            raw_full_content,
            kw_terms,
            max_passages=4,
            radius=240,
            max_chars=min(full_excerpt_chars, 2200),
        )
        truncated = len(raw_full_content) > full_excerpt_chars
        if truncated:
            full_excerpt += "\n...[truncated — use get_file_content for complete text]"
        first = file_rows[0]
        meta = first.get("metadata", {}) or {}
        full_files.append({
            "source_file":    fname,
            "display_name":   _display_name_for_source(fname, meta),
            "source_type":    first.get("source_type", "unknown"),
            "full_content":   full_excerpt,
            "chunk_count":    len(file_rows),
            "confidence":     seen_files.get(fname, 0),
            "confidence_reason": "expanded evidence excerpt",
            "topics":         [_topic_display(t) for t in _sanitize_topics(meta.get("topics", []))],
            "upload_context": meta.get("upload_context", ""),
            "truncated": truncated,
        })

    # ── Step 4: keyword exact matching ───────────────────────────────────
    kw_hits: dict = {}
    candidate_pool_rows: list[dict] = []
    for file_rows in rows_by_file.values():
        candidate_pool_rows.extend(file_rows)

    for r in candidate_pool_rows:
        content = r.get("content", "")
        r_meta = r.get("metadata", {}) or {}
        upload_ctx = r_meta.get("upload_context", "")
        searchable = (content + " " + upload_ctx).lower()
        matched_terms = [kw for kw in kw_terms if kw in searchable]
        if not matched_terms:
            continue
        fname = r["source_file"]
        if fname not in kw_hits:
            kw_entry: dict = {
                "source_file":      fname,
                "display_name":     _display_name_for_source(fname, r_meta),
                "source_type":      r.get("source_type", "unknown"),
                "matched_keywords": set(),
                "snippet":          "",
                "confidence":       seen_files.get(fname, 0),
                "confidence_reason": "keyword/text match",
            }
            if r.get("source_type") == "video":
                tx      = r_meta.get("transcript", "")
                ts_s    = r_meta.get("timestamp_start")
                ts_e    = r_meta.get("timestamp_end")
                tx_abs  = r_meta.get("transcript_absolute", False)
                if tx:
                    kw_entry["transcript"]          = tx
                    kw_entry["timestamp_start"]     = ts_s
                    kw_entry["timestamp_end"]       = ts_e
                    kw_entry["transcript_absolute"] = tx_abs
            kw_hits[fname] = kw_entry

        for kw in matched_terms:
            kw_hits[fname]["matched_keywords"].add(kw)
            idx = content.lower().find(kw)
            if idx >= 0:
                s = max(0, idx - 260)
                e = min(len(content), idx + 260)
                new_snippet = ("..." if s > 0 else "") + content[s:e] + ("..." if e < len(content) else "")
                existing = kw_hits[fname]["snippet"]
                if not existing:
                    kw_hits[fname]["snippet"] = _compact_snippet(new_snippet, max_chars=340)
                elif new_snippet[:80] not in existing and len(existing) < 1800:
                    kw_hits[fname]["snippet"] = existing + "\n---\n" + new_snippet
            elif not kw_hits[fname]["snippet"] and upload_ctx:
                kw_hits[fname]["snippet"] = f"[From upload label]: {upload_ctx}"

        if kw_hits[fname].get("confidence", 0) <= 0:
            kw_hits[fname]["confidence"] = min(90, 62 + len(kw_hits[fname]["matched_keywords"]) * 6)

    def _kw_row(v: dict) -> dict:
        row = {
            "source_file":      v["source_file"],
            "display_name":     v.get("display_name", v["source_file"]),
            "source_type":      v["source_type"],
            "matched_keywords": sorted(v["matched_keywords"]),
            "snippet":          _compact_snippet(v["snippet"], max_chars=420),
            "confidence":       v["confidence"],
            "confidence_reason": v.get("confidence_reason", "keyword/text match"),
        }
        # Preserve transcript data for video files
        if v.get("transcript"):
            row["transcript"]          = v["transcript"]
            row["timestamp_start"]     = v.get("timestamp_start")
            row["timestamp_end"]       = v.get("timestamp_end")
            row["transcript_absolute"] = v.get("transcript_absolute", False)
        return row

    kw_results = sorted(
        [_kw_row(v) for v in kw_hits.values()],
        key=lambda x: (-len(x["matched_keywords"]), -x.get("confidence", 0))
    )[:keyword_limit]

    # ── Step 5: topic-connected files (other modes not in semantic) ───────
    top_topics: set = set()
    for r in sem_results[:5]:
        for t in r.get("topics", []):
            top_topics.add(t.lower())

    connected: dict = {}
    for fname, file_rows in rows_by_file.items():
        if fname in seen_files:
            continue
        first = file_rows[0] if file_rows else {}
        meta = first.get("metadata", {}) or {}
        file_topics = {_topic_display(t).lower() for t in _sanitize_topics(meta.get("topics", []))}
        shared = top_topics & file_topics
        if shared and fname not in connected:
            connected[fname] = {
                "source_file":    fname,
                "display_name":   _display_name_for_source(fname, meta),
                "source_type":    first.get("source_type", "unknown"),
                "shared_topics":  sorted(list(shared))[:4],
                "content_preview": _compact_snippet(first.get("content", ""), max_chars=260),
                "upload_context": meta.get("upload_context", ""),
            }

    # ── Step 6: rank-fusion + diversity-aware file selection ─────────────
    weights = intent["weights"]
    file_rank: dict[str, dict] = {}

    def _ensure_rank_entry(item: dict) -> dict:
        fname = item.get("source_file", "")
        if not fname:
            return {}
        if fname not in file_rank:
            seeded_signals = set(item.get("signals", []))
            if fname in candidate_signals:
                seeded_signals.add("sidecar-candidate")
            file_rank[fname] = {
                "source_file": fname,
                "display_name": item.get("display_name") or fname,
                "source_type": item.get("source_type", "unknown"),
                "score": 0.0,
                "max_conf": int(item.get("confidence", 0) or 0),
                "signals": seeded_signals,
                "topics": set(item.get("topics", [])),
                "upload_context": item.get("upload_context", ""),
                "matched_keywords": set(),
                "fragments": [],
            }
        return file_rank[fname]

    for row in sem_results:
        rec = _ensure_rank_entry(row)
        if not rec:
            continue
        sem_component = max(0.0, min(1.0, (row.get("confidence", 0) or 0) / 100.0))
        rec["score"] += sem_component * weights["semantic"]
        rec["max_conf"] = max(rec["max_conf"], int(row.get("confidence", 0) or 0))
        rec["signals"].add("semantic")
        _append_unique_snippet(rec["fragments"], row.get("content", ""), max_items=5, max_chars=300)

    for row in kw_results:
        rec = _ensure_rank_entry(row)
        if not rec:
            continue
        kw_count = len(row.get("matched_keywords", []))
        kw_density = min(1.0, kw_count / max(1, min(6, len(kw_terms))))
        rec["score"] += max(0.15, kw_density) * weights["keyword"]
        rec["max_conf"] = max(rec["max_conf"], int(row.get("confidence", 0) or 0))
        rec["signals"].add("keyword")
        rec["matched_keywords"].update(row.get("matched_keywords", []))
        _append_unique_snippet(rec["fragments"], row.get("snippet", ""), max_items=5, max_chars=320)

    for row in connected.values():
        rec = _ensure_rank_entry(row)
        if not rec:
            continue
        topic_density = min(1.0, len(row.get("shared_topics", [])) / 4.0)
        rec["score"] += max(0.12, topic_density) * weights["topic"]
        rec["signals"].add("topic-neighbor")
        rec["topics"].update(row.get("shared_topics", []))
        _append_unique_snippet(rec["fragments"], row.get("content_preview", ""), max_items=5, max_chars=280)

    for row in full_files:
        rec = _ensure_rank_entry(row)
        if not rec:
            continue
        rec["score"] += 0.06
        rec["signals"].add("file-excerpt")
        _append_unique_snippet(rec["fragments"], row.get("full_content", ""), max_items=5, max_chars=360)

    if query_names:
        for rec in file_rank.values():
            haystack = " ".join(rec.get("fragments", [])).lower()
            name_hits = 0
            for name in query_names:
                if re.search(rf"\b{re.escape(name.lower())}\b", haystack):
                    name_hits += 1
            if name_hits:
                rec["score"] += min(weights["name"], 0.04 * name_hits)
                rec["signals"].add("name-match")

    # Media type boost: images and videos are rare in most knowledge bases so they
    # naturally get buried by the many PDFs. When the query has any visual/media intent
    # words, give them a meaningful lift so they surface.
    _visual_query_words = {
        "photo", "photos", "picture", "pictures", "image", "images", "screenshot",
        "pic", "pics", "video", "videos", "clip", "clips", "recording", "film",
        "watch", "see", "show", "look", "view", "visual", "capture", "shot",
        "selfie", "portrait", "event", "party", "birthday", "gallery", "media",
    }
    _query_lower_words = set(w.strip("?.,!\"'()[]").lower() for w in query.split())
    _has_visual_intent = bool(_query_lower_words & _visual_query_words)

    for rec in file_rank.values():
        stype = str(rec.get("source_type", "")).lower()
        fname = rec.get("source_file", "")
        if stype in ("image", "video"):
            if _has_visual_intent:
                # Strong boost when query has visual intent (e.g. "find me a photo")
                rec["score"] += 0.85
                rec["signals"].add("media-type-boost")
            else:
                # Small always-on boost so media files don't fully disappear
                rec["score"] += 0.25
                rec["signals"].add("media-type-boost")
        # Notes penalty: brain dump notes (note_*.md) are supporting context, not primary sources.
        # Apply a small score penalty so they don't dominate results over uploaded files.
        if fname.startswith("note_") and fname.endswith(".md"):
            rec["score"] = max(0.0, rec["score"] - 0.35)
            rec["signals"].add("note-penalty")

    ranked_files = sorted(
        file_rank.values(),
        key=lambda x: (-x.get("score", 0.0), -x.get("max_conf", 0), x.get("source_file", "")),
    )

    selected_ranked: list[dict] = []
    selected_names: set[str] = set()

    def _select_ranked(rec: dict):
        fname = rec.get("source_file", "")
        if not fname or fname in selected_names:
            return
        selected_names.add(fname)
        selected_ranked.append(rec)

    if intent["is_broad"]:
        for must_signal in ("semantic", "keyword", "topic-neighbor"):
            for rec in ranked_files:
                if must_signal in rec.get("signals", set()):
                    _select_ranked(rec)
                    break

    min_target = min(evidence_file_limit, max(2, intent.get("min_files", 4)))
    for rec in ranked_files:
        if len(selected_ranked) >= min_target:
            break
        _select_ranked(rec)

    for rec in ranked_files:
        if len(selected_ranked) >= evidence_file_limit:
            break
        _select_ranked(rec)

    if len(selected_ranked) < min_target:
        file_info = sidecar.get("file_info", {}) or {}
        for fname in candidate_files:
            if len(selected_ranked) >= min_target:
                break
            if fname in selected_names:
                continue
            file_rows = rows_by_file.get(fname, [])
            if not file_rows:
                continue

            info = file_info.get(fname, {})
            first = file_rows[0]
            meta = first.get("metadata", {}) or {}
            score_floor = 0.03 + min(0.11, 0.02 * len(candidate_signals.get(fname, set())))
            fallback_rec = {
                "source_file": fname,
                "display_name": info.get("display_name") or _display_name_for_source(fname, meta),
                "source_type": info.get("source_type") or first.get("source_type", "unknown"),
                "score": score_floor,
                "max_conf": max(32, int(seen_files.get(fname, 0) or 0)),
                "signals": {"sidecar-candidate"},
                "topics": set(info.get("topics", []) or [_topic_display(t) for t in _sanitize_topics(meta.get("topics", []))]),
                "upload_context": info.get("upload_context", meta.get("upload_context", "")),
                "matched_keywords": set(),
                "fragments": [_compact_snippet(first.get("content", ""), max_chars=300)],
            }
            file_rank[fname] = fallback_rec
            _select_ranked(fallback_rec)

    if not selected_ranked:
        for rec in ranked_files[:evidence_file_limit]:
            _select_ranked(rec)

    # Rebuild file excerpts for selected ranked files (stage-3 compact evidence packing)
    focus_terms = list(dict.fromkeys(kw_terms + query_names + [w.strip().lower() for w in query.split() if len(w.strip()) >= 4]))
    full_files = []
    for rec in selected_ranked:
        if len(full_files) >= full_file_limit:
            break
        if rec.get("source_type") == "video":
            continue
        fname = rec.get("source_file", "")
        file_rows = sorted(rows_by_file.get(fname, []), key=lambda r: r.get("chunk_index") or 0)
        if not file_rows:
            continue

        raw_full_content = "\n\n---\n\n".join(r.get("content", "") for r in file_rows)
        full_excerpt = _build_focus_passages(
            raw_full_content,
            focus_terms,
            max_passages=4,
            radius=240,
            max_chars=min(full_excerpt_chars, 2200),
        )
        truncated = len(raw_full_content) > full_excerpt_chars
        if truncated:
            full_excerpt += "\n...[truncated — use get_file_content for complete text]"

        rec_conf = max(30, min(98, int(24 + rec.get("score", 0.0) * 76)))
        full_files.append({
            "source_file": fname,
            "display_name": rec.get("display_name") or fname,
            "source_type": rec.get("source_type", "unknown"),
            "full_content": full_excerpt,
            "chunk_count": len(file_rows),
            "confidence": rec_conf,
            "confidence_reason": "rank-fused evidence excerpt",
            "topics": sorted(list(rec.get("topics", set())))[:8],
            "upload_context": rec.get("upload_context", ""),
            "truncated": truncated,
        })

    rank_score_map = {rec.get("source_file", ""): float(rec.get("score", 0.0)) for rec in selected_ranked}
    rank_conf_map = {
        fname: max(30, min(98, int(24 + score * 76)))
        for fname, score in rank_score_map.items()
        if fname
    }

    candidate_rows = []
    for rec in selected_ranked:
        fname = rec.get("source_file", "")
        rows = sorted(rows_by_file.get(fname, []), key=lambda r: r.get("chunk_index") or 0)
        per_file_cap = 10 if intent["is_broad"] else (11 if intent.get("is_process") else 7)
        for row in rows[:per_file_cap]:
            candidate_rows.append(row)
            if len(candidate_rows) >= 140:
                break
        if len(candidate_rows) >= 140:
            break

    process_steps: list[dict] = []
    if intent.get("is_process"):
        process_steps = _build_process_steps(
            query=query,
            selected_ranked=selected_ranked,
            rows_by_file=rows_by_file,
            rank_score_map=rank_score_map,
            max_files=5,
            max_steps=14,
        )

    claim_validation_enabled = _requires_claim_validation(query, intent)
    numeric_tokens = _extract_numeric_tokens(query)

    claim_rows = candidate_rows
    if claim_validation_enabled:
        expanded_claim_rows = _collect_claim_candidate_rows(
            query=query,
            candidate_files=candidate_files,
            rows_by_file=rows_by_file,
            query_names=query_names,
            numeric_tokens=numeric_tokens,
            max_rows=360,
        )
        if expanded_claim_rows:
            claim_rows = expanded_claim_rows

    claim_conf_map = dict(rank_conf_map)
    for fname, conf in seen_files.items():
        claim_conf_map.setdefault(fname, max(30, min(98, int(conf))))

    claim_verification = _build_claim_verification(query, claim_rows, claim_conf_map) if claim_validation_enabled else []
    claim_adjudication = _build_claim_adjudication(claim_verification) if claim_verification else {
        "supported": [],
        "conflicting": [],
        "weak_support": [],
        "insufficient_evidence": [],
        "counts": {
            "supported": 0,
            "conflicting": 0,
            "weak_support": 0,
            "insufficient_evidence": 0,
            "total": 0,
        },
        "policy": {
            "requires_uncertainty": False,
            "can_answer_directly": True,
            "must_not_guess": False,
        },
    }

    evidence_map: dict[str, dict] = {}

    def _ensure_evidence_row(item: dict) -> dict:
        fname = item.get("source_file", "")
        if not fname or fname not in selected_names:
            return {}
        if fname not in evidence_map:
            seeded_signals = set()
            if fname in candidate_signals:
                seeded_signals.add("sidecar-candidate")
            evidence_map[fname] = {
                "source_file": fname,
                "display_name": item.get("display_name") or fname,
                "source_type": item.get("source_type", "unknown"),
                "confidence": int(rank_conf_map.get(fname, item.get("confidence", 0) or 0)),
                "confidence_reason": item.get("confidence_reason", "rank-fused retrieval"),
                "topics": list(item.get("topics", []))[:10],
                "upload_context": item.get("upload_context", ""),
                "truncated": bool(item.get("truncated", False)),
                "matched_keywords": [],
                "match_signals": seeded_signals,
                "evidence_snippets": [],
                "rank_score": rank_score_map.get(fname, 0.0),
            }
        return evidence_map[fname]

    # Seed evidence rows for every selected ranked file so broad queries preserve
    # minimum multi-file coverage even when some files only match via sidecar rank signals.
    for rec in selected_ranked:
        base_item = {
            "source_file": rec.get("source_file", ""),
            "display_name": rec.get("display_name", rec.get("source_file", "")),
            "source_type": rec.get("source_type", "unknown"),
            "confidence": int(rank_conf_map.get(rec.get("source_file", ""), rec.get("max_conf", 0) or 0)),
            "confidence_reason": "rank-fused retrieval",
            "topics": sorted(list(rec.get("topics", set())))[:10],
            "upload_context": rec.get("upload_context", ""),
            "truncated": False,
        }
        seeded = _ensure_evidence_row(base_item)
        if not seeded:
            continue
        seeded["match_signals"].add("rank-selected")
        fragments = rec.get("fragments", []) or []
        for fragment in fragments[:2]:
            _append_unique_snippet(seeded["evidence_snippets"], fragment, max_items=4, max_chars=320)

    for row in sem_results:
        rec = _ensure_evidence_row(row)
        if not rec:
            continue
        rec["match_signals"].add("semantic")
        rec["confidence"] = max(rec["confidence"], int(row.get("confidence", 0) or 0))
        _append_unique_snippet(rec["evidence_snippets"], row.get("content", ""), max_items=4, max_chars=340)

    for row in kw_results:
        rec = _ensure_evidence_row(row)
        if not rec:
            continue
        rec["match_signals"].add("keyword")
        rec["confidence"] = max(rec["confidence"], int(row.get("confidence", 0) or 0))
        rec["matched_keywords"] = sorted(list(set(rec.get("matched_keywords", []) + row.get("matched_keywords", []))))[:10]
        _append_unique_snippet(rec["evidence_snippets"], row.get("snippet", ""), max_items=4, max_chars=340)

    for row in connected.values():
        rec = _ensure_evidence_row(row)
        if not rec:
            continue
        rec["match_signals"].add("topic-neighbor")
        _append_unique_snippet(rec["evidence_snippets"], row.get("content_preview", ""), max_items=4, max_chars=320)

    for row in full_files:
        rec = _ensure_evidence_row(row)
        if not rec:
            continue
        rec["match_signals"].add("file-excerpt")
        rec["truncated"] = rec.get("truncated", False) or bool(row.get("truncated", False))
        _append_unique_snippet(rec["evidence_snippets"], row.get("full_content", ""), max_items=4, max_chars=520)

    if process_steps:
        for step in process_steps:
            rec = _ensure_evidence_row(
                {
                    "source_file": step.get("source_file", ""),
                    "display_name": step.get("display_name", step.get("source_file", "")),
                    "source_type": "text",
                    "confidence": int(step.get("confidence", 0) or 0),
                    "confidence_reason": "process-focused deep retrieval",
                    "topics": [],
                    "upload_context": "",
                }
            )
            if not rec:
                continue
            rec["match_signals"].add("process-deep")
            rec["confidence"] = max(rec.get("confidence", 0), int(step.get("confidence", 0) or 0))
            _append_unique_snippet(rec["evidence_snippets"], step.get("snippet", ""), max_items=4, max_chars=360)

    evidence_files = []
    for rec in evidence_map.values():
        signals = sorted(list(rec.pop("match_signals")))
        rec["match_signals"] = signals
        if not rec.get("confidence_reason") or rec.get("confidence_reason") == "retrieval evidence":
            rec["confidence_reason"] = f"rank-fused: {', '.join(signals[:3])}" if signals else "rank-fused retrieval"
        evidence_files.append(rec)

    evidence_files.sort(
        key=lambda x: (-x.get("rank_score", 0.0), -x.get("confidence", 0), x.get("source_file", ""))
    )
    evidence_files = evidence_files[:evidence_file_limit]
    for rec in evidence_files:
        rec.pop("rank_score", None)

    if intent["is_broad"] and len(evidence_files) < 4:
        existing_files = {ev.get("source_file", "") for ev in evidence_files}
        file_info = sidecar.get("file_info", {}) or {}
        fallback_pool = list(dict.fromkeys(candidate_files + sorted(list(sidecar_rows_by_file.keys()))))
        for fname in fallback_pool:
            if len(evidence_files) >= 4:
                break
            if not fname or fname in existing_files:
                continue
            file_rows = rows_by_file.get(fname, [])
            if not file_rows:
                sidecar_file_rows = sidecar_rows_by_file.get(fname, [])
                if sidecar_file_rows:
                    file_rows = sidecar_file_rows[:8]
            if not file_rows:
                continue

            first = file_rows[0]
            meta = first.get("metadata", {}) or {}
            snippet = _compact_snippet(first.get("content", ""), max_chars=320)
            evidence_files.append(
                {
                    "source_file": fname,
                    "display_name": file_info.get(fname, {}).get("display_name") or _display_name_for_source(fname, meta),
                    "source_type": file_info.get(fname, {}).get("source_type") or first.get("source_type", "unknown"),
                    "confidence": max(32, int(seen_files.get(fname, 0) or 0)),
                    "confidence_reason": "broad-query fallback coverage",
                    "topics": file_info.get(fname, {}).get("topics") or [_topic_display(t) for t in _sanitize_topics(meta.get("topics", []))][:8],
                    "upload_context": file_info.get(fname, {}).get("upload_context", meta.get("upload_context", "")),
                    "truncated": False,
                    "matched_keywords": [],
                    "match_signals": ["broad-fallback"],
                    "evidence_snippets": [snippet] if snippet else [],
                }
            )
            existing_files.add(fname)

        evidence_files.sort(key=lambda x: (-x.get("confidence", 0), x.get("source_file", "")))
        evidence_files = evidence_files[:evidence_file_limit]

    semantic_selected = [r for r in sem_results if r.get("source_file") in selected_names][:12]
    keyword_selected = [r for r in kw_results if r.get("source_file") in selected_names][:keyword_limit]
    connected_values = list(connected.values())
    if intent["is_broad"]:
        connected_selected = connected_values[:connected_limit]
    else:
        connected_selected = [r for r in connected_values if r.get("source_file") in selected_names][:connected_limit]

    # Build tier2_recommendations: top 3 files worth deepening into
    _tier2_recs = []
    for _ev in evidence_files[:6]:
        _fname = _ev.get("source_file", "")
        _conf = int(_ev.get("confidence", 0) or 0)
        _sigs = _ev.get("match_signals", []) or []
        _truncated = bool(_ev.get("truncated", False))
        _topics = _ev.get("topics", []) or []
        # Prioritise: truncated files first, then high-confidence files, then keyword-only
        _why = []
        if _truncated:
            _why.append("excerpt truncated - full content may contain more detail")
        if _conf >= 85:
            _why.append(f"high confidence match ({_conf}%)")
        if set(_sigs) & {"semantic", "rank-selected"}:
            _why.append("strong semantic relevance")
        if _topics:
            _why.append(f"covers: {', '.join(_topics[:3])}")
        if _why:
            _tier2_recs.append({
                "source_file": _fname,
                "confidence": _conf,
                "truncated": _truncated,
                "why_check": "; ".join(_why[:2]),
                "topics": _topics[:4],
            })
        if len(_tier2_recs) >= 3:
            break

    return {
        "query":            query,
        "query_raw":        query_raw,
        "evidence_files":   evidence_files,
        "semantic_results": semantic_selected or sem_results[: min(len(sem_results), 12)],
        "full_files":       full_files,
        "keyword_hits":     keyword_selected,
        "connected_files":  connected_selected,
        "process_steps": process_steps[:14],
        "claim_verification": claim_verification,
        "claim_adjudication": claim_adjudication,
        "tier2_recommendations": _tier2_recs,
        "retrieval_meta": {
            "scan_limit": sidecar.get("scan_limit", SIDECAR_SCAN_LIMIT),
            "rows_scanned": rows_scanned,
            "candidate_rows_limit": candidate_rows_limit,
            "semantic_rows": len(sem_rows),
            "semantic_limit": semantic_limit,
            "keyword_limit": keyword_limit,
            "full_file_limit": full_file_limit,
            "connected_limit": connected_limit,
            "compact_mode": True,
            "sidecar_mode": True,
            "sidecar_files_loaded": sidecar.get("files_loaded", len(sidecar_rows_by_file)),
            "sidecar_rows_loaded": sidecar.get("rows_loaded", 0),
            "sidecar_truncated": bool(sidecar.get("truncated", False)),
            "candidate_files": len(rows_by_file),
            "embedding_cache_hit": bool(embedding_cache_hit),
            "embedding_status": embedding_status,
            "semantic_degraded": not bool(query_vector),
            "query_intent": intent.get("label", "hybrid"),
            "fusion_candidates": len(file_rank),
            "selected_files": len(evidence_files),
            "rollout_mode": MSB_ROLLOUT_MODE,
            "rollout_candidate_factor": ROLLOUT_CANDIDATE_FACTOR,
            "rollout_depth_factor": ROLLOUT_RESPONSE_DEPTH_FACTOR,
            "diversity_min_files": intent.get("min_files", 4),
            "claim_validation_enabled": claim_validation_enabled,
            "claim_candidate_rows": len(claim_rows) if claim_validation_enabled else 0,
            "claim_status_counts": claim_adjudication.get("counts", {}),
            "process_query": bool(intent.get("is_process")),
            "process_steps": len(process_steps),
            "process_source_files": len({str(step.get("source_file", "") or "") for step in process_steps if step.get("source_file")}),
            "process_semantic_backfill_attempted": bool(process_semantic_backfill_attempted),
            "process_semantic_backfill_used": bool(process_semantic_backfill_used),
            "answer_first_contract": True,
            "limitation_sentence_max": 1,
        },
    }


def _aggregate_entities(all_rows: list) -> tuple[list, list]:
    """
    Aggregate all entities and relationships across all DB rows.
    Returns (entities_list, relationships_list) — deduplicated.
    entities_list items: {name, type, description, files: [str], file_count: int}
    relationships_list items: {from, relationship, to, file}
    """
    entity_map: dict = {}
    seen_rels: set = set()
    relationships: list = []

    for row in all_rows:
        meta = json.loads(row["metadata"]) if isinstance(row.get("metadata"), str) else row.get("metadata", {})
        ents_data = (meta or {}).get("entities", {})
        if not ents_data:
            continue
        fname = row["source_file"]
        for e in ents_data.get("entities", []):
            name = str(e.get("name", "")).strip()
            if not name:
                continue
            key = name.lower()
            if key not in entity_map:
                entity_map[key] = {
                    "name": name,
                    "type": e.get("type", "concept"),
                    "description": e.get("description", ""),
                    "files": set(),
                }
            entity_map[key]["files"].add(fname)
        for r in ents_data.get("relationships", []):
            frm = str(r.get("from", "")).strip()
            rel = str(r.get("relationship", "")).strip()
            to  = str(r.get("to", "")).strip()
            rkey = (frm.lower(), rel.lower(), to.lower())
            if frm and rel and to and rkey not in seen_rels:
                seen_rels.add(rkey)
                relationships.append({"from": frm, "relationship": rel, "to": to, "file": fname})

    entities = []
    for e in entity_map.values():
        entities.append({
            "name": e["name"],
            "type": e["type"],
            "description": e["description"],
            "files": sorted(list(e["files"])),
            "file_count": len(e["files"]),
        })
    entities.sort(key=lambda x: (-x["file_count"], x["name"]))
    return entities, relationships


@app.get("/api/mcp/entities")
def mcp_get_entities():
    """Return all extracted entities and relationships across the knowledge base."""
    try:
        sidecar = _get_sidecar_index()
    except Exception as e:
        return {"error": f"Sidecar lookup failed: {str(e)}"}

    entities = sidecar.get("entities", []) or []
    relationships = sidecar.get("relationships", []) or []
    return {
        "entities": entities,
        "relationships": relationships,
        "total_entities": len(entities),
        "total_relationships": len(relationships),
        "retrieval_meta": {
            "scan_mode": "sidecar_index",
            "sidecar_files": sidecar.get("files_loaded", 0),
        },
    }


@app.post("/api/mcp/entity_search")
def mcp_entity_search(req: MCPEntitySearchRequest):
    """Find entities matching a query string, with their relationships and source files."""
    try:
        sidecar = _get_sidecar_index()
    except Exception as e:
        return {"error": f"Sidecar lookup failed: {str(e)}"}

    entities = sidecar.get("entities", []) or []
    relationships = sidecar.get("relationships", []) or []

    query_lower = req.query.lower().strip()
    query_words = [w for w in query_lower.split() if len(w) > 2]

    matches = []
    for e in entities:
        if req.entity_types and e["type"] not in req.entity_types:
            continue
        name_lower = e["name"].lower()
        desc_lower = e.get("description", "").lower()
        score = 0
        if name_lower == query_lower:
            score = 100
        elif query_lower in name_lower or name_lower in query_lower:
            score = 85
        elif any(w in name_lower for w in query_words):
            score = 65
        elif any(w in desc_lower for w in query_words):
            score = 40
        if score > 0:
            matches.append({**e, "score": score})

    matches.sort(key=lambda x: -x["score"])
    matched_names_lower = {m["name"].lower() for m in matches[:15]}
    relevant_rels = [
        r for r in relationships
        if r["from"].lower() in matched_names_lower or r["to"].lower() in matched_names_lower
    ]

    return {
        "query": req.query,
        "matches": matches[:15],
        "relationships": relevant_rels[:30],
        "total_matches": len(matches),
        "retrieval_meta": {
            "scan_mode": "sidecar_index",
            "sidecar_files": sidecar.get("files_loaded", 0),
        },
    }


@app.get("/api/mcp/topics")
def mcp_get_topics():
    """Return all topics and their associated files for the MCP server."""
    try:
        sidecar = _get_sidecar_index()
    except Exception as e:
        return {"error": f"Sidecar lookup failed: {str(e)}"}

    topics = sidecar.get("topics_map", {}) or {}
    return {
        "topics": topics,
        "retrieval_meta": {
            "scan_mode": "sidecar_index",
            "sidecar_files": sidecar.get("files_loaded", 0),
        },
    }


class MCPClipRequest(BaseModel):
    file: str
    topic: str  # what the user wants to see — Claude passes the quote/topic verbatim


@app.post("/api/mcp/find_clip")
def mcp_find_clip(req: MCPClipRequest):
    """
    Given a video filename and a topic/quote, scan all stored transcript chunks for
    that video, find the lines most relevant to the topic, and return a ready-made
    clip URL. Claude should never build clip URLs manually — it should call this.
    """
    try:
        sidecar = _get_sidecar_index()
    except Exception as e:
        return {"error": f"Sidecar lookup failed: {str(e)}"}

    rows_by_file = sidecar.get("rows_by_file", {}) or {}
    file_rows = rows_by_file.get(req.file, [])
    if not file_rows:
        lowered = req.file.lower()
        for fname, rows in rows_by_file.items():
            if fname.lower() == lowered:
                file_rows = rows
                break

    # Collect all chunks for this file that have a transcript
    video_chunks = []
    for row in file_rows:
        if row.get("source_type", "") != "video":
            continue
        meta = row.get("metadata", {}) or {}
        transcript = meta.get("transcript", "")
        if not transcript:
            continue
        ts_start = meta.get("timestamp_start")
        ts_end = meta.get("timestamp_end")
        tx_abs = meta.get("transcript_absolute", False)
        video_chunks.append({
            "chunk_index": row.get("chunk_index", 0),
            "ts_start": ts_start,
            "ts_end": ts_end,
            "transcript": transcript,
            "transcript_absolute": tx_abs,
            "content": row.get("content", ""),
        })

    if not video_chunks:
        return {"error": f"No transcript data found for '{req.file}'. The video may not have been processed with transcript extraction."}

    video_chunks.sort(key=lambda c: c["chunk_index"])

    # Parse transcript lines into (abs_second, text) pairs
    import re as _re

    def parse_lines(chunk):
        lines = []
        offset = 0 if chunk["transcript_absolute"] else (int(chunk["ts_start"]) if chunk["ts_start"] is not None else 0)
        for line in chunk["transcript"].splitlines():
            line = line.strip()
            if not line:
                continue
            m = _re.match(r'^\[(\d+):(\d{2})\]', line)
            if m:
                abs_s = int(m.group(1)) * 60 + int(m.group(2)) + offset
                text = line[m.end():].strip(" :-")
                lines.append((abs_s, text))
        return lines

    all_lines = []
    for chunk in video_chunks:
        all_lines.extend(parse_lines(chunk))

    if not all_lines:
        # No parseable timestamps — fall back to whole-chunk URLs
        enc = urllib.parse.quote(req.file, safe="")
        first = video_chunks[0]
        s = int(first["ts_start"]) if first["ts_start"] is not None else 0
        e = int(first["ts_end"]) if first["ts_end"] is not None else s + 60
        url = f"/clip?file={enc}&start={s}&end={e}"
        return {
            "clip_url": url,
            "preview_url": url,
            "start": s,
            "end": e,
            "matched_lines": [],
            "note": "No timestamp lines found in transcript — returning first chunk window.",
        }

    # Score each line by weighted keyword overlap with the topic query.
    # Uses TF-style weighting: longer/rarer topic words score higher.
    stop = {"the","a","an","is","it","in","on","of","to","and","or","that","this",
            "i","you","we","he","she","they","do","did","does","what","how","why",
            "who","when","where","can","could","would","should","will","just","get",
            "got","its","be","been","was","were","are","have","has","had","with","for",
            "from","at","by","about","as","into","up","out","not","but","so","if","me"}

    raw_topic_words = [w for w in _re.sub(r'[^\w\s]', '', req.topic.lower()).split() if w not in stop and len(w) > 2]
    # Weight longer/more-specific words higher
    word_weights = {w: (1.0 + 0.3 * max(0, len(w) - 4)) for w in raw_topic_words}
    total_weight = sum(word_weights.values()) or 1.0

    def score_line(text: str) -> float:
        words = set(_re.sub(r'[^\w\s]', '', text.lower()).split())
        return sum(word_weights[w] for w in word_weights if w in words) / total_weight

    # Also score each line by positional context: lines near a high-scoring line get a bonus
    scored = [(score_line(text), abs_s, text) for abs_s, text in all_lines]

    # Find the densest cluster: slide a 45-second window, pick the window with highest total score
    WINDOW = 45  # seconds — tighter than before
    all_seconds = [abs_s for _, abs_s, _ in scored]
    score_by_second = {abs_s: sc for sc, abs_s, _ in scored}

    best_window_start = None
    best_window_score = -1.0

    for i, (sc, abs_s, _) in enumerate(scored):
        if sc == 0:
            continue
        window_end = abs_s + WINDOW
        window_score = sum(score_by_second.get(s2, 0) for s2 in all_seconds if abs_s <= s2 <= window_end)
        if window_score > best_window_score:
            best_window_score = window_score
            best_window_start = abs_s

    if best_window_start is None or best_window_score == 0:
        enc = urllib.parse.quote(req.file, safe="")
        url = f"/clip?file={enc}&start=0&end=60"
        return {
            "clip_url": url,
            "start": 0,
            "end": 60,
            "matched_lines": [],
            "note": f"No transcript lines matched '{req.topic}' — returning first 60 seconds.",
        }

    # Collect all lines inside the best window
    window_lines = [(sc, abs_s, text) for sc, abs_s, text in scored
                    if best_window_start <= abs_s <= best_window_start + WINDOW and sc > 0]
    window_lines.sort(key=lambda x: x[1])  # sort by timestamp

    if not window_lines:
        enc = urllib.parse.quote(req.file, safe="")
        url = f"/clip?file={enc}&start=0&end=60"
        return {
            "clip_url": url,
            "start": 0,
            "end": 60,
            "matched_lines": [],
            "note": f"No matching lines in window — returning first 60 seconds.",
        }

    first_hit = window_lines[0][1]
    last_hit  = window_lines[-1][1]

    # Find the next transcript line after last_hit so we don't cut mid-sentence
    all_sorted_seconds = sorted(set(abs_s for _, abs_s, _ in scored))
    idx_last = all_sorted_seconds.index(last_hit) if last_hit in all_sorted_seconds else -1
    # Include 1–2 more lines after the last hit for natural ending
    tail_end = last_hit
    if idx_last >= 0 and idx_last + 2 < len(all_sorted_seconds):
        tail_end = all_sorted_seconds[min(idx_last + 2, len(all_sorted_seconds) - 1)]
    elif idx_last >= 0 and idx_last + 1 < len(all_sorted_seconds):
        tail_end = all_sorted_seconds[idx_last + 1]

    clip_start = max(0, first_hit - 4)   # 4s lead-in for natural context
    clip_end   = tail_end + 5            # 5s after last hit line to let sentence finish

    # Cap at 2 minutes — if answer is longer, something is wrong with matching
    if clip_end - clip_start > 120:
        clip_end = clip_start + 120

    clip_url = _clip_relative_url(req.file, int(clip_start), int(clip_end))
    matched_lines = [text for _, _, text in window_lines]

    return {
        "clip_url": clip_url,
        "start": int(clip_start),
        "end": int(clip_end),
        "duration_seconds": int(clip_end - clip_start),
        "matched_lines": matched_lines,
        "note": f"Clip covers {int(clip_start)}s–{int(clip_end)}s ({int(clip_end-clip_start)}s). Densest match window in video.",
    }


@app.get("/")
def serve_frontend_root():
    index_path = os.path.join(FRONTEND_DIST_DIR, "index.html")
    if not os.path.isfile(index_path):
        return JSONResponse(
            {
                "status": "frontend_not_built",
                "message": "Frontend build not found. Run: cd frontend && npm run build"
            },
            status_code=503
        )
    return FileResponse(index_path)

@app.get("/{full_path:path}")
def serve_frontend_spa(full_path: str):
    if full_path.startswith("api/") or full_path.startswith("brain_data/"):
        raise HTTPException(status_code=404, detail="Not found")

    file_path = os.path.join(FRONTEND_DIST_DIR, full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)

    index_path = os.path.join(FRONTEND_DIST_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)

    return JSONResponse(
        {
            "status": "frontend_not_built",
            "message": "Frontend build not found. Run: cd frontend && npm run build"
        },
        status_code=503
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
