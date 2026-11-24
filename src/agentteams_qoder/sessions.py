"""Durable mirror of SDK-committed transcript rows, without auth files."""
import asyncio
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile


class SessionStore:
    def __init__(self, path, remote=None, committed=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.remote, self.committed = remote, committed
        self.lock = asyncio.Lock()
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS entries (sequence INTEGER PRIMARY KEY, scope TEXT NOT NULL, "
                       "session TEXT NOT NULL, identity TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(scope, identity))")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def encode(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def save(self, key, entries):
        scope = self.encode(key)
        with self.connect() as db:
            for row in entries:
                value = self.encode(row)
                identity = row.get("uuid") or hashlib.sha256(value.encode()).hexdigest()
                previous = db.execute("SELECT value FROM entries WHERE scope=? AND identity=?", (scope, identity)).fetchone()
                if previous and previous[0] != value:
                    raise ValueError("SDK replay changed a committed transcript row")
                db.execute("INSERT OR IGNORE INTO entries(scope, session, identity, value) VALUES(?,?,?,?)",
                           (scope, key["session_id"], identity, value))
        self.persist()

    def persist(self):
        if self.remote:
            with tempfile.TemporaryDirectory(dir=self.path.parent) as directory:
                backup = Path(directory) / self.path.name
                with self.connect() as source, closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
                subprocess.run(["mc", "cp", str(backup), self.remote], check=True, capture_output=True, timeout=30)

    async def append(self, key, entries):
        async with self.lock:
            await asyncio.to_thread(self.save, key, entries)
            if self.committed:
                await self.committed(key, entries)

    async def load(self, key):
        with self.connect() as db:
            rows = db.execute("SELECT value FROM entries WHERE scope=? ORDER BY sequence", (self.encode(key),)).fetchall()
        return [json.loads(row[0]) for row in rows] or None

    def digest(self, session_id):
        with self.connect() as db:
            rows = db.execute("SELECT scope, identity, value FROM entries WHERE session=? ORDER BY sequence",
                              (session_id,)).fetchall()
        return hashlib.sha256(self.encode(rows).encode()).hexdigest() if rows else None

    def has_task_receipt(self, session, call_id, receipt_id):
        with self.connect() as db:
            rows=db.execute("SELECT value FROM entries WHERE session=? ORDER BY sequence",(session,)).fetchall()
        def matches(value):
            if isinstance(value,dict):
                if value.get("type")=="tool_result" and value.get("tool_use_id")==call_id:
                    content=value.get("content",[])
                    parts=[content] if isinstance(content,str) else [p.get("text","") for p in content if isinstance(p,dict)]
                    for text in parts:
                        try: parsed=json.JSONDecoder().raw_decode(text.lstrip())[0]
                        except (ValueError,TypeError):continue
                        if isinstance(parsed,dict) and parsed.get("receiptId")==receipt_id:return True
                return any(matches(item) for item in value.values())
            return isinstance(value,list) and any(matches(item) for item in value)
        return any(matches(json.loads(row[0])) for row in rows)
