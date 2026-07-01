#!/usr/bin/env python3
"""
mandem_mcp — narrow MCP server exposing first-class tools to the Mandem FC agent.

Runs as `python3 scripts/mandem_mcp.py` over stdio. Any MCP-capable agent runtime
can spawn it as a child process. Tools wrap the Python modules under
`scripts/mandem/` and the schema in `scripts/mandem_db.py`.

All tools return {"ok": bool, ...}.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))

# These work both locally (loaded via .env) and on a server (via MANDEM_ENV_FILE).
from mandem import _env  # noqa: E402 — env loader (cheap; safe to keep)
from mandem.footy_api import FootballClient  # noqa: E402
from mandem.season_mode import active_competitions, current_mode  # noqa: E402
from mandem.image import ImageBrief, make_image, normalize_to_45  # noqa: E402
from mandem.captions import (  # noqa: E402
    recaption_styled_draft as _recaption_impl,
    resolve_publish_caption,
)
from mandem.importance import (  # noqa: E402
    BIG_TEAMS,
    RIVALRIES,
    importance_for_fulltime,
    importance_for_preview,
)
from mandem import lineup_graphic as lg  # noqa: E402
from mandem import news_image as ni  # noqa: E402
from mandem import news_rss as nr  # noqa: E402
from mandem import player_photo as pp  # noqa: E402
from mandem import stylize_async as sta  # noqa: E402
from mandem import vision_check as vc  # noqa: E402
from mandem import wikimedia as wm  # noqa: E402

DATA_DIR = _env.data_dir()
DB_PATH = DATA_DIR / "db.sqlite"
QUEUE_ROOT = DATA_DIR / "queue"
IMAGES_DIR = DATA_DIR / "images"

mcp = FastMCP("mandem")


# ---------- helpers ----------

def _db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise RuntimeError(
            f"DB not found at {DB_PATH}. Run `python3 scripts/mandem_db.py footy init` first."
        )
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r else None


# ---------- footy event lifecycle ----------

@mcp.tool()
def poll_live_fixtures() -> dict:
    """Hit API-Football, find covered fixtures whose status is FT/AET/PEN, and insert any new ones into ft_events.
    Returns {ok, new_events: int, events: [...]}.
    Use this at the start of every cron-triggered turn before drafting."""
    async def _go() -> dict:
        client = FootballClient()
        comps = active_competitions()
        live = await client.get_live_fixtures()
        finished = [f for f in live if f.league_id in comps and f.is_finished]
        out: list[dict] = []
        for f in finished:
            try:
                events = await client.get_fixture_events(f.id)
            except Exception:
                events = []
            scorers = [
                {
                    "minute": (e.get("time") or {}).get("elapsed"),
                    "name": (e.get("player") or {}).get("name"),
                    "team": (e.get("team") or {}).get("name"),
                    "kind": e.get("detail", "Goal"),
                }
                for e in events
                if e.get("type") == "Goal" and e.get("detail") not in {"Missed Penalty"}
            ]
            reds = [
                {
                    "minute": (e.get("time") or {}).get("elapsed"),
                    "name": (e.get("player") or {}).get("name"),
                    "team": (e.get("team") or {}).get("name"),
                }
                for e in events
                if e.get("type") == "Card" and e.get("detail") in {"Red Card", "Second Yellow card"}
            ]
            importance = importance_for_fulltime(
                home_team=f.home_team.name,
                away_team=f.away_team.name,
                home_score=f.home_score or 0,
                away_score=f.away_score or 0,
                league_name=f.league_name,
                had_red_card=bool(reds),
            )
            out.append({
                "fixture_id": f.id,
                "league": f.league_name,
                "home": f.home_team.name,
                "away": f.away_team.name,
                "score_home": f.home_score or 0,
                "score_away": f.away_score or 0,
                "importance": importance,
                "scorers": scorers,
                "red_cards": reds,
                "ended_at_utc": f.kickoff,
            })
        return {"ok": True, "live_total": len(live), "covered_finished": out}

    try:
        result = asyncio.run(_go())
    except Exception as e:
        return {"ok": False, "error": str(e)}

    inserted = 0
    new_event_rows: list[dict] = []
    with _db() as conn:
        for ev in result["covered_finished"]:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO ft_events
                  (fixture_id, league, home, away, score_home, score_away, importance,
                   scorers_json, red_cards_json, ended_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ev["fixture_id"], ev["league"], ev["home"], ev["away"],
                    ev["score_home"], ev["score_away"], ev["importance"],
                    json.dumps(ev["scorers"]), json.dumps(ev["red_cards"]),
                    ev["ended_at_utc"],
                ),
            )
            if cur.rowcount:
                inserted += 1
                row = conn.execute(
                    "SELECT * FROM ft_events WHERE fixture_id = ?", (ev["fixture_id"],),
                ).fetchone()
                new_event_rows.append(_row(row))
        conn.commit()

    return {
        "ok": True,
        "live_total": result["live_total"],
        "covered_finished": len(result["covered_finished"]),
        "new_events": inserted,
        "events": new_event_rows,
    }


@mcp.tool()
def events_pending(limit: int = 5) -> dict:
    """List ft_events where used=0 (no draft yet). Most recent first.
    Returns {ok, events: [...]}."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM ft_events WHERE used = 0 ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return {"ok": True, "events": [_row(r) for r in rows]}


