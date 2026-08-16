"""Trained captcha solver: a CNN sequence model read with length-constrained CTC.

GePNIC document-download captchas are fixed-length (6 chars) in a consistent
font, but carry heavy, varied noise (blue speckle, solid black blocks,
strike-through lines) that defeats Tesseract. Real labels are scarce — the
captcha is session-wide, so a whole scrape cycle yields at most one — so the
bulk of training comes from ``captcha_synth``, which reproduces the portal's
generator closely enough to be indistinguishable after thresholding.

Pipeline:
  tenders-captcha-collect   -> save raw captcha PNGs (data/captcha/raw)
  tenders-captcha-label     -> web UI to type labels (data/captcha/labels.json)
  tenders-captcha-train     -> train + save model (data/captcha/model.pt)
At solve time, ``captcha.solve_image`` uses this model if a trained model exists,
falling back to Tesseract otherwise.

The nightly retrain is unattended, so ``train`` is deliberately conservative:
it does nothing unless enough *new* labels have arrived, it validates against a
split that is stable as the label set grows, and it re-scores the incumbent
model on that same split so a newly trained candidate can only replace it by
genuinely beating it. A bad night cannot regress the live solver.

Torch is imported lazily so the rest of the project runs without it.
"""

from __future__ import annotations

import json
import logging
import string
from pathlib import Path

from PIL import Image

log = logging.getLogger("captcha_model")

# Fixed model geometry.
NUM_CHARS = 6
CHARSET = string.digits + string.ascii_uppercase + string.ascii_lowercase  # 62
CHAR_TO_IDX = {c: i for i, c in enumerate(CHARSET)}
IMG_H, IMG_W = 50, 200

# CTC needs one extra output symbol for "no character here".
BLANK = len(CHARSET)

# Characters the portal actually issues. Across 1902 hand-labelled characters
# not one of 0 9 G M O Q W g l m o q w ever appeared — the generator omits
# exactly the glyphs that are ambiguous at this size. Decoding is restricted to
# this subset: allowing the missing 13 only ever invents confusions (8/g, 1/l,
# 0/O) that the portal never poses. The full 62-way CHARSET stays the model's
# output space so the index mapping and saved format are unchanged.
ALPHABET_IDX = tuple(sorted(
    CHAR_TO_IDX[c] for c in "12345678ABCDEFHIJKLNPRSTUVXYZabcdefhijknprstuvxyz"
))


def model_path(cfg) -> Path:
    return Path(cfg.captcha_dir) / "model.pt"


def model_stats_path(cfg) -> Path:
    return Path(cfg.captcha_dir) / "model_stats.json"


# The CNN needs at least this exact-string validation accuracy before we trust
# it over the fallback solvers. Below threshold it is skipped entirely.
MIN_USABLE_VAL_ACC = 0.50

# Nightly retrains are pointless until the label set has actually moved: a run
# costs ~25 min of CPU on a box shared with the live scraper, and 10 extra
# labels on top of several hundred cannot shift validation accuracy by more
# than measurement noise. Verified labels arrive at roughly one per scrape
# cycle, so this makes the retrain fire every week or two rather than nightly.
MIN_NEW_LABELS = 10


def labels_path(cfg) -> Path:
    return Path(cfg.captcha_dir) / "labels.json"


def auto_labels_path(cfg) -> Path:
    """Machine-generated labels, kept out of the hand-labelled ground truth.

    These are model+vision consensus reads of the unlabelled backlog: good
    enough to train on, not good enough to *validate* on. Keeping them in a
    separate file is what lets ``train`` report an honest accuracy.
    """
    return Path(cfg.captcha_dir) / "labels_auto.json"


