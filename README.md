# My Second Brain 🧠

>  The first truly private Second Brain that understands everything - PDFs, videos, images, etc. Drop files into the app, then ask anything about your knowledge through Claude MCP - get cited answers, synthesised information, and trimmed video clips. No cloud. No subscriptions. No re-uploading.

---

## The Problem

Every AI tool has the same flaw: **it forgets everything the moment the chat ends.**

You re-upload the same files. You re-explain the same context. You switch between your notes app, your AI, and your file system constantly. Your knowledge is fragmented across a dozen tools, none of them talk to each other, none of them remember anything, and none of them understand your videos or images (multimodal inputs).

**My Second Brain fixes this.**

---

## What It Does

One local app that ingests everything you throw at it - PDFs, Word docs, images, MP4s, text files, then embeds them all into the same vector space using **Gemini Embedding 2**, and gives you:

- A **living knowledge graph** that maps every concept and connection across your files just like a second brain
- A **chat interface** that answers questions by synthesising across all your content
- **Claude Desktop integration** via MCP — ask Claude anything about your saved knowledge, get trimmed video clips, cited sources, and deep answers without uploading a single file ever again
- **Zero cloud, zero fees, zero data leaving your machine**

---

## Features

### 🗂 True Multimodal Ingestion
PDFs, DOCX, TXT, MD, PNG, JPG, MP4, MOV, AVI, MKV — all embedded into the same 1536-dimensional vector space via Gemini Embedding 2. Not summarised. Not OCR'd into plain text. Embedded natively, as the model understands them.

### 🕸 Interactive Knowledge Graph
Every file becomes a node. Every shared concept becomes a link. The graph updates live as you add files — built on `react-force-graph-2d` with physics-based layout, real-time search, and node zoom animations.

### ⚡ Auto-Sync via Watchdog
Drop a file into `brain_data/` and it's ingested automatically. No upload button. No manual trigger. Watchdog detects the file, chunks it, embeds it, and updates the graph — silently, in the background.

### 🎬 AI Video Clip Trimming
Ask Claude "show me what Mark Cuban said about hiring" and it returns a trimmed, playable clip — precise to the relevant 10–30 second window. No CapCut. No manual scrubbing. FFmpeg + transcript-density scoring finds the exact moment.

### 🤖 Claude Desktop MCP Integration
My Second Brain runs as an MCP server alongside Claude Desktop. Claude can search your knowledge base, retrieve file contents, find connections between files, identify entities, and generate video clips — all without you uploading anything to Claude.

### 💬 Built-in Chat Interface
Ask questions directly in the app. Powered by Gemini (free tier) or your own Claude account via OAuth. Answers are cited, synthesised across sources, and grounded in what you actually saved.

### 🔒 100% Local & Private
LanceDB runs entirely on your machine. No Supabase. No Pinecone. No cloud vector store. Your data never leaves your laptop.

---

## Stack

| Layer | Technology |
|---|---|
| Embeddings | Gemini Embedding 2 (`gemini-embedding-2-preview`, 1536-dim) |
| Vector DB | LanceDB (local, on-disk) |
| Backend | FastAPI + Uvicorn |
| File Watching | Watchdog |
| PDF parsing | PyMuPDF |
| Video processing | FFmpeg + OpenCV + Gemini vision transcripts |
| Frontend | React 19 + Vite + Tailwind CSS |
| Knowledge Graph | react-force-graph-2d |
| MCP Server | FastMCP (stdio) |
| Chat | Gemini Flash / Claude OAuth |

---

## Quick Start

