"""Lossless verification for the paradrew scrape.

For every page we have on disk (in ``cache_pd/``) compare the raw HTML's
visible text against what's serialized in ``data/<section>.json``. Reports:

- coverage: fraction of source words/tokens that appear in the JSON record
- missing data: words present in HTML but absent from the JSON record
- coverage of typed fields (stats, tiers, etc.)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from scrape_paradrew import _scrub, _article_root, strip_tags  # type: ignore

CACHE = SCRIPT_DIR / "cache_pd"
DATA = SCRIPT_DIR / "data"

# Glue words / boilerplate to ignore in coverage computation
STOPWORDS = set("""
а в во и или не на по с со для от до за из у к ко о об что как это эти этот эта тот та те бы же ли ну да то ни но
the a of to in for with on at by is are was were be been being and or not it this that these those an as if then so
javascript fonts googleapis gstatic webp avif png jpg svg w h ai astro paradrew tier
""".split())


def tokens(text: str) -> set[str]:
    text = text.lower()
    text = re.sub(r"[^a-z0-9а-яёїіє\s-]+", " ", text)
    raw = [w for w in text.split() if len(w) > 1 and w not in STOPWORDS]
    return set(raw)


def flatten_json(obj) -> str:
    """Concatenate every string value in a nested structure."""
    buf: list[str] = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                buf.append(str(k))
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, str):
            buf.append(o)
        elif o is not None:
            buf.append(str(o))
    walk(obj)
    return " ".join(buf)


def html_visible_text(html: str) -> str:
    body = _scrub(_article_root(html))
    return strip_tags(body)


def url_to_cache_path(url: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", url)[-180:]
    return CACHE / f"{safe}.html"


SECTIONS = ["units", "heroes", "skills", "spells", "laws", "guides"]


def section_iter(section: str):
    """Yield (record, source_html_text) pairs."""
    path = DATA / f"{section}.json"
    blob = json.loads(path.read_text())
    items = blob.get(section)
    if section == "laws":
        # laws is a single page with .entries
        url = items.get("source_url")
        html_path = url_to_cache_path(url) if url else None
        if html_path and html_path.exists():
            html = html_path.read_text(encoding="utf-8")
            yield ("laws (full page)", items, html_visible_text(html))
        return
    if section == "guides":
        # guides has index + items
        for it in items.get("items", []):
            url = it.get("source_url")
            html_path = url_to_cache_path(url) if url else None
            if html_path and html_path.exists():
                html = html_path.read_text(encoding="utf-8")
                yield (it.get("name") or url, it, html_visible_text(html))
        return
    # units / heroes / skills / spells — list of records, each from a unique URL
    for rec in items:
        url = rec.get("source_url")
        html_path = url_to_cache_path(url) if url else None
        if not (html_path and html_path.exists()):
            continue
        html = html_path.read_text(encoding="utf-8")
        yield (rec.get("name") or url, rec, html_visible_text(html))


def verify_section(section: str, sample_missing: int = 0) -> dict:
    total_records = 0
    total_src_tokens = 0
    total_covered = 0
    worst = []  # (coverage, name, missing_sample)
    for name, rec, src_text in section_iter(section):
        src = tokens(src_text)
        if not src:
            continue
        rec_text = flatten_json(rec)
        rec_toks = tokens(rec_text)
        covered = src & rec_toks
        missing = src - rec_toks
        total_records += 1
        total_src_tokens += len(src)
        total_covered += len(covered)
        coverage = len(covered) / max(1, len(src))
        worst.append((coverage, name, missing))
    worst.sort(key=lambda x: x[0])
    return {
        "section": section,
        "records_compared": total_records,
        "src_tokens": total_src_tokens,
        "covered_tokens": total_covered,
        "coverage_pct": round(100 * total_covered / max(1, total_src_tokens), 2),
        "worst": worst[:sample_missing],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sample", type=int, default=3,
                   help="show N worst-coverage records per section (default 3)")
    p.add_argument("--max-missing", type=int, default=15,
                   help="max missing tokens to print per record")
    args = p.parse_args()
    print(f"{'section':<10} {'records':>8} {'src_tokens':>11} {'covered':>9} {'pct':>7}")
    print("-" * 50)
    grand_src = grand_cov = 0
    detail = []
    for s in SECTIONS:
        r = verify_section(s, sample_missing=args.sample)
        print(f"{r['section']:<10} {r['records_compared']:>8} "
              f"{r['src_tokens']:>11} {r['covered_tokens']:>9} "
              f"{r['coverage_pct']:>6}%")
        grand_src += r['src_tokens']
        grand_cov += r['covered_tokens']
        detail.append(r)
    print("-" * 50)
    pct = round(100 * grand_cov / max(1, grand_src), 2)
    print(f"{'TOTAL':<10} {'':>8} {grand_src:>11} {grand_cov:>9} {pct:>6}%")
    print()
    for r in detail:
        if not r["worst"]:
            continue
        print(f"\n=== {r['section']}: {len(r['worst'])} worst-coverage records ===")
        for cov, name, miss in r["worst"]:
            print(f"  [{cov*100:5.1f}%] {name}")
            sample = sorted(miss)[: args.max_missing]
            print(f"    missing sample: {sample}")


if __name__ == "__main__":
    main()
