"""Ported verbatim from v1 ApiChain--Backend/backend/tests/test_geocode.py."""

import app.services.geocode as g


def test_reverse_geocode_builds_place_string(monkeypatch):
    class _R:
        status_code = 200

        def json(self):
            return {"address": {"county": "Nyeri", "state": "Central", "country": "Kenya"}}

    monkeypatch.setattr(g.requests, "get", lambda *a, **k: _R())
    assert g.reverse_geocode(-0.42, 36.95) == "Nyeri, Central, Kenya"


def test_reverse_geocode_failsoft_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(g.requests, "get", _boom)
    assert g.reverse_geocode(-0.42, 36.95) is None


def test_reverse_geocode_non200_returns_none(monkeypatch):
    class _R:
        status_code = 503

        def json(self):
            return {}

    monkeypatch.setattr(g.requests, "get", lambda *a, **k: _R())
    assert g.reverse_geocode(-0.42, 36.95) is None
