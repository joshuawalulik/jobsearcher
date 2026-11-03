"""MCP server exposing the tracker's considering lane to Claude Desktop.

Stdio server registered in Claude Desktop's claude_desktop_config.json
(command: .venv/bin/python, args: [<abs path to this file>]). Reads
data/jobs.db directly, so it works whether or not uvicorn is running.
The one write tool appends to an application's notes; lane moves and
everything else stay in the web UI.

Run by hand for debugging: .venv/bin/python mcp_server.py
"""

import json

from mcp.server.mcpserver import MCPServer

import db

mcp = MCPServer(
    "jobsearcher",
    instructions=(
        "Job-search tracker for Joshua's local JobSearcher app. "
        "list_considering_jobs shows what's queued for an application "
        "workflow pass (has_notes=false usually means not yet processed); "
        "get_job returns the full description and score breakdown; "
        "save_application_note appends workflow output to the tracker card. "
        "When asked to run the application workflow for new/considering "
        "jobs without naming a specific one: call list_considering_jobs, "
        "show the unprocessed jobs as a short numbered list (company, "
        "title, score), and ask which one to work on. Process only the "
        "chosen job (get_job, then the workflow), and offer the next "
        "unprocessed job when it's done. Never batch-process all jobs "
        "unless explicitly asked to do them all."
    ),
)


def _connect():
    # db.DB_PATH passed explicitly so tests can monkeypatch it.
    return db.get_connection(db.DB_PATH, check_same_thread=False)


@mcp.tool()
def list_considering_jobs() -> list[dict]:
    """Jobs in the tracker's 'considering' lane, most recently touched first.

    has_notes=false usually means the application workflow hasn't been
    run for that job yet. Use get_job(app_id) for the full description.
    """
    conn = _connect()
    try:
        rows = [r for r in db.fetch_applications(conn) if r["lane"] == "considering"]
        return [
            {
                "app_id": r["id"],
                "company": r["company"],
                "title": r["title"],
                "location": r["location"],
                "score": r["score"],
                "url": r["url"],
                "has_notes": bool((r["notes"] or "").strip()),
                "tracked_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


@mcp.tool()
def get_job(app_id: int) -> dict:
    """Full detail for one tracked job by app_id (from list_considering_jobs).

    Includes the complete job description, the deterministic score
    breakdown (matched/missing keywords, coverage), and current notes.
    """
    conn = _connect()
    try:
        row = db.get_application(conn, app_id)
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"no application with app_id {app_id}")
    try:
        breakdown = json.loads(row["score_breakdown"]) if row["score_breakdown"] else None
    except json.JSONDecodeError:
        breakdown = row["score_breakdown"]
    return {
        "app_id": row["id"],
        "company": row["company"],
        "title": row["title"],
        "location": row["location"],
        "url": row["url"],
        "lane": row["lane"],
        "score": row["score"],
        "score_breakdown": breakdown,
        "description": row["description"],
        "notes": row["notes"],
        "resume_version": row["resume_version"],
        "tracked_at": row["created_at"],
    }


@mcp.tool()
def save_application_note(app_id: int, text: str) -> str:
    """Append text to an application's notes on the tracker card.

    Appends (never overwrites), separated by a blank line. Use it to
    save application-workflow output — tailored bullets, a cover letter
    draft, a fit summary — so it shows up in the tracker UI.
    """
    if not text.strip():
        raise ValueError("text is empty")
    conn = _connect()
    try:
        if not db.append_application_note(conn, app_id, text.strip()):
            raise ValueError(f"no application with app_id {app_id}")
    finally:
        conn.close()
    return f"Saved to notes for app_id {app_id}."


if __name__ == "__main__":
    mcp.run()
