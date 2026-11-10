"""Stewards and the Steward Vote (see the design's "Stewards and the treasury").

Three Stewards pay the bills and keep the server running; they have no other
powers. A Stewardship lasts a year, nobody serves twice, and a new Steward is
chosen every four months, so there are always two who already know the job.

The site runs each Steward Vote by itself:

1. It opens three months before a Stewardship ends. The very first one opens a
   year after Meetinghouse opens to Members (the first Member joining) and
   fills all three seats.
2. Volunteering stays open 3 weeks. Any current Member who has never been a
   Steward can put their name forward, and withdraw until the Vote begins.
3. Then it depends on how many stood:
   - more than there are seats: the rate-the-choices Vote (ranked.py). No
     Threshold, so a Steward is always chosen.
   - at least one, but no more than there are seats: a Yes-or-No Vote on each
     volunteer, with the Rules Committee's Threshold and Approval. Seats still
     empty get their own Steward Vote straight away.
   - none: the Daily Summary asks again every week. The outgoing Steward stays
     on meanwhile; that is a short extension, not a second term.
4. Either Vote stays open at least a Minimum Life (3 weeks) and closes once
   no ballot has changed for a Seasoning Time. Only each Member's first 5
   ballots restart that clock.
5. A new Steward starts the day the outgoing Steward's year ends. Each
   volunteer who wasn't chosen gets a thank-you, once, with a check box to
   stand again next time.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import uuid

import persistence
import ranked
import security
from meetinghouse import MINIMUM_LIFE, SEASONING_TIME, Subsection

TERM = dt.timedelta(days=365)
FIRST_TERMS = [365, 243, 122]  # days: twelve, eight and four months
LEAD = dt.timedelta(days=90)  # a Steward Vote opens three months before a Stewardship ends
VOLUNTEERING = dt.timedelta(weeks=3)
ASK_AGAIN = dt.timedelta(weeks=1)
FIRST_VOTE_AFTER = dt.timedelta(days=365)
RESTART_LIMIT = 5
THANK_YOU = (
    "Thank you so much for volunteering for this critical position. "
    "Please volunteer again for the next Steward position if you are still interested."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS stewards (
    id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    election_id TEXT NOT NULL,
    replaces_id TEXT
);
CREATE TABLE IF NOT EXISTS steward_elections (
    id TEXT PRIMARY KEY,
    seats INTEGER NOT NULL,
    terms_json TEXT NOT NULL,
    replaces_id TEXT,
    phase TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    volunteering_ends TEXT NOT NULL,
    voting_started TEXT,
    last_change TEXT,
    closed_at TEXT,
    voter_fraction REAL,
    approval_fraction REAL,
    result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS steward_volunteers (
    election_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    at TEXT NOT NULL,
    withdrawn INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (election_id, participant_id)
);
CREATE TABLE IF NOT EXISTS steward_ballots (
    election_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    ranking_json TEXT NOT NULL DEFAULT '[]',
    approvals_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    at TEXT NOT NULL,
    changes INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (election_id, participant_id)
);
CREATE TABLE IF NOT EXISTS steward_contactors (
    election_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (election_id, participant_id)
);
CREATE TABLE IF NOT EXISTS steward_comments (
    id TEXT PRIMARY KEY,
    election_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steward_thanks (
    participant_id TEXT NOT NULL,
    election_id TEXT NOT NULL,
    shown INTEGER NOT NULL DEFAULT 0,
    wants_next INTEGER NOT NULL DEFAULT 0,
    statement TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (participant_id, election_id)
);
"""


class StewardError(Exception):
    """Something a Member tried that the Steward Vote doesn't allow; the message says why."""


def InitSchema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _Iso(value):
    return None if value is None else value.isoformat()


def _Time(value):
    return None if value is None else dt.datetime.fromisoformat(value)


def _Log(conn, kind, now, **fields):
    persistence.LogEvent(conn, {"kind": kind, "at": now, **fields})


# --- Reading ------------------------------------------------------------------------


def Election(conn, election_id):
    row = conn.execute("SELECT * FROM steward_elections WHERE id = ?", (election_id,)).fetchone()
    return _ElectionDict(row) if row else None


