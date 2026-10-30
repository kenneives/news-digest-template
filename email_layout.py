"""Grid email layout for the news digest.

Turns the model's sectioned HTML (<h2> sections of <ul><li> items, plus the
code-built sweep and Bot Activity sections) into a table-based, email-safe
page: three cards for "Three Things", two-column thumbnail rows for Top
Priority and the topic sections, paired columns for the short lists, image
tiles for Popular, cards per lane for the weekly sweep, a podcast card and a
colophon at the bottom. Columns are <table> cells (Outlook has no CSS grid),
stacked to one column on phones by a media query, and the palette follows the
OS through prefers-color-scheme with light as the fallback.

Generic: nothing in here names a person or a venture; section routing keys on
header words that come from the digest prompt. Content is never rewritten,
only re-laid-out; a section the router doesn't recognise is rendered as a
simple box with its original HTML.

Entry point: render_grid_email(...). Enabled from news_digest.py with
EMAIL_LAYOUT=grid.
"""

from __future__ import annotations

import html as H
import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

# ---------------------------------------------------------------- palette
LIGHT = {"bg": "#ffffff", "ink": "#1c1c1a", "muted": "#5c5c57", "faint": "#8a8a84", "rule": "#e3e2dd",
         "card": "#f4f3ef", "tile": "#e6e4de", "accent": "#0f6e56", "warm": "#b45309", "act": "#b91c1c",
         "notable": "#d97706", "info": "#9c9c96", "on_accent": "#ffffff"}
DARK = {"bg": "#141413", "ink": "#ecebe6", "muted": "#a9a8a1", "faint": "#7f7e78", "rule": "#2e2e2b",
        "card": "#1d1d1b", "tile": "#2b2b28", "accent": "#3fbf97", "warm": "#e8a04a", "act": "#f87171",
        "notable": "#fbbf24", "info": "#8a8a84", "on_accent": "#0b0b0a"}

FONT = "Inter,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


# ---------------------------------------------------------------- parsing
@dataclass
class Item:
    title: str = ""
    link: str = ""
    source: str = ""
    body: str = ""
    image: str = ""
    name: str = ""          # bolded lead-in (Job Radar style)
    raw_html: str = ""


@dataclass
class Section:
    title: str
    html: str
    items: list = field(default_factory=list)
    groups: list = field(default_factory=list)   # [(h3 title, [Item])] for the sweep


_SOURCE_RE = re.compile(r"^\s*[\(\[]\s*([^()\[\]]{2,60})\s*[\)\]]")


def _parse_item(li) -> Item:
    it = Item(raw_html=str(li))
    img = li.find("img")
    if img and img.get("src"):
        it.image = img["src"]
        img.decompose()
    a = li.find("a", href=True)
    strong = li.find("strong")
    lead = None
    if strong and (not a or strong.find("a") or (strong.sourceline or 0) <= (a.sourceline or 10**9)):
        lead = strong
    elif a:
        lead = a
    if lead is not None:
        it.title = lead.get_text(" ", strip=True)
        inner_a = lead if lead.name == "a" else lead.find("a", href=True)
        if inner_a:
            it.link = inner_a.get("href", "")
        else:
            it.name = it.title
            if a:
                it.link = a.get("href", "")
        # text right after the lead: "(Source)" then <br> then body
        after = ""
        node = lead.next_sibling
        pieces = []
        while node is not None:
            pieces.append(node.get_text(" ", strip=False) if hasattr(node, "get_text") else str(node))
            node = node.next_sibling
        after = "".join(pieces)
        m = _SOURCE_RE.match(after)
        if m:
            it.source = m.group(1).strip()
            after = after[m.end():]
        it.body = after
    else:
        it.body = li.get_text(" ", strip=True)
    if not it.link and a:
        it.link = a.get("href", "")
    it.body = re.sub(r"^\s*[-–—:·]\s*", "", it.body.replace("\xa0", " ")).strip()
    it.body = re.sub(r"\s+", " ", it.body)
    if not it.title:
        it.title = it.body[:120]
        it.body = ""
    return it


