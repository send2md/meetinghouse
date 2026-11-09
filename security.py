"""Login security: passkeys, joining, email recovery, and limits on guessing.

Like credentials (see persistence.py), none of this is a Meetinghouse
domain concept, so it lives in its own tables beside the engine's.

* Joining. A new account can log in but can't take part until it has
  added a passkey and passed the ID check (see idcheck.py); then it is
  activated. Accounts from before this existed count as activated.

* Passkeys (WebAuthn). Logging in needs the member's own phone or
  computer plus its face/fingerprint/PIN unlock. Nothing secret is
  stored here: only each passkey's public key. There are no passwords;
  accounts made before passkeys keep theirs only until they add one.

* Recovery by email. A lost device is handled by an emailed one-time
  code that works only 48 hours after it was requested, while the same
  email offers a link to cancel it: a stranger in someone's mailbox
  can't take over their account before they notice.

Codes are stored only as salted hashes, and every guessable secret
(passwords, codes, the admin password) is behind a lockout after
repeated wrong tries.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_status (
    participant_id TEXT PRIMARY KEY REFERENCES participants(id),
    activated_at TEXT
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

CREATE TABLE IF NOT EXISTS member_info (
    participant_id TEXT PRIMARY KEY REFERENCES participants(id),
    email TEXT NOT NULL DEFAULT '',
    town TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    photo_path TEXT NOT NULL DEFAULT '',
    verified_at TEXT,
    verified_country TEXT NOT NULL DEFAULT '',
    verified_ref TEXT NOT NULL DEFAULT '',
    last_login_at TEXT,
    reminders_sent TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS id_sessions (
    id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants(id),
    purpose TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    reference TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS recovery_requests (
    id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants(id),
    code_hash TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    usable_from TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting'
);
"""

RECOVERY_WAIT = dt.timedelta(hours=48)
RECOVERY_VALID = dt.timedelta(days=7)

# Codes avoid look-alike characters (no I, O, 0 or 1) since people
# copy them by hand from an email.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

MAX_FAILURES = 5
LOCKOUT = dt.timedelta(minutes=15)

# An unusable password hash: check_password_hash never matches it. Used
# when an administrator resets a login, so the old password stops working.
NO_PASSWORD = "!disabled"


def InitSchema(conn):
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(member_info)")}
    for column in ("zip", "country", "face_print"):
        if column not in columns:
            conn.execute(f"ALTER TABLE member_info ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _Iso(value):
    return None if value is None else value.isoformat()


def _Parse(value):
    return None if value is None else dt.datetime.fromisoformat(value)


# --- Activation ------------------------------------------------------------


def EnsureStatusRows(conn, house, now):
    """Give every Participant an account_status row, grandfathering old ones.

    Members who registered before activation-by-mail existed already
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


# --- One-time codes (emailed for recovery) ----------------------------------------


def NormalizeCode(raw: str) -> str:
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def FormatCode(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


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


# --- Member details ------------------------------------------------------------


def GetMember(conn, participant_id):
    """Email, town, photo and verification details (a row, possibly empty)."""
    conn.execute("INSERT OR IGNORE INTO member_info (participant_id) VALUES (?)", (participant_id,))
    return conn.execute("SELECT * FROM member_info WHERE participant_id = ?", (participant_id,)).fetchone()


def UpdateMember(conn, participant_id, **fields):
    allowed = {"email", "town", "zip", "country", "photo_path", "verified_at", "verified_country", "verified_ref", "last_login_at", "reminders_sent"}
    assert set(fields) <= allowed, fields
    GetMember(conn, participant_id)
    for key, value in fields.items():
        if isinstance(value, dt.datetime):
            value = _Iso(value)
        conn.execute(f"UPDATE member_info SET {key} = ? WHERE participant_id = ?", (value, participant_id))
    conn.commit()


def RecordLogin(conn, participant_id, now):
    """Remember this login; return when they last logged in before it (or None)."""
    previous = _Parse(GetMember(conn, participant_id)["last_login_at"])
    UpdateMember(conn, participant_id, last_login_at=now)
    return previous


# --- ID-check sessions -----------------------------------------------------------


def StartIdSession(conn, participant_id, purpose, provider, now):
    session_id = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO id_sessions (id, participant_id, purpose, provider, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
        (session_id, participant_id, purpose, provider, _Iso(now)),
    )
    conn.commit()
    return session_id


def SetIdSessionReference(conn, session_id, reference):
    conn.execute("UPDATE id_sessions SET reference = ? WHERE id = ?", (reference, session_id))
    conn.commit()


def FindIdSessionByReference(conn, reference):
    return conn.execute("SELECT * FROM id_sessions WHERE reference = ?", (reference,)).fetchone()


def GetIdSession(conn, session_id):
    return conn.execute("SELECT * FROM id_sessions WHERE id = ?", (session_id,)).fetchone()


def LatestIdSession(conn, participant_id, purpose):
    return conn.execute(
        "SELECT * FROM id_sessions WHERE participant_id = ? AND purpose = ? ORDER BY created_at DESC LIMIT 1",
        (participant_id, purpose),
    ).fetchone()


def FinishIdSession(conn, session_id, status, reference, now):
    conn.execute(
        "UPDATE id_sessions SET status = ?, reference = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
        (status, reference, _Iso(now), session_id),
    )
    conn.commit()


# --- Recovery by email -------------------------------------------------------------


def RequestRecovery(conn, participant_id, now):
    """Start a recovery: returns (cancel token, code). The code works only after RECOVERY_WAIT."""
    conn.execute(
        "UPDATE recovery_requests SET state = 'replaced' WHERE participant_id = ? AND state = 'waiting'",
        (participant_id,),
    )
    token = secrets.token_urlsafe(24)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    conn.execute(
        """
        INSERT INTO recovery_requests (id, participant_id, code_hash, requested_at, usable_from, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            token,
            participant_id,
            generate_password_hash(code, method="pbkdf2:sha256"),
            _Iso(now),
            _Iso(now + RECOVERY_WAIT),
            _Iso(now + RECOVERY_VALID),
        ),
    )
    conn.commit()
    return token, code


