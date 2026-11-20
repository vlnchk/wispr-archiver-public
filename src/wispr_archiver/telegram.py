"""Telegram Bot API notifier (token and chat id from the environment only).

The bot token is part of the request URL, so neither the URL nor exception messages are
ever logged; the dispatcher logs only the exception class on failure.
"""

from __future__ import annotations

import httpx2

API = "https://api.telegram.org"
MAX_LEN = 4000


class TelegramNotifier:
    def __init__(
        self, token: str, chat_id: str, *, transport: httpx2.BaseTransport | None = None
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self.transport = transport

    def send(self, text: str) -> None:
        with httpx2.Client(transport=self.transport, timeout=httpx2.Timeout(15.0)) as http:
            resp = http.post(
                f"{API}/bot{self._token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text[:MAX_LEN],
                    "disable_web_page_preview": True,
                },
            )
        if resp.status_code != 200:
            raise RuntimeError(f"telegram HTTP {resp.status_code}")
