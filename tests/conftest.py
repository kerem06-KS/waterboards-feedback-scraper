import socket
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for var in ("WBSCRAPER_DATA_DIR", "WBSCRAPER_CONFIG_DIR", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Tests must never touch the network: any real socket connect fails loudly."""
    def guard(*args, **kwargs):
        raise RuntimeError("real network access is blocked in tests")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