def parse_sections(digest_html: str) -> list[Section]:
    soup = BeautifulSoup(digest_html or "", "html.parser")
    out: list[Section] = []
    for h2 in soup.find_all("h2"):
        chunk = []
        node = h2.next_sibling
        while node is not None and getattr(node, "name", None) != "h2":
            chunk.append(str(node))
            node = node.next_sibling
        sec_html = "".join(chunk).strip()
        sec = Section(title=h2.get_text(" ", strip=True), html=sec_html)
        frag = BeautifulSoup(sec_html, "html.parser")
        h3s = frag.find_all("h3")
        if h3s:
            for h3 in h3s:
                ul = h3.find_next_sibling("ul")
                items = [_parse_item(li) for li in ul.find_all("li", recursive=False)] if ul else []
                sec.groups.append((h3.get_text(" ", strip=True), items))
                sec.items.extend(items)
        else:
            for ul in frag.find_all("ul"):
                sec.items.extend(_parse_item(li) for li in ul.find_all("li", recursive=False))
        out.append(sec)
    return out


# ---------------------------------------------------------------- routing
def kind_of(title: str) -> str:
    t = title.lower()
    checks = [
        ("three things", "three"), ("top priority", "top"), ("ventures", "box"), ("competitive radar", "box"),
        ("job radar", "list"), ("companies to watch", "list"), ("popular across", "tiles"),
        ("quick hits", "quick"), ("local", "quick"), ("family", "quick"), ("worth watching", "rows"),
        ("tonight", "box"), ("sky", "box"), ("web sweep", "sweep"), ("bot activity", "activity"),
        ("from the inbox", "box"),
    ]
    for key, kind in checks:
        if key in t:
            return kind
    return "rows"   # topic sections: AI & Robotics, Health, Automotive, ...


PAIRS = [("job radar", "companies to watch"), ("global news", "usa news"), ("local", "family")]


# ---------------------------------------------------------------- pieces
def _e(s: str) -> str:
    return H.escape(s or "", quote=True)


_COLOR_CLASS = {LIGHT["ink"]: "ink", LIGHT["accent"]: "accent", LIGHT["warm"]: "warm", LIGHT["muted"]: "muted"}


def _label(title: str, color: str, sub: str = "") -> str:
    cls = _COLOR_CLASS.get(color, "ink")
    s = (f'<div class="lbl {cls}" style="font-family:{FONT};font-size:15px;font-weight:700;color:{color};'
         f'margin:0 0 12px 0;line-height:1.3">{_e(title)}')
    if sub:
        s += f' <span class="muted" style="font-size:12px;font-weight:400;color:{LIGHT["muted"]}">{_e(sub)}</span>'
    return s + "</div>"


def _img(src: str, h: int, w: Optional[int] = None, source: str = "", radius: int = 6) -> str:
    """Picture, or the no-image state: a source monogram tile. Same box either way."""
    wcss = f"width:{w}px;" if w else "width:100%;"
    if src:
        return (f'<img src="{_e(src)}" alt="" class="pic" style="display:block;{wcss}height:{h}px;'
                f'object-fit:cover;border-radius:{radius}px;border:0;outline:none;text-decoration:none">')
    mono = "".join(p[0] for p in (source or "N").replace("r/", "").split()[:2] if p and p[0].isalnum()).upper() or "N"
    return (f'<div class="tile" style="{wcss}height:{h}px;border-radius:{radius}px;background:{LIGHT["card"]};'
            f'border:1px solid {LIGHT["rule"]};box-sizing:border-box;text-align:center;line-height:{h}px;'
            f'font-family:{FONT};font-size:{max(12, h // 4)}px;font-weight:700;letter-spacing:1px;color:{LIGHT["faint"]}">{_e(mono)}</div>')


def _title_link(it: Item, px: int) -> str:
    text = _e(it.title)
    style = f"font-family:{FONT};font-size:{px}px;font-weight:600;line-height:1.3;color:{LIGHT['ink']};text-decoration:none"
    return f'<a href="{_e(it.link)}" class="ink" style="{style}">{text}</a>' if it.link else f'<span class="ink" style="{style}">{text}</span>'


def _body(text: str, px: int) -> str:
    return (f'<div class="muted" style="font-family:{FONT};font-size:{px}px;color:{LIGHT["muted"]};line-height:1.45;margin-top:4px">{_e(text)}</div>'
            if text else "")


