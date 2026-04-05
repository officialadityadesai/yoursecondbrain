<div align="center">

<img src="./Gradient%20lightning%20bolt%20design.png" alt="Your Second Brain" width="140" />

# Your Second Brain: The First Multimodal Knowledge Visualisation & Semantic Retrieval Framework

<div align="center">
        <img src="https://readme-typing-svg.herokuapp.com?font=Share+Tech+Mono&size=24&duration=2800&pause=850&color=7A86C8&center=true&vCenter=true&width=1200&lines=Multimodal+Knowledge+Retrieval+Reimagined;Semantic+Search+Across+Multimodal+Inputs+-+Text%2C+Images%2C+Video%2C+and+Documents;Visualise+Relationships+Between+Ideas+As+An+Interactive+Nodal+Network;Ingest+Once%2C+Query+Forever+Without+Re-uploading+Context;Retrieve+Trimmed+Video+Clips+From+Natural+Language+Queries;Cross-Modal+Intelligence%3A+Find+Connections+Across+All+Content+Types" alt="Typing Animation" />
</div>

<div style="background: linear-gradient(135deg, #0b0d12 0%, #161b25 50%, #1e2432 100%); border-radius: 14px; padding: 18px; margin: 18px auto; max-width: 980px; border: 1px solid rgba(122, 134, 200, 0.38); box-shadow: 0 0 24px rgba(89, 102, 171, 0.18);">
        <p>
                <a href="https://github.com/officialadityadesai/yoursecondbrain/tree/main">
                        <img src="https://img.shields.io/badge/Mode-Local-6D77BA?style=for-the-badge&logo=icloud&logoColor=white&labelColor=111827" alt="Local First" />
                </a>
                <a href="https://ai.google.dev/gemini-api/docs/embeddings">
                
                        <img src="https://img.shields.io/badge/Embeddings-Gemini%20Embedding%202-6D77BA?style=for-the-badge&logo=google&logoColor=white&labelColor=111827" alt="Embeddings" />
                </a>
                <a href="https://lancedb.com">
                        <img src="https://img.shields.io/badge/Vector%20DB-LanceDB-6D77BA?style=for-the-badge&logo=databricks&logoColor=white&labelColor=111827" alt="LanceDB" />
                </a>
        </p>
        <p>
                <a href="https://www.python.org/downloads/">
                        <img src="https://img.shields.io/badge/Python-3.10%2B-6D77BA?style=for-the-badge&logo=python&logoColor=white&labelColor=111827" alt="Python" />
                </a>
                <a href="https://vite.dev/">
                        <img src="https://img.shields.io/badge/Frontend-React%2019%20%2B%20Vite-6D77BA?style=for-the-badge&logo=react&logoColor=white&labelColor=111827" alt="Frontend" />
                </a>
                <a href="https://fastapi.tiangolo.com/">
                        <img src="https://img.shields.io/badge/API-FastAPI-6D77BA?style=for-the-badge&logo=fastapi&logoColor=white&labelColor=111827" alt="FastAPI" />
                </a>
                <a href="https://ffmpeg.org/">
                        <img src="https://img.shields.io/badge/Video Clipping-FFmpeg-6D77BA?style=for-the-badge&logo=ffmpeg&logoColor=white&labelColor=111827" alt="Video Clipping" />
                </a>
        </p>
        <p>
                <a href="https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop">
                        <img src="https://img.shields.io/badge/MCP-Claude%20Integration-6D77BA?style=for-the-badge&logo=anthropic&logoColor=white&labelColor=111827" alt="MCP Ready" />
                </a>
                <a href="https://opensource.org/license/mit">
                        <img src="https://img.shields.io/badge/License-MIT-6D77BA?style=for-the-badge&labelColor=111827" alt="MIT License" />
                </a>
        </p>
</div>

</div>

<div align="center">
        <div style="width: 100%; height: 2px; margin: 24px 0; background: linear-gradient(90deg, transparent, #6E78BF, transparent);"></div>
</div>

## 🎯 The Problem

**Hitting your AI/API usage limits mid-conversation and losing context about everything is a problem you can't avoid...** Until now. You have a confusing dump of **files scattered everywhere**: PDFs, Word docs, images, videos, notes, etc. Every time you want to ask an AI a question or request about those files, you re-upload the same context, prompts, and files over and over again. Your scarce token budget bleeds away. You can't see how the files relate. You risk hallucinations and context rot with every message you send. You're trapped in a cycle of re-uploading, re-explaining, and re-sending.