def Elections(conn):
    return [_ElectionDict(r) for r in conn.execute("SELECT * FROM steward_elections ORDER BY opened_at DESC")]


def OpenElections(conn):
    return [e for e in Elections(conn) if e["phase"] != "closed"]


def _ElectionDict(row):
    d = dict(row)
    for key in ("opened_at", "volunteering_ends", "voting_started", "last_change", "closed_at"):
        d[key] = _Time(d[key])
    d["terms"] = json.loads(d.pop("terms_json"))
    d["result"] = json.loads(d.pop("result_json") or "{}")
    return d


def Stewards(conn):
    rows = conn.execute("SELECT * FROM stewards ORDER BY starts_at").fetchall()
    return [dict(r, starts_at=_Time(r["starts_at"]), ends_at=_Time(r["ends_at"])) for r in rows]


def CurrentStewards(conn, now):
    """The Stewards serving now. One whose year is over stays on until whoever
    replaces them starts (a short extension while volunteers are found)."""
    everyone = Stewards(conn)
    serving = []
    for s in everyone:
        if s["starts_at"] > now:
            continue
        if now < s["ends_at"]:
            serving.append(s)
            continue
        successor = next((t for t in everyone if t["replaces_id"] == s["id"]), None)
        if successor is None or successor["starts_at"] > now:
            serving.append(dict(s, extended=True))
    return serving


def Volunteers(conn, election_id, include_withdrawn=False):
    rows = conn.execute(
        "SELECT * FROM steward_volunteers WHERE election_id = ? ORDER BY at", (election_id,)
    ).fetchall()
    return [dict(r, at=_Time(r["at"])) for r in rows if include_withdrawn or not r["withdrawn"]]


def Ballots(conn, election_id):
    rows = conn.execute("SELECT * FROM steward_ballots WHERE election_id = ? ORDER BY at", (election_id,)).fetchall()
    return [
        dict(r, at=_Time(r["at"]), ranking=json.loads(r["ranking_json"]), approvals=json.loads(r["approvals_json"]))
        for r in rows
    ]


def Comments(conn, election_id):
    rows = conn.execute("SELECT * FROM steward_comments WHERE election_id = ? ORDER BY at", (election_id,)).fetchall()
    return [dict(r, at=_Time(r["at"])) for r in rows]


def Contactors(conn, election_id):
    return [r[0] for r in conn.execute("SELECT participant_id FROM steward_contactors WHERE election_id = ?", (election_id,))]


def PendingThanks(conn, participant_id):
    return conn.execute(
        "SELECT * FROM steward_thanks WHERE participant_id = ? AND shown = 0", (participant_id,)
    ).fetchall()


def ViewerOrder(volunteers, viewer_key, election_id):
    """The volunteers in a different random order for each viewer (the same
    order every time that viewer looks), so nobody gains by being listed first."""
    order = list(volunteers)
    random.Random(f"{viewer_key}:{election_id}").shuffle(order)
    return order


def EverSteward(conn, participant_id):
    return conn.execute("SELECT 1 FROM stewards WHERE participant_id = ?", (participant_id,)).fetchone() is not None


def _IsMember(house, conn, participant_id, now):
    participant = house.participants.get(participant_id)
    return participant is not None and participant.IsActive(now) and security.IsActivated(conn, participant_id)


def CanVolunteer(house, conn, election, participant_id, now):
    """None if they can, else why not."""
    if election["phase"] != "volunteering":
        return "Volunteering for this Steward Vote has closed."
    if not _IsMember(house, conn, participant_id, now):
        return "Only current Members can volunteer."
    if EverSteward(conn, participant_id):
        return "Nobody can be a Steward twice, so you can't stand again. Thank you for serving."
    if any(v["participant_id"] == participant_id for v in Volunteers(conn, election["id"])):
        return "You've already put your name forward."
    if any(
        v["participant_id"] == participant_id
        for other in OpenElections(conn)
        if other["id"] != election["id"]
        for v in Volunteers(conn, other["id"])
    ):
        return "You're already standing in another open Steward Vote."
    return None


# --- Opening and running elections -----------------------------------------------