def _source(text: str) -> str:
    return (f'<div class="faint" style="font-family:{FONT};font-size:11px;color:{LIGHT["faint"]};margin-top:4px">{_e(text)}</div>'
            if text else "")


def _card(it: Item, img_h: int, num: Optional[int] = None) -> str:
    n = f'<div class="warm" style="font-family:{FONT};font-size:11px;font-weight:700;color:{LIGHT["warm"]};margin:8px 0 6px 0">{num}</div>' if num else '<div style="height:8px"></div>'
    return (_img(it.image, img_h, source=it.source or it.title) + n + _title_link(it, 15) + _body(it.body, 13) + _source(it.source))


def _row(it: Item, thumb_w: int, thumb_h: int, title_px: int, body_px: int) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
            f'<td width="{thumb_w}" valign="top" style="width:{thumb_w}px;padding:0 12px 0 0">{_img(it.image, thumb_h, thumb_w, it.source or it.title, 5)}</td>'
            f'<td valign="top">{_title_link(it, title_px)}{_body(it.body, body_px)}{_source(it.source)}</td></tr></table>')


def _quick(it: Item, thumb: int = 56, px: int = 13) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
            f'<td width="{thumb}" valign="middle" style="width:{thumb}px;padding:0 10px 0 0">{_img(it.image, thumb, thumb, it.source or it.title, 5)}</td>'
            f'<td valign="middle">{_title_link(it, px)}{_body(it.body, px - 1) if it.body and len(it.body) < 220 else ""}{_source(it.source)}</td></tr></table>')


def _tile(it: Item, h: int = 92) -> str:
    return _img(it.image, h, source=it.source or it.title) + f'<div style="height:8px"></div>' + _title_link(it, 12) + _source(it.source)


def _name_item(it: Item) -> str:
    """Bold-name list item. A parenthetical after the name here is a
    qualifier ("(ex-Volley)"), not a source, so it stays in the sentence."""
    name = _e(it.name or it.title)
    lead = f'<a href="{_e(it.link)}" class="ink" style="font-weight:600;color:{LIGHT["ink"]};text-decoration:none">{name}</a>' if it.link else f'<strong class="ink">{name}</strong>'
    body = (f"({it.source}) " if it.source else "") + it.body
    return (f'<div class="ink" style="font-family:{FONT};font-size:13px;line-height:1.45;color:{LIGHT["ink"]};margin:0 0 10px 0">'
            f'{lead} {_e(body)}</div>')


def _columns(cells: list[str], gap: int = 24, cls: str = "col") -> str:
    """Equal columns as table cells. class "col" stacks them on phones;
    class "col-half" keeps two across on phones (used for image tiles)."""
    n = max(1, len(cells))
    width = f"{100 / n:.3f}%"
    tds = []
    for i, c in enumerate(cells):
        pad = f"padding:0 {gap // 2}px 0 0" if i == 0 else (f"padding:0 0 0 {gap // 2}px" if i == n - 1 else f"padding:0 {gap // 2}px")
        tds.append(f'<td class="{cls}" valign="top" width="{width}" style="width:{width};{pad};box-sizing:border-box">{c}</td>')
    return f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>{"".join(tds)}</tr></table>'


def _stack(cells: list[str], gap: int = 16) -> str:
    return "".join(f'<div style="margin:0 0 {gap}px 0">{c}</div>' for c in cells)


def _grid(cells: list[str], per_row: int, gap: int, row_gap: int = 18, pad: bool = False, cls: str = "col") -> str:
    """Rows of equal cells. pad=True keeps a short last row at the same cell
    width (empty cells) instead of letting one tile stretch across."""
    rows = []
    for i in range(0, len(cells), per_row):
        row = cells[i:i + per_row]
        if pad and len(row) < per_row:
            row = row + [""] * (per_row - len(row))
        rows.append(_columns(row, gap, cls))
    return f'<div style="margin:0 0 {row_gap}px 0">' + f'</div><div style="margin:0 0 {row_gap}px 0">'.join(rows) + "</div>"


def _section(inner: str) -> str:
    return f'<div class="sec" style="padding:22px 0 6px 0">{inner}</div>'


