import os
import glob
import uuid
import json
import re
import shutil
import hashlib
import subprocess
import tempfile
import concurrent.futures
import fitz # PyMuPDF
import docx # python-docx
from google import genai
from google.genai import types
from db import get_table
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("Warning: GEMINI_API_KEY not found in .env")
    gemini = None
else:
    gemini = genai.Client(api_key=GEMINI_API_KEY)

EMBEDDING_MODEL   = "gemini-embedding-2-preview"
EMBEDDING_DIM     = 1536
LLM_MODEL         = "gemini-3.1-flash-lite-preview"
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".avi", ".mkv"}

# ── Chunking constants ───────────────────────────────────────────────────────
CHUNK_SIZE      = 600   # target words per chunk
CHUNK_OVERLAP   = 100   # words of overlap between consecutive chunks
MIN_CHUNK_WORDS = 80    # minimum words to keep a trailing chunk
MAX_TOPIC_LEN = 56
MAX_TOPIC_WORDS = 6
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

import time

# ── Video chunking ────────────────────────────────────────────────────────────
VIDEO_CHUNK_DURATION = 120  # seconds per chunk
VIDEO_CHUNK_OVERLAP  = 5    # seconds of overlap between consecutive chunks
VIDEO_CHUNK_MIN_LEN  = 8    # skip trailing chunks shorter than this (seconds)

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

def _get_video_duration(file_path: str) -> float:
    """Return video duration in seconds using ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", file_path],
        capture_output=True, text=True, timeout=30
    )
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])

def _split_video(file_path: str) -> list[tuple[float, float, str]]:
    """
    Split video into overlapping 120-second chunks using ffmpeg.
    Returns list of (start_sec, end_sec, temp_path). Caller must delete temp files.
    """
    duration = _get_video_duration(file_path)
    chunks = []
    start = 0.0
    idx = 0
    while start < duration:
        end = min(start + VIDEO_CHUNK_DURATION, duration)
        if end - start < VIDEO_CHUNK_MIN_LEN and idx > 0:
            break  # skip tiny trailing fragment
        tmp = os.path.join(
            tempfile.gettempdir(),
            f"sensei_vc_{idx}_{uuid.uuid4().hex[:6]}.mp4"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", file_path,
             "-ss", str(start), "-to", str(end),
             "-c", "copy", tmp],
            capture_output=True, timeout=60
        )
        if os.path.isfile(tmp):
            chunks.append((start, end, tmp))
        if end >= duration:
            break
        start += VIDEO_CHUNK_DURATION - VIDEO_CHUNK_OVERLAP
        idx += 1
    return chunks


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping word-count chunks. Returns [text] unchanged if short enough."""
    words = text.split()
    if len(words) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        piece = " ".join(words[start:end])
        if len(piece.split()) >= MIN_CHUNK_WORDS:
            chunks.append(piece)
        if end >= len(words):
            break
        start = end - overlap
    return chunks if chunks else [text]

def robust_gemini_call(func, *args, **kwargs):
    max_retries = 6
    base_delay = 2
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) or "exhausted" in str(e).lower() or "quota" in str(e).lower():
                print(f"Gemini Rate Limit Hit. Waiting {base_delay}s... (Attempt {attempt+1}/{max_retries})")
                time.sleep(base_delay)
                base_delay *= 2
            else:
                raise e
    raise Exception("Max retries exceeded on Gemini API due to free-tier rate limits.")

def embed_text(text: str) -> list[float]:
    def _call():
        return gemini.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=[text],
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=EMBEDDING_DIM,
            ),
        )
    result = robust_gemini_call(_call)
    return result.embeddings[0].values

