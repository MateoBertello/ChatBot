"""Cliente mínimo de la API de Chatwoot usada por el bot."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Valores de message_type en la API de Chatwoot (la API de mensajes los devuelve
# como entero; los webhooks, como texto).
INCOMING, OUTGOING, ACTIVITY, TEMPLATE = 0, 1, 2, 3
_MESSAGE_TYPES = {"incoming": INCOMING, "outgoing": OUTGOING, "activity": ACTIVITY, "template": TEMPLATE}


def message_type_code(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _MESSAGE_TYPES.get(value.lower())
    return None


class ChatwootError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ChatwootClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- Conversaciones -------------------------------------------------------

    async def get_conversation(self, token: str, account_id: int, conversation_id: int) -> dict[str, Any]:
        return await self._request("GET", token, f"/accounts/{account_id}/conversations/{conversation_id}")

    async def list_messages(self, token: str, account_id: int, conversation_id: int) -> list[dict[str, Any]]:
        """Devuelve los últimos 20 mensajes (límite fijo de Chatwoot), en orden cronológico,
        excluyendo notas privadas y mensajes de actividad."""
        data = await self._request(
            "GET",
            token,
            f"/accounts/{account_id}/conversations/{conversation_id}/messages",
            params={"filter_internal_messages": "true"},
        )
        return data.get("payload", []) if isinstance(data, dict) else []

    async def send_message(
        self, token: str, account_id: int, conversation_id: int, content: str, *, private: bool = False
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            token,
            f"/accounts/{account_id}/conversations/{conversation_id}/messages",
            json={"content": content, "message_type": "outgoing", "private": private},
        )

    async def set_status(self, token: str, account_id: int, conversation_id: int, status: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            token,
            f"/accounts/{account_id}/conversations/{conversation_id}/toggle_status",
            json={"status": status},
        )

    async def add_labels(self, token: str, account_id: int, conversation_id: int, labels: list[str]) -> list[str]:
        """Agrega etiquetas conservando las existentes (el endpoint de Chatwoot reemplaza la lista)."""
        path = f"/accounts/{account_id}/conversations/{conversation_id}/labels"
        current = await self._request("GET", token, path)
        existing = list(current.get("payload", [])) if isinstance(current, dict) else []
        merged = existing + [label for label in labels if label not in existing]
        if merged == existing:
            return existing
        result = await self._request("POST", token, path, json={"labels": merged})
        return list(result.get("payload", merged)) if isinstance(result, dict) else merged

    # --- Interno ----------------------------------------------------------------

    async def _request(self, method: str, token: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}/api/v1{path}"
        try:
            response = await self._http.request(method, url, headers={"api_access_token": token}, **kwargs)
        except httpx.HTTPError as exc:
            raise ChatwootError(f"{method} {path}: {exc!r}") from exc
        if response.status_code >= 400:
            raise ChatwootError(
                f"{method} {path}: HTTP {response.status_code} {response.text[:300]}", response.status_code
            )
        if not response.content:
            return {}
        return response.json()
