from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, goal TEXT NOT NULL, criterion TEXT NOT NULL,
  epoch_seconds INTEGER NOT NULL, total_epochs INTEGER NOT NULL,
  population_size INTEGER NOT NULL, status TEXT NOT NULL,
  current_epoch INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  run_id TEXT NOT NULL, agent_id TEXT NOT NULL, status TEXT NOT NULL,
  active_genome TEXT NOT NULL, parent_agent_id TEXT, branch_name TEXT NOT NULL,
  birth_epoch INTEGER NOT NULL, last_rank INTEGER,
  PRIMARY KEY(run_id, agent_id), FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS genomes (
  hash TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_hash TEXT,
  prompt TEXT NOT NULL, manifest_json TEXT NOT NULL, created_epoch INTEGER NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS scores (
  run_id TEXT NOT NULL, epoch INTEGER NOT NULL, agent_id TEXT NOT NULL,
  genome_hash TEXT NOT NULL, value REAL NOT NULL, tie_break_value REAL NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL DEFAULT 'success', evidence_json TEXT NOT NULL,
  PRIMARY KEY(run_id, epoch, agent_id)
);
CREATE TABLE IF NOT EXISTS lineage (
  run_id TEXT NOT NULL, child_agent_id TEXT NOT NULL, parent_agent_id TEXT,
  parent_genome TEXT, child_genome TEXT NOT NULL, epoch INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, epoch INTEGER NOT NULL,
  agent_id TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  published_at TEXT NOT NULL, content_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS memory_links (
  run_id TEXT NOT NULL, agent_id TEXT NOT NULL, knowledge_id INTEGER NOT NULL,
  inherited_from_agent_id TEXT, inherited_at_epoch INTEGER NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, agent_id, knowledge_id)
);
CREATE TABLE IF NOT EXISTS knowledge_ratings (
  run_id TEXT NOT NULL, knowledge_id INTEGER NOT NULL, agent_id TEXT NOT NULL,
  value INTEGER NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, knowledge_id, agent_id)
);
CREATE INDEX IF NOT EXISTS knowledge_run_id_idx ON knowledge(run_id, id);
CREATE INDEX IF NOT EXISTS memory_links_agent_idx ON memory_links(run_id, agent_id);
CREATE INDEX IF NOT EXISTS knowledge_ratings_record_idx ON knowledge_ratings(run_id, knowledge_id);
CREATE TABLE IF NOT EXISTS epochs (
  run_id TEXT NOT NULL, epoch INTEGER NOT NULL, status TEXT NOT NULL,
  started_at TEXT, completed_at TEXT, PRIMARY KEY(run_id, epoch)
);
CREATE TABLE IF NOT EXISTS agent_attempts (
  run_id TEXT NOT NULL, epoch INTEGER NOT NULL, agent_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, started_at TEXT, completed_at TEXT, error TEXT,
  PRIMARY KEY(run_id, epoch, agent_id)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, goal TEXT NOT NULL, criterion TEXT NOT NULL,
  epoch_seconds INTEGER NOT NULL, total_epochs INTEGER NOT NULL,
  population_size INTEGER NOT NULL, status TEXT NOT NULL,
  current_epoch INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  run_id TEXT NOT NULL, agent_id TEXT NOT NULL, status TEXT NOT NULL,
  active_genome TEXT NOT NULL, parent_agent_id TEXT, branch_name TEXT NOT NULL,
  birth_epoch INTEGER NOT NULL, last_rank INTEGER,
  PRIMARY KEY(run_id, agent_id)
);
CREATE TABLE IF NOT EXISTS genomes (
  hash TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_hash TEXT,
  prompt TEXT NOT NULL, manifest_json TEXT NOT NULL, created_epoch INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
  run_id TEXT NOT NULL, epoch INTEGER NOT NULL, agent_id TEXT NOT NULL,
  genome_hash TEXT NOT NULL, value DOUBLE PRECISION NOT NULL, tie_break_value DOUBLE PRECISION NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL DEFAULT 'success', evidence_json TEXT NOT NULL,
  PRIMARY KEY(run_id, epoch, agent_id)
);
CREATE TABLE IF NOT EXISTS lineage (
  run_id TEXT NOT NULL, child_agent_id TEXT NOT NULL, parent_agent_id TEXT,
  parent_genome TEXT, child_genome TEXT NOT NULL, epoch INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge (
  id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, epoch INTEGER NOT NULL,
  agent_id TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  published_at TIMESTAMPTZ NOT NULL, content_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS memory_links (
  run_id TEXT NOT NULL, agent_id TEXT NOT NULL, knowledge_id BIGINT NOT NULL,
  inherited_from_agent_id TEXT, inherited_at_epoch INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(run_id, agent_id, knowledge_id)
);
CREATE TABLE IF NOT EXISTS knowledge_ratings (
  run_id TEXT NOT NULL, knowledge_id BIGINT NOT NULL, agent_id TEXT NOT NULL,
  value INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(run_id, knowledge_id, agent_id)
);
CREATE INDEX IF NOT EXISTS knowledge_run_id_idx ON knowledge(run_id, id);
CREATE INDEX IF NOT EXISTS memory_links_agent_idx ON memory_links(run_id, agent_id);
CREATE INDEX IF NOT EXISTS knowledge_ratings_record_idx ON knowledge_ratings(run_id, knowledge_id);
CREATE TABLE IF NOT EXISTS epochs (
  run_id TEXT NOT NULL, epoch INTEGER NOT NULL, status TEXT NOT NULL,
  started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, PRIMARY KEY(run_id, epoch)
);
CREATE TABLE IF NOT EXISTS agent_attempts (
  run_id TEXT NOT NULL, epoch INTEGER NOT NULL, agent_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, error TEXT,
  PRIMARY KEY(run_id, epoch, agent_id)
);
CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL,
  event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL
);

-- The rows in this table are the agents' logical writable buckets.  The
-- kernel is a member of kadath_kernel; an organism connection may only act on
-- the bucket named by its transaction-local kadath.agent_id setting, while
-- published records are readable population-wide.
DO $$ BEGIN
  CREATE ROLE kadath_kernel NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE ROLE kadath_agent NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  EXECUTE format('GRANT kadath_kernel TO %I', current_user);
EXCEPTION WHEN insufficient_privilege THEN
  RAISE EXCEPTION 'KADATH PostgreSQL user must be able to create/grant kadath_kernel and kadath_agent roles';
END $$;
ALTER TABLE knowledge ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS kadath_kernel_all ON knowledge;
CREATE POLICY kadath_kernel_all ON knowledge TO PUBLIC
  USING (pg_has_role(current_user, 'kadath_kernel', 'member'))
  WITH CHECK (pg_has_role(current_user, 'kadath_kernel', 'member'));
DROP POLICY IF EXISTS kadath_agent_own_bucket ON knowledge;
CREATE POLICY kadath_agent_own_bucket ON knowledge TO kadath_agent
  USING (agent_id = current_setting('kadath.agent_id', true))
  WITH CHECK (agent_id = current_setting('kadath.agent_id', true));
DROP POLICY IF EXISTS kadath_agent_published_read ON knowledge;
CREATE POLICY kadath_agent_published_read ON knowledge FOR SELECT TO kadath_agent
  USING (payload_json::jsonb ->> 'visibility' = 'shared');
REVOKE ALL ON runs, agents, genomes, scores, lineage, epochs, agent_attempts, events FROM kadath_agent;
REVOKE ALL ON memory_links, knowledge_ratings FROM kadath_agent;
REVOKE ALL ON knowledge FROM kadath_agent;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge TO kadath_agent;
GRANT USAGE, SELECT ON SEQUENCE knowledge_id_seq TO kadath_agent;
"""


class Store:
    """SQLite for local proof runs, PostgreSQL when KADATH_DATABASE_URL is set."""

    def __init__(self, path: Path, database_url: str | None = None):
        self.path = path
        self.database_url = database_url or os.getenv("KADATH_DATABASE_URL")
        self.is_postgres = bool(self.database_url and self.database_url.startswith(("postgres://", "postgresql://")))
        self._pool = None
        self._pool_lock = threading.Lock()
        if not self.is_postgres:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            if self.is_postgres:
                with con.cursor() as cur:
                    cur.execute(POSTGRES_SCHEMA)
                    cur.execute("ALTER TABLE scores ADD COLUMN IF NOT EXISTS tie_break_value DOUBLE PRECISION NOT NULL DEFAULT 0")
                    cur.execute("ALTER TABLE scores ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'success'")
                    cur.execute("ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT ''")
                    cur.execute("ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS attempt_id TEXT NOT NULL DEFAULT ''")
                    self._backfill_knowledge_hashes(con)
                    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS knowledge_content_dedup ON knowledge(run_id,agent_id,kind,content_hash) WHERE content_hash <> ''")
            else:
                con.executescript(SQLITE_SCHEMA)
                columns = {row[1] for row in con.execute("PRAGMA table_info(scores)").fetchall()}
                if "tie_break_value" not in columns: con.execute("ALTER TABLE scores ADD COLUMN tie_break_value REAL NOT NULL DEFAULT 0")
                if "outcome" not in columns: con.execute("ALTER TABLE scores ADD COLUMN outcome TEXT NOT NULL DEFAULT 'success'")
                knowledge_columns = {row[1] for row in con.execute("PRAGMA table_info(knowledge)").fetchall()}
                if "content_hash" not in knowledge_columns: con.execute("ALTER TABLE knowledge ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
                attempt_columns = {row[1] for row in con.execute("PRAGMA table_info(agent_attempts)").fetchall()}
                if "attempt_id" not in attempt_columns: con.execute("ALTER TABLE agent_attempts ADD COLUMN attempt_id TEXT NOT NULL DEFAULT ''")
                self._backfill_knowledge_hashes(con)
                con.execute("CREATE UNIQUE INDEX IF NOT EXISTS knowledge_content_dedup ON knowledge(run_id,agent_id,kind,content_hash) WHERE content_hash <> ''")

    def _backfill_knowledge_hashes(self, con: Any) -> None:
        rows = self._query(con, "SELECT id,run_id,agent_id,kind,payload_json FROM knowledge WHERE content_hash='' ORDER BY id", ()).fetchall()
        seen: dict[tuple[str, str, str, str], int] = {}
        for raw in rows:
            row = dict(raw) if not isinstance(raw, dict) else raw
            try: canonical = json.dumps(json.loads(str(row["payload_json"])), sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError, json.JSONDecodeError): canonical = str(row["payload_json"])
            content_hash = hashlib.sha256(canonical.encode()).hexdigest()
            key = (str(row["run_id"]), str(row["agent_id"]), str(row["kind"]), content_hash)
            if key in seen:
                retained_id, duplicate_id = seen[key], int(row["id"])
                self._query(con, "INSERT OR IGNORE INTO memory_links(run_id,agent_id,knowledge_id,inherited_from_agent_id,inherited_at_epoch,created_at) SELECT run_id,agent_id,?,inherited_from_agent_id,inherited_at_epoch,created_at FROM memory_links WHERE run_id=? AND knowledge_id=?", (retained_id, row["run_id"], duplicate_id))
                self._query(con, "INSERT OR IGNORE INTO knowledge_ratings(run_id,knowledge_id,agent_id,value,created_at) SELECT run_id,?,agent_id,value,created_at FROM knowledge_ratings WHERE run_id=? AND knowledge_id=?", (retained_id, row["run_id"], duplicate_id))
                self._query(con, "DELETE FROM memory_links WHERE run_id=? AND knowledge_id=?", (row["run_id"], duplicate_id))
                self._query(con, "DELETE FROM knowledge_ratings WHERE run_id=? AND knowledge_id=?", (row["run_id"], duplicate_id))
                self._query(con, "DELETE FROM knowledge WHERE id=?", (duplicate_id,))
            else:
                seen[key] = int(row["id"])
                self._query(con, "UPDATE knowledge SET content_hash=? WHERE id=?", (content_hash, row["id"]))

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self.is_postgres:
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError("PostgreSQL selected but psycopg pool support is not installed; reinstall KADATH dependencies") from exc
            if self._pool is None:
                with self._pool_lock:
                    if self._pool is None:
                        self._pool = ConnectionPool(
                            conninfo=self.database_url,
                            min_size=1,
                            max_size=max(2, min(int(os.getenv("KADATH_DB_POOL_SIZE", "24")), 64)),
                            timeout=30,
                            kwargs={"row_factory": dict_row},
                        )
            with self._pool.connection() as con:
                try:
                    yield con
                    con.commit()
                except BaseException:
                    con.rollback()
                    raise
            return
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 30000")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as con:
            row = self._query(con, sql, args).fetchone()
            return dict(row) if row else None

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as con:
            return [dict(row) for row in self._query(con, sql, args).fetchall()]

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> None:
        with self.connect() as con:
            self._query(con, sql, args)

    def _query(self, con: Any, sql: str, args: tuple[Any, ...]):
        if self.is_postgres:
            sql = sql.replace("?", "%s")
            if sql.startswith("INSERT OR IGNORE INTO "):
                sql = sql.replace("INSERT OR IGNORE INTO ", "INSERT INTO ", 1) + " ON CONFLICT DO NOTHING"
            return con.execute(sql, args)
        return con.execute(sql, args)

    def add_knowledge(self, run_id: str, epoch: int, agent_id: str, kind: str, payload: dict[str, Any], now: str) -> int:
        """Insert one canonical memory record and return its stable row id.

        Exact repeats from the same owner collapse into one bank record.  Agent
        inheritance is represented by memory_links rather than copied payloads.
        """
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as con:
            self._query(
                con,
                "INSERT INTO knowledge(run_id,epoch,agent_id,kind,payload_json,published_at,content_hash) VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (run_id, epoch, agent_id, kind, encoded, now, content_hash),
            )
            row = self._query(
                con,
                "SELECT id FROM knowledge WHERE run_id=? AND agent_id=? AND kind=? AND content_hash=?",
                (run_id, agent_id, kind, content_hash),
            ).fetchone()
            if not row:
                raise RuntimeError("knowledge record could not be persisted")
            return int(row["id"] if isinstance(row, dict) else row[0])

    def link_memory(self, run_id: str, agent_id: str, knowledge_id: int, inherited_from: str | None, epoch: int, now: str) -> None:
        self.execute(
            "INSERT INTO memory_links(run_id,agent_id,knowledge_id,inherited_from_agent_id,inherited_at_epoch,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (run_id, agent_id, knowledge_id, inherited_from, epoch, now),
        )

    def rate_knowledge(self, run_id: str, knowledge_id: int, agent_id: str, value: int, now: str) -> None:
        record = self.one("SELECT agent_id FROM knowledge WHERE run_id=? AND id=?", (run_id, knowledge_id))
        if not record: raise ValueError("knowledge record does not exist")
        if record["agent_id"] == agent_id: raise ValueError("agents cannot rate their own knowledge")
        if not self.memory_visible(run_id, agent_id, knowledge_id): raise ValueError("knowledge record is not visible to this agent")
        value = max(-1, min(1, int(value)))
        self.execute(
            "INSERT INTO knowledge_ratings(run_id,knowledge_id,agent_id,value,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(run_id,knowledge_id,agent_id) DO UPDATE SET value=excluded.value,created_at=excluded.created_at",
            (run_id, knowledge_id, agent_id, value, now),
        )

    def epoch_memory_trust(self, run_id: str) -> dict[tuple[str, int], float]:
        """Quality of a memory's source in the exact epoch that produced it."""
        by_epoch: dict[int, list[dict[str, Any]]] = {}
        rows = self.rows(
            "SELECT agent_id,epoch,value,tie_break_value,outcome FROM scores WHERE run_id=? "
            "ORDER BY epoch,CASE outcome WHEN 'success' THEN 0 ELSE 1 END,value DESC,tie_break_value DESC,agent_id",
            (run_id,),
        )
        for row in rows: by_epoch.setdefault(int(row["epoch"]), []).append(row)
        trust: dict[tuple[str, int], float] = {}
        for epoch, cohort in by_epoch.items():
            successful = [row for row in cohort if row["outcome"] == "success"]
            elite_count = min(max(1, round(len(cohort) * .30)), len(successful)) if successful else 0
            target_cull = max(1, round(len(cohort) * .30))
            successful_to_cull = max(0, target_cull - (len(cohort) - len(successful)))
            middle_end = max(elite_count, len(successful) - successful_to_cull)
            for rank, row in enumerate(cohort, 1):
                key = (str(row["agent_id"]), epoch)
                if row["outcome"] != "success": trust[key] = 0.10
                elif rank <= elite_count:
                    trust[key] = 1.0 - (0.10 * (rank - 1) / max(1, elite_count - 1))
                elif rank <= middle_end: trust[key] = 0.65
                else: trust[key] = 0.25
        return trust

    def agent_memory_trust(self, run_id: str) -> dict[str, float]:
        epoch_trust = self.epoch_memory_trust(run_id)
        latest: dict[str, tuple[int, float]] = {}
        for (agent_id, epoch), value in epoch_trust.items():
            if agent_id not in latest or epoch > latest[agent_id][0]: latest[agent_id] = (epoch, value)
        agents = self.rows("SELECT agent_id FROM agents WHERE run_id=?", (run_id,))
        return {str(row["agent_id"]): latest.get(str(row["agent_id"]), (0, 0.25))[1] for row in agents}

    def knowledge_rating_scores(self, run_id: str) -> dict[int, float]:
        """Return bounded, performance-weighted peer consensus.

        Self-votes are ignored even for legacy rows, and a coordinated pile of
        votes cannot grow without bound and overwhelm verified record quality.
        """
        trust = self.agent_memory_trust(run_id)
        owners = {int(row["id"]): row["agent_id"] for row in self.rows("SELECT id,agent_id FROM knowledge WHERE run_id=?", (run_id,))}
        totals: dict[int, list[float]] = {}
        for row in self.rows("SELECT knowledge_id,agent_id,value FROM knowledge_ratings WHERE run_id=?", (run_id,)):
            knowledge_id = int(row["knowledge_id"])
            if owners.get(knowledge_id) == row["agent_id"]: continue
            weight = trust.get(row["agent_id"], 0.25)
            if weight <= 0: continue
            pair = totals.setdefault(knowledge_id, [0.0, 0.0])
            pair[0] += float(row["value"]) * weight
            pair[1] += weight
        return {knowledge_id: weighted / (2.0 + total_weight) for knowledge_id, (weighted, total_weight) in totals.items()}

    def memory_visible(self, run_id: str, agent_id: str, knowledge_id: int) -> bool:
        row = self.one("SELECT agent_id,payload_json FROM knowledge WHERE run_id=? AND id=?", (run_id, knowledge_id))
        if not row: return False
        if row["agent_id"] == agent_id or json.loads(row["payload_json"]).get("visibility") == "shared": return True
        return self.one("SELECT knowledge_id FROM memory_links WHERE run_id=? AND agent_id=? AND knowledge_id=?", (run_id, agent_id, knowledge_id)) is not None

    def ranked_memory(self, run_id: str, agent_id: str, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """Return deduplicated own, inherited, and published memories by usefulness."""
        limit = max(1, min(int(limit), 500))
        linked = {int(row["knowledge_id"]): row for row in self.rows(
            "SELECT knowledge_id,inherited_from_agent_id,inherited_at_epoch FROM memory_links WHERE run_id=? AND agent_id=?",
            (run_id, agent_id),
        )}
        ratings = self.knowledge_rating_scores(run_id)
        trust = self.agent_memory_trust(run_id)
        epoch_trust = self.epoch_memory_trust(run_id)
        requester_quality = trust.get(agent_id, 0.25)
        requester_is_middle = 0.25 < requester_quality < 0.90
        terms = {term for term in re.findall(r"[a-z0-9_]{2,}", query.lower())}
        ranked: list[tuple[float, int, str, dict[str, Any]]] = []
        for row in self.rows("SELECT id,epoch,agent_id,kind,payload_json,published_at,content_hash FROM knowledge WHERE run_id=? ORDER BY id", (run_id,)):
            payload = json.loads(row["payload_json"])
            row_id = int(row["id"])
            own, inherited, shared = row["agent_id"] == agent_id, row_id in linked, payload.get("visibility") == "shared"
            if not (own or inherited or shared):
                continue
            dedup = row.get("content_hash") or hashlib.sha256(row["payload_json"].encode()).hexdigest()
            haystack = json.dumps(payload, sort_keys=True).lower()
            matches = sum(1 for term in terms if term in haystack)
            if terms and not matches:
                continue
            usefulness = ratings.get(row_id, 0.0)
            kind_weight = {"epoch_report": 20.0, "adaptation": 12.0, "adaptation_crash": 10.0, "runtime_crash": 10.0, "worker_completion": 7.0, "activity": 4.0}.get(row["kind"], 0.0)
            evidence_weight = 4.0 if payload.get("evidence_refs") or payload.get("evidence") else 0.0
            credibility = epoch_trust.get((str(row["agent_id"]), int(row["epoch"])), trust.get(row["agent_id"], 0.25))
            elite_boost = 12.0 if requester_is_middle and credibility >= 0.90 else 0.0
            score = matches * 20.0 + usefulness * 8.0 + kind_weight + evidence_weight + credibility * 20.0 + elite_boost + min(row_id, 1_000_000_000) / 1_000_000_000
            record = {"id": row_id, "epoch": row["epoch"], "agent_id": row["agent_id"], "kind": row["kind"], "published_at": str(row["published_at"]), "payload": payload, "usefulness": usefulness, "source_credibility": credibility}
            if inherited:
                record.update({"agent_id": agent_id, "memory_scope": "inherited", "source_agent_id": row["agent_id"], "inherited_from_agent_id": linked[row_id]["inherited_from_agent_id"], "original_record_id": row_id})
                score += 4.0
            elif own:
                record["memory_scope"] = "own"
                score += 3.0
            else:
                record["memory_scope"] = "population"
            ranked.append((score, row_id, dedup, record))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected: list[dict[str, Any]] = []; seen: set[str] = set()
        for _score, _row_id, dedup, record in ranked:
            if dedup in seen: continue
            seen.add(dedup); selected.append(record)
            if len(selected) >= limit: break
        return selected

    def add_event(self, run_id: str, event_type: str, payload: dict[str, Any], now: str) -> None:
        self.execute("INSERT INTO events(run_id,event_type,payload_json,created_at) VALUES(?,?,?,?)", (run_id, event_type, json.dumps(payload, sort_keys=True), now))

    def delete_run(self, run_id: str) -> None:
        """Atomically remove one run's relational state."""
        tables = ("knowledge_ratings", "memory_links", "knowledge", "events", "agent_attempts", "epochs", "lineage", "scores", "agents", "genomes", "runs")
        with self.connect() as con:
            for table in tables:
                column = "id" if table == "runs" else "run_id"
                self._query(con, f"DELETE FROM {table} WHERE {column}=?", (run_id,))
