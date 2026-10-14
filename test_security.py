"""Tests for login security: activation letters, passkeys, recovery, lockouts.

Passkeys are exercised end to end with SoftAuthenticator, a tiny
software stand-in for a phone or laptop's passkey hardware: it makes a
real P-256 key pair and real signed WebAuthn responses, so the server
code under test runs exactly as it would against a browser.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import os
import re
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

from test_app import _AdminLogin, _Login, _Register, client  # noqa: F401  (client is a fixture)

ORIGIN = "http://localhost"
PASSWORD = "correct horse battery"


class SoftAuthenticator:
    def __init__(self, origin=ORIGIN):
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.count = 0

    @property
    def id(self):
        return bytes_to_base64url(self.credential_id)

    def _cose_public_key(self):
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})

    def create(self, options):
        client_data = json.dumps(
            {"type": "webauthn.create", "challenge": options["challenge"], "origin": self.origin, "crossOrigin": False}
        ).encode()
        auth_data = (
            hashlib.sha256(options["rp"]["id"].encode()).digest()
            + bytes([0x01 | 0x04 | 0x40])  # user present, user verified, has credential data
            + struct.pack(">I", self.count)
            + b"\x00" * 16
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_public_key()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }

    def get(self, options, rp_id="localhost", user_verified=True, signing_key=None):
        self.count += 1
        client_data = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": self.origin}).encode()
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([0x01 | (0x04 if user_verified else 0)])
            + struct.pack(">I", self.count)
        )
        key = signing_key or self.key
        signature = key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }


def _Pid(app_module, name="Jane Doe"):
    return next(p.id for p in app_module.house.participants.values() if p.name == name)


def _PrintLetter(test_client, participant_id, purpose="activation"):
    """As admin, print a letter and return the code shown on it."""
    _AdminLogin(test_client)
    resp = test_client.post(f"/admin/participant/{participant_id}/letter", data={"purpose": purpose})
    assert resp.status_code == 200
    match = re.search(rb'class="code">([A-Z0-9]{4}-[A-Z0-9]{4})<', resp.data)
    assert match, resp.data
    test_client.get("/admin/logout")
    return match.group(1).decode()


def _LoginWithCode(test_client, username, code):
    test_client.post("/login", data={"username": username})
    return test_client.post("/login/code", data={"code": code}, follow_redirects=True)


def _AddPasskey(test_client, authenticator, label="Test device"):
    options = test_client.post("/account/passkey/options", json={}).get_json()
    return test_client.post(
        "/account/passkey/verify", json={"label": label, "credential": authenticator.create(options)}
    )


def _PasskeyLogin(test_client, username, authenticator, **get_kwargs):
    step1 = test_client.post("/login", data={"username": username})
    assert step1.headers["Location"].endswith("/login/passkey")
    options = test_client.post("/login/passkey/options", json={}).get_json()
    return test_client.post("/login/passkey/verify", json={"credential": authenticator.get(options, **get_kwargs)})


def _ActivatedUser(test_client, app_module, username="janedoe", name="Jane Doe"):
    _Register(test_client, username, name=name)
    test_client.get("/logout")
    code = _PrintLetter(test_client, _Pid(app_module, name))
    _LoginWithCode(test_client, username, code)
    return _Pid(app_module, name)


# --- Activation by mail -----------------------------------------------------


def test_new_registration_waits_for_activation(client):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    pid = _Pid(app_module)
    assert not app_module.security.IsActivated(app_module.conn, pid)
    participant = app_module.house.participants[pid]
    assert not participant.IsActive(app_module.Now())

    dashboard = test_client.get("/")
    assert b"isn&#39;t activated yet" in dashboard.data or b"isn't activated yet" in dashboard.data

    test_client.post("/topic/new/forum", data={"title": "Should not appear"}, follow_redirects=True)
    assert not any(t.title == "Should not appear" for t in app_module.house.topics.values())


def test_activation_letter_code_logs_in_and_activates(client):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")
    pid = _Pid(app_module)
    code = _PrintLetter(test_client, pid)

    resp = _LoginWithCode(test_client, "janedoe", code.lower().replace("-", " "))  # forgiving about case/spacing
    assert b"Your account is activated" in resp.data
    assert b"add a passkey" in resp.data
    assert app_module.security.IsActivated(app_module.conn, pid)
    assert app_module.house.participants[pid].IsActive(app_module.Now())

    test_client.post("/topic/new/forum", data={"title": "First topic"})
    assert any(t.title == "First topic" for t in app_module.house.topics.values())


def test_activation_code_typed_on_the_dashboard(client):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    pid = _Pid(app_module)
    code = _PrintLetter(test_client, pid)
    _Login(test_client, "janedoe", PASSWORD)
    resp = test_client.post("/account/activate", data={"code": code}, follow_redirects=True)
    assert b"Your account is activated" in resp.data
    assert app_module.security.IsActivated(app_module.conn, pid)


def test_codes_work_once_and_new_letters_replace_old_codes(client):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")
    pid = _Pid(app_module)
    first = _PrintLetter(test_client, pid)
    second = _PrintLetter(test_client, pid)
    assert first != second

    assert b"didn" in _LoginWithCode(test_client, "janedoe", first).data  # replaced
    assert b"activated" in _LoginWithCode(test_client, "janedoe", second).data
    test_client.get("/logout")
    assert b"didn" in _LoginWithCode(test_client, "janedoe", second).data  # used up


def test_expired_code_is_refused(client, monkeypatch):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")
    code = _PrintLetter(test_client, _Pid(app_module))
    later = app_module.Now() + dt.timedelta(days=91)
    monkeypatch.setattr(app_module, "Now", lambda: later)
    assert b"didn" in _LoginWithCode(test_client, "janedoe", code).data


def test_admin_can_activate_in_person(client):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")
    pid = _Pid(app_module)
    _AdminLogin(test_client)
    page = test_client.get("/admin")
    assert b"Waiting for activation" in page.data and b"123 Main St" in page.data
    test_client.post(f"/admin/participant/{pid}/activate")
    assert app_module.security.IsActivated(app_module.conn, pid)
    assert app_module.house.participants[pid].IsActive(app_module.Now())


def test_admin_cannot_sign_form_for_unactivated_participant(client):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")
    pid = _Pid(app_module)
    _AdminLogin(test_client)
    resp = test_client.post("/admin/sign_form", data={"participant_id": pid}, follow_redirects=True)
    assert b"activated yet" in resp.data
    assert not app_module.house.participants[pid].forms


def test_existing_participants_are_grandfathered_as_activated(client, tmp_path):
    test_client, app_module = client
    _Register(test_client, "olduser", name="Old User")
    _Register(test_client, "newuser", name="New User")
    old_pid, new_pid = _Pid(app_module, "Old User"), _Pid(app_module, "New User")
    # Simulate a database from before this feature: Old User had a signed
    # form, and nobody had account_status rows at all.
    app_module.house.participants[old_pid].SignForm(app_module.Now().date())
    app_module.persistence.SaveParticipant(app_module.conn, app_module.house.participants[old_pid])
    app_module.conn.execute("DROP TABLE account_status")
    app_module.conn.commit()

    importlib.reload(app_module)
    assert app_module.security.IsActivated(app_module.conn, old_pid)
    assert not app_module.security.IsActivated(app_module.conn, new_pid)


# --- Passkeys ---------------------------------------------------------------


def test_passkey_setup_and_login(client):
    test_client, app_module = client
    pid = _ActivatedUser(test_client, app_module)
    device = SoftAuthenticator()

    resp = _AddPasskey(test_client, device, label="Jane's phone")
    assert resp.status_code == 200 and resp.get_json()["ok"]
    stored = app_module.security.ListPasskeys(app_module.conn, pid)
    assert [p["label"] for p in stored] == ["Jane's phone"]

    test_client.get("/logout")
    resp = _PasskeyLogin(test_client, "janedoe", device)
    assert resp.status_code == 200, resp.get_json()
    assert test_client.get("/account").status_code == 200  # logged in
    assert app_module.security.GetPasskey(app_module.conn, device.id)["sign_count"] == device.count


def test_password_stops_working_once_a_passkey_exists(client):
    test_client, app_module = client
    _ActivatedUser(test_client, app_module)
    test_client.post("/account/password", data={"password": PASSWORD, "confirm": PASSWORD})
    _AddPasskey(test_client, SoftAuthenticator())
    test_client.get("/logout")

    test_client.post("/login", data={"username": "janedoe"})
    resp = test_client.post("/login/password", data={"password": PASSWORD})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login/passkey")
    assert test_client.get("/account").status_code == 302  # still logged out


def test_passkey_with_wrong_key_is_refused(client):
    test_client, app_module = client
    _ActivatedUser(test_client, app_module)
    device = SoftAuthenticator()
    _AddPasskey(test_client, device)
    test_client.get("/logout")

    impostor_key = ec.generate_private_key(ec.SECP256R1())
    resp = _PasskeyLogin(test_client, "janedoe", device, signing_key=impostor_key)
    assert resp.status_code == 400
    assert test_client.get("/account").status_code == 302


def test_passkey_without_face_fingerprint_or_pin_is_refused(client):
    test_client, app_module = client
    _ActivatedUser(test_client, app_module)
    device = SoftAuthenticator()
    _AddPasskey(test_client, device)
    test_client.get("/logout")
    resp = _PasskeyLogin(test_client, "janedoe", device, user_verified=False)
    assert resp.status_code == 400


def test_passkey_from_another_site_is_refused(client):
    test_client, app_module = client
    _ActivatedUser(test_client, app_module)
    device = SoftAuthenticator()
    _AddPasskey(test_client, device)
    test_client.get("/logout")
    phishing = SoftAuthenticator(origin="https://meetinghouse-login.example")
    phishing.key, phishing.credential_id = device.key, device.credential_id
    resp = _PasskeyLogin(test_client, "janedoe", phishing)
    assert resp.status_code == 400


def test_someone_elses_passkey_cannot_log_into_this_account(client):
    test_client, app_module = client
    _ActivatedUser(test_client, app_module, "janedoe", "Jane Doe")
    _AddPasskey(test_client, SoftAuthenticator())
    test_client.get("/logout")
    _ActivatedUser(test_client, app_module, "bob", "Bob Roe")
    bobs_device = SoftAuthenticator()
    _AddPasskey(test_client, bobs_device)
    test_client.get("/logout")

    test_client.post("/login", data={"username": "janedoe"})
    options = test_client.post("/login/passkey/options", json={}).get_json()
    resp = test_client.post("/login/passkey/verify", json={"credential": bobs_device.get(options)})
    assert resp.status_code == 400


def test_challenge_cannot_be_replayed(client):
    test_client, app_module = client
    _ActivatedUser(test_client, app_module)
    device = SoftAuthenticator()
    _AddPasskey(test_client, device)
    test_client.get("/logout")
    test_client.post("/login", data={"username": "janedoe"})
    options = test_client.post("/login/passkey/options", json={}).get_json()
    answer = device.get(options)
    assert test_client.post("/login/passkey/verify", json={"credential": answer}).status_code == 200
    test_client.get("/logout")
    test_client.post("/login", data={"username": "janedoe"})
    assert test_client.post("/login/passkey/verify", json={"credential": answer}).status_code == 400


def test_removing_last_passkey_brings_password_back(client):
    test_client, app_module = client
    pid = _ActivatedUser(test_client, app_module)
    test_client.post("/account/password", data={"password": PASSWORD, "confirm": PASSWORD})
    device = SoftAuthenticator()
    _AddPasskey(test_client, device)
    resp = test_client.post("/account/passkey/remove", data={"credential_id": device.id}, follow_redirects=True)
    assert b"password logs you in again" in resp.data
    assert not app_module.security.HasPasskeys(app_module.conn, pid)


# --- Recovery ---------------------------------------------------------------


def test_admin_reset_and_recovery_letter(client):
    test_client, app_module = client
    pid = _ActivatedUser(test_client, app_module)
    test_client.post("/account/password", data={"password": PASSWORD, "confirm": PASSWORD})
    lost_device = SoftAuthenticator()
    _AddPasskey(test_client, lost_device)
    test_client.get("/logout")

    code = _PrintLetter(test_client, pid, purpose="recovery")
    assert not app_module.security.HasPasskeys(app_module.conn, pid)

    # The old password no longer works either.
    resp = _Login(test_client, "janedoe", PASSWORD)
    assert b"that password didn" in resp.data.lower()

    resp = _LoginWithCode(test_client, "janedoe", code)
    assert b"Welcome back" in resp.data
    assert b"no way to log in next time" in resp.data  # nudged to add a passkey or password
    new_device = SoftAuthenticator()
    assert _AddPasskey(test_client, new_device).status_code == 200
    test_client.get("/logout")
    assert _PasskeyLogin(test_client, "janedoe", new_device).status_code == 200


# --- Limits on guessing -----------------------------------------------------


def test_password_guessing_is_locked_out(client):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")
    for _ in range(5):
        resp = _Login(test_client, "janedoe", "wrong guess")
    assert b"paused" in resp.data
    resp = _Login(test_client, "janedoe", PASSWORD)
    assert b"paused" in resp.data
    assert test_client.get("/account").status_code == 302  # not logged in


def test_lockout_ends_after_fifteen_minutes(client, monkeypatch):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")
    for _ in range(5):
        _Login(test_client, "janedoe", "wrong guess")
    later = app_module.Now() + dt.timedelta(minutes=16)
    monkeypatch.setattr(app_module, "Now", lambda: later)
    resp = _Login(test_client, "janedoe", PASSWORD)
    assert b"Welcome, Jane Doe" in resp.data


def test_code_guessing_is_locked_out(client):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")
    code = _PrintLetter(test_client, _Pid(app_module))
    for _ in range(5):
        resp = _LoginWithCode(test_client, "janedoe", "AAAA-AAAA")
    assert b"paused" in resp.data
    assert b"paused" in _LoginWithCode(test_client, "janedoe", code).data


def test_admin_password_guessing_is_locked_out(client):
    test_client, _ = client
    for _ in range(5):
        resp = test_client.post("/admin/login", data={"password": "nope"}, follow_redirects=True)
    assert b"paused" in resp.data
    resp = test_client.post("/admin/login", data={"password": "test-admin-password"}, follow_redirects=True)
    assert b"paused" in resp.data


def test_next_parameter_cannot_send_people_to_another_site(client):
    test_client, _ = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")
    test_client.post("/login", data={"username": "janedoe"})
    resp = test_client.post("/login/password?next=https://evil.example/", data={"password": PASSWORD})
    assert resp.headers["Location"] == "/"