def _box(inner_html: str) -> str:
    return (f'<div class="card ink" style="border:1px solid {LIGHT["rule"]};border-radius:8px;background:{LIGHT["card"]};'
            f'padding:14px 16px;font-family:{FONT};font-size:13px;line-height:1.5;color:{LIGHT["ink"]}">{inner_html}</div>')


def _sanitize_box_html(sec_html: str) -> str:
    """Original section HTML with light inline styling so it reads inside a box."""
    soup = BeautifulSoup(sec_html, "html.parser")
    for h3 in soup.find_all("h3"):
        h3["style"] = f"font-family:{FONT};font-size:13px;font-weight:700;margin:12px 0 6px 0"
        h3["class"] = "ink"
    for ul in soup.find_all("ul"):
        ul["style"] = "margin:0 0 8px 18px;padding:0"
    for li in soup.find_all("li"):
        li["style"] = "margin:0 0 8px 0"
    for a in soup.find_all("a"):
        a["style"] = f"color:{LIGHT['ink']}"
        a["class"] = "ink"
    for p in soup.find_all("p"):
        p["style"] = "margin:6px 0"
    return str(soup)


# ---------------------------------------------------------------- section renderers
def render_three(sec: Section) -> str:
    items = sec.items or []
    cells = [_card(it, 118, i + 1) for i, it in enumerate(items)]
    if len(cells) <= 3:
        body = _columns(cells, 16) if cells else ""
    else:
        body = _columns(cells[:3], 16) + '<div style="height:18px"></div>' + _columns(cells[3:], 16)
    return _section(_label(sec.title, LIGHT["warm"], "What happened, not what was said") + body)


def render_rows(sec: Section, thumb=(80, 62), title_px=13, body_px=12, label_color=None) -> str:
    cells = [_row(it, thumb[0], thumb[1], title_px, body_px) for it in sec.items]
    return _section(_label(sec.title, label_color or LIGHT["ink"]) + _grid(cells, 2, 24))


def render_top(sec: Section) -> str:
    return render_rows(sec, (92, 72), 14, 12, LIGHT["accent"])


def render_quick(sec: Section) -> str:
    cells = [_quick(it) for it in sec.items]
    return _label(sec.title, LIGHT["ink"]) + _stack(cells, 10)


def render_list(sec: Section) -> str:
    return _label(sec.title, LIGHT["accent"]) + "".join(_name_item(it) for it in sec.items)


def render_tiles(sec: Section) -> str:
    cells = [_tile(it) for it in sec.items]
    return _section(_label(sec.title, LIGHT["ink"], "The world's front page, every topic") + _grid(cells, 4, 14, pad=True, cls="col-half"))


def render_box(sec: Section, label_color=None) -> str:
    return _section(_label(sec.title, label_color or LIGHT["accent"]) + _box(_sanitize_box_html(sec.html)))


_SEV = [("🔴", "ACT NOW", "act"), ("🟡", "NOTABLE", "notable"), ("▫️", "INFO", "info")]


def render_sweep(sec: Section) -> str:
    groups = sec.groups or [("", sec.items)]
    cards = []
    for gname, items in groups:
        rows = []
        for it in items:
            sev_txt, sev_key = "INFO", "info"
            raw = BeautifulSoup(it.raw_html, "html.parser").get_text(" ", strip=True)
            for badge, txt, key in _SEV:
                if raw.startswith(badge):
                    sev_txt, sev_key = txt, key
                    break
            # subject (lane): summary — relevance <a>source</a>
            lane = ""
            if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", it.source or ""):
                lane, it.source = it.source, ""
            m = re.search(r"\(([a-z0-9\-]+)\)\s*:", it.body)
            if m:
                lane = lane or m.group(1)
                it.body = it.body[m.end():].strip()
            raw_soup = BeautifulSoup(it.raw_html, "html.parser")
            src_a = raw_soup.find_all("a", href=True)
            if src_a:
                it.source = src_a[-1].get_text(" ", strip=True) or it.source
                it.link = src_a[-1]["href"]
                it.body = it.body.replace(src_a[-1].get_text(" ", strip=True), "").strip()
            rows.append(
                f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:0 0 10px 0"><tr>'
                f'<td width="64" valign="top" style="width:64px;font-family:{FONT};font-size:10px;font-weight:700;letter-spacing:.6px;color:{LIGHT[sev_key]};padding-top:3px" class="{sev_key}">{sev_txt}</td>'
                f'<td valign="top">{_title_link(it, 13)}{_body(it.body, 12)}{_source((lane + " · " if lane else "") + it.source)}</td></tr></table>')
        head = f'<div class="ink" style="font-family:{FONT};font-size:12px;font-weight:700;margin:0 0 10px 0;color:{LIGHT["ink"]}">{_e(gname)}</div>' if gname else ""
        cards.append(_box(head + "".join(rows)))
    # footer line from the original section (<p><em>…</em></p>)
    foot = ""
    m = re.search(r"<p>\s*<em>(.*?)</em>\s*</p>", sec.html, re.S)
    if m:
        foot = f'<div class="faint" style="font-family:{FONT};font-size:11px;color:{LIGHT["faint"]};margin-top:4px">{m.group(1)}</div>'
    return _section(_label(sec.title, LIGHT["ink"], "Mondays · web-grounded · every item cites a source") + _grid(cards, 2, 12) + foot)