def OpenElection(conn, now, seats, terms, replaces_id=None):
    election_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO steward_elections (id, seats, terms_json, replaces_id, phase, opened_at, volunteering_ends)
           VALUES (?, ?, ?, ?, 'volunteering', ?, ?)""",
        (election_id, seats, json.dumps(terms), replaces_id, _Iso(now), _Iso(now + VOLUNTEERING)),
    )
    conn.commit()
    _Log(conn, "steward_vote_opened", now, election_id=election_id, seats=seats)
    # Volunteers who were thanked last time and ticked "put my name forward
    # for the next one" stand again automatically.
    for row in conn.execute("SELECT * FROM steward_thanks WHERE wants_next = 1").fetchall():
        if not EverSteward(conn, row["participant_id"]):
            conn.execute(
                "INSERT OR IGNORE INTO steward_volunteers (election_id, participant_id, statement, at) VALUES (?, ?, ?, ?)",
                (election_id, row["participant_id"], row["statement"], _Iso(now)),
            )
            _Log(conn, "steward_volunteered", now, election_id=election_id, participant_id=row["participant_id"])
        conn.execute(
            "UPDATE steward_thanks SET wants_next = 0 WHERE participant_id = ? AND election_id = ?",
            (row["participant_id"], row["election_id"]),
        )
    conn.commit()
    return election_id


def Volunteer(house, conn, election_id, participant_id, statement, now):
    election = Election(conn, election_id)
    if election is None:
        raise StewardError("There's no such Steward Vote.")
    why_not = CanVolunteer(house, conn, election, participant_id, now)
    if why_not:
        raise StewardError(why_not)
    statement = (statement or "").strip()
    if not statement:
        raise StewardError("Please write a few words about why you're standing.")
    conn.execute(
        """INSERT INTO steward_volunteers (election_id, participant_id, statement, at, withdrawn) VALUES (?, ?, ?, ?, 0)
           ON CONFLICT(election_id, participant_id) DO UPDATE SET statement = excluded.statement, at = excluded.at, withdrawn = 0""",
        (election_id, participant_id, statement, _Iso(now)),
    )
    conn.commit()
    _Log(conn, "steward_volunteered", now, election_id=election_id, participant_id=participant_id)


def Withdraw(conn, election_id, participant_id, now):
    election = Election(conn, election_id)
    if election is None or election["phase"] != "volunteering":
        raise StewardError("You can withdraw only until the Vote begins.")
    conn.execute(
        "UPDATE steward_volunteers SET withdrawn = 1 WHERE election_id = ? AND participant_id = ?",
        (election_id, participant_id),
    )
    conn.commit()
    _Log(conn, "steward_withdrew", now, election_id=election_id, participant_id=participant_id)


def Touch(house, conn, election_id, participant_id, now):
    """A current Member opened this Steward Vote's page: they're one of its Contactors."""
    election = Election(conn, election_id)
    if election is None or election["phase"] == "closed" or not _IsMember(house, conn, participant_id, now):
        return
    conn.execute(
        "INSERT OR IGNORE INTO steward_contactors (election_id, participant_id, at) VALUES (?, ?, ?)",
        (election_id, participant_id, _Iso(now)),
    )
    conn.commit()


def AddComment(house, conn, election_id, participant_id, text, now):
    election = Election(conn, election_id)
    if election is None or election["phase"] == "closed":
        raise StewardError("This Steward Vote has closed.")
    if not _IsMember(house, conn, participant_id, now):
        raise StewardError("Only current Members can comment.")
    if not (text or "").strip():
        raise StewardError("Write your Comment first.")
    conn.execute(
        "INSERT INTO steward_comments (id, election_id, participant_id, text, at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), election_id, participant_id, text.strip(), _Iso(now)),
    )
    conn.commit()
    Touch(house, conn, election_id, participant_id, now)
    _Log(conn, "steward_comment", now, election_id=election_id, participant_id=participant_id)


def CastBallot(house, conn, election_id, participant_id, reason, now, ranking=None, approvals=None):
    """Record (or replace) a Member's ballot. `ranking`: volunteer ids, 1st
    first (rate-the-choices). `approvals`: {volunteer id: "Yes" or "No"} (Yes-or-No)."""
    election = Election(conn, election_id)
    if election is None or election["phase"] not in ("rating", "yesno"):
        raise StewardError("This Steward Vote isn't taking ballots now.")
    if not _IsMember(house, conn, participant_id, now):
        raise StewardError("Only current Members can Vote.")
    if not (reason or "").strip():
        raise StewardError("Every Vote needs its reason. Please write why you're Voting this way.")
    standing = [v["participant_id"] for v in Volunteers(conn, election_id)]
    if election["phase"] == "rating":
        ranking = [pid for pid in (ranking or []) if pid in standing]
        if not ranking:
            raise StewardError("Rate at least one volunteer: 1 for your first choice.")
        if len(set(ranking)) != len(ranking):
            raise StewardError("Give each volunteer a different number.")
        approvals = {}
    else:
        approvals = {pid: v for pid, v in (approvals or {}).items() if pid in standing and v in ("Yes", "No")}
        if set(approvals) != set(standing):
            raise StewardError("Vote Yes or No on each volunteer.")
        ranking = []
    previous = conn.execute(
        "SELECT changes FROM steward_ballots WHERE election_id = ? AND participant_id = ?", (election_id, participant_id)
    ).fetchone()
    changes = (previous["changes"] + 1) if previous else 1
    conn.execute(
        """INSERT INTO steward_ballots (election_id, participant_id, ranking_json, approvals_json, reason, at, changes)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(election_id, participant_id) DO UPDATE SET ranking_json = excluded.ranking_json,
             approvals_json = excluded.approvals_json, reason = excluded.reason, at = excluded.at, changes = excluded.changes""",
        (election_id, participant_id, json.dumps(ranking), json.dumps(approvals), reason.strip(), _Iso(now), changes),
    )
    restarted = changes <= RESTART_LIMIT
    if restarted:
        conn.execute("UPDATE steward_elections SET last_change = ? WHERE id = ?", (_Iso(now), election_id))
    conn.commit()
    Touch(house, conn, election_id, participant_id, now)
    _Log(conn, "steward_ballot", now, election_id=election_id, participant_id=participant_id)
    return max(0, RESTART_LIMIT - changes)


def AnswerThanks(conn, participant_id, wants_next):
    rows = PendingThanks(conn, participant_id)
    for row in rows:
        conn.execute(
            "UPDATE steward_thanks SET shown = 1, wants_next = ? WHERE participant_id = ? AND election_id = ?",
            (1 if wants_next else 0, participant_id, row["election_id"]),
        )
    conn.commit()


# --- The clock ------------------------------------------------------------------------


def LaunchedAt(conn):
    """When Meetinghouse opened to Members: the first Member joining."""
    row = conn.execute("SELECT at FROM event_log WHERE kind = 'member_joined' ORDER BY at LIMIT 1").fetchone()
    return _Time(row["at"]) if row else None


def Tick(house, conn, now):
    """Open, advance and close Steward Votes as their dates come. Returns the
    Elections that closed."""
    InitSchema(conn)
    everyone = Stewards(conn)
    elections = Elections(conn)
    # The very first Steward Vote: a year after Meetinghouse opens.
    if not everyone and not elections:
        launched = LaunchedAt(conn)
        if launched is not None and now >= launched + FIRST_VOTE_AFTER:
            OpenElection(conn, now, seats=3, terms=FIRST_TERMS)
    # Each Stewardship's successor: three months before it ends.
    open_replacing = {e["replaces_id"] for e in elections if e["phase"] != "closed" and e["replaces_id"]}
    replaced = {s["replaces_id"] for s in everyone if s["replaces_id"]}
    for s in everyone:
        if s["id"] in replaced or s["id"] in open_replacing:
            continue
        if now >= s["ends_at"] - LEAD:
            OpenElection(conn, now, seats=1, terms=[365], replaces_id=s["id"])
    closed = []
    for election in OpenElections(conn):
        if election["phase"] == "volunteering" and now >= election["volunteering_ends"]:
            _EndVolunteering(house, conn, election, now)
        election = Election(conn, election["id"])
        if election["phase"] in ("rating", "yesno"):
            if now >= election["voting_started"] + MINIMUM_LIFE and now >= election["last_change"] + SEASONING_TIME:
                _Close(house, conn, election, now)
                closed.append(Election(conn, election["id"]))
    return closed


def _EndVolunteering(house, conn, election, now):
    standing = [v for v in Volunteers(conn, election["id"]) if _IsMember(house, conn, v["participant_id"], now)]
    if not standing:
        conn.execute(
            "UPDATE steward_elections SET volunteering_ends = ? WHERE id = ?",
            (_Iso(now + ASK_AGAIN), election["id"]),
        )
        conn.commit()
        _Log(conn, "steward_volunteers_wanted", now, election_id=election["id"])
        return
    phase = "rating" if len(standing) > election["seats"] else "yesno"
    rules = house.current_rules[Subsection.RULES]
    conn.execute(
        """UPDATE steward_elections SET phase = ?, voting_started = ?, last_change = ?, voter_fraction = ?, approval_fraction = ?
           WHERE id = ?""",
        (phase, _Iso(now), _Iso(now), rules.voter_fraction, rules.approval_fraction, election["id"]),
    )
    conn.commit()
    _Log(conn, "steward_vote_started", now, election_id=election["id"], vote_kind=phase, volunteers=len(standing))


def YesNoTally(house, conn, election, now):
    """For a Yes-or-No Steward Vote: each volunteer's Yes, No, and whether they pass."""
    contactors = [pid for pid in Contactors(conn, election["id"]) if _IsMember(house, conn, pid, now)]
    ballots = [b for b in Ballots(conn, election["id"]) if _IsMember(house, conn, b["participant_id"], now)]
    tally = {}
    for v in Volunteers(conn, election["id"]):
        pid = v["participant_id"]
        yes = sum(1 for b in ballots if b["approvals"].get(pid) == "Yes")
        no = sum(1 for b in ballots if b["approvals"].get(pid) == "No")
        voters = yes + no
        threshold_met = bool(contactors) and voters / len(contactors) >= (election["voter_fraction"] or 0) - 1e-9
        approval_met = voters > 0 and yes / voters >= (election["approval_fraction"] or 0) - 1e-9
        tally[pid] = {"yes": yes, "no": no, "passes": threshold_met and approval_met}
    return tally, len(contactors)


