# Third-party notices

The AGPL-3.0-or-later in `LICENSE` covers **the software in this repository**.
It does not, and cannot, relicense the third-party assets bundled alongside it.
Each of those keeps the licence it came with, listed here.

Read this before forking, and especially before redistributing a modified copy:
one entry below is **licensed artwork, not open source**, and it is the one most
likely to catch you out.

---

## Icons — Noun Project · licensed, NOT covered by the AGPL

`src/tenders/web/static/icons/`, plus the pixel icons inlined in
`src/tenders/web/templates/_icons.html` and `_logo.html`.

These were licensed from [The Noun Project](https://thenounproject.com) for use
on this site. **A Noun Project licence is granted to a licensee, not to a work**
— it does not travel to you because you cloned the repository, and nothing in
the AGPL can make it. Per-icon attribution is on the site's `/credits` page and
in the table below.

| Icon | Creator |
| --- | --- |
| Logo (pixel-art whistle) | nakals |
| Download | Aiwanz D |
| View | Color Combo |
| Moon (dark mode) | Linkdestypee |
| Sun (light mode) | nakals |
| Search | Filippo Lessio |

If you run your own mirror, either obtain your own Noun Project licence for
these, or replace them. Replacing them is easy and expected: they are single
`<svg>` blocks in `_icons.html`, and the ones drawn in-project (`clock`,
`alert`, `bell`, `flag`, `prev`, `next`, `close`, `calendar`, `tray`, the
`file_*` family) are plain 8×8 pixel grids written for this repository and are
covered by the AGPL like the rest of the source.

The social-network marks in `icons/social/` are the respective platforms'
trademarks, included only to label share links. Trademark law, not copyright,
governs those; do not restyle them into anything that implies endorsement.

## PDF.js — Apache-2.0

`src/tenders/web/static/pdf.min.js`, `pdf.worker.min.js` — from Mozilla's
[PDF.js](https://github.com/mozilla/pdf.js), Apache License 2.0. Vendored rather
than loaded from a CDN so the site has no third-party runtime dependency and
keeps working if the CDN does not.

## Press Start 2P — SIL Open Font License 1.1

`src/tenders/web/static/PressStart2P-Regular.{ttf,woff2}` — by CodeMan38,
[SIL OFL 1.1](https://scripts.sil.org/OFL). The OFL permits bundling and
redistribution; it forbids selling the font on its own and requires that any
modified version be renamed.

## DejaVu Sans — not bundled, but required

Not shipped in this repository. `src/tenders/captcha_synth.py` loads it from the
host (`fonts-dejavu-core` on Debian/Ubuntu, installed in the Docker image),
because the portal's captchas are rendered in it and reconstructing that is what
lets a mirror train its own solver. DejaVu is under its own permissive licence.

---

## What is *not* third-party

The archived tender documents and metadata are public records published by
Tamil Nadu government departments on `tntenders.gov.in`. They are not covered by
this repository's licence in either direction — this project asserts no
copyright over them, and none is claimed here on the departments' behalf. No
tender data is committed to this repository; `data/` is gitignored in full.