### Prerequisites
- Python 3.10+
- Node.js 18+
- A free [Gemini API Key](https://aistudio.google.com/app/apikey)
- FFmpeg on your system PATH *(only needed for video files)*

### Install & Run (Windows)

```bash
# 1. Clone the repo
git clone https://github.com/your-username/my-second-brain.git
cd my-second-brain

# 2. Run the installer (installs Python + Node deps, builds frontend)
install.bat

# 3. Set up your API key
# Rename .env.example to .env and paste your Gemini API key inside

# 4. Start the app
run.bat

# 5. Open in browser
# http://127.0.0.1:8000
```

### Auto-start on Login (Optional)
```bash
# Runs the app silently at every Windows login — no terminal needed
powershell -File scripts\create-startup-task.ps1
```

---

## Claude Desktop MCP Setup

Connect My Second Brain directly to Claude Desktop so Claude can search your knowledge, retrieve clips, and cite sources — without you doing anything.

```bash
# Run once after install
scripts\setup_mcp.bat

# Then: right-click Claude Desktop in system tray → Quit → Reopen
```

Claude will show **My Second Brain** in its tools list. From any chat:

> *"What did I save about fundraising?"*
> *"Show me the clip where Mark Cuban talks about hiring"*
> *"What connects my meeting notes to my research papers?"*

---

## Environment Variables

```env
# Required
GEMINI_API_KEY=your_key_here

# Optional — Claude OAuth (connect your Claude account as chat provider)
CLAUDE_OAUTH_CLIENT_ID=...
CLAUDE_OAUTH_CLIENT_SECRET=...
CLAUDE_OAUTH_REDIRECT_URI=http://127.0.0.1:8000/api/auth/claude/callback
CLAUDE_MODEL=claude-sonnet-4-20250514
```

---

## How Ingestion Works

```
File dropped into brain_data/
        ↓
Watchdog detects change
        ↓
File type router:
  Text/PDF/DOCX → chunk (600 words, 100 overlap) → embed_text()
  Image         → raw bytes → Gemini multimodal embed
  Video         → FFmpeg chunks (120s, 5s overlap) → Gemini vision transcript → embed
        ↓
Gemini Embedding 2 → 1536-dim vector
        ↓
LanceDB (local) → stored with topics, metadata, content hash
        ↓
Knowledge graph updates live
```

All file types land in the same vector space. Semantic search works across PDFs, images, and videos simultaneously.

---

## Supported File Types

| Type | Formats |
|---|---|
| Documents | `.pdf` `.docx` `.txt` `.md` |
| Images | `.png` `.jpg` `.jpeg` `.webp` |
| Video | `.mp4` `.mov` `.avi` `.mkv` |

---

## vs. Everything Else

| | My Second Brain | ChatGPT / Claude | Notion AI | Traditional RAG |
|---|---|---|---|---|
| Remembers across sessions | ✅ | ❌ | ❌ | ✅ |
| Multimodal (video + images) | ✅ | ❌ (upload each time) | ❌ | ❌ |
| Runs locally / private | ✅ | ❌ | ❌ | Varies |
| Video clip trimming | ✅ | ❌ | ❌ | ❌ |
| Knowledge graph | ✅ | ❌ | ❌ | ❌ |
| MCP / Claude integration | ✅ | ❌ | ❌ | ❌ |
| Zero monthly cost | ✅ | ❌ | ❌ | ❌ |
| Auto file watching | ✅ | ❌ | ❌ | ❌ |

---

## Project Structure

```
my-second-brain/
├── backend/
│   ├── main.py           # FastAPI server, all API routes
│   ├── ingest.py         # File processing, chunking, embedding
│   ├── db.py             # LanceDB table management
│   ├── watcher.py        # Watchdog file observer
│   └── mcp_server.py     # Claude Desktop MCP server
├── frontend/
│   └── src/
│       ├── App.jsx
│       └── components/
│           ├── KnowledgeGraph.jsx   # Force-directed graph
│           ├── ChatInterface.jsx    # In-app chat
│           ├── FileManager.jsx      # Upload + manage files
│           └── PreviewModal.jsx     # Node preview
├── brain_data/           # Drop your files here
├── scripts/
│   ├── setup_mcp.bat     # Claude Desktop MCP setup
│   └── create-startup-task.ps1
├── install.bat
├── run.bat
└── .env.example
```

---

## Contributing

PRs welcome. If you're adding a new file type, the place to start is `backend/ingest.py` — add your extension to `SUPPORTED_EXTENSIONS` and write a handler following the existing pattern.

---

## License

MIT — free to use, fork, and build on.

---

<p align="center">Built to make your knowledge actually useful.</p>
