"""Login security: mailed one-time codes, passkeys, and limits on guessing.

Like credentials (see persistence.py), none of this is a Meetinghouse
domain concept, so it lives in its own tables beside the engine's.

Three ideas, each answering "is this really the right person?":

* Activation by mail. A new registration can log in but cannot take
  part until it enters a one-time code that the administrator mailed to
  the postal address it gave. That ties each account to a real address,
  which makes fake or duplicate accounts much harder to create.

* Passkeys (WebAuthn). Once a Participant adds a passkey, logging in
  needs their own phone or computer plus its face/fingerprint/PIN
  unlock; their password stops working for that account. Nothing
  secret is stored here: only each passkey's public key.

* Recovery by mail. Losing a device (or a password) is handled by the
  administrator resetting the login and mailing a fresh one-time code,
  never by email or security questions an impostor could answer.

Mailed codes and passwords are stored only as salted hashes, and every
guessable secret (passwords, mailed codes, the admin password) is
behind a lockout after repeated wrong tries.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_status (
    participant_id TEXT PRIMARY KEY REFERENCES participants(id),
    activated_at TEXT
);

CREATE TABLE IF NOT EXISTS mailed_codes (
    participant_id TEXT PRIMARY KEY REFERENCES participants(id),
    purpose TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS passkeys (
    credential_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants(id),
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS login_throttle (
    key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    locked_until TEXT
);
"""

ACTIVATION = "activation"
RECOVERY = "recovery"

# Codes avoid look-alike characters (no I, O, 0 or 1) since people type
# them in from a paper letter.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_VALID_DAYS = 90

MAX_FAILURES = 5
LOCKOUT = dt.timedelta(minutes=15)

# An unusable password hash: check_password_hash never matches it. Used
# when an administrator resets a login, so the old password stops working.
NO_PASSWORD = "!disabled"


def InitSchema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _Iso(value):
    return None if value is None else value.isoformat()


def _Parse(value):
    return None if value is None else dt.datetime.fromisoformat(value)


# --- Activation ------------------------------------------------------------


def EnsureStatusRows(conn, house, now):
    """Give every Participant an account_status row, grandfathering old ones.

    Participants who registered before activation-by-mail existed already
    have a signed form; they count as activated. Anyone without a form
    (which can only happen for registrations made after this change)
    stays waiting for their letter.
    """
    for participant in house.participants.values():
        activated = _Iso(now) if participant.forms else None
        conn.execute(
            "INSERT OR IGNORE INTO account_status (participant_id, activated_at) VALUES (?, ?)",
            (participant.id, activated),
        )
    conn.commit()


def CreatePendingStatus(conn, participant_id):
    conn.execute(
        "INSERT OR REPLACE INTO account_status (participant_id, activated_at) VALUES (?, NULL)",
        (participant_id,),
    )
    conn.commit()


def IsActivated(conn, participant_id) -> bool:
    row = conn.execute("SELECT activated_at FROM account_status WHERE participant_id = ?", (participant_id,)).fetchone()
    return row is not None and row["activated_at"] is not None


def ActivatedAt(conn, participant_id) -> Optional[dt.datetime]:
    row = conn.execute("SELECT activated_at FROM account_status WHERE participant_id = ?", (participant_id,)).fetchone()
    return None if row is None else _Parse(row["activated_at"])


def MarkActivated(conn, participant_id, now):
    conn.execute(
        """
        INSERT INTO account_status (participant_id, activated_at) VALUES (?, ?)
        ON CONFLICT(participant_id) DO UPDATE SET activated_at = excluded.activated_at
        """,
        (participant_id, _Iso(now)),
    )
    conn.commit()


# --- Mailed one-time codes -------------------------------------------------


