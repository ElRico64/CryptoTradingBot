"""Optional Telegram alerts for entries, exits and daily stats."""

from __future__ import annotations

import logging

from .data.http import Http

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, http: Http, bot_token: str = "", chat_id: str = ""):
        self.http = http
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage" if bot_token and chat_id else ""
        self.chat_id = chat_id

    async def send(self, text: str) -> None:
        log.info("NOTIFY %s", text.replace("\n", " | "))
        if not self.url:
            return
        try:
            await self.http.post_json(self.url, {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)
