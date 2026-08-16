"""Tiny web UI for hand-labelling collected captchas.

Run ``tenders-captcha-label`` and open the page; it shows one captcha at a time
(enlarged, on a white background), pre-filled with the current OCR guess so you
usually just fix a character or two and press Enter. Labels are saved to
data/captcha/labels.json. Stop anytime; progress is preserved.
"""

from __future__ import annotations

import base64
import io
import json

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from PIL import Image

from .config import load_config
from .captcha_model import NUM_CHARS, labels_path, raw_dir

# How many *usable* (non-skipped) labels we're aiming for. A small task-specific
# CNN needs a few hundred examples to generalise over the noise; below ~100 it
# tends to score 0% on validation.
GOAL = 300


def _png_data_uri(path, scale: int = 3) -> str:
    img = Image.open(path).convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    comp = Image.alpha_composite(bg, img).convert("RGB")
    comp = comp.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    buf = io.BytesIO()
    comp.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _guess(path, vision: dict | None = None) -> str:
    # `vision` is a legacy guesses.json from the removed pre-labeller, which
    # read captchas with a hosted vision model. Nothing produces one now: this
    # project must run with no external service (see captcha.py's docstring),
    # and the labelling UI it fed exists to bootstrap a model that today reads
    # 1,640 of 1,640 real portal captchas anyway. Still honoured when present so
    # an existing working copy does not lose its head start; Tesseract
    # otherwise.
    if vision and path.stem in vision:
        return vision[path.stem]
    try:
        from .captcha import preprocess, ocr, tesseract_available

        if tesseract_available():
            return ocr(preprocess(Image.open(path)))
    except Exception:  # noqa: BLE001
        pass
    return ""


def make_app() -> FastAPI:
    cfg = load_config()
    cfg.ensure_dirs()
    app = FastAPI(title="Captcha labeller")
    rd = raw_dir(cfg)
    lp = labels_path(cfg)
    # Any guesses.json left by the old vision pre-labeller is still read if it
    # happens to exist, but nothing writes one any more — see _guess below.
    gp = lp.parent / "guesses.json"

    def load_labels() -> dict:
        return json.loads(lp.read_text()) if lp.exists() else {}

    def load_vision() -> dict:
        try:
            return json.loads(gp.read_text()) if gp.exists() else {}
        except Exception:  # noqa: BLE001
            return {}

    def save_labels(d: dict) -> None:
        lp.write_text(json.dumps(d, indent=0))

    def next_unlabelled(labels: dict, vision: dict):
        # Prefer captchas the vision worker has already read (instant pre-fill);
        # only fall back to a not-yet-read one if none are ready.
        fallback = None
        for p in sorted(rd.glob("*.png")):
            if p.name in labels:
                continue
            if p.stem in vision:
                return p
            if fallback is None:
                fallback = p
        return fallback

    def _progress(labels: dict):
        total = len(list(rd.glob("*.png")))
        done = len(labels)                                   # seen (incl. skips)
        usable = sum(1 for v in labels.values() if v.strip())  # trainable
        left = max(0, GOAL - usable)
        pct = min(100, round(usable / GOAL * 100)) if GOAL else 100
        return total, done, usable, left, pct

    def _bar(usable: int, pct: int, left: int, total: int) -> str:
        return f"""
        <div style="margin:0 auto 18px;max-width:480px">
          <div style="display:flex;justify-content:space-between;font-size:14px;color:#444">
            <b>{usable}/{GOAL} usable labels</b><span>{left} left · {total} collected</span>
          </div>
          <div style="height:14px;background:#eee;border-radius:7px;overflow:hidden;margin-top:4px">
            <div style="height:100%;width:{pct}%;background:#2e7d32;transition:width .2s"></div>
          </div>
        </div>"""

    @app.get("/", response_class=HTMLResponse)
    def index():
        labels = load_labels()
        vision = load_vision()
        total, done, usable, left, pct = _progress(labels)
        p = next_unlabelled(labels, vision)

        if p is None:
            # Caught up. If we've hit the goal, celebrate; otherwise the
            # collector is still fetching — auto-refresh and wait for more.
            if usable >= GOAL:
                return f"""<html><head><title>Done</title>
                <style>body{{font-family:sans-serif;max-width:560px;margin:60px auto;text-align:center}}</style>
                </head><body><h2>Goal reached 🎉</h2>
                {_bar(usable, pct, left, total)}
                <p>Now run <code>tenders-captcha-train</code>.</p></body></html>"""
            return f"""<html><head><title>Waiting for captchas</title>
            <meta http-equiv="refresh" content="4">
            <style>body{{font-family:sans-serif;max-width:560px;margin:60px auto;text-align:center}}
            .muted{{color:#888}}</style></head><body>
            <h3>Caught up — waiting for the collector to fetch more…</h3>
            {_bar(usable, pct, left, total)}
            <p class="muted">All {total} collected so far are labelled. This page
            refreshes every 4s; new captchas appear automatically. Keep it open.</p>
            </body></html>"""

        guess = _guess(p, vision)
        src = "👁 vision" if p.stem in vision else "tesseract"
        return f"""
        <html><head><title>Label captchas — {usable}/{GOAL}</title>
        <style>body{{font-family:sans-serif;max-width:560px;margin:40px auto;text-align:center}}
        img{{image-rendering:pixelated;border:1px solid #ccc;background:#fff;padding:8px}}
        input{{font-size:22px;padding:10px;width:240px;text-align:center;letter-spacing:3px}}
        .muted{{color:#888}} button{{font-size:16px;padding:10px 20px}}</style></head>
        <body>
        {_bar(usable, pct, left, total)}
        <p class="muted">{p.name} · pre-fill: {src}</p>
        <img src="{_png_data_uri(p)}" alt="captcha"><br><br>
        <form method="post" action="/save" autocomplete="off">
          <input type="hidden" name="name" value="{p.name}">
          <input name="label" value="{guess}" autofocus
                 onfocus="this.select()" placeholder="type the {NUM_CHARS} characters"><br><br>
          <button type="submit">Save &amp; next (Enter)</button>
          <button type="submit" name="skip" value="1" formnovalidate>Skip (unreadable)</button>
        </form>
        <p class="muted">Type exactly what you see, case-sensitive. Guess pre-filled.</p>
        </body></html>
        """

    @app.post("/save")
    def save(name: str = Form(...), label: str = Form(""), skip: str = Form("")):
        labels = load_labels()
        if skip:
            labels[name] = ""  # mark seen-but-unreadable; excluded from training
        else:
            labels[name] = label.strip()
        save_labels(labels)
        return RedirectResponse("/", status_code=303)

    return app


app = make_app()
