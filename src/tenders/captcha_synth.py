"""Synthetic GePNIC captcha generator, reverse-engineered from the real images.

The training set is tiny (a few hundred hand-labelled captchas) because a
captcha only appears once per scrape session. Rather than wait months for it to
grow, we reproduce the portal's generator and train on unlimited exact-labelled
samples.

The reconstruction is not guesswork — every constant below was measured against
``data/captcha/raw``:

* **Font.** Rendering each of the 49 observed characters at a range of sizes and
  scoring IoU against per-class median glyphs extracted from real captchas puts
  DejaVu Sans 38px far ahead of every other installed face (0.81 vs 0.70 for the
  runner-up), and the well-sampled classes match to 0.95–0.99 with *identical*
  bounding-box dimensions. That is a font identification, not a resemblance.
* **No anti-aliasing.** Real captchas contain exactly three colours — black,
  white and (0,0,255) — and their alpha channel is strictly 0 or 255, so the
  portal rasterises with AA off. We render to greyscale and hard-threshold to
  match; anti-aliased edges would otherwise survive ``to_tensor``'s <110 cut as
  spurious ink.
* **Where that threshold sits.** Not at the midpoint. Real stems are 4px wide in
  50% of x-height scanlines and 3px in only 2%; cutting DejaVu's coverage ramp at
  128 produces 3px stems 23% of the time, which is a visibly lighter face than
  the portal's. Sweeping the cut puts ``<160`` on top: 3px runs fall to 3.4% and
  glyph ink lands within 1% of real. The portal keeps more of the coverage ramp
  than a naive midpoint cut does.
* **Layout.** Fitting ``ink_left = x0 + Σadvance + gap·i + bearing`` over the
  cleanly segmentable real captchas gives a left margin of 0–4px and an
  inter-glyph gap of ~11.8px, at a fixed baseline (the measured vertical offset
  is 0 for 60% of glyphs and the rest is block contamination — the portal does
  not jitter y).
* **Noise.** ~30 solid black squares of side 3–9 per image, drawn *over* the
  text (they merge with glyphs, which is what breaks naive segmentation), plus
  blue speckle and 0–3 thin coloured strike-through lines. The lines matter
  even though they are not black: drawn on top, they punch 1px white gaps
  through glyphs once the <110 threshold runs.

  The square count and side weights are fitted, not eyeballed. Glyphs alone
  contain essentially no solid k×k block for k≥5 (1.5 per image at k=5, 0.1 at
  k=6), so counting such blocks measures the noise directly, independent of the
  text. Matching real's (381, 156, 76, 32, 12, 3) blocks at k=4…9 and its 1903
  ink pixels needs ~30 squares biased towards the small end — the previous ~5.5
  left synthetic images at 1279 ink pixels, a third cleaner than the portal's.
* **Speckle is drawn under the squares.** Of 21948 blue pixels across 150 real
  captchas exactly one is fully surrounded by ink, so the portal lays speckle
  down before the blocks and lets them cover it. Drawing it last would leave
  blue confetti on top of every block, which real captchas never show.

``ALPHABET`` is the real character set: across 1902 labelled characters, none of
``0 9 G M O Q W g l m o q w`` ever appears — the portal excludes exactly the
glyphs that are ambiguous at this size. Generating them would teach the model
confusions the portal never actually poses.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# The 49 characters the portal actually uses (see module docstring).
ALPHABET = "12345678ABCDEFHIJKLNPRSTUVXYZabcdefhijknprstuvxyz"

FONT_SIZE = 38
IMG_HEIGHT = 45

# Coverage cut that reproduces the portal's stroke weight (see module docstring).
INK_CUT = 160

# Solid black squares per image, and the relative frequency of sides 3..9.
# Both fitted by matching solid-block counts against data/captcha/raw.
N_SQUARES = (22, 42)
SQUARE_SIDES = (3, 4, 5, 6, 7, 8, 9)
SQUARE_WEIGHTS = (3.0, 3.0, 2.5, 2.0, 1.5, 1.0, 0.8)

# No square starts on row 0. Glyphs put no ink above row 7 at all, so rows 0-6
# of a real captcha are pure noise, and their ink ramps 0.007, 0.018, 0.033 …
# — a square whose top edge could land on row 0 would start that ramp at
# 0.017. One row of clearance reproduces the measured profile.
SQUARE_Y_MIN = 1

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
    "/Library/Fonts/DejaVuSans.ttf",
)

_font = None
_font_missing = False


def font():
    """The portal's face, or None if DejaVu Sans is not installed."""
    global _font, _font_missing
    if _font is None and not _font_missing:
        for cand in _FONT_CANDIDATES:
            if Path(cand).exists():
                _font = ImageFont.truetype(cand, FONT_SIZE)
                break
        else:
            try:  # matplotlib vendors DejaVu Sans; use it rather than give up
                import matplotlib
                p = Path(matplotlib.__file__).parent / "mpl-data/fonts/ttf/DejaVuSans.ttf"
                if p.exists():
                    _font = ImageFont.truetype(str(p), FONT_SIZE)
            except Exception:  # noqa: BLE001
                pass
            if _font is None:
                _font_missing = True
    return _font


