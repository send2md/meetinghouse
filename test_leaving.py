"""Leaving Meetinghouse (the design's "Leaving Meetinghouse")."""

from __future__ import annotations

import datetime as dt
import html

import persistence
from testkit import As, Join, PasskeyLogin


def _Text(resp):
    return html.unescape(resp.data.decode())


def _New(test_client, title, question):
    resp = test_client.post("/topic/new/forum", data={"title": title, "poll_question": question})
    return resp.headers["Location"].rsplit("/", 1)[-1]


def test_leaving_takes_comments_and_votes_off_open_conversations(client):
    test_client, app_module = client
    jane, jane_device = Join(test_client, app_module, "janedoe", "Jane Doe")
    hall = _New(test_client, "Paint the hall", "Paint it blue?")
    test_client.get("/logout")
    bob, bob_device = Join(test_client, app_module, "bob", "Bob Roe")
    test_client.post(f"/topic/{hall}/post", data={"text": "Blue is a calm colour for a meeting room."})
    test_client.post(f"/topic/{hall}/vote", data={"choice": "Yes", "comment": "Calm."})
    garden = _New(test_client, "A garden", "Plant a garden?")
    house = app_module.house
    before = house.topics[hall].last_activity_at

    page = _Text(test_client.get("/account/leave"))
    assert "Leave Meetinghouse" in page and "goes on to its result" in page
    refused = test_client.post("/account/leave", data={}, follow_redirects=True)
    assert "tick the box" in _Text(refused) and not house.participants[bob].HasLeft()
    done = _Text(test_client.post("/account/leave", data={"confirm": "yes"}))
    assert "You have left Meetinghouse" in done

    topic = house.topics[hall]
    assert bob not in topic.votes and all(p.participant_id != bob for p in topic.posts)
    assert bob not in topic.readers
    assert topic.last_activity_at > before  # Seasoning Time starts over
    # The Conversation Bob started goes on, under his name.
    assert house.topics[garden].author_id == bob and not house.topics[garden].closed
    page = _Text(test_client.get(f"/topic/{garden}"))
    assert "Bob Roe" in page and "Plant a garden?" in page
    # His photo is the plain picture; his login, email and passkeys are gone.
    photo = test_client.get(f"/photo/{bob}")
    assert photo.status_code == 200 and b"left Meetinghouse" in photo.data
    assert persistence.GetCredentialsForParticipant(app_module.conn, bob) is None
    assert app_module.security.GetMember(app_module.conn, bob)["email"] == ""
    assert not app_module.security.HasPasskeys(app_module.conn, bob)
    # It is in the public log, and it survives a restart.
    assert "Bob Roe left Meetinghouse" in _Text(test_client.get("/log"))
    reloaded = persistence.LoadHouse(app_module.conn)
    assert reloaded.participants[bob].HasLeft()
    assert bob not in reloaded.topics[hall].votes


def test_leaving_leaves_closed_conversations_alone():
    from meetinghouse import Meetinghouse, Subsection, VoteChoice

    house = Meetinghouse()
    t = dt.datetime(2026, 11, 1)
    a = house.RegisterParticipant("Ann")
    b = house.RegisterParticipant("Ben")
    for p in (a, b):
        p.SignForm(signed_date=t.date())
    topic = house.CreateTopic(a.id, Subsection.FORUM, "Old matter", t, poll_question="Agree?")
    house.AddPost(topic.id, b.id, "I agree.", t)
    house.CastVote(topic.id, b.id, VoteChoice.YES, "Yes.", t)
    topic.closed = True
    assert house.Leave(b.id, t + dt.timedelta(days=1)) == []
    assert b.id in topic.votes and topic.posts
    assert not b.IsParticipant(t + dt.timedelta(days=2))
