import base64
import json

import httpx

from app.agent import build_messages
from app.media import MediaProcessor, pending_incoming_ids
from app.transcription import Transcriber
from app.usage import UsageTracker

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
PDF = b"%PDF-1.4 comprobante"
OGG = b"OggS" + b"1" * 50


def history_with(*attachments, previous=None):
    msgs = list(previous or [])
    msgs.append({"id": 10, "message_type": 0, "content": "", "attachments": list(attachments)})
    return msgs


class Server:
    """Simula Chatwoot (descargas) y la API de transcripción."""

    def __init__(self, files=None):
        self.files = files or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/audio/transcriptions"):
            assert request.headers["authorization"] == "Bearer tk"
            assert b'name="language"' in request.content
            return httpx.Response(200, json={"text": "quiero dos docenas de medialunas"})
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://s3.test/blob"})
        body, ctype = self.files.get(str(request.url), (None, None))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": ctype})

    def downloads(self):
        return [r for r in self.requests if not r.url.path.endswith("/audio/transcriptions")]


def make(server, transcriber=True, usage=None, max_media=4):
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    t = Transcriber("groq", "https://api.groq.test/v1", "tk", "whisper", "es", http) if transcriber else None
    return MediaProcessor(http, ("cw.test",), transcriber=t, usage=usage, max_media_per_run=max_media)


def test_pending_incoming_ids():
    history = [
        {"id": 1, "message_type": 0},
        {"id": 2, "message_type": 1},
        {"id": 3, "message_type": 1, "private": True},
        {"id": 4, "message_type": 0},
        {"id": 5, "message_type": "incoming"},
    ]
    assert pending_incoming_ids(history) == {4, 5}


