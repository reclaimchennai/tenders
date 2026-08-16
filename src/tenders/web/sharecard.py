"""Per-tender Open Graph share cards (1200x630 PNG), rendered with PIL.

The card is what a tender looks like when someone posts the link — for an
archive whose whole point is getting procurement in front of people, it is the
front page. It carries the same fact list the page's ``og:description`` does
(:func:`facts` is the single source for both), so the picture and the text a
platform falls back to can never drift apart.

Cards are rendered on demand and cached under ``data/og/``. Each PNG stores the
token it was rendered from, so a changed tender *or* a bumped
:data:`RENDER_VERSION` invalidates it without anyone having to clear the
directory. The same token cache-busts the URL, which matters because Cloudflare
fronts this site and will otherwise serve a redesigned card's old pixels for
days.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..shortnames import pretty_name
from .dates import fmt_date

SITE = "https://tenders.reclaimchennai.city"
# Bump whenever the drawing changes, so cached cards and CDN copies are refetched.
# 6: place and department are spelled as names, not as the stored keys.
RENDER_VERSION = 6

W, H = 1200, 630
PAD = 64
BG = (13, 15, 20)
ACCENT = (91, 140, 255)
GOLD = (224, 169, 46)
GOLD_LINE = (108, 82, 26)
# The awarded amount must never be mistaken for the estimate, so it gets its own
# colour rather than sharing the money chip's gold. Green reads as "settled".
AWARD = (110, 231, 183)
AWARD_LINE = (23, 106, 82)
TEXT = (242, 244, 247)
MUTED = (154, 163, 180)
FAINT = (107, 115, 130)
LINE = (58, 64, 79)
FLAG_FILL = (255, 173, 163)
FLAG_INK = (122, 26, 26)
FLAG_LINE = (214, 84, 84)
FLAG_TEXT = (248, 113, 113)

PIXEL_FONT = Path(__file__).parent / "static" / "PressStart2P-Regular.ttf"
_SANS = {
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
           "/System/Library/Fonts/Supplemental/Arial Bold.ttf"],
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf"],
}
# Values the portal writes when a field is simply not filled in.
_BLANK = {"", "na", "n/a", "nil", "none", "-", "0"}


# --------------------------------------------------------------------------
# facts — shared by the image and by og:description
# --------------------------------------------------------------------------

def _clean(value) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _BLANK else text


def _inr(n) -> str:
    n = float(n)
    if n >= 1e7:
        return f"₹{n / 1e7:.2f} Cr"
    if n >= 1e5:
        return f"₹{n / 1e5:.2f} L"
    return f"₹{n:,.0f}"


def _date(raw) -> str:
    """The site's ``13-August-2026`` spelling.

    Deliberately absolute where every other surface leads with "Closes in 5
    days": this card is rendered once and cached on disk under a token that has
    no clock in it (see og_token), so a relative phrase would be frozen at
    whatever it was the day someone first shared the link.
    """
    return fmt_date(_clean(raw)) or _clean(raw)[:10]


def _hours(hrs: float) -> str:
    rounded = round(float(hrs), 1)
    return f"{rounded:g}"


def facts(tender: dict, flag_hours: float | None = None,
          doc_count: int | None = None) -> list[dict]:
    """The card's fact chips, in drawing order.

    A tender with no disclosed value gets **no** value chip at all: the tender
    type ("Open Tender", "Limited") already stands in for it a chip earlier, and
    printing it twice — or printing "Value NA" — says nothing.

    An awarded tender gets a second, separately-toned money chip that is always
    labelled "Won". The estimate and the accepted bid are different facts and
    the distance between them is frequently the story, so the two are never
    merged and never rendered in the same colour — a reader who reads the award
    as the estimate has been misinformed by this card, which is worse than the
    card having said nothing at all.
    """
    out: list[dict] = []

    def add(text: str, tone: str = "plain") -> None:
        if text:
            out.append({"text": text, "tone": tone})

    awarded = bool(tender.get("awarded_at") or tender.get("award_stage"))
    add(_clean(tender.get("tender_category")))
    add(_clean(tender.get("tender_type")))
    if tender.get("tender_value_num"):
        add(("Est. " if awarded else "") + _inr(tender["tender_value_num"]), "money")
    elif _clean(tender.get("tender_value_raw")):
        add(("Est. ₹ " if awarded else "₹ ") + _clean(tender["tender_value_raw"]), "money")
    if awarded and tender.get("award_value_num"):
        add("Won " + _inr(tender["award_value_num"]), "award")
    if _clean(tender.get("awarded_to")):
        add(_clean(tender["awarded_to"]), "award")
    if awarded and not _clean(tender.get("awarded_to")):
        # Nothing to name, but the contract is still let, and saying so is the
        # single most useful thing this card can tell a reader.
        add("Awarded", "award")
    if _clean(tender.get("emd_raw")):
        add("EMD ₹" + _clean(tender["emd_raw"]), "money")
    closing = _date(tender.get("closing_date"))
    add(f"Closes {closing}" if closing else "")
    add(pretty_name(_clean(tender.get("location"))))
    if doc_count:
        add(f"{doc_count} document" + ("s" if doc_count != 1 else ""))
    if flag_hours:
        add(f"{_hours(flag_hours)} h bid window", "flag")
    return out


def og_token(tender: dict, flag_hours: float | None = None) -> str:
    """Cache key for one tender's card: its content plus the drawing version."""
    seed = "|".join([
        str(tender.get("last_updated_at") or ""),
        str(RENDER_VERSION), "1" if flag_hours else "0",
    ])
    return hashlib.blake2s(seed.encode(), digest_size=5).hexdigest()


