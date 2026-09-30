import pytest

from server import config as config_mod
from server.security import Box, fernet_key, hash_password, recap_token, redact, verify_password


def test_passwords():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct horse" not in h
    assert verify_password("correct horse", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", None)
    assert not verify_password("x", "garbage")


def test_fernet():
    k = fernet_key("a" * 40)
    assert k == fernet_key("a" * 40) and k != fernet_key("b" * 40)
    box = Box(k)
    sealed = box.seal("sk-123")
    assert "sk-123" not in sealed and box.open(sealed) == "sk-123"
    assert Box(fernet_key("b" * 40)).open(sealed) is None
    from cryptography.fernet import Fernet
    explicit = Fernet.generate_key().decode()
    assert fernet_key("a" * 40, explicit) == explicit.encode()


def test_tokens_and_redact():
    t = recap_token()
    assert len(t) >= 43 and t != recap_token()
    assert redact("key=sk-abcdef here", ["sk-abcdef"]) == "key=[redacted] here"


def test_secret_key_required_in_production(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("TIMELINE_DEMO", raising=False)
    monkeypatch.setenv("BASE_URL", "https://timeline.example.com")
    with pytest.raises(RuntimeError):
        config_mod.load()
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    cfg = config_mod.load()  # local: a generated key is kept in the data dir
    assert cfg.secret_key == config_mod.load().secret_key and not cfg.secure_cookies
    monkeypatch.setenv("BASE_URL", "https://timeline.example.com")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    assert config_mod.load().secure_cookies


def test_base_url_defaults_to_railway_domain(monkeypatch, tmp_path):
    from server import config
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-that-is-long-enough")
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "timeline-production.up.railway.app")
    assert config.load().base_url == "https://timeline-production.up.railway.app"
    monkeypatch.setenv("BASE_URL", "https://timeline.rox.com/")
    assert config.load().base_url == "https://timeline.rox.com"