def verified_path(cfg) -> Path:
    """Filenames whose label the *portal itself* accepted.

    The hand labels are not ground truth: most were pre-filled by Claude
    vision and waved through, and vision systematically mis-reads letter case
    — a pixel-height audit of the case-ambiguous glyphs (C/c S/s U/u V/v X/x
    Z/z, which differ only in size) found 8 of 41 checkable ones provably
    wrong. A server-accepted solve has no such doubt, so once enough of them
    have accumulated they take over as the validation set and the reported
    accuracy stops being capped by label noise.
    """
    return Path(cfg.captcha_dir) / "verified.json"


# Below this many server-verified labels, a verified-only validation set would
# be too small to distinguish two models; fall back to hashing all labels.
MIN_VERIFIED_FOR_VAL = 40


def raw_dir(cfg) -> Path:
    d = Path(cfg.captcha_dir) / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def to_tensor(img: Image.Image):
    """PIL image -> (1, IMG_H, IMG_W) float tensor in [0,1] (1 = ink).

    Applies the same noise-removal step as the labelling UI: composite on white,
    then keep only near-black pixels (r<110, g<110, b<110). This strips the blue
    speckle noise so the CNN sees the same clean binary text the human saw when
    generating the labels — without this, the model memorises noise patterns
    instead of character shapes and fails to generalise.
    """
    import numpy as np
    import torch

    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    comp = Image.alpha_composite(bg, img).convert("RGB")
    rgb = np.asarray(comp)
    # 255 everywhere except near-black pixels; training set size makes the
    # per-pixel Python loop this replaces the dominant cost of an epoch.
    mask = (rgb < 110).all(axis=2)
    clean = Image.fromarray(np.where(mask, 0, 255).astype("uint8"), mode="L")
    clean = clean.resize((IMG_W, IMG_H), Image.LANCZOS)
    arr = 1.0 - (np.asarray(clean, dtype="float32") / 255.0)  # invert: ink ~1
    return torch.from_numpy(arr).unsqueeze(0)


# Architectures the GPU search can produce, by name. ``legacy`` is the shape the
# first deployed model was trained in and is kept verbatim so that ``model.pt``
# from before the registry existed still loads — the promotion gate has to be
# able to re-score the incumbent, and a candidate that "wins" only because the
# incumbent failed to load would defeat the whole safeguard.
ARCHS = {
    "v1": dict(widths=(24, 48, 96),   head=2, hdim=256, dilations=(1, 2),       rnn=0, heads=0),
    "v2": dict(widths=(32, 64, 128),  head=3, hdim=320, dilations=(1, 2, 4),    rnn=0, heads=0),
    "v3": dict(widths=(32, 64, 128),  head=3, hdim=320, dilations=(1, 2, 4),    rnn=1, heads=0),
    "v4": dict(widths=(48, 96, 192),  head=3, hdim=384, dilations=(1, 2, 4, 8), rnn=0, heads=0),
    "v5": dict(widths=(32, 64, 128),  head=3, hdim=320, dilations=(1, 2, 4),    rnn=0, heads=8),
}