def og_meta(tender: dict, flag_hours: float | None = None,
            doc_count: int | None = None) -> dict:
    """Everything base.html needs for the OG/Twitter block."""
    tender_id = str(tender.get("tender_id") or "")
    name = tender.get("short_name") or tender.get("title") or tender_id
    chips = facts(tender, flag_hours, doc_count)
    return {
        "title": ("RED FLAG · " if flag_hours else "") + name,
        "description": " · ".join(c["text"] for c in chips),
        "image": f"{SITE}/og/{tender_id}.png?v={og_token(tender, flag_hours)}",
        "alt": f"TN Tenders Mirror share card for tender {tender_id}: {name}",
    }


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------

def _pixel_font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(str(PIXEL_FONT), size)
    except OSError:
        return ImageFont.load_default()


def _sans(size: int, bold: bool = True):
    from PIL import ImageFont
    for path in _SANS[bold]:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _tracked_width(d, text: str, font, tracking: int) -> int:
    return sum(int(d.textlength(ch, font=font)) + tracking for ch in text) - tracking


def _tracked(d, xy, text: str, font, fill, tracking: int) -> int:
    """Draw with letter-spacing, which PIL has no notion of, and return the width.

    The pixel font only reads as a wordmark when it is spaced out."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += int(d.textlength(ch, font=font)) + tracking
    return x - tracking - xy[0]


def _ellipsize(d, text: str, font, max_w: int) -> str:
    if d.textlength(text, font=font) <= max_w:
        return text
    while text and d.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(d, text: str, font, max_w: int, max_lines: int) -> list[str]:
    words = (text or "").split()
    lines: list[str] = []
    cur, overflowed = "", False
    for word in words:
        trial = (cur + " " + word).strip()
        if not cur or d.textlength(trial, font=font) <= max_w:
            cur = trial
        elif len(lines) + 1 == max_lines:
            overflowed = True
            break
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur + " …" if overflowed else cur)
    return [_ellipsize(d, ln, font, max_w) for ln in lines]


def _warning_triangle(d, x: int, y: int, size: int, fill, ink) -> None:
    """A drawn hazard triangle. Deliberately not an emoji — the card must render
    identically everywhere, and emoji fall back to whatever the host has."""
    top = (x + size // 2, y)
    d.polygon([top, (x + size, y + size), (x, y + size)], fill=fill, outline=ink)
    bar_w = max(2, size // 9)
    cx = x + size // 2
    d.rectangle([cx - bar_w // 2, y + size // 3, cx + bar_w // 2, y + int(size * 0.66)], fill=ink)
    d.rectangle([cx - bar_w // 2, y + int(size * 0.74), cx + bar_w // 2,
                 y + int(size * 0.74) + bar_w], fill=ink)


def _flag_badge(d, right: int, y: int) -> None:
    font = _pixel_font(15)
    label, tracking, icon = "RED FLAG", 3, 26
    text_w = _tracked_width(d, label, font, tracking)
    pad_x, height = 18, 48
    width = pad_x * 2 + icon + 12 + text_w
    x0 = right - width
    d.rounded_rectangle([x0, y, right, y + height], radius=9, fill=FLAG_FILL)
    _warning_triangle(d, x0 + pad_x, y + (height - icon) // 2, icon, FLAG_FILL, FLAG_INK)
    _tracked(d, (x0 + pad_x + icon + 12, y + (height - 15) // 2 - 1),
             label, font, FLAG_INK, tracking)


def _award_badge(d, right: int, y: int) -> None:
    """Top-right AWARDED marker, drawn only when the red-flag badge is not.

    The two share the corner, and an awarded tender can also carry a short-bid-
    window flag. Overlapping them would produce an unreadable card, and the flag
    is the more urgent of the two, so it wins the slot (see render_og).
    """
    font = _pixel_font(15)
    label, tracking = "AWARDED", 3
    text_w = _tracked_width(d, label, font, tracking)
    pad_x, height = 20, 48
    x0 = right - (pad_x * 2 + text_w)
    d.rounded_rectangle([x0, y, right, y + height], radius=9,
                        outline=AWARD_LINE, width=2)
    _tracked(d, (x0 + pad_x, y + (height - 15) // 2 - 1), label, font, AWARD, tracking)


def _chips(d, chips: list[dict], x0: int, y0: int, max_w: int, max_rows: int) -> None:
    font = _sans(23, True)
    height, gap, pad_x, radius = 48, 12, 18, 10
    x, y, row = x0, y0, 1
    for chip in chips:
        text = chip["text"]
        width = int(d.textlength(text, font=font)) + pad_x * 2
        if x > x0 and x + width > x0 + max_w:
            if row >= max_rows:
                return
            row += 1
            x, y = x0, y + height + gap
        outline, ink = LINE, MUTED
        if chip["tone"] == "money":
            outline, ink = GOLD_LINE, GOLD
        elif chip["tone"] == "award":
            outline, ink = AWARD_LINE, AWARD
        elif chip["tone"] == "flag":
            outline, ink = FLAG_LINE, FLAG_TEXT
        d.rounded_rectangle([x, y, x + width, y + height], radius=radius, outline=outline, width=2)
        d.text((x + pad_x, y + height // 2), text, font=font, fill=ink, anchor="lm")
        x += width + gap


def render_og(tender: dict, out_path: Path, flag_hours: float | None = None,
              doc_count: int | None = None) -> Path:
    from PIL import Image, ImageDraw
    from PIL.PngImagePlugin import PngInfo

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Pixel-dashed rule across the top — the site's 8-bit accent, in one line.
    for x in range(0, W, 18):
        d.rectangle([x, 0, x + 10, 5], fill=ACCENT)

    _tracked(d, (PAD, 46), "TN TENDERS MIRROR", _pixel_font(20), ACCENT, 6)
    if flag_hours:
        _flag_badge(d, W - PAD, 34)
    elif tender.get("awarded_at") or tender.get("award_stage"):
        _award_badge(d, W - PAD, 34)

    name = tender.get("short_name") or tender.get("title") or tender.get("tender_id")
    title_font = _sans(52, True)
    y = 132
    for line in _wrap(d, str(name), title_font, W - 2 * PAD, 2):
        d.text((PAD, y), line, font=title_font, fill=TEXT)
        y += 66

    org = pretty_name((tender.get("organisation_chain") or "").split("||")[0].strip())
    if org:
        org_font = _sans(28, False)
        d.text((PAD, y + 4), _ellipsize(d, org, org_font, W - 2 * PAD), font=org_font, fill=MUTED)

    _tracked(d, (PAD, 372), "TENDER DETAILS", _pixel_font(13), FAINT, 4)
    _chips(d, facts(tender, flag_hours, doc_count), PAD, 404, W - 2 * PAD, 2)

    d.text((PAD, H - 96), str(tender.get("tender_id") or ""), font=_sans(24, False), fill=FAINT)
    _tracked(d, (PAD, H - 54), "tenders.reclaimchennai.city", _pixel_font(17), ACCENT, 3)

    meta = PngInfo()
    meta.add_text("og-token", og_token(tender, flag_hours))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", pnginfo=meta)
    return out_path


def og_path(cfg, tender_id: str) -> Path:
    safe = "".join(c for c in tender_id if c.isalnum() or c in "_-")
    return Path(cfg.docs_dir).parent / "og" / f"{safe}.png"


def is_fresh(out_path: Path, token: str) -> bool:
    """Whether a cached card was drawn from the current tender and design."""
    if not out_path.exists():
        return False
    try:
        from PIL import Image
        with Image.open(out_path) as im:
            return im.info.get("og-token") == token
    except Exception:  # noqa: BLE001 - a corrupt cache is just a stale cache
        return False
