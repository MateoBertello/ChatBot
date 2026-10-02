import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app.limits import RateLimiter
from app.usage import UsageTracker, estimate_cost


def test_estimate_cost():
    assert estimate_cost("claude-opus-5", 1000, 100, 0, 4000) == 0.0095
    assert estimate_cost("claude-opus-5", 0, 0, 1_000_000, 0) == 6.25
    assert estimate_cost("modelo-desconocido", 1, 1, 0, 0) is None


async def test_record_claude_con_fallback(tmp_path):
    tracker = UsageTracker(tmp_path / "u.jsonl")
    usage = SimpleNamespace(
        input_tokens=50, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=0,
        iterations=[
            SimpleNamespace(type="message", model="claude-opus-5", input_tokens=100, output_tokens=0,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0),
            SimpleNamespace(type="fallback_message", model="claude-opus-4-8", input_tokens=50, output_tokens=20,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0),
        ],
    )
    await tracker.record_claude(1, 7, SimpleNamespace(usage=usage, model="claude-opus-4-8"))
    entries = [json.loads(line) for line in (tmp_path / "u.jsonl").read_text().splitlines()]
    assert [e["model"] for e in entries] == ["claude-opus-5", "claude-opus-4-8"]


async def test_summarize(tmp_path):
    path = tmp_path / "u.jsonl"
    tracker = UsageTracker(path)
    response = SimpleNamespace(model="claude-opus-5", usage=SimpleNamespace(
        input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=4000))
    await tracker.record_claude(1, 7, response)
    await tracker.record_claude(2, 8, response)
    await tracker.record("run", 1, 7, outcome="handed_off", handed_off=True, interest_registered=False)
    await tracker.record("transcription", 1, 7, bytes=300)
    with path.open("a") as fh:  # un registro de otro mes
        fh.write(json.dumps({"ts": "2020-01-01T00:00:00+00:00", "type": "claude", "account_id": 1,
                             "model": "claude-opus-5", "input_tokens": 9, "cost_usd": 1.0}) + "\n")

    month = tracker.summarize()["accounts"]["1"]
    assert month["llamadas_claude"] == 2  # sin filtro de mes
    summary = tracker.summarize(month=datetime.now(timezone.utc).strftime("%Y-%m"), account_id=1)
    acc = summary["accounts"]["1"]
    assert acc["llamadas_claude"] == 1 and acc["costo_estimado_usd"] == 0.0095
    assert acc["mensajes_atendidos"] == 1 and acc["derivaciones"] == 1
    assert acc["transcripciones"] == 1 and acc["audio_bytes"] == 300
    assert list(summary["accounts"]) == ["1"]


def test_rate_limiter():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert limiter.allow("k", 2, 60) and limiter.allow("k", 2, 60)
    assert not limiter.allow("k", 2, 60)
    now[0] = 61
    assert limiter.allow("k", 2, 60)
    assert all(limiter.allow("sin-limite", 0, 60) for _ in range(100))
    now[0] = 1000
    limiter.prune(60)
    assert limiter._hits == {}