def strip_binary_content(text: str) -> str:
    """
    If text decoded from a .txt/.md file contains too many non-printable characters
    (binary garbage from files that use a text extension but contain binary data),
    extract only the readable ASCII text regions.
    """
    if not text:
        return text
    # Count printable ASCII characters (0x20-0x7E + standard whitespace)
    sample = text[:5000]  # Check first 5000 chars as a representative sample
    ascii_printable = sum(
        1 for c in sample
        if (0x20 <= ord(c) <= 0x7E) or c in '\n\r\t'
    )
    ratio = ascii_printable / max(len(sample), 1)
    if ratio >= 0.85:
        return text  # Content is clean enough
    # Extract only printable ASCII regions (sequences of at least 20 readable chars)
    import re
    readable_regions = re.findall(r'[ -~\t\n\r]{20,}', text)
    if not readable_regions:
        return "[File contains binary or encoded data that could not be extracted as text.]"
    return '\n'.join(readable_regions)


def normalize_upload_context(upload_context: str | None) -> str:
    if not upload_context:
        return ""
    return " ".join(upload_context.strip().split())

def contextualize_text(text: str, upload_context: str | None) -> str:
    context = normalize_upload_context(upload_context)
    if not context:
        return text
    return f"UPLOAD CONTEXT:\n{context}\n\nDOCUMENT CONTENT:\n{text}"

def blend_vectors(primary: list[float], secondary: list[float], secondary_weight: float = 0.18) -> list[float]:
    if not primary or not secondary or len(primary) != len(secondary):
        return primary
    w2 = max(0.0, min(0.45, float(secondary_weight)))
    w1 = 1.0 - w2
    return [(w1 * p) + (w2 * s) for p, s in zip(primary, secondary)]

def embed_media(file_path: str, mime_type: str) -> list[float]:
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    def _call():
        return gemini.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=types.Content(parts=[
                types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            ]),
            config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
        )
    result = robust_gemini_call(_call)
    return result.embeddings[0].values

def describe_content(file_path: str, mime_type: str) -> str:
    with open(file_path, "rb") as f:
        data = f.read()
    def _call():
        return gemini.models.generate_content(
            model=LLM_MODEL,
            contents=types.Content(parts=[
                types.Part.from_bytes(data=data, mime_type=mime_type),
                types.Part.from_text(text=
                    "Analyze this image for a personal knowledge base. Respond in four sections:\n\n"
                    "NAMED ENTITIES: List every specific name visible or inferable — "
                    "people (use their name if shown on screen, badge, slide, or caption), "
                    "organisations (company names, team names, logos), "
                    "tools and software (exact product/app names visible on screen), "
                    "and locations. If a name is clearly visible, use it exactly. "
                    "Do not say 'a person' or 'an application' — use the actual name if present.\n\n"
                    "VISUAL DESCRIPTION: Describe what is shown in detail — layout, content, objects, people, text visible.\n\n"
                    "CONTEXT & THEMES: What is the purpose or topic of this image? What concepts does it relate to?\n\n"
                    "CONNECTIONS: How might this connect to other documents about work, projects, people, or tools?"
                ),
            ]),
        )
    response = robust_gemini_call(_call)
    return response.text

