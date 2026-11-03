"""Web-level tests for the decision rules: what Members actually see and do."""

from __future__ import annotations

import datetime as dt
import html
import importlib

from testkit import As, Join


def _Text(resp):
    return html.unescape(resp.data.decode())


def _NewForumConversation(test_client, title="Paint the hall", question="Paint it blue?"):
    resp = test_client.post("/topic/new/forum", data={"title": title, "poll_question": question})
    return resp.headers["Location"].rsplit("/", 1)[-1]


def _Setup(test_client, app_module):
    """Jane and Bob both join; Bob stays logged in. Returns their ids and passkeys."""
    jane, jane_device = Join(test_client, app_module, "janedoe", "Jane Doe")
    test_client.get("/logout")
    bob, bob_device = Join(test_client, app_module, "bob", "Bob Roe")
    return {"jane": (jane, jane_device), "bob": (bob, bob_device)}


def test_page_uses_the_design_words_and_puts_discussion_before_votes(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    conversation_id = _NewForumConversation(test_client)
    page = _Text(test_client.get(f"/topic/{conversation_id}"))
    assert "Forum Committee" in page and "Initiator: Bob Roe" in page and "Pollster: Bob Roe" in page
    assert page.index('id="discussion"') < page.index('id="votes"')
    assert "What is your reason?" in page
    for old_word in ("Topic", "quiet time", "favour"):
        assert old_word not in page, old_word


def test_asked_once_each_would_you_like_to_vote(client):
    test_client, app_module = client
    people = _Setup(test_client, app_module)
    no_question = _NewForumConversation(test_client, title="Chairs", question="")
    with_question = _NewForumConversation(test_client)  # Bob is its Pollster
    As(test_client, "janedoe", people["jane"][1])
    assert "Would you like to Vote?" in _Text(test_client.get(f"/topic/{with_question}"))
    assert "Would you like to Vote?" not in _Text(test_client.get(f"/topic/{with_question}"))  # only once

    assert "Would you like to Vote?" not in _Text(test_client.get(f"/topic/{no_question}"))  # nothing to vote on yet
    As(test_client, "bob", people["bob"][1])
    test_client.post(f"/topic/{no_question}/question", data={"question": "Buy 40 chairs?"})
    As(test_client, "janedoe", people["jane"][1])
    assert "Would you like to Vote?" in _Text(test_client.get(f"/topic/{no_question}"))  # now there is

    importlib.reload(app_module)  # remembered after a restart
    assert people["jane"][0] in app_module.house.topics[no_question].asked_to_vote


def test_any_contactor_can_attach_the_one_question_later(client):
    test_client, app_module = client
    people = _Setup(test_client, app_module)
    conversation_id = _NewForumConversation(test_client, title="Chairs for the hall", question="")
    page = _Text(test_client.get(f"/topic/{conversation_id}"))
    assert "Do you want to attach a Question to this Conversation?" in page
    assert 'name="choice"' not in page  # nothing to vote on yet

    As(test_client, "janedoe", people["jane"][1])
    page = _Text(test_client.get(f"/topic/{conversation_id}"))
    assert "Do you want to attach a Question to this Conversation?" in page  # asked of every Contactor
    resp = test_client.post(f"/topic/{conversation_id}/question", data={"question": "Shall we buy 40 chairs?"}, follow_redirects=True)
    page = _Text(resp)
    assert "you are its Pollster" in page and "Shall we buy 40 chairs?" in page
    assert "Pollster: Jane Doe" in page and "Initiator: Bob Roe" in page
    assert "Do you want to attach a Question" not in page
    second = test_client.post(f"/topic/{conversation_id}/question", data={"question": "Or 50?"}, follow_redirects=True)
    assert "There can be only one." in _Text(second)
    topic = app_module.house.topics[conversation_id]
    assert topic.pollster_id == people["jane"][0] and topic.question_attached_at is not None

    importlib.reload(app_module)  # saved and reloaded
    topic = app_module.house.topics[conversation_id]
    assert topic.poll_question == "Shall we buy 40 chairs?" and topic.pollster_id == people["jane"][0]


def test_vote_needs_a_reason_and_receipts_count_down(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    conversation_id = _NewForumConversation(test_client)
    no_reason = test_client.post(f"/topic/{conversation_id}/vote", data={"choice": "Yes", "comment": " "}, follow_redirects=True)
    assert "Every Vote needs its reason" in _Text(no_reason)
    messages = []
    for i in range(6):
        resp = test_client.post(f"/topic/{conversation_id}/vote", data={"choice": "Yes", "comment": f"reason {i}"}, follow_redirects=True)
        messages.append(_Text(resp))
    assert "can keep this Conversation open 4 more times" in messages[0]
    assert "can keep this Conversation open 1 more time." in messages[3]
    assert "That was the last of your Votes that can keep this Conversation open" in messages[4]
    assert "It counts, but it no longer keeps the Conversation open" in messages[5]


def test_only_forum_offers_abstain(client):
    test_client, app_module = client
    people = _Setup(test_client, app_module)
    forum_id = _NewForumConversation(test_client)
    assert b'<option value="Abstain"' in test_client.get(f"/topic/{forum_id}").data
    resp = test_client.post("/topic/new/jury", data={"target_participant_id": people["jane"][0]})
    jury_id = resp.headers["Location"].rsplit("/", 1)[-1]
    page = test_client.get(f"/topic/{jury_id}").data
    assert b'<option value="Abstain"' not in page and b'<option value="No"' in page


def test_comments_keep_paragraphs_and_flag_repeats(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    conversation_id = _NewForumConversation(test_client)
    text = "First paragraph about color.\n\nSecond paragraph about cost."
    test_client.post(f"/topic/{conversation_id}/post", data={"text": text})
    page = test_client.get(f"/topic/{conversation_id}").data.decode()
    assert '<div class="prewrap">First paragraph about color.\n\nSecond paragraph about cost.</div>' in page.replace("\r", "")
    again = test_client.post(
        f"/topic/{conversation_id}/post", data={"text": "First paragraph about color. Second paragraph about cost!"}, follow_redirects=True
    )
    again = _Text(again)
    assert "This sounds close to what you said on" in again
    assert "It's posted, but it won't keep the Conversation open." in again


def test_rules_form_offers_only_the_allowed_whole_percentages(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    form = _Text(test_client.get("/topic/new/rules"))
    for percent in range(63, 71):
        assert f'value="Forum:approval_fraction:{percent}"' in form
    assert 'value="Forum:approval_fraction:62"' not in form and 'value="Forum:approval_fraction:71"' not in form
    assert 'value="Forum:voter_fraction:24"' in form and 'value="Forum:voter_fraction:26"' in form
    assert 'value="Forum:voter_fraction:23"' not in form

    too_far = test_client.post("/topic/new/rules", data={"kind": "setting", "change": "Forum:approval_fraction:80"})
    assert "can propose 63% to 70%" in _Text(too_far)
    ok = test_client.post("/topic/new/rules", data={"kind": "setting", "change": "Forum:approval_fraction:70"})
    assert ok.status_code == 302
    page = _Text(test_client.get(ok.headers["Location"]))
    assert "Change Forum Approval from 66.7% to 70%" in page and "Adopt this change?" in page
    assert '<option value="Abstain"' not in page
    dup = _Text(test_client.post("/topic/new/rules", data={"kind": "setting", "change": "Forum:approval_fraction:64"}))
    assert "already an open Conversation about Forum Approval" in dup
    assert ok.headers["Location"] in dup
    rules_page = _Text(test_client.get("/current-rules"))
    assert "Change Forum Approval from 66.7% to 70%" in rules_page
    assert "63% to 70%" in rules_page and "24% to 26%" in rules_page
    assert "between 9% and 100%" in rules_page


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
    people = _Setup(test_client, app_module)
    forum_id = _NewForumConversation(test_client)  # by Bob
    As(test_client, "janedoe", people["jane"][1])
    test_client.post(f"/topic/{forum_id}/vote", data={"choice": "No", "comment": "Too dark."})
    As(test_client, "bob", people["bob"][1])
    jury = test_client.post("/topic/new/jury", data={"target_participant_id": people["jane"][0]})
    jury_id = jury.headers["Location"].rsplit("/", 1)[-1]
    test_client.post(f"/topic/{jury_id}/vote", data={"choice": "Yes", "comment": "Repeated cussing."})
    dup = _Text(test_client.post("/topic/new/jury", data={"target_participant_id": people["jane"][0]}))
    assert "already an open Jury Conversation about Jane Doe" in dup

    later = app_module.Now() + dt.timedelta(weeks=6)
    monkeypatch.setattr(app_module, "Now", lambda: later)
    app_module.RunTick()

    page = _Text(test_client.get(f"/topic/{forum_id}"))
    assert "Dismissed 1" in page
    assert "Jane Doe's No vote removed" in page
    assert f"/topic/{jury_id}" in page
    jury_page = _Text(test_client.get(f"/topic/{jury_id}"))
    assert "Jane Doe was dismissed on" in jury_page and "Paint the hall" in jury_page
    assert "Seasoning Time restarted" in jury_page
    summary = _Text(test_client.get("/summary"))
    assert "Jane Doe was dismissed today" in summary and "Paint the hall" in summary

    importlib.reload(app_module)
    topic = app_module.house.topics[forum_id]
    assert topic.dismissal_notes and app_module.house.topics[jury_id].dismissal_effects


def test_no_old_words_on_the_main_pages(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    _NewForumConversation(test_client)
    for path in ("/", "/subsection/forum", "/subsection/rules", "/subsection/jury", "/current-rules", "/summary",
                 "/archive?q=hall", "/topic/new/forum", "/topic/new/rules", "/topic/new/jury", "/account"):
        page = _Text(test_client.get(path))
        for old_word in ("Topic", "Participant", "quiet time", "Quiet time", "favour", "Poll question", "turnout"):
            assert old_word not in page, (path, old_word)


def test_anyone_can_just_look_around_but_only_members_take_part(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    conversation_id = _NewForumConversation(test_client)
    test_client.post(f"/topic/{conversation_id}/post", data={"text": "Blue is calm."})
    test_client.get("/logout")

    front = _Text(test_client.get("/"))
    assert "meetinghouse-sketch.png" in front and ">OR<" in front and "Just look around?" in front
    look = _Text(test_client.get("/look"))
    assert "Paint the hall" in look and "You're looking around without logging in" in look
    assert "New Conversation" not in look
    page = _Text(test_client.get(f"/topic/{conversation_id}"))
    assert "Paint it blue?" in page and "Blue is calm." in page
    assert 'name="choice"' not in page and "Add a Comment" not in page
    assert "Paint the hall" in _Text(test_client.get("/subsection/forum"))
    assert "Paint the hall" in _Text(test_client.get("/archive?q=paint"))
    test_client.post(f"/topic/{conversation_id}/post", data={"text": "Sneaky."})
    assert all(p.text != "Sneaky." for p in app_module.house.topics[conversation_id].posts)


def test_tabs_show_where_you_are(client):
    test_client, app_module = client
    _Setup(test_client, app_module)
    page = test_client.get("/subsection/rules").data.decode()
    assert 'href="/subsection/rules" class="active"' in page
    assert 'href="/subsection/forum" class=""' in page


def test_jury_list_leaves_out_yourself_and_is_alphabetical(client):
    test_client, app_module = client
    people = _Setup(test_client, app_module)  # Jane Doe and Bob Roe; Bob is logged in
    test_client.get("/logout")
    Join(test_client, app_module, "al", "Al Zimmer")
    test_client.get("/logout")
    Join(test_client, app_module, "cy", "Cy Adams")
    As(test_client, "bob", people["bob"][1])
    form = _Text(test_client.get("/topic/new/jury"))
    options = form[form.index('name="target_participant_id"'):form.index("</select>")]
    assert "Bob Roe" not in options
    assert options.index("Cy Adams") < options.index("Jane Doe") < options.index("Al Zimmer")
