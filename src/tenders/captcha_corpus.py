"""Build and read a stored synthetic captcha corpus.

``captcha_synth.make`` is cheap (~1ms) but ``to_tensor`` is not (~7ms), so a
training run that synthesises on the fly spends most of its time in PIL rather
than in the model. Pre-rendering once and reading packed arrays turns that into
a memory-bandwidth problem instead, which matters most on a rented GPU where
every idle second is billed.

Storage format — a directory of compressed ``.npz`` shards plus a manifest:

    data/captcha/synth/
        manifest.json           split sizes, seeds, generator fingerprint
        train-0000.npz .. N     x: uint8 (n, 50, 200), y: uint8 (n, 6)
        val-0000.npz   .. N
        test-0000.npz  .. N

``x`` holds the *post*-``to_tensor`` image as bytes (0 = ink, 255 = paper), which
is exactly what the model consumes after ``1 - x/255``; storing it this way
keeps the corpus a fifth the size of float32 and lossless against the live
preprocessing path. ``y`` holds ``CHAR_TO_IDX`` indices.

Splits are generated from disjoint seed ranges rather than by shuffling one
pool, so ``train``/``val``/``test`` stay reproducible and cannot leak into each
other no matter how many times the corpus is rebuilt or extended. Two samples
in different splits can still happen to draw the same six characters — with
49**6 possible strings that is a handful of collisions in half a million draws
— but they are different images, which is what the split is protecting.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("captcha_corpus")

SHARD = 10_000

# Disjoint seed bases, so a sample can never migrate between splits.
SEED_BASE = {"train": 1_000_000_000, "val": 2_000_000_000, "test": 3_000_000_000}


def corpus_dir(cfg) -> Path:
    return Path(cfg.captcha_dir) / "synth"


def _render(args):
    """One shard, in a worker process."""
    import random

    import numpy as np
    from PIL import Image

    from . import captcha_synth
    from .captcha_model import CHAR_TO_IDX, IMG_H, IMG_W, NUM_CHARS, to_tensor

    split, base, start, n, out = args
    x = np.empty((n, IMG_H, IMG_W), dtype=np.uint8)
    y = np.empty((n, NUM_CHARS), dtype=np.uint8)
    for i in range(n):
        rng = random.Random(base + start + i)
        img, text = captcha_synth.make(rng)
        # to_tensor is the single source of truth for preprocessing: going
        # through it means a stored sample and a live portal capture reach the
        # model by the exact same path.
        t = to_tensor(img)
        x[i] = np.asarray((1.0 - t[0].numpy()) * 255.0 + 0.5, dtype=np.uint8)
        y[i] = [CHAR_TO_IDX[c] for c in text]
    np.savez_compressed(out, x=x, y=y)
    return out.name, n


def build(cfg, *, train: int = 400_000, val: int = 25_000, test: int = 25_000,
          workers: int | None = None, nice: int = 10) -> dict:
    """Generate the corpus. Returns the manifest."""
    import multiprocessing as mp
    import time

    from . import captcha_synth

    if not captcha_synth.available():
        raise SystemExit("DejaVu Sans not available; cannot build corpus")

    d = corpus_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    workers = workers or max(1, (os.cpu_count() or 2) - 1)

    jobs = []
    counts = {"train": train, "val": val, "test": test}
    for split, total in counts.items():
        for si, start in enumerate(range(0, total, SHARD)):
            n = min(SHARD, total - start)
            jobs.append((split, SEED_BASE[split], start, n,
                         d / f"{split}-{si:04d}.npz"))

    t0 = time.time()
    # Workers inherit the niceness; the live scraper shares this box and must
    # keep meeting its politeness delays.
    os.nice(nice)
    with mp.get_context("fork").Pool(workers) as pool:
        for i, (name, n) in enumerate(pool.imap_unordered(_render, jobs), 1):
            if i % 5 == 0 or i == len(jobs):
                log.info("shard %d/%d (%s) %.0fs elapsed", i, len(jobs), name,
                         time.time() - t0)
    elapsed = time.time() - t0

    man = {
        "counts": counts,
        "shard": SHARD,
        "seed_base": SEED_BASE,
        "generator": captcha_synth.fingerprint(),
        "seconds": round(elapsed, 1),
        "workers": workers,
        "format": "npz shards; x uint8 (n,50,200) 0=ink 255=paper, y uint8 (n,6) CHAR_TO_IDX",
    }
    (d / "manifest.json").write_text(json.dumps(man, indent=2))
    log.info("built %d samples in %.1f min", sum(counts.values()), elapsed / 60)
    return man


def load(cfg, split: str, limit: int | None = None):
    """(x uint8 (n,50,200), y uint8 (n,6)) for one split, concatenated."""
    import numpy as np

    d = corpus_dir(cfg)
    xs, ys, got = [], [], 0
    for p in sorted(d.glob(f"{split}-*.npz")):
        with np.load(p) as z:
            xs.append(z["x"])
            ys.append(z["y"])
        got += len(xs[-1])
        if limit and got >= limit:
            break
    if not xs:
        raise SystemExit(f"no {split} shards in {d} — run tenders-captcha-corpus")
    x, y = np.concatenate(xs), np.concatenate(ys)
    return (x[:limit], y[:limit]) if limit else (x, y)