def describe_video(file_path: str, mime_type: str) -> tuple[str, str]:
    """
    Uploads the video once, makes one Gemini call that returns both a content
    description and a timestamped transcript.
    Returns (description, transcript).
    Transcript timestamps are relative to the start of the clip (0:00 = clip start).
    """
    print(f"Uploading video {file_path} for processing...")

    # 1. Upload file using the File API
    try:
        video_file = gemini.files.upload(file=file_path)
    except TypeError:
        # Backward compatibility for SDK variants that take positional arg
        video_file = gemini.files.upload(file_path)

    # 2. Wait for processing to complete
    while True:
        video_file = gemini.files.get(name=video_file.name)
        state_name = getattr(getattr(video_file, "state", None), "name", None)
        if state_name == "PROCESSING":
            print("Waiting for video processing...")
            time.sleep(2)
        elif state_name == "ACTIVE":
            print("Video is ready.")
            break
        elif state_name == "FAILED":
            raise Exception("Video processing failed.")
        elif state_name is None:
            raise Exception("Video processing state unavailable from Gemini File API.")
        else:
            raise Exception(f"Unknown video processing state: {state_name}")

    try:
        def _call():
            return gemini.models.generate_content(
                model=LLM_MODEL,
                contents=[
                    video_file,
                    "Analyze this video for a personal knowledge base.\n\n"
                    "=== CONTENT ANALYSIS ===\n"
                    "NAMED ENTITIES: List every specific name mentioned or shown — "
                    "people who introduce themselves or are named, organisations mentioned by name, "
                    "tools and software shown on screen (use exact product names), projects or initiatives named. "
                    "Use real names, not generic descriptions.\n\n"
                    "SUMMARY: What is this video about? What is the main topic or purpose?\n\n"
                    "SPOKEN CONTENT: Transcribe or closely summarize what is said, noting who said what if people identify themselves.\n\n"
                    "KEY POINTS: The most important facts, decisions, or insights from this video.\n\n"
                    "VISUAL DETAILS: What is shown on screen — slides, interfaces, environments, demos.\n\n"
                    "=== TIMESTAMPED TRANSCRIPT ===\n"
                    "List every spoken line with its exact timestamp relative to the start of this clip.\n"
                    "Format each line as: [MM:SS] Name (if known): exact words spoken\n"
                    "Example: [00:05] Mark: \"Young people should learn to prompt AI...\"\n"
                    "Be precise — these timestamps will be used to cut exact video clips.\n"
                    "If no speech is detected, write: [00:00] No spoken content."
                ]
            )
        response = robust_gemini_call(_call)
        raw = response.text

        # Split response into description + transcript parts
        if "=== TIMESTAMPED TRANSCRIPT ===" in raw:
            parts = raw.split("=== TIMESTAMPED TRANSCRIPT ===", 1)
            description = parts[0].replace("=== CONTENT ANALYSIS ===", "").strip()
            transcript = parts[1].strip()
        else:
            description = raw
            transcript = ""

        return description, transcript
    finally:
        # Always attempt cleanup of remote uploaded file
        try:
            gemini.files.delete(name=video_file.name)
        except Exception as cleanup_err:
            print(f"Video file cleanup warning: {cleanup_err}")

def _assess_pdf_text_quality(raw_text: str, page_count: int) -> tuple[bool, str]:
    """
    Decide whether fitz-extracted PDF text is good enough to use directly.
    Returns (is_usable, reason_string).
    Criteria: enough text per page, high printability ratio, enough real words.
    """
    stripped = raw_text.strip() if raw_text else ""
    if not stripped:
        return False, "no text extracted"

    chars_per_page = len(stripped) / max(page_count, 1)
    if chars_per_page < 40:
        return False, f"too sparse ({int(chars_per_page)} chars/page — likely scanned)"

    # Printability ratio on a representative sample
    sample = stripped[:8000]
    printable = sum(1 for c in sample if c.isprintable() or c in "\n\r\t")
    ratio = printable / max(len(sample), 1)
    if ratio < 0.80:
        return False, f"low printability ({ratio:.0%}) — likely binary/encoded"

    # Enough real alphabetic words
    real_words = [w for w in stripped.split() if len(w) >= 3 and any(c.isalpha() for c in w)]
    if len(real_words) < 30:
        return False, "too few real words — likely symbol/image heavy"

    return True, "good"


def _light_clean_pdf(raw_text: str) -> str:
    """
    Minimal, non-AI cleanup of fitz-extracted text.
    Preserves every word, name, number, and attribution exactly.
    Only removes PDF rendering artifacts (page numbers, excess whitespace).
    """
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    # Form-feed page breaks → double newline
    text = text.replace("\x0c", "\n\n")
    # Lines that are purely numeric (page numbers)
    text = re.sub(r"^\s*\d+\s*$", "", text, flags=re.MULTILINE)
    # Collapse 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse multiple spaces (but not newlines)
    text = re.sub(r"[^\S\n]+", " ", text)
    return text.strip()


