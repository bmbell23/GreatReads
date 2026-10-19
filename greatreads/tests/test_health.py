def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["app"] == "GreatReads"


def test_ereader_health_degraded_without_calibre(client):
    # Calibre points at a dead port in tests -> degraded, not an error.
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["calibre_connected"] is False


def test_scheduler_status_when_disabled(client):
    r = client.get("/api/scheduler/status")
    assert r.status_code == 200


def test_tests_use_temp_db():
    from greatreads.database import engine
    assert "greatreads-tests-" in str(engine.url)