**With "Your Second Brain", these will be problems of the past.**

## 💡 Core Idea

**Upload your files once**. 
The framework:
- **Centralises** them in a unified multimodal local vector database
- **Understands** them semantically across all modalities (text, images, video, documents, etc)
- **Lets you steer memory formation** with upload context labels that shape embeddings and retrieval intent from day one
- **Visualises** relationships, ideas, and entities in an interactive nodal knowledge graph
- **Protects memory quality** with duplicate-name and duplicate-content blocking before ingest
- **Self-heals old knowledge** using startup backfills that enrich missing entities and video transcripts automatically
- **Retrieves** grounded answers and information only from your knowledge with neuron-level evidence
- **Integrates** with Claude MCP to find hidden information in files, retrieve trimmed timestamp-precise video clips, and get grounded answers from your knowledge base
- **Supports dual chat intelligence** with both Gemini and connected Claude account modes in-app

This is a **generously feature-rich free framework that you can adapt** to your projects, workflows, product development, knowledge management, customer support, personal learning, and team collaboration initiatives. In practice, this means local, unlimited ingestion, a unified multimodal semantic space, node-focused knowedge visualisation, token-efficient retrieval assembly, and Claude MCP as a native memory interface with source-based answers.

## 🏗️ How It Works

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#1F2535', 'primaryTextColor': '#D7DBF4', 'primaryBorderColor': '#6E78BF', 'lineColor': '#7E6FAF', 'secondaryColor': '#181C27', 'tertiaryColor': '#12151C', 'background': '#0B0D12'}}}%%
flowchart LR
                Q[Your Files & Questions]:::entry

                subgraph S1[Stage 1: Ingestion]
                        U1[Upload Panel / brain_data folder]
                        U2[File Router by Type]
                        U3[Parse & Chunk]
                        U1 --> U2 --> U3
                end

                subgraph S2[Stage 2: Understanding]
                        E1[Semantic Embeddings]
                        E2[Entity & Topic Extraction]
                        E3[Video Transcription]
                        E1 --> E2
                        E1 --> E3
                end

                subgraph S3[Stage 3: Memory]
                        K1[Vector Index]
                        K2[Knowledge Graph]
                        K3[Citation Metadata]
                        K1 --> K2
                        K1 --> K3
                end

                subgraph S4[Stage 4: Retrieval & Action]
                        R1[Semantic Search]
                        R2[Chat with Citations]
                        R3[Claude MCP Tools]
                        R1 --> R2
                        R1 --> R3
                end

                Q --> U1
                U3 --> E1
                E2 --> K1
                K2 --> R1

                classDef entry fill:#2A3150,stroke:#8B79C4,color:#E6E9FF,stroke-width:2px;
