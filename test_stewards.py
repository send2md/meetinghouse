"""Stewards and the Steward Vote."""

from __future__ import annotations

import datetime as dt

import persistence
import security
import stewards
from meetinghouse import Meetinghouse

T0 = dt.datetime(2027, 1, 1, 12, 0)


def _Setup(tmp_path, n=6):
    conn = persistence.Connect(str(tmp_path / "s.db"))
    security.InitSchema(conn)
    stewards.InitSchema(conn)
    house = persistence.LoadHouse(conn)
    people = []
    for i in range(n):
        p = house.RegisterParticipant(f"Person {chr(65 + i)}")
        p.SignForm(signed_date=(T0 + dt.timedelta(days=400)).date())  # current throughout these tests
        security.CreatePendingStatus(conn, p.id)
        security.MarkActivated(conn, p.id, T0)
        people.append(p.id)
    persistence.LogEvent(conn, {"kind": "member_joined", "at": T0, "participant_id": people[0]})
    return house, conn, people


def _Run(house, conn, until):
    return stewards.Tick(house, conn, until)


def test_first_vote_opens_a_year_after_launch_for_three_seats(tmp_path):
    house, conn, _ = _Setup(tmp_path)
    _Run(house, conn, T0 + dt.timedelta(days=364))
    assert stewards.Elections(conn) == []
    _Run(house, conn, T0 + dt.timedelta(days=365))
    (election,) = stewards.Elections(conn)
    assert election["seats"] == 3 and election["phase"] == "volunteering"


def test_rate_the_choices_fills_three_seats_with_staggered_terms(tmp_path):
    house, conn, p = _Setup(tmp_path)
    start = T0 + dt.timedelta(days=365)
    _Run(house, conn, start)
    eid = stewards.Elections(conn)[0]["id"]
    for pid in p[:4]:
        stewards.Volunteer(house, conn, eid, pid, "I'd like to help.", start)
    vote_day = start + stewards.VOLUNTEERING
    _Run(house, conn, vote_day)
    assert stewards.Election(conn, eid)["phase"] == "rating"
    stewards.CastBallot(house, conn, eid, p[4], "A first", vote_day, ranking=[p[0], p[1], p[2]])
    stewards.CastBallot(house, conn, eid, p[5], "B first", vote_day, ranking=[p[1], p[0]])
    stewards.CastBallot(house, conn, eid, p[3], "Myself", vote_day, ranking=[p[3]])
    _Run(house, conn, vote_day + dt.timedelta(days=20))
    assert stewards.Election(conn, eid)["phase"] == "rating"  # Minimum Life not over
    end = vote_day + dt.timedelta(days=21)
    closed = _Run(house, conn, end)
    assert [e["id"] for e in closed] == [eid]
    chosen = stewards.Stewards(conn)
    assert len(chosen) == 3
    terms = sorted((s["ends_at"] - s["starts_at"]).days for s in chosen)
    assert terms == [122, 243, 365]
    # The volunteer who wasn't chosen is thanked once.
    loser = next(pid for pid in p[:4] if pid not in {s["participant_id"] for s in chosen})
    assert len(stewards.PendingThanks(conn, loser)) == 1
    stewards.AnswerThanks(conn, loser, wants_next=True)
    assert stewards.PendingThanks(conn, loser) == []
    # The four-month Steward's successor Vote opens three months before they finish,
    # and the thanked volunteer stands in it automatically.
    shortest = min(chosen, key=lambda s: s["ends_at"])
    _Run(house, conn, shortest["ends_at"] - stewards.LEAD)
    successor = next(e for e in stewards.OpenElections(conn) if e["replaces_id"] == shortest["id"])
    assert [v["participant_id"] for v in stewards.Volunteers(conn, successor["id"])] == [loser]


def test_nobody_serves_twice(tmp_path):
    house, conn, p = _Setup(tmp_path)
    eid = stewards.OpenElection(conn, T0, seats=1, terms=[365])
    conn.execute(
        "INSERT INTO stewards (id, participant_id, starts_at, ends_at, election_id) VALUES ('x', ?, ?, ?, 'old')",
        (p[0], T0.isoformat(), (T0 + dt.timedelta(days=365)).isoformat()),
    )
    election = stewards.Election(conn, eid)
    assert "twice" in stewards.CanVolunteer(house, conn, election, p[0], T0)
    assert stewards.CanVolunteer(house, conn, election, p[1], T0) is None


def test_one_volunteer_gets_a_yes_or_no_vote_and_empty_seats_reopen(tmp_path):
    house, conn, p = _Setup(tmp_path)
    eid = stewards.OpenElection(conn, T0, seats=3, terms=[365, 243, 122])
    stewards.Volunteer(house, conn, eid, p[0], "Happy to.", T0)
    vote_day = T0 + stewards.VOLUNTEERING
    _Run(house, conn, vote_day)
    assert stewards.Election(conn, eid)["phase"] == "yesno"
    for voter in p[1:4]:
        stewards.Touch(house, conn, eid, voter, vote_day)
        stewards.CastBallot(house, conn, eid, voter, "Yes, thank you.", vote_day, approvals={p[0]: "Yes"})
    _Run(house, conn, vote_day + dt.timedelta(days=21))
    chosen = stewards.Stewards(conn)
    assert [s["participant_id"] for s in chosen] == [p[0]] and (chosen[0]["ends_at"] - chosen[0]["starts_at"]).days == 365
    reopened = stewards.OpenElections(conn)
    assert len(reopened) == 1 and reopened[0]["seats"] == 2 and reopened[0]["terms"] == [243, 122]


