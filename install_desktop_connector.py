"""Register mcp_server.py in Claude Desktop's claude_desktop_config.json.

Claude Desktop rewrites that file from memory while it runs, clobbering
manual edits — so this must run while the app is fully quit (the script
refuses otherwise). Steps: quit Claude Desktop (Cmd+Q), run
`python3 install_desktop_connector.py` in Terminal, reopen Claude.
Verify with: ls ~/Library/Logs/Claude/mcp*.log
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
CONFIG = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
ENTRY = {
    "command": str(REPO / ".venv/bin/python"),
    "args": [str(REPO / "mcp_server.py")],
}


def claude_is_running() -> bool:
    # ps, not pgrep: pgrep can't always see other apps' processes here.
    result = subprocess.run(
        ["ps", "-axo", "command"], capture_output=True, text=True
    )
    return any(
        line.strip().startswith("/Applications/Claude.app/Contents/MacOS/Claude")
        for line in result.stdout.splitlines()
    )


def main() -> int:
    if claude_is_running():
        print("Claude Desktop is running — quit it first (Cmd+Q), then rerun this.")
        print("(The app overwrites its config file while running, which would")
        print("silently discard this change.)")
        return 1
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    existing = config.get("mcpServers", {}).get("jobsearcher")
    config.setdefault("mcpServers", {})["jobsearcher"] = ENTRY
    CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    print(f"{'Updated' if existing else 'Added'} 'jobsearcher' in {CONFIG}")
    print("Now reopen Claude Desktop. The connector should appear in chats;")
    print("if not, check ~/Library/Logs/Claude/mcp-server-jobsearcher.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