def _Close(house, conn, election, now):
    standing = [v["participant_id"] for v in Volunteers(conn, election["id"])]
    result = {}
    if election["phase"] == "rating":
        ballots = [b["ranking"] for b in Ballots(conn, election["id"]) if _IsMember(house, conn, b["participant_id"], now)]
        count = ranked.Count(ballots, standing, seats=election["seats"])
        winners = count["winners"]
        result["rounds"] = count["rounds"]
    else:
        tally, contactors = YesNoTally(house, conn, election, now)
        winners = [pid for pid in standing if tally[pid]["passes"]][: election["seats"]]
        result["tally"] = tally
        result["contactors"] = contactors
    result["winners"] = winners
    replaced = next((s for s in Stewards(conn) if s["id"] == election["replaces_id"]), None)
    start = max(now, replaced["ends_at"]) if replaced else now
    terms = list(election["terms"])
    for pid in winners:
        days = terms.pop(0)
        conn.execute(
            "INSERT INTO stewards (id, participant_id, starts_at, ends_at, election_id, replaces_id) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), pid, _Iso(start), _Iso(start + dt.timedelta(days=days)), election["id"], election["replaces_id"]),
        )
        _Log(conn, "steward_chosen", now, election_id=election["id"], participant_id=pid, starts=_Iso(start), days=days)
    statements = {v["participant_id"]: v["statement"] for v in Volunteers(conn, election["id"])}
    for pid in standing:
        if pid not in winners:
            conn.execute(
                "INSERT OR IGNORE INTO steward_thanks (participant_id, election_id, statement) VALUES (?, ?, ?)",
                (pid, election["id"], statements.get(pid, "")),
            )
    conn.execute(
        "UPDATE steward_elections SET phase = 'closed', closed_at = ?, result_json = ? WHERE id = ?",
        (_Iso(now), json.dumps(result), election["id"]),
    )
    conn.commit()
    _Log(conn, "steward_vote_closed", now, election_id=election["id"], chosen=len(winners))
    if terms:  # seats still empty get their own Steward Vote straight away
        OpenElection(conn, now, seats=len(terms), terms=terms, replaces_id=election["replaces_id"])
