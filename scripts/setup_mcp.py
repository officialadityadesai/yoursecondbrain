"""
My Second Brain — Claude Desktop MCP Setup
Automatically writes the correct MCP config so Claude Desktop can find your Second Brain.

Run via:  scripts\setup_mcp.bat   (from the repo root)
Or:       python scripts\setup_mcp.py
"""

import os
import sys
import json

# ── Paths ────────────────────────────────────────────────────────────────────

scripts_dir  = os.path.dirname(os.path.abspath(__file__))
repo_root    = os.path.dirname(scripts_dir)
mcp_script   = os.path.join(repo_root, "backend", "mcp_server.py")

# Use the exact Python interpreter that is running this script.
# This guarantees the same environment where 'mcp' and 'requests' are installed.
python_exe   = sys.executable

# Claude Desktop config location on Windows
appdata      = os.environ.get("APPDATA", "")
claude_dir   = os.path.join(appdata, "Claude")
config_path  = os.path.join(claude_dir, "claude_desktop_config.json")

# ── Validate ─────────────────────────────────────────────────────────────────

print()
print("My Second Brain — MCP Setup")
print("=" * 50)

if not os.path.isfile(mcp_script):
    print(f"\n[ERROR] Cannot find mcp_server.py at:\n  {mcp_script}")
    print("Make sure you are running this from inside the repo.")
    input("\nPress Enter to close...")
    sys.exit(1)

if not appdata:
    print("\n[ERROR] APPDATA environment variable not set. Are you on Windows?")
    input("\nPress Enter to close...")
    sys.exit(1)

# ── Read existing config (preserve any other MCPs the user already has) ──────

os.makedirs(claude_dir, exist_ok=True)

existing_config: dict = {}
if os.path.isfile(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            existing_config = json.load(f)
        print(f"\nFound existing Claude Desktop config at:\n  {config_path}")
    except Exception as e:
        print(f"\n[WARNING] Could not read existing config ({e}). It will be replaced.")
        existing_config = {}
else:
    print(f"\nNo existing Claude Desktop config found. Creating new one at:\n  {config_path}")

if "mcpServers" not in existing_config or not isinstance(existing_config["mcpServers"], dict):
    existing_config["mcpServers"] = {}

# ── Write My Second Brain entry ───────────────────────────────────────────────

existing_config["mcpServers"]["my-second-brain"] = {
    "command": python_exe,
    "args":    [mcp_script],
}

try:
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(existing_config, f, indent=2)
except Exception as e:
    print(f"\n[ERROR] Could not write config file: {e}")
    input("\nPress Enter to close...")
    sys.exit(1)

# ── Success ───────────────────────────────────────────────────────────────────

print()
print("[OK] Config written successfully!")
print()
print("  Python:     " + python_exe)
print("  MCP server: " + mcp_script)
print("  Config:     " + config_path)
print()
print("─" * 50)
print("NEXT STEPS:")
print()
print("  1. My Second Brain auto-starts on login — it's already running.")
print("     (If unsure, open http://127.0.0.1:8000 to confirm.)")
print()
print("  2. Fully quit Claude Desktop:")
print("     Right-click the Claude icon in the system tray → Quit")
print()
print("  3. Reopen Claude Desktop.")
print()
print("  4. Start a new chat. You'll see a small hammer/tools icon (⚒)")
print("     near the message box. Click it — 'My Second Brain' will")
print("     be listed with tools available.")
print()
print("  5. Just ask Claude anything about your uploaded knowledge!")
print("     Example: 'What do my notes say about X?'")
print("─" * 50)
print()
input("Press Enter to close...")
