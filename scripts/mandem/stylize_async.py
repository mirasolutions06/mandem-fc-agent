#!/usr/bin/env python3
# scripts/mandem/stylize_async.py
# Async wrapper around stylize.stylize_for_publish so the MCP tool returns
# in <1s with a job_id and a background worker handles the 60-90s gen latency.
# The previous synchronous tool blew through the agent runtime's MCP RPC timeout
# during gpt-image-2 ref-edits.
#
# Job lifecycle:
#   queued  → row inserted; submitted to ThreadPoolExecutor
#   running → worker started; updates timestamps
#   done    → queue_dir/styled_path/backend filled; post_drafts marked published
#   failed  → error filled; post_drafts.error set
#
# State lives in db.sqlite::stylize_jobs. The MCP server is a long-running
# Python process; threads spawned from tool handlers persist until the gen
# completes or the process restarts. On restart, `recover_orphans()` flips
# any leftover queued/running rows to failed.

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import _env
from . import stylize as _st
from .captions import resolve_publish_caption

DB_PATH = _env.data_dir() / "db.sqlite"

# Cap concurrency: image gen is expensive ($0.04 each) and we don't want fan-out.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stylize")
_executor_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r else None


def recover_orphans() -> int:
    """Mark any 'queued' or 'running' jobs as failed. Call once at MCP startup —
    threads from a previous process don't survive a restart, so those rows
    would otherwise sit in 'running' forever and confuse the agent."""
    with _db() as conn:
        cur = conn.execute(
            """
            UPDATE stylize_jobs
            SET status = 'failed',
                finished_at = datetime('now'),
                error = COALESCE(error, '') || 'orphaned by mcp restart'
            WHERE status IN ('queued', 'running')
            """
        )
        conn.commit()
        return cur.rowcount


def get_job(job_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM stylize_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    return _row(row)


def get_active_job_for_draft(draft_id: int) -> dict | None:
    """Return the most-recent non-terminal job for a draft, if any.
    Used to short-circuit duplicate stylize calls."""
    with _db() as conn:
        row = conn.execute(
            """
            SELECT * FROM stylize_jobs
            WHERE draft_id = ? AND status IN ('queued', 'running')
            ORDER BY started_at DESC LIMIT 1
            """,
            (int(draft_id),),
        ).fetchone()
    return _row(row)


def submit_job(draft_id: int, headline_color: str = "orange") -> dict:
    """Insert a stylize_jobs row, submit the worker, return the job receipt.
    Returns immediately (<1s). Caller polls with `get_job(job_id)`.
    headline_color: Seedream overlay colour (mix-by-moment — orange default /
    gold for legacy moments / white for everyday)."""
    # Short-circuit if a job is already in flight for this draft
    active = get_active_job_for_draft(draft_id)
    if active:
        return {
            "ok": True,
            "job_id": active["job_id"],
            "draft_id": draft_id,
            "status": active["status"],
            "started": False,
            "note": "existing job already in flight",
        }

    job_id = uuid.uuid4().hex
    with _db() as conn:
        conn.execute(
            "INSERT INTO stylize_jobs (job_id, draft_id, status) VALUES (?, ?, 'queued')",
            (job_id, int(draft_id)),
        )
        conn.commit()

    # Submit under a lock — keeps executor.submit + the queued→running update
    # atomic so the worker can't race ahead of the row.
    with _executor_lock:
        _executor.submit(_run_job, job_id, int(draft_id), headline_color)

    return {
        "ok": True,
        "job_id": job_id,
        "draft_id": draft_id,
        "status": "queued",
        "started": True,
        "poll_in_seconds": 30,
    }


def _set_status(job_id: str, status: str, **fields: Any) -> None:
    sets = ["status = ?"]
    args: list[Any] = [status]
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        args.append(v)
    args.append(job_id)
    with _db() as conn:
        conn.execute(
            f"UPDATE stylize_jobs SET {', '.join(sets)} WHERE job_id = ?", args
        )
        conn.commit()


def _read_draft_and_event(draft_id: int) -> tuple[dict | None, dict | None]:
    with _db() as conn:
        draft = conn.execute(
            "SELECT * FROM post_drafts WHERE id = ?", (int(draft_id),)
        ).fetchone()
        ev = None
        if draft is not None:
            ev_row = conn.execute(
                "SELECT * FROM ft_events WHERE id = ?", (draft["event_id"],)
            ).fetchone()
            ev = _row(ev_row)
    return _row(draft), ev


def _build_article_context(draft: dict) -> str:
    """For news drafts, fetch the originating news_items row so the overlay
    generator grounds in the story (title + summary) — not just the agent's
    caption. Returns "" for non-news drafts or when the lookup fails."""
    kind = draft.get("kind") or ""
    subject_id = draft.get("subject_id") or 0
    if kind != "news" or not subject_id:
        return ""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT title, summary, source_name FROM news_items WHERE id = ?",
                (int(subject_id),),
            ).fetchone()
    except Exception:
        return ""
    if not row:
        return ""
    parts = [f"News article — title: {row['title']}"]
    if row["source_name"]:
        parts.append(f"Source: {row['source_name']}")
    if row["summary"]:
        parts.append(f"Summary: {row['summary']}")
    return "\n".join(parts)


