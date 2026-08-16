"""Summary counts for the CLI and the web /healthz route."""

from __future__ import annotations

from .config import load_config
from .db import connect


def gather_stats(db_path=None) -> dict:
    cfg = load_config()
    db_path = db_path or cfg.db_path
    conn = connect(db_path, read_only=True)
    try:
        def one(sql, *args):
            r = conn.execute(sql, args).fetchone()
            return r[0] if r else 0

        return {
            "tenders_total": one("SELECT count(*) FROM tenders"),
            "tenders_csv": one("SELECT count(*) FROM tenders WHERE source='csv'"),
            "tenders_scraped": one("SELECT count(*) FROM tenders WHERE source='scraped'"),
            "tenders_discovered": one("SELECT count(*) FROM tenders WHERE status='discovered'"),
            "documents_total": one("SELECT count(*) FROM documents"),
            "documents_captured": one("SELECT count(*) FROM documents WHERE status='captured'"),
            "documents_lost": one("SELECT count(*) FROM documents WHERE status='lost'"),
            "documents_pending": one("SELECT count(*) FROM documents WHERE status='pending'"),
            "documents_failed": one("SELECT count(*) FROM documents WHERE status='failed'"),
            "docs_text_extracted": one("SELECT count(*) FROM doc_text"),
            "last_forward_run": one(
                "SELECT max(fetched_at) FROM fetch_log WHERE kind='listing'"
            ),
        }
    finally:
        conn.close()
