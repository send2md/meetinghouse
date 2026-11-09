"""SQLite persistence for the Meetinghouse engine.

meetinghouse.py is a pure, in-memory domain engine by design (see its
module docstring): every action takes an explicit `now` and nothing in
it touches a clock, a disk, or a network. This module is the "someone
else's job" the engine docstring defers to: it snapshots each mutated
Participant/Topic to SQLite after every engine call, and can rebuild
an equivalent in-memory Meetinghouse from those snapshots on startup.

Login credentials are a web-app concern, not a Meetinghouse domain
concept (the real Participation form arrives by mail, not a signup
form), so they live in their own table here rather than on the
Participant dataclass.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
from typing import Optional

from meetinghouse import (
    MINIMUM_LIFE,
    SEASONING_TIME,
    DiscussionPost,
    Dismissal,
    DismissalNote,
    Meetinghouse,
    Participant,
    ParticipationForm,
    PollResult,
    RuleChange,
    RuleConfig,
    Subsection,
    Topic,
    VoteChoice,
    VoteRecord,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    forms_json TEXT NOT NULL,
    dismissals_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    participant_id TEXT PRIMARY KEY REFERENCES participants(id),
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participant_profiles (
    participant_id TEXT PRIMARY KEY REFERENCES participants(id),
    address TEXT NOT NULL,
    mothers_maiden_name TEXT NOT NULL,
    age INTEGER NOT NULL,
    photo_path TEXT NOT NULL,
    thumbprint_path TEXT NOT NULL,
    charge_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    subsection TEXT PRIMARY KEY,
    quiet_time_seconds REAL NOT NULL,
    min_life_seconds REAL NOT NULL,
    voter_fraction REAL NOT NULL,
    approval_fraction REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    subsection TEXT NOT NULL,
    author_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    poll_question TEXT,
    created_at TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    target_subsection TEXT,
    target_participant_id TEXT,
    proposed_rule_config_json TEXT,
    readers_json TEXT NOT NULL,
    votes_json TEXT NOT NULL,
    closed INTEGER NOT NULL,
    closed_at TEXT,
    result TEXT,
    last_activity_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
"""


# Columns added to `topics` after its first release; added in place to
# databases created before them (see Connect).
_TOPIC_COLUMNS_ADDED = {
    "software_ref": "TEXT",
    "posts_json": "TEXT NOT NULL DEFAULT '[]'",
    "restarts_json": "TEXT NOT NULL DEFAULT '{}'",
    "dismissal_notes_json": "TEXT NOT NULL DEFAULT '[]'",
    "dismissal_effects_json": "TEXT NOT NULL DEFAULT '[]'",
    "pollster_id": "TEXT",
    "question_attached_at": "TEXT",
    "asked_to_vote_json": "TEXT NOT NULL DEFAULT '[]'",
    "same_as_id": "TEXT",
}


