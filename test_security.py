"""Tests for membership and login security: joining with a passkey and an ID
check, passkey login, email recovery, renewal, lockouts, and the Didit link."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import importlib
import json

from cryptography.hazmat.primitives.asymmetric import ec

import idcheck
import mailer
from testkit import (
    PASSWORD,
    AddPasskey,
    As,
    Join,
    LegacyMember,
    PasskeyLogin,
    PasswordLogin,
    Pid,
    PracticeCheck,
    Register,
    SoftAuthenticator,
)


def _Later(monkeypatch, app_module, **delta):
    later = app_module.Now() + dt.timedelta(**delta)
    monkeypatch.setattr(app_module, "Now", lambda: later)
    return later


# --- Joining ----------------------------------------------------------------


def test_someone_still_joining_can_read_but_not_take_part(client):
    test_client, app_module = client
    Register(test_client, "janedoe")
    pid = Pid(app_module)
    assert not app_module.security.IsActivated(app_module.conn, pid)
    assert not app_module.house.participants[pid].IsActive(app_module.Now())
    assert b"haven't finished joining" in test_client.get("/").data
    test_client.post("/topic/new/forum", data={"title": "Should not appear"})
    assert not app_module.house.topics


def test_id_check_needs_a_passkey_first(client):
    test_client, app_module = client
    Register(test_client, "janedoe")
    resp = test_client.post("/join/idcheck", data={"provider": "practice"}, follow_redirects=True)
    assert b"Add your passkey first" in resp.data
    assert app_module.security.LatestIdSession(app_module.conn, Pid(app_module), idcheck.JOIN) is None


def test_passing_the_id_check_makes_a_member(client):
    test_client, app_module = client
    pid, device = Join(test_client, app_module)
    assert app_module.security.IsActivated(app_module.conn, pid)
    assert app_module.house.participants[pid].IsActive(app_module.Now())
    welcome = [m for m in mailer.OUTBOX if m["subject"] == "Welcome to Meetinghouse"]
    assert welcome and welcome[0]["to"] == "janedoe@example.com"
    photo = test_client.get(f"/photo/{pid}")
    assert photo.status_code == 200 and photo.data == idcheck.PRACTICE_SELFIE
    test_client.post("/topic/new/forum", data={"title": "First Conversation"})
    assert any(t.title == "First Conversation" for t in app_module.house.topics.values())


def test_declined_or_duplicate_id_check_does_not_make_a_member(client):
    test_client, app_module = client
    Register(test_client, "janedoe")
    AddPasskey(test_client, SoftAuthenticator())
    pid = Pid(app_module)
    assert b"didn't pass" in PracticeCheck(test_client, "declined").data
    assert not app_module.security.IsActivated(app_module.conn, pid)
    assert b"already belongs to a Meetinghouse member" in PracticeCheck(test_client, "duplicate").data
    assert not app_module.security.IsActivated(app_module.conn, pid)
    assert b"Each person can be a member only once" in test_client.get("/join").data


def test_member_chooses_from_the_approved_id_check_companies(client, monkeypatch):
    test_client, app_module = client
    monkeypatch.setenv("MEETINGHOUSE_IDCHECK", "didit,practice")
    Register(test_client, "janedoe")
    AddPasskey(test_client, SoftAuthenticator())
    page = test_client.get("/join").data
    assert b'value="didit"' in page and b'value="practice"' in page
    refused = test_client.post("/join/idcheck", data={"provider": "acme"}, follow_redirects=True)
    assert b"Choose one of the ID-check companies on the list" in refused.data
    assert b"membership" in PracticeCheck(test_client, provider="practice").data.lower()


def test_practice_check_is_unreachable_unless_configured(client, monkeypatch):
    test_client, app_module = client
    Register(test_client, "janedoe")
    AddPasskey(test_client, SoftAuthenticator())
    start = test_client.post("/join/idcheck", data={"provider": "practice"})
    practice_url = start.headers["Location"]
    monkeypatch.setenv("MEETINGHOUSE_IDCHECK", "didit")
    assert test_client.post(practice_url, data={"outcome": "approved"}).status_code == 404
    monkeypatch.setenv("MEETINGHOUSE_IDCHECK", "")
    resp = test_client.post("/join/idcheck", data={"provider": "practice"}, follow_redirects=True)
    assert b"ID checks aren't available yet" in resp.data
    assert not app_module.security.IsActivated(app_module.conn, Pid(app_module))


def test_existing_members_are_grandfathered_as_activated(client):
    test_client, app_module = client
    old_pid = LegacyMember(app_module)
    Register(test_client, "newuser", name="New User")
    new_pid = Pid(app_module, "New User")
    app_module.conn.execute("DROP TABLE account_status")
    app_module.conn.commit()
    importlib.reload(app_module)
    assert app_module.security.IsActivated(app_module.conn, old_pid)
    assert not app_module.security.IsActivated(app_module.conn, new_pid)


# --- Passkeys ---------------------------------------------------------------


def test_passkey_login_and_last_login_message(client, monkeypatch):
    test_client, app_module = client
    pid, device = Join(test_client, app_module)
    test_client.get("/logout")
    resp = PasskeyLogin(test_client, "janedoe", device)
    assert resp.status_code == 200, resp.get_json()
    first = test_client.get("/").data
    assert b"You last logged in on" in first or b"This is your first login" in first
    assert app_module.security.GetPasskey(app_module.conn, device.id)["sign_count"] == device.count
    test_client.get("/logout")
    _Later(monkeypatch, app_module, days=3)
    PasskeyLogin(test_client, "janedoe", device)
    assert b"You last logged in on" in test_client.get("/").data


def test_password_stops_working_once_a_passkey_exists(client):
    test_client, app_module = client
    LegacyMember(app_module)
    PasswordLogin(test_client, "olduser", PASSWORD)
    AddPasskey(test_client, SoftAuthenticator())
    test_client.get("/logout")
    test_client.post("/login", data={"username": "olduser"})
    resp = test_client.post("/login/password", data={"password": PASSWORD})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login/passkey")
    assert test_client.get("/account").status_code == 302


def test_passkey_with_wrong_key_is_refused(client):
    test_client, app_module = client
    _, device = Join(test_client, app_module)
    test_client.get("/logout")
    impostor_key = ec.generate_private_key(ec.SECP256R1())
    assert PasskeyLogin(test_client, "janedoe", device, signing_key=impostor_key).status_code == 400
    assert test_client.get("/account").status_code == 302


def test_passkey_without_face_fingerprint_or_pin_is_refused(client):
    test_client, app_module = client
    _, device = Join(test_client, app_module)
    test_client.get("/logout")
    assert PasskeyLogin(test_client, "janedoe", device, user_verified=False).status_code == 400


def test_passkey_from_another_site_is_refused(client):
    test_client, app_module = client
    _, device = Join(test_client, app_module)
    test_client.get("/logout")
    phishing = SoftAuthenticator(origin="https://meetinghouse-login.example")
    phishing.key, phishing.credential_id = device.key, device.credential_id
    assert PasskeyLogin(test_client, "janedoe", phishing).status_code == 400


def test_someone_elses_passkey_cannot_log_into_this_account(client):
    test_client, app_module = client
    Join(test_client, app_module, "janedoe", "Jane Doe")
    test_client.get("/logout")
    _, bobs_device = Join(test_client, app_module, "bob", "Bob Roe")
    test_client.get("/logout")
    test_client.post("/login", data={"username": "janedoe"})
    options = test_client.post("/login/passkey/options", json={}).get_json()
    assert test_client.post("/login/passkey/verify", json={"credential": bobs_device.get(options)}).status_code == 400


def test_challenge_cannot_be_replayed(client):
    test_client, app_module = client
    _, device = Join(test_client, app_module)
    test_client.get("/logout")
    test_client.post("/login", data={"username": "janedoe"})
    options = test_client.post("/login/passkey/options", json={}).get_json()
    answer = device.get(options)
    assert test_client.post("/login/passkey/verify", json={"credential": answer}).status_code == 200
    test_client.get("/logout")
    test_client.post("/login", data={"username": "janedoe"})
    assert test_client.post("/login/passkey/verify", json={"credential": answer}).status_code == 400


def test_only_passkey_cannot_be_removed(client):
    test_client, app_module = client
    pid, device = Join(test_client, app_module)
    resp = test_client.post("/account/passkey/remove", data={"credential_id": device.id}, follow_redirects=True)
    assert "That's your only passkey" in html.unescape(resp.data.decode())
    assert app_module.security.HasPasskeys(app_module.conn, pid)
    second = SoftAuthenticator()
    AddPasskey(test_client, second)
    test_client.post("/account/passkey/remove", data={"credential_id": device.id})
    assert [p["credential_id"] for p in app_module.security.ListPasskeys(app_module.conn, pid)] == [second.id]


# --- Recovery by email --------------------------------------------------------


def _RequestRecovery(test_client, username="janedoe"):
    test_client.post("/login", data={"username": username})
    test_client.post("/login/recover")
    mail = [m for m in mailer.OUTBOX if "recover" in m["subject"]][-1]
    code = mail["body"].split("enter this code: ")[1].split()[0]
    cancel = mail["body"].split("cancel it now: ")[1].split()[0]
    return code, cancel


def test_recovery_code_works_only_after_48_hours_and_removes_lost_passkeys(client, monkeypatch):
    test_client, app_module = client
    pid, lost = Join(test_client, app_module)
    test_client.get("/logout")
    code, _ = _RequestRecovery(test_client)

    too_soon = test_client.post("/login/code", data={"code": code}, follow_redirects=True)
    assert b"only starts working on" in too_soon.data
    assert test_client.get("/account").status_code == 302

    _Later(monkeypatch, app_module, hours=49)
    resp = test_client.post("/login/code", data={"code": code}, follow_redirects=True)
    assert "You're back in" in html.unescape(resp.data.decode())
    assert not app_module.security.HasPasskeys(app_module.conn, pid)
    new_device = SoftAuthenticator()
    assert AddPasskey(test_client, new_device).status_code == 200
    test_client.get("/logout")
    assert PasskeyLogin(test_client, "janedoe", new_device).status_code == 200
    test_client.get("/logout")
    test_client.post("/login", data={"username": "janedoe"})
    options = test_client.post("/login/passkey/options", json={}).get_json()
    assert test_client.post("/login/passkey/verify", json={"credential": lost.get(options)}).status_code == 400


def test_wasnt_you_link_cancels_recovery(client, monkeypatch):
    test_client, app_module = client
    pid, _ = Join(test_client, app_module)
    test_client.get("/logout")
    code, cancel = _RequestRecovery(test_client)
    assert b"Recovery cancelled" in test_client.get(cancel.replace("http://localhost", "")).data
    _Later(monkeypatch, app_module, hours=49)
    test_client.post("/login", data={"username": "janedoe"})
    resp = test_client.post("/login/code", data={"code": code}, follow_redirects=True)
    assert "didn't match" in html.unescape(resp.data.decode())
    assert app_module.security.HasPasskeys(app_module.conn, pid)


def test_recovery_code_guessing_is_locked_out(client, monkeypatch):
    test_client, app_module = client
    Join(test_client, app_module)
    test_client.get("/logout")
    code, _ = _RequestRecovery(test_client)
    _Later(monkeypatch, app_module, hours=49)
    for _ in range(5):
        resp = test_client.post("/login/code", data={"code": "AAAA-AAAA"}, follow_redirects=True)
    assert b"paused" in resp.data
    assert b"paused" in test_client.post("/login/code", data={"code": code}, follow_redirects=True).data


# --- The yearly selfie ----------------------------------------------------------


def test_reminders_then_pause_then_renewal(client, monkeypatch):
    test_client, app_module = client
    pid, device = Join(test_client, app_module)
    start = app_module.Now()

    _Later(monkeypatch, app_module, days=340)  # 25 days left: the 30-day reminder
    sent = app_module.tick_runner.SendRenewalReminders(app_module.house, app_module.conn, app_module.Now())
    assert sent == [(pid, 30)]
    assert app_module.tick_runner.SendRenewalReminders(app_module.house, app_module.conn, app_module.Now()) == []
    monkeypatch.setattr(app_module, "Now", lambda: start + dt.timedelta(days=360))
    assert app_module.tick_runner.SendRenewalReminders(app_module.house, app_module.conn, app_module.Now()) == [(pid, 7)]

    monkeypatch.setattr(app_module, "Now", lambda: start + dt.timedelta(days=370))
    As(test_client, "janedoe", device)  # a paused member can still log in...
    assert b"Your membership is paused" in test_client.get("/").data
    test_client.post("/topic/new/forum", data={"title": "Not while paused"})
    assert not app_module.house.topics  # ...but can't take part

    assert b"renewed for another year" in PracticeCheck(test_client, url="/account/renew").data
    assert app_module.house.participants[pid].IsActive(app_module.Now())


# --- Limits on guessing -----------------------------------------------------


def test_password_guessing_is_locked_out_for_fifteen_minutes(client, monkeypatch):
    test_client, app_module = client
    LegacyMember(app_module)
    for _ in range(5):
        resp = PasswordLogin(test_client, "olduser", "wrong guess")
    assert b"paused" in resp.data
    assert b"paused" in PasswordLogin(test_client, "olduser", PASSWORD).data
    assert test_client.get("/account").status_code == 302
    _Later(monkeypatch, app_module, minutes=16)
    assert b"Welcome, Old User" in PasswordLogin(test_client, "olduser", PASSWORD).data


def test_admin_password_guessing_is_locked_out(client):
    test_client, _ = client
    for _ in range(5):
        resp = test_client.post("/admin/login", data={"password": "nope"}, follow_redirects=True)
    assert b"paused" in resp.data
    resp = test_client.post("/admin/login", data={"password": "test-admin-password"}, follow_redirects=True)
    assert b"paused" in resp.data


def test_next_parameter_cannot_send_people_to_another_site(client):
    test_client, app_module = client
    LegacyMember(app_module)
    test_client.post("/login", data={"username": "olduser"})
    resp = test_client.post("/login/password?next=https://evil.example/", data={"password": PASSWORD})
    assert resp.headers["Location"] == "/"


# --- Didit --------------------------------------------------------------------


def _Signed(secret, body, timestamp):
    raw = json.dumps(body).encode()
    canonical = json.dumps(idcheck._WholeFloatsToInts(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return raw, {
        "X-Timestamp": str(timestamp),
        "X-Signature-V2": hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest(),
    }


def test_didit_webhook_signature_is_checked(monkeypatch):
    monkeypatch.setenv("MEETINGHOUSE_DIDIT_WEBHOOK_SECRET", "s3cret")
    provider = idcheck.DiditProvider()
    body = {"session_id": "abc", "status": "Approved", "score": 1.0}
    raw, headers = _Signed("s3cret", body, 1000)
    assert provider.VerifyWebhook(raw, headers, 1100) == body
    assert provider.VerifyWebhook(raw, headers, 1400) is None  # more than 5 minutes off
    _, forged = _Signed("wrong", body, 1000)
    assert provider.VerifyWebhook(raw, forged, 1100) is None
    tampered = json.dumps({**body, "status": "Declined"}).encode()
    assert provider.VerifyWebhook(tampered, headers, 1100) is None


def test_didit_unsigned_webhook_is_refused_by_the_site(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setenv("MEETINGHOUSE_IDCHECK", "didit")
    monkeypatch.setenv("MEETINGHOUSE_DIDIT_WEBHOOK_SECRET", "s3cret")
    resp = test_client.post("/idcheck/didit/webhook", data=b'{"status":"Approved"}', headers={"X-Timestamp": "1"})
    assert resp.status_code == 401


def test_didit_decisions_are_interpreted(monkeypatch):
    provider = idcheck.DiditProvider()
    assert provider.Interpret("s", idcheck.JOIN, {"status": "In Review"}) is None
    assert provider.Interpret("s", idcheck.JOIN, {"status": "Declined", "session_id": "d1"}).outcome == idcheck.DECLINED
    duplicate = {
        "status": "Approved",
        "session_id": "d1",
        "liveness_checks": [{"matches": [{"session_id": "d0", "status": "Approved"}]}],
    }
    assert provider.Interpret("s", idcheck.JOIN, duplicate).outcome == idcheck.DUPLICATE
    approved = {"status": "Approved", "session_id": "d1", "id_verifications": [{"issuing_state_name": "United States"}]}
    result = provider.Interpret("s", idcheck.JOIN, approved)
    assert result.outcome == idcheck.APPROVED and result.country == "United States" and result.reference == "d1"