```

**Process:**
1. Upload files (documents, images, videos) once.
2. System parses them, generates semantic embeddings, and extracts entities, topics, and ideas.
3. Everything is indexed and connected in a multimodal nodal knowledge graph.
4. Ask questions, and get answers grounded in your actual files with citations.
5. Use Claude MCP to extend it into your current AI operations.

## ✨ Core Capabilities

<div style="background: linear-gradient(135deg, #12151f 0%, #1d2230 100%); border-radius: 14px; padding: 22px; margin-top: 10px; border-left: 4px solid #6E78BF;">

- **🔄 Multimodal Ingestion**: Text, PDFs, Word docs, images, and videos - one unified pipeline
- **🧭 Context-Steered Ingestion**: Attach optional upload context per batch so the system indexes files with your intended meaning alongside raw content
- **🛡️ Memory Integrity Controls**: Duplicate filename and exact content-hash blocking keeps your graph clean and non-redundant
- **⛓️ Queue-Safe Processing**: Serialised ingestion with queued/processing/done states prevents rate-limit spikes and keeps ingestion stable at scale
- **🧠 Knowledge Graph Visualization**: See relationships between files and concepts in an interactive Obsidian-style nodal graph
- **🕸️ Entity Relationship Intelligence**: Auto-extracted people, organisations, tools, concepts, and explicit relationships are linked across files
- **🔍 Semantic Retrieval**: Find relevant content by meaning and keyword signals across all file types
- **🧾 Holistic Retrieval Engine**: Semantic search, keyword exact matches, full-file reconstruction, and topic-neighbour expansion in one retrieval flow
- **📝 Citation-Grounded Answers**: Chat interface returns answers with linked sources and citations
- **🤝 Claude MCP Integration**: The app becomes your second brain. Claude is your voice - search by description, retrieve timestamp-precise clips, trace connections, and get grounded answers from your entire knowledge base in chat
- **🔁 Self-Healing Enrichment**: On startup, the system backfills missing entities/transcripts for previously ingested files
- **⚡ Token Optimization**: Intelligent chunking, context blending, and retrieval discipline to minimise token waste and limit usage
- **🎬 Video Retrieval**: Automated transcript timestamps, semantic line matching, and allows you to retrieve trimmed clips from longer video uploads to find specific moments with natural language
- **🔒 Private by Design**: Everything stays local - no re-uploading, no external indexing

</div>

This framework is adaptable across business IP, SOPs, research, studying, customer support, personal knowledge management, team collaboration, media analysis, and compliance-heavy workflows where persistent multimodal retrieval and explainable evidence matter.

### 🧩 Additional Power Features Already In The App

- **Interactive graph control surface**: tune node distance, center force, repel force, link thickness, and label visibility in real time
- **Persistent graph state**: layout and graph settings are saved and restored between sessions
- **Shareable node deep-links**: open specific knowledge nodes directly via URL path/query links
- **Rich multimodal preview layer**: PDF, DOCX (HTML conversion), image, and video previews in a single modal workspace
- **Evidence explorer UX**: citations open source previews with highlighted quotes for rapid verification

### ⚠️ Important: Where Advanced Query Features Run

The most advanced query capabilities — semantic video clip finding, holistic retrieval, and deep entity tracing — run through **Claude Desktop + MCP**, not the built-in Gemini chat pane. Setup instructions are in the Quick Start and Manual Installation sections below.

## 🚀 Quick Start

The easiest way to get set up — Claude Code installs everything, configures the app, and wires up Claude Desktop for you automatically. You just paste one prompt and answer one question.

### What you need first

**A Claude plan that includes Claude Code** — Pro, Max, Team, or Enterprise. Claude Code is not available on the free plan.

If you're not sure which plan you have, go to [claude.ai](https://claude.ai) and check your account. To upgrade, visit [claude.ai/upgrade](https://claude.ai/upgrade).

### Get Claude Code

Claude Code works inside VS Code — install it as an extension:

1. Open VS Code
2. Click the Extensions icon in the left sidebar (or press `Ctrl+Shift+X` on Windows / `Cmd+Shift+X` on Mac)
3. Search for **Claude Code**
4. Click **Install**
5. Once installed, click the Claude Code icon in the sidebar and sign in with your Anthropic account

### Run the setup prompt

Open Claude Code, start a new conversation, and paste this prompt exactly:

```
Clone this repo: https://github.com/officialadityadesai/yoursecondbrain — then read the CLAUDE-CODE-BLUEPRINT.md file in the root of the cloned repo and follow every step in it exactly to set up the app on my computer. Do everything yourself — I should only need to paste my Gemini API key when you ask for it. Walk me through anything you need from me in plain English.
```

Claude Code will:
- Clone the repo
- Detect your OS (Windows or macOS) and tailor everything to it
- Install Python, Node.js, and FFmpeg if they're missing
- Install all dependencies and build the app
- Pause once to ask for your free Gemini API key, with step-by-step instructions on where to get it
- Write your config, start the app, and open it in your browser
- Set up auto-start so the app runs silently on every login
- Configure Claude Desktop MCP if you have it installed (or walk you through installing it)

When it's done, open **http://127.0.0.1:8000** — your second brain is ready.

### Claude Desktop MCP (quick start)

Claude Desktop is a separate free app that connects to your knowledge base so you can ask Claude questions about your files directly in chat. Claude Code sets this up automatically during the prompt above — but if you need to do it manually:

> **Important:** This requires the [Claude Desktop app](https://claude.ai/download), not the Claude website. The website cannot connect to local MCP servers.

**Windows:**

1. Make sure the backend is running at `http://127.0.0.1:8000`
2. Open Claude Desktop → **Settings → Developer** → **Edit Config**
3. Add the following (replace `YourName` with your Windows username — run `where python` in PowerShell to find your exact Python path):

```json
{
  "mcpServers": {
    "my-second-brain": {
      "command": "C:\\Users\\YourName\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
      "args": ["C:\\Users\\YourName\\yoursecondbrain\\backend\\mcp_server.py"]
    }
  }
}
```