def build_arch(name: str):
    """One of ``ARCHS`` — same body as ``build_model`` with the sizes varied.

    The backbone still reduces 50x200 to 6x25 and the height is still folded
    into channels; what the registry varies is width, depth, how far the
    dilation stack sees, and whether a recurrent or attention layer reads the
    column sequence afterwards.
    """
    import torch.nn as nn

    cfg = ARCHS[name]
    w0, w1, w2 = cfg["widths"]
    hdim = cfg["hdim"]

    def bn_relu(c):
        return [nn.BatchNorm2d(c), nn.ReLU(inplace=True)]

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            layers = [nn.Conv2d(1, w0, 3, stride=2, padding=1), *bn_relu(w0),
                      nn.Conv2d(w0, w1, 3, padding=1), *bn_relu(w1),
                      nn.Conv2d(w1, w1, 3, padding=1), *bn_relu(w1),
                      nn.MaxPool2d(2),
                      nn.Conv2d(w1, w2, 3, padding=1), *bn_relu(w2),
                      nn.Conv2d(w2, w2, 3, padding=1), *bn_relu(w2)]
            if cfg["head"] >= 3:
                layers += [nn.Conv2d(w2, w2, 3, padding=1), *bn_relu(w2)]
            layers += [nn.MaxPool2d(2)]
            self.features = nn.Sequential(*layers)
            h = [nn.Conv1d(w2 * 6, hdim, 1), nn.BatchNorm1d(hdim),
                 nn.ReLU(inplace=True), nn.Dropout(0.2)]
            for d in cfg["dilations"]:
                h += [nn.Conv1d(hdim, hdim, 3, padding=d, dilation=d),
                      nn.BatchNorm1d(hdim), nn.ReLU(inplace=True), nn.Dropout(0.2)]
            self.head = nn.Sequential(*h)
            self.rnn = nn.GRU(hdim, hdim // 2, num_layers=cfg["rnn"], batch_first=True,
                              bidirectional=True) if cfg["rnn"] else None
            self.attn = nn.TransformerEncoder(
                nn.TransformerEncoderLayer(hdim, cfg["heads"], hdim * 2, 0.2,
                                           batch_first=True, norm_first=True),
                2) if cfg["heads"] else None
            self.out = nn.Linear(hdim, len(CHARSET) + 1)

        def forward(self, x):
            f = self.features(x)
            b, c, hh, w = f.shape
            z = self.head(f.reshape(b, c * hh, w)).permute(0, 2, 1)
            if self.rnn is not None:
                z = self.rnn(z)[0]
            if self.attn is not None:
                z = self.attn(z)
            return self.out(z)

    return Net()


def save_checkpoint(path, arch: str, state) -> None:
    """Weights plus the name of the architecture that produced them."""
    import torch

    torch.save({"arch": arch, "state_dict": state}, path)


def load_checkpoint(path):
    """Rebuild whichever architecture a checkpoint was saved from.

    Files written before the registry are a bare ``state_dict`` and are the
    legacy shape; newer ones carry their architecture name alongside.
    """
    import torch

    blob = torch.load(path, map_location="cpu", weights_only=True)
    if isinstance(blob, dict) and "state_dict" in blob and "arch" in blob:
        model = build_arch(str(blob["arch"]))
        model.load_state_dict(blob["state_dict"])
    else:
        model = build_model()
        model.load_state_dict(blob)
    model.eval()
    return model


def build_model():
    """Conv backbone -> 25-step column sequence -> per-step 63-way CTC logits.

    The previous head flattened the whole feature map into Linear(19200, 512),
    9.8M parameters that had to relearn "characters sit at fixed positions"
    from a few hundred examples; it never even fit the training set. This one
    keeps the spatial axis: the backbone reduces 50x200 to 6x25, the height is
    folded into channels, and a small dilated 1-D stack reads left-to-right
    over the 25 columns. Weight sharing across columns means one character's
    worth of evidence trains every position at once.

    CTC rather than six positional heads because nothing here is positionally
    fixed: image widths vary 179-219px before ``to_tensor`` normalises them to
    200, and per-glyph spacing scatters by ~5px, so a character can sit a
    sixth of a slot away from where a fixed head expects it. CTC learns the
    alignment instead of assuming one; ``decode`` then puts the known length
    back in.

    The first conv strides by 2 rather than pooling after: at 50x200 the second
    conv is the single most expensive layer in the network, and on the CPU-only
    box this runs on that stride roughly halves epoch time for no measured
    accuracy cost.
    """
    import torch.nn as nn

    class CaptchaNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(1, 24, 3, stride=2, padding=1),
                nn.BatchNorm2d(24), nn.ReLU(),                     # 25x100
                nn.Conv2d(24, 48, 3, padding=1), nn.BatchNorm2d(48), nn.ReLU(),
                nn.Conv2d(48, 48, 3, padding=1), nn.BatchNorm2d(48), nn.ReLU(),
                nn.MaxPool2d(2),                                   # 12x50
                nn.Conv2d(48, 96, 3, padding=1), nn.BatchNorm2d(96), nn.ReLU(),
                nn.Conv2d(96, 96, 3, padding=1), nn.BatchNorm2d(96), nn.ReLU(),
                nn.MaxPool2d(2),                                   # 6x25
            )
            self.head = nn.Sequential(
                nn.Conv1d(96 * 6, 256, 1), nn.BatchNorm1d(256), nn.ReLU(),
                nn.Dropout(0.2),
                nn.Conv1d(256, 256, 3, padding=1), nn.BatchNorm1d(256), nn.ReLU(),
                nn.Dropout(0.2),
                # Dilation widens the receptive field to ~9 columns (~72px, two
                # character pitches) so a glyph half-buried under a noise block
                # can still be placed from its neighbours.
                nn.Conv1d(256, 256, 3, padding=2, dilation=2),
                nn.BatchNorm1d(256), nn.ReLU(),
                nn.Conv1d(256, len(CHARSET) + 1, 1),
            )

        def forward(self, x):
            f = self.features(x)
            b, c, h, w = f.shape
            return self.head(f.reshape(b, c * h, w)).permute(0, 2, 1)  # B,T,63

    return CaptchaNet()


