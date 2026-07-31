"""The public API surface — only what has to be public, and no more.

Caddy allows /webhooks/* and /health through, and nothing else. These tests
guard the two things that were true anyway and should not depend on a proxy
config file staying correct: the docs are not served outside development, and
a public unauthenticated endpoint cannot be turned into a connection-exhaustion
lever by holding down refresh.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from career import main


def test_the_interactive_docs_are_not_served_outside_development() -> None:
    client = TestClient(main.app)

    # staging/production: a map of every route is not public
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_health_does_not_open_a_connection_pair_per_request(monkeypatch) -> None:
    """It is reachable from the internet so Meta and Salla can see we are up.
    Each call used to open a fresh Postgres AND Redis connection."""
    main._health_cache = None
    calls = {"db": 0, "redis": 0}

    def _db() -> bool:
        calls["db"] += 1
        return True

    def _redis() -> bool:
        calls["redis"] += 1
        return True

    monkeypatch.setattr(main, "_check_db", _db)
    monkeypatch.setattr(main, "_check_redis", _redis)
    client = TestClient(main.app)

    for _ in range(25):
        assert client.get("/health").status_code == 200

    assert calls == {"db": 1, "redis": 1}
    main._health_cache = None


def test_health_still_reports_a_real_outage(monkeypatch) -> None:
    main._health_cache = None
    monkeypatch.setattr(main, "_check_db", lambda: False)
    monkeypatch.setattr(main, "_check_redis", lambda: True)
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["database"] is False
    main._health_cache = None
