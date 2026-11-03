# Claude Desktop connector — care and feeding

The `jobsearcher` MCP connector lets a claude.ai Project chat pull
considering-lane jobs and save workflow output back to tracker cards.
`mcp_server.py` is the server; `install_desktop_connector.py` registers
it in Claude Desktop's config.

## When to rerun the installer

```
python3 install_desktop_connector.py   # from the repo root
```

Normally never. Rerun only when:

- **The connector disappears from chats** (symptom: the project chat
  asks you to paste postings instead of calling tools). Usually a
  Claude Desktop update or reinstall reset its config.
- **The repo folder moves or is renamed** — the config stores absolute
  paths. Run the script from the repo's new location.
- **`.venv` is recreated at a different path.** Rebuilding it in place
  doesn't need a rerun, but does need
  `pip install -r requirements.txt` (the server needs the `mcp`
  package).
- **New Mac or fresh Claude Desktop install.**

No rerun needed for: quitting/reopening the app, reboots, edits to
`mcp_server.py` (just relaunch Claude Desktop — the server respawns on
launch), or anything the web app does.

## The one rule

**Claude Desktop must be fully quit (Cmd+Q) before running the
installer.** The app rewrites its config file from memory every minute
or so while running, silently discarding manual edits. The script
refuses to run while the app is open for exactly this reason. Order:
quit → run script in Terminal → reopen.

## Troubleshooting

- Did the app launch the server? Check that
  `~/Library/Logs/Claude/mcp-server-jobsearcher.log` exists; read its
  tail for startup errors.
- Is the entry still in the config?
  `python3 -c "import json, os; print(json.load(open(os.path.expanduser('~/Library/Application Support/Claude/claude_desktop_config.json'))).get('mcpServers'))"`
- Does the server run at all?
  `.venv/bin/python mcp_server.py` from the repo — it should sit
  silently waiting on stdin (Ctrl+C to exit); a traceback means a
  broken venv or import.
- A claude.ai chat says the tool "received no result after waiting ~125
  seconds": the request never reached this computer. The web chat can
  only run the connector while the Claude desktop app is open on an
  awake Mac — nothing appears in mcp-server-jobsearcher.log when this
  happens, which is how you tell it apart from a server crash (a real
  server failure shows the tools/call arriving with no result, or a
  traceback). Open/wake the desktop app and retry; the server itself
  needs no restart.
