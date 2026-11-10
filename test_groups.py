"""Groups, circles, and splitting a group."""

from __future__ import annotations

import datetime as dt

import groups
import persistence
import security
from meetinghouse import Subsection, VoteChoice

TOWNS = {  # four towns 30-60 km apart
    "north": (42.90, -76.50),
    "south": (42.10, -76.50),
    "east": (42.50, -75.90),
    "west": (42.50, -77.10),
}


def _Spots(counts):
    spots = {}
    for town, n in counts.items():
        lat, lon = TOWNS[town]
        for i in range(n):
            spots[f"{town}{i:02d}"] = (lat + 0.01 * (i % 3), lon + 0.01 * (i // 3))
    return spots


def test_circles_follow_the_towns():
    circles = groups.Circles(_Spots({"north": 10, "south": 10, "east": 10, "west": 10}))
    assert len(circles) == 4
    assert all(len(c["members"]) == 10 for c in circles)
    assert circles[0]["lat"] > circles[-1]["lat"]  # numbered north to south


def test_a_lone_member_counts_with_the_nearest_circle():
    spots = _Spots({"north": 10, "south": 10})
    spots["stray"] = (42.80, -76.50)  # 10 km south of "north" is still near it
    spots["far"] = (42.50, -76.50)  # alone in the middle
    circles = groups.Circles(spots)
    assert len(circles) == 2 and sum(len(c["members"]) for c in circles) == 22


def test_a_circle_with_more_than_60_percent_is_cut():
    spots = _Spots({"north": 30, "south": 5})
    for i in range(30):  # spread "north" out east to west so it can be cut
        spots[f"north{i:02d}"] = (42.90, -76.80 + 0.02 * i)
    circles = groups.Circles(spots)
    assert all(len(c["members"]) <= 0.6 * 35 for c in circles)


def test_software_ways_and_checking_a_suggestion():
    circles = groups.Circles(_Spots({"north": 10, "south": 10, "east": 10, "west": 10}))
    ways = groups.SoftwareWays(circles)
    assert len(ways) == 2
    for _, first in ways:
        assert groups.CheckWay(first, circles) is None
    north = next(i for i, c in enumerate(circles) if c["lat"] > 42.8)
    assert "40% and 60%" in groups.CheckWay([north], circles)
    assert "at least one circle" in groups.CheckWay([], circles)


def _House(tmp_path, n_per_town=8):
    conn = persistence.Connect(str(tmp_path / "g.db"))
    security.InitSchema(conn)
    groups.InitSchema(conn)
    house = persistence.LoadHouse(conn)
    spots = {}
    t0 = dt.datetime(2027, 1, 1)
    for town, (lat, lon) in TOWNS.items():
        for i in range(n_per_town):
            p = house.RegisterParticipant(f"{town.title()} {i}")
            p.SignForm(signed_date=t0.date())
            security.CreatePendingStatus(conn, p.id)
            security.MarkActivated(conn, p.id, t0)
            spots[p.id] = (lat + 0.01 * (i % 3), lon + 0.01 * (i // 3))
    return house, conn, spots, t0


def test_splitting_a_group_from_start_to_finish(tmp_path):
    house, conn, spots, t0 = _House(tmp_path)
    people = sorted(spots)
    assert groups.CanProposeSplit(house, conn, "A", people[0], t0) is None
    topic = house.CreateTopic(people[0], Subsection.RULES, None, t0, split_group="A")
    assert topic.title == "Should Group A split?" and topic.group == "A"
    split_id = groups.StartDeciding(house, conn, topic, t0, spots.get)
    assert "already deciding" in groups.CanProposeSplit(house, conn, "A", people[1], t0)
    for pid in people[:20]:
        house.CastVote(topic.id, pid, VoteChoice.YES, "It's too big to know each other.", t0)
    house.Tick(t0 + dt.timedelta(weeks=4))
    t1 = t0 + dt.timedelta(weeks=4)
    groups.Tick(house, conn, t1, spots.get)
    split = groups.Split(conn, split_id)
    assert split["phase"] == "suggesting" and len(split["circles"]) == 4
    choices = groups.Choices(conn, split_id)
    assert len(choices) == 2
    # A Member suggests the same as a software way: refused.
    try:
        groups.Suggest(house, conn, split_id, people[0], choices[0]["first_half"], t1)
        assert False
    except groups.SplitError as exc:
        assert "exactly as Choice 1" in str(exc)
    t2 = t1 + dt.timedelta(weeks=3)
    groups.Tick(house, conn, t2, spots.get)
    assert groups.Split(conn, split_id)["phase"] == "rating"
    for pid in people[:5]:
        groups.CastBallot(house, conn, split_id, pid, [choices[1]["id"], choices[0]["id"]], "East and west fits the roads.", t2)
    groups.Tick(house, conn, t2 + dt.timedelta(weeks=3), spots.get)
    split = groups.Split(conn, split_id)
    assert split["phase"] == "done" and split["result"]["winner"] == choices[1]["id"]
    assert sorted(split["result"]["sizes"]) == ["AA", "AB"]
    assert set(house.member_groups.values()) == {"AA", "AB"}
    a_member = people[0]
    assert house.InGroup(a_member, "A") and house.GroupsOf(a_member)[0] == "A"
    # The groups survive a restart, and a new Member is placed by where they live.
    reloaded = persistence.LoadHouse(conn)
    assert reloaded.member_groups == house.member_groups
    newcomer = house.RegisterParticipant("New Person")
    group = groups.PlaceNewMember(house, conn, newcomer.id, TOWNS["east"])
    east_member = next(pid for pid, s in spots.items() if s[1] > -76.0)
    assert group == house.GroupOf(east_member)


def test_too_few_members_cannot_split(tmp_path):
    house, conn, spots, t0 = _House(tmp_path, n_per_town=5)
    assert "at least 30" in groups.CanProposeSplit(house, conn, "A", sorted(spots)[0], t0)


def test_group_conversations_are_for_their_group_only(tmp_path):
    house, conn, spots, t0 = _House(tmp_path, n_per_town=2)
    people = sorted(spots)
    house.member_groups.update({people[0]: "AA", people[1]: "AA", people[2]: "AB"})
    topic = house.CreateTopic(people[0], Subsection.FORUM, "Our bus stop", t0, poll_question="Ask for a shelter?", group="AA")
    house.CastVote(topic.id, people[1], VoteChoice.YES, "Yes.", t0)
    try:
        house.CastVote(topic.id, people[2], VoteChoice.YES, "Me too.", t0)
        assert False
    except Exception as exc:
        assert "Group AA" in str(exc)
    assert house.MarkRead(topic.id, people[2], t0) is False
    try:
        house.CreateTopic(people[2], Subsection.FORUM, "Not mine", t0, group="AA")
        assert False
    except Exception as exc:
        assert "your own groups" in str(exc)
    # The Initiator can move it to all of A during its first week.
    house.MarkRead(topic.id, people[1], t0)
    house.FlagWrongGroup(topic.id, people[1], t0)
    house.MoveToGroup(topic.id, people[0], "A", t0 + dt.timedelta(days=2))
    assert topic.group == "A" and topic.group_flags == []
    house.CastVote(topic.id, people[2], VoteChoice.YES, "Me too.", t0 + dt.timedelta(days=2))