4. Save the file. Right-click the Claude icon in the system tray → **Quit** (closing the window is not enough). Reopen Claude Desktop.
5. Start a new chat — look for the hammer icon (🔨) near the message box. Click it and **My Second Brain** will be listed.

**macOS:**

1. Make sure the backend is running at `http://127.0.0.1:8000`
2. Open Claude Desktop → **Settings → Developer** → **Edit Config**
3. Find your paths first — run these in Terminal:

```bash
which python3   # e.g. /Users/YourName/yoursecondbrain/.venv/bin/python
pwd             # run from inside the yoursecondbrain folder
```

4. Add the following (substitute your actual paths):

```json
{
  "mcpServers": {
    "my-second-brain": {
      "command": "/Users/YourName/yoursecondbrain/.venv/bin/python",
      "args": ["/Users/YourName/yoursecondbrain/backend/mcp_server.py"]
    }
  }
}
```

5. Save the file. Press **Cmd+Q** to fully quit Claude Desktop, then reopen it.
6. Start a new chat — look for the hammer icon (🔨) near the message box. Click it and **My Second Brain** will be listed.

> If your config already has other entries, keep them — only add the `mcpServers` block, don't replace the whole file.

---

## 🛠️ Manual Installation

Prefer to set things up yourself? Follow the steps below for your OS.

### Prerequisites

- Python 3.10+
- Node.js 18+
- Gemini API key: https://aistudio.google.com/app/apikey
- FFmpeg (required for video clipping)

### Install prerequisites (Windows)

1. Install Python 3.10+:
        - Download from: https://www.python.org/downloads/windows/
        - During install, tick **Add Python to PATH**.

2. Install Node.js 18+:
        - Download LTS from: https://nodejs.org/en/download

3. Install FFmpeg:

```powershell
winget install Gyan.FFmpeg
```

4. Restart PowerShell, then verify:

```powershell
python -V
node -v
ffmpeg -version
```

### Windows (step-by-step)

Goal: after setup, open **http://127.0.0.1:8000** any time after login — no manual start needed.

1. Open PowerShell and clone the repo:

```powershell
cd "$env:USERPROFILE"
git clone https://github.com/officialadityadesai/yoursecondbrain.git
cd .\yoursecondbrain
```

2. Install all dependencies and build the frontend:

```powershell
.\install.bat
```

3. Verify the frontend build exists:

```powershell
Test-Path ".\frontend\dist\index.html"
```

If it returns `False`, run:

```powershell
cd .\frontend
npm run build
cd ..
```

4. Add your Gemini API key:

```powershell
Copy-Item .env.example .env
```

Open `.env` in a text editor and set:

```env
GEMINI_API_KEY=your_key_here
```

5. Start the app:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-background.ps1"
```

Open **http://127.0.0.1:8000** and confirm it loads.

6. Enable auto-start on login (one-time, Admin PowerShell):

```powershell
cd "$env:USERPROFILE\yoursecondbrain"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\create-startup-task.ps1"
```

Verify:

```powershell
Get-ScheduledTask -TaskName "MySecondBrain"
```

### Windows — Claude Desktop MCP

1. Make sure the backend is running at `http://127.0.0.1:8000`
2. Run the MCP setup script — it writes the config automatically:

```powershell
python scripts\setup_mcp.py
```

3. Right-click the Claude icon in the system tray → **Quit**. Reopen Claude Desktop.
4. Start a new chat and look for the hammer icon (🔨) — **My Second Brain** will be listed.

#### Windows troubleshooting

**Error: `Register-ScheduledTask : Access is denied`**
Reopen PowerShell as Administrator and rerun step 6.

**Browser shows `{"status":"frontend_not_built"}`**

```powershell
cd "$env:USERPROFILE\yoursecondbrain\frontend"
npm install && npm run build
cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-background.ps1"
```

**Error: `install.bat is not recognized`**

```powershell
cd "$env:USERPROFILE\yoursecondbrain"
.\install.bat
```

---

### Install prerequisites (macOS)

1. Install Homebrew if not already installed: https://brew.sh

2. Install Python, Node.js, and FFmpeg:

```bash
brew install python node ffmpeg
```

3. Verify:

```bash
python3 -V && node -v && ffmpeg -version
```

### macOS (step-by-step)

1. Open Terminal and clone the repo:

```bash
cd "$HOME"
git clone https://github.com/officialadityadesai/yoursecondbrain.git
cd yoursecondbrain
```

2. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. Install backend dependencies:

