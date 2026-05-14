import argparse
import os
import sqlite3
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="CBS Coordinator Visualizer")

_db_path: str = ""
_project_root: str = ""

STATIC_DIR = Path(__file__).parent / "static"


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{_db_path}?mode=ro", uri=True, check_same_thread=False)


def _relpath(abs_path: str) -> str:
    try:
        return os.path.relpath(abs_path, _project_root)
    except ValueError:
        return abs_path


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


@app.get("/")
def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/graph")
def get_graph():
    if not os.path.exists(_db_path):
        return {"nodes": [], "edges": []}

    conn = _conn()
    try:
        if not _table_exists(conn, "nodes"):
            return {"nodes": [], "edges": []}

        rows = conn.execute(
            """
            SELECT file_path,
                   COUNT(*) AS total,
                   SUM(CASE WHEN kind = 'structural' THEN 1 ELSE 0 END) AS structural
            FROM nodes
            GROUP BY file_path
            """
        ).fetchall()

        locked: dict[str, int] = {}
        if _table_exists(conn, "locks"):
            for file_path, count in conn.execute(
                "SELECT file_path, COUNT(DISTINCT agent_id) FROM locks GROUP BY file_path"
            ).fetchall():
                locked[file_path] = count

        abs_to_rel: dict[str, str] = {}
        nodes = []
        for file_path, _, structural in rows:
            rel = _relpath(file_path)
            abs_to_rel[file_path] = rel
            parts = rel.replace("\\", "/").split("/")
            mod = parts[0] if len(parts) > 1 else "."
            agent_count = locked.get(file_path, 0)
            nodes.append(
                {
                    "id": rel,
                    "label": rel,
                    "mod": mod,
                    "structural_count": structural or 0,
                    "hot": agent_count > 0,
                    "agents": agent_count,
                    "queue": 0,
                }
            )

        edges: list[list[str]] = []
        if _table_exists(conn, "edges"):
            for from_f, to_f in conn.execute("SELECT from_file, to_file FROM edges"):
                fr = abs_to_rel.get(from_f) or _relpath(from_f)
                to = abs_to_rel.get(to_f) or _relpath(to_f)
                edges.append([fr, to])

        return {"nodes": nodes, "edges": edges}
    finally:
        conn.close()


@app.get("/api/locks")
def get_locks():
    if not os.path.exists(_db_path):
        return []

    conn = _conn()
    try:
        if not _table_exists(conn, "locks"):
            return []
        rows = conn.execute(
            "SELECT node_id, file_path, agent_id, agent_type, acquired_at FROM locks"
        ).fetchall()
        return [
            {
                "node_id": r[0],
                "file_path": _relpath(r[1]),
                "agent_id": r[2],
                "agent_type": r[3],
                "acquired_at": r[4],
            }
            for r in rows
        ]
    finally:
        conn.close()


@app.get("/api/stats")
def get_stats():
    empty = {
        "total_files": 0,
        "total_edges": 0,
        "total_structural_nodes": 0,
        "active_locks": 0,
        "locked_files": 0,
        "agents": [],
    }
    if not os.path.exists(_db_path):
        return empty

    conn = _conn()
    try:
        result = dict(empty)
        if _table_exists(conn, "nodes"):
            result["total_files"] = conn.execute(
                "SELECT COUNT(DISTINCT file_path) FROM nodes"
            ).fetchone()[0]
            result["total_structural_nodes"] = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind='structural'"
            ).fetchone()[0]
        if _table_exists(conn, "edges"):
            result["total_edges"] = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        if _table_exists(conn, "locks"):
            result["active_locks"] = conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0]
            result["locked_files"] = conn.execute(
                "SELECT COUNT(DISTINCT file_path) FROM locks"
            ).fetchone()[0]
            result["agents"] = [
                r[0] for r in conn.execute("SELECT DISTINCT agent_id FROM locks").fetchall()
            ]
        return result
    finally:
        conn.close()


@app.get("/api/events")
def get_events():
    return []


def main():
    global _db_path, _project_root

    parser = argparse.ArgumentParser(description="CBS Coordinator Visualizer")
    parser.add_argument("--db", default=".claude/coordinator.db")
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    _project_root = str(root)
    _db_path = str(root / args.db) if not os.path.isabs(args.db) else args.db

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    print(f"CBS Coordinator Visualizer")
    print(f"  project root : {_project_root}")
    print(f"  database     : {_db_path}")
    print(f"  listening    : http://{args.host}:{args.port}")

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