def render_activity(sec: Section) -> str:
    inner = _sanitize_box_html(sec.html)
    return _section(_label(sec.title, LIGHT["muted"]) +
                    f'<div class="card" style="border:1px solid {LIGHT["rule"]};border-radius:8px;padding:12px 14px;font-family:{FONT};font-size:12px;line-height:1.5;color:{LIGHT["muted"]}" class="muted">{inner}</div>')


# ---------------------------------------------------------------- page
def _masthead(title: str, date_line: str, podcast_url: Optional[str]) -> str:
    pill = ""
    if podcast_url:
        pill = (f'<a href="{_e(podcast_url)}" style="display:inline-block;padding:10px 14px;border-radius:999px;background:{LIGHT["accent"]};'
                f'color:{LIGHT["on_accent"]};font-family:{FONT};font-size:13px;font-weight:600;text-decoration:none" class="pill">▶&nbsp; Listen</a>')
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-bottom:2px solid {LIGHT["ink"]}" class="rule-ink"><tr>'
            f'<td valign="bottom" style="padding:0 0 16px 0"><div class="ink" style="font-family:{FONT};font-size:26px;font-weight:700;letter-spacing:-.3px;color:{LIGHT["ink"]}">{_e(title)}</div>'
            f'<div class="muted" style="font-family:{FONT};font-size:13px;color:{LIGHT["muted"]};margin-top:4px">{_e(date_line)}</div></td>'
            f'<td valign="bottom" align="right" style="padding:0 0 16px 0" class="col-pill">{pill}</td></tr></table>')


def _podcast_card(podcast_url: Optional[str], top_topics: Optional[list]) -> str:
    if not podcast_url:
        return ""
    topics = "".join(f'<li style="margin:0 0 6px 0">{_e(t)}</li>' for t in (top_topics or [])[:5])
    left = (f'<div class="ink" style="font-family:{FONT};font-size:16px;font-weight:700;color:{LIGHT["ink"]}">🎧 Daily News Podcast</div>'
            f'<div class="muted" style="font-family:{FONT};font-size:13px;color:{LIGHT["muted"]};line-height:1.45;margin:8px 0 12px 0">Listen to today\'s digest as a podcast with hosts Alex &amp; Sam.</div>'
            f'<a href="{_e(podcast_url)}" class="pill" style="display:inline-block;padding:10px 16px;border-radius:6px;background:{LIGHT["accent"]};color:{LIGHT["on_accent"]};font-family:{FONT};font-size:13px;font-weight:600;text-decoration:none">▶&nbsp; Listen now</a>'
            f'<div class="faint" style="font-family:{FONT};font-size:12px;color:{LIGHT["faint"]};margin-top:10px">Available anywhere. Log in with your Audiobookshelf account.</div>')
    right = (f'<div class="muted" style="font-family:{FONT};font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:{LIGHT["muted"]};margin:0 0 8px 0">Today\'s top topics</div>'
             f'<ol class="ink" style="margin:0;padding-left:18px;font-family:{FONT};font-size:13px;line-height:1.4;color:{LIGHT["ink"]}">{topics}</ol>') if topics else ""
    return _box(_columns([left, right] if right else [left], 24))


