# Your Second Brain — Claude Code Setup Blueprint

Paste this file into Claude Code and say:

**"Read this file and follow every step exactly to set up Your Second Brain on my computer. Walk me through anything you need from me in plain English."**

---

## Instructions for Claude Code

You are a setup assistant. The user wants to get Your Second Brain running end-to-end on their machine. The app now runs entirely inside Docker — no Python, Node.js, or FFmpeg installs required. Docker Desktop is the only prerequisite.

Do not ask for permission before each step. Run everything sequentially. Tell the user what you are doing in plain English as you go. Handle errors yourself — diagnose and fix them without asking the user unless it is truly unresolvable. The only times you should pause and wait for the user are when Docker Desktop needs to be installed (Step 1) and when you need their Gemini API key (Step 3).

---

### Step 1 — Check for Docker Desktop

Run the following to check if Docker is already installed:

```bash
docker --version && docker compose version
```

**If Docker is installed:** tell the user "Docker is already installed, moving on." and skip to Step 2.

**If Docker is not installed:** tell the user:

> "The only thing you need to install yourself is Docker Desktop. It's free and takes about 3 minutes.
>
> 1. Go to https://www.docker.com/products/docker-desktop/ and download the version for your OS
> 2. Run the installer (it will ask to restart your computer — that's normal, go ahead)
> 3. After restart, open Docker Desktop from your Start menu or Applications folder
> 4. Wait until the whale icon appears in your system tray (Windows) or menu bar (Mac) and shows a green 'Running' status
> 5. Come back here and tell me when it's ready"

Wait for the user to confirm Docker Desktop is running before continuing.

---

### Step 2 — Clone the Repo

Clone the repository into the user's home directory:

**Windows (PowerShell):**
```powershell
git clone https://github.com/officialadityadesai/yoursecondbrain.git "$env:USERPROFILE\yoursecondbrain"
```

**macOS / Linux:**
```bash
git clone https://github.com/officialadityadesai/yoursecondbrain.git "$HOME/yoursecondbrain"
```

If git is not installed:
- Windows: tell the user to download from https://git-scm.com/download/win, install with default settings, then restart Claude Code and retry
- macOS: running `git` will trigger a prompt to install Xcode Command Line Tools — tell the user to click Install, wait for it, then continue

After cloning, navigate into the repo:

**Windows:**
```powershell
cd "$env:USERPROFILE\yoursecondbrain"
```

**macOS / Linux:**
```bash
cd "$HOME/yoursecondbrain"
```

---

### Step 3 — Set Up the API Key

Ask the user:

> "I need one thing from you: a free Gemini API key. Here's how to get it:
>
> 1. Go to https://aistudio.google.com/app/apikey
> 2. Sign in with a Google account
> 3. Click 'Create API key'
> 4. Copy the key and paste it here"

Once they provide the key, copy the example env file and write their key into it:

**Windows:**
```powershell
Copy-Item .env.example .env
```

**macOS / Linux:**
```bash
cp .env.example .env
```

Then open `.env` and set the key — the file should contain:

```
GEMINI_API_KEY=their_key_here
```

Write this using whatever file editing tool is available. Confirm to the user: "API key saved."

---

### Step 4 — Start the App

Run the app in detached (background) mode so it starts automatically on every login:

```bash
docker compose up -d
```

Docker will pull the pre-built image from Docker Hub on first run. This takes 2–5 minutes depending on internet speed. Tell the user: "Downloading the app — this will take a few minutes on first run, nothing to worry about."

Wait for the command to complete, then verify the app is running:

```bash
docker compose ps
```

The container should show status `running` or `Up`. Then verify the API responds:

**Windows:**
```powershell
Start-Sleep -Seconds 10
Invoke-WebRequest -Uri http://localhost:8000/api/files -UseBasicParsing | Select-Object StatusCode
```

**macOS / Linux:**
```bash
sleep 10 && curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/files
```

You should see status code `200`. If not, check logs:

```bash
docker compose logs --tail=50
```

Look for errors and fix them before continuing. Common issues:
- Port 8000 already in use: check if another process is using it and stop it
- `.env` file missing or malformed: verify it exists in the repo root with a valid API key

Once `200` is confirmed, tell the user: "The app is running. You can open it at http://localhost:8000 — go ahead and check it loads."

---

### Step 5 — Set Up Claude Desktop MCP (Optional but Recommended)

This wires the knowledge base into Claude Desktop so the user can ask Claude questions about their files in natural language, retrieve video clips, and get cited answers — all without re-uploading anything.

First check if Claude Desktop is installed by asking:

> "Do you have the Claude Desktop app installed? (This is different from the Claude website — it's a desktop app you download from claude.ai/download.) Yes or no?"

**If they say no:**

> "You can download Claude Desktop for free from https://claude.ai/download. Install it, open it, and sign in with your Anthropic account. Let me know when it's open."

**If they say yes (or once installed):** run the MCP setup script. This requires Python on the host machine (outside Docker). Check if Python is available:

```bash
python --version
```

or

```bash
python3 --version
```

**If Python is available:** run the setup script using whichever python command worked:

```bash
python scripts/setup_mcp.py
```

or

```bash
python3 scripts/setup_mcp.py
```

The script will detect the OS automatically and write the correct config to Claude Desktop's config file.

**If Python is not available on the host:** tell the user to manually edit the Claude Desktop config file.

For Windows, the config file is at: `%APPDATA%\Claude\claude_desktop_config.json`
For macOS, the config file is at: `~/Library/Application Support/Claude/claude_desktop_config.json`
For Linux, the config file is at: `~/.config/Claude/claude_desktop_config.json`

Open that file (create it if it doesn't exist) and add or merge in:

```json
{
  "mcpServers": {
    "my-second-brain": {
      "command": "python",
      "args": ["PATH_TO_REPO/backend/mcp_server.py"],
      "env": { "MSB_BACKEND_URL": "http://127.0.0.1:8000" }
    }
  }
}
```

Replace `PATH_TO_REPO` with the full path to the cloned repo. If there are already other entries in the config, keep them — only add the `my-second-brain` entry inside `mcpServers`.

After the script runs or the config is written, tell the user:

> "Now you need to fully quit Claude Desktop and reopen it — this is important:
> - Windows: right-click the Claude icon in the system tray at the bottom-right of your screen → Quit. Then reopen Claude Desktop.
> - Mac: press Cmd+Q to quit. Then reopen Claude Desktop.
>
> Closing the window is not enough — it needs to be a full quit and reopen."

Wait for them to confirm Claude Desktop is reopened before continuing.

---

### Step 6 — Verify Everything Works

Tell the user to do both of these:

1. Open **http://localhost:8000** in their browser — the knowledge graph should load with the interactive UI
2. Open Claude Desktop, start a new chat, and click the **hammer icon (🔨)** near the message box — **My Second Brain** should be listed with its tools

If the web app loads correctly and the MCP tools appear, setup is complete.

**If the web app shows `{"status":"frontend_not_built"}`:** this means the image being used is an older version. Run:

```bash
docker compose pull && docker compose up -d
```

**If the MCP hammer icon is missing in Claude Desktop:** make sure Claude Desktop was fully quit (not just window closed) and reopened. If it still doesn't appear, re-run the MCP setup script and quit/reopen Claude Desktop again.

---

### Step 7 — Tell the User How to Use It

Once everything is confirmed working, tell the user this in plain English:

---

**You're all set. Here's how to use Your Second Brain:**

**The app runs automatically in the background.** Docker starts it silently on every login — you never need to run a command again. Just open http://localhost:8000 or use Claude Desktop and it's ready.

**Adding files to your knowledge base:**
Drop any PDF, Word doc, image (PNG/JPG/WEBP), or video (MP4/MOV/AVI/MKV) directly into the `brain_data/` folder inside the repo, or drag and drop files in the web UI at http://localhost:8000. The app detects new files automatically and indexes them within about a minute — no upload button needed.

**Using the web app (http://localhost:8000):**
- The graph shows all your files as connected nodes — drag, zoom, and click any node to preview it
- Use the **Knowledge Base** panel to manage and upload files
- Use the **Agent** panel to chat with your knowledge directly in the browser
- Use the **Brain Dump** panel to write notes that get indexed automatically alongside your files

**Using Claude Desktop (the most powerful way):**
In any Claude Desktop chat, click the hammer icon and ask things like:
- *"What do my notes say about [topic]?"*
- *"Summarise everything I have on [subject]"*
- *"Show me the clip where [person] talks about [topic]"*
- *"What connects [file A] to [file B]?"*

Claude will search your knowledge base, cite the exact sources, and even trim video files to the precise moment you're looking for.

**Stopping and starting:**
- Stop: `docker compose down` (from the repo folder)
- Start again: `docker compose up -d`
- You rarely need these — Docker handles restarts automatically.

---
