#!/usr/bin/env python3
# scripts/mandem/news_image.py
# Image search via DuckDuckGo's image endpoint (no API key required).
# Returns top result's URL + downloads it locally.
#
# DMCA NOTE: results are from news / agency / club sites. They may be copyrighted.
# Mandem MVP accepts this risk; switch to Brave Search API or Imago license when the
# account grows past hobby scale.

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DDG_HTML = "https://duckduckgo.com/"
DDG_JSON = "https://duckduckgo.com/i.js"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class NewsImage:
    url: str            # full-size source URL
    thumbnail: str      # DDG-cached thumbnail (faster, smaller)
    title: str
    source: str         # publishing site domain
    width: int
    height: int


def _get_vqd(query: str) -> str:
    """DDG image search needs a vqd token issued by the HTML page."""
    url = DDG_HTML + "?" + urllib.parse.urlencode({"q": query, "iax": "images", "ia": "images"})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", errors="ignore")
    # vqd appears multiple ways in DDG HTML — try them in order
    patterns = [
        r'vqd=["\']([\d-]+)["\']',
        r'vqd=([\d-]+)&',
        r'"vqd":"([\d-]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    raise RuntimeError("DDG: vqd token not found in image-search HTML")


def search(query: str, max_results: int = 10) -> list[NewsImage]:
    """Search DDG images. Returns up to max_results in DDG's relevance order."""
    vqd = _get_vqd(query)
    params = {
        "l": "us-en",
        "o": "json",
        "q": query,
        "vqd": vqd,
        "f": ",,,",
        "p": "1",
        "v7exp": "a",
    }
    url = DDG_JSON + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://duckduckgo.com/",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read().decode("utf-8", errors="ignore")
    data = json.loads(body)
    results = data.get("results") or []
    out: list[NewsImage] = []
    for item in results[:max_results]:
        out.append(NewsImage(
            url=item.get("image") or "",
            thumbnail=item.get("thumbnail") or "",
            title=item.get("title") or "",
            source=item.get("source") or "",
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
        ))
    return [r for r in out if r.url]


def download(image: NewsImage, out_dir: Path, filename_hint: str = "news") -> Path:
    """Download a NewsImage's full-size URL to disk. Falls back to thumbnail on 4xx/5xx."""
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = [(image.url, "url"), (image.thumbnail, "thumbnail")]
    last_err = None
    for url, label in candidates:
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://duckduckgo.com/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
                ctype = r.headers.get("Content-Type", "")
            ext = "jpg"
            if "png" in ctype:
                ext = "png"
            elif "webp" in ctype:
                ext = "webp"
            elif "gif" in ctype:
                ext = "gif"
            out = out_dir / f"{filename_hint}_{int(time.time())}.{ext}"
            out.write_bytes(data)
            return out
        except Exception as e:  # try the next URL
            last_err = f"{label}: {e}"
            continue
    raise RuntimeError(f"news_image.download failed for both URL and thumbnail. last: {last_err}")


def search_and_download(query: str, out_dir: Path, filename_hint: str = "news") -> tuple[Path, NewsImage]:
    """Convenience: search → pick top result → download. Returns (path, NewsImage meta).
    Legacy single-shot path. New code should prefer search_top_n + download_url so the
    agent can pick a relevant result from a list rather than blindly trusting #1."""
    results = search(query, max_results=10)
    if not results:
        raise RuntimeError(f"no DDG image results for query={query!r}")
    top = results[0]
    path = download(top, out_dir=out_dir, filename_hint=filename_hint)
    return path, top


# Quality lever: the stylise edits to mush from a tiny source. Drop unusable
# thumbnails and surface the highest-resolution candidates first so the agent picks
# a sharp photo (aura-sr upscale in falimg backstops anything still on the small side).
_MIN_USABLE_PX = 700


def _rank_candidates(items: list[dict]) -> list[dict]:
    """Drop candidates with KNOWN small dimensions; sort the rest by pixel area
    (largest first). Items with unknown dims (0) or errors are kept, ranked last."""
    def area(d: dict) -> int:
        return int(d.get("width") or 0) * int(d.get("height") or 0)

    def usable(d: dict) -> bool:
        w, h = int(d.get("width") or 0), int(d.get("height") or 0)
        if w == 0 and h == 0:
            return True  # unknown dims — can't judge, keep it
        return max(w, h) >= _MIN_USABLE_PX

    return sorted([d for d in items if usable(d)], key=lambda d: -area(d))


def search_top_n(query: str, n: int = 5, source: str = "ddg") -> list[dict]:
    """Search for images, return top N with metadata (NO download), ranked so the
    HIGHEST-RESOLUTION candidates come first and tiny thumbnails are dropped.
    The agent then picks the most relevant and calls download_url.

    `source`: 'ddg' (DuckDuckGo, no key) or 'brave' (needs BRAVE_API_KEY).
    """
    if source == "brave":
        return _rank_candidates(_brave_search_top_n(query, n))
    # default DDG
    items = search(query, max_results=n)
    return _rank_candidates([{
        "url": i.url,
        "thumbnail": i.thumbnail,
        "title": i.title,
        "source": i.source,
        "width": i.width,
        "height": i.height,
        "engine": "ddg",
    } for i in items])


def download_url(url: str, out_dir: Path, filename_hint: str = "img",
                 referer: str = "") -> Path:
    """Download a specific image URL. Used after search_top_n + agent rerank."""
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
        ctype = r.headers.get("Content-Type", "")
    ext = "jpg"
    if "png" in ctype:
        ext = "png"
    elif "webp" in ctype:
        ext = "webp"
    out = out_dir / f"{filename_hint}_{int(time.time())}.{ext}"
    out.write_bytes(data)
    return out


# ---------- Brave Search Image API ----------

BRAVE_IMAGE_ENDPOINT = "https://api.search.brave.com/res/v1/images/search"


def _brave_search_top_n(query: str, n: int = 5) -> list[dict]:
    """Brave Search images vertical. Free tier: 2,000 queries/mo. Better ranking than DDG.
    Requires BRAVE_API_KEY in env. Returns [] if key missing (caller should fall back)."""
    api_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not api_key:
        return []
    qs = urllib.parse.urlencode({
        "q": query,
        "count": min(int(n), 20),
        "safesearch": "off",  # we want sports/news content; sometimes match photos get flagged
    })
    req = urllib.request.Request(
        f"{BRAVE_IMAGE_ENDPOINT}?{qs}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return [{"engine": "brave", "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}]
    except Exception as e:
        return [{"engine": "brave", "error": str(e)[:200]}]
    out = []
    for r in (data.get("results") or [])[:n]:
        props = r.get("properties") or {}
        out.append({
            "url": r.get("thumbnail", {}).get("src") or r.get("url") or props.get("url"),
            # Brave gives both a thumbnail AND a source page; for direct image, use thumbnail.src
            "thumbnail": r.get("thumbnail", {}).get("src", ""),
            "title": r.get("title") or "",
            "source": (r.get("source") or "")[:60],
            "page_url": r.get("url"),
            "width": int((props.get("width") or 0)),
            "height": int((props.get("height") or 0)),
            "engine": "brave",
        })
    return out


# ---------- CLI ----------

def _cli(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="news_image")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="search DDG images and print top results")
    s.add_argument("--query", required=True)
    s.add_argument("--max", type=int, default=5)
    d = sub.add_parser("download", help="search + download top result")
    d.add_argument("--query", required=True)
    from . import _env
    d.add_argument("--out-dir", default=str(_env.data_dir() / "images"))
    args = p.parse_args(argv)
    if args.cmd == "search":
        for i, r in enumerate(search(args.query, max_results=args.max), 1):
            print(f"  [{i}] {r.source}  {r.width}x{r.height}  {r.title[:80]}")
            print(f"      {r.url}")
    elif args.cmd == "download":
        path, meta = search_and_download(args.query, Path(args.out_dir))
        print(f"  saved: {path}")
        print(f"  source: {meta.source}")
        print(f"  title: {meta.title}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