def _describe_scanned_pdf(filepath: str, upload_context: str | None) -> str:
    """
    Use Gemini's native PDF vision support to extract content from scanned /
    image-based PDFs where fitz returns little or no text.
    Instructs Gemini to preserve ALL verbatim detail — names, numbers, attributions.
    """
    with open(filepath, "rb") as f:
        pdf_bytes = f.read()
    ctx_note = f"\nDocument context provided by the user: {upload_context}\n" if upload_context else ""
    prompt = (
        "TASK: Verbatim transcript extraction. You are an OCR tool, NOT a summariser.\n\n"
        "RULES — follow every one without exception:\n"
        "1. Reproduce what each speaker said, word for word, with their name before each line.\n"
        "   Example: 'Dan: My rate is $70 an hour.' — write it exactly like that.\n"
        "2. NEVER group, merge, or combine what different people said into a single bullet or sentence.\n"
        "3. NEVER write a range like '$50–$70' unless a speaker literally said those words together.\n"
        "   If Dan says '$70' and Ruby says '$50', write each on its own line attributed to that person.\n"
        "4. NEVER paraphrase, summarise, or infer. If the text says 'fifty dollars', write 'fifty dollars'.\n"
        "5. Every specific number, name, dollar amount, date, or percentage must appear EXACTLY as spoken.\n"
        "6. Preserve the full conversation — do not skip any speaker turns or condense long sections.\n"
        "7. If you cannot read a word clearly, write [unclear] — do not guess or approximate.\n\n"
        "OUTPUT FORMAT: Speaker Name: exact words spoken. New line for each speaker turn.\n"
        "You may use headers for major topic shifts, but NEVER use bullet points to group statements.\n"
        + ctx_note
    )
    def _call():
        return gemini.models.generate_content(
            model=LLM_MODEL,
            contents=types.Content(parts=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                types.Part.from_text(text=prompt),
            ]),
        )
    try:
        response = robust_gemini_call(_call)
        return response.text
    except Exception as e:
        print(f"  Scanned PDF vision extraction failed: {e}")
        return "[PDF content could not be extracted — file may be encrypted or corrupted.]"

def extract_topics(text: str) -> list[str]:
    try:
        def _call():
            prompt = (
                "Analyze the following content and identify 6 to 10 concise conceptual tags for retrieval.\n"
                "Rules:\n"
                "- Return ONLY a comma-separated list.\n"
                "- Each tag must be 1-4 words and under 40 characters.\n"
                "- No sentences, explanations, disclaimers, or punctuation-heavy text.\n"
                "- Focus on key entities, technologies, concepts, domains.\n\n"
                f"{text}"
            )
            return gemini.models.generate_content(
                model=LLM_MODEL,
                contents=prompt
            )
        response = robust_gemini_call(_call)
        return sanitize_topics([t.strip() for t in response.text.split(",") if t.strip()])
    except Exception as e:
        print(f"Failed to extract topics: {e}")
        return []

def sanitize_topics(raw_topics: list[str]) -> list[str]:
    cleaned = []
    for topic in raw_topics or []:
        if not isinstance(topic, str):
            continue
        for part in topic.replace("\n", ",").split(","):
            t = part.strip(" -•\t\r\n\"'()[]{}")
            t = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in t.lower())
            t = " ".join(t.split())
            if not t:
                continue
            if len(t) > MAX_TOPIC_LEN or len(t.split()) > MAX_TOPIC_WORDS:
                continue
            if any(noise in t for noise in TOPIC_NOISE_SUBSTRINGS):
                continue
            if ":" in t:
                continue
            cleaned.append(t)
    return list(dict.fromkeys(cleaned))[:12]

def extract_topics_fallback(text: str) -> list[str]:
    # Conservative local fallback when LLM topic extraction fails (e.g., quota).
    stop_words = {
        "this", "that", "with", "from", "have", "your", "about", "were", "will", "would",
        "there", "their", "them", "they", "what", "when", "where", "which", "while", "into",
        "video", "summary", "details", "visual", "audio", "section", "points", "focus",
        "identified", "following", "conceptual", "themes", "entities", "metadata", "typical",
        "technical", "context", "associated", "such", "system", "operations", "processed",
        "analysis", "unavailable", "retried", "node", "file", "level", "retrieval", "software"
    }
    words = []
    for token in text.replace("\n", " ").split():
        clean = "".join(ch for ch in token.lower() if ch.isalpha())
        if 4 <= len(clean) <= 24 and clean not in stop_words:
            words.append(clean)

    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1

    top = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:12]
    return sanitize_topics([w for w, _ in top])

