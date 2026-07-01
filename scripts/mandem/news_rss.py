#!/usr/bin/env python3
# scripts/mandem/news_rss.py
# RSS poller for football news + Reddit r/soccer top-hourly radar.
# Inserts into news_items / reddit_radar with sha256(url)/permalink dedup.
#
# Stdlib-only (urllib + xml.etree). No feedparser dep needed for these well-formed feeds.

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from . import _env
from .season_mode import current_mode

DB_PATH = _env.data_dir() / "db.sqlite"
USER_AGENT = "mandem-fc-agent/0.1 (football-social-agent)"

# Mainstream RSS sources — narrative, daily, free
RSS_FEEDS: dict[str, dict] = {
    "rss:bbc-football": {
        "name": "BBC Sport — Football",
        "url": "http://feeds.bbci.co.uk/sport/football/rss.xml",
    },
    "rss:guardian-football": {
        "name": "The Guardian — Football",
        "url": "https://www.theguardian.com/football/rss",
    },
    "rss:espn-soccer": {
        "name": "ESPN Soccer",
        "url": "https://www.espn.com/espn/rss/soccer/news",
    },
    "rss:skysports-football": {
        "name": "Sky Sports — Football",
        "url": "https://www.skysports.com/rss/0,20514,11661,00.xml",
    },
}

# Transfer-specific feeds — only polled when the content mode has the transfer window
# open (summer / international break). See season_mode.py. Verified transfer-specific.
TRANSFER_FEEDS: dict[str, dict] = {
    "rss:guardian-transfers": {
        "name": "The Guardian — Transfer Window",
        "url": "https://www.theguardian.com/football/transfer-window/rss",
    },
    "rss:bbc-gossip": {
        "name": "BBC Sport — Gossip Column",
        "url": "https://feeds.bbci.co.uk/sport/football/gossip/rss.xml",
    },
}

REDDIT_SOCCER_URL = "https://www.reddit.com/r/soccer/top.json?t=hour&limit=25"


@dataclass
class NewsItem:
    source: str
    source_name: str
    url: str
    title: str
    summary: str
    published: str
    league: str | None = None


# ---------- DB helpers ----------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()


# ---------- RSS ----------

def _fetch_text(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _parse_rss(xml_text: str, source: str, source_name: str) -> list[NewsItem]:
    """Parse a (possibly RSS or Atom) feed. Returns a list of NewsItem."""
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    # Strip namespaces for simpler XPath
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    # RSS 2.0 (channel/item) and Atom (feed/entry)
    for entry in list(root.iter("item")) + list(root.iter("entry")):
        title = (entry.findtext("title") or "").strip()
        link_el = entry.find("link")
        link = ""
        if link_el is not None:
            link = (link_el.text or link_el.get("href") or "").strip()
        if not title or not link:
            continue
        summary = (entry.findtext("description") or entry.findtext("summary") or "").strip()
        published = (entry.findtext("pubDate") or entry.findtext("published") or "").strip()
        items.append(NewsItem(
            source=source,
            source_name=source_name,
            url=link,
            title=title,
            summary=summary[:1000],
            published=published,
        ))
    return items


def poll_all_rss() -> dict:
    """Hit every configured feed, dedupe by url_hash, insert new rows. Returns counts."""
    inserted = 0
    fetched = 0
    errors: list[dict] = []
    # Transfer feeds join the rotation only while the transfer window is "open" per the
    # current content mode (summer / international break).
    feeds = dict(RSS_FEEDS)
    if current_mode().transfers_on:
        feeds.update(TRANSFER_FEEDS)
    with _db() as conn:
        for source, meta in feeds.items():
            try:
                xml = _fetch_text(meta["url"])
                items = _parse_rss(xml, source, meta["name"])
                fetched += len(items)
                for it in items:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO news_items
                          (source, source_name, url_hash, url, title, summary, published, league, heat_score)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            it.source, it.source_name, _url_hash(it.url),
                            it.url, it.title, it.summary, it.published,
                            it.league, 0,
                        ),
                    )
                    if cur.rowcount:
                        inserted += 1
            except Exception as e:
                errors.append({"source": source, "error": str(e)[:200]})
        conn.commit()
    return {
        "ok": True,
        "feeds_polled": len(feeds),
        "items_fetched": fetched,
        "new_items": inserted,
        "errors": errors,
    }


# ---------- Reddit r/soccer (heat radar) ----------

