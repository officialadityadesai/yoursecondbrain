<div align="center">

<img src="./Gradient%20lightning%20bolt%20design.png" alt="Your Second Brain" width="140" />

# Your Second Brain: The First Multimodal Knowledge Visualisation & Semantic Retrieval Framework

<div align="center">
        <img src="https://readme-typing-svg.herokuapp.com?font=Share+Tech+Mono&size=24&duration=2800&pause=850&color=7A86C8&center=true&vCenter=true&width=1200&lines=Multimodal+Knowledge+Retrieval+Reimagined;Semantic+Search+Across+Multimodal+Inputs+-+Text%2C+Images%2C+Video%2C+and+Documents;Visualise+Relationships+Between+Ideas+As+An+Interactive+Nodal+Network;Ingest+Once%2C+Query+Forever+Without+Re-uploading+Context;Retrieve+Trimmed+Video+Clips+From+Natural+Language+Queries;Cross-Modal+Intelligence%3A+Find+Connections+Across+All+Content+Types" alt="Typing Animation" />
</div>

<div style="background: linear-gradient(135deg, #0b0d12 0%, #161b25 50%, #1e2432 100%); border-radius: 14px; padding: 18px; margin: 18px auto; max-width: 980px; border: 1px solid rgba(122, 134, 200, 0.38); box-shadow: 0 0 24px rgba(89, 102, 171, 0.18);">
        <p>
                <a href="https://github.com/officialadityadesai/yoursecondbrain/tree/main">
                        <img src="https://img.shields.io/badge/Mode-Local-4A568F?style=for-the-badge&logo=icloud&logoColor=white&labelColor=111827" alt="Local First" />
                </a>
                <a href="https://ai.google.dev/gemini-api/docs/embeddings">
                        <img src="https://img.shields.io/badge/Embeddings-Gemini%20Embedding%202-6D77BA?style=for-the-badge&logo=google&logoColor=white&labelColor=111827" alt="Embeddings" />
                </a>
                <a href="https://lancedb.com">
                        <img src="https://img.shields.io/badge/Vector%20DB-LanceDB-58639B?style=for-the-badge&logo=databricks&logoColor=white&labelColor=111827" alt="LanceDB" />
                </a>
        </p>
        <p>
                <a href="https://www.python.org/downloads/">
                        <img src="https://img.shields.io/badge/Python-3.10%2B-4F5B8F?style=for-the-badge&logo=python&logoColor=white&labelColor=111827" alt="Python" />
                </a>
                <a href="https://vite.dev/">
                        <img src="https://img.shields.io/badge/Frontend-React%2019%20%2B%20Vite-636FB2?style=for-the-badge&logo=react&logoColor=white&labelColor=111827" alt="Frontend" />
                </a>
                <a href="https://fastapi.tiangolo.com/">
                        <img src="https://img.shields.io/badge/API-FastAPI-5B669E?style=for-the-badge&logo=fastapi&logoColor=white&labelColor=111827" alt="FastAPI" />
                </a>
                <a href="https://ffmpeg.org/">
                        <img src="https://img.shields.io/badge/Video Clipping-FFmpeg-8665A7?style=for-the-badge&logo=ffmpeg&logoColor=white&labelColor=111827" alt="Video Clipping" />
                </a>
        </p>
        <p>
                <a href="https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop">
                        <img src="https://img.shields.io/badge/MCP-Claude%20Integration-7B6AAE?style=for-the-badge&logo=anthropic&logoColor=white&labelColor=111827" alt="MCP Ready" />
                </a>
                <a href="https://opensource.org/license/mit">
                        <img src="https://img.shields.io/badge/License-MIT-5A5F7F?style=for-the-badge&labelColor=111827" alt="MIT License" />
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
- **Visualises** relationships, ideas, and entities in an interactive nodal knowledge graph
- **Retrieves** grounded answers and information only from your knowledge with neuron-level evidence
- **Integrates** with Claude MCP to find hidden information in files, retrieve trimmed timestamp-precise video clips, and get grounded answers from your knowledge base

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
- **🧠 Knowledge Graph Visualization**: See relationships between files and concepts in an interactive Obsidian-style nodal graph
- **🔍 Semantic Retrieval**: Find relevant content by meaning, not just keywords, across all file types
- **📝 Citation-Grounded Answers**: Chat interface returns answers with linked sources and citations
- **🤝 Claude MCP Integration**: The app becomes your second brain. Claude is your voice - search by description, retrieve timestamp-precise clips, trace connections, and get grounded answers from your entire knowledge base in chat
- **⚡ Token Optimization**: Intelligent chunking, context blending, and retrieval discipline to minimise token waste and limit usage
- **🎬 Video-Aware Retrieval**: Transcript timestamps enable precise evidence and clip generation
- **🔒 Private by Design**: Everything stays local - no re-uploading, no external indexing

</div>

This framework is adaptable across research, engineering docs, customer support, personal knowledge management, team collaboration, media analysis, and compliance-heavy workflows where persistent multimodal retrieval and explainable evidence matter.

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+
- Gemini API key: https://aistudio.google.com/app/apikey
- FFmpeg available on PATH (required for video clipping)

### Windows (Step-by-step setup + auto-start on login)

Goal: after setup, open **http://127.0.0.1:8000** any time and the app should be running after Windows login, without manually running `run.bat`.

