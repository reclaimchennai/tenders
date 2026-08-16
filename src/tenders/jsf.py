"""Helpers for GePNIC's JSF-style stateful forms.

The portal round-trips a set of server-generated hidden fields (formids,
seedids, tokenSecret, component, page, ...). To submit a form we must echo those
back verbatim alongside our own inputs (e.g. the solved captcha).
"""

from __future__ import annotations

from bs4 import BeautifulSoup


def extract_form(html: str, form_id: str) -> dict | None:
    """Return {action, fields, captcha_src} for the named form, or None."""
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form", id=form_id)
    if form is None:
        return None
    fields: dict[str, str] = {}
    for inp in form.find_all(["input", "select", "textarea"]):
        name = inp.get("name")
        if not name:
            continue
        if inp.name == "select":
            opt = inp.find("option", selected=True) or inp.find("option")
            fields[name] = opt.get("value", "") if opt else ""
        else:
            fields[name] = inp.get("value", "") or ""

    captcha_src = None
    img = soup.find("img", id="captchaImage") or form.find("img", id="captchaImage")
    if img is not None:
        captcha_src = img.get("src")

    return {"action": form.get("action", "/nicgep/app"), "fields": fields,
            "captcha_src": captcha_src}