def Connect(path):
    """Open (and, if needed, initialize or upgrade) the SQLite store at `path`."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(topics)")}
    for column, kind in _TOPIC_COLUMNS_ADDED.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE topics ADD COLUMN {column} {kind}")
    participant_columns = {row["name"] for row in conn.execute("PRAGMA table_info(participants)")}
    if "left_at" not in participant_columns:
        conn.execute("ALTER TABLE participants ADD COLUMN left_at TEXT")
    log_columns = {row["name"] for row in conn.execute("PRAGMA table_info(event_log)")}
    for column in ("prev_hash", "hash"):
        if column not in log_columns:
            conn.execute(f"ALTER TABLE event_log ADD COLUMN {column} TEXT")
    conn.commit()
    _ChainUnhashedEntries(conn)
    return conn


# --- The public log ------------------------------------------------------------------
#
# Every entry carries the fingerprint (SHA-256) of the entry before it plus its
# own words, so each entry's fingerprint depends on everything logged before it.
# Change, add or remove any earlier entry and every later fingerprint changes.
# Anyone who saved a day's fingerprint can later check that the log still leads
# to it. The rule is simple enough to check by hand with any SHA-256 tool:
#
#     hash = sha256(prev_hash + "\n" + kind + "\n" + at + "\n" + data)
#
# where data is the entry's JSON exactly as stored, and the first entry's
# prev_hash is GENESIS.

GENESIS = "0" * 64

import threading

_LOG_LOCK = threading.Lock()  # one writer at a time within this process


def EntryHash(prev_hash, kind, at, data_json):
    import hashlib

    return hashlib.sha256(f"{prev_hash}\n{kind}\n{at}\n{data_json}".encode("utf-8")).hexdigest()


def _LastHash(conn):
    row = conn.execute("SELECT hash FROM event_log WHERE hash IS NOT NULL ORDER BY seq DESC LIMIT 1").fetchone()
    return row["hash"] if row else GENESIS


def _ChainUnhashedEntries(conn):
    """Give fingerprints to entries written before the log had them, in order."""
    rows = conn.execute("SELECT seq, kind, at, data_json FROM event_log WHERE hash IS NULL ORDER BY seq").fetchall()
    if not rows:
        return
    prev = _LastHash(conn)
    for row in rows:
        digest = EntryHash(prev, row["kind"], row["at"], row["data_json"])
        conn.execute("UPDATE event_log SET prev_hash = ?, hash = ? WHERE seq = ?", (prev, digest, row["seq"]))
        prev = digest
    conn.commit()


def _Iso(value):
    return None if value is None else value.isoformat()


def _ParseDateTime(value):
    return None if value is None else dt.datetime.fromisoformat(value)


def _ParseDate(value):
    return None if value is None else dt.date.fromisoformat(value)


def _RuleToDict(rule: RuleConfig):
    return {
        "quiet_time": rule.quiet_time.total_seconds(),
        "min_life": rule.min_life.total_seconds(),
        "voter_fraction": rule.voter_fraction,
        "approval_fraction": rule.approval_fraction,
    }


def _RuleFromDict(data):
    return RuleConfig(
        quiet_time=dt.timedelta(seconds=data["quiet_time"]),
        min_life=dt.timedelta(seconds=data["min_life"]),
        voter_fraction=data["voter_fraction"],
        approval_fraction=data["approval_fraction"],
    )


def _FormsToJson(forms):
    return json.dumps(
        [{"id": f.id, "signed_date": f.signed_date.isoformat(), "scan_ref": f.scan_ref} for f in forms]
    )


def _FormsFromJson(raw):
    return [
        ParticipationForm(signed_date=_ParseDate(f["signed_date"]), scan_ref=f["scan_ref"], id=f["id"])
        for f in json.loads(raw)
    ]


def _DismissalsToJson(dismissals):
    return json.dumps(
        [
            {"start": _Iso(d.start), "end": _Iso(d.end), "ordinal": d.ordinal, "topic_id": d.topic_id}
            for d in dismissals
        ]
    )


def _DismissalsFromJson(raw):
    return [
        Dismissal(start=_ParseDateTime(d["start"]), end=_ParseDateTime(d["end"]), ordinal=d["ordinal"], topic_id=d["topic_id"])
        for d in json.loads(raw)
    ]


def _ReadersToJson(readers):
    return json.dumps({pid: _Iso(when) for pid, when in readers.items()})


def _ReadersFromJson(raw):
    return {pid: _ParseDateTime(when) for pid, when in json.loads(raw).items()}


def _VotesToJson(votes):
    return json.dumps(
        {
            pid: [
                {
                    "participant_id": r.participant_id,
                    "choice": r.choice.value,
                    "comment": r.comment,
                    "timestamp": _Iso(r.timestamp),
                    "revoked": r.revoked,
                    "revoked_at": _Iso(r.revoked_at),
                    "removed_by": r.removed_by,
                }
                for r in records
            ]
            for pid, records in votes.items()
        }
    )


def _VotesFromJson(raw):
    votes = {}
    for pid, records in json.loads(raw).items():
        votes[pid] = [
            VoteRecord(
                participant_id=r["participant_id"],
                choice=VoteChoice(r["choice"]),
                comment=r["comment"],
                timestamp=_ParseDateTime(r["timestamp"]),
                revoked=r["revoked"],
                revoked_at=_ParseDateTime(r["revoked_at"]),
                removed_by=r.get("removed_by"),
            )
            for r in records
        ]
    return votes


def _ChangeToJson(change):
    if change is None:
        return None
    return json.dumps({"subsection": change.subsection.value, "setting": change.setting, "value": change.value})


def _ChangeFromJson(raw):
    """A one-setting change; Topics saved before those existed (a whole RuleConfig) load as None."""
    if not raw:
        return None
    data = json.loads(raw)
    if "setting" not in data:
        return None
    return RuleChange(subsection=Subsection(data["subsection"]), setting=data["setting"], value=data["value"])


def _PostsToJson(posts):
    return json.dumps(
        [
            {
                "id": p.id,
                "participant_id": p.participant_id,
                "text": p.text,
                "timestamp": _Iso(p.timestamp),
                "extended": p.extended,
                "near_identical_to": _Iso(p.near_identical_to),
            }
            for p in posts
        ]
    )


def _PostsFromJson(raw):
    return [
        DiscussionPost(
            id=p["id"],
            participant_id=p["participant_id"],
            text=p["text"],
            timestamp=_ParseDateTime(p["timestamp"]),
            extended=p["extended"],
            near_identical_to=_ParseDateTime(p["near_identical_to"]),
        )
        for p in json.loads(raw or "[]")
    ]


def _NotesToJson(notes):
    return json.dumps(
        [
            {
                "participant_id": n.participant_id,
                "jury_topic_id": n.jury_topic_id,
                "at": _Iso(n.at),
                "removed_choice": n.removed_choice.value if n.removed_choice else None,
            }
            for n in notes
        ]
    )


def _NotesFromJson(raw):
    return [
        DismissalNote(
            participant_id=n["participant_id"],
            jury_topic_id=n["jury_topic_id"],
            at=_ParseDateTime(n["at"]),
            removed_choice=VoteChoice(n["removed_choice"]) if n["removed_choice"] else None,
        )
        for n in json.loads(raw or "[]")
    ]


def SaveParticipant(conn, participant: Participant):
    """Persist the full current state of one Participant."""
    conn.execute(
        """
        INSERT INTO participants (id, name, forms_json, dismissals_json, left_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            forms_json = excluded.forms_json,
            dismissals_json = excluded.dismissals_json,
            left_at = excluded.left_at
        """,
        (
            participant.id,
            participant.name,
            _FormsToJson(participant.forms),
            _DismissalsToJson(participant.dismissals),
            _Iso(participant.left_at),
        ),
    )
    conn.commit()


def SaveTopic(conn, topic: Topic):
    """Persist the full current state of one Topic (and its Poll)."""
    conn.execute(
        """
        INSERT INTO topics (
            id, subsection, author_id, title, description, poll_question, created_at,
            rules_json, target_subsection, target_participant_id, proposed_rule_config_json,
            readers_json, votes_json, closed, closed_at, result, last_activity_at,
            software_ref, posts_json, restarts_json, dismissal_notes_json, dismissal_effects_json,
            pollster_id, question_attached_at, asked_to_vote_json, same_as_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            poll_question = excluded.poll_question,
            pollster_id = excluded.pollster_id,
            question_attached_at = excluded.question_attached_at,
            asked_to_vote_json = excluded.asked_to_vote_json,
            readers_json = excluded.readers_json,
            votes_json = excluded.votes_json,
            posts_json = excluded.posts_json,
            restarts_json = excluded.restarts_json,
            dismissal_notes_json = excluded.dismissal_notes_json,
            dismissal_effects_json = excluded.dismissal_effects_json,
            closed = excluded.closed,
            closed_at = excluded.closed_at,
            result = excluded.result,
            last_activity_at = excluded.last_activity_at
        """,
        (
            topic.id,
            topic.subsection.value,
            topic.author_id,
            topic.title,
            topic.description,
            topic.poll_question,
            _Iso(topic.created_at),
            json.dumps(_RuleToDict(topic.rules)),
            topic.target_subsection.value if topic.target_subsection else None,
            topic.target_participant_id,
            _ChangeToJson(topic.proposed_change),
            _ReadersToJson(topic.readers),
            _VotesToJson(topic.votes),
            int(topic.closed),
            _Iso(topic.closed_at),
            topic.result.value if topic.result else None,
            _Iso(topic.last_activity_at),
            topic.software_ref,
            _PostsToJson(topic.posts),
            json.dumps(topic.restarts),
            _NotesToJson(topic.dismissal_notes),
            json.dumps(topic.dismissal_effects),
            topic.pollster_id,
            _Iso(topic.question_attached_at),
            json.dumps(topic.asked_to_vote),
            topic.same_as_id,
        ),
    )
    conn.commit()


def SaveRules(conn, subsection: Subsection, rule: RuleConfig):
    """Persist the current RuleConfig for one subsection."""
    d = _RuleToDict(rule)
    conn.execute(
        """
        INSERT INTO rules (subsection, quiet_time_seconds, min_life_seconds, voter_fraction, approval_fraction)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(subsection) DO UPDATE SET
            quiet_time_seconds = excluded.quiet_time_seconds,
            min_life_seconds = excluded.min_life_seconds,
            voter_fraction = excluded.voter_fraction,
            approval_fraction = excluded.approval_fraction
        """,
        (subsection.value, d["quiet_time"], d["min_life"], d["voter_fraction"], d["approval_fraction"]),
    )
    conn.commit()


def LogEvent(conn, entry: dict):
    """Append one entry to the public log, chained to the entry before it."""
    entry = dict(entry)
    kind = entry.pop("kind")
    at = _Iso(entry.pop("at"))
    data_json = json.dumps(entry, default=str, sort_keys=True, separators=(",", ":"))
    with _LOG_LOCK:
        conn.commit()
        # The web site and the cron job both write here; take the database's
        # write lock before reading the last fingerprint, so two entries never
        # chain to the same one.
        conn.execute("BEGIN IMMEDIATE")
        try:
            prev = _LastHash(conn)
            conn.execute(
                "INSERT INTO event_log (kind, at, data_json, prev_hash, hash) VALUES (?, ?, ?, ?, ?)",
                (kind, at, data_json, prev, EntryHash(prev, kind, at, data_json)),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def ReadLog(conn, before=None, limit=100):
    """Log entries, newest first: dicts with seq, kind, at, data, prev_hash, hash."""
    if before is None:
        rows = conn.execute("SELECT * FROM event_log ORDER BY seq DESC LIMIT ?", (limit,))
    else:
        rows = conn.execute("SELECT * FROM event_log WHERE seq < ? ORDER BY seq DESC LIMIT ?", (before, limit))
    return [_LogRow(row) for row in rows]


def AllLogEntries(conn):
    """Every log entry, oldest first, exactly as stored (for checking the chain)."""
    return [_LogRow(row) for row in conn.execute("SELECT * FROM event_log ORDER BY seq")]


def _LogRow(row):
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "at": row["at"],
        "data_json": row["data_json"],
        "data": json.loads(row["data_json"]),
        "prev_hash": row["prev_hash"],
        "hash": row["hash"],
    }


def LogEntriesOfKind(conn, kind):
    return [_LogRow(row) for row in conn.execute("SELECT * FROM event_log WHERE kind = ? ORDER BY seq", (kind,))]


def CheckLog(conn):
    """Recompute every fingerprint. Returns (entries checked, seq of the first bad entry or None)."""
    prev = GENESIS
    count = 0
    for row in conn.execute("SELECT seq, kind, at, data_json, prev_hash, hash FROM event_log ORDER BY seq"):
        if row["prev_hash"] != prev or row["hash"] != EntryHash(prev, row["kind"], row["at"], row["data_json"]):
            return count, row["seq"]
        prev = row["hash"]
        count += 1
    return count, None


def Fingerprint(conn, before):
    """The log's fingerprint as it stood at `before`: the hash of the last entry
    logged earlier, with how many entries that covers."""
    row = conn.execute(
        "SELECT seq, hash FROM event_log WHERE at < ? ORDER BY seq DESC LIMIT 1", (_Iso(before),)
    ).fetchone()
    if row is None:
        return {"hash": GENESIS, "entries": 0}
    count = conn.execute("SELECT COUNT(*) FROM event_log WHERE seq <= ?", (row["seq"],)).fetchone()[0]
    return {"hash": row["hash"], "entries": count}


def NoteCodeVersion(conn, version, now):
    """Log which version of the code is running, whenever it changes."""
    if not version:
        return
    row = conn.execute("SELECT data_json FROM event_log WHERE kind = 'code_version' ORDER BY seq DESC LIMIT 1").fetchone()
    if row and json.loads(row["data_json"]).get("version") == version:
        return
    LogEvent(conn, {"kind": "code_version", "at": now, "version": version})


def LastRuleChanges(conn):
    """When each (subsection name, setting) was last changed by a Rules vote."""
    last = {}
    for row in conn.execute("SELECT at, data_json FROM event_log WHERE kind = 'rule_changed' ORDER BY seq"):
        data = json.loads(row["data_json"])
        if "setting" in data:
            last[(data.get("target_subsection"), data["setting"])] = _ParseDateTime(row["at"])
    return last


def SetCredentials(conn, participant_id: str, username: str, password_hash: str):
    conn.execute(
        """
        INSERT INTO credentials (participant_id, username, password_hash)
        VALUES (?, ?, ?)
        ON CONFLICT(participant_id) DO UPDATE SET
            username = excluded.username,
            password_hash = excluded.password_hash
        """,
        (participant_id, username, password_hash),
    )
    conn.commit()


def GetCredentialsByUsername(conn, username: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM credentials WHERE username = ?", (username,)).fetchone()


def ForgetPersonalDetails(conn, participant_id: str):
    """Erase what lets a departed Member log in or be reached: their user name
    and password, and any old sign-up profile. Their name stays on the record."""
    conn.execute("DELETE FROM credentials WHERE participant_id = ?", (participant_id,))
    conn.execute("DELETE FROM participant_profiles WHERE participant_id = ?", (participant_id,))
    conn.commit()


def GetCredentialsForParticipant(conn, participant_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM credentials WHERE participant_id = ?", (participant_id,)).fetchone()


def SaveParticipantProfile(
    conn,
    participant_id: str,
    address: str,
    age: int,
    photo_path: str,
    created_at: dt.datetime,
):
    """Record the identity details a self-registering Participant supplied.

    These back up the paper form they print and mail in (see app.py's
    Register view) so the Meeting can manually cross-check a submission
    against what arrives by mail; they aren't used by the engine itself.

    The mothers_maiden_name and thumbprint_path columns are retired (see
    ForgetRetiredProfileFields) and always stored empty.
    """
    conn.execute(
        """
        INSERT INTO participant_profiles
            (participant_id, address, mothers_maiden_name, age, photo_path, thumbprint_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (participant_id, address, "", age, photo_path, "", _Iso(created_at)),
    )
    conn.commit()


def ForgetRetiredProfileFields(conn):
    """Erase mother's maiden names and thumbprint images collected earlier.

    Registration used to ask for both. Neither proves who someone is (a
    maiden name is often findable, and anyone can upload any thumbprint
    picture), so they're no longer collected, and copies already stored
    are deleted rather than kept as sensitive data with no purpose.
    Safe to run on every startup: it does nothing once they're gone.
    """
    import os

    rows = conn.execute(
        "SELECT participant_id, thumbprint_path FROM participant_profiles "
        "WHERE mothers_maiden_name != '' OR thumbprint_path != ''"
    ).fetchall()
    for row in rows:
        path = row["thumbprint_path"]
        if path and os.path.isfile(path):
            os.remove(path)
        conn.execute(
            "UPDATE participant_profiles SET mothers_maiden_name = '', thumbprint_path = '' WHERE participant_id = ?",
            (row["participant_id"],),
        )
    conn.commit()


def GetParticipantProfile(conn, participant_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM participant_profiles WHERE participant_id = ?", (participant_id,)
    ).fetchone()


def SetChargeStatus(conn, participant_id: str, status: str):
    conn.execute(
        "UPDATE participant_profiles SET charge_status = ? WHERE participant_id = ?",
        (status, participant_id),
    )
    conn.commit()


def LoadHouse(conn) -> Meetinghouse:
    """Rebuild an Meetinghouse instance from everything persisted so far."""
    rule_rows = conn.execute("SELECT * FROM rules").fetchall()
    rules = {
        Subsection(row["subsection"]): RuleConfig(
            quiet_time=dt.timedelta(seconds=row["quiet_time_seconds"]),
            min_life=dt.timedelta(seconds=row["min_life_seconds"]),
            voter_fraction=row["voter_fraction"],
            approval_fraction=row["approval_fraction"],
        )
        for row in rule_rows
    }
    # Seasoning Time and Minimum Life are fixed by the design, not by a
    # Rules vote, so the stored values follow the code when it changes.
    for subsection, rule in list(rules.items()):
        fixed = dataclasses.replace(rule, quiet_time=SEASONING_TIME, min_life=MINIMUM_LIFE)
        if fixed != rule:
            rules[subsection] = fixed
            SaveRules(conn, subsection, fixed)
    house = Meetinghouse(rules=rules or None)

    for row in conn.execute("SELECT * FROM participants"):
        participant = Participant(
            name=row["name"],
            id=row["id"],
            forms=_FormsFromJson(row["forms_json"]),
            dismissals=_DismissalsFromJson(row["dismissals_json"]),
            left_at=_ParseDateTime(row["left_at"]),
        )
        house.participants[participant.id] = participant

    for row in conn.execute("SELECT * FROM topics"):
        topic = Topic(
            id=row["id"],
            subsection=Subsection(row["subsection"]),
            author_id=row["author_id"],
            title=row["title"],
            description=row["description"],
            poll_question=row["poll_question"],
            created_at=_ParseDateTime(row["created_at"]),
            rules=_RuleFromDict(json.loads(row["rules_json"])),
            target_subsection=Subsection(row["target_subsection"]) if row["target_subsection"] else None,
            target_participant_id=row["target_participant_id"],
            proposed_change=_ChangeFromJson(row["proposed_rule_config_json"]),
            software_ref=row["software_ref"],
            readers=_ReadersFromJson(row["readers_json"]),
            votes=_VotesFromJson(row["votes_json"]),
            posts=_PostsFromJson(row["posts_json"]),
            restarts=json.loads(row["restarts_json"] or "{}"),
            dismissal_notes=_NotesFromJson(row["dismissal_notes_json"]),
            dismissal_effects=json.loads(row["dismissal_effects_json"] or "[]"),
            closed=bool(row["closed"]),
            closed_at=_ParseDateTime(row["closed_at"]),
            result=PollResult(row["result"]) if row["result"] else None,
            last_activity_at=_ParseDateTime(row["last_activity_at"]),
            pollster_id=row["pollster_id"],
            question_attached_at=_ParseDateTime(row["question_attached_at"]),
            asked_to_vote=json.loads(row["asked_to_vote_json"] or "[]"),
            same_as_id=row["same_as_id"],
        )
        house.topics[topic.id] = topic

    for subsection, rule in house.current_rules.items():
        if subsection not in rules:
            SaveRules(conn, subsection, rule)

    return house
