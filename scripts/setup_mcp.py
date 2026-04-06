"""
My Second Brain — Claude Desktop MCP Setup
Supports Windows, macOS, and Linux.
Run: python scripts/setup_mcp.py
"""
import os, sys, json, platform

scripts_dir = os.path.dirname(os.path.abspath(__file__))
repo_root   = os.path.dirname(scripts_dir)
mcp_script  = os.path.join(repo_root, "backend", "mcp_server.py")
python_exe  = sys.executable
system      = platform.system()

if system == "Windows":
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        print("\n[ERROR] APPDATA not set. Are you on Windows?")
        input("\nPress Enter to close...")
        sys.exit(1)
    claude_dir  = os.path.join(appdata, "Claude")
    is_windows  = True
elif system == "Darwin":
    claude_dir  = os.path.expanduser("~/Library/Application Support/Claude")
    is_windows  = False
elif system == "Linux":
    claude_dir  = os.path.expanduser("~/.config/Claude")
    is_windows  = False
else:
    print(f"\n[ERROR] Unsupported OS: {system}")
    sys.exit(1)

config_path = os.path.join(claude_dir, "claude_desktop_config.json")

print(f"\nMy Second Brain — MCP Setup\n{'='*50}")
print(f"OS: {system}")

if not os.path.isfile(mcp_script):
    print(f"\n[ERROR] mcp_server.py not found at:\n  {mcp_script}")
    if is_windows: input("\nPress Enter to close...")
    sys.exit(1)

os.makedirs(claude_dir, exist_ok=True)
existing_config = {}
if os.path.isfile(config_path):
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            existing_config = json.load(f)
        print(f"\nFound existing config at:\n  {config_path}")
    except Exception as e:
        print(f"\n[WARNING] Could not read existing config ({e}). Replacing.")
else:
    print(f"\nCreating new config at:\n  {config_path}")

if "mcpServers" not in existing_config or not isinstance(existing_config["mcpServers"], dict):
    existing_config["mcpServers"] = {}

existing_config["mcpServers"]["my-second-brain"] = {
    "command": python_exe,
    "args":    [mcp_script],
    "env":     {"MSB_BACKEND_URL": "http://127.0.0.1:8000"}
}

try:
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(existing_config, f, indent=2)
except Exception as e:
    print(f"\n[ERROR] Could not write config: {e}")
    if is_windows: input("\nPress Enter to close...")
    sys.exit(1)

print(f"\n[OK] Config written successfully!")
print(f"\n  Python:     {python_exe}")
print(f"  MCP server: {mcp_script}")
print(f"  Config:     {config_path}")
print(f"\n{'─'*50}")
print("NEXT STEPS:")
print("\n  1. Confirm the app is running at http://127.0.0.1:8000")
if system == "Windows":
    print("  2. Right-click Claude Desktop in system tray → Quit")
elif system == "Darwin":
    print("  2. Press Cmd+Q to fully quit Claude Desktop")
else:
    print("  2. Fully quit and restart Claude Desktop")
print("  3. Reopen Claude Desktop")
print("  4. Start a new chat — look for the hammer icon (🔨)")
print("     Click it — 'My Second Brain' will be listed")
print(f"{'─'*50}\n")

if is_windows:
    input("Press Enter to close...")
