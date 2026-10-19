"""Every test is offline: unmocked HTTP is a test failure."""
import pytest
import requests


def pytest_configure(config):
    config.option.basetemp = str(config.rootpath / ".cache" / "pytest-tmp")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    def blocked(*args, **kwargs):
        raise AssertionError("Tests may not access the network")
    monkeypatch.setattr(requests.Session, "request", blocked)
