"""Solve the GePNIC image captcha (used to gate document downloads).

The captcha is a fixed-length 6-character alphanumeric image embedded in the
``DocDownCaptcha`` page as an inline ``data:image/png;base64`` PNG. The text is
pure black; on top of it sit blue speckle, solid black squares and thin
coloured strike-through lines. Removing non-black pixels clears the speckle and
the lines, but the black squares survive and fuse with the glyphs — which is why
Tesseract only manages ~15% here and a trained CNN (``captcha_model``) does the
real work.

**Everything here runs locally.** Nothing in this module — or anywhere on the
capture path — calls a network service, a hosted model or an external CLI to
read a captcha. That is a hard requirement of the project, not a preference: a
public-interest archive that depends on somebody else's API to function can be
switched off by that somebody, and this one exists precisely because the
material it preserves is inconvenient to powerful people.

This file used to carry a fallback that shelled out to a headless ``claude -p``
for vision when no trained model existed. It was measured and removed. By then
the CNN in ``captcha_model`` was reading **1,640 of 1,640** real portal
captchas correctly — every one whose answer the portal itself had confirmed —
and the vision path had been invoked **zero** times in production across 1,675
solves. The cold start it was meant to cover no longer exists either:
``captcha_synth`` reconstructs the portal's own generator (DejaVu Sans 38px)
well enough to train from scratch offline, so a fresh checkout needs no labels
and no outside help.

Fallbacks for the case where no usable model has been trained yet are therefore
local-only: Tesseract (~15% here — the black squares fuse with the glyphs), then
a manual prompt (config ``captcha_manual``).
"""

from __future__ import annotations

import base64
import logging
import re
from collections import deque
from io import BytesIO

from PIL import Image

log = logging.getLogger("captcha")

_DATA_URI = re.compile(r'data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)')
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def tesseract_available() -> bool:
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001
        return False


def decode_data_uri(src: str) -> Image.Image:
    m = _DATA_URI.search(src)
    raw = m.group(1) if m else src
    data = base64.b64decode(re.sub(r"\s+", "", raw))
    return Image.open(BytesIO(data))


def _components(px, w: int, h: int):
    """Yield connected black components as lists of (x, y) points (8-connectivity)."""
    seen = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            if px[x, y] == 0 and not seen[y * w + x]:
                comp = []
                q = deque([(x, y)])
                seen[y * w + x] = 1
                while q:
                    cx, cy = q.popleft()
                    comp.append((cx, cy))
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            nx, ny = cx + dx, cy + dy
                            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] \
                                    and px[nx, ny] == 0:
                                seen[ny * w + nx] = 1
                                q.append((nx, ny))
                yield comp


def _denoise(px, w: int, h: int, speck: int, block_fill: float, line_thin: float):
    """Remove three kinds of GePNIC captcha noise, in place:

    * speckle  — components smaller than ``speck`` pixels,
    * solid blocks — near-fully-filled bounding boxes (glyphs are ~half-filled),
    * thin lines — long components only 1–2 px thick (diagonal strike-throughs).
    """
    for comp in _components(px, w, h):
        xs = [p[0] for p in comp]
        ys = [p[1] for p in comp]
        cw = max(xs) - min(xs) + 1
        ch = max(ys) - min(ys) + 1
        n = len(comp)
        fill = n / (cw * ch)
        thinness = n / max(cw, ch)
        if (n < speck
                or (fill >= block_fill and min(cw, ch) >= 5)
                or (thinness < line_thin and max(cw, ch) > 10)):
            for cx, cy in comp:
                px[cx, cy] = 255


def preprocess(img: Image.Image, scale: int = 6, denoise: bool = False) -> Image.Image:
    """Produce an upscaled binary image for OCR.

    Composite on white → keep only near-black pixels (this alone removes the blue
    speckle noise, which dominates) → upscale. This minimal pipeline is, in live
    testing, the most accurate: aggressive component-based denoising of solid
    blocks / strike-through lines removed glyph fragments too and hurt accuracy,
    so it is opt-in via ``denoise`` (kept for experimentation, off by default).
    """
    import numpy as np

    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    comp = Image.alpha_composite(bg, img).convert("RGB")
    w, h = comp.size
    mask = (np.asarray(comp) < 110).all(axis=2)
    # frombytes rather than fromarray: fromarray wraps the numpy buffer
    # read-only, and _denoise writes through the PixelAccess in place.
    out = Image.frombytes("L", (w, h), np.where(mask, 0, 255).astype("uint8").tobytes())
    if denoise:
        _denoise(out.load(), w, h, speck=6, block_fill=0.97, line_thin=2.6)
    return out.resize((w * scale, h * scale), Image.LANCZOS)


_WHITELIST = (
    "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz0123456789"
)


def _ocr_with(img: Image.Image, psm: int) -> str:
    import pytesseract

    cfg = f"--psm {psm} --oem 3 {_WHITELIST}"
    return _NON_ALNUM.sub("", pytesseract.image_to_string(img, config=cfg))


def ocr(img: Image.Image) -> str:
    return _ocr_with(img, 7)