def _constrained_ctc(logprobs, n: int = NUM_CHARS, allowed=ALPHABET_IDX):
    """Best CTC path that collapses to exactly ``n`` characters.

    Greedy CTC decoding routinely emits 5 or 7 characters, which the portal
    rejects outright — but we *know* the answer is 6 long, so that knowledge
    belongs in the decoder. This is a Viterbi over states (characters emitted
    so far, symbol on the previous frame); the previous frame's symbol is all
    that is needed to know whether the current one starts a new character or
    repeats the last, which is the whole of the CTC collapse rule. 7x63 states
    over 25 frames, so it costs microseconds.
    """
    import numpy as np

    T, C = logprobs.shape
    V = C - 1
    mask = np.full(C, -1e30, dtype="float64")
    mask[list(allowed)] = 0.0
    mask[BLANK] = 0.0
    lp = logprobs.astype("float64") + mask

    NEG = -1e30
    dp = np.full((n + 1, C), NEG)
    dp[0, BLANK] = lp[0, BLANK]
    dp[1, :V] = lp[0, :V]
    back = np.zeros((T, n + 1, C, 2), dtype="int16")

    chars = np.arange(V)
    for t in range(1, T):
        nd = np.full((n + 1, C), NEG)
        for k in range(n + 1):
            row = dp[k]
            s_best = int(row.argmax())
            nd[k, BLANK] = row[s_best] + lp[t, BLANK]
            back[t, k, BLANK] = (k, s_best)

            stay = dp[k, :V]                       # same char continues
            if k > 0:
                prev = dp[k - 1]
                i1 = int(prev.argmax())
                m1 = prev[i1]
                tmp = prev.copy()
                tmp[i1] = NEG
                i2 = int(tmp.argmax())
                m2 = tmp[i2]
                # A new character may follow any previous symbol except itself.
                emit = np.where(chars == i1, m2, m1)
                emit_from = np.where(chars == i1, i2, i1)
            else:
                emit = np.full(V, NEG)
                emit_from = np.zeros(V, dtype="int64")
            take = emit > stay
            nd[k, :V] = np.where(take, emit, stay) + lp[t, :V]
            back[t, k, :V, 0] = np.where(take, k - 1, k)
            back[t, k, :V, 1] = np.where(take, emit_from, chars)
        dp = nd

    s = int(dp[n].argmax())
    if dp[n, s] <= NEG / 2:
        return ""
    k = n
    path = [s]
    for t in range(T - 1, 0, -1):
        k, s = int(back[t, k, s, 0]), int(back[t, k, s, 1])
        path.append(s)
    path.reverse()

    out, prev = [], -1
    for s in path:
        if s != prev and s != BLANK:
            out.append(CHARSET[s])
        prev = s
    return "".join(out)