def available() -> bool:
    return font() is not None


def fingerprint() -> dict:
    """The tuned constants, recorded alongside anything generated from them.

    A stored corpus outlives the generator that made it; without this there is
    no way to tell whether shards on disk predate a fidelity fix.
    """
    return {
        "alphabet": ALPHABET,
        "font_size": FONT_SIZE,
        "img_height": IMG_HEIGHT,
        "ink_cut": INK_CUT,
        "n_squares": N_SQUARES,
        "square_sides": SQUARE_SIDES,
        "square_weights": SQUARE_WEIGHTS,
        "square_y_min": SQUARE_Y_MIN,
    }


def make(rng: random.Random, text: str | None = None) -> tuple[Image.Image, str]:
    """Render one synthetic captcha; returns (RGB image, its text)."""
    f = font()
    if f is None:
        raise RuntimeError("DejaVu Sans not available; cannot synthesise captchas")
    if text is None:
        text = "".join(rng.choice(ALPHABET) for _ in range(6))

    advances = [f.getlength(c) for c in text]
    # Per-image mean gap is tight (11.4–12.3 across real captchas) but individual
    # glyphs scatter around it; over-dispersing slightly is free robustness
    # against the horizontal jitter to_tensor's width-normalisation introduces.
    base_gap = rng.gauss(11.8, 1.0)
    x = rng.uniform(0.0, 4.0)
    xs = []
    for i, c in enumerate(text):
        xs.append(x)
        x += advances[i] + max(4.0, base_gap + rng.gauss(0.0, 2.2))
    width = int(round(xs[-1] + advances[-1] + rng.uniform(0.0, 3.0)))

    # Draw the glyphs into a greyscale layer and hard-threshold: the portal's
    # rasteriser is not anti-aliased, and soft edges would read as ink later.
    layer = Image.new("L", (width, IMG_HEIGHT), 255)
    ld = ImageDraw.Draw(layer)
    for xi, c in zip(xs, text):
        ld.text((xi, 0), c, font=f, fill=0)
    ink = np.asarray(layer) < INK_CUT

    img = Image.new("RGB", (width, IMG_HEIGHT), (255, 255, 255))
    arr = np.asarray(img).copy()
    arr[ink] = (0, 0, 0)
    img = Image.fromarray(arr)
    d = ImageDraw.Draw(img)

    # Laid down before the squares so they cover it, as the portal does.
    # Drawn count runs above the ~147 that survive in real captchas because the
    # squares then bury about a tenth of it.
    for _ in range(rng.randint(120, 210)):
        d.point((rng.randrange(width), rng.randrange(IMG_HEIGHT)), (0, 0, 255))

    # Solid squares land on top of the text — this is the noise that actually
    # hurts, because it is the same colour as ink and fuses with the glyphs.
    #
    # They arrive in small huddles rather than independently. At matched ink and
    # matched block counts, independent placement gives 318 horizontal ink runs
    # of mean length 5.98 where real captchas have 304 of mean 6.29: real ink is
    # more clumped than uniform scattering can account for. Huddles of up to
    # four within ±6px reproduce all three run statistics at once (303 / 6.15 /
    # p90 11.2 against 304 / 6.29 / 10.9). This is a mechanism that matches the
    # measured spatial correlation, not a claim about the portal's own code.
    n_squares = rng.randint(*N_SQUARES)
    placed = 0
    while placed < n_squares:
        cx = rng.randrange(width)
        cy = rng.randrange(SQUARE_Y_MIN, IMG_HEIGHT)
        for _ in range(min(rng.randint(1, 4), n_squares - placed)):
            s = rng.choices(SQUARE_SIDES, weights=SQUARE_WEIGHTS)[0]
            x0 = max(0, min(width - 1, cx + rng.randint(-6, 6)))
            y0 = max(SQUARE_Y_MIN, min(IMG_HEIGHT - 1, cy + rng.randint(-6, 6)))
            d.rectangle([x0, y0, x0 + s - 1, y0 + s - 1], fill=(0, 0, 0))
            placed += 1

    # Short segments, not corner-to-corner diagonals: the surviving line pixels
    # in real captchas have a median extent of 23px and a mean of 31, where
    # sampling two independent endpoints would average over 100.
    for _ in range(rng.randint(0, 3)):
        col = tuple(rng.randrange(256) for _ in range(3))
        if max(col) < 110:  # a near-black line would read as ink, not a gap
            col = (col[0], col[1], 200)
        x0, y0 = rng.randrange(width), rng.randrange(IMG_HEIGHT)
        length = min(210.0, 3.0 + rng.expovariate(1 / 30.0))
        ang = rng.uniform(0, 6.283185)
        d.line([x0, y0, x0 + length * math.cos(ang), y0 + length * math.sin(ang)],
               fill=col, width=1)
    return img, text
