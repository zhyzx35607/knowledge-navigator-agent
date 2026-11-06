"""SQLite storage and deterministic Chinese/English FTS5 retrieval (MIT)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

UNCHANGED = object()


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
                CREATE TABLE IF NOT EXISTS collections (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_records (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, query TEXT NOT NULL,
                    mode TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS embedding_vectors (
                    cache_key TEXT PRIMARY KEY,
                    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
                    vector TEXT NOT NULL);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
            if "collection_id" not in columns:
                conn.execute("ALTER TABLE documents ADD COLUMN collection_id TEXT REFERENCES collections(id) ON DELETE SET NULL")
            conn.execute("INSERT INTO metadata VALUES ('schema_version','2') ON CONFLICT(key) DO UPDATE SET value='2'")
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

    def put(self, id: str, title: str, body: str, entry_ids: list[str], expected_version=None,
            collection_id=UNCHANGED):
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
            if collection_id is not UNCHANGED:
                self._check_collection(conn, collection_id)
            self._put(conn, id, title, body, entry_ids)
            if collection_id is not UNCHANGED:
                conn.execute("UPDATE documents SET collection_id=? WHERE id=?", (collection_id, id))
        return self.get(id)

    def _put(self, conn, id, title, body, entry_ids):
        old = conn.execute("SELECT version FROM documents WHERE id=?", (id,)).fetchone()
        version = (old[0] if old else 0) + 1
        digest = hashlib.sha256(body.encode()).hexdigest()
        conn.execute("DELETE FROM search_index WHERE document_id=?", (id,))
        conn.execute("DELETE FROM chunks WHERE document_id=?", (id,))
        conn.execute("""INSERT INTO documents (id,title,body,entry_ids,version,digest,updated_at) VALUES (?,?,?,?,?,?,?)
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

    @staticmethod
    def _check_collection(conn, collection_id):
        if collection_id is not None and not conn.execute(
                "SELECT 1 FROM collections WHERE id=?", (collection_id,)).fetchone():
            raise ValueError("知识集合不存在，请刷新后重试")

    def collections(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("""SELECT c.*,COUNT(d.id) AS document_count
                FROM collections c LEFT JOIN documents d ON d.collection_id=c.id
                GROUP BY c.id ORDER BY c.name,c.id""")]

    def save_collection(self, name, collection_id=None):
        creating = collection_id is None
        collection_id = collection_id or uuid.uuid4().hex
        with self.connection() as conn:
            try:
                if creating:
                    conn.execute("INSERT INTO collections VALUES (?,?,?)", (collection_id, name, datetime.now(timezone.utc).isoformat()))
                elif not conn.execute("UPDATE collections SET name=? WHERE id=?", (name, collection_id)).rowcount:
                    raise LookupError("集合不存在")
            except sqlite3.IntegrityError as exc:
                raise ValueError("已存在同名集合") from exc
            row = conn.execute("SELECT * FROM collections WHERE id=?", (collection_id,)).fetchone()
            count = conn.execute("SELECT COUNT(*) FROM documents WHERE collection_id=?", (collection_id,)).fetchone()[0]
            return {**dict(row), "document_count": count}

    def delete_collection(self, collection_id):
        with self.connection() as conn:
            return conn.execute("DELETE FROM collections WHERE id=?", (collection_id,)).rowcount > 0

    def import_documents(self, items, collection_id=None, entry_ids=()):
        imported = []
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check_collection(conn, collection_id)
            known = {row[0] for row in conn.execute("SELECT id FROM entries")}
            if not set(entry_ids) <= known:
                raise PermissionError("文档绑定了白名单外的入口")
            for item in items:
                document_id = uuid.uuid4().hex
                self._put(conn, document_id, item["filename"], item["body"], list(entry_ids))
                conn.execute("UPDATE documents SET collection_id=? WHERE id=?", (collection_id, document_id))
                imported.append({"id": document_id, "title": item["filename"]})
        return imported

    def save_research(self, result):
        """Only the server search flow calls this with a completed result or safe failure."""
        record_id, created_at = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        snapshot = {key: result[key] for key in (
            "query", "mode", "status", "answer", "scope_document_ids", "scope_collection_ids",
            "scope_entry_ids", "citations", "entries", "trace", "model_called", "model_calls", "code", "message",
            "retrieval", "parent_research_id", "continuation_context")
            if key in result}
        with self.connection() as conn:
            conn.execute("INSERT INTO research_records VALUES (?,?,?,?,?,?)", (
                record_id, created_at, result["query"], result["mode"], result["status"],
                json.dumps(snapshot, ensure_ascii=False)))
        return {"research_id": record_id, "recorded_at": created_at}

    def research_records(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("""SELECT id,created_at,query,mode,status
                FROM research_records ORDER BY created_at DESC,id DESC""")]

    def research(self, record_id):
        with self.connection() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM research_records WHERE id=?", (record_id,)).fetchone()
            if not row:
                return None
            result = {**json.loads(row["snapshot"]), "research_id": row["id"], "recorded_at": row["created_at"]}
            stale_count = 0
            for citation in result.get("citations", []):
                current = conn.execute("SELECT version,digest,body FROM documents WHERE id=?",
                                       (citation["document_id"],)).fetchone()
                reason = ("文档已删除" if current is None else "文档已更新"
                          if current["version"] != citation["version"] or current["digest"] != citation["digest"]
                          or citation["quote"] not in current["body"] else None)
                citation["stale"] = reason is not None
                citation["stale_reason"] = reason
                stale_count += int(reason is not None)
            result["stale_citation_count"] = stale_count
            result["evidence_status"] = ("stale" if stale_count else "current"
                                         if result.get("citations") else "none")
            result["checked_at"] = datetime.now(timezone.utc).isoformat()
            return result

    def delete_research(self, record_id):
        with self.connection() as conn:
            return conn.execute("DELETE FROM research_records WHERE id=?", (record_id,)).rowcount > 0

    def scoped_chunks(self, scope):
        if not scope:
            return []
        with self.connection() as conn:
            placeholders = ",".join("?" for _ in scope)
            rows = conn.execute(f"""SELECT c.id AS citation_id,c.document_id,c.quote,
                d.title,d.version,d.digest,d.entry_ids FROM chunks c JOIN documents d ON d.id=c.document_id
                WHERE c.document_id IN ({placeholders}) ORDER BY c.id LIMIT 2001""", scope).fetchall()
            return [{**dict(row), "entry_ids": json.loads(row["entry_ids"])} for row in rows]

    def cached_vectors(self, keys):
        if not keys:
            return {}
        with self.connection() as conn:
            # Bound SQL variables even when the corpus has many chunks.
            found = {}
            for start in range(0, len(keys), 400):
                batch = keys[start:start + 400]
                rows = conn.execute(f"SELECT cache_key,vector FROM embedding_vectors WHERE cache_key IN ({','.join('?' for _ in batch)})", batch)
                found.update({row["cache_key"]: json.loads(row["vector"]) for row in rows})
            return found

    def cache_vectors(self, values):
        with self.connection() as conn:
            for cache_key, chunk_id, digest, vector in values:
                conn.execute("""INSERT OR REPLACE INTO embedding_vectors(cache_key,chunk_id,vector)
                    SELECT ?,c.id,? FROM chunks c JOIN documents d ON d.id=c.document_id
                    WHERE c.id=? AND d.digest=?""", (cache_key, json.dumps(vector), chunk_id, digest))