async def test_imagen_se_envia_a_claude():
    server = Server({"https://cw.test/img": (PNG, "image/png")})
    att = {"id": 1, "file_type": "image", "data_url": "https://cw.test/img"}
    history = history_with(att)
    rendered = await make(server).render(history, 1, 7)
    messages = build_messages(history, rendered)
    content = messages[0]["content"]
    assert content[0] == {"type": "text", "text": "[El cliente envió esta imagen]"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"
    assert base64.b64decode(content[1]["source"]["data"]) == PNG


async def test_sigue_redirecciones_de_active_storage():
    server = Server({"https://s3.test/blob": (PNG, "image/png")})
    att = {"id": 1, "file_type": "image", "data_url": "https://cw.test/redirect"}
    rendered = await make(server).render(history_with(att), 1, 7)
    assert rendered[10][1]["type"] == "image"


async def test_pdf():
    server = Server({"https://cw.test/f.pdf": (PDF, "application/pdf")})
    att = {"id": 1, "file_type": "file", "extension": "pdf", "data_url": "https://cw.test/f.pdf"}
    rendered = await make(server).render(history_with(att), 1, 7)
    assert rendered[10][1]["type"] == "document"


async def test_host_no_permitido_no_descarga():
    server = Server({"https://evil.test/img": (PNG, "image/png")})
    att = {"id": 1, "file_type": "image", "data_url": "https://evil.test/img"}
    rendered = await make(server).render(history_with(att), 1, 7)
    assert server.requests == []
    assert "no se pudo procesar" in rendered[10][0]["text"]


async def test_imagen_demasiado_grande():
    server = Server({"https://cw.test/img": (b"x" * (5 * 1024 * 1024 + 1), "image/jpeg")})
    att = {"id": 1, "file_type": "image", "data_url": "https://cw.test/img"}
    rendered = await make(server).render(history_with(att), 1, 7)
    assert "no se pudo procesar" in rendered[10][0]["text"]


async def test_imagenes_viejas_no_se_descargan():
    server = Server({"https://cw.test/img": (PNG, "image/png")})
    previous = [
        {"id": 1, "message_type": 0, "content": "mirá", "attachments": [
            {"id": 9, "file_type": "image", "data_url": "https://cw.test/img"}]},
        {"id": 2, "message_type": 1, "content": "¡Linda foto!"},
    ]
    history = history_with(previous=previous)
    history[-1]["content"] = "¿Tienen?"
    rendered = await make(server).render(history, 1, 7)
    assert server.requests == []
    assert rendered[1] == [{"type": "text", "text": "[El cliente envió una imagen]"}]
    assert build_messages(history, rendered)[0]["content"] == "mirá\n[El cliente envió una imagen]"


async def test_limite_de_adjuntos_por_respuesta():
    server = Server({"https://cw.test/img": (PNG, "image/png")})
    atts = [{"id": i, "file_type": "image", "data_url": "https://cw.test/img"} for i in range(3)]
    rendered = await make(server, max_media=2).render(history_with(*atts), 1, 7)
    assert [b["type"] for b in rendered[10]] == ["text", "image", "text", "image", "text"]
    assert rendered[10][-1]["text"] == "[El cliente envió una imagen]"


async def test_nota_de_voz_se_transcribe_una_sola_vez(tmp_path):
    server = Server({"https://cw.test/audio": (OGG, "audio/ogg")})
    usage = UsageTracker(tmp_path / "usage.jsonl")
    media = make(server, usage=usage)
    att = {"id": 5, "file_type": "audio", "extension": "ogg", "data_url": "https://cw.test/audio"}
    history = history_with(att)

    rendered = await media.render(history, 1, 7)
    expected = "[Nota de voz, transcripción automática]: quiero dos docenas de medialunas"
    assert rendered[10] == [{"type": "text", "text": expected}]
    assert build_messages(history, rendered)[0]["content"] == expected

    # La transcripción queda en caché aunque el audio ya no sea "pendiente".
    history.append({"id": 11, "message_type": 1, "content": "¡Dale!"})
    history.append({"id": 12, "message_type": 0, "content": "gracias"})
    rendered = await media.render(history, 1, 7)
    assert rendered[10][0]["text"] == expected
    transcriptions = [r for r in server.requests if r.url.path.endswith("/audio/transcriptions")]
    assert len(transcriptions) == 1
    entries = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert entries[0]["type"] == "transcription" and entries[0]["bytes"] == len(OGG)


async def test_usa_transcripcion_de_chatwoot_si_existe():
    server = Server()
    att = {"id": 5, "file_type": "audio", "transcribed_text": "hola, ¿abren hoy?", "data_url": "https://cw.test/a"}
    rendered = await make(server).render(history_with(att), 1, 7)
    assert server.requests == []
    assert rendered[10][0]["text"].endswith("hola, ¿abren hoy?")


async def test_audio_sin_transcriptor():
    server = Server()
    att = {"id": 5, "file_type": "audio", "data_url": "https://cw.test/a"}
    rendered = await make(server, transcriber=False).render(history_with(att), 1, 7)
    assert "no hay transcripción" in rendered[10][0]["text"]


async def test_ubicacion_y_contacto():
    atts = [
        {"id": 1, "file_type": "location", "coordinates_lat": -34.58, "coordinates_long": -58.42,
         "fallback_title": "Mi casa"},
        {"id": 2, "file_type": "contact", "fallback_title": "Juan"},
    ]
    rendered = await make(Server()).render(history_with(*atts), 1, 7)
    assert rendered[10][0]["text"] == "[El cliente compartió una ubicación: Mi casa (-34.58, -58.42)]"
    assert rendered[10][1]["text"] == "[El cliente compartió un contacto: Juan]"


async def test_cupo_de_imagenes_no_bloquea_audios():
    server = Server({"https://cw.test/img": (PNG, "image/png"), "https://cw.test/audio": (OGG, "audio/ogg")})
    atts = [
        {"id": 1, "file_type": "image", "data_url": "https://cw.test/img"},
        {"id": 2, "file_type": "audio", "extension": "oga", "data_url": "https://cw.test/audio"},
    ]
    rendered = await make(server, max_media=1).render(history_with(*atts), 1, 7)
    assert rendered[10][1]["type"] == "image"
    assert rendered[10][2]["text"].endswith("quiero dos docenas de medialunas")
    transcription = next(r for r in server.requests if r.url.path.endswith("/audio/transcriptions"))
    assert b'filename="audio.ogg"' in transcription.content
