"""Helpers the web tests share: a software passkey, and the steps of joining.

SoftAuthenticator is a tiny software stand-in for a phone or laptop's
passkey hardware: it makes a real P-256 key pair and real signed WebAuthn
responses, so the server code under test runs exactly as it would
against a browser.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url
from werkzeug.security import generate_password_hash

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


def Pid(app_module, name="Jane Doe"):
    return next(p.id for p in app_module.house.participants.values() if p.name == name)


def Register(test_client, username, name="Jane Doe", **overrides):
    """Step one of joining: /login with a new user name, then the /register form."""
    test_client.post("/login", data={"username": username})
    data = {"name": name, "email": f"{username}@example.com", "town": "Ithaca", "zip": "14850", "country": "United States", "agree": "1"}
    data.update(overrides)
    return test_client.post("/register", data=data, follow_redirects=True)


def AddPasskey(test_client, authenticator, label="Test device"):
    options = test_client.post("/account/passkey/options", json={}).get_json()
    return test_client.post("/account/passkey/verify", json={"label": label, "credential": authenticator.create(options)})


def PasskeyLogin(test_client, username, authenticator, **get_kwargs):
    step1 = test_client.post("/login", data={"username": username})
    assert step1.headers["Location"].endswith("/login/passkey"), step1.headers.get("Location")
    options = test_client.post("/login/passkey/options", json={}).get_json()
    return test_client.post("/login/passkey/verify", json={"credential": authenticator.get(options, **get_kwargs)})


def PracticeCheck(test_client, outcome="approved", url="/join/idcheck", provider="practice"):
    """Start an ID check and answer the practice page with `outcome`. Returns the final page."""
    start = test_client.post(url, data={"provider": provider})
    assert "/idcheck/practice/" in start.headers.get("Location", ""), start.headers.get("Location")
    return test_client.post(start.headers["Location"], data={"outcome": outcome}, follow_redirects=True)


def Join(test_client, app_module, username="janedoe", name="Jane Doe"):
    """Join completely (details, passkey, practice ID check). Returns (participant id, passkey)."""
    Register(test_client, username, name=name)
    device = SoftAuthenticator()
    assert AddPasskey(test_client, device).status_code == 200
    page = PracticeCheck(test_client)
    assert b"membership" in page.data.lower(), page.data
    return Pid(app_module, name), device


def As(test_client, username, device):
    test_client.get("/logout")
    assert PasskeyLogin(test_client, username, device).status_code == 200


def LegacyMember(app_module, username="olduser", name="Old User", password=PASSWORD):
    """A member from before passkeys: activated, with a password and no passkey."""
    house, conn = app_module.house, app_module.conn
    participant = house.RegisterParticipant(name)
    participant.SignForm(app_module.Now().date())
    app_module.persistence.SaveParticipant(conn, participant)
    app_module.persistence.SetCredentials(conn, participant.id, username, generate_password_hash(password))
    app_module.security.CreatePendingStatus(conn, participant.id)
    app_module.security.MarkActivated(conn, participant.id, app_module.Now())
    return participant.id


def PasswordLogin(test_client, username, password, follow_redirects=True):
    test_client.post("/login", data={"username": username})
    return test_client.post("/login/password", data={"password": password}, follow_redirects=follow_redirects)
