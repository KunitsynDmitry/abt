"""DatabaseManager — SQLite persistence for agent runs, traces, and tool cache."""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    project_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    final_state TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS llm_traces (
    trace_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_name TEXT NOT NULL,
    step_name TEXT,
    request_messages TEXT NOT NULL,
    response_content TEXT,
    tool_calls TEXT,
    latency_ms INTEGER,
    tokens_input INTEGER,
    tokens_output INTEGER,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);

CREATE TABLE IF NOT EXISTS tool_results (
    cache_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    input_params TEXT NOT NULL,
    output_data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_executions (
    execution_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    input_state TEXT,
    output_state TEXT,
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);

CREATE TABLE IF NOT EXISTS node_cache (
    node_name TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    outputs_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (node_name, inputs_hash)
);

CREATE INDEX IF NOT EXISTS idx_llm_traces_run ON llm_traces(run_id);
CREATE INDEX IF NOT EXISTS idx_node_exec_run ON node_executions(run_id);
CREATE INDEX IF NOT EXISTS idx_tool_results_run ON tool_results(run_id);
"""


class DatabaseManager:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def connect(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Thread-safe execute with lock."""
        with self._lock:
            return self.conn.execute(sql, params)

    def _commit(self):
        """Thread-safe commit with lock."""
        with self._lock:
            self._conn.commit()

    # ── Run management ──────────────────────────────────────────────

    def create_run(self, thread_id: str | None = None, project_name: str = "") -> str:
        run_id = str(uuid.uuid4())[:8]
        thread_id = thread_id or run_id
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO agent_runs (run_id, thread_id, project_name, started_at) VALUES (?, ?, ?, ?)",
            (run_id, thread_id, project_name, now),
        )
        self._commit()
        return run_id

    def complete_run(self, run_id: str, status: str = "completed",
                     final_state: dict | None = None, error: str | None = None):
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "UPDATE agent_runs SET completed_at=?, status=?, final_state=?, error_message=? WHERE run_id=?",
            (now, status, json.dumps(final_state) if final_state else None, error, run_id),
        )
        self._commit()

    # ── LLM tracing ─────────────────────────────────────────────────

    def log_llm_call(
        self, run_id: str, node_name: str, step_name: str,
        messages: list, model: str, latency_ms: int = 0,
        response_content: str | None = None,
        tool_calls: list | None = None,
        tokens_input: int = 0, tokens_output: int = 0,
    ) -> str:
        trace_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            """INSERT INTO llm_traces
               (trace_id, run_id, node_name, step_name, request_messages,
                response_content, tool_calls, latency_ms, tokens_input, tokens_output,
                model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trace_id, run_id, node_name, step_name,
             json.dumps(messages, default=str),
             response_content,
             json.dumps(tool_calls, default=str) if tool_calls else None,
             latency_ms, tokens_input, tokens_output,
             model, now),
        )
        self._commit()
        return trace_id

    # ── Tool result caching ─────────────────────────────────────────

    def get_cached_tool_result(self, cache_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT output_data FROM tool_results WHERE cache_key=? ORDER BY created_at DESC LIMIT 1",
            (cache_key,),
        ).fetchone()
        if row:
            return json.loads(row["output_data"])
        return None

    def cache_tool_result(self, run_id: str, cache_key: str,
                          source_name: str, table_name: str,
                          input_params: dict, output_data: dict):
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT OR REPLACE INTO tool_results VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cache_key, run_id, source_name, table_name,
             json.dumps(input_params), json.dumps(output_data), now),
        )
        self._commit()

    # ── Node output caching (incremental execution) ──────────────────

    def get_cached_node_output(self, node_name: str, inputs_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT outputs_json FROM node_cache WHERE node_name=? AND inputs_hash=? "
            "ORDER BY created_at DESC LIMIT 1",
            (node_name, inputs_hash),
        ).fetchone()
        if row:
            return json.loads(row["outputs_json"])
        return None

    def cache_node_output(self, node_name: str, inputs_hash: str, output: dict):
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT OR REPLACE INTO node_cache VALUES (?, ?, ?, ?)",
            (node_name, inputs_hash, json.dumps(output), now),
        )
        self._commit()

    # ── Node execution tracking ─────────────────────────────────────

    def log_node_start(self, run_id: str, node_name: str, input_state: dict) -> str:
        execution_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO node_executions (execution_id, run_id, node_name, started_at, input_state) VALUES (?,?,?,?,?)",
            (execution_id, run_id, node_name, now, json.dumps(input_state, default=str)),
        )
        self._commit()
        return execution_id

    def log_node_complete(self, execution_id: str, output_state: dict):
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "UPDATE node_executions SET completed_at=?, status='completed', output_state=? WHERE execution_id=?",
            (now, json.dumps(output_state, default=str), execution_id),
        )
        self._commit()

    def log_node_retry(self, execution_id: str, attempt: int, error: str):
        self._execute(
            "UPDATE node_executions SET retry_count=?, error_message=? WHERE execution_id=?",
            (attempt, error, execution_id),
        )
        self._commit()

    def log_node_failed(self, execution_id: str, error: str):
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "UPDATE node_executions SET completed_at=?, status='failed', error_message=? WHERE execution_id=?",
            (now, error, execution_id),
        )
        self._commit()

    # ── Query helpers ───────────────────────────────────────────────

    def get_run_traces(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM llm_traces WHERE run_id=? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_node_history(self, node_name: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM node_executions WHERE node_name=? ORDER BY started_at DESC LIMIT 50",
            (node_name,),
        ).fetchall()
        return [dict(r) for r in rows]
