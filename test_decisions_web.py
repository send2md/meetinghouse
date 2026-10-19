"""Web-level tests for the decision rules: what members actually see and do."""

from __future__ import annotations

import datetime as dt
import html

from test_app import _Login, client  # noqa: F401  (client is a fixture)
from test_security import PASSWORD, _ActivatedUser


def _As(test_client, username):
    test_client.get("/logout")
    _Login(test_client, username, PASSWORD)


def _SetPasswords(test_client):
    test_client.post("/account/password", data={"password": PASSWORD, "confirm": PASSWORD})


def _NewForumTopic(test_client, title="Paint the hall", poll="Paint it blue?"):
    resp = test_client.post("/topic/new/forum", data={"title": title, "poll_question": poll})
    return resp.headers["Location"].rsplit("/", 1)[-1]


def _Setup(test_client, app_module):
    jane = _ActivatedUser(test_client, app_module, "janedoe", "Jane Doe")
    _SetPasswords(test_client)
    test_client.get("/logout")
    bob = _ActivatedUser(test_client, app_module, "bob", "Bob Roe")
    _SetPasswords(test_client)
    return jane, bob


def test_asked_once_would_you_like_to_vote(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    topic_id = _NewForumTopic(test_client)  # Bob writes it
    _As(test_client, "janedoe")
    first = test_client.get(f"/topic/{topic_id}").data
    assert b"Would you like to vote?" in first
    second = test_client.get(f"/topic/{topic_id}").data
    assert b"Would you like to vote?" not in second


def test_vote_receipts_count_down(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    topic_id = _NewForumTopic(test_client)
    messages = []
    for i in range(6):
        resp = test_client.post(f"/topic/{topic_id}/vote", data={"choice": "Yes", "comment": f"reason {i}"}, follow_redirects=True)
        messages.append(resp.data.decode())
    assert "can keep this Topic open 4 more times" in messages[0]
    assert "can keep this Topic open 1 more time." in messages[3]
    assert "That was the last of your votes that can keep this Topic open" in messages[4]
    assert "It counts, but it no longer keeps the Topic open" in messages[5]


def test_only_forum_offers_abstain(client):
    test_client, app_module = client
    jane, bob = _Setup(test_client, app_module)
    forum_id = _NewForumTopic(test_client)
    assert b'<option value="Abstain"' in test_client.get(f"/topic/{forum_id}").data
    resp = test_client.post("/topic/new/jury", data={"target_participant_id": jane})
    jury_id = resp.headers["Location"].rsplit("/", 1)[-1]
    page = test_client.get(f"/topic/{jury_id}").data
    assert b'<option value="Abstain"' not in page and b'<option value="No"' in page


def test_discussion_keeps_paragraphs_and_flags_repeats(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    topic_id = _NewForumTopic(test_client)
    text = "First paragraph about colour.\n\nSecond paragraph about cost."
    test_client.post(f"/topic/{topic_id}/post", data={"text": text})
    page = test_client.get(f"/topic/{topic_id}").data.decode()
    assert '<div class="prewrap">First paragraph about colour.\n\nSecond paragraph about cost.</div>' in page.replace("\r", "")
    again = test_client.post(
        f"/topic/{topic_id}/post", data={"text": "First paragraph about colour. Second paragraph about cost!"}, follow_redirects=True
    ).data.decode()
    again = html.unescape(again)
    assert "This sounds close to what you said on" in again
    assert "It's posted, but it won't keep the Topic open." in again


def test_rules_topic_form_enforces_one_small_step(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    too_far = test_client.post("/topic/new/rules", data={"kind": "setting", "setting": "Forum:approval_fraction", "value": "80"})
    assert b"at most 5 points at a time" in too_far.data
    ok = test_client.post("/topic/new/rules", data={"kind": "setting", "setting": "Forum:approval_fraction", "value": "70"})
    assert ok.status_code == 302
    topic_page = test_client.get(ok.headers["Location"]).data.decode()
    assert "Change Forum approval from 66.7% to 70%" in topic_page
    assert '<option value="Abstain"' not in topic_page
    dup = test_client.post("/topic/new/rules", data={"kind": "setting", "setting": "Forum:approval_fraction", "value": "62"}).data.decode()
    assert "already an open Topic about Forum approval" in dup
    assert ok.headers["Location"] in dup  # link to the existing Topic
    rules_page = test_client.get("/current-rules").data.decode()
    assert "Change Forum approval from 66.7% to 70%" in rules_page
    assert "51% to 100%" in rules_page and "10% to 49%" in rules_page


def test_software_change_needs_a_full_commit_id(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    bad = test_client.post("/topic/new/rules", data={"kind": "software", "title": "Map", "software_ref": "abc123"})
    assert b"40-character commit id" in bad.data
    good = test_client.post("/topic/new/rules", data={"kind": "software", "title": "Map", "software_ref": "0123456789abcdef0123456789abcdef01234567"})
    assert good.status_code == 302
    assert b"github.com/send2md/meetinghouse/commit/0123456789abcdef" in test_client.get(good.headers["Location"]).data


def test_dismissal_shows_named_note_tally_and_announcement(client, monkeypatch):
    test_client, app_module = client
    jane, bob = _Setup(test_client, app_module)
    forum_id = _NewForumTopic(test_client)  # by Bob
    _As(test_client, "janedoe")
    test_client.post(f"/topic/{forum_id}/vote", data={"choice": "No", "comment": "Too dark."})
    _As(test_client, "bob")
    jury = test_client.post("/topic/new/jury", data={"target_participant_id": jane})
    jury_id = jury.headers["Location"].rsplit("/", 1)[-1]
    test_client.post(f"/topic/{jury_id}/vote", data={"choice": "Yes", "comment": "Repeated cussing."})
    dup = test_client.post("/topic/new/jury", data={"target_participant_id": jane}).data.decode()
    assert "already an open Jury Topic about Jane Doe" in dup

    later = app_module.Now() + dt.timedelta(weeks=6)
    monkeypatch.setattr(app_module, "Now", lambda: later)
    app_module.RunTick()

    page = test_client.get(f"/topic/{forum_id}").data.decode()
    assert "Dismissed 1" in page
    assert "Jane Doe&#39;s No vote removed" in page or "Jane Doe's No vote removed" in page
    assert f"/topic/{jury_id}" in page
    jury_page = test_client.get(f"/topic/{jury_id}").data.decode()
    assert "Jane Doe was dismissed on" in jury_page and "Paint the hall" in jury_page
    summary = test_client.get("/summary").data.decode()
    assert "Jane Doe was dismissed today" in summary and "Paint the hall" in summary

    # Saved and reloaded: the notes and effects survive a restart.
    import importlib
    importlib.reload(app_module)
    topic = app_module.house.topics[forum_id]
    assert topic.dismissal_notes and app_module.house.topics[jury_id].dismissal_effects
