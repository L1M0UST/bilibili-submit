"""SQLite 任务存储：状态机、租约、事件日志。"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config as cfg
from .util import now_iso, now_ts

# 主链路：pending → validated → uploading → uploaded → submitting → submitted
#         → reviewing → published
# 异常：pending_auth 待授权 / retrying 待重试 / pending_confirm 结果待核对
#       returned 已退回 / manual 待人工处理 / cancelled 已取消
STATES = (
    "pending", "validated", "uploading", "uploaded", "submitting",
    "submitted", "reviewing", "published",
    "pending_auth", "retrying", "pending_confirm", "returned", "manual",
    "cancelled",
)
# 可被 run 领取处理的状态
CLAIMABLE = ("pending", "retrying")
# 需要 reconcile 回查的状态
RECONCILABLE = ("submitted", "reviewing", "pending_confirm")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL DEFAULT 'main',
    video_path TEXT NOT NULL,
    cover_path TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    tid INTEGER NOT NULL,
    copyright INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT '',
    no_reprint INTEGER NOT NULL DEFAULT 0,
    submit_at INTEGER,
    dtime INTEGER,
    content_hash TEXT,
    video_size INTEGER,
    state TEXT NOT NULL DEFAULT 'pending',
    aid INTEGER,
    bvid TEXT,
    cover_url TEXT,
    upos_filename TEXT,
    upload_progress TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER,
    runner_id TEXT,
    claimed_at INTEGER,
    last_error TEXT,
    reconciled_at INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    submitted_at INTEGER,
    published_at INTEGER,
    UNIQUE(account_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    state TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id);
"""


@contextmanager
def connect(db_path: Path | None = None):
    path = db_path or cfg.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def add_event(conn: sqlite3.Connection, task_id: int, state: str | None, message: str) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, ts, state, message) VALUES (?,?,?,?)",
        (task_id, now_iso(), state, message[:2000]),
    )


def insert_task(conn: sqlite3.Connection, fields: dict) -> int:
    fields = dict(fields)
    fields.setdefault("created_at", now_iso())
    fields.setdefault("updated_at", fields["created_at"])
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", list(fields.values()))
    add_event(conn, cur.lastrowid, fields.get("state", "pending"), "任务创建")
    return cur.lastrowid


def get_task(conn: sqlite3.Connection, *, job_id: str | None = None, id_: int | None = None):
    if job_id is not None:
        return conn.execute("SELECT * FROM tasks WHERE job_id=?", (job_id,)).fetchone()
    if id_ is not None:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (id_,)).fetchone()
    raise ValueError("需要 job_id 或 id_")


def list_tasks(conn: sqlite3.Connection, states: list[str] | None = None) -> list[sqlite3.Row]:
    if states:
        marks = ",".join("?" for _ in states)
        return conn.execute(
            f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY id DESC", states
        ).fetchall()
    return conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()


def claim_due(conn: sqlite3.Connection, runner_id: str, lease_seconds: int = 1800):
    """领取到期任务：串行第一版，单条领取。上传中超过租约的行按崩溃恢复重领。"""
    now = now_ts()
    row = conn.execute(
        """SELECT * FROM tasks
           WHERE (state IN ('pending','retrying','validated')
                  AND (submit_at IS NULL OR submit_at<=?)
                  AND (next_attempt_at IS NULL OR next_attempt_at<=?))
              OR (state IN ('uploading','uploaded','submitting')
                  AND claimed_at IS NOT NULL AND claimed_at<=?)
           ORDER BY COALESCE(submit_at,0), id LIMIT 1""",
        (now, now, now - lease_seconds),
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE tasks SET runner_id=?, claimed_at=?, updated_at=? WHERE id=?",
        (runner_id, now, now_iso(), row["id"]),
    )
    return conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()


def update_task(conn: sqlite3.Connection, task_id: int, fields: dict, event: str | None = None) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", [*fields.values(), task_id])
    if event:
        add_event(conn, task_id, fields.get("state"), event)


def task_events(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