def render_grid_email(digest_html: str, *, title: str, date_line: str, podcast_url: Optional[str] = None,
                      top_topics: Optional[list] = None, colophon: str = "") -> str:
    """Full HTML document for the grid layout."""
    sections = parse_sections(digest_html)
    rendered: list[str] = []
    consumed: set = set()
    for i, sec in enumerate(sections):
        if i in consumed:
            continue
        kind = kind_of(sec.title)
        # pair the short lists / quick-hit sections side by side when both exist
        paired = None
        for a_key, b_key in PAIRS:
            if a_key in sec.title.lower():
                for j in range(i + 1, min(i + 4, len(sections))):
                    if j not in consumed and b_key in sections[j].title.lower():
                        paired = j
                        break
        if paired is not None and kind in ("list", "quick"):
            other = sections[paired]
            fn = render_list if kind == "list" else render_quick
            rendered.append(_section(_columns([fn(sec), fn(other)], 24)))
            consumed.add(paired)
            continue
        if kind == "three":
            rendered.append(render_three(sec))
        elif kind == "top":
            rendered.append(render_top(sec))
        elif kind == "list":
            rendered.append(_section(render_list(sec)))
        elif kind == "quick":
            rendered.append(_section(render_quick(sec)))
        elif kind == "tiles":
            rendered.append(render_tiles(sec))
        elif kind == "sweep":
            rendered.append(render_sweep(sec))
        elif kind == "activity":
            rendered.append(render_activity(sec))
        elif kind == "box":
            rendered.append(render_box(sec))
        else:
            rendered.append(render_rows(sec))

    body = "".join(rendered)
    L, D = LIGHT, DARK
    css = f"""
    body {{ margin:0; padding:0; background:{L['bg']}; -webkit-text-size-adjust:100%; }}
    .wrap {{ max-width:680px; margin:0 auto; padding:24px 32px 28px 32px; background:{L['bg']}; }}
    a {{ color:{L['ink']}; }}
    @media only screen and (max-width:600px) {{
      .wrap {{ padding:20px 16px 24px 16px !important; }}
      .col {{ display:block !important; width:100% !important; padding:0 0 16px 0 !important; box-sizing:border-box !important; }}
      .col-half {{ display:inline-block !important; width:48% !important; padding:0 0 14px 0 !important; box-sizing:border-box !important; vertical-align:top !important; }}
      .col-pill {{ display:block !important; text-align:left !important; padding:8px 0 16px 0 !important; }}
      .pic {{ width:100% !important; }}
      .lbl {{ font-size:15px !important; }}
    }}
    @media (prefers-color-scheme: dark) {{
      body, .wrap {{ background:{D['bg']} !important; }}
      .ink, .ink a, h2, h3, strong {{ color:{D['ink']} !important; }}
      .muted {{ color:{D['muted']} !important; }}
      .faint {{ color:{D['faint']} !important; }}
      .warm {{ color:{D['warm']} !important; }}
      .accent {{ color:{D['accent']} !important; }}
      .card {{ background:{D['card']} !important; border-color:{D['rule']} !important; }}
      .tile {{ background:{D['card']} !important; border-color:{D['rule']} !important; color:{D['faint']} !important; }}
      .rule-ink {{ border-color:{D['ink']} !important; }}
      .pill {{ background:{D['accent']} !important; color:{D['on_accent']} !important; }}
      .act {{ color:{D['act']} !important; }} .notable {{ color:{D['notable']} !important; }} .info {{ color:{D['info']} !important; }}
    }}
    """
    colophon_html = (f'<div class="faint" style="font-family:{FONT};font-size:11px;color:{L["faint"]};line-height:1.5;text-align:center;margin-top:18px">{_e(colophon)}</div>'
                     if colophon else "")
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
            f'<title>{_e(title)}</title><style>{css}</style></head>'
            f'<body class="wrap-body"><div class="wrap">{_masthead(title, date_line, podcast_url)}{body}'
            f'<div style="border-top:1px solid {L["rule"]};margin-top:16px;padding-top:20px" class="card-rule">{_podcast_card(podcast_url, top_topics)}{colophon_html}</div>'
            f'</div></body></html>')
