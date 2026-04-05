# Your Second Brain — macOS Setup Blueprint

Paste this file into Claude Code and say:

**"Read this file and follow every step exactly to set up the app on my Mac. Do everything yourself — only pause when you need my Gemini API key."**

Claude Code will handle everything. You just answer the one question it asks (your Gemini API key) and you're done.

---

## Instructions for Claude Code

You are a developer setup assistant. The user has given you this file and wants you to set up Your Second Brain end-to-end on their Mac so it works immediately after you finish.

Do not ask for permission before each step. Do not explain what you are about to do before doing it. Run everything sequentially, tell the user what you are doing in plain English as you go, and only pause when you need their Gemini API key. Handle all errors yourself — diagnose and fix them without asking the user unless it is truly unresolvable.

---

### Step 1 — Clone the Repo

Clone the repo into the user's home directory:

```bash
git clone https://github.com/officialadityadesai/yoursecondbrain.git "$HOME/yoursecondbrain"
cd "$HOME/yoursecondbrain"
```

If git is not installed, macOS will prompt the user to install Xcode Command Line Tools automatically — tell them to click Install when the popup appears, wait for it to finish, then continue.

---

### Step 2 — Check and Install Prerequisites

**Homebrew** (package manager for macOS):

```bash
which brew
```

If missing, install it:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

After installing, follow any instructions Homebrew prints to add it to your PATH (it will tell you to run one or two commands — run them).

**Python, Node.js, and FFmpeg:**

```bash
brew install python node ffmpeg
```

Verify all three installed:

```bash
python3 --version && node --version && ffmpeg -version
```

Python must be 3.10 or higher. Node must be 18 or higher. If either is below the required version, run `brew upgrade python` or `brew upgrade node`.

---

### Step 3 — Create a Virtual Environment and Install Backend Dependencies

```bash
cd "$HOME/yoursecondbrain"
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

The virtual environment keeps the app's Python packages isolated from the rest of the system.

---

### Step 4 — Install Frontend Dependencies and Build

```bash
cd "$HOME/yoursecondbrain/frontend"
npm install
npm run build
cd ..
```

This compiles the React app into `frontend/dist/` which the backend serves at http://127.0.0.1:8000.

---

### Step 5 — API Key Setup

Ask the user:

> "Please go to https://aistudio.google.com/app/apikey, create a free API key, and paste it here."

Once they provide the key:

```bash
cd "$HOME/yoursecondbrain"
cp .env.example .env
```

Then write their key into the `.env` file so it looks like:

```
GEMINI_API_KEY=AIza...their_key_here
```

---

### Step 6 — Set Up Auto-Start on Login

This makes the backend start automatically every time the Mac starts, so the app is always available at http://127.0.0.1:8000 without the user needing to run anything.

Run this entire block as one command from the repo root:

```bash
REPO="$HOME/yoursecondbrain"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$HOME/Library/LaunchAgents/com.yoursecondbrain.backend.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>Label</key>
        <string>com.yoursecondbrain.backend</string>
        <key>ProgramArguments</key>
        <array>
                <string>$REPO/.venv/bin/python</string>
                <string>-m</string>
                <string>uvicorn</string>
                <string>main:app</string>
                <string>--host</string>
                <string>127.0.0.1</string>
                <string>--port</string>
                <string>8000</string>
        </array>
        <key>WorkingDirectory</key>
        <string>$REPO/backend</string>
        <key>EnvironmentVariables</key>
        <dict>
                <key>GEMINI_API_KEY</key>
                <string>$(grep GEMINI_API_KEY "$REPO/.env" | cut -d= -f2- | tr -d '[:space:]')</string>
        </dict>
        <key>RunAtLoad</key>
        <true/>
        <key>KeepAlive</key>
        <true/>
        <key>StandardOutPath</key>
        <string>$REPO/scripts/macos-backend.out.log</string>
        <key>StandardErrorPath</key>
        <string>$REPO/scripts/macos-backend.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/com.yoursecondbrain.backend" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.yoursecondbrain.backend.plist"
launchctl enable "gui/$(id -u)/com.yoursecondbrain.backend"
launchctl kickstart -k "gui/$(id -u)/com.yoursecondbrain.backend"
```

Wait a few seconds then verify the backend is running:

```bash
sleep 4 && curl -s http://127.0.0.1:8000/api/files | head -c 50
```

You should see JSON output starting with `[`. If it returns nothing or an error, check the log:

```bash
cat "$HOME/yoursecondbrain/scripts/macos-backend.err.log"
```

---

### Step 7 — Set Up Claude Desktop MCP Integration

This wires My Second Brain into Claude Desktop so the user can ask Claude anything about their saved knowledge.

Run the setup script:

```bash
source "$HOME/yoursecondbrain/.venv/bin/activate"
python "$HOME/yoursecondbrain/scripts/setup_mcp.py"
```

The script will automatically write the correct config to Claude Desktop's config file using the virtual environment's Python path.

If the script errors, manually find the Claude Desktop config file:

```bash
open "$HOME/Library/Application Support/Claude/"
```

Open `claude_desktop_config.json` in a text editor and add (or merge into existing content):

```json
{
  "mcpServers": {
    "my-second-brain": {
      "command": "/Users/YOURUSERNAME/yoursecondbrain/.venv/bin/python",
      "args": ["/Users/YOURUSERNAME/yoursecondbrain/backend/mcp_server.py"]
    }
  }
}
```

Replace `YOURUSERNAME` with the output of `echo $USER`.

After setup, tell the user:

> "Now fully quit Claude Desktop — press **Cmd+Q** or right-click its icon in the Dock and click **Quit**. Closing the window is not enough. Then reopen Claude Desktop."

---

### Step 8 — Verify Everything Works

1. Open http://127.0.0.1:8000 in a browser — the knowledge graph UI should load
2. Open Claude Desktop, start a new chat, and look for the hammer icon (🔨) near the message box — click it and **My Second Brain** should be listed with tools available

If the UI shows `{"status":"frontend_not_built"}`, re-run Step 4.

If the MCP tools are not showing in Claude Desktop, make sure Claude Desktop was fully quit with Cmd+Q and reopened, not just the window closed.

---

### Step 9 — Tell the User How to Use It

Once everything is running, tell the user this in plain English:

---

**You're all set. Here's how to use My Second Brain:**

**Adding files to your knowledge base:**
Drop any PDF, Word doc, image (PNG/JPG), or video (MP4/MOV) into the `brain_data/` folder inside the repo at `~/yoursecondbrain/brain_data/`. The app detects it automatically and indexes it within a minute — no upload button needed. You can also drag and drop files directly in the web UI at http://127.0.0.1:8000.

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

**The app runs automatically on login** — every time you start your Mac, the backend starts silently in the background. Just open http://127.0.0.1:8000 or use Claude Desktop and it is ready.

---