def decode(logits) -> str:
    """Model output (1,T,63) or (T,63) -> the most likely 6-character string."""
    import torch

    if logits.dim() == 3:
        logits = logits[0]
    lp = torch.log_softmax(logits, dim=-1).cpu().numpy()
    return _constrained_ctc(lp)


class TrainedSolver:
    """Lazy-loaded singleton wrapper around the trained model.

    Only loads if ``model_stats.json`` records a ``best_val_acc`` above
    ``MIN_USABLE_VAL_ACC``. Below that threshold the model is not accurate
    enough to be useful and we fall back to Claude vision / Tesseract.

    The cache is keyed on the on-disk mtimes, not just "have we loaded once":
    the nightly retrain runs in a *different* process from the long-lived
    scraper, so an instance-only cache would pin whatever model existed when
    the scraper started and a freshly promoted model would never be picked up
    until someone restarted the service.
    """

    _instance = None
    _key = None

    def __init__(self, path: Path):
        self.model = load_checkpoint(path)

    @classmethod
    def get(cls, cfg):
        p = model_path(cfg)
        if not p.exists():
            return None
        sp = model_stats_path(cfg)
        try:
            key = (p.stat().st_mtime_ns,
                   sp.stat().st_mtime_ns if sp.exists() else 0)
        except OSError:
            return None
        if cls._instance is not None and cls._key == key:
            return cls._instance
        cls._instance, cls._key = None, key

        # Check saved validation accuracy before loading.
        if sp.exists():
            try:
                stats = json.loads(sp.read_text())
                acc = stats.get("best_val_acc", 0.0)
                if acc < MIN_USABLE_VAL_ACC:
                    log.debug("captcha model val_acc=%.2f < %.2f threshold; using fallback",
                              acc, MIN_USABLE_VAL_ACC)
                    return None
            except Exception:  # noqa: BLE001
                pass
        try:
            cls._instance = cls(p)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load captcha model: %s", exc)
            return None
        return cls._instance

    def predict(self, img: Image.Image) -> str:
        import torch

        with torch.no_grad():
            return decode(self.model(to_tensor(img).unsqueeze(0)))


# --------------------------------------------------------------------------
# training


def _is_val(name: str) -> bool:
    """Stable 1-in-8 validation split, by filename hash.

    Not a random shuffle: the label set grows every scrape cycle, and a
    reshuffled split would make each night's validation accuracy incomparable
    with the last — which is exactly the number the promotion decision turns
    on. Hashing pins every captcha to the same fold for life.
    """
    import hashlib

    return hashlib.sha1(name.encode()).digest()[0] % 8 == 0


def _load_items(cfg):
    """(train, val, n_labels) — machine-generated labels never reach val."""
    rd = raw_dir(cfg)

    def read(p):
        return json.loads(p.read_text()) if p.exists() else {}

    def usable(d):
        return [(k, v) for k, v in d.items()
                if isinstance(v, str) and len(v) == NUM_CHARS
                and all(c in CHAR_TO_IDX for c in v) and (rd / k).exists()]

    truth = read(labels_path(cfg))
    auto = read(auto_labels_path(cfg))
    verified = set(read(verified_path(cfg)) or ())
    truth_items = usable(truth)
    auto_items = [(k, v) for k, v in usable(auto) if k not in truth]

    ver_items = [it for it in truth_items if it[0] in verified]
    if len(ver_items) >= MIN_VERIFIED_FOR_VAL:
        val_names = {it[0] for it in ver_items}
        log.info("validating on %d server-verified labels", len(val_names))
    else:
        val_names = {it[0] for it in truth_items if _is_val(it[0])}
    val = [it for it in truth_items if it[0] in val_names]
    train = [it for it in truth_items if it[0] not in val_names] + auto_items
    return train, val, len(truth_items)


