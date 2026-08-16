"""Collect raw captcha images for labelling/training.

Captchas only appear on the document-download (DocDownCaptcha) page, so we visit
active tenders that have a live document, open the download gate, and save the
inline captcha image. Load is spread across many tenders (a few captchas each)
to stay polite, and is rate-limited by HttpClient.
"""

from __future__ import annotations

import base64
import logging
import re

from .config import load_config
from .db import connect, init_db
from .http_client import HttpClient, RequestCapExceeded
from .jsf import extract_form
from .parse_detail import parse_detail

log = logging.getLogger("captcha_collect")

_B64 = re.compile(r"base64,([A-Za-z0-9+/=\s]+)")


def collect(db_path=None, *, target: int = 400, per_tender: int = 6) -> dict:
    cfg = load_config()
    db_path = db_path or cfg.db_path
    cfg.ensure_dirs()
    init_db(db_path)
    from .captcha_model import raw_dir

    out_dir = raw_dir(cfg)
    existing = len(list(out_dir.glob("*.png")))
    conn = connect(db_path)
    client = HttpClient(cfg)
    saved = 0
    seq = existing
    try:
        # Captchas only exist on the document-download gate, which only renders
        # for tenders whose files are still live. CSV-seed tenders are all
        # expired (documents purged), so restrict to org-tree-scraped (active)
        # tenders — otherwise we waste rate-limited fetches on thousands of
        # expired tenders that yield nothing.
        rows = conn.execute(
            "SELECT tender_id, detail_url FROM tenders "
            "WHERE detail_url IS NOT NULL AND source = 'scraped' "
            "ORDER BY closing_date DESC"
        ).fetchall()
        log.info("collecting from %d active (scraped) tenders, target %d",
                 len(rows), target)
        for row in rows:
            if saved >= target:
                break
            try:
                parsed = parse_detail(client.get(row["detail_url"]).text,
                                      base_url=cfg.base_url)
            except RequestCapExceeded:
                break
            except Exception:  # noqa: BLE001
                continue
            live = [d["download_url"] for d in parsed["documents"] if d["download_url"]]
            if not live:
                continue
            dl = live[0]
            for _ in range(per_tender):
                if saved >= target:
                    break
                try:
                    page = client.get(dl)
                except RequestCapExceeded:
                    conn.close()
                    return {"saved": saved, "total": existing + saved, "capped": True}
                form = extract_form(page.text, "frmCaptcha")
                if not form or not form.get("captcha_src"):
                    break
                m = _B64.search(form["captcha_src"])
                if not m:
                    continue
                data = base64.b64decode(re.sub(r"\s+", "", m.group(1)))
                seq += 1
                (out_dir / f"cap_{seq:05d}.png").write_bytes(data)
                saved += 1
                if saved % 10 == 0:
                    log.info("collected %d/%d captchas (total on disk %d)",
                             saved, target, existing + saved)
    finally:
        conn.close()
    return {"saved": saved, "total": existing + saved}
