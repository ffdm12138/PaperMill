from __future__ import annotations

import pytest
import requests


@pytest.fixture
def install_pdf_transport_get(monkeypatch):
    """Install a fake requests.Session that delegates to a fake_get callable."""

    class _Session:
        def __init__(self):
            self.trust_env = True
            self.closed = False

        def request(self, method, url, **kwargs):
            return self._fake_get(url, **kwargs)

        def close(self):
            self.closed = True

    def _install(fake_get):
        _Session._fake_get = staticmethod(fake_get)
        monkeypatch.setattr(requests, "Session", _Session)

    return _install