def extract_entities(text: str) -> dict:
    """
    Extract named entities and relationships from document text using Gemini Flash.
    Returns {"entities": [...], "relationships": [...]} or empty dicts on failure.
    Non-blocking — any error returns empty result rather than failing the upload.
    """
    if not gemini or not text or not text.strip():
        return {"entities": [], "relationships": []}
    # Truncate to ~4000 words to keep API cost and latency low
    words = text.split()
    truncated = " ".join(words[:4000])
    prompt = (
        "You are a precise information extractor. Extract named entities and ONLY explicitly stated relationships from the document below.\n"
        "Return ONLY valid JSON — no explanation, no markdown code fences.\n\n"
        'Format: {"entities": [{"name": "...", "type": "person|organisation|tool|concept", '
        '"description": "one phrase under 12 words"}], '
        '"relationships": [{"from": "EntityName", "relationship": "verb phrase", "to": "EntityName"}]}\n\n'
        "ENTITY RULES:\n"
        "- person: real named individuals only (first name, full name, or clear nickname). NOT job titles alone.\n"
        "- organisation: named companies, teams, groups, projects, platforms, institutions\n"
        "- tool: named software, APIs, technologies, frameworks, services\n"
        "- concept: named methodologies, processes, strategies — only if given a specific name in the text\n"
        "- Only extract entities that are EXPLICITLY and CLEARLY named in the text\n"
        "- Do NOT invent, guess, or infer entity names not present in the text\n\n"
        "RELATIONSHIP RULES — READ CAREFULLY:\n"
        "- Only include a relationship if the text DIRECTLY AND EXPLICITLY states it\n"
        "- The relationship verb must come from the actual words used in the text, not your interpretation\n"
        "- Do NOT infer, assume, or imply relationships (e.g. proximity in text is NOT a relationship)\n"
        "- Do NOT use verbs like 'leads', 'manages', 'oversees', 'owns' unless that exact word or a direct synonym is literally written in the document about those two entities\n"
        "- Both 'from' and 'to' must be entity names already in your entities list\n"
        "- If you are not 100% certain a relationship is explicitly stated, omit it\n"
        "- It is better to return 0 relationships than to include a guessed one\n\n"
        "QUANTITY:\n"
        "- Extract 3–15 entities. Only include entities that appear meaningfully in the text.\n"
        "- Extract 0–8 relationships. Quality over quantity — only ironclad, explicit ones.\n"
        "- If no clear named entities exist, return {\"entities\": [], \"relationships\": []}\n\n"
        f"Document:\n{truncated}"
    )
    try:
        def _call():
            return gemini.models.generate_content(model=LLM_MODEL, contents=prompt)
        response = robust_gemini_call(_call)
        raw = response.text.strip()
        # Strip markdown code fences if model wraps output in them
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        # Validate and sanitise
        entities = result.get("entities", [])
        relationships = result.get("relationships", [])
        if not isinstance(entities, list):
            entities = []
        if not isinstance(relationships, list):
            relationships = []
        valid_types = {"person", "organisation", "organization", "tool", "concept"}
        cleaned_entities = []
        for e in entities:
            if not isinstance(e, dict) or not e.get("name"):
                continue
            t = e.get("type", "concept").lower().replace("organization", "organisation")
            if t not in valid_types:
                t = "concept"
            cleaned_entities.append({
                "name": str(e["name"]).strip(),
                "type": t,
                "description": str(e.get("description", "")).strip()
            })
        cleaned_rels = []
        entity_names_lower = {e["name"].lower() for e in cleaned_entities}
        for r in relationships:
            if not isinstance(r, dict):
                continue
            frm = str(r.get("from", "")).strip()
            rel = str(r.get("relationship", "")).strip()
            to  = str(r.get("to", "")).strip()
            # Only include if both ends exist in our entity list
            if frm and rel and to and frm.lower() in entity_names_lower and to.lower() in entity_names_lower:
                cleaned_rels.append({"from": frm, "relationship": rel, "to": to})
        print(f"  -> Entities: {len(cleaned_entities)} extracted, {len(cleaned_rels)} relationships")
        return {"entities": cleaned_entities, "relationships": cleaned_rels}
    except Exception as ex:
        print(f"  Entity extraction failed (non-critical): {ex}")
        return {"entities": [], "relationships": []}