@mcp.tool()
def get_event(event_id: int) -> dict:
    """Read a single ft_events row."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM ft_events WHERE id = ?", (int(event_id),)).fetchone()
    return {"ok": True, "event": _row(row)} if row else {"ok": False, "error": "not found"}


# ---------- image search tools ----------

@mcp.tool()
def search_news_images_top_n(query: str, n: int = 5, engine: str = "ddg") -> dict:
    """Search image engines, return top N candidates with metadata (NO download).
    The agent reads titles/sources/dimensions and PICKS the most relevant one,
    then calls `download_image_url` with its url. This avoids the
    'top-1-blind' relevance trap.

    `engine`: 'ddg' (no key) or 'brave' (needs BRAVE_API_KEY). 'brave' returns
    [] if key missing — caller should retry with 'ddg'."""
    try:
        results = ni.search_top_n(query, n=n, source=engine)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "engine": engine, "query": query, "results": results}


@mcp.tool()
def download_image_url(url: str, filename_hint: str = "img", referer: str = "") -> dict:
    """Download a specific image URL to local disk. Use after `search_news_images_top_n`
    when you've picked the best match. `referer` defaults to engine origin if blank."""
    try:
        path = ni.download_url(url, IMAGES_DIR, filename_hint=filename_hint, referer=referer or "https://duckduckgo.com/")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(path), "url": url, "image_source": "news:custom"}


@mcp.tool()
def find_player_photo(player_name: str, team_id: int = 0) -> dict:
    """Look up a player's official headshot via API-Football. ALWAYS the right person —
    no DDG search relevance issues. If `team_id` known (from ft_events.scorers_json or
    fixture data), uses the team-squad endpoint. Else falls back to name search.

    Returns {ok, path, player_id, player_name, photo_url, image_source}.

    Use this for FT / goal posts where a specific scorer is named in the caption."""
    try:
        return pp.find(player_name, team_id=team_id, out_dir=IMAGES_DIR)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def assess_image_relevance(image_path: str, expected_subject: str) -> dict:
    """Vision-check: does the image actually show the expected subject?

    `expected_subject`: free-text description, e.g. "Bukayo Saka celebrating an
    Arsenal goal", "Anfield stadium crowd". Uses Gemini 2.5 Flash multimodal
    (~$0.001 per call).

    Returns {ok, verdict: 'yes'|'no'|'unclear', confidence: 0-1, reason}.

    Workflow: after `download_image_url`, call this. If verdict='no', go back
    to `search_news_images_top_n` and pick a different result. If 'unclear',
    use your judgment from the title."""
    try:
        return vc.assess(image_path, expected_subject)
    except Exception as e:
        return {"ok": False, "error": str(e), "verdict": "unclear"}


@mcp.tool()
def generate_brand_image(theme_prompt: str, backend: str = "gpt_image") -> dict:
    """Last-resort fallback — generate a brand-style football image from scratch.
    NO reference photo, just text-to-image. Use when:
      - search returned nothing relevant (vision check kept failing)
      - the caption is generic / abstract (no specific scorer/team to photograph)
      - the post is about an emotional theme not a specific moment

    `backend`: 'gpt_image' (gpt-image-2, ~$0.04) or 'gemini' (gemini-3-pro-image-preview,
    ~$0.04). Outputs a 1024x1024 PNG saved to images/.

    The Mandem house style preamble is auto-prepended (cinematic, muted palette,
    no text overlay — overlay happens later at stylize time)."""
    try:
        result = make_image(
            ImageBrief(prompt=theme_prompt, aspect="1:1"),
            source=backend,
            out_dir=IMAGES_DIR,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "path": str(result.path),
        "backend": result.source,
        "cost_usd": result.cost_usd,
        "image_source": f"gen:{result.source}",
    }


@mcp.tool()
def search_wiki_image(query: str) -> dict:
    """Wikimedia Commons search for CC-licensed images. Captures attribution.
    Returns {ok, path, attribution, license, page_url, image_source='wikimedia'}."""
    try:
        path, top = wm.search_and_download(query, IMAGES_DIR, filename_hint="wiki")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "path": str(path),
        "attribution": top.attribution,
        "license": top.license,
        "page_url": top.page_url,
        "image_source": "wikimedia",
    }


@mcp.tool()
def search_pexels(query: str) -> dict:
    """Free Pexels stock search (good for stadium / crowd shots).
    Returns {ok, path, photographer, pexels_url, image_source='pexels'}."""
    try:
        result = make_image(
            ImageBrief(prompt=query, query=query, aspect="1:1"),
            source="pexels",
            out_dir=IMAGES_DIR,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "path": str(result.path),
        "photographer": result.meta.get("photographer"),
        "pexels_url": result.meta.get("pexels_url"),
        "image_source": "pexels",
    }


# ---------- draft lifecycle ----------