def _augment(x, rng):
    """Shift + occlude. Real samples are few, so they need to be stretched."""
    import torch

    dx, dy = rng.randint(-6, 6), rng.randint(-3, 3)
    if dx or dy:
        out = torch.zeros_like(x)
        _, H, W = x.shape
        out[:, max(0, dy):min(H, H + dy), max(0, dx):min(W, W + dx)] = \
            x[:, max(0, -dy):min(H, H - dy), max(0, -dx):min(W, W - dx)]
        x = out
    if rng.random() < 0.5:  # an extra noise block, on top of whatever is there
        _, H, W = x.shape
        s = rng.randint(4, 12)
        y0, x0 = rng.randrange(H), rng.randrange(W)
        x = x.clone()
        x[:, y0:y0 + s, x0:x0 + s] = 1.0
    return x


def _evaluate(model, rows, cache) -> tuple[float, float]:
    """(exact-string accuracy, per-character accuracy) over ``rows``."""
    import torch

    if not rows:
        return 0.0, 0.0
    exact = chars = 0
    model.eval()
    with torch.no_grad():
        for i in range(0, len(rows), 32):
            chunk = rows[i:i + 32]
            x = torch.stack([cache[n] for n, _ in chunk])
            out = model(x)
            for j, (_, text) in enumerate(chunk):
                pred = decode(out[j])
                exact += pred == text
                chars += sum(a == b for a, b in zip(pred.ljust(NUM_CHARS), text))
    return exact / len(rows), chars / (len(rows) * NUM_CHARS)


