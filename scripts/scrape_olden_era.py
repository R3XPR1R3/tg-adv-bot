"""Scrape the Heroes of Might and Magic: Olden Era section of wiki.hoodedhorse.com.

Four phases (run with ``--phase``):
  discover  - BFS through internal links to enumerate every page in the section
  fetch     - render each page through Playwright (bypasses Cloudflare challenge),
              save raw HTML to disk
  parse     - turn the saved HTML into a generic structured representation
              (infobox, tables, sections, links, categories)
  normalize - classify pages into heroes / units / spells / skills / factions / other
              and emit the final JSON file
  all       - all four in order
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

BASE = "https://wiki.hoodedhorse.com"
SECTION_PREFIX = "/Heroes_of_Might_and_Magic_Olden_Era"
SEEDS = [
    f"{BASE}{SECTION_PREFIX}",
    f"{BASE}{SECTION_PREFIX}/%D0%A7%D0%B0%D1%80%D0%BE%D0%B4%D0%B5%D0%B9%D1%81%D1%82%D0%B2%D0%BE",
]

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE = SCRIPT_DIR / "cache"
DEFAULT_OUT = SCRIPT_DIR / "olden_era.json"

log = logging.getLogger("scrape")


# -------------------- URL helpers --------------------

def normalize_url(href: str, page_url: str) -> str | None:
    """Resolve a link and decide whether it belongs to the Olden Era section."""
    if not href:
        return None
    href = href.split("#", 1)[0].strip()
    if not href:
        return None
    abs_url = urljoin(page_url, href)
    parsed = urlparse(abs_url)
    if parsed.netloc and parsed.netloc != urlparse(BASE).netloc:
        return None
    path = parsed.path
    if not path.startswith(SECTION_PREFIX):
        return None
    # decoded path part for keyword filtering (Special:, File:, etc are namespace
    # prefixes that show up after the last slash in MediaWiki URLs)
    decoded = unquote(path)
    last = decoded.rsplit("/", 1)[-1]
    for ns in ("Special:", "File:", "Talk:", "User:", "Template:", "Help:",
               "MediaWiki:", "Служебная:", "Файл:", "Обсуждение:",
               "Участник:", "Шаблон:", "Справка:"):
        if last.startswith(ns):
            return None
    # strip query (we want canonical page URL)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def url_slug(url: str) -> str:
    return unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "index"


# -------------------- Playwright wrapper --------------------

class Browser:
    """Async Playwright wrapper with Cloudflare-challenge handling.

    Single page, single context — Cloudflare is paranoid about parallelism.
    """

    def __init__(self, delay: float):
        self.delay = delay
        self._pw = None
        self._browser = None
        self._ctx = None
        self.page = None
        self._last_request_at = 0.0

    async def __aenter__(self):
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        self._ctx = await self._browser.new_context(
            user_agent=UA,
            locale="ru-RU",
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )
        # Stealth — try the modern API, fall back to legacy
        try:
            from playwright_stealth import Stealth  # type: ignore

            await Stealth().apply_stealth_async(self._ctx)
        except Exception:
            try:
                from playwright_stealth import stealth_async  # type: ignore

                self._ctx_stealth = stealth_async
            except Exception:
                log.warning("playwright-stealth not active; continuing without it")
        self.page = await self._ctx.new_page()
        if hasattr(self, "_ctx_stealth"):
            try:
                await self._ctx_stealth(self.page)
            except Exception:
                pass
        return self

    async def __aexit__(self, *_):
        try:
            await self._ctx.close()
        finally:
            await self._browser.close()
            await self._pw.stop()

    async def _throttle(self):
        delta = time.monotonic() - self._last_request_at
        if delta < self.delay:
            await asyncio.sleep(self.delay - delta)
        self._last_request_at = time.monotonic()

    async def goto(self, url: str, *, max_retries: int = 5) -> str:
        """Navigate to ``url`` and return the page HTML once Cloudflare is cleared."""
        for attempt in range(1, max_retries + 1):
            await self._throttle()
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                log.warning("goto error (attempt %d): %s", attempt, exc)
                await asyncio.sleep(2 ** attempt)
                continue

            # Cloudflare challenge shows up as title "Just a moment..." or a
            # known form id. Wait it out — Cloudflare auto-redirects once JS
            # passes.
            cleared = await self._wait_cloudflare_clear()
            if cleared:
                return await self.page.content()
            log.warning("Cloudflare challenge not cleared on attempt %d", attempt)
            await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"failed to clear Cloudflare for {url}")

    async def _wait_cloudflare_clear(self, timeout: float = 35.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                title = (await self.page.title()) or ""
                if "Just a moment" not in title and "Attention Required" not in title:
                    # also make sure body exists and is non-trivial
                    body_len = await self.page.evaluate(
                        "() => (document.body && document.body.innerText || '').length"
                    )
                    if body_len > 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False


# -------------------- Phase 1: discover --------------------

async def phase_discover(cache_dir: Path, delay: float, limit: int | None) -> Path:
    out = cache_dir / "urls.json"
    seen: set[str] = set()
    queue: deque[str] = deque()
    for s in SEEDS:
        if s not in seen:
            seen.add(s)
            queue.append(s)

    async with Browser(delay) as br:
        n = 0
        while queue:
            url = queue.popleft()
            n += 1
            log.info("[discover %d] %s", n, url)
            try:
                html = await br.goto(url)
            except Exception as exc:
                log.error("discover failed for %s: %s", url, exc)
                continue
            # extract links from the rendered page
            try:
                hrefs = await br.page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]'))."
                    "map(a => a.getAttribute('href'))"
                )
            except Exception:
                hrefs = []
            new_count = 0
            for h in hrefs:
                u = normalize_url(h, url)
                if u and u not in seen:
                    seen.add(u)
                    queue.append(u)
                    new_count += 1
            log.info("  found %d new URLs (queue=%d, total=%d)",
                     new_count, len(queue), len(seen))
            if limit is not None and n >= limit:
                log.info("hit --limit=%d during discover", limit)
                break

    payload = {
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "count": len(seen),
        "urls": sorted(seen),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    log.info("wrote %s (%d urls)", out, len(seen))
    return out


# -------------------- Phase 2: fetch --------------------

async def phase_fetch(cache_dir: Path, delay: float, limit: int | None) -> None:
    urls_file = cache_dir / "urls.json"
    if not urls_file.exists():
        raise SystemExit("run --phase discover first")
    urls = json.loads(urls_file.read_text())["urls"]
    html_dir = cache_dir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    mapping_path = cache_dir / "url_map.json"
    mapping: dict[str, str] = (
        json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
    )
    todo = [u for u in urls if not (html_dir / f"{url_hash(u)}.html").exists()]
    log.info("fetch: %d todo / %d total", len(todo), len(urls))
    if limit is not None:
        todo = todo[:limit]

    async with Browser(delay) as br:
        for i, u in enumerate(todo, 1):
            log.info("[fetch %d/%d] %s", i, len(todo), u)
            try:
                html = await br.goto(u)
            except Exception as exc:
                log.error("fetch failed for %s: %s", u, exc)
                continue
            h = url_hash(u)
            (html_dir / f"{h}.html").write_text(html, encoding="utf-8")
            mapping[h] = u
            if i % 10 == 0 or i == len(todo):
                mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))
        mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))
    log.info("fetch done; %d pages on disk", len(mapping))


# -------------------- Phase 3: parse --------------------

def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parse_infobox(soup) -> dict[str, Any]:
    box = soup.select_one(".infobox, table.infobox")
    if not box:
        return {}
    out: dict[str, Any] = {}
    for tr in box.select("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) == 2:
            key = _clean_text(cells[0].get_text(" "))
            val = _clean_text(cells[1].get_text(" "))
            if key:
                out[key] = val
        elif len(cells) == 1:
            # caption-like row — keep under a synthetic key
            text = _clean_text(cells[0].get_text(" "))
            if text:
                out.setdefault("_caption", []).append(text)
    return out


def _parse_table(table) -> dict[str, Any]:
    caption_el = table.find("caption")
    caption = _clean_text(caption_el.get_text(" ")) if caption_el else None
    rows = table.find_all("tr")
    headers: list[str] = []
    data: list[list[str]] = []
    for i, tr in enumerate(rows):
        ths = tr.find_all("th")
        tds = tr.find_all("td")
        if i == 0 and ths and not tds:
            headers = [_clean_text(th.get_text(" ")) for th in ths]
            continue
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        data.append([_clean_text(c.get_text(" ")) for c in cells])
    return {"caption": caption, "headers": headers, "rows": data}


def _parse_sections(content) -> list[dict[str, Any]]:
    """Walk top-level children of the article body and group by headings."""
    sections: list[dict[str, Any]] = []
    path: list[str] = []
    buf: list[str] = []
    lists: list[list[str]] = []

    def flush():
        if not (path or buf or lists):
            return
        sections.append({
            "heading_path": list(path),
            "text": _clean_text(" ".join(buf)),
            "lists": [lst for lst in lists if lst],
        })

    for el in content.children:
        name = getattr(el, "name", None)
        if name in {"h2", "h3", "h4", "h5"}:
            flush()
            buf = []
            lists = []
            level = int(name[1])
            head_text = _clean_text(el.get_text(" "))
            # crude path: keep only as many ancestors as level allows
            target_depth = level - 1  # h2 -> depth 1
            path = path[: target_depth - 1] + [head_text]
        elif name in {"p", "div"} and el.get_text(strip=True):
            buf.append(_clean_text(el.get_text(" ")))
        elif name in {"ul", "ol"}:
            lst = [_clean_text(li.get_text(" ")) for li in el.find_all("li", recursive=False)]
            lists.append(lst)
    flush()
    return sections


def parse_page(html: str, url: str) -> dict[str, Any]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one("#firstHeading, h1")
    title = _clean_text(title_el.get_text(" ")) if title_el else url_slug(url)
    content = soup.select_one("#mw-content-text .mw-parser-output, #mw-content-text, #content")
    if content is None:
        content = soup.body or soup
    # strip edit-section links, references, etc, to clean the text
    for sel in (".mw-editsection", ".mw-cite-backlink", "sup.reference"):
        for n in content.select(sel):
            n.decompose()
    categories = [
        _clean_text(a.get_text(" "))
        for a in soup.select("#mw-normal-catlinks a")
        if "Категории" not in a.get_text() and "Categories" not in a.get_text()
    ]
    infobox = _parse_infobox(content)
    tables = [_parse_table(t) for t in content.select("table.wikitable")]
    sections = _parse_sections(content)
    links: list[str] = []
    for a in content.select("a[href]"):
        u = normalize_url(a.get("href", ""), url)
        if u and u != url:
            links.append(u)
    links = sorted(set(links))
    images = []
    img_root = content.select_one(".infobox") or content
    for img in img_root.select("img"):
        src = img.get("src") or ""
        if src:
            images.append({"src": urljoin(url, src), "alt": img.get("alt") or ""})
    return {
        "title": title,
        "url": url,
        "slug": url_slug(url),
        "categories": categories,
        "infobox": infobox,
        "tables": tables,
        "sections": sections,
        "links": links,
        "images": images,
    }


def phase_parse(cache_dir: Path) -> Path:
    html_dir = cache_dir / "html"
    mapping_path = cache_dir / "url_map.json"
    if not mapping_path.exists():
        raise SystemExit("run --phase fetch first")
    mapping: dict[str, str] = json.loads(mapping_path.read_text())
    pages: list[dict[str, Any]] = []
    for h, url in sorted(mapping.items(), key=lambda kv: kv[1]):
        f = html_dir / f"{h}.html"
        if not f.exists():
            log.warning("missing html for %s (%s)", url, h)
            continue
        html = f.read_text(encoding="utf-8")
        try:
            page = parse_page(html, url)
        except Exception as exc:
            log.exception("parse failed for %s: %s", url, exc)
            continue
        pages.append(page)
    out = cache_dir / "raw_pages.json"
    out.write_text(json.dumps({
        "meta": {
            "source": BASE,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "count": len(pages),
        },
        "pages": pages,
    }, ensure_ascii=False, indent=2))
    log.info("wrote %s (%d pages)", out, len(pages))
    return out


# -------------------- Phase 4: normalize --------------------

NUM_RE = re.compile(r"^-?\d+([.,]\d+)?$")


def _to_num(s: str):
    s = s.strip().replace(" ", " ")
    m = NUM_RE.match(s.replace(" ", ""))
    if not m:
        return s
    s2 = s.replace(",", ".").replace(" ", "")
    try:
        if "." in s2:
            return float(s2)
        return int(s2)
    except ValueError:
        return s


def _typify(d: dict[str, Any]) -> dict[str, Any]:
    return {k: _to_num(v) if isinstance(v, str) else v for k, v in d.items()}


def _has_any(box_keys: set[str], *needles: str) -> bool:
    low = {k.lower() for k in box_keys}
    return any(any(n in k for k in low) for n in (n.lower() for n in needles))


def _cats_contain(cats: list[str], *needles: str) -> bool:
    low = [c.lower() for c in cats]
    return any(any(n.lower() in c for c in low) for n in needles)


def classify(page: dict[str, Any]) -> str:
    cats = page.get("categories", [])
    box = page.get("infobox", {}) or {}
    keys = set(box.keys())
    title_low = page.get("title", "").lower()

    if _cats_contain(cats, "Герои", "Heroes"):
        return "hero"
    if _has_any(keys, "Класс", "Фракция", "Специализация", "Class", "Faction", "Specialty"):
        return "hero"
    if _cats_contain(cats, "Существа", "Юниты", "Units", "Creatures"):
        return "unit"
    if _has_any(keys, "Атака", "Защита", "Здоровье", "Урон", "Attack", "Defense", "Health", "Damage"):
        return "unit"
    if _cats_contain(cats, "Заклинания", "Spells"):
        return "spell"
    if _has_any(keys, "Школа", "Мана", "School", "Mana"):
        return "spell"
    if _cats_contain(cats, "Навыки", "Skills"):
        return "skill"
    # Skill detection by tier headings inside sections
    section_headings = " ".join(
        " ".join(s.get("heading_path", [])) for s in page.get("sections", [])
    ).lower()
    if any(t in section_headings for t in ("базовый", "продвинутый", "эксперт", "мастер")):
        return "skill"
    if _cats_contain(cats, "Фракции", "Factions"):
        return "faction"
    if _has_any(keys, "Столица", "Capital"):
        return "faction"
    if _cats_contain(cats, "Артефакты", "Artifacts"):
        return "artifact"
    if _cats_contain(cats, "Постройки", "Buildings"):
        return "building"
    return "other"


def _section_by_heading(page: dict[str, Any], *needles: str) -> str | None:
    needles_low = [n.lower() for n in needles]
    for s in page.get("sections", []):
        head = " ".join(s.get("heading_path", [])).lower()
        if any(n in head for n in needles_low):
            return s.get("text") or None
    return None


def normalize_hero(page) -> dict[str, Any]:
    box = _typify(page.get("infobox", {}) or {})
    out: dict[str, Any] = {
        "name": page["title"],
        "source_url": page["url"],
    }
    for src, dst in [
        ("Фракция", "faction"), ("Faction", "faction"),
        ("Класс", "class"), ("Class", "class"),
        ("Специализация", "specialty"), ("Specialty", "specialty"),
    ]:
        if src in box and dst not in out:
            out[dst] = box[src]
    # starting stats
    stats = {}
    for src, dst in [
        ("Атака", "attack"), ("Защита", "defense"),
        ("Сила магии", "spellpower"), ("Знания", "knowledge"),
        ("Attack", "attack"), ("Defense", "defense"),
        ("Spellpower", "spellpower"), ("Knowledge", "knowledge"),
    ]:
        if src in box:
            stats[dst] = box[src]
    if stats:
        out["starting_stats"] = stats
    desc = _section_by_heading(page, "Описание", "Биография", "Description", "Biography")
    if desc:
        out["description"] = desc
    out["infobox"] = box
    return out


def normalize_unit(page) -> dict[str, Any]:
    box = _typify(page.get("infobox", {}) or {})
    stats = {}
    for src, dst in [
        ("Атака", "attack"), ("Защита", "defense"),
        ("Здоровье", "hp"), ("Урон", "damage"),
        ("Скорость", "speed"), ("Инициатива", "initiative"),
        ("Дальность", "range"), ("Боезапас", "ammo"),
        ("Мана", "mana"),
        ("Attack", "attack"), ("Defense", "defense"),
        ("Health", "hp"), ("Damage", "damage"),
        ("Speed", "speed"), ("Initiative", "initiative"),
    ]:
        if src in box:
            stats[dst] = box[src]
    out: dict[str, Any] = {"name": page["title"], "source_url": page["url"]}
    for src, dst in [
        ("Фракция", "faction"), ("Faction", "faction"),
        ("Уровень", "tier"), ("Tier", "tier"),
        ("Стоимость", "cost"), ("Cost", "cost"),
        ("Прирост", "growth"), ("Growth", "growth"),
    ]:
        if src in box and dst not in out:
            out[dst] = box[src]
    if stats:
        out["stats"] = stats
    abilities = _section_by_heading(page, "Способности", "Abilities")
    if abilities:
        out["abilities_text"] = abilities
    out["infobox"] = box
    return out


def normalize_spell(page) -> dict[str, Any]:
    box = _typify(page.get("infobox", {}) or {})
    out = {"name": page["title"], "source_url": page["url"]}
    for src, dst in [
        ("Школа", "school"), ("School", "school"),
        ("Мана", "mana_cost"), ("Mana", "mana_cost"),
        ("Уровень", "level"), ("Level", "level"),
    ]:
        if src in box:
            out[dst] = box[src]
    for label in ("Базовый", "Продвинутый", "Эксперт", "Мастер",
                  "Basic", "Advanced", "Expert", "Master"):
        text = _section_by_heading(page, label)
        if text:
            key = {
                "Базовый": "effect_basic", "Продвинутый": "effect_advanced",
                "Эксперт": "effect_expert", "Мастер": "effect_master",
                "Basic": "effect_basic", "Advanced": "effect_advanced",
                "Expert": "effect_expert", "Master": "effect_master",
            }[label]
            out.setdefault(key, text)
    out["infobox"] = box
    return out


def normalize_skill(page) -> dict[str, Any]:
    out: dict[str, Any] = {"name": page["title"], "source_url": page["url"]}
    tiers: dict[str, str] = {}
    for label, key in (
        ("Базовый", "basic"), ("Продвинутый", "advanced"),
        ("Эксперт", "expert"), ("Мастер", "master"),
        ("Basic", "basic"), ("Advanced", "advanced"),
        ("Expert", "expert"), ("Master", "master"),
    ):
        text = _section_by_heading(page, label)
        if text and key not in tiers:
            tiers[key] = text
    if tiers:
        out["tiers"] = tiers
    desc = _section_by_heading(page, "Описание", "Description")
    if desc:
        out["description"] = desc
    perks: list[str] = []
    for s in page.get("sections", []):
        head = " ".join(s.get("heading_path", [])).lower()
        if "перк" in head or "perk" in head or "умен" in head:
            for lst in s.get("lists", []):
                perks.extend(lst)
    if perks:
        out["perks"] = perks
    return out


def normalize_faction(page) -> dict[str, Any]:
    box = _typify(page.get("infobox", {}) or {})
    out = {"name": page["title"], "source_url": page["url"]}
    for src, dst in [
        ("Столица", "capital"), ("Capital", "capital"),
        ("Бонусы", "bonuses"), ("Bonuses", "bonuses"),
    ]:
        if src in box:
            out[dst] = box[src]
    desc = _section_by_heading(page, "Описание", "Description", "Лор", "Lore")
    if desc:
        out["description"] = desc
    out["infobox"] = box
    return out


def phase_normalize(cache_dir: Path, out_path: Path) -> Path:
    raw_path = cache_dir / "raw_pages.json"
    if not raw_path.exists():
        raise SystemExit("run --phase parse first")
    raw = json.loads(raw_path.read_text())
    bucket: dict[str, list[dict[str, Any]]] = {
        "heroes": [], "units": [], "spells": [], "skills": [],
        "factions": [], "artifacts": [], "buildings": [], "other": [],
    }
    for page in raw["pages"]:
        kind = classify(page)
        if kind == "hero":
            bucket["heroes"].append(normalize_hero(page))
        elif kind == "unit":
            bucket["units"].append(normalize_unit(page))
        elif kind == "spell":
            bucket["spells"].append(normalize_spell(page))
        elif kind == "skill":
            bucket["skills"].append(normalize_skill(page))
        elif kind == "faction":
            bucket["factions"].append(normalize_faction(page))
        elif kind == "artifact":
            bucket["artifacts"].append({
                "name": page["title"], "source_url": page["url"],
                "infobox": _typify(page.get("infobox", {}) or {}),
            })
        elif kind == "building":
            bucket["buildings"].append({
                "name": page["title"], "source_url": page["url"],
                "infobox": _typify(page.get("infobox", {}) or {}),
            })
        else:
            bucket["other"].append({"title": page["title"], "url": page["url"],
                                     "raw": page})
    final = {
        "meta": {
            "source": BASE,
            "scraped_at": raw["meta"]["scraped_at"],
            "normalized_at": datetime.now(timezone.utc).isoformat(),
            "counts": {k: len(v) for k, v in bucket.items()},
            "version": 1,
        },
        **bucket,
    }
    out_path.write_text(json.dumps(final, ensure_ascii=False, indent=2))
    log.info("wrote %s", out_path)
    for k, v in bucket.items():
        log.info("  %s: %d", k, len(v))
    return out_path


# -------------------- entry point --------------------

async def main_async(args):
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    if args.phase in ("discover", "all"):
        await phase_discover(args.cache_dir, args.delay, args.limit)
    if args.phase in ("fetch", "all"):
        await phase_fetch(args.cache_dir, args.delay, args.limit)
    if args.phase in ("parse", "all"):
        phase_parse(args.cache_dir)
    if args.phase in ("normalize", "all"):
        phase_normalize(args.cache_dir, args.out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase",
                        choices=["discover", "fetch", "parse", "normalize", "all"],
                        default="all")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE,
                        help="where to keep urls.json / html cache / raw_pages.json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="final classified JSON")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="seconds between requests (default 1.5)")
    parser.add_argument("--limit", type=int, default=None,
                        help="max URLs to process in discover/fetch (for smoke testing)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