def _run_job(job_id: str, draft_id: int, headline_color: str = "orange") -> None:
    """Worker — runs in a ThreadPoolExecutor thread. Blocks for 60-90s on gen.
    Must NEVER raise out — every exit path updates the job row."""
    _set_status(job_id, "running")
    try:
        draft, ev = _read_draft_and_event(draft_id)
        if not draft:
            _set_status(
                job_id,
                "failed",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                error="draft not found",
            )
            return

        # CC-licensed sources (Wikimedia / Pexels) legally require attribution in the
        # public caption; the no-outlet-credits rule is waived only for those. The
        # carve-out lives in one place now: captions.resolve_publish_caption.
        final_caption = resolve_publish_caption(draft, draft["edit_text"] or draft["caption"])
        event_summary = (
            f"{ev['home']} {ev['score_home']}-{ev['score_away']} {ev['away']} "
            f"({ev['league']}, importance {ev['importance']}/10)"
            if ev else f"draft #{draft_id}"
        )
        article_context = _build_article_context(draft)

        result = _st.stylize_for_publish(
            draft_id=int(draft_id),
            raw_image_path=draft["image_path"],
            final_caption=final_caption,
            event_summary=event_summary,
            article_context=article_context,
            headline_color=headline_color,
        )

        # Update job row + flip the draft to published in the same logical step
        with _db() as conn:
            conn.execute(
                """
                UPDATE stylize_jobs
                SET status = 'done',
                    finished_at = datetime('now'),
                    queue_dir = ?,
                    styled_path = ?,
                    caption_path = ?,
                    backend = ?,
                    cost_usd = ?,
                    error = NULL
                WHERE job_id = ?
                """,
                (
                    str(result.queue_dir),
                    str(result.image_path),
                    str(result.caption_path),
                    result.backend,
                    float(result.cost_usd or 0.0),
                    job_id,
                ),
            )
            conn.execute(
                """
                UPDATE post_drafts
                SET status = 'published',
                    queued_path = ?,
                    styled_path = ?,
                    error = NULL
                WHERE id = ?
                """,
                (str(result.queue_dir), str(result.image_path), int(draft_id)),
            )
            conn.commit()
    except Exception as e:
        err = str(e)[:600]
        try:
            with _db() as conn:
                conn.execute(
                    """
                    UPDATE stylize_jobs
                    SET status = 'failed',
                        finished_at = datetime('now'),
                        error = ?
                    WHERE job_id = ?
                    """,
                    (err, job_id),
                )
                conn.execute(
                    "UPDATE post_drafts SET error = ? WHERE id = ?",
                    (err, int(draft_id)),
                )
                conn.commit()
        except Exception:
            # Last-resort: don't bubble; the orphan recovery will clean us up.
            pass


def wait_for_terminal(job_id: str, max_wait_seconds: int = 50,
                      poll_interval: float = 2.0) -> dict:
    """Block up to max_wait_seconds for the job to reach a terminal status.
    Returns the latest job row regardless. Designed to be called from inside
    an MCP tool — caps below the ~60s MCP RPC timeout so the call always
    returns cleanly. Agent re-calls if status is still 'running'/'queued'."""
    deadline = time.monotonic() + max(0, int(max_wait_seconds))
    last: dict | None = None
    while True:
        last = get_job(job_id)
        if last is None:
            return {"ok": False, "error": "job not found", "job_id": job_id}
        if last["status"] in ("done", "failed"):
            return {"ok": True, "job": last}
        if time.monotonic() >= deadline:
            return {"ok": True, "job": last}
        time.sleep(poll_interval)