def poll_reddit_soccer() -> dict:
    """Pull r/soccer top-hourly JSON; upsert into reddit_radar.
    Reddit aggressively rate-limits / blocks bot UAs. Use a real-browser UA and
    fail soft — heat radar is a nice-to-have, not load-bearing."""
    try:
        # Real-browser UA + Reddit-specific headers reduce 403 rate
        req = urllib.request.Request(
            REDDIT_SOCCER_URL,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124 Safari/537"
                ),
                "Accept": "application/json,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        # Heat radar is optional. Don't block the news pipeline on Reddit blocks.
        return {"ok": False, "error": str(e)[:200], "soft_fail": True}

    posts = (data.get("data") or {}).get("children") or []
    inserted = 0
    with _db() as conn:
        for p in posts:
            d = p.get("data") or {}
            permalink = d.get("permalink") or ""
            if not permalink:
                continue
            cur = conn.execute(
                """
                INSERT INTO reddit_radar (permalink, title, score, num_comments, flair)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(permalink) DO UPDATE SET
                  score = excluded.score,
                  num_comments = excluded.num_comments,
                  ts = datetime('now')
                """,
                (
                    permalink,
                    (d.get("title") or "")[:500],
                    int(d.get("score") or 0),
                    int(d.get("num_comments") or 0),
                    d.get("link_flair_text") or None,
                ),
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
    return {"ok": True, "posts_seen": len(posts), "rows_touched": inserted}


# ---------- Pick a take to draft ----------

def pick_news_for_take(min_age_minutes: int = 5, max_age_hours: int = 24,
                       auto_mark: bool = True) -> dict:
    """Pick the next-best unused news item for a hot take.
    Joins news_items with reddit_radar via fuzzy title match to weight hot stories.

    By default auto-marks the picked item as used (auto_mark=True) so back-to-back
    cron runs can't pick the same story twice. Pass auto_mark=False if the caller
    wants to inspect first and explicitly call mark_news_used later.

    Returns the item or {item: None}."""
    with _db() as conn:
        # Fuzzy heat: any reddit_radar entry whose first 3 words appear in news_item.title
        rows = conn.execute(
            f"""
            SELECT ni.*,
                   COALESCE(MAX(rr.score), 0) AS reddit_heat
              FROM news_items ni
              LEFT JOIN reddit_radar rr
                ON LOWER(ni.title) LIKE '%' || LOWER(SUBSTR(rr.title, 1, 30)) || '%'
             WHERE ni.used = 0
               AND ni.ts >= datetime('now', '-{int(max_age_hours)} hours')
               AND ni.ts <= datetime('now', '-{int(min_age_minutes)} minutes')
             GROUP BY ni.id
             ORDER BY reddit_heat DESC, ni.ts DESC
             LIMIT 1
            """
        ).fetchall()
        if not rows:
            return {"ok": True, "item": None}
        r = dict(rows[0])
        if auto_mark:
            conn.execute("UPDATE news_items SET used = 1 WHERE id = ?", (r["id"],))
            conn.commit()
        # Strip the `used` field from the agent-facing response so the agent doesn't
        # see "used: 1" (it's just the auto-mark side-effect of THIS pick) and bail.
        r.pop("used", None)
    return {"ok": True, "item": r}


def mark_news_used(news_id: int) -> dict:
    with _db() as conn:
        conn.execute("UPDATE news_items SET used = 1 WHERE id = ?", (int(news_id),))
        conn.commit()
    return {"ok": True}


# ---------- CLI ----------

def _cli(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="news_rss")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll-rss", help="poll all RSS feeds, insert new items into news_items")
    sub.add_parser("poll-reddit", help="poll r/soccer top-hourly into reddit_radar")
    sub.add_parser("pick", help="pick next news item for a hot take")
    sub.add_parser("recent", help="show recent news_items")
    args = p.parse_args(argv)

    if args.cmd == "poll-rss":
        print(json.dumps(poll_all_rss(), indent=2))
    elif args.cmd == "poll-reddit":
        print(json.dumps(poll_reddit_soccer(), indent=2))
    elif args.cmd == "pick":
        print(json.dumps(pick_news_for_take(), indent=2, default=str))
    elif args.cmd == "recent":
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, source, title, used FROM news_items ORDER BY ts DESC LIMIT 20"
            ).fetchall()
        for r in rows:
            mark = "✓" if r["used"] else " "
            print(f"  [{mark}] {r['id']:>4} {r['source']:<26} {r['title'][:80]}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