```bash
python3 -m pip install -r backend/requirements.txt
```

4. Install frontend dependencies and build:

```bash
cd frontend
npm install
npm run build
cd ..
```

5. Add your Gemini API key:

```bash
cp .env.example .env
```

Edit `.env` and set:

```env
GEMINI_API_KEY=your_key_here
```

6. Start the backend:

```bash
cd backend
UVICORN_HOST=127.0.0.1 UVICORN_PORT=8000 python -m uvicorn main:app
```

Open **http://127.0.0.1:8000** and confirm it loads.

7. Enable auto-start on login:

```bash
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$HOME/Library/LaunchAgents/com.yoursecondbrain.backend.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>Label</key>
        <string>com.yoursecondbrain.backend</string>
        <key>ProgramArguments</key>
        <array>
                <string>/bin/zsh</string>
                <string>-lc</string>
                <string>cd "$HOME/yoursecondbrain/backend"; source "$HOME/yoursecondbrain/.venv/bin/activate"; UVICORN_HOST=127.0.0.1 UVICORN_PORT=8000 python -m uvicorn main:app</string>
        </array>
        <key>RunAtLoad</key>
        <true/>
        <key>KeepAlive</key>
        <true/>
        <key>WorkingDirectory</key>
        <string>$HOME/yoursecondbrain/backend</string>
        <key>StandardOutPath</key>
        <string>$HOME/yoursecondbrain/scripts/macos-backend.out.log</string>
        <key>StandardErrorPath</key>
        <string>$HOME/yoursecondbrain/scripts/macos-backend.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)"/com.yoursecondbrain.backend 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.yoursecondbrain.backend.plist"
launchctl enable "gui/$(id -u)"/com.yoursecondbrain.backend
launchctl kickstart -k "gui/$(id -u)"/com.yoursecondbrain.backend
```

Verify:

```bash
launchctl print "gui/$(id -u)/com.yoursecondbrain.backend" | grep state
lsof -i :8000
```

### macOS — Claude Desktop MCP

1. Make sure the backend is running at `http://127.0.0.1:8000`
2. Run the MCP setup script — it writes the config automatically:

```bash
.venv/bin/python scripts/setup_mcp.py
```

3. Press **Cmd+Q** to fully quit Claude Desktop, then reopen it.
4. Start a new chat and look for the hammer icon (🔨) — **My Second Brain** will be listed.

#### macOS troubleshooting

**Error: `python3: command not found`**

```bash
brew install python
```

**Error: `node: command not found`**

```bash
brew install node
```

**Browser shows `{"status":"frontend_not_built"}`**

```bash
cd "$HOME/yoursecondbrain/frontend"
npm install && npm run build
cd ../backend
UVICORN_HOST=127.0.0.1 UVICORN_PORT=8000 python -m uvicorn main:app
```

## 🧩 Supported Content Types

| Category | Formats |
|---|---|
| Documents | .pdf .docx .txt .md |
| Images | .png .jpg .jpeg .webp |
| Videos | .mp4 .mov .avi .mkv |

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Backend API | FastAPI + Uvicorn |
| Auth & Token Storage | Claude OAuth + OS keyring |
| Vector Database | LanceDB |
| Embeddings | Gemini Embedding 2 (1536-dim) |
| Ingestion | PyMuPDF, python-docx, Mammoth, OpenCV, FFmpeg |
| File Watcher | watchdog |
| Frontend | React 19 + Vite + Axios + React Markdown |
| Graph Engine | react-force-graph-2d |
| MCP Server | mcp + FastMCP |

## 🗂️ Project Layout

```text
yoursecondbrain/
├── backend/
│   ├── main.py
│   ├── ingest.py
│   ├── db.py
│   ├── watcher.py
│   └── mcp_server.py
├── frontend/
│   └── src/components/
│       ├── ChatInterface.jsx
│       ├── FileManager.jsx
│       ├── KnowledgeGraph.jsx
│       └── PreviewModal.jsx
├── brain_data/
├── scripts/
├── install.bat
└── run.bat
```

## 📄 License

MIT

---

<div align="center" style="margin-top: 16px;">
        <a href="https://www.instagram.com/officialadityadesai/">
                <img src="https://img.shields.io/badge/Owner%20%26%20Creator-Aditya%20Desai-6E78BF?style=for-the-badge&logo=instagram&logoColor=white&labelColor=111827" alt="Owner and Creator: Aditya Desai" />
        </a>
</div>
