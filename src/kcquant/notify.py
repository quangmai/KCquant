"""Telegram and system notification utilities for KCquant."""
from __future__ import annotations

import os
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[2]


def load_env(env_path: Path | None = None) -> None:
    """Load key-value pairs from .env into os.environ if not already present."""
    path = env_path or (ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def send_telegram_message(
    text: str,
    token: str | None = None,
    chat_id: str | None = None,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None
) -> tuple[bool, str]:
    """Send a message to a Telegram chat via the Telegram Bot API.

    Returns:
        (success: bool, response_or_error_message: str)
    """
    load_env()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if token is None else token
    target_chat = os.environ.get("TELEGRAM_CHAT_ID") if chat_id is None else chat_id

    if not bot_token or not target_chat:
        return False, "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in environment / .env"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        resp = requests.post(url, json=payload, timeout=12.0)
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            return True, "Message sent successfully"
        error_desc = data.get("description", f"HTTP {resp.status_code}")
        return False, f"Telegram API error: {error_desc}"
    except requests.RequestException as err:
        return False, f"Network exception sending Telegram message: {err}"


def send_telegram_photo(
    photo_path: str | Path,
    caption: str | None = None,
    token: str | None = None,
    chat_id: str | None = None,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None
) -> tuple[bool, str]:
    """Send a photo image to a Telegram chat with optional HTML caption and inline keyboard markup.

    Returns:
        (success: bool, response_or_error_message: str)
    """
    import json
    load_env()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if token is None else token
    target_chat = os.environ.get("TELEGRAM_CHAT_ID") if chat_id is None else chat_id

    if not bot_token or not target_chat:
        return False, "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in environment / .env"

    path = Path(photo_path)
    if not path.exists():
        return False, f"Photo file not found: {path}"

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    data = {
        "chat_id": target_chat,
        "parse_mode": parse_mode,
    }
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)

    try:
        with open(path, "rb") as f:
            files = {"photo": f}
            resp = requests.post(url, data=data, files=files, timeout=25.0)
        data_resp = resp.json()
        if resp.status_code == 200 and data_resp.get("ok"):
            return True, "Photo sent successfully"
        error_desc = data_resp.get("description", f"HTTP {resp.status_code}")
        return False, f"Telegram API error: {error_desc}"
    except requests.RequestException as err:
        return False, f"Network exception sending Telegram photo: {err}"


def answer_telegram_callback(
    callback_query_id: str,
    text: str | None = None,
    show_alert: bool = False,
    token: str | None = None
) -> bool:
    """Acknowledge a Telegram inline button tap."""
    load_env()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if token is None else token
    if not bot_token:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text

    try:
        resp = requests.post(url, json=payload, timeout=8.0)
        return bool(resp.status_code == 200 and resp.json().get("ok"))
    except Exception:
        return False


def get_telegram_updates(
    offset: int | None = None,
    timeout: int = 5,
    token: str | None = None
) -> list[dict]:
    """Retrieve new messages and callback query events from Telegram."""
    load_env()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if token is None else token
    if not bot_token:
        return []

    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset

    try:
        resp = requests.get(url, params=params, timeout=timeout + 5)
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception:
        pass
    return []

