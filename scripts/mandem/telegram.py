#!/usr/bin/env python3
# scripts/mandem/telegram.py
# Telegram Bot API helper for sending draft DMs with inline-keyboard buttons.
# Uses httpx (already a dep via footy_api).

from __future__ import annotations

import json
from pathlib import Path

import httpx

from . import _env

BOT_API = "https://api.telegram.org"
TELEGRAM_CAPTION_LIMIT = 1024  # photos cap at 1024 chars; longer captions truncate or split


def _bot_url(method: str) -> str:
    _env.load()
    token = _env.require("MANDEM_BOT_TOKEN")
    return f"{BOT_API}/bot{token}/{method}"


def _build_keyboard(draft_id: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"ft:approve:{draft_id}"},
            {"text": "✏ Edit",     "callback_data": f"ft:edit:{draft_id}"},
            {"text": "🗑 Skip",    "callback_data": f"ft:skip:{draft_id}"},
        ]]
    }


def send_draft_dm(image_path: str | Path, caption: str, draft_id: int, chat_id: int | None = None) -> dict:
    """Post a draft preview to the operator. Returns the Telegram API response dict."""
    _env.load()
    chat_id = chat_id or int(_env.require("MJ_MANDEM_CHAT_ID"))
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"image not found: {image_path}")

    if len(caption) > TELEGRAM_CAPTION_LIMIT:
        # Truncate with ellipsis — Mandem captions should fit comfortably under 1024 anyway
        caption = caption[: TELEGRAM_CAPTION_LIMIT - 1].rstrip() + "…"

    data = {
        "chat_id": str(chat_id),
        "caption": caption,
        "reply_markup": json.dumps(_build_keyboard(draft_id)),
    }
    with image_path.open("rb") as f:
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        files = {"photo": (image_path.name, f, mime)}
        with httpx.Client(timeout=30.0) as c:
            r = c.post(_bot_url("sendPhoto"), data=data, files=files)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram sendPhoto failed: {r.status_code} {r.text[:300]}")
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto error: {body}")
    return body


def send_text(text: str, chat_id: int | None = None, reply_to: int | None = None) -> dict:
    """Plain text message — used for 'send rewrite as a reply' prompts and simple acks."""
    _env.load()
    chat_id = chat_id or int(_env.require("MJ_MANDEM_CHAT_ID"))
    data = {"chat_id": str(chat_id), "text": text}
    if reply_to:
        data["reply_to_message_id"] = str(reply_to)
    with httpx.Client(timeout=15.0) as c:
        r = c.post(_bot_url("sendMessage"), data=data)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram sendMessage error: {body}")
    return body


def set_reaction(message_id: int, emoji: str, chat_id: int | None = None) -> dict:
    """React to a message with an emoji (used to ack ✅/🗑 callbacks)."""
    _env.load()
    chat_id = chat_id or int(_env.require("MJ_MANDEM_CHAT_ID"))
    data = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
    }
    with httpx.Client(timeout=10.0) as c:
        r = c.post(_bot_url("setMessageReaction"), data=data)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": False, "raw": r.text}
    return body


def get_me() -> dict:
    """Return the bot's identity. Cached per-process via _bot_url."""
    with httpx.Client(timeout=10.0) as c:
        r = c.get(_bot_url("getMe"))
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram getMe error: {body}")
    return body["result"]


def get_updates(offset: int = 0, timeout_seconds: int = 30,
                allowed_updates: list[str] | None = None) -> list[dict]:
    """Long-poll Telegram for new updates. Returns the list (possibly empty)."""
    params: dict[str, str] = {
        "timeout": str(timeout_seconds),
        "offset": str(offset),
    }
    if allowed_updates:
        params["allowed_updates"] = json.dumps(allowed_updates)
    # httpx connect timeout > server long-poll timeout to avoid premature timeouts
    with httpx.Client(timeout=httpx.Timeout(timeout_seconds + 10, connect=10.0)) as c:
        r = c.get(_bot_url("getUpdates"), params=params)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram getUpdates error: {body}")
    return body.get("result") or []


def answer_callback_query(callback_query_id: str, text: str | None = None,
                          show_alert: bool = False) -> dict:
    """Acknowledge a callback within Telegram's ~10s window. Required for inline-button taps."""
    data = {"callback_query_id": callback_query_id}
    if text:
        data["text"] = text[:200]
    if show_alert:
        data["show_alert"] = "true"
    with httpx.Client(timeout=10.0) as c:
        r = c.post(_bot_url("answerCallbackQuery"), data=data)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": False, "raw": r.text}
    return body


def edit_message_reply_markup(chat_id: int, message_id: int,
                              keyboard: dict | None = None) -> dict:
    """Replace (or remove) the inline keyboard on a previously-sent message.
    Pass keyboard=None to strip buttons after they've been used."""
    data: dict[str, str] = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
    }
    if keyboard is not None:
        data["reply_markup"] = json.dumps(keyboard)
    else:
        # Empty keyboard removes the buttons
        data["reply_markup"] = json.dumps({"inline_keyboard": []})
    with httpx.Client(timeout=10.0) as c:
        r = c.post(_bot_url("editMessageReplyMarkup"), data=data)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": False, "raw": r.text}
    return body


def send_photo(image_path: str | Path, caption: str = "",
               chat_id: int | None = None, reply_to: int | None = None) -> dict:
    """Plain photo send (no inline keyboard) — used for delivering the stylized post-approval preview."""
    _env.load()
    chat_id = chat_id or int(_env.require("MJ_MANDEM_CHAT_ID"))
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"image not found: {image_path}")
    if len(caption) > TELEGRAM_CAPTION_LIMIT:
        caption = caption[: TELEGRAM_CAPTION_LIMIT - 1].rstrip() + "…"
    data: dict[str, str] = {"chat_id": str(chat_id), "caption": caption}
    if reply_to:
        data["reply_to_message_id"] = str(reply_to)
    with image_path.open("rb") as f:
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        files = {"photo": (image_path.name, f, mime)}
        with httpx.Client(timeout=30.0) as c:
            r = c.post(_bot_url("sendPhoto"), data=data, files=files)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram sendPhoto failed: {r.status_code} {r.text[:300]}")
    return r.json()