@mcp.tool()
def save_draft(
    caption: str,
    image_path: str,
    image_source: str,
    kind: str = "ft",
    subject_id: int = 0,
    event_id: int = 0,
    image_attribution: str = "",
) -> dict:
    """Insert a post_drafts row in 'pending' status. Kind-aware:
      - kind='ft': pass event_id (the ft_events.id). Marks ft_events.used=1.
      - kind='preview' | 'lineup': pass subject_id = API-Football fixture_id.
      - kind='news': pass subject_id = news_items.id.
      - kind='goal': pass subject_id = ft_events.id (or fixture_id if no row yet).
    Returns {ok, draft_id, kind}."""
    valid_kinds = {"ft", "preview", "lineup", "news", "goal"}
    if kind not in valid_kinds:
        return {"ok": False, "error": f"kind must be one of {sorted(valid_kinds)}"}

    if kind == "ft":
        # Backward compat: subject_id mirrors event_id when only one is passed
        if event_id == 0 and subject_id != 0:
            event_id = subject_id
        if subject_id == 0 and event_id != 0:
            subject_id = event_id
        if event_id == 0:
            return {"ok": False, "error": "kind=ft requires event_id (the ft_events.id)"}
    else:
        if subject_id == 0:
            return {"ok": False, "error": f"kind={kind} requires subject_id"}
        # event_id is sentinel 0 for non-FT (FK not enforced; column is NOT NULL with default integer)

    attr = (image_attribution or "").strip() or None
    with _db() as conn:
        cur = conn.execute(
            """
            INSERT INTO post_drafts
              (event_id, caption, image_path, image_source, status, kind, subject_id, image_attribution)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (int(event_id), caption, image_path, image_source, kind, int(subject_id), attr),
        )
        draft_id = cur.lastrowid
        if kind == "ft":
            conn.execute("UPDATE ft_events SET used = 1 WHERE id = ?", (int(event_id),))
        conn.commit()
    return {"ok": True, "draft_id": draft_id, "kind": kind, "image_attribution": attr}


@mcp.tool()
def get_draft(draft_id: int) -> dict:
    """Read a post_drafts row."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM post_drafts WHERE id = ?", (int(draft_id),)
        ).fetchone()
    return {"ok": True, "draft": _row(row)} if row else {"ok": False, "error": "not found"}