def train(cfg, *, epochs: int = 60, lr: float = 1e-3, synth_per_epoch: int = 3000,
          min_new_labels: int = MIN_NEW_LABELS, force: bool = False,
          threads: int = 4) -> dict:
    import copy
    import os
    import random as _r

    import torch
    from torch.utils.data import DataLoader, Dataset

    from . import captcha_synth

    torch.set_num_threads(threads)  # the live scraper shares this box
    force = force or os.environ.get("TENDERS_CAPTCHA_FORCE_TRAIN") == "1"

    train_rows, val_rows, n_truth = _load_items(cfg)
    if n_truth < 30:
        raise SystemExit(f"need >=30 labelled captchas, have {n_truth}")

    prev = {}
    if model_stats_path(cfg).exists():
        try:
            prev = json.loads(model_stats_path(cfg).read_text())
        except Exception:  # noqa: BLE001
            prev = {}
    new_labels = n_truth - int(prev.get("labels_trained_on", 0))
    if not force and model_path(cfg).exists() and new_labels < min_new_labels \
            and prev.get("usable"):
        log.info("only %d new verified labels since last train (need %d) — skipping",
                 new_labels, min_new_labels)
        return dict(prev, skipped=True, new_labels=new_labels)

    rawd = raw_dir(cfg)
    cache = {n: to_tensor(Image.open(rawd / n)) for n, _ in train_rows + val_rows}

    synth_ok = captcha_synth.available()
    if not synth_ok:
        log.warning("DejaVu Sans not found — training on %d real captchas only, "
                    "which historically is not enough to clear the usable threshold",
                    len(train_rows))

    class RealDS(Dataset):
        def __init__(self, rows, reps=1):
            self.rows = rows * reps

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, i):
            name, text = self.rows[i]
            rng = _r.Random()
            x = _augment(cache[name], rng)
            return x, torch.tensor([CHAR_TO_IDX[c] for c in text])

    class SynthDS(Dataset):
        def __init__(self, n, epoch):
            self.n, self.epoch = n, epoch

        def __len__(self):
            return self.n

        def __getitem__(self, i):
            rng = _r.Random((self.epoch + 1) * 1000003 + i)
            img, text = captcha_synth.make(rng)
            x = _augment(to_tensor(img), rng)
            return x, torch.tensor([CHAR_TO_IDX[c] for c in text])

    model = build_model()
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs, eta_min=lr / 50)
    ctc = torch.nn.CTCLoss(blank=BLANK, zero_infinity=True)

    # Synthetic data alone teaches the glyphs; the real captchas only have to
    # close whatever gap is left between the reconstruction and the portal, so
    # they are held back until the model can already read.
    pretrain_epochs = epochs // 2 if synth_ok else 0
    best_acc, best_char, best_state = -1.0, 0.0, None

    for ep in range(1, epochs + 1):
        parts = []
        if synth_ok:
            n = synth_per_epoch if ep <= pretrain_epochs else synth_per_epoch // 2
            parts.append(SynthDS(n, ep))
        if ep > pretrain_epochs and train_rows:
            parts.append(RealDS(train_rows, reps=max(1, 900 // max(1, len(train_rows)))))
        if not parts:  # no synthesis and every label held out for validation
            raise SystemExit("nothing to train on")
        ds = torch.utils.data.ConcatDataset(parts) if len(parts) > 1 else parts[0]
        dl = DataLoader(ds, batch_size=48, shuffle=True, drop_last=False)

        model.train()
        total = 0.0
        for x, y in dl:
            opt.zero_grad()
            out = model(x)                                  # B,T,63
            lp = torch.log_softmax(out, dim=-1).permute(1, 0, 2)
            b, T = out.size(0), out.size(1)
            loss = ctc(lp, y,
                       torch.full((b,), T, dtype=torch.long),
                       torch.full((b,), NUM_CHARS, dtype=torch.long))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            total += loss.item()
        sched.step()

        acc, char_acc = _evaluate(model, val_rows, cache)
        if acc > best_acc:
            best_acc, best_char = acc, char_acc
            best_state = copy.deepcopy(model.state_dict())
        if ep % 5 == 0 or ep == epochs:
            log.info("epoch %d/%d  loss=%.3f  val_exact=%.3f  val_char=%.3f  (best %.3f)",
                     ep, epochs, total / max(1, len(dl)), acc, char_acc, best_acc)

    # Score the deployed model on the *same* split before replacing it. Comparing
    # against the accuracy recorded last time would be comparing across different
    # validation sets; re-running the incumbent here makes the promotion decision
    # a like-for-like test, which is what makes the nightly loop monotonic.
    incumbent, incumbent_char = -1.0, 0.0
    if model_path(cfg).exists():
        try:
            incumbent, incumbent_char = _evaluate(load_checkpoint(model_path(cfg)),
                                                  val_rows, cache)
        except Exception as exc:  # noqa: BLE001
            log.info("incumbent model not comparable (%s) — candidate wins by default", exc)

    promoted = best_state is not None and best_acc > incumbent
    if promoted:
        torch.save(best_state, model_path(cfg))
        log.info("promoted new model: val_exact %.3f > incumbent %.3f", best_acc, incumbent)
    else:
        log.info("keeping incumbent model: candidate %.3f did not beat %.3f",
                 best_acc, incumbent)

    deployed = best_acc if promoted else incumbent
    result = {"train": len(train_rows), "val": len(val_rows),
              "best_val_acc": round(deployed, 3),
              "val_char_acc": round(best_char if promoted else incumbent_char, 3),
              "candidate_val_acc": round(best_acc, 3),
              "incumbent_val_acc": round(incumbent, 3),
              "promoted": promoted,
              "labels_trained_on": n_truth,
              "synthetic": synth_ok,
              "model": str(model_path(cfg)),
              "usable": deployed >= MIN_USABLE_VAL_ACC}
    model_stats_path(cfg).write_text(json.dumps(result, indent=2))
    if deployed < MIN_USABLE_VAL_ACC:
        log.info("val_acc=%.2f below usable threshold (%.2f) — "
                 "Claude vision will be used for live captchas; retrain when you have more labels",
                 deployed, MIN_USABLE_VAL_ACC)
    # Reset singleton so next solve_image call re-evaluates the threshold.
    TrainedSolver._instance = None
    TrainedSolver._key = None
    return result