# Consecutive model answers that the portal has never confirmed. The document
# gate gets 15 fresh captchas per tender, and a model at 0.8 misses all 15 about
# once in 10^10 attempts — so a run this long means the model has stopped
# working (the portal changed its generator, most likely), not bad luck. At
# that point we stop trusting it and fall through to Tesseract, which is far
# worse but local, and keeps the archive limping while someone retrains. Any
# server-confirmed solve, by either route, clears the streak.
#
# The streak is process-global, which is only sound while the solves feeding it
# are. A call site that solves captchas but never reports the portal's verdict
# ratchets it to the limit on its own and demotes *every* other call site to the
# slow fallback — which is exactly what happened when the newest-first poll's
# form submission broke: a bug on one page silently took the document downloads
# off the CNN. Hence ``tracked``: only a caller that confirms an accepted solve
# may push the counter up. See solve_image.
_CNN_STREAK_LIMIT = 15
_unverified_streak = 0


def save_verified_label(data_uri: str, text: str, cfg) -> str | None:
    """Save a server-confirmed captcha solution as a verified training label.

    Called after the portal accepts the captcha POST (no 'Invalid Captcha'
    response), so every successful document download automatically grows the
    CNN training set without any manual effort.

    Returns the saved filename (e.g. 'cap_00317.png'), or None on error.
    """
    import json

    global _unverified_streak
    _unverified_streak = 0

    from .captcha_model import labels_path, raw_dir, verified_path

    try:
        m = _DATA_URI.search(data_uri)
        raw_bytes = base64.b64decode(re.sub(r"\s+", "", m.group(1))) if m else base64.b64decode(
            re.sub(r"\s+", "", data_uri.split(",", 1)[-1])
        )
        out_dir = raw_dir(cfg)
        out_dir.mkdir(parents=True, exist_ok=True)
        lp = labels_path(cfg)
        labels = json.loads(lp.read_text()) if lp.exists() else {}
        existing = sorted(out_dir.glob("cap_*.png"))
        seq = int(existing[-1].stem.split("_")[1]) + 1 if existing else 1
        fname = f"cap_{seq:05d}.png"
        (out_dir / fname).write_bytes(raw_bytes)
        labels[fname] = text
        lp.write_text(json.dumps(labels, indent=2, ensure_ascii=False))
        # Record provenance: unlike the hand labels, this one was checked by the
        # portal, so the trainer can eventually validate on these alone.
        vp = verified_path(cfg)
        seen = json.loads(vp.read_text()) if vp.exists() else []
        seen.append(fname)
        vp.write_text(json.dumps(seen, indent=0))
        log.debug("auto-labelled captcha %s = %r", fname, text)
        return fname
    except Exception as exc:  # noqa: BLE001
        log.debug("save_verified_label failed: %s", exc)
        return None


def solve_image(src: str, *, manual: bool = False,
                tracked: bool = True) -> str | None:
    """Return the best-guess captcha text, or None if unsolvable.

    Priority order, all of it local (see the module docstring):
      1. Trained CNN  — ~10ms, offline, needs data/captcha/model.pt to exist
                        *and* to have cleared MIN_USABLE_VAL_ACC
      2. Tesseract OCR — ~15% accuracy, multi-second retries
      3. Manual       — operator types it (config captcha_manual=true)

    Only step 1 runs in practice: measured against every captcha the portal has
    confirmed an answer for, the CNN reads 1,640 of 1,640. Steps 2-3 are the
    cold-start path for a checkout that has not trained yet, and the supported
    way to leave that state is ``tenders-captcha-train`` on synthetic data —
    no labels, no network. The caller retries with a *fresh* captcha on failure
    (scrape.captcha_attempts, default 15), so per-attempt accuracy compounds:
    a model at 0.8 fails a whole tender about once in 10^10 tries.

    ``tracked`` says this solve may count towards the unconfirmed-solve breaker,
    and must be left True only by a call site that calls ``save_verified_label``
    when the portal accepts an answer — i.e. one whose silence really does mean
    "the model was wrong". An untracked caller still *benefits* from the breaker
    (a broken model demotes it too) and may still clear it by confirming a
    solve; it just cannot trip it for everyone else.
    """
    global _unverified_streak

    img = decode_data_uri(src)
    if manual:
        return _manual(img)

    # 1. Trained CNN.
    try:
        from .config import load_config
        from .captcha_model import TrainedSolver

        solver = TrainedSolver.get(load_config())
        if solver is not None and _unverified_streak < _CNN_STREAK_LIMIT:
            text = solver.predict(img)
            # INFO, not debug: which solver answered is the one thing an
            # operator needs from the log to confirm the trained model — and
            # not the degraded fallback — is the one doing the work.
            log.info("captcha: CNN -> %r", text)
            if text:
                if tracked:
                    _unverified_streak += 1
                    if _unverified_streak == _CNN_STREAK_LIMIT:
                        log.warning(
                            "captcha model has produced %d unconfirmed solves in a "
                            "row at the document gate — falling back to tesseract "
                            "until one is accepted; retrain the model",
                            _CNN_STREAK_LIMIT)
                return text
    except Exception as exc:  # noqa: BLE001
        log.debug("trained captcha model unavailable: %s", exc)

    # 2. Tesseract fallback — poor, but local, and it keeps a fresh checkout
    #    collecting until `tenders-captcha-train` has produced a model.
    if not tesseract_available():
        log.warning("no captcha solver available (no trained model, no tesseract)")
        return None
    text = _ocr_with(preprocess(img), 7)
    log.info("captcha: tesseract -> %r", text)
    return text or None


def _manual(img: Image.Image) -> str | None:  # pragma: no cover - interactive
    import tempfile

    path = tempfile.mktemp(suffix=".png")
    preprocess(img, scale=4).save(path)
    print(f"\n[captcha] Open this image and type the text: {path}")
    try:
        return input("captcha> ").strip() or None
    except EOFError:
        return None
