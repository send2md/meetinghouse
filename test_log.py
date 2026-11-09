"""The public log: every entry chained to the one before, readable by anyone."""

from __future__ import annotations

import datetime as dt
import json

import persistence
import tick_runner
from testkit import Join


def test_entries_chain_and_check_out(tmp_path):
    conn = persistence.Connect(str(tmp_path / "log.db"))
    t = dt.datetime(2026, 11, 1, 12, 0)
    for i in range(3):
        persistence.LogEvent(conn, {"kind": "post_added", "at": t + dt.timedelta(minutes=i), "n": i})
    entries = persistence.AllLogEntries(conn)
    assert entries[0]["prev_hash"] == persistence.GENESIS
    for before, after in zip(entries, entries[1:]):
        assert after["prev_hash"] == before["hash"]
    # The rule the log page states, recomputed by hand.
    import hashlib

    e = entries[1]
    assert e["hash"] == hashlib.sha256(f"{e['prev_hash']}\n{e['kind']}\n{e['at']}\n{e['data_json']}".encode()).hexdigest()
    assert persistence.CheckLog(conn) == (3, None)


def test_changing_an_old_entry_is_detected(tmp_path):
    conn = persistence.Connect(str(tmp_path / "log.db"))
    t = dt.datetime(2026, 11, 1, 12, 0)
    for i in range(3):
        persistence.LogEvent(conn, {"kind": "vote_cast", "at": t, "choice": "Yes"})
    conn.execute("UPDATE event_log SET data_json = ? WHERE seq = 2", (json.dumps({"choice": "No"}),))
    conn.commit()
    assert persistence.CheckLog(conn) == (1, 2)


def test_entries_from_before_the_chain_get_fingerprints(tmp_path):
    path = str(tmp_path / "old.db")
    conn = persistence.Connect(path)
    conn.execute("INSERT INTO event_log (kind, at, data_json) VALUES ('topic_created', '2026-10-01T00:00:00', '{}')")
    conn.commit()
    conn.close()
    conn = persistence.Connect(path)
    persistence.LogEvent(conn, {"kind": "post_added", "at": dt.datetime(2026, 10, 2)})
    assert persistence.CheckLog(conn) == (2, None)


def test_fingerprint_is_the_log_as_of_a_moment(tmp_path):
    conn = persistence.Connect(str(tmp_path / "log.db"))
    persistence.LogEvent(conn, {"kind": "a", "at": dt.datetime(2026, 11, 1, 10)})
    persistence.LogEvent(conn, {"kind": "b", "at": dt.datetime(2026, 11, 2, 10)})
    first = persistence.AllLogEntries(conn)[0]
    assert persistence.Fingerprint(conn, dt.datetime(2026, 11, 2)) == {"hash": first["hash"], "entries": 1}


def test_code_version_logged_only_when_it_changes(tmp_path):
    conn = persistence.Connect(str(tmp_path / "log.db"))
    now = dt.datetime(2026, 11, 1)
    for version in ("abc123", "abc123", "def456", ""):
        persistence.NoteCodeVersion(conn, version, now)
    assert [e["data"]["version"] for e in persistence.LogEntriesOfKind(conn, "code_version")] == ["abc123", "def456"]


def test_log_page_is_public_and_shows_joining_and_conversations(client):
    test_client, app_module = client
    Join(test_client, app_module, "janedoe", "Jane Doe")
    test_client.post("/topic/new/forum", data={"title": "Bike racks"})
    test_client.get("/logout")
    page = test_client.get("/log").data.decode()
    assert "Jane Doe joined" in page
    assert "Jane Doe started “Bike racks”" in page
    assert "entries match" in page and "Today's fingerprint" in page
    download = test_client.get("/log/download")
    lines = [json.loads(line) for line in download.data.decode().splitlines()]
    assert lines and all(len(line["hash"]) == 64 for line in lines)


def test_lapsed_yearly_check_is_logged_once(client):
    _, app_module = client
    house, conn = app_module.house, app_module.conn
    person = house.RegisterParticipant("Sam Lapse")
    person.SignForm(signed_date=dt.date(2025, 1, 1))
    now = dt.datetime(2026, 11, 1)
    tick_runner.LogMembershipChanges(house, conn, now)
    tick_runner.LogMembershipChanges(house, conn, now)
    paused = persistence.LogEntriesOfKind(conn, "member_paused")
    assert [e["data"]["participant_id"] for e in paused] == [person.id]