@mcp.tool()
def pending_drafts() -> dict:
    """List drafts in 'pending' status (sent to the operator, awaiting yes/edit/skip)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM post_drafts WHERE status = 'pending' ORDER BY id DESC LIMIT 10"
        ).fetchall()
    return {"ok": True, "drafts": [_row(r) for r in rows]}


@mcp.tool()
def set_draft_status(draft_id: int, status: str, edit_text: str = "") -> dict:
    """Update a draft's status. Valid (agent-settable): pending, approved, edited, skipped,
    published, awaiting_post_confirm. The publish path owns posting/posted/post_unknown — the
    agent never sets those directly.
    For 'edited', pass the rewritten caption in edit_text.
    Returns {ok}."""
    valid = {"pending", "approved", "edited", "skipped", "published",
             "awaiting_post_confirm"}
    if status not in valid:
        return {"ok": False, "error": f"invalid status; want one of {sorted(valid)}"}
    with _db() as conn:
        if status == "edited" and edit_text:
            conn.execute(
                "UPDATE post_drafts SET status = ?, edit_text = ?, approved_at = datetime('now') WHERE id = ?",
                (status, edit_text, int(draft_id)),
            )
        elif status == "approved":
            conn.execute(
                "UPDATE post_drafts SET status = ?, approved_at = datetime('now') WHERE id = ?",
                (status, int(draft_id)),
            )
        else:
            conn.execute(
                "UPDATE post_drafts SET status = ? WHERE id = ?",
                (status, int(draft_id)),
            )
        conn.commit()
    return {"ok": True}


# ---------- Telegram outbound (sends approval DMs via Telegram Bot API) ----------

@mcp.tool()
def send_draft_dm(draft_id: int) -> dict:
    """Send the draft (photo + caption + 'reply yes/edit/skip' tail) to the operator.
    Persists tg_message_id on the draft row. Returns {ok, telegram_message_id}."""
    _env.load()
    with _db() as conn:
        draft = conn.execute(
            "SELECT * FROM post_drafts WHERE id = ?", (int(draft_id),)
        ).fetchone()
    if not draft:
        return {"ok": False, "error": "draft not found"}
    if not draft["image_path"] or not os.path.exists(draft["image_path"]):
        return {"ok": False, "error": f"image not found at {draft['image_path']!r}"}

    body = draft["caption"]
    # Surface CC attribution in the approval DM so the operator sees what license
    # constraints the final caption will inherit. Stays out of the published caption
    # unless it's legally required (handled in stylize_async).
    attr = draft["image_attribution"] if "image_attribution" in draft.keys() else None
    if attr:
        body = f"{body}\n\n[meta] {attr}"
    body = (
        body
        + "\n\n— reply `yes` to publish · `edit: <new caption>` to rewrite · `skip` to drop"
    )

    try:
        # Use send_text + sendPhoto separately would split the message; use the existing
        # helper that does sendPhoto with caption (no inline keyboard).
        from mandem.telegram import send_photo
        chat_id = int(_env.require("MJ_MANDEM_CHAT_ID"))
        resp = send_photo(draft["image_path"], body, chat_id=chat_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    msg_id = resp["result"]["message_id"]
    chat_id = resp["result"]["chat"]["id"]
    with _db() as conn:
        conn.execute(
            "UPDATE post_drafts SET tg_chat_id = ?, tg_message_id = ? WHERE id = ?",
            (str(chat_id), int(msg_id), int(draft_id)),
        )
        conn.commit()
    return {"ok": True, "telegram_message_id": msg_id, "telegram_chat_id": chat_id}


@mcp.tool()
def send_text_message(text: str) -> dict:
    """Send a plain-text message to the operator. Use sparingly - for confirmations/errors only."""
    from mandem.telegram import send_text
    try:
        resp = send_text(text)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "telegram_message_id": resp["result"]["message_id"]}


# ---------- stylize + publish ----------

@mcp.tool()
def stylize_image(draft_id: int, headline_color: str = "orange") -> dict:
    """Kick off the AI stylise ASYNCHRONOUSLY (Seedream v4 via fal). Returns a job_id
    in <1s; the heavy work (~30-60s) runs in a worker thread so the MCP RPC doesn't
    time out. Edits the REAL approved photo into the Mandem graphic (orange bold
    headline up top, shadowed real background + glow, player in focus, 4:5).

    headline_color — MIX BY MOMENT: pass **"orange"** (default, everyday/most posts),
    **"gold"** for legacy / huge / record / GOAT moments, **"white"** for plain
    everyday takes. The brain chooses based on the story.

    Returns {ok, job_id, draft_id, status: 'queued', started: bool, poll_in_seconds}.

    Next step: call `check_stylize(job_id)` — it polls internally up to ~50s
    and returns the latest job row. Re-call until status is 'done' or 'failed'.
    On 'done' the post_drafts row is already flipped to 'published' with
    queued_path + styled_path set; call `deliver_styled_preview(draft_id)` to
    DM the operator the finished graphic.

    If a job is already in flight for this draft, returns its existing job_id
    (started=False) instead of double-spending."""
    with _db() as conn:
        draft = conn.execute(
            "SELECT id FROM post_drafts WHERE id = ?", (int(draft_id),)
        ).fetchone()
    if not draft:
        return {"ok": False, "error": "draft not found"}
    color = (headline_color or "orange").strip().lower()
    if color not in ("orange", "gold", "white"):
        color = "orange"
    return sta.submit_job(int(draft_id), headline_color=color)


@mcp.tool()
def check_stylize(job_id: str, wait_seconds: int = 50) -> dict:
    """Check (and optionally wait for) an async stylize job. Polls internally
    up to wait_seconds (default 50, capped under MCP RPC timeout) — returns
    immediately if the job already finished. Re-call if the returned status is
    still 'running' / 'queued'.

    Returns {ok, job: {job_id, draft_id, status, queue_dir, styled_path,
                       backend, cost_usd, error, started_at, finished_at}}.

    status: 'queued' | 'running' | 'done' | 'failed'
      - done   → safe to call deliver_styled_preview(draft_id)
      - failed → check `error`; usually retry once via stylize_image, else skip
      - others → call check_stylize again"""
    return sta.wait_for_terminal(job_id, max_wait_seconds=int(wait_seconds))


@mcp.tool()
def publish_raw_to_queue(draft_id: int) -> dict:
    """For drafts where the raw image IS the final (lineup posts, manual stub).
    Skips AI stylize; just copies image + caption.md to queue dir, marks published.
    Returns {ok, queue_dir, image_path}."""
    import shutil
    with _db() as conn:
        draft = conn.execute("SELECT * FROM post_drafts WHERE id = ?", (int(draft_id),)).fetchone()
    if not draft:
        return {"ok": False, "error": "draft not found"}
    raw = Path(draft["image_path"])
    if not raw.exists():
        return {"ok": False, "error": f"raw image missing: {raw}"}
    queue_dir = QUEUE_ROOT / time.strftime("%Y-%m-%d-%H%M%S")
    queue_dir.mkdir(parents=True, exist_ok=True)
    final_image = queue_dir / f"image{raw.suffix}"
    shutil.copyfile(raw, final_image)
    # Force IG-native 4:5 for photo passthroughs — but NOT the pre-designed lineup
    # graphic (a deliberate 1080x1080 layout; a 4:5 crop would clip the XI columns).
    # Gate on the validated `kind` column, not the brain-supplied image_source string.
    kind = (draft["kind"] or "").lower() if "kind" in draft.keys() else ""
    if kind != "lineup":
        try:
            final_image = normalize_to_45(final_image)
        except Exception:
            pass
    final_caption = resolve_publish_caption(dict(draft), draft["edit_text"] or draft["caption"])
    (queue_dir / "caption.md").write_text(final_caption, encoding="utf-8")
    (queue_dir / "meta.json").write_text(json.dumps({
        "draft_id": draft_id, "kind": draft["kind"] if "kind" in draft.keys() else "ft",
        "mode": "raw_passthrough", "raw_image_path": str(raw),
    }, indent=2), encoding="utf-8")
    with _db() as conn:
        conn.execute(
            "UPDATE post_drafts SET status = 'published', queued_path = ?, styled_path = ?, error = NULL WHERE id = ?",
            (str(queue_dir), str(final_image), int(draft_id)),
        )
        conn.commit()
    return {"ok": True, "queue_dir": str(queue_dir), "image_path": str(final_image)}


@mcp.tool()
def deliver_styled_preview(draft_id: int) -> dict:
    """After stylize_image succeeds, DM the styled graphic as a preview.
    Useful so the operator sees the final result before manually uploading to IG.
    Returns {ok, telegram_message_id}."""
    from mandem.telegram import send_photo
    with _db() as conn:
        draft = conn.execute(
            "SELECT * FROM post_drafts WHERE id = ?", (int(draft_id),)
        ).fetchone()
    if not draft or not draft["styled_path"]:
        return {"ok": False, "error": "draft not styled yet (call stylize_image first)"}
    _env.load()
    chat_id = int(_env.require("MJ_MANDEM_CHAT_ID"))
    body = (
        f"✨ Queued: {draft['queued_path']}\n"
        f"Backend: {draft['image_source']}"
    )
    try:
        resp = send_photo(draft["styled_path"], body, chat_id=chat_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "telegram_message_id": resp["result"]["message_id"]}


@mcp.tool()
def recaption_styled_draft(draft_id: int, new_caption: str) -> dict:
    """Change ONLY the caption on an ALREADY-STYLIZED draft, reusing the approved
    image as-is. Use this - NOT stylize_image - whenever the operator has already approved the
    styled graphic and now wants different caption text.

    Re-running stylize_image would generate a brand-new image (the overlay word is
    baked in by gpt-image-2 and the gen is non-deterministic), swapping out the
    picture the operator approved. This rewrites the queued caption.md + edit_text and leaves
    the image, styled_path, queued_path and status untouched, so the next
    publish_to_instagram ships the SAME approved image with the new caption.

    Only call stylize_image again if the operator explicitly asks for a NEW image or a
    different overlay word. Returns {ok, caption, queued_path}."""
    return _recaption_impl(int(draft_id), new_caption, db_path=DB_PATH)


# ---------- Instagram publish (the FINAL step of the two-step confirm) ----------

def _set_status(draft_id: int, status: str, error: str | None = None) -> None:
    """Internal: set a draft's status (+ optional error). Used by the publish path to
    release/normalise the row on failure."""
    with _db() as conn:
        conn.execute(
            "UPDATE post_drafts SET status = ?, error = ? WHERE id = ?",
            (status, error, int(draft_id)),
        )
        conn.commit()


@mcp.tool()
def publish_to_instagram(draft_id: int) -> dict:
    """Publish a stylized draft to Instagram (@mandemfchq). FINAL step of the two-step
    confirm: only call after deliver_styled_preview showed the operator the graphic AND they replied
    `post` / `send it`.

    Safety invariants (code-enforced, not just convention):
      - Idempotent — a draft already 'posted' (or carrying an ig_media_id) is never re-posted.
      - Gated — only a draft that reached Gate 2 ('awaiting_post_confirm', or the auto-set
        'published') can publish; a stray call on a pending/skipped draft is rejected.
      - Atomic claim — the row is flipped to 'posting' in one UPDATE before any network work,
        so a repeated `post` reply (or an agent retry) can't double-publish.
      - Uncertain outcome — if media_publish is sent but the response is lost, the draft is
        marked 'post_unknown' (NOT auto-retryable); a human reconciles so we never blind-retry
        a maybe-live post.

    Returns {ok, media_id, permalink} on success; {ok: False, error, ...} otherwise."""
    from mandem import instagram as ig
    from mandem import imagehost as ih

    # Idempotent atomic claim: exactly one caller may move a Gate-2 draft into 'posting'.
    with _db() as conn:
        cur = conn.execute(
            "UPDATE post_drafts SET status = 'posting' "
            "WHERE id = ? AND status IN ('awaiting_post_confirm', 'published') "
            "AND ig_media_id IS NULL",
            (int(draft_id),),
        )
        conn.commit()
        claimed = cur.rowcount == 1

    with _db() as conn:
        draft = conn.execute(
            "SELECT * FROM post_drafts WHERE id = ?", (int(draft_id),)
        ).fetchone()
    if not draft:
        return {"ok": False, "error": "draft not found"}

    if not claimed:
        status = draft["status"]
        if status == "posted" or draft["ig_media_id"]:
            return {"ok": False, "error": "already posted",
                    "media_id": draft["ig_media_id"], "permalink": draft["ig_permalink"]}
        if status == "posting":
            return {"ok": False, "error": "a publish is already in progress for this draft"}
        if status == "post_unknown":
            return {"ok": False, "error": "previous publish outcome is UNKNOWN — check "
                    "@mandemfchq in the IG app before retrying; once reconciled, "
                    "set_draft_status back to awaiting_post_confirm (retry) or skipped (it's live)."}
        return {"ok": False, "error": f"draft not ready to post (status={status!r}); "
                "it must clear Gate 2 (reply `post` after the styled preview)."}

    styled = draft["styled_path"]
    if not styled or not os.path.exists(styled):
        _set_status(draft_id, "awaiting_post_confirm", error="not stylized (no styled_path)")
        return {"ok": False, "error": "draft not stylized yet (no styled_path) — stylize first"}

    # Defensive: guarantee 4:5 + IG-safe JPEG at publish time too. A draft stylized
    # BEFORE the normalize fix shipped still points at a square/wrong-format file; this
    # heals it on the way out. Skip lineup graphics (square 1080x1080 by design).
    # normalize may change the extension (.png/.webp → .jpg) and delete the original,
    # so persist the new path or a failed-publish retry would hit a missing file.
    kind = (draft["kind"] or "").lower() if "kind" in draft.keys() else ""
    if kind != "lineup":
        try:
            new_styled = str(normalize_to_45(styled))
            if new_styled != styled:
                styled = new_styled
                with _db() as conn:
                    conn.execute("UPDATE post_drafts SET styled_path = ? WHERE id = ?",
                                 (styled, int(draft_id)))
                    conn.commit()
        except Exception:
            pass  # never block a publish on a resize; upload whatever we have

    # Final caption. The queued caption.md is authoritative (carries CC attribution appended
    # at stylize time). If we can't read it, re-append attribution ourselves so a CC image
    # never publishes without its legally-required credit.
    caption = None
    qd = draft["queued_path"] if "queued_path" in draft.keys() else None
    if qd:
        cap_file = Path(qd) / "caption.md"
        if cap_file.exists():
            try:
                caption = cap_file.read_text(encoding="utf-8").strip()
            except OSError:
                caption = None
    if not caption:
        caption = resolve_publish_caption(dict(draft), draft["edit_text"] or draft["caption"])
    if not caption:
        _set_status(draft_id, "awaiting_post_confirm", error="empty caption — refused blank post")
        return {"ok": False, "error": "resolved caption is empty — refusing to publish a blank post"}

    # Host the styled image at a public URL for IG to fetch.
    try:
        hosted = ih.upload_public(styled)
    except Exception as e:  # imagehost already sanitizes its message (no creds in text)
        _set_status(draft_id, "awaiting_post_confirm", error=f"image hosting failed: {e}")
        return {"ok": False, "error": f"image hosting failed: {e}"}

    # Publish (phase-aware), then ALWAYS clean up the temp upload.
    try:
        result = ig.publish_image(hosted["url"], caption)
    except ig.PublishUncertain as e:
        ih.delete(hosted.get("key", ""))
        _set_status(draft_id, "post_unknown", error=f"publish outcome UNKNOWN: {e}")
        return {"ok": False, "uncertain": True,
                "error": "publish outcome UNKNOWN (response lost). The post MAY already be "
                         "live — check @mandemfchq before any retry."}
    except Exception as e:
        ih.delete(hosted.get("key", ""))
        _set_status(draft_id, "awaiting_post_confirm", error=f"ig publish failed: {e}")
        return {"ok": False, "error": f"ig publish failed: {e}"}
    ih.delete(hosted.get("key", ""))

    with _db() as conn:
        conn.execute(
            "UPDATE post_drafts SET status = 'posted', ig_media_id = ?, ig_permalink = ?, "
            "posted_at = datetime('now'), error = NULL WHERE id = ?",
            (result.get("media_id"), result.get("permalink"), int(draft_id)),
        )
        conn.commit()
    return {"ok": True, "media_id": result.get("media_id"), "permalink": result.get("permalink")}


@mcp.tool()
def refresh_ig_token() -> dict:
    """Refresh the long-lived Instagram token (resets its 60-day clock). Safe to run any
    time; intended for a ~50-day cron so the token never lapses. Returns {ok, expires_days}."""
    from mandem import instagram as ig
    try:
        return {"ok": True, **ig.refresh_token()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def ig_whoami() -> dict:
    """Diagnostics: confirm the Instagram token works. Returns {ok, user_id, username}."""
    from mandem import instagram as ig
    try:
        return {"ok": True, **ig.whoami()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
# Phase A — News (RSS + Reddit)
# ============================================================================

@mcp.tool()
def poll_rss() -> dict:
    """Poll all configured football RSS feeds (BBC, Guardian, ESPN, Sky), insert new
    items into news_items deduped by url-hash. Returns counts. Call from the news cron."""
    return nr.poll_all_rss()


@mcp.tool()
def poll_reddit_soccer() -> dict:
    """Poll r/soccer top-hourly into reddit_radar (heat signal for RSS items). Call
    alongside poll_rss to keep the heat signal fresh."""
    return nr.poll_reddit_soccer()


@mcp.tool()
def pick_news_for_take(min_age_minutes: int = 5, max_age_hours: int = 24) -> dict:
    """Pick the next-best unused news item for a hot take. Joins news_items with
    reddit_radar to weight stories by social heat. Returns the item or {item: None}."""
    return nr.pick_news_for_take(min_age_minutes=min_age_minutes, max_age_hours=max_age_hours)


@mcp.tool()
def mark_news_used(news_id: int) -> dict:
    """Mark a news_items row as used after drafting a take from it (so we don't re-pick)."""
    return nr.mark_news_used(news_id)


# ============================================================================
# Phase B — Pre-match preview
# ============================================================================

import asyncio as _asyncio  # noqa: E402 (re-import alias)
from datetime import date as _date, datetime as _dt  # noqa: E402


@mcp.tool()
def get_today_covered_fixtures() -> dict:
    """Return today's fixtures across the 6 covered leagues (PL, UCL, La Liga, Serie A,
    Bundesliga, Ligue 1), sorted by kickoff. Each with home/away/league/kickoff/status.
    Uses 1 API-Football request per league = ~6 reqs."""
    async def _go():
        client = FootballClient()
        today = _date.today().isoformat()
        out = []
        for league_id, league_name in active_competitions().items():
            try:
                rs = await client.get_upcoming_fixtures(league_id, next_n=10)
            except Exception:
                continue
            for f in rs:
                # Filter to "today" by kickoff date
                if (f.kickoff or "").startswith(today):
                    out.append({
                        "fixture_id": f.id,
                        "league": f.league_name,
                        "league_id": f.league_id,
                        "home": f.home_team.name,
                        "home_id": f.home_team.id,
                        "away": f.away_team.name,
                        "away_id": f.away_team.id,
                        "kickoff": f.kickoff,
                        "status": f.status,
                    })
        out.sort(key=lambda x: x["kickoff"])
        return out
    try:
        fixtures = _asyncio.run(_go())
    except Exception as e:
        return {"ok": False, "error": str(e)}
    for f in fixtures:
        f["preview_importance"] = importance_for_preview(f["home"], f["away"], f["league"])
    fixtures.sort(key=lambda x: -x["preview_importance"])
    return {"ok": True, "fixtures": fixtures}


@mcp.tool()
def get_team_form(team_id: int, last: int = 5) -> dict:
    """Last N completed fixtures for a team (form line). Returns simplified rows
    with W/D/L outcome + score + opponent."""
    async def _go():
        from mandem.footy_api import BASE_URL  # noqa: F401  # base URL constant
        import httpx
        from mandem.footy_api import FootballClient as FC
        c = FC()
        async with httpx.AsyncClient(timeout=20.0) as cli:
            r = await cli.get(
                "https://v3.football.api-sports.io/fixtures",
                headers=c._headers,
                params={"team": team_id, "last": last},
            )
            r.raise_for_status()
            data = r.json().get("response", [])
        out = []
        for f in data:
            home = f["teams"]["home"]
            away = f["teams"]["away"]
            hg, ag = f["goals"]["home"], f["goals"]["away"]
            if hg is None or ag is None:
                continue
            is_home = home["id"] == team_id
            us, them = (hg, ag) if is_home else (ag, hg)
            opp = away["name"] if is_home else home["name"]
            outcome = "W" if us > them else ("D" if us == them else "L")
            out.append({
                "date": f["fixture"]["date"][:10],
                "league": f["league"]["name"],
                "opp": opp,
                "venue": "H" if is_home else "A",
                "score": f"{us}-{them}",
                "outcome": outcome,
            })
        return out
    try:
        return {"ok": True, "form": _asyncio.run(_go())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
# Phase C — Lineup post (T-60min before KO)
# ============================================================================

@mcp.tool()
def check_imminent_lineups(window_min_minutes: int = 50, window_max_minutes: int = 75) -> dict:
    """Find covered fixtures with kickoff between [now+min, now+max] minutes.
    For each, fetch /fixtures/lineups and return any with lineups available.
    Use to trigger lineup posts. Also returns 'too_early' fixtures (kickoff > max_min)
    so the agent can stay quiet without further polling."""
    async def _go():
        client = FootballClient()
        # Pull next-N upcoming for each covered league
        candidates = []
        for league_id in active_competitions():
            try:
                rs = await client.get_upcoming_fixtures(league_id, next_n=5)
            except Exception:
                continue
            for f in rs:
                # Parse kickoff to UTC datetime
                try:
                    ko = _dt.fromisoformat(f.kickoff.replace("Z", "+00:00"))
                except Exception:
                    continue
                now_utc = _dt.now(ko.tzinfo)
                delta_min = (ko - now_utc).total_seconds() / 60.0
                candidates.append({"fixture": f, "delta_min": delta_min})
        # In window
        in_window = [c for c in candidates if window_min_minutes <= c["delta_min"] <= window_max_minutes]
        out = []
        for c in in_window:
            f = c["fixture"]
            try:
                lineups = await client.get_lineups(f.id)
            except Exception:
                lineups = []
            if lineups:
                out.append({
                    "fixture_id": f.id,
                    "league": f.league_name,
                    "home": f.home_team.name,
                    "away": f.away_team.name,
                    "kickoff": f.kickoff,
                    "delta_min": round(c["delta_min"], 1),
                    "lineups": lineups,
                })
        too_early = [{"home": c["fixture"].home_team.name, "away": c["fixture"].away_team.name,
                      "delta_min": round(c["delta_min"], 1)} for c in candidates if c["delta_min"] > window_max_minutes]
        return {"ready": out, "too_early": too_early[:5]}
    try:
        return {"ok": True, **_asyncio.run(_go())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def render_lineup_graphic(fixture_id: int, lineups: list) -> dict:
    """Render a 1080x1080 lineup graphic (Pillow) showing both XIs.
    `lineups` is the array returned by check_imminent_lineups (API-Football shape).
    Saves to the configured Mandem image directory."""
    try:
        path = lg.render(fixture_id=fixture_id, lineups=lineups, out_dir=IMAGES_DIR)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(path)}


# ============================================================================
# Phase E — Goal reactions (DISABLED until API-Football Pro)
# ============================================================================

@mcp.tool()
def poll_live_goals_for_covered() -> dict:
    """For Pro tier ONLY. Walk live covered fixtures, fetch their events, return any
    NEW goals (not yet drafted). On free tier this will burn quota fast — keep cron
    disabled until upgraded. Dedup is based on (fixture_id, minute, scorer) tuple."""
    async def _go():
        client = FootballClient()
        comps = active_competitions()
        live = await client.get_live_fixtures()
        covered_live = [f for f in live if f.league_id in comps and not f.is_finished]
        out = []
        for f in covered_live:
            try:
                events = await client.get_fixture_events(f.id)
            except Exception:
                continue
            for e in events:
                if e.get("type") != "Goal" or e.get("detail") in {"Missed Penalty"}:
                    continue
                out.append({
                    "fixture_id": f.id,
                    "league": f.league_name,
                    "home": f.home_team.name,
                    "away": f.away_team.name,
                    "score_home": f.home_score or 0,
                    "score_away": f.away_score or 0,
                    "minute": (e.get("time") or {}).get("elapsed"),
                    "scorer": (e.get("player") or {}).get("name"),
                    "team": (e.get("team") or {}).get("name"),
                    "detail": e.get("detail"),
                })
        return out
    try:
        return {"ok": True, "goals": _asyncio.run(_go())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- read-only db query (mirrors mira's pattern) ----------

@mcp.tool()
def query_db(sql: str) -> dict:
    """Run a read-only SELECT on db.sqlite. Rejects any non-SELECT.
    Useful when you need to look up specific rows the typed tools don't cover."""
    sql_l = sql.lstrip().lower()
    if not sql_l.startswith("select"):
        return {"ok": False, "error": "only SELECT allowed"}
    with _db() as conn:
        rows = conn.execute(sql).fetchall()
    return {"ok": True, "rows": [_row(r) for r in rows]}


@mcp.tool()
def current_content_mode() -> dict:
    """What football is live right now. Returns the active content mode:
    {mode, transfers_on, competitions, voice}. In summer/international-break windows
    'mode' is e.g. 'summer-2026', competitions = the tournament (World Cup), and
    transfers_on flags that the transfer window is open. Otherwise 'club'.
    Use the 'voice' note to set the situational frame before drafting."""
    m = current_mode()
    return {
        "ok": True,
        "mode": m.name,
        "transfers_on": m.transfers_on,
        "competitions": [{"id": k, "name": v} for k, v in m.competitions.items()],
        "voice": m.voice,
    }


# ---------- MCP Resources (static reference data the agent can read without a tool call) ----------

@mcp.resource("mandem://leagues/covered")
def res_covered_leagues() -> str:
    """The competitions Mandem covers RIGHT NOW (mode-aware: club season vs a summer
    tournament window). Includes the active mode name so the agent knows the frame."""
    mode = current_mode()
    return json.dumps(
        {
            "mode": mode.name,
            "transfers_on": mode.transfers_on,
            "competitions": [{"id": k, "name": v} for k, v in mode.competitions.items()],
        },
        indent=2,
    )


@mcp.resource("mandem://teams/big")
def res_big_teams() -> str:
    """Teams treated as 'big' for importance scoring. Single source of truth — used by
    importance_for_fulltime() and importance_for_preview()."""
    return json.dumps(sorted(BIG_TEAMS), indent=2)


@mcp.resource("mandem://teams/rivalries")
def res_rivalries() -> str:
    """Rivalry pairs that bump importance by +2. Set order is irrelevant ({A,B} == {B,A})."""
    return json.dumps([sorted(list(r)) for r in RIVALRIES], indent=2)


# ---------- MCP Prompts (machine-readable mirror of the voice contract in AGENTS.md) ----------

@mcp.prompt()
def banter_guidelines() -> str:
    """Mandem FC voice rules. Load at the start of any drafting turn."""
    return (
        "Mandem FC voice — load-bearing, override nothing:\n"
        "- Banter the MOMENT, not the MAN. Rip the event, not the player personally.\n"
        "- Bleacher Report headline energy, NOT Twitter pile-on.\n"
        "- Caps OK when the moment earns it. Don't shout for shouting's sake.\n"
        "- OK drama words: SCENES, BIBLICAL, BOTTLED, COOKED, MASTERCLASS, CHAOS.\n"
        "- NEVER: FRAUD, EMBARRASSMENT, DISGRACE, PATHETIC, RUINED, DESTROYED — "
        "these punch down on individual players. Hard banlist.\n"
        "- Football ONLY. No race, religion, or personal stuff. Strict line.\n"
        "- Calibrate to importance (1-10). A 9 derby winner gets full chaos; a 3 "
        "dead-rubber gets one casual line.\n"
        "- Repetition discipline — same nickname/joke max 1 in 3 posts. "
        "query_db recent captions to check.\n"
        "- Captions: no outlet credits ('— via Sky Sports'). CC image attribution only "
        "when legally required (handled automatically for wikimedia/pexels sources)."
    )


@mcp.prompt()
def when_not_to_post() -> str:
    """Filter rules — what doesn't earn a Mandem post."""
    return (
        "Don't post when:\n"
        "- It's generic transfer noise with no source ('player X linked with club Y').\n"
        "- It's a table summary ('Arsenal still top'). State of the table isn't news.\n"
        "- It's a derby with no actual talking point (a 1-1 with no narrative — skip).\n"
        "- It's an injury claim without a credible source. Use 'reportedly' or skip.\n"
        "- The image options are all weak (memes, stock photos with wrong subject). "
        "A bad photo kills the post — better to stand down.\n"
        "- The story would force a personal attack to land. If the only joke crosses "
        "into FRAUD/DISGRACE territory, don't post.\n"
        "- It's already been ranted about in the last 3 drafts. Repetition kills the brand.\n"
        "When in doubt, reply 'nothing earned a take. standing down.' The operator trusts a quiet "
        "agent more than a noisy one."
    )


if __name__ == "__main__":
    # Orphan any stylize jobs left in 'queued'/'running' from a prior process —
    # their worker threads died with the previous mcp instance.
    try:
        recovered = sta.recover_orphans()
        if recovered:
            print(f"[mandem_mcp] orphaned {recovered} stale stylize job(s)", file=sys.stderr)
    except Exception as e:
        print(f"[mandem_mcp] orphan recovery skipped: {e}", file=sys.stderr)
    # A draft left in 'posting' means a publish was interrupted by the restart — the
    # media_publish outcome is genuinely uncertain, so route it to the human-reconcile
    # path ('post_unknown') rather than leaving it invisible/un-postable.
    try:
        with _db() as conn:
            cur = conn.execute(
                "UPDATE post_drafts SET status = 'post_unknown', "
                "error = 'publish interrupted by restart — outcome unknown, check @mandemfchq' "
                "WHERE status = 'posting'"
            )
            conn.commit()
            if cur.rowcount:
                print(f"[mandem_mcp] {cur.rowcount} interrupted publish(es) -> post_unknown", file=sys.stderr)
    except Exception as e:
        print(f"[mandem_mcp] posting-recovery skipped: {e}", file=sys.stderr)
    mcp.run()