def derive_video_topics(filename: str, description: str, upload_context: str | None = None) -> list[str]:
    # Ensure video nodes consistently get meaningful topic neighbors.
    topic_source = contextualize_text(description, upload_context)
    topics = extract_topics(topic_source)
    if not topics:
        topics = extract_topics_fallback(topic_source)
    if not topics:
        base = os.path.splitext(filename)[0]
        tokens = [t.strip().lower() for t in base.replace("-", " ").replace("_", " ").split() if len(t.strip()) >= 3]
        topics = sanitize_topics(tokens)
    return sanitize_topics(topics)

def _process_video_single(filepath, filename, mime, upload_context, content_hash):
    """Original single-description approach (fallback when ffmpeg is not available)."""
    transcript = ""
    try:
        description, transcript = describe_video(filepath, mime)
    except Exception as e:
        print(f"Video analysis fallback for {filename}: {e}")
        description = "Video content analysis unavailable for this upload attempt. This file is indexed for retrieval by filename and any future reprocessing."
    try:
        video_vector = embed_media(filepath, mime)
    except Exception as e:
        print(f"Video byte-embedding failed for {filename}, falling back to text embedding: {e}")
        video_vector = embed_text(contextualize_text(description, upload_context))
    if upload_context:
        ctx_vec = embed_text(upload_context)
        video_vector = blend_vectors(video_vector, ctx_vec)
    video_topics = derive_video_topics(filename, description, upload_context=upload_context)
    entity_source = f"User context: {upload_context}\n\n{description}" if upload_context else description
    entities = extract_entities(entity_source)
    meta = {"topics": video_topics}
    if transcript:
        meta["transcript"] = transcript
    insert_document(description, video_vector, "video", filename, meta=meta,
                    upload_context=upload_context, content_hash=content_hash, entities=entities)


def _process_video_chunked(filepath, filename, mime, upload_context, content_hash):
    """
    Chunked video processing: splits into 120-second segments, describes and embeds each chunk,
    stores timestamp_start/timestamp_end in metadata for scene-level search and clip serving.
    Falls back to single-description if chunking fails.
    """
    print(f"  Splitting {filename} into {VIDEO_CHUNK_DURATION}s chunks for scene search...")
    try:
        chunks = _split_video(filepath)
    except Exception as e:
        print(f"  Video split failed for {filename}: {e} — falling back to single processing")
        _process_video_single(filepath, filename, mime, upload_context, content_hash)
        return

    if not chunks:
        print(f"  No chunks generated — falling back to single processing")
        _process_video_single(filepath, filename, mime, upload_context, content_hash)
        return

    print(f"  -> {len(chunks)} chunk(s) for {filename}")

    # Step 1: describe and embed each chunk sequentially
    chunk_records: list[tuple[int, float, float, str, str, list]] = []  # idx, t_start, t_end, description, transcript, vec
    for idx, (t_start, t_end, chunk_path) in enumerate(chunks):
        try:
            description, transcript = describe_video(chunk_path, "video/mp4")
            try:
                vec = embed_media(chunk_path, "video/mp4")
            except Exception:
                vec = embed_text(contextualize_text(description, upload_context))
            if upload_context:
                ctx_vec = embed_text(upload_context)
                vec = blend_vectors(vec, ctx_vec)
            chunk_records.append((idx, t_start, t_end, description, transcript, vec))
            print(f"  -> Chunk {idx}: {int(t_start)//60}:{int(t_start)%60:02d}–{int(t_end)//60}:{int(t_end)%60:02d}")
        except Exception as e:
            print(f"  Chunk {idx} ({t_start:.0f}s) failed: {e}")
        finally:
            try:
                os.remove(chunk_path)
            except Exception:
                pass

    if not chunk_records:
        print(f"  All chunks failed for {filename} — nothing to insert")
        return

    # Step 2: extract entities once across all chunk descriptions (saves Gemini quota)
    full_text = "\n\n".join(desc for _, _, _, desc, _, _ in chunk_records)
    entity_source = f"User context: {upload_context}\n\n{full_text}" if upload_context else full_text
    entities = extract_entities(entity_source)

    # Step 3: insert all chunks with timestamp + transcript metadata
    for idx, t_start, t_end, description, transcript, vec in chunk_records:
        topics = derive_video_topics(filename, description, upload_context=upload_context)
        meta = {"topics": topics, "timestamp_start": t_start, "timestamp_end": t_end}
        if transcript:
            meta["transcript"] = transcript
        insert_document(
            description, vec, "video", filename,
            chunk_index=idx,
            meta=meta,
            upload_context=upload_context,
            content_hash=content_hash if idx == 0 else None,
            entities=entities,
        )


