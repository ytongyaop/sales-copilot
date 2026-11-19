from fastapi.testclient import TestClient

from salescopilot import __version__
from salescopilot.entrypoints.api.app import app


def test_health_returns_ok_in_fake_mode(monkeypatch) -> None:
    """默认（不设开关）是假模型：不联网、不花钱，模型名如实写 fake。"""
    monkeypatch.delenv("SALESCOPILOT_LIVE", raising=False)
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.3",
        "mode": "fake",
        "models": {"chat": "fake", "embed": "fake"},
    }


def test_health_reports_version() -> None:
    """`/health` 要带上包版本，部署方一眼能对上是哪个版本在跑。"""
    body = TestClient(app).get("/health").json()

    assert body["version"] == "0.1.3"
    assert body["version"] == __version__


def test_health_reports_live_mode_and_configured_models(monkeypatch) -> None:
    """开了真模型开关时，/health 要一眼看出是真模型，并带上实际模型名。"""
    monkeypatch.setenv("SALESCOPILOT_LIVE", "yes")
    monkeypatch.setenv("CHAT_MODEL", "test-chat-model")
    monkeypatch.setenv("EMBED_MODEL", "test-embed-model")
    body = TestClient(app).get("/health").json()

    assert body["status"] == "ok"
    assert body["mode"] == "live"
    assert body["models"] == {"chat": "test-chat-model", "embed": "test-embed-model"}
