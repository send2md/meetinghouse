"""The ID check: proving each member is a real, single, live person.

Meetinghouse hands people to an outside service (see the design
document) and only keeps the outcome: verified or not, the date, the
country and the service's reference number, plus the live selfie as
the member's public photo. The service keeps the ID photos and the face
template it uses to spot one person joining twice.

Which service is used is set in config.py:

    MEETINGHOUSE_IDCHECK = "didit"     # the real service (adapter: DiditProvider)
    MEETINGHOUSE_IDCHECK = "practice"  # a stand-in for local testing ONLY:
                                       # it lets anyone approve themselves
    (unset)                            # ID checks unavailable: nobody can join

Every provider does the same few things: Start() returns the web
address to send the person to (and the service's own reference for the
session); a result later arrives as an IdResult, either pushed by the
service (a signed webhook) or fetched with Fetch() when the person comes
back; app.ApplyIdResult acts on it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Optional

JOIN = "join"
RENEWAL = "renewal"
RETAKE = "retake"  # a new live selfie after a face match (see facecheck.py)

APPROVED = "approved"
DECLINED = "declined"
DUPLICATE = "duplicate"


@dataclass
class IdResult:
    session_id: str
    outcome: str  # APPROVED, DECLINED or DUPLICATE
    reference: str = ""
    country: str = ""
    selfie: Optional[bytes] = None
    selfie_ext: str = "jpg"


class NotAvailable(Exception):
    """No ID-check service is configured, so nobody can join or renew yet."""


class PracticeProvider:
    """Stand-in for local testing: the person decides their own outcome."""

    name = "practice"
    label = "a practice ID check (testing only)"

    def Start(self, session_id, participant_id, purpose, return_url, url_for):
        return url_for("PracticeIdCheck", session_id=session_id), ""

    def Fetch(self, row):
        return None


class DiditProvider:
    """didit.me (https://docs.didit.me), configured in config.py:

    MEETINGHOUSE_DIDIT_API_KEY          the application's API key
    MEETINGHOUSE_DIDIT_WEBHOOK_SECRET   the webhook destination's secret
    MEETINGHOUSE_DIDIT_WORKFLOW_JOIN    workflow id: ID document + live selfie + face search
    MEETINGHOUSE_DIDIT_WORKFLOW_RENEWAL workflow id: live selfie matched to the stored face

    The member's Meetinghouse id is sent as Didit's `vendor_data`, which
    Didit uses to link sessions and to find the same face joining twice.
    """

    name = "didit"
    label = "Didit"
    base = "https://verification.didit.me"

    def __init__(self):
        self.api_key = os.environ.get("MEETINGHOUSE_DIDIT_API_KEY", "")
        self.secret = os.environ.get("MEETINGHOUSE_DIDIT_WEBHOOK_SECRET", "")
        self.workflows = {
            JOIN: os.environ.get("MEETINGHOUSE_DIDIT_WORKFLOW_JOIN", ""),
            RENEWAL: os.environ.get("MEETINGHOUSE_DIDIT_WORKFLOW_RENEWAL", ""),
        }
        self.workflows[RETAKE] = os.environ.get("MEETINGHOUSE_DIDIT_WORKFLOW_RETAKE", "") or self.workflows[RENEWAL]

    # -- talking to Didit --

    def _Request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"x-api-key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())

    def Start(self, session_id, participant_id, purpose, return_url, url_for):
        if not self.api_key or not self.workflows.get(purpose):
            raise NotAvailable("The ID check isn't fully set up yet (missing Didit keys in config.py).")
        try:
            answer = self._Request(
                "POST",
                "/v3/session/",
                {
                    "workflow_id": self.workflows[purpose],
                    "vendor_data": participant_id,
                    "callback": return_url,
                    "metadata": {"meetinghouse_session": session_id, "purpose": purpose},
                },
            )
        except Exception as exc:
            raise NotAvailable(f"Couldn't reach the ID-check service just now. Please try again in a few minutes. ({exc})")
        return answer["url"], answer["session_id"]

    def Fetch(self, row):
        """Ask Didit for a decision (used when the person returns, in case the webhook is slow)."""
        if not row["reference"]:
            return None
        decision = self._Request("GET", f"/v3/session/{row['reference']}/decision/")
        return self.Interpret(row["id"], row["purpose"], decision)

    def Interpret(self, session_id, purpose, decision):
        """Turn a Didit decision into an IdResult, or None while it isn't decided."""
        status = decision.get("status", "")
        if status in ("Not Started", "In Progress", "Awaiting User", "In Review", "Resubmitted"):
            return None
        reference = decision.get("session_id", "")
        if status != "Approved":
            return IdResult(session_id=session_id, outcome=DECLINED, reference=reference)

        ids = decision.get("id_verifications") or []
        lives = decision.get("liveness_checks") or []
        if purpose == JOIN:
            for item in ids + lives:
                for match in item.get("matches") or []:
                    if match.get("session_id") != reference and match.get("status") == "Approved":
                        return IdResult(session_id=session_id, outcome=DUPLICATE, reference=reference)
        country = next((i.get("issuing_state_name") or i.get("issuing_state") for i in ids if i), "") or ""
        selfie_url = next((l.get("reference_image") for l in lives if l.get("reference_image")), None)
        if not selfie_url:
            matches = decision.get("face_matches") or []
            selfie_url = next((m.get("target_image") for m in matches if m.get("target_image")), None)
        selfie = None
        if selfie_url:
            try:
                with urllib.request.urlopen(selfie_url, timeout=30) as response:
                    selfie = response.read()
            except Exception:
                selfie = None
        return IdResult(
            session_id=session_id,
            outcome=APPROVED,
            reference=reference,
            country=country,
            selfie=selfie,
            selfie_ext="jpg",
        )

    # -- webhooks --

    def VerifyWebhook(self, raw_body, headers, now_epoch):
        """Return the parsed body if Didit really sent it (and recently), else None."""
        if not self.secret:
            return None
        try:
            timestamp = int(headers.get("X-Timestamp", "0"))
        except ValueError:
            return None
        if abs(now_epoch - timestamp) > 300:
            return None
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        key = self.secret.encode()
        v2 = headers.get("X-Signature-V2", "")
        if v2:
            canonical = json.dumps(_WholeFloatsToInts(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if hmac.compare_digest(v2, hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()):
                return body
        raw = headers.get("X-Signature", "")
        if raw and hmac.compare_digest(raw, hmac.new(key, raw_body, hashlib.sha256).hexdigest()):
            return body
        return None


def _WholeFloatsToInts(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _WholeFloatsToInts(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_WholeFloatsToInts(v) for v in value]
    return value


_PROVIDERS = {"practice": PracticeProvider, "didit": DiditProvider}


def GetProviders():
    """The approved ID-check companies Members may choose from, in the order configured.

    MEETINGHOUSE_IDCHECK lists them, separated by commas, e.g. "didit".
    The practice check approves anyone, so it is for testing only and
    must never be listed on the live site.
    """
    names = [n.strip() for n in os.environ.get("MEETINGHOUSE_IDCHECK", "").split(",") if n.strip()]
    return [_PROVIDERS[n]() for n in dict.fromkeys(names) if n in _PROVIDERS]


def GetProvider(name=None):
    """The approved provider called `name`, or (with no name) the first one. None if not approved."""
    providers = GetProviders()
    if name is None:
        return providers[0] if providers else None
    return next((p for p in providers if p.name == name), None)


def _PracticeSelfie(size=96):
    """A plain grey square PNG, standing in for a selfie in the practice check."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + b"\xb4\xb4\xb4" * size
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


PRACTICE_SELFIE = _PracticeSelfie()
