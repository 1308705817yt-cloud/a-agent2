"""SQLite job index, optimistic writes and cross-process run leases."""

import json
import re
import sqlite3
import time
import base64
import math
from contextlib import closing


def valid_id(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{12}", run_id):
        raise ValueError("任务 ID 必须是 12 位十六进制字符。")
    return run_id


class Store:
    def __init__(self, root):
        self.path = root / ".agentforge" / "agentforge.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS leases (id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL)")
            if "created" not in {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}:
                conn.execute("ALTER TABLE jobs ADD COLUMN created REAL")
                conn.execute("UPDATE jobs SET created=COALESCE(json_extract(payload,'$.created_at'),updated)")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created DESC,id DESC)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def load(self, run_id):
        valid_id(run_id)
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT payload FROM jobs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("未找到 v3 任务；旧版任务仍保留在 runs 中。")
        return json.loads(row[0])

    def save(self, state, owner=None):
        run_id = valid_id(state["run_id"])
        revision = state.get("revision", 0)
        updated = time.time()
        payload = {**state, "revision": revision + 1, "updated_at": updated}
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT owner,expires FROM leases WHERE id=?", (run_id,)).fetchone()
            if lease and lease[1] > updated and lease[0] != owner:
                raise ValueError("任务正在由另一个进程处理。")
            if owner and (not lease or lease[0] != owner):
                raise ValueError("任务执行租约已丢失。")
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            if revision == 0:
                conn.execute("INSERT INTO jobs(id,revision,updated,payload,created) VALUES (?,?,?,?,?)", (run_id, 1, updated, data, state.get("created_at", updated)))
            elif conn.execute("UPDATE jobs SET revision=?,updated=?,payload=? WHERE id=? AND revision=?",
                              (revision + 1, updated, data, run_id, revision)).rowcount != 1:
                raise ValueError("任务已被更新，请重新读取。")
            if owner:
                conn.execute("UPDATE leases SET expires=? WHERE id=? AND owner=?", (updated + 900, run_id, owner))
        state.update(revision=revision + 1, updated_at=updated)

    def acquire(self, run_id, owner):
        valid_id(run_id)
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM leases WHERE expires<?", (time.time(),))
            try:
                conn.execute("INSERT INTO leases VALUES (?,?,?)", (run_id, owner, time.time() + 900))
            except sqlite3.IntegrityError:
                raise ValueError("任务正在运行，请等待当前构建完成。")

    def release(self, run_id, owner):
        with closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM leases WHERE id=? AND owner=?", (run_id, owner))

    def renew(self, run_id, owner):
        with closing(self.connect()) as conn, conn:
            if conn.execute("UPDATE leases SET expires=? WHERE id=? AND owner=? AND expires>?",
                            (time.time() + 900, run_id, owner, time.time())).rowcount != 1:
                raise ValueError("任务执行租约已丢失。")

    def active(self, run_id):
        with closing(self.connect()) as conn:
            return bool(conn.execute("SELECT 1 FROM leases WHERE id=? AND expires>?", (run_id, time.time())).fetchone())

    def list(self):
        """Compatibility API: consume every page, including startup/CLI recovery."""
        return list(self.iter_all())

    def iter_all(self, limit=100):
        cursor = None
        while True:
            page = self.page(limit, cursor)
            yield from page["jobs"]
            cursor = page["next_cursor"]
            if cursor is None:
                break

    def page(self, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("任务分页 limit 应为 1 至 200。")
        boundary = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 256:
                    raise ValueError()
                boundary = json.loads(base64.b64decode(cursor.encode(), altchars=b"-_", validate=True))
                if (not isinstance(boundary, list) or len(boundary) != 2 or type(boundary[0]) not in (int, float)
                        or not 0 <= boundary[0] <= 1e12 or not math.isfinite(boundary[0])):
                    raise ValueError()
                valid_id(boundary[1])
            except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
                raise ValueError("任务分页 cursor 无效。") from exc
        with closing(self.connect()) as conn:
            where = "WHERE created<? OR (created=? AND id<?)" if boundary else ""
            params = (boundary[0], boundary[0], boundary[1], limit + 1) if boundary else (limit + 1,)
            rows = conn.execute("SELECT payload,created,id FROM jobs " + where + " ORDER BY created DESC,id DESC LIMIT ?", params).fetchall()
            counts = conn.execute("SELECT json_extract(payload,'$.status'),COUNT(*) FROM jobs GROUP BY json_extract(payload,'$.status')").fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = base64.urlsafe_b64encode(json.dumps([rows[-1][1], rows[-1][2]]).encode()).decode() if more else None
        return {"jobs": [json.loads(r[0]) for r in rows], "next_cursor": next_cursor,
                "total": sum(r[1] for r in counts), "status_counts": dict(counts)}
