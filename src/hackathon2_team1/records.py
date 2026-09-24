"""Assessment record store (SQLite) - the 'enterprise system of record' behind the MCP server."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


class RecordStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS assessments (
                    assessment_id TEXT PRIMARY KEY, vendor TEXT, recommendation TEXT, overall_risk TEXT,
                    status TEXT, payload TEXT, recorded_by TEXT, created_at TEXT, updated_at TEXT)"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS human_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT, decision TEXT, approver TEXT,
                    role TEXT, comments TEXT, created_at TEXT)"""
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record_assessment(self, assessment_id: str, vendor: str, recommendation: str, overall_risk: str,
                          payload: dict, recorded_by: str) -> dict:
        now = datetime.now(UTC).isoformat()
        with self._conn() as c:
            c.execute(
                """INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(assessment_id) DO UPDATE SET recommendation=excluded.recommendation,
                   overall_risk=excluded.overall_risk, payload=excluded.payload, updated_at=excluded.updated_at""",
                (assessment_id, vendor, recommendation, overall_risk, "PENDING_HUMAN_REVIEW",
                 json.dumps(payload), recorded_by, now, now),
            )
        return {"assessment_id": assessment_id, "status": "PENDING_HUMAN_REVIEW", "recorded_at": now}

    def record_human_decision(self, assessment_id: str, decision: str, approver: str, role: str, comments: str) -> dict:
        now = datetime.now(UTC).isoformat()
        status = {"APPROVE": "APPROVED", "CONDITIONAL APPROVAL": "CONDITIONALLY_APPROVED", "REJECT": "REJECTED"}[decision]
        with self._conn() as c:
            c.execute(
                "INSERT INTO human_decisions (assessment_id, decision, approver, role, comments, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (assessment_id, decision, approver, role, comments, now),
            )
            c.execute("UPDATE assessments SET status=?, updated_at=? WHERE assessment_id=?", (status, now, assessment_id))
        return {"assessment_id": assessment_id, "status": status, "decided_by": approver, "decided_at": now}

    def prior_assessments(self, vendor: str | None = None, limit: int = 10) -> list[dict]:
        q = "SELECT assessment_id, vendor, recommendation, overall_risk, status, created_at FROM assessments"
        args: tuple = ()
        if vendor:
            q += " WHERE lower(vendor) LIKE ?"
            args = (f"%{vendor.lower()}%",)
        q += " ORDER BY created_at DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(q, (*args, limit)).fetchall()
        keys = ["assessment_id", "vendor", "recommendation", "overall_risk", "status", "created_at"]
        return [dict(zip(keys, r)) for r in rows]
