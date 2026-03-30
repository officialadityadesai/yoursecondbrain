# My Second Brain — Claude Code Setup Blueprint

Paste this entire file into Claude Code and say: **"Follow this blueprint to set up My Second Brain for me."**

Claude Code will handle everything — installing dependencies, building the frontend, configuring the API key, setting up auto-start on login, and wiring up Claude Desktop MCP. You just answer the one question it asks (your Gemini API key) and you're done.

---

## Instructions for Claude Code

You are a developer setup assistant. The user has cloned the My Second Brain repo and wants you to set it up end-to-end on their Windows machine so it works immediately after you finish.

Do not ask for permission for each step. Run everything sequentially, tell the user what you're doing in plain English, and only pause when you need their Gemini API key. Handle all errors yourself.

---

### Step 1 — Check Prerequisites

Check that the following are installed and usable:
- Python 3.10 or higher (`python --version`)
- Node.js 18 or higher (`node --version`)

If Python is missing, tell the user to download it from https://python.org/downloads — make sure they tick "Add Python to PATH" during install — then ask them to restart Claude Code and run this blueprint again.

If Node.js is missing, tell the user to download it from https://nodejs.org — then ask them to restart Claude Code and run this blueprint again.

If both are present, continue.

---

### Step 2 — Install Backend Dependencies

Navigate to the `backend/` folder and install Python packages:

```
cd backend
pip install -r requirements.txt
```

If pip fails, try `pip3 install -r requirements.txt`. Tell the user what's happening.

---

### Step 3 — Install Frontend Dependencies

Navigate to the `frontend/` folder and install Node packages:

```
cd frontend
npm install
```

This installs React, Vite, Tailwind CSS v4 (native Vite plugin), and all other frontend dependencies.

---

### Step 4 — Build the Frontend

Still in the `frontend/` folder, build the production bundle that the backend serves:

```
npm run build
```

This compiles the React app into `frontend/dist/` which FastAPI serves at http://127.0.0.1:8000.

---

### Step 5 — API Key Setup

Ask the user:

> "Please go to https://aistudio.google.com/app/apikey, create a free API key, and paste it here."

Once they provide the key:
1. Copy `.env.example` to `.env` at the repo root
2. Replace `your_gemini_api_key_here` with their actual key

The `.env` file should look like:
```
GEMINI_API_KEY=AIza...their_key_here
```

---

### Step 6 — Set Up Auto-Start on Login

This makes the app start automatically every time Windows starts, so the user never has to run a bat file again. The app will always be available at http://127.0.0.1:8000.

Run this from the repo root:

```
powershell -ExecutionPolicy Bypass -File scripts\create-startup-task.ps1
```

This registers a Windows scheduled task called `MySecondBrain` that silently starts the backend at login and keeps it running in the background. It also starts the backend immediately right now so the user doesn't need to reboot.

If powershell execution policy blocks it, run:
```
powershell -Command "Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force"
```
Then retry.

---

### Step 7 — Set Up Claude Desktop MCP Integration

This wires My Second Brain into Claude Desktop so the user can ask Claude anything about their saved knowledge.

Run from the repo root:

```
scripts\setup_mcp.bat
```

Or if that fails:
```
python scripts\setup_mcp.py
```

This writes the MCP server config into Claude Desktop's config file automatically.

After it completes, tell the user:
> "Fully quit Claude Desktop — right-click its icon in the system tray at the bottom-right of your screen and click Quit. Then reopen Claude Desktop normally."

---

### Step 8 — Verify Everything Works

1. Open http://127.0.0.1:8000 in a browser. The My Second Brain knowledge graph UI should load.
2. Open Claude Desktop, start a new chat, and look for the hammer/tools icon (⚒) near the message box. Click it — "My Second Brain" should be listed.

If http://127.0.0.1:8000 doesn't load, check if the backend is running by looking at `scripts/backend.err.log` for errors.

---

### Step 9 — Tell the User How to Use It

Once everything is running, tell the user this in plain language:

---

**You're all set! Here's how to use My Second Brain:**

**Adding knowledge:**
Drop any PDF, image (PNG/JPG), Word doc, or video (MP4/MOV) into the `brain_data/` folder inside this repo. The app detects it automatically and adds it to your knowledge graph within a minute — no upload button needed.

**Using the app (http://127.0.0.1:8000):**
- The graph shows all your files as nodes — drag, zoom, click any node to preview it
- Click **Knowledge Base** (bottom toolbar) to manage your files
- Click **Agent** (bottom toolbar) to chat with your knowledge directly in the app

**Using Claude Desktop (the best way):**
In any Claude Desktop chat, just ask:
- *"What do my notes say about [topic]?"*
- *"Summarise everything I have on [subject]"*
- *"Show me the clip where [person] talks about [topic]"*
- *"What connects [file A] to [file B]?"*

Claude will search your knowledge base, cite sources, and even trim video clips to the exact relevant moment.

**The app runs automatically on login** — every time you start your computer, the backend starts silently in the background. Just open http://127.0.0.1:8000 or ask Claude Desktop and it's ready.

---