def NormalizeCode(raw: str) -> str:
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def FormatCode(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def IssueCode(conn, participant_id, purpose, now) -> str:
    """Create a fresh code to mail, replacing any earlier one. Returns it once."""
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    conn.execute(
        """
        INSERT INTO mailed_codes (participant_id, purpose, code_hash, issued_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(participant_id) DO UPDATE SET
            purpose = excluded.purpose,
            code_hash = excluded.code_hash,
            issued_at = excluded.issued_at,
            expires_at = excluded.expires_at
        """,
        (
            participant_id,
            purpose,
            generate_password_hash(code, method="pbkdf2:sha256"),
            _Iso(now),
            _Iso(now + dt.timedelta(days=CODE_VALID_DAYS)),
        ),
    )
    conn.commit()
    return code


def PendingCode(conn, participant_id):
    """The outstanding code's purpose and dates (never the code), or None."""
    row = conn.execute(
        "SELECT purpose, issued_at, expires_at FROM mailed_codes WHERE participant_id = ?", (participant_id,)
    ).fetchone()
    if row is None:
        return None
    return {"purpose": row["purpose"], "issued_at": _Parse(row["issued_at"]), "expires_at": _Parse(row["expires_at"])}


def RedeemCode(conn, participant_id, raw_code, now) -> Optional[str]:
    """If `raw_code` is this Participant's live code, use it up and return its purpose."""
    row = conn.execute("SELECT * FROM mailed_codes WHERE participant_id = ?", (participant_id,)).fetchone()
    if row is None or _Parse(row["expires_at"]) < now:
        return None
    code = NormalizeCode(raw_code)
    if len(code) != CODE_LENGTH or not check_password_hash(row["code_hash"], code):
        return None
    conn.execute("DELETE FROM mailed_codes WHERE participant_id = ?", (participant_id,))
    conn.commit()
    return row["purpose"]


# --- Passkeys ----------------------------------------------------------------


def ListPasskeys(conn, participant_id):
    return conn.execute(
        "SELECT * FROM passkeys WHERE participant_id = ? ORDER BY created_at", (participant_id,)
    ).fetchall()


def HasPasskeys(conn, participant_id) -> bool:
    return conn.execute("SELECT 1 FROM passkeys WHERE participant_id = ? LIMIT 1", (participant_id,)).fetchone() is not None


def GetPasskey(conn, credential_id):
    return conn.execute("SELECT * FROM passkeys WHERE credential_id = ?", (credential_id,)).fetchone()


def AddPasskey(conn, participant_id, credential_id, public_key, sign_count, label, now):
    conn.execute(
        """
        INSERT INTO passkeys (credential_id, participant_id, public_key, sign_count, label, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (credential_id, participant_id, public_key, sign_count, label, _Iso(now)),
    )
    conn.commit()


def TouchPasskey(conn, credential_id, sign_count, now):
    conn.execute(
        "UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE credential_id = ?",
        (sign_count, _Iso(now), credential_id),
    )
    conn.commit()


def RemovePasskey(conn, participant_id, credential_id):
    conn.execute(
        "DELETE FROM passkeys WHERE participant_id = ? AND credential_id = ?", (participant_id, credential_id)
    )
    conn.commit()


def RemoveAllPasskeys(conn, participant_id):
    conn.execute("DELETE FROM passkeys WHERE participant_id = ?", (participant_id,))
    conn.commit()


# --- Limits on guessing ------------------------------------------------------


def LockedUntil(conn, key, now) -> Optional[dt.datetime]:
    """When `key` (an account or the admin login) is locked out, until when."""
    row = conn.execute("SELECT locked_until FROM login_throttle WHERE key = ?", (key,)).fetchone()
    if row is None or row["locked_until"] is None:
        return None
    until = _Parse(row["locked_until"])
    return until if until > now else None


def RecordFailure(conn, key, now) -> Optional[dt.datetime]:
    """Count one wrong try; returns the lockout end if this one triggered it."""
    row = conn.execute("SELECT failures, locked_until FROM login_throttle WHERE key = ?", (key,)).fetchone()
    failures = 0 if row is None else row["failures"]
    failures += 1
    locked_until = None
    if failures >= MAX_FAILURES:
        locked_until = now + LOCKOUT
        failures = 0
    conn.execute(
        """
        INSERT INTO login_throttle (key, failures, locked_until) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET failures = excluded.failures, locked_until = excluded.locked_until
        """,
        (key, failures, _Iso(locked_until)),
    )
    conn.commit()
    return locked_until


def ClearFailures(conn, key):
    conn.execute("DELETE FROM login_throttle WHERE key = ?", (key,))
    conn.commit()
