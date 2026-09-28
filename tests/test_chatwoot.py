import json

import httpx
import pytest

from app.chatwoot import ChatwootClient, ChatwootError


def make_client(handler):
    return ChatwootClient("https://cw.test/", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_add_labels_conserva_las_existentes():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["api_access_token"] == "tok"
        if request.method == "GET":
            return httpx.Response(200, json={"payload": ["vip"]})
        return httpx.Response(200, json={"payload": json.loads(request.content)["labels"]})

    labels = await make_client(handler).add_labels("tok", 1, 7, ["venta"])
    assert labels == ["vip", "venta"]
    assert str(requests[1].url) == "https://cw.test/api/v1/accounts/1/conversations/7/labels"


async def test_send_message_nota_privada():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"content": "hola", "message_type": "outgoing", "private": True}
        return httpx.Response(200, json={"id": 1})

    assert await make_client(handler).send_message("tok", 1, 7, "hola", private=True) == {"id": 1}


async def test_error_http():
    client = make_client(lambda request: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(ChatwootError) as exc:
        await client.list_messages("tok", 1, 7)
    assert exc.value.status_code == 401
