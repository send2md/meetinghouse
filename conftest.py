"""Shared pytest fixture: a fresh copy of the app on its own temporary database."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETINGHOUSE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MEETINGHOUSE_ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("MEETINGHOUSE_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("MEETINGHOUSE_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv("MEETINGHOUSE_IDCHECK", "practice")
    monkeypatch.delenv("MEETINGHOUSE_MAIL", raising=False)
    monkeypatch.delenv("MEETINGHOUSE_ORIGINS", raising=False)
    monkeypatch.delenv("MEETINGHOUSE_RP_ID", raising=False)

    import app as app_module
    import mailer

    importlib.reload(app_module)
    mailer.OUTBOX.clear()
    app_module.app.testing = True
    with app_module.app.test_client() as test_client:
        yield test_client, app_module
