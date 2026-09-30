import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set before server.app is imported anywhere: its module-level app must not touch ./var.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="timeline-test-")
os.environ["TIMELINE_DEMO"] = "1"
os.environ["TIMELINE_WORKER"] = "0"
os.environ.pop("BASE_URL", None)
os.environ.pop("SECRET_KEY", None)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TIMELINE_DEMO", "1")
    monkeypatch.setenv("TIMELINE_WORKER", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-that-is-long-enough")
    from server import config
    from server.app import create_app
    return create_app(config.load())


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


def signup(client, email="ana@example.com", password="correct horse"):
    r = client.post("/api/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()
