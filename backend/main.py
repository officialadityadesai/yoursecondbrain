import os
import re
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
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Form
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, HTMLResponse
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
                    print(f"Entity backfill: {sf} → {len(entities.get('entities', []))} entities")
                else:
                    print(f"Entity backfill: {sf} → no entities found (content may be too sparse)")

                time.sleep(1.5)  # Gentle rate-limit buffer between Gemini calls
            except Exception as e:
                print(f"Entity backfill: failed for {sf}: {e}")

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
                    print(f"Transcript backfill: {source_file} chunk {row.get('chunk_index', 0)} — done", flush=True)

                time.sleep(2)
            except Exception as e:
                print(f"Transcript backfill: failed for {source_file}: {e}", flush=True)

        print("Transcript backfill: complete.", flush=True)
    except Exception as e:
        print(f"Transcript backfill error: {e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_entity_backfill, daemon=True).start()
    threading.Thread(target=_transcript_backfill, daemon=True).start()
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
from threading import Lock, Semaphore

UPLOAD_STATUS = {}
UPLOAD_STATUS_LOCK = Lock()
# Only one file processes at a time — prevents Gemini rate-limit hammering
# when multiple files are uploaded simultaneously.
PROCESSING_SEMAPHORE = Semaphore(1)
OAUTH_PENDING = {}
OAUTH_PENDING_LOCK = Lock()

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
                enc = urllib.parse.quote(r["source_file"])
                if transcript:
                    if transcript_absolute:
                        clip_line = (
                            f"\nTranscript (timestamps are ABSOLUTE — use directly as clip start/end):\n"
                            f"{transcript}\n"
                            f"Preview URL template: http://127.0.0.1:8000/clip?file={enc}&start=ABSOLUTE_START&end=ABSOLUTE_END\n"
                        )
                    else:
                        clip_line = (
                            f"\nChunk window: {int(ts_start)}s–{int(ts_end)}s in full video\n"
                            f"Transcript (timestamps relative to chunk start = {int(ts_start)}s):\n"
                            f"{transcript}\n"
                            f"To get absolute position: add transcript seconds to {int(ts_start)}.\n"
                            f"Preview URL template: http://127.0.0.1:8000/clip?file={enc}&start=ABSOLUTE_START&end=ABSOLUTE_END\n"
                        )
                else:
                    clip_url = f"http://127.0.0.1:8000/clip?file={enc}&start={int(ts_start)}&end={int(ts_end)}"
                    clip_line = f"\nWatch full chunk: {clip_url}\n"
            elif transcript:
                # Whole-file video (no ffmpeg chunking) — ts_start is None but transcript exists.
                # Transcript timestamps are relative to video start = 0, so they are absolute.
                enc = urllib.parse.quote(r["source_file"])
                clip_line = (
                    f"\nTranscript (timestamps are ABSOLUTE — video start = 00:00):\n"
                    f"{transcript}\n"
                    f"Preview URL template: http://127.0.0.1:8000/clip?file={enc}&start=ABSOLUTE_START&end=ABSOLUTE_END\n"
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
            citations.append({
                "file": r["source_file"],
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
    import json
    tbl = get_table()
    try:
        # Increased limit to ensure all nodes are retrieved
        data = tbl.search().limit(10000).to_list()
        nodes = []
        links = {}
        doc_file_index = {}

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
            if file_key in doc_file_index:
                doc_id = doc_file_index[file_key]
            else:
                doc_id = d["id"]
                doc_file_index[file_key] = doc_id
                doc_vectors[doc_id] = d.get("vector")
                nodes.append({
                    "id": doc_id,
                    "name": d["source_file"],
                    "group": "document",
                    "val": 4.5,
                    "source_type": d["source_type"]
                })
            meta = json.loads(d["metadata"]) if isinstance(d["metadata"], str) else d.get("metadata", {})
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
        
    files = {}
    for d in docs:
        if d["source_file"] not in files:
            files[d["source_file"]] = {
                "name": d["source_file"],
                "type": d["source_type"]
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
    file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "brain_data", filename)
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

    return {"status": "success"}

@app.post("/api/upload")
async def upload_file(background_tasks: BackgroundTasks, file: UploadFile = File(...), upload_context: str = Form(default="")):
    os.makedirs(os.path.join(os.path.dirname(os.path.dirname(__file__)), "brain_data"), exist_ok=True)
    file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "brain_data", file.filename)
    file_bytes = file.file.read()
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    tbl = get_table()
    existing_rows = tbl.search().limit(20000).to_list()
    duplicate_by_hash = None
    for row in existing_rows:
        meta = json.loads(row["metadata"]) if isinstance(row.get("metadata"), str) else row.get("metadata", {})
        if (meta or {}).get("content_hash") == file_hash:
            duplicate_by_hash = row.get("source_file")
            break
    if duplicate_by_hash:
        raise HTTPException(
            status_code=409,
            detail=f"Exact duplicate blocked: this file content already exists as '{duplicate_by_hash}'."
        )

    same_name_rows = [r for r in existing_rows if r.get("source_file") == file.filename]
    if same_name_rows:
        raise HTTPException(status_code=409, detail=f"Duplicate filename blocked: '{file.filename}' already exists. Rename file or delete existing one first.")

    with open(file_path, "wb") as buffer:
        buffer.write(file_bytes)
    
    def _process_with_status(path: str, filename: str, context: str, content_hash: str):
        # Mark as queued while waiting for the semaphore slot
        with UPLOAD_STATUS_LOCK:
            UPLOAD_STATUS[filename] = {"status": "queued", "error": None}
        # Acquire slot — blocks until any currently-processing file finishes
        with PROCESSING_SEMAPHORE:
            with UPLOAD_STATUS_LOCK:
                UPLOAD_STATUS[filename] = {"status": "processing", "error": None}
            try:
                process_file(path, upload_context=context, content_hash=content_hash)
                with UPLOAD_STATUS_LOCK:
                    UPLOAD_STATUS[filename] = {"status": "done", "error": None}
            except Exception as e:
                with UPLOAD_STATUS_LOCK:
                    UPLOAD_STATUS[filename] = {"status": "failed", "error": str(e)}

    # Queue for background processing (serialised via PROCESSING_SEMAPHORE)
    background_tasks.add_task(_process_with_status, file_path, file.filename, upload_context or "", file_hash)
        
    return {"status": "success", "filename": file.filename, "message": "File queued for processing."}

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
            import json
            source_file = d["source_file"]
            meta = json.loads(d["metadata"]) if isinstance(d["metadata"], str) else d.get("metadata", {})

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
                "name": source_file,
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
        import json
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

    try:
        def _do_embed():
            return gemini.models.embed_content(
                model="gemini-embedding-2-preview",
                contents=[req.query],
                config=genai.types.EmbedContentConfig(
                    task_type="RETRIEVAL_QUERY",
                    output_dimensionality=1536
                )
            )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_do_embed)
            result = future.result(timeout=15)
        query_vector = result.embeddings[0].values
    except concurrent.futures.TimeoutError:
        return {"error": "Embedding timed out — Gemini API is slow. Try again in a moment."}
    except Exception as e:
        return {"error": f"Embedding failed: {str(e)}"}

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
        topics = [_topic_display(t) for t in _sanitize_topics((meta or {}).get("topics", []))]
        row: dict = {
            "content": r["content"],
            "source_file": r["source_file"],
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

    return {"results": results}


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
        if fname not in files:
            files[fname] = {"name": fname, "type": d["source_type"], "topics": set()}
        meta = json.loads(d["metadata"]) if isinstance(d.get("metadata"), str) else d.get("metadata", {})
        for t in _sanitize_topics((meta or {}).get("topics", [])):
            files[fname]["topics"].add(_topic_display(t))

    result = []
    for f in sorted(files.values(), key=lambda x: x["name"]):
        result.append({"name": f["name"], "type": f["type"], "topics": sorted(list(f["topics"]))})

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
                    "type": other_file_types.get(fname, "unknown"),
                    "confidence": max(0, min(100, int(sim * 100))),
                })
        semantic_peers.sort(key=lambda x: -x["confidence"])
        semantic_peers = semantic_peers[:10]

    return {
        "name": filename,
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

    tbl = get_table()
    try:
        all_rows = tbl.search().limit(50000).to_list()
    except Exception as e:
        return {"error": f"Database query failed: {str(e)}"}

    results = []
    seen_files = set()
    for r in all_rows:
        content = r.get("content", "")
        content_lower = content.lower()
        meta = json.loads(r["metadata"]) if isinstance(r.get("metadata"), str) else (r.get("metadata") or {})
        upload_ctx = (meta or {}).get("upload_context", "")
        searchable = (content + " " + upload_ctx).lower()
        if keyword_lower not in searchable:
            continue

        # Extract snippets — from content if keyword found there, else from upload_context label
        occurrences = content_lower.count(keyword_lower)
        if occurrences > 0:
            snippets = []
            search_from = 0
            for _ in range(min(occurrences, 4)):
                idx = content_lower.find(keyword_lower, search_from)
                if idx == -1:
                    break
                s = max(0, idx - 500)
                e = min(len(content), idx + 500)
                prefix = "..." if s > 0 else ""
                suffix = "..." if e < len(content) else ""
                snippets.append(prefix + content[s:e] + suffix)
                search_from = idx + len(keyword_lower)
            snippet = "\n---\n".join(snippets)
        else:
            # Keyword found only in upload_context (user's file label)
            occurrences = 1
            snippet = f"[From upload label]: {upload_ctx}"

        topics = [_topic_display(t) for t in _sanitize_topics((meta or {}).get("topics", []))]

        results.append({
            "source_file": r["source_file"],
            "source_type": r["source_type"],
            "snippet": snippet,
            "occurrences": occurrences,
            "topics": topics,
            "upload_context": (meta or {}).get("upload_context", ""),
        })
        seen_files.add(r["source_file"])
        if len(results) >= req.max_results:
            break

    return {"results": results, "total_files_matched": len(seen_files)}


@app.post("/api/mcp/holistic_search")
def mcp_holistic_search(req: MCPHolisticRequest):
    """
    Single-call multimodal search that combines semantic vector search, keyword exact
    matching, full file content retrieval, and topic graph connections — all in one
    server-side pass. Replaces the need for Claude to make 3-4 sequential tool calls.
    """
    if not gemini:
        return {"error": "Gemini API Key missing."}
    query = req.query.strip()
    if not query:
        return {"error": "query required"}

    def _parse_meta(r):
        m = r.get("metadata")
        return (json.loads(m) if isinstance(m, str) else m) or {}

    # ── Step 1: embed query + load all rows concurrently ─────────────────
    def _embed():
        return gemini.models.embed_content(
            model="gemini-embedding-2-preview",
            contents=[query],
            config=genai.types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=1536
            )
        )

    tbl = get_table()

    def _load_rows():
        return tbl.search().limit(50000).to_list()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            embed_fut = ex.submit(_embed)
            rows_fut  = ex.submit(_load_rows)
            try:
                embed_result = embed_fut.result(timeout=15)
            except concurrent.futures.TimeoutError:
                return {"error": "Embedding timed out."}
            query_vector = embed_result.embeddings[0].values
            all_rows = rows_fut.result(timeout=10)
    except Exception as e:
        return {"error": f"Search setup failed: {str(e)}"}

    if not all_rows:
        return {"semantic_results": [], "full_files": [], "keyword_hits": [], "connected_files": []}

    # ── Step 2: semantic vector search ───────────────────────────────────
    try:
        sem_rows = tbl.search(query_vector).limit(20).to_list()
    except Exception as e:
        sem_rows = []

    sem_results = []
    seen_files: dict = {}   # fname -> best confidence
    for r in sem_rows:
        dist = r.get("_distance", 0.5)
        conf = max(30, min(98, int((1 - dist) * 100)))
        meta = _parse_meta(r)
        topics = [_topic_display(t) for t in _sanitize_topics(meta.get("topics", []))]
        fname = r["source_file"]
        row: dict = {
            "content":        r["content"][:700],
            "source_file":    fname,
            "source_type":    r["source_type"],
            "confidence":     conf,
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

    # ── Step 3: full file content for top non-video semantic results ──────
    top_doc_files = []
    for r in sem_results:
        fn = r["source_file"]
        if r["source_type"] != "video" and fn not in top_doc_files:
            top_doc_files.append(fn)
        if len(top_doc_files) >= 3:
            break

    full_files = []
    for fname in top_doc_files:
        file_rows = sorted(
            [r for r in all_rows if r["source_file"] == fname],
            key=lambda r: r.get("chunk_index") or 0
        )
        if not file_rows:
            continue
        full_content = "\n\n---\n\n".join(r["content"] for r in file_rows)
        if len(full_content) > 6000:
            full_content = full_content[:6000] + "\n...[truncated — use get_file_content for complete text]"
        first = file_rows[0]
        meta  = _parse_meta(first)
        full_files.append({
            "source_file":    fname,
            "source_type":    first["source_type"],
            "full_content":   full_content,
            "chunk_count":    len(file_rows),
            "confidence":     seen_files.get(fname, 0),
            "topics":         [_topic_display(t) for t in _sanitize_topics(meta.get("topics", []))],
            "upload_context": meta.get("upload_context", ""),
        })

    # ── Step 4: keyword exact matching ───────────────────────────────────
    kw_set: set = set()
    for m in re.findall(r'\$?\d+(?:\.\d+)?', query):
        kw_set.add(m.lstrip("$").lower())
    for m in re.findall(r'\b[A-Z][a-z]{2,}\b', query):
        kw_set.add(m.lower())
    _stop = {
        'what','where','when','which','their','there','about','tell','show','find',
        'give','from','with','this','that','have','does','were','been','they','them',
        'know','like','make','your','second','brain','check','please','search','query',
        'looking','need','want','rate','rates','hour','hourly',
    }
    for w in query.lower().split():
        w = w.strip('?.,!"\' ')
        if len(w) >= 4 and w not in _stop:
            kw_set.add(w)

    kw_hits: dict = {}
    for kw in list(kw_set)[:7]:
        kw_lower = kw.lower()
        for r in all_rows:
            content = r.get("content", "")
            r_meta = _parse_meta(r)
            upload_ctx = r_meta.get("upload_context", "")
            searchable = (content + " " + upload_ctx).lower()
            if kw_lower not in searchable:
                continue
            fname = r["source_file"]
            if fname not in kw_hits:
                kw_entry: dict = {
                    "source_file":      fname,
                    "source_type":      r["source_type"],
                    "matched_keywords": set(),
                    "snippet":          "",
                    "confidence":       seen_files.get(fname, 0),
                }
                # For video files, include transcript + timestamp so MCP can show annotated clip info
                if r["source_type"] == "video":
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
            kw_hits[fname]["matched_keywords"].add(kw)
            if not kw_hits[fname]["snippet"]:
                idx = content.lower().find(kw_lower)
                if idx >= 0:
                    s = max(0, idx - 350)
                    e = min(len(content), idx + 350)
                    kw_hits[fname]["snippet"] = (
                        ("..." if s > 0 else "") + content[s:e] + ("..." if e < len(content) else "")
                    )
                elif upload_ctx:
                    kw_hits[fname]["snippet"] = f"[From upload label]: {upload_ctx}"

    def _kw_row(v: dict) -> dict:
        row = {
            "source_file":      v["source_file"],
            "source_type":      v["source_type"],
            "matched_keywords": sorted(v["matched_keywords"]),
            "snippet":          v["snippet"],
            "confidence":       v["confidence"],
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
        key=lambda x: -len(x["matched_keywords"])
    )[:8]

    # ── Step 5: topic-connected files (other modes not in semantic) ───────
    top_topics: set = set()
    for r in sem_results[:6]:
        for t in r.get("topics", []):
            top_topics.add(t.lower())

    connected: dict = {}
    for r in all_rows:
        fname = r["source_file"]
        if fname in seen_files:
            continue
        meta = _parse_meta(r)
        file_topics = {_topic_display(t).lower() for t in _sanitize_topics(meta.get("topics", []))}
        shared = top_topics & file_topics
        if shared and fname not in connected:
            connected[fname] = {
                "source_file":    fname,
                "source_type":    r["source_type"],
                "shared_topics":  sorted(list(shared))[:4],
                "content_preview": r["content"][:350],
                "upload_context": meta.get("upload_context", ""),
            }

    return {
        "query":            query,
        "semantic_results": sem_results,
        "full_files":       full_files,
        "keyword_hits":     kw_results,
        "connected_files":  list(connected.values())[:6],
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
    tbl = get_table()
    try:
        all_rows = tbl.search().limit(20000).to_list()
    except Exception as e:
        return {"error": f"Database query failed: {str(e)}"}

    entities, relationships = _aggregate_entities(all_rows)
    return {
        "entities": entities,
        "relationships": relationships,
        "total_entities": len(entities),
        "total_relationships": len(relationships),
    }


@app.post("/api/mcp/entity_search")
def mcp_entity_search(req: MCPEntitySearchRequest):
    """Find entities matching a query string, with their relationships and source files."""
    tbl = get_table()
    try:
        all_rows = tbl.search().limit(20000).to_list()
    except Exception as e:
        return {"error": f"Database query failed: {str(e)}"}

    entities, relationships = _aggregate_entities(all_rows)

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
    }


@app.get("/api/mcp/topics")
def mcp_get_topics():
    """Return all topics and their associated files for the MCP server."""
    tbl = get_table()
    try:
        docs = tbl.search().limit(10000).to_list()
    except Exception as e:
        return {"error": f"Database query failed: {str(e)}"}

    seen: dict = {}
    for d in docs:
        meta = json.loads(d["metadata"]) if isinstance(d.get("metadata"), str) else d.get("metadata", {})
        for t in _sanitize_topics((meta or {}).get("topics", [])):
            display = _topic_display(t)
            if display not in seen:
                seen[display] = set()
            seen[display].add(d["source_file"])

    topics = {k: sorted(list(v)) for k, v in seen.items()}
    return {"topics": topics}


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
    tbl = get_table()
    try:
        all_rows = tbl.search().limit(20000).to_list()
    except Exception as e:
        return {"error": f"Database query failed: {str(e)}"}

    # Collect all chunks for this file that have a transcript
    video_chunks = []
    for row in all_rows:
        if row.get("source_file", "").lower() != req.file.lower():
            continue
        if row.get("source_type", "") != "video":
            continue
        meta = json.loads(row["metadata"]) if isinstance(row.get("metadata"), str) else (row.get("metadata") or {})
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
        url = f"http://127.0.0.1:8000/clip?file={enc}&start={s}&end={e}"
        return {
            "clip_url": url,
            "preview_url": url.replace("/api/video-clip", "/clip"),
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
        url = f"http://127.0.0.1:8000/clip?file={enc}&start=0&end=60"
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
        url = f"http://127.0.0.1:8000/clip?file={enc}&start=0&end=60"
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

    enc = urllib.parse.quote(req.file, safe="")
    clip_url = f"http://127.0.0.1:8000/clip?file={enc}&start={int(clip_start)}&end={int(clip_end)}"
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
