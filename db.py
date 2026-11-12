"""
SQLite persistence layer for the review pipeline.

Tracks proposals (what the pipeline found) and decisions (what the reviewer
approved or rejected).  Idempotency guarantee: re-running the pipeline never
re-proposes an item that already has a decision.

Schema
------
proposals
  id              TEXT PRIMARY KEY  -- deterministic hash of (scraped_name, crm_id)
  match_type      TEXT
  scraped_json    TEXT              -- JSON blob of scraped location
  crm_json        TEXT              -- JSON blob of matched CRM account (nullable)
  issues_json     TEXT              -- JSON list of issue strings
  proposed_changes_json TEXT        -- JSON dict of field -> new value
  evidence_json   TEXT              -- JSON dict shown in review UI
  created_at      TEXT

decisions
  proposal_id     TEXT PRIMARY KEY
  decision        TEXT              -- 'approved' | 'rejected'
  note            TEXT
  decided_at      TEXT
  crm_result_json TEXT              -- response from CRM API after approved write
"""

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "pipeline.db"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _proposal_id(scraped_name: str, crm_id: Optional[str]) -> str:
    key = f"{scraped_name or ''}::{crm_id or 'NEW'}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


@contextmanager
def _conn(db_path: Path = DB_PATH):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with _conn(db_path) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS proposals (
                id                     TEXT PRIMARY KEY,
                match_type             TEXT NOT NULL,
                scraped_json           TEXT,
                crm_json               TEXT,
                issues_json            TEXT,
                proposed_changes_json  TEXT,
                evidence_json          TEXT,
                created_at             TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decisions (
                proposal_id    TEXT PRIMARY KEY,
                decision       TEXT NOT NULL,
                note           TEXT,
                decided_at     TEXT NOT NULL,
                crm_result_json TEXT
            );
        """)
    logger.debug("Database initialised at %s", db_path)


def upsert_proposal(
    match_type: str,
    scraped: Optional[dict],
    crm_account: Optional[dict],
    issues: list[str],
    proposed_changes: dict,
    evidence: dict,
    db_path: Path = DB_PATH,
) -> Optional[str]:
    """
    Insert or ignore a proposal.  Returns the proposal ID, or None if
    this item already has a decision (idempotency guard).
    """
    scraped_name = (scraped or {}).get("name", "")
    crm_id = (crm_account or {}).get("id")
    pid = _proposal_id(scraped_name, crm_id)

    with _conn(db_path) as con:
        # Check if already decided
        row = con.execute(
            "SELECT decision FROM decisions WHERE proposal_id = ?", (pid,)
        ).fetchone()
        if row:
            logger.debug("Skipping already-decided proposal %s (%s)", pid, row["decision"])
            return None

        # Re-runs regenerate the same id. A still-pending proposal is refreshed
        # with today's data (keeping its original created_at); decided ones
        # returned above and are never touched.
        con.execute(
            """
            INSERT INTO proposals
                (id, match_type, scraped_json, crm_json,
                 issues_json, proposed_changes_json, evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                match_type = excluded.match_type,
                scraped_json = excluded.scraped_json,
                crm_json = excluded.crm_json,
                issues_json = excluded.issues_json,
                proposed_changes_json = excluded.proposed_changes_json,
                evidence_json = excluded.evidence_json
            """,
            (
                pid,
                match_type,
                json.dumps(scraped),
                json.dumps(crm_account),
                json.dumps(issues),
                json.dumps(proposed_changes),
                json.dumps(evidence),
                _utcnow(),
            ),
        )
    return pid


def location_reviewed(scraped_name: str, db_path: Path = DB_PATH) -> bool:
    """Has a reviewer already decided some proposal for this website location?

    Approving a fix changes the CRM, so the next run sees a *different* match
    (e.g. a CHOW or new account has a new id) and a different proposal id. A
    confident match for an already-reviewed location is therefore not news.
    """
    with _conn(db_path) as con:
        row = con.execute(
            """
            SELECT 1 FROM proposals p
            JOIN decisions d ON d.proposal_id = p.id
            WHERE json_extract(p.scraped_json, '$.name') = ?
            LIMIT 1
            """,
            (scraped_name,),
        ).fetchone()
    return row is not None


def get_pending_proposals(db_path: Path = DB_PATH) -> list[dict]:
    """Return proposals that have no decision yet."""
    with _conn(db_path) as con:
        rows = con.execute(
            """
            SELECT p.*
            FROM proposals p
            LEFT JOIN decisions d ON d.proposal_id = p.id
            WHERE d.proposal_id IS NULL
            ORDER BY p.created_at
            """
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_proposals(db_path: Path = DB_PATH) -> list[dict]:
    """Return all proposals with their decision status."""
    with _conn(db_path) as con:
        rows = con.execute(
            """
            SELECT p.*,
                   d.decision,
                   d.note AS decision_note,
                   d.decided_at,
                   d.crm_result_json
            FROM proposals p
            LEFT JOIN decisions d ON d.proposal_id = p.id
            ORDER BY p.created_at
            """
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_proposal(pid: str, db_path: Path = DB_PATH) -> Optional[dict]:
    with _conn(db_path) as con:
        row = con.execute(
            """
            SELECT p.*,
                   d.decision,
                   d.note AS decision_note,
                   d.decided_at,
                   d.crm_result_json
            FROM proposals p
            LEFT JOIN decisions d ON d.proposal_id = p.id
            WHERE p.id = ?
            """,
            (pid,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def record_decision(
    proposal_id: str,
    decision: str,
    note: str = "",
    crm_result: Optional[dict] = None,
    db_path: Path = DB_PATH,
) -> None:
    assert decision in ("approved", "rejected"), f"Invalid decision: {decision!r}"
    with _conn(db_path) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO decisions
                (proposal_id, decision, note, decided_at, crm_result_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                decision,
                note,
                _utcnow(),
                json.dumps(crm_result) if crm_result else None,
            ),
        )
    logger.info("Recorded decision %s for proposal %s", decision, proposal_id)


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    for json_col in [
        "scraped_json", "crm_json", "issues_json",
        "proposed_changes_json", "evidence_json", "crm_result_json",
    ]:
        if json_col in d and d[json_col]:
            try:
                d[json_col] = json.loads(d[json_col])
            except (json.JSONDecodeError, TypeError):
                pass
    return d