def insert_document(content, embedding, source_type, source_file, chunk_index=-1, meta=None, upload_context: str | None = None, content_hash: str | None = None, entities: dict | None = None):
    if meta is None: meta = {}
    upload_context = normalize_upload_context(upload_context)
    if upload_context:
        meta["upload_context"] = upload_context
    if content_hash:
        meta["content_hash"] = content_hash
    # Store entity graph in every chunk's metadata so search results carry entity context
    if entities and (entities.get("entities") or entities.get("relationships")):
        meta["entities"] = entities
    
    topics = meta.get("topics")
    if not topics:
        topics = extract_topics(contextualize_text(content, upload_context))
    if not topics:
        topics = extract_topics_fallback(contextualize_text(content, upload_context))
    meta["topics"] = sanitize_topics(topics)
    
    tbl = get_table()
    doc_id = str(uuid.uuid4())
    tbl.add([{"id": doc_id, "content": content, "vector": embedding, 
             "source_type": source_type, "source_file": source_file, 
             "chunk_index": chunk_index, "metadata": json.dumps(meta)}])
    print(f"Inserted {source_file} as {doc_id} with topics {topics}")

def process_file(filepath: str, upload_context: str | None = None, content_hash: str | None = None):
    if not gemini:
        raise Exception("My Second Brain Warning: You have not properly saved your Gemini API Key in the .env file! Please add it and restart the run.cmd script.")

    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()
    upload_context = normalize_upload_context(upload_context)
    
    print(f"Processing {filename}...")
    if ext not in SUPPORTED_EXTENSIONS:
        raise Exception(f"Unsupported file type: {ext}. Supported types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
    
    # Clean up existing entries for this file to avoid duplicates
    try:
        tbl = get_table()
        existing = tbl.search().limit(20000).to_list()
        ids_to_delete = [r.get("id") for r in existing if r.get("source_file") == filename and r.get("id")]
        for row_id in ids_to_delete:
            tbl.delete(f"id = '{row_id}'")
        if ids_to_delete:
            print(f"Cleared {len(ids_to_delete)} previous entries for {filename}")
    except Exception as e:
        print(f"No previous entries cleared (or error): {e}")
    
    try:
        if ext in (".txt", ".md"):
            # Try UTF-8 first, fall back through common Windows encodings
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    with open(filepath, "r", encoding=enc) as f:
                        text = f.read()
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                # Last resort: read bytes and replace undecodable characters
                with open(filepath, "rb") as f:
                    text = f.read().decode("utf-8", errors="replace")
            # Strip binary garbage if the file contains non-printable content
            # (e.g. files with .md/.txt extension that are actually binary containers)
            text = strip_binary_content(text)
            chunks = chunk_text(text)
            print(f"  -> {len(chunks)} chunk(s) for {filename}")
            # Run topics, entities, and all chunk embeddings in parallel
            embed_sources = [contextualize_text(c, upload_context) for c in chunks]
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                topics_fut   = ex.submit(lambda: extract_topics(contextualize_text(text, upload_context)) or extract_topics_fallback(text))
                entities_fut = ex.submit(extract_entities, text)
                vec_futs     = [ex.submit(embed_text, src) for src in embed_sources]
                topics   = topics_fut.result()
                entities = entities_fut.result()
                vectors  = [f.result() for f in vec_futs]
            for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
                insert_document(chunk, vec, "text", filename,
                                chunk_index=i, meta={"topics": topics},
                                upload_context=upload_context,
                                content_hash=content_hash if i == 0 else None,
                                entities=entities)

        elif ext == ".pdf":
            doc = fitz.open(filepath)
            page_count = len(doc)
            raw_text = "\x0c".join([page.get_text() for page in doc])
            is_usable, reason = _assess_pdf_text_quality(raw_text, page_count)
            if is_usable:
                print(f"  PDF text quality: {reason} — using raw text directly (exact attributions preserved)")
                clean_text = _light_clean_pdf(raw_text)
            else:
                print(f"  PDF text quality: {reason} — using Gemini vision extraction")
                clean_text = _describe_scanned_pdf(filepath, upload_context)
            chunks = chunk_text(clean_text)
            print(f"  -> {len(chunks)} chunk(s) for {filename}")
            # Run topics, entities, and all chunk embeddings in parallel
            embed_sources = [contextualize_text(c, upload_context) for c in chunks]
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                topics_fut   = ex.submit(lambda: extract_topics(contextualize_text(clean_text, upload_context)) or extract_topics_fallback(clean_text))
                entities_fut = ex.submit(extract_entities, clean_text)
                vec_futs     = [ex.submit(embed_text, src) for src in embed_sources]
                topics   = topics_fut.result()
                entities = entities_fut.result()
                vectors  = [f.result() for f in vec_futs]
            for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
                insert_document(chunk, vec, "pdf", filename,
                                chunk_index=i, meta={"topics": topics},
                                upload_context=upload_context,
                                content_hash=content_hash if i == 0 else None,
                                entities=entities)

        elif ext == ".docx":
            doc = docx.Document(filepath)
            text = "\n".join([para.text for para in doc.paragraphs])
            chunks = chunk_text(text)
            print(f"  -> {len(chunks)} chunk(s) for {filename}")
            # Run topics, entities, and all chunk embeddings in parallel
            embed_sources = [contextualize_text(c, upload_context) for c in chunks]
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                topics_fut   = ex.submit(lambda: extract_topics(contextualize_text(text, upload_context)) or extract_topics_fallback(text))
                entities_fut = ex.submit(extract_entities, text)
                vec_futs     = [ex.submit(embed_text, src) for src in embed_sources]
                topics   = topics_fut.result()
                entities = entities_fut.result()
                vectors  = [f.result() for f in vec_futs]
            for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
                insert_document(chunk, vec, "docx", filename,
                                chunk_index=i, meta={"topics": topics},
                                upload_context=upload_context,
                                content_hash=content_hash if i == 0 else None,
                                entities=entities)

        elif ext in (".png", ".jpg", ".jpeg", ".webp"):
            mime = "image/png" if ext == ".png" else "image/jpeg"
            if ext == ".webp": mime = "image/webp"
            description = describe_content(filepath, mime)
            entity_source = f"User context: {upload_context}\n\n{description}" if upload_context else description
            entities = extract_entities(entity_source)
            image_vec = embed_media(filepath, mime)
            if upload_context:
                ctx_vec = embed_text(upload_context)
                image_vec = blend_vectors(image_vec, ctx_vec)
            insert_document(description, image_vec, "image", filename, upload_context=upload_context, content_hash=content_hash, entities=entities)
            
        elif ext in (".mp4", ".mov", ".avi", ".mkv"):
            mime = "video/mp4"
            if ext == ".mov": mime = "video/quicktime"
            if ext == ".avi": mime = "video/x-msvideo"
            if _ffmpeg_available():
                _process_video_chunked(filepath, filename, mime, upload_context, content_hash)
            else:
                print(f"  ffmpeg not found — processing {filename} as single unit (install ffmpeg for scene-level search)")
                _process_video_single(filepath, filename, mime, upload_context, content_hash)
            
        else:
            print(f"Skipping unsupported file type: {ext}")
            
    except Exception as e:
        print(f"Error processing {filename}: {e}")
        raise
