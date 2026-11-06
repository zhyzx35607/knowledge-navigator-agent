"""SQLite storage and deterministic Chinese/English FTS5 retrieval (MIT)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def terms(text: str) -> list[str]:
    result = []
    for run in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9_]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", run):
            result.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
        else:
            result.append(run)
    return list(dict.fromkeys(result))


def chunks(body: str) -> list[str]:
    return [part[i:i + 500] for part in re.split(r"\n\s*\n", body.strip())
            if part.strip() for i in range(0, len(part), 500)]


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                    entry_ids TEXT NOT NULL, version INTEGER NOT NULL,
                    digest TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id)
                    ON DELETE CASCADE, quote TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                    chunk_id UNINDEXED, document_id UNINDEXED, tokens);
            """)
            # Seed once, even when the user subsequently deletes every document.
            if not conn.execute("SELECT 1 FROM metadata WHERE key='seeded'").fetchone():
                demo = json.loads(Path(__file__).with_name("demo.json").read_text("utf-8"))
                for entry in demo["entries"]:
                    conn.execute("INSERT INTO entries VALUES (:id,:title,:path,:description)", entry)
                for document in demo["documents"]:
                    self._put(conn, **document)
                conn.execute("INSERT INTO metadata VALUES ('seeded','1')")

    def entries(self) -> list[dict]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM entries ORDER BY id")]

    def documents(self) -> list[dict]:
        with self.connection() as conn:
            return [self._document(row) for row in conn.execute("SELECT * FROM documents ORDER BY title")]

    @staticmethod
    def _document(row):
        return {**dict(row), "entry_ids": json.loads(row["entry_ids"])}

    def get(self, document_id: str):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            return self._document(row) if row else None

    def put(self, id: str, title: str, body: str, entry_ids: list[str], expected_version=None):
        with self.connection() as conn:
            # Lock before checking the version so another writer cannot pass
            # the same optimistic-lock check before this update commits.
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT version FROM documents WHERE id=?", (id,)).fetchone()
            actual = old["version"] if old else 0
            if expected_version is not None and expected_version != actual:
                raise ValueError("文档版本已变化，请刷新后再保存")
            known = {row[0] for row in conn.execute("SELECT id FROM entries")}
            if not set(entry_ids) <= known:
                raise PermissionError("文档绑定了白名单外的入口")
            self._put(conn, id, title, body, entry_ids)
        return self.get(id)

    def _put(self, conn, id, title, body, entry_ids):
        old = conn.execute("SELECT version FROM documents WHERE id=?", (id,)).fetchone()
        version = (old[0] if old else 0) + 1
        digest = hashlib.sha256(body.encode()).hexdigest()
        conn.execute("DELETE FROM search_index WHERE document_id=?", (id,))
        conn.execute("DELETE FROM chunks WHERE document_id=?", (id,))
        conn.execute("""INSERT INTO documents VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,body=excluded.body,
            entry_ids=excluded.entry_ids,version=excluded.version,digest=excluded.digest,
            updated_at=excluded.updated_at""", (id, title, body, json.dumps(sorted(set(entry_ids))),
            version, digest, datetime.now(timezone.utc).isoformat()))
        for number, quote in enumerate(chunks(body)):
            chunk_id = f"{id}:v{version}:{number}"
            conn.execute("INSERT INTO chunks VALUES (?,?,?)", (chunk_id, id, quote))
            conn.execute("INSERT INTO search_index VALUES (?,?,?)",
                         (chunk_id, id, " ".join(terms((title + " " if number == 0 else "") + quote))))

    def delete(self, document_id: str) -> bool:
        with self.connection() as conn:
            conn.execute("DELETE FROM search_index WHERE document_id=?", (document_id,))
            return conn.execute("DELETE FROM documents WHERE id=?", (document_id,)).rowcount > 0

    def retrieve(self, query: str, scope: list[str], limit: int = 6) -> list[dict]:
        tokens = terms(query)[:60]
        if not tokens or not scope:
            return []
        match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        placeholders = ",".join("?" for _ in scope)
        with self.connection() as conn:
            rows = conn.execute(f"""SELECT c.id AS citation_id,c.document_id,c.quote,
                d.title,d.version,d.digest,d.entry_ids,bm25(search_index) AS rank
                FROM search_index JOIN chunks c ON c.id=search_index.chunk_id
                JOIN documents d ON d.id=c.document_id
                WHERE search_index MATCH ? AND c.document_id IN ({placeholders})
                ORDER BY rank,c.id LIMIT ?""", [match, *scope, limit]).fetchall()
            return [{**dict(row), "entry_ids": json.loads(row["entry_ids"])} for row in rows]

    def read_chunks(self, chunk_ids: list[str], scope: list[str]) -> list[dict]:
        """Read current full chunks, filtering again by the original request scope."""
        if not chunk_ids or not scope:
            return []
        ids_sql = ",".join("?" for _ in chunk_ids)
        scope_sql = ",".join("?" for _ in scope)
        with self.connection() as conn:
            rows = conn.execute(f"""SELECT c.id AS citation_id,c.document_id,c.quote,
                d.title,d.version,d.digest,d.entry_ids
                FROM chunks c JOIN documents d ON d.id=c.document_id
                WHERE c.id IN ({ids_sql}) AND c.document_id IN ({scope_sql})""",
                [*chunk_ids, *scope]).fetchall()
            found = {row["citation_id"]: {**dict(row), "entry_ids": json.loads(row["entry_ids"])} for row in rows}
            return [found[chunk_id] for chunk_id in chunk_ids if chunk_id in found]
