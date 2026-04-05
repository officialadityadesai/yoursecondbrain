# Your Second Brain — Windows Setup Blueprint

Paste this file into Claude Code and say:

**"Read this file and follow every step exactly to set up the app on my Windows machine. Do everything yourself — only pause when you need my Gemini API key."**

Claude Code will handle everything. You just answer the one question it asks (your Gemini API key) and you're done.

---

## Instructions for Claude Code

You are a developer setup assistant. The user has given you this file and wants you to set up Your Second Brain end-to-end on their Windows machine so it works immediately after you finish.

Do not ask for permission before each step. Do not explain what you are about to do before doing it. Run everything sequentially, tell the user what you are doing in plain English as you go, and only pause when you need their Gemini API key. Handle all errors yourself — diagnose and fix them without asking the user unless it is truly unresolvable.

---

### Step 1 — Clone the Repo

Clone the repo into the user's home directory:

```
git clone https://github.com/officialadityadesai/yoursecondbrain.git "%USERPROFILE%\yoursecondbrain"
cd "%USERPROFILE%\yoursecondbrain"
```

If git is not installed, tell the user to download it from https://git-scm.com/download/win, install it with default settings, then restart Claude Code and try this blueprint again.

---

### Step 2 — Check Prerequisites

Check that the following are installed:

```
python --version
node --version
ffmpeg -version
```

**Python** (3.10 or higher required):
- If missing, tell the user to download it from https://python.org/downloads
- Critical: they must tick **"Add Python to PATH"** during install
- After installing, restart Claude Code and continue

**Node.js** (18 or higher required):
- If missing, tell the user to download it from https://nodejs.org and install with default settings
- After installing, restart Claude Code and continue

**FFmpeg** (needed for video clip trimming — optional but recommended):
- Install via winget if available:
  ```
  winget install ffmpeg
  ```
- If winget is not available, skip FFmpeg — the app still works, video trimming just falls back to full video playback

---

### Step 3 — Install Backend Dependencies

```
cd "%USERPROFILE%\yoursecondbrain\backend"
pip install -r requirements.txt
```

If pip fails, try `pip3 install -r requirements.txt`. If that also fails, try `python -m pip install -r requirements.txt`.

---

### Step 4 — Install Frontend Dependencies and Build

```
cd "%USERPROFILE%\yoursecondbrain\frontend"
npm install
npm run build
```

This compiles the React app into `frontend/dist/` which the backend serves at http://127.0.0.1:8000.

---

### Step 5 — API Key Setup

Ask the user:

> "Please go to https://aistudio.google.com/app/apikey, create a free API key, and paste it here."

Once they provide the key:
1. Copy `.env.example` to `.env` in the repo root
2. Replace `your_gemini_api_key_here` with their actual key

The `.env` file should look like:
```
GEMINI_API_KEY=AIza...their_key_here
```

---

### Step 6 — Set Up Auto-Start on Login

This makes the backend start silently every time Windows starts, so the app is always available at http://127.0.0.1:8000 without the user needing to run anything.

Run from the repo root:

```
powershell -ExecutionPolicy Bypass -File "%USERPROFILE%\yoursecondbrain\scripts\create-startup-task.ps1"
```

This registers a Windows Scheduled Task called `MySecondBrain` that starts the backend at login and also starts it immediately right now.

If PowerShell execution policy blocks it, run this first then retry:

```
powershell -Command "Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force"
```

Wait up to 15 seconds for the backend to start, then verify it is running:

```
powershell -Command "try { Invoke-WebRequest -Uri http://127.0.0.1:8000/api/files -UseBasicParsing -TimeoutSec 10 | Out-Null; Write-Host 'Backend is running' } catch { Write-Host 'Backend not yet up' }"
```

If the backend is not up after 15 seconds, check the error log:

```
type "%USERPROFILE%\yoursecondbrain\scripts\backend.err.log"
```

---

### Step 7 — Set Up Claude Desktop MCP Integration

This wires My Second Brain into Claude Desktop so the user can ask Claude anything about their saved knowledge.

Run from the repo root:

```
python "%USERPROFILE%\yoursecondbrain\scripts\setup_mcp.py"
```

If that fails, try:

```
"%USERPROFILE%\yoursecondbrain\scripts\setup_mcp.bat"
```

After it completes, tell the user:

> "Now fully quit Claude Desktop — right-click its icon in the system tray at the bottom-right of your screen and click **Quit**. Closing the window is not enough. Then reopen Claude Desktop."

---

### Step 8 — Verify Everything Works

1. Open http://127.0.0.1:8000 in a browser — the knowledge graph UI should load
2. Open Claude Desktop, start a new chat, and look for the hammer icon (🔨) near the message box — click it and **My Second Brain** should be listed with tools available

If the UI shows `{"status":"frontend_not_built"}`, re-run Step 4.

If the MCP tools are not showing in Claude Desktop, make sure Claude Desktop was fully quit (system tray → Quit) and reopened, not just the window closed.

---

### Step 9 — Tell the User How to Use It

Once everything is running, tell the user this in plain English:

---

**You're all set. Here's how to use My Second Brain:**

**Adding files to your knowledge base:**
Drop any PDF, Word doc, image (PNG/JPG), or video (MP4/MOV) into the `brain_data\` folder inside the repo. The app detects it automatically and indexes it within a minute — no upload button needed. You can also drag and drop files directly in the web UI at http://127.0.0.1:8000.

**Using the web app (http://127.0.0.1:8000):**
- The graph shows all your files as connected nodes — drag, zoom, and click any node to preview it
- Use the **Knowledge Base** panel to manage files
- Use the **Agent** panel to chat with your knowledge directly in the browser
- Use the **Brain Dump** panel to write notes that get indexed automatically

**Using Claude Desktop (the most powerful way):**
In any Claude Desktop chat, just ask:
- *"What do my notes say about [topic]?"*
- *"Summarise everything I have on [subject]"*
- *"Show me the clip where [person] talks about [topic]"*
- *"What connects [file A] to [file B]?"*

Claude will search your knowledge base, cite sources, and even trim video clips to the exact relevant moment.

**The app runs automatically on login** — every time you start your computer, the backend starts silently in the background. Just open http://127.0.0.1:8000 or use Claude Desktop and it is ready.

---
