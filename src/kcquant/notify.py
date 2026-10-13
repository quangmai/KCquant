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


def send_telegram_message(text: str, token: str | None = None,
                          chat_id: str | None = None, parse_mode: str = "HTML") -> tuple[bool, str]:
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

    try:
        resp = requests.post(url, json=payload, timeout=12.0)
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            return True, "Message sent successfully"
        error_desc = data.get("description", f"HTTP {resp.status_code}")
        return False, f"Telegram API error: {error_desc}"
    except requests.RequestException as err:
        return False, f"Network exception sending Telegram message: {err}"