def PendingRecovery(conn, participant_id):
    row = conn.execute(
        "SELECT * FROM recovery_requests WHERE participant_id = ? AND state = 'waiting' ORDER BY requested_at DESC LIMIT 1",
        (participant_id,),
    ).fetchone()
    if row is None:
        return None
    return {"requested_at": _Parse(row["requested_at"]), "usable_from": _Parse(row["usable_from"]), "expires_at": _Parse(row["expires_at"])}


def CancelRecovery(conn, token):
    """Cancel a waiting recovery from the link in its email; returns the participant id or None."""
    row = conn.execute("SELECT * FROM recovery_requests WHERE id = ? AND state = 'waiting'", (token,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE recovery_requests SET state = 'cancelled' WHERE id = ?", (token,))
    conn.commit()
    return row["participant_id"]


def UseRecoveryCode(conn, participant_id, raw_code, now):
    """'ok' (code used up), 'too_soon' (before the 48-hour wait ends), or 'wrong'."""
    row = conn.execute(
        "SELECT * FROM recovery_requests WHERE participant_id = ? AND state = 'waiting' ORDER BY requested_at DESC LIMIT 1",
        (participant_id,),
    ).fetchone()
    if row is None or _Parse(row["expires_at"]) < now:
        return "wrong"
    code = NormalizeCode(raw_code)
    if len(code) != CODE_LENGTH or not check_password_hash(row["code_hash"], code):
        return "wrong"
    if now < _Parse(row["usable_from"]):
        return "too_soon"
    conn.execute("UPDATE recovery_requests SET state = 'used' WHERE id = ?", (row["id"],))
    conn.commit()
    return "ok"


def ForgetMember(conn, participant_id):
    """A Member left: delete their passkeys, cancel any login recovery, and
    erase their email address and photo. Their name, town and the record of
    their ID check (date, country, reference) stay, as the public record does."""
    RemoveAllPasskeys(conn, participant_id)
    conn.execute(
        "UPDATE recovery_requests SET state = 'cancelled' WHERE participant_id = ? AND state = 'waiting'",
        (participant_id,),
    )
    member = GetMember(conn, participant_id)
    if member is not None and member["photo_path"]:
        try:
            os.remove(member["photo_path"])
        except OSError:
            pass
    conn.execute(
        "UPDATE member_info SET email = '', photo_path = '', reminders_sent = '' WHERE participant_id = ?",
        (participant_id,),
    )
    conn.commit()


def SetFacePrint(conn, participant_id, encoded):
    conn.execute("UPDATE member_info SET face_print = ? WHERE participant_id = ?", (encoded or "", participant_id))
    conn.commit()


def FacePrints(conn):
    """{participant id: encoded face print} for everyone with one kept."""
    return {
        row[0]: row[1]
        for row in conn.execute("SELECT participant_id, face_print FROM member_info WHERE face_print != ''")
    }
