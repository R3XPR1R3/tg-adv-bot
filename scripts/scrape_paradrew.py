"""Scrape the Heroes of Might and Magic: Olden Era reference at paradrew.com.

The site is plain HTML (no JS-rendered content, no Cloudflare challenge),
so we use ``requests`` + ``html.parser``. Six sections: units, heroes,
skills, spells, laws, guides. Output is a single JSON file.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

BASE = "https://paradrew.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR / "data"
DEFAULT_CACHE = SCRIPT_DIR / "cache_pd"

log = logging.getLogger("pd")


# -------------------- HTTP --------------------

def fetch(url: str, *, cache_dir: Path | None = None, delay: float = 0.25) -> str:
    """GET ``url``; cache on disk if ``cache_dir`` set."""
    cache_path = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # slugify URL into a file name
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", url)[-180:]
        cache_path = cache_dir / f"{safe}.html"
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8")
    time.sleep(delay)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", errors="replace")
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            wait = 2 ** attempt
            log.warning("fetch %s failed (%s), retry in %ss", url, exc, wait)
            time.sleep(wait)
    else:
        raise RuntimeError(f"fetch failed: {url}")
    if cache_path is not None:
        cache_path.write_text(body, encoding="utf-8")
    return body


# -------------------- HTML helpers --------------------

# Strip HTML tags and decode entities — paradrew uses plain markup, no nested
# tags inside values we care about, so this is enough.
def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def find_all(pattern: str, html: str, flags: int = re.S) -> list:
    return re.findall(pattern, html, flags)


def extract_index_links(html: str, prefix: str) -> list[str]:
    """Find detail page paths under ``prefix`` (e.g. /ru/olden-era/units/)."""
    pat = re.escape(prefix.rstrip("/") + "/") + r'[^"#?]+'
    links = sorted(set(re.findall(rf'href="({pat})"', html)))
    return [l for l in links if l.rstrip("/") != prefix.rstrip("/")]


# -------------------- Lossless section tree --------------------

def _article_root(html: str) -> str:
    """Return the inner HTML of the main content container, or the whole body."""
    for sel in (r'<main\b[^>]*>(.*?)</main>',
                r'<article\b[^>]*>(.*?)</article>',
                r'<body\b[^>]*>(.*?)</body>'):
        m = re.search(sel, html, re.S | re.I)
        if m:
            return m.group(1)
    return html


def _scrub(html: str) -> str:
    """Drop scripts/styles/breadcrumbs before tree-parsing.

    Do NOT strip ``<header>`` — paradrew wraps real content in <header> for
    each section, and the non-greedy regex tends to match nested closings on
    that tag and swallow whole pages.
    """
    for sel in (r'<nav\b[^>]*>.*?</nav>',
                r'<script\b[^>]*>.*?</script>',
                r'<style\b[^>]*>.*?</style>',
                r'<noscript\b[^>]*>.*?</noscript>'):
        html = re.sub(sel, '', html, flags=re.S | re.I)
    return html


def _capture_lists(block: str) -> list[list[str]]:
    out: list[list[str]] = []
    for ul in re.finditer(r'<(?:ul|ol)\b[^>]*>(.*?)</(?:ul|ol)>', block, re.S | re.I):
        items = [strip_tags(li) for li in
                 re.findall(r'<li\b[^>]*>(.*?)</li>', ul.group(1), re.S | re.I)]
        items = [i for i in items if i]
        if items:
            out.append(items)
    return out


def _capture_dl(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(
        r'<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>', block, re.S | re.I):
        k = strip_tags(m.group(1))
        v = strip_tags(m.group(2))
        if k:
            out[k] = v
    return out


def _capture_tables(block: str) -> list[dict[str, Any]]:
    tables = []
    for t in re.finditer(r'<table\b[^>]*>(.*?)</table>', block, re.S | re.I):
        body = t.group(1)
        rows = []
        for tr in re.finditer(r'<tr\b[^>]*>(.*?)</tr>', body, re.S | re.I):
            cells = re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', tr.group(1), re.S | re.I)
            rows.append([strip_tags(c) for c in cells])
        if rows:
            tables.append({"rows": rows})
    return tables


def _capture_images(block: str) -> list[dict[str, str]]:
    out = []
    seen_alts: set[str] = set()
    for m in re.finditer(r'<img\b[^>]*>', block, re.I):
        tag = m.group(0)
        src = re.search(r'\bsrc="([^"]+)"', tag)
        alt = re.search(r'\balt="([^"]+)"', tag)
        if not (src or alt):
            continue
        a = alt.group(1) if alt else ""
        s = src.group(1) if src else ""
        key = f"{a}|{s}"
        if key in seen_alts:
            continue
        seen_alts.add(key)
        out.append({"src": s, "alt": a})
    return out


def _capture_tags(block: str) -> list[str]:
    classes = (r'\btag\b', r'\bsp__tag\b', r'\bcard__faction\b', r'\bcard__tier\b')
    found: list[str] = []
    seen: set[str] = set()
    for cls in classes:
        for m in re.finditer(
            rf'<(?:span|div)\b[^>]*class="[^"]*{cls}[^"]*"[^>]*>(.*?)</(?:span|div)>',
            block, re.S | re.I):
            t = strip_tags(m.group(1))
            if t and t not in seen:
                seen.add(t)
                found.append(t)
    return found


_HEADING_RE = re.compile(r'<(h[1-6])\b[^>]*>(.*?)</\1>', re.S | re.I)


def parse_section_tree(html: str) -> list[dict[str, Any]]:
    """Walk all headings and bucket the content between them.

    Returns a flat list of {level, title, text, lists, stats, tables, images,
    tags}. The hierarchy is preserved via ``level`` (1–6) — easier to traverse
    than nested children when the source markup isn't strictly nested.
    """
    body = _scrub(_article_root(html))
    headings = list(_HEADING_RE.finditer(body))
    sections: list[dict[str, Any]] = []
    # capture content before the first heading too (intro)
    if headings and headings[0].start() > 0:
        intro = body[:headings[0].start()]
        if strip_tags(intro):
            sections.append(_section_from_block(0, "_intro", intro))
    for i, m in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        block = body[m.end():end]
        title = strip_tags(m.group(2))
        if not title:
            continue
        level = int(m.group(1)[1])
        sec = _section_from_block(level, title, block)
        sections.append(sec)
    return sections


def _section_from_block(level: int, title: str, block: str) -> dict[str, Any]:
    text = strip_tags(block)
    sec: dict[str, Any] = {"level": level, "title": title}
    if text:
        sec["text"] = text
    lists = _capture_lists(block)
    if lists:
        sec["lists"] = lists
    stats = _capture_dl(block)
    if stats:
        sec["stats"] = stats
    tables = _capture_tables(block)
    if tables:
        sec["tables"] = tables
    images = _capture_images(block)
    if images:
        sec["images"] = images
    tags = _capture_tags(block)
    if tags:
        sec["tags"] = tags
    return sec


def _common_meta(html: str, url: str) -> dict[str, Any]:
    """Page-level fields (name, faction, tier) plus safety-net full text."""
    out: dict[str, Any] = {"source_url": url}
    h1 = re.search(r'<h1[^>]*>(.+?)</h1>', html, re.S)
    if h1:
        out["name"] = strip_tags(h1.group(1))
    f = re.search(r'data-faction="([^"]+)"', html)
    if f:
        out["faction_key"] = f.group(1)
    t = re.search(r'Тир\s*(\d+)', html)
    if t:
        out["tier"] = int(t.group(1))
    body = _scrub(_article_root(html))
    out["_full_text"] = strip_tags(body)
    out["_images"] = _capture_images(body)
    return out


# -------------------- Section parsers --------------------

def parse_variant_block(block: str) -> dict[str, Any]:
    """Parse a single <article class="variant" ...> block (unit form)."""
    out: dict[str, Any] = {}
    m = re.search(r'id="([^"]+)"', block)
    if m: out["id"] = m.group(1)
    for label, regex in (
        ("kind",   r'class="variant__kind"[^>]*>([^<]+)<'),
        ("name",   r'<h2[^>]*>([^<]+)</h2>'),
        ("class",  r'class="variant__class"[^>]*>([^<]+)<'),
        ("cost",   r'class="variant__cost"[^>]*>([^<]+)<'),
    ):
        m = re.search(regex, block)
        if m: out[label] = strip_tags(m.group(1))
    # stats
    stats: dict[str, str] = {}
    for s in re.finditer(r'<dt[^>]*>([^<]+)</dt>\s*<dd[^>]*>([^<]+)</dd>', block):
        stats[strip_tags(s.group(1))] = strip_tags(s.group(2))
    if stats: out["stats"] = stats
    # narrative / lore
    nar = re.search(r'class="variant__narrative"[^>]*>(.+?)</p>', block, re.S)
    if nar: out["lore"] = strip_tags(nar.group(1))
    # abilities (passive)
    abilities = []
    for ab in re.finditer(
        r'<li[^>]*>\s*<span class="ability__name"[^>]*>([^<]+)</span>'
        r'\s*<span class="ability__desc"[^>]*>(.+?)</span>', block, re.S):
        abilities.append({"name": strip_tags(ab.group(1)),
                          "desc": strip_tags(ab.group(2))})
    if abilities: out["abilities"] = abilities
    # immunities / tags
    tags = re.findall(r'class="tag"[^>]*>([^<]+)<', block)
    tags = [strip_tags(t) for t in tags]
    if tags: out["tags"] = tags
    return out


def parse_unit(html: str, url: str) -> dict[str, Any]:
    out = _common_meta(html, url)
    # split into variant blocks (the structural backbone of unit pages)
    parts = list(re.finditer(r'<[^>]+class="variant"[^>]*>', html))
    variants = []
    for i, m in enumerate(parts):
        end = parts[i + 1].start() if i + 1 < len(parts) else len(html)
        variants.append(parse_variant_block(html[m.start():end]))
    if variants:
        out["variants"] = variants
    out["sections"] = parse_section_tree(html)
    return out


def parse_hero(html: str, url: str) -> dict[str, Any]:
    out = _common_meta(html, url)
    # faction block contains: <dot/> faction-text <class-span> · class-text </class-span>
    fac_block = re.search(r'class="hp__faction"[^>]*>(.+?)</div>', html, re.S)
    if fac_block:
        block = fac_block.group(1)
        cls_m = re.search(r'class="hp__class"[^>]*>([^<]+)</', block)
        if cls_m:
            out["class"] = strip_tags(cls_m.group(1)).lstrip("· ").strip()
            # strip the class span before extracting faction
            block_no_cls = re.sub(r'<span\b[^>]*class="hp__class"[^>]*>.*?</span>',
                                    '', block, flags=re.S)
        else:
            block_no_cls = block
        faction = strip_tags(block_no_cls)
        if faction:
            out["faction"] = faction
    for label, regex in (
        ("motto", r'class="hp__motto"[^>]*>([^<]+)<'),
        ("specialty_name", r'class="hp__spec-name"[^>]*>([^<]+)<'),
    ):
        m = re.search(regex, html)
        if m:
            out[label] = strip_tags(m.group(1))
    # starting stats — capture all dt/dd before the first h2 (header card)
    head = re.split(r'<h2', html, maxsplit=1)[0]
    stats = _capture_dl(head)
    if stats:
        out["starting_stats"] = stats
    # skill probability cards (matrix)
    skill_chances: list[dict[str, Any]] = []
    for c in re.finditer(
        r'<div\b[^>]*class="skill-card[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html, re.S):
        block = c.group(1)
        nm = re.search(r'class="skill-card__name"[^>]*>([^<]+)<', block)
        pct = re.search(r'class="skill-card__pct"[^>]*>([^<]+)<', block)
        if nm:
            skill_chances.append({
                "skill": strip_tags(nm.group(1)),
                "chance": strip_tags(pct.group(1)) if pct else None,
            })
    if skill_chances:
        out["available_skills"] = skill_chances
    out["sections"] = parse_section_tree(html)
    return out


def parse_skill(html: str, url: str) -> dict[str, Any]:
    out = _common_meta(html, url)
    intro = re.search(r'<h1[^>]*>[^<]+</h1>(.*?)<h2', html, re.S)
    if intro:
        t = strip_tags(intro.group(1))
        if t:
            out["description"] = t
    sections = parse_section_tree(html)
    out["sections"] = sections
    # match by stem — Russian adjectives change gender: Продвинутая/Продвинутый/
    # Продвинутое; Экспертная/Экспертный/Экспертное.
    stem_map = [
        ("Основы",     "basic"),
        ("Продвинут",  "advanced"),
        ("Эксперт",    "expert"),
        ("Мастер",     "master"),
    ]
    tiers: dict[str, dict[str, Any]] = {}
    for sec in sections:
        if sec.get("level") != 2:
            continue
        title = sec.get("title", "")
        key = next((v for stem, v in stem_map if title.startswith(stem)), None)
        if key and key not in tiers:
            tiers[key] = sec
    if tiers:
        out["tiers"] = tiers
    return out


def parse_spell(html: str, url: str) -> dict[str, Any]:
    out = _common_meta(html, url)
    school = re.search(r'class="sp__school"[^>]*>([^<]+)<', html)
    if school:
        out["school"] = strip_tags(school.group(1))
    tags = re.findall(r'class="sp__tag"[^>]*>([^<]+)<', html)
    if tags:
        out["tags"] = [strip_tags(t) for t in tags]
    # cost rows: pair-by-pair (single </div> close — there's only one wrapper)
    costs: list[dict[str, str]] = []
    for c in re.finditer(
        r'class="sp__cost-row"[^>]*>(.*?)</div>', html, re.S):
        block = c.group(1)
        dts = re.findall(r'<dt[^>]*>(.*?)</dt>', block, re.S)
        dds = re.findall(r'<dd[^>]*>(.*?)</dd>', block, re.S)
        if dts and dds:
            costs.append({"label": strip_tags(dts[0]),
                           "value": strip_tags(dds[0])})
    if costs:
        out["costs"] = costs
    sections = parse_section_tree(html)
    out["sections"] = sections
    # tier blocks are <section class="level [active]" data-level="N">
    tiers: dict[str, dict[str, Any]] = {}
    for lvl in re.finditer(
        r'<section\b[^>]*class="level\b[^"]*"[^>]*\bdata-level="(?P<n>\d+)"[^>]*>(?P<body>.*?)</section>',
        html, re.S | re.I):
        n = lvl.group("n")
        body = lvl.group("body")
        entry: dict[str, Any] = {}
        mana = re.search(
            r'class="level__mana"[^>]*>([^<]*<strong[^>]*>([^<]+)</strong>)', body)
        if mana:
            entry["mana"] = strip_tags(mana.group(2))
        desc = re.search(r'class="level__desc"[^>]*>(.*?)</p>', body, re.S)
        if desc:
            entry["description"] = strip_tags(desc.group(1))
        mechs: list[dict[str, str]] = []
        for s in re.finditer(
            r'<li\b[^>]*class="spell-mech__item"[^>]*>(.*?)</li>', body, re.S):
            l = re.search(r'class="spell-mech__label"[^>]*>([^<]+)<', s.group(1))
            v = re.search(r'class="spell-mech__value"[^>]*>([^<]+)<', s.group(1))
            if l and v:
                mechs.append({"label": strip_tags(l.group(1)),
                               "value": strip_tags(v.group(1))})
        if mechs:
            entry["mechanics"] = mechs
        tiers[f"Уровень {n}"] = entry
    if tiers:
        out["tiers"] = tiers
    return out


def parse_laws(html: str, url: str | None = None) -> dict[str, Any]:
    """Parse the laws tree.

    paradrew renders every law as ``<div class="law-node" id="..." data-*>``.
    The ``data-*`` attributes carry all key metadata (faction, tier, branch,
    point costs, max level, name). Inside the node is an icon and the visible
    label; rules text usually lives inside a tooltip elsewhere or in the
    rendered text after the icon.
    """
    out: dict[str, Any] = {}
    if url:
        out["source_url"] = url
    # Top-of-page explanation + roadmap (everything before the calculator div)
    out["sections"] = parse_section_tree(html)
    out["_full_text"] = strip_tags(_scrub(_article_root(html)))
    out["_images"] = _capture_images(_article_root(html))

    # Enumerate every law-node
    node_re = re.compile(
        r'<div\b[^>]*\bclass="law-node[^"]*"[^>]*\bid="(?P<id>[^"]+)"(?P<attrs>[^>]*)>',
        re.I)
    nodes = list(node_re.finditer(html))
    entries: list[dict[str, Any]] = []
    for i, m in enumerate(nodes):
        end = nodes[i + 1].start() if i + 1 < len(nodes) else len(html)
        block = html[m.end():end]
        attrs_blob = m.group("attrs")
        entry: dict[str, Any] = {"slug": m.group("id")}
        # data-* attrs
        for a in re.finditer(r'\bdata-([a-z-]+)="([^"]*)"', attrs_blob):
            key, val = a.group(1), a.group(2).strip()
            # numeric coercion where it makes sense
            if key in ("tier", "max"):
                try: val = int(val)
                except ValueError: pass
            if key == "costs":
                val = [int(x) for x in val.split(",") if x.strip().lstrip("-").isdigit()]
            entry[f"data_{key}"] = val
        # human label
        if "data_name" in entry:
            entry["name"] = entry.pop("data_name")
        # any inline text (besides the icon) — capture as description
        # strip out the button/image, keep the rest as text
        body = re.sub(r'<button\b[^>]*>.*?</button>', '', block, flags=re.S | re.I)
        body = re.sub(r'<svg\b[^>]*>.*?</svg>', '', body, flags=re.S | re.I)
        body = re.sub(r'<img\b[^>]*>', '', body, flags=re.I)
        text = strip_tags(body)
        if text:
            entry["text"] = text
        # capture any list/dl inside
        lists = _capture_lists(block)
        if lists: entry["lists"] = lists
        stats = _capture_dl(block)
        if stats: entry["stats"] = stats
        entries.append(entry)
    out["entries"] = entries
    return out


# -------------------- Pipeline --------------------

SECTIONS = [
    ("units",  "/ru/olden-era/units",  parse_unit),
    ("heroes", "/ru/olden-era/heroes", parse_hero),
    ("skills", "/ru/olden-era/skills", parse_skill),
    ("spells", "/ru/olden-era/spells", parse_spell),
]


def scrape(out_dir: Path, cache_dir: Path, delay: float, limit: int | None) -> None:
    """Write one JSON per section into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_template = {
        "source": BASE,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "note": "paradrew.com — Russian Olden Era reference (canonical for stats).",
    }
    counts: dict[str, int] = {}

    def dump(name: str, items: Any, count: int):
        path = out_dir / f"{name}.json"
        payload = {"meta": {**meta_template, "section": name, "count": count},
                    name: items}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        log.info("wrote %s (%d)", path, count)

    for name, prefix, parser in SECTIONS:
        log.info("--- %s ---", name)
        index_url = urljoin(BASE, prefix + "/")
        index_html = fetch(index_url, cache_dir=cache_dir, delay=delay)
        slugs = extract_index_links(index_html, prefix + "/")
        log.info("  index: %d entries", len(slugs))
        if limit is not None:
            slugs = slugs[:limit]
        items = []
        for i, path in enumerate(slugs, 1):
            url = urljoin(BASE, path)
            try:
                html = fetch(url, cache_dir=cache_dir, delay=delay)
                items.append(parser(html, url))
            except Exception as exc:
                log.exception("  %s %d/%d %s → %s", name, i, len(slugs), url, exc)
                continue
            if i % 10 == 0 or i == len(slugs):
                log.info("  %s %d/%d", name, i, len(slugs))
        counts[name] = len(items)
        dump(name, items, len(items))

    # laws — one page with the whole tree
    log.info("--- laws ---")
    laws_url = urljoin(BASE, "/ru/olden-era/laws/")
    laws_html = fetch(laws_url, cache_dir=cache_dir, delay=delay)
    laws = parse_laws(laws_html, laws_url)
    n_laws = len(laws.get("entries", []))
    counts["laws"] = n_laws
    dump("laws", laws, n_laws)

    # guides — index + detail pages
    log.info("--- guides ---")
    guides_html = fetch(urljoin(BASE, "/ru/olden-era/guides/"),
                         cache_dir=cache_dir, delay=delay)
    guide_links = extract_index_links(guides_html, "/ru/olden-era/guides/")
    guide_items: list[dict[str, Any]] = []
    for path in guide_links:
        url = urljoin(BASE, path)
        try:
            h = fetch(url, cache_dir=cache_dir, delay=delay)
            name_m = re.search(r'<h1[^>]*>(.+?)</h1>', h, re.S)
            guide_items.append({
                "source_url": url,
                "name": strip_tags(name_m.group(1)) if name_m else url,
                "sections": parse_section_tree(h),
                "_full_text": strip_tags(_scrub(_article_root(h))),
            })
        except Exception as exc:
            log.warning("guide fetch failed %s: %s", url, exc)
    guides_payload = {
        "index": {
            "source_url": urljoin(BASE, "/ru/olden-era/guides/"),
            "sections": parse_section_tree(guides_html),
            "_full_text": strip_tags(_scrub(_article_root(guides_html))),
        },
        "items": guide_items,
    }
    counts["guides"] = len(guide_items)
    dump("guides", guides_payload, len(guide_items))

    # summary index
    summary = {
        "meta": {**meta_template, "counts": counts},
        "files": {n: f"{n}.json" for n in
                   ("units", "heroes", "skills", "spells", "laws", "guides")},
    }
    (out_dir / "_index.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2))
    log.info("done. counts: %s", counts)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help="output directory; one JSON file per section")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--delay", type=float, default=0.25,
                   help="seconds between requests (default 0.25)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap entries per section (for smoke testing)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    scrape(args.out, args.cache_dir, args.delay, args.limit)


if __name__ == "__main__":
    main()