def test_no_volunteers_asks_again_and_the_outgoing_steward_stays_on(tmp_path):
    house, conn, p = _Setup(tmp_path)
    conn.execute(
        "INSERT INTO stewards (id, participant_id, starts_at, ends_at, election_id) VALUES ('s1', ?, ?, ?, 'old')",
        (p[0], T0.isoformat(), (T0 + dt.timedelta(days=365)).isoformat()),
    )
    conn.commit()
    opened = T0 + dt.timedelta(days=275)
    _Run(house, conn, opened)
    (election,) = stewards.OpenElections(conn)
    _Run(house, conn, opened + stewards.VOLUNTEERING)
    election = stewards.Election(conn, election["id"])
    assert election["phase"] == "volunteering" and election["volunteering_ends"] == opened + stewards.VOLUNTEERING + stewards.ASK_AGAIN
    later = T0 + dt.timedelta(days=400)
    assert [s["participant_id"] for s in stewards.CurrentStewards(conn, later)] == [p[0]]
    assert stewards.CurrentStewards(conn, later)[0]["extended"]


def test_only_five_ballots_keep_it_open(tmp_path):
    house, conn, p = _Setup(tmp_path)
    eid = stewards.OpenElection(conn, T0, seats=1, terms=[365])
    for pid in p[:2]:
        stewards.Volunteer(house, conn, eid, pid, "Me.", T0)
    vote_day = T0 + stewards.VOLUNTEERING
    _Run(house, conn, vote_day)
    lefts = [
        stewards.CastBallot(house, conn, eid, p[3], "Changing my mind", vote_day + dt.timedelta(days=i), ranking=[p[i % 2]])
        for i in range(6)
    ]
    assert lefts == [4, 3, 2, 1, 0, 0]
    assert stewards.Election(conn, eid)["last_change"] == vote_day + dt.timedelta(days=4)


def test_pages(client):
    test_client, app_module = client
    assert "No Stewards yet" in test_client.get("/stewards").data.decode()
    eid = stewards.OpenElection(app_module.conn, app_module.Now(), seats=1, terms=[365])
    page = test_client.get(f"/stewards/vote/{eid}").data.decode()
    assert "Volunteers wanted" in page
    assert "Steward Vote" in test_client.get("/subsection/rules").data.decode()
    assert "A Steward Vote opened" in test_client.get("/log").data.decode()


def test_volunteering_voting_and_the_thank_you_on_the_site(client):
    import html

    from testkit import As, Join

    test_client, app_module = client
    conn, house = app_module.conn, app_module.house
    devices = {}
    for username in ("ann", "ben", "cat"):
        test_client.get("/logout")
        _, devices[username] = Join(test_client, app_module, username, username.capitalize() + " Lee")
    now = app_module.Now()
    eid = stewards.OpenElection(conn, now, seats=1, terms=[365])
    for username in ("ann", "ben"):
        As(test_client, username, devices[username])
        page = html.unescape(test_client.post(f"/stewards/vote/{eid}/volunteer", data={"statement": f"{username} would pay the bills on time."}, follow_redirects=True).data.decode())
        assert "Thank you for standing" in page
    conn.execute("UPDATE steward_elections SET volunteering_ends = ? WHERE id = ?", ((now - dt.timedelta(minutes=1)).isoformat(), eid))
    conn.commit()
    stewards.Tick(house, conn, now)
    As(test_client, "cat", devices["cat"])
    page = html.unescape(test_client.get(f"/stewards/vote/{eid}").data.decode())
    assert "Rate the volunteers" in page and "ann would pay the bills on time." in page
    ann = next(pid for pid, p in house.participants.items() if p.name == "Ann Lee")
    ben = next(pid for pid, p in house.participants.items() if p.name == "Ben Lee")
    page = html.unescape(test_client.post(
        f"/stewards/vote/{eid}/ballot", data={f"rank_{ann}": "1", f"rank_{ben}": "2", "reason": "Ann has done this before."}, follow_redirects=True
    ).data.decode())
    assert "Vote recorded" in page and "Ann has done this before." in page
    later = now + dt.timedelta(weeks=7)
    stewards.Tick(house, conn, later)
    assert [s["participant_id"] for s in stewards.Stewards(conn)] == [ann]
    As(test_client, "ben", devices["ben"])
    page = html.unescape(test_client.get("/").data.decode())
    assert "Thank you so much for volunteering" in page
    test_client.post("/stewards/thanks", data={"next": "yes"})
    assert "Thank you so much for volunteering" not in html.unescape(test_client.get("/").data.decode())
    import re

    page = re.sub(r"<[^>]+>", "", html.unescape(test_client.get(f"/stewards/vote/{eid}").data.decode()))
    assert "Round 1" in page and "Chosen: Ann Lee" in page