1. Open PowerShell in the correct folder (Go to File Explorer, Click Local Disk c:, Click Users, then double click your windows username. Mine is "Adi Desai". That's where you should be in your terminal, which is usually where you are on default):

```powershell
git clone https://github.com/officialadityadesai/yoursecondbrain.git
cd yoursecondbrain
```

Important: always run project commands from your repo folder (example: `C:\Users\Adi Desai\yoursecondbrain`).

2. Install backend/frontend dependencies and build frontend (one-time):

PowerShell note: when running local scripts from the current folder, use the `./` or `.\` prefix.

```powershell
.\install.bat
```

3. Verify frontend build exists:

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

- Go to your `yoursecondbrain` folder.
- Open `.env.example` in a text editor.
- Go to Google AI Studio and create/copy your Gemini API key (make sure your Google account profile is 18+).
- Paste your key into the file as:

```env
GEMINI_API_KEY=your_key_here
```

- Save the file as `.env` in the same folder.
- Delete the old `.env.example` file to avoid confusion.

5. Start once now (quick check):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-background.ps1"
```

This starts the app for your current session only.
For automatic start after future logins/reboots, complete Step 6 (scheduled task).

Open **http://127.0.0.1:8000** and confirm the app loads.

6. Enable auto-start on login (one-time, Admin PowerShell):

Open **PowerShell as Administrator**, then run (REMEMBER TO REPLACE ADI DESAI WITH YOUR WINDOWS USERNAME):

```powershell
cd "C:\Users\Adi Desai\yoursecondbrain"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\create-startup-task.ps1"
```

Verify the scheduled task exists:

```powershell
Get-ScheduledTask -TaskName "MySecondBrain"
```

Now, you can just open **http://127.0.0.1:8000** whenever you want to use the app.

No manual `run.bat` will be needed for normal use anymore.
If you want to start it manually, run this in your project directly in your terminal:

```powershell
.\run.bat
```

#### Windows troubleshooting (common issues)

1. Error: `The argument 'scripts\create-startup-task.ps1' ... does not exist`

Cause: you ran the command from the wrong folder (for example `C:\Windows\System32`).

Fix:

```powershell
cd "$env:USERPROFILE\yoursecondbrain"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\create-startup-task.ps1"
```

2. Error: `Register-ScheduledTask : Access is denied`

Cause: task creation was run in a non-admin PowerShell window.

Fix: reopen PowerShell as Administrator and rerun Step 6.

3. Browser shows: `{"status":"frontend_not_built",...}`

Cause: frontend build files are missing.

Fix:

```powershell
cd yoursecondbrain\frontend
npm install
npm run build
cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-background.ps1"
```

4. Optional health checks:

```powershell
Test-Path ".\frontend\dist\index.html"
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

Expected:

- `Test-Path` returns `True`
- port `8000` is in `Listen` state

5. Error: `install.bat : The term 'install.bat' is not recognized...`

Cause: in PowerShell, commands in the current directory are not executed unless prefixed.

Fix:

```powershell
cd yoursecondbrain
.\install.bat
```

### macOS
```bash
git clone https://github.com/officialadityadesai/yoursecondbrain.git
cd yoursecondbrain

python3 -m venv .venv
source .venv/bin/activate

pip install -r backend/requirements.txt

cd frontend
npm install
npm run build
cd ..

cp .env.example .env
# add GEMINI_API_KEY=your_key_here

cd backend
uvicorn main:app --host 127.0.0.1 --port 8000
```

In a second terminal (optional frontend dev mode):
```bash
cd frontend
npm run dev
```

## 🤖 Claude MCP Integration

Your Second Brain is a native MCP server. Claude Desktop can retrieve, search, connect, and generate clips directly from your local workspace.

### Windows (Automated Setup)
```bash
cd yoursecondbrain
scripts\setup_mcp.bat
```

Restart Claude Desktop fully. 

Check if the mcp server connected by asking Claude "Do you have access to my second brain mcp?".

Your Second Brain is now available to talk to through Claude. 

IMPORTANT: You can only use the Your Second Brain MCP through Claude Desktop, not claude.ai in your browser.

### What Claude Can Do via MCP

- **Holistic multimodal search**: Find relevant content across all your files by semantic meaning
- **Entity & connection tracing**: Explore relationships between people, organizations, and concepts
- **Grounded answering**: Retrieve context and return answers with source citations
- **Video clip generation**: From a semantic query, generate timestamp-precise, playable clips
- **File exploration**: Browse your knowledge base structure, topics, and relationships

**Example MCP Workflows:**
- *"Search my knowledge base for documents about machine learning and show me how they connect"*
- *"Find a clip from my video where someone discusses authentication, and timestamp it"*
- *"What are the main entities mentioned across my research files, and which are most connected?"*
- *"Retrieve the top 3 documents relevant to this question and cite them in your answer"*

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
| Vector Database | LanceDB |
| Embeddings | Gemini Embedding 2 (1536-dim) |
| Ingestion | PyMuPDF, python-docx, OpenCV, FFmpeg |
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
        <img src="https://img.shields.io/badge/Built%20for-Persistent%20AI%20Memory-6E78BF?style=for-the-badge&labelColor=111827" alt="Persistent AI Memory" />
</div>
