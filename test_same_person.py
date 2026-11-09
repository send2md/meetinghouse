"""When a newcomer's face matches someone's (the design's "When a face matches")."""

from __future__ import annotations

import datetime as dt
import html

import tick_runner
from meetinghouse import Dismissal
from testkit import As, Join, PracticeCheck, Register, AddPasskey, SoftAuthenticator, Pid

def _Unit(i, size=8):
    return [1.0 if j == i else 0.0 for j in range(size)]


ALICE = _Unit(0)
LOOKALIKE = [0.95, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
SOMEONE_ELSE = _Unit(7)


class FakePrinter:
    """Hands out the next face print from a list, whatever the photo."""

    def __init__(self, prints):
        self.prints = list(prints)

    def Available(self):
        return True

    def Print(self, image_bytes):
        return self.prints.pop(0)


def _Text(resp):
    return html.unescape(resp.data.decode())


def _Start(client, prints):
    test_client, app_module = client
    printer = FakePrinter(prints)
    app_module._FacePrinter = lambda: printer
    return test_client, app_module


def _Newcomer(test_client, app_module, username="bob", name="Bob Roe"):
    """Bob registers, adds a passkey and passes the ID check; returns his id and device."""
    test_client.get("/logout")
    Register(test_client, username, name=name)
    device = SoftAuthenticator()
    assert AddPasskey(test_client, device).status_code == 200
    page = _Text(PracticeCheck(test_client))
    return Pid(app_module, name), device, page


def test_no_match_lets_them_in(client):
    test_client, app_module = _Start(client, [ALICE, SOMEONE_ELSE])
    Join(test_client, app_module, "alice", "Alice Ames")
    bob, _, page = _Newcomer(test_client, app_module)
    assert app_module.security.IsActivated(app_module.conn, bob)


def test_a_match_asks_for_a_new_selfie_and_a_clear_one_lets_them_in(client):
    test_client, app_module = _Start(client, [ALICE, LOOKALIKE, SOMEONE_ELSE])
    Join(test_client, app_module, "alice", "Alice Ames")
    bob, _, page = _Newcomer(test_client, app_module)
    assert "One more selfie, please" in page
    assert not app_module.security.IsActivated(app_module.conn, bob)
    assert "One more selfie" in _Text(test_client.get("/join"))
    PracticeCheck(test_client, url="/join/retake")
    assert app_module.security.IsActivated(app_module.conn, bob)
    assert not any(t.same_as_id for t in app_module.house.topics.values())


def _ToJury(client, extra_members=("carol", "dan", "erin")):
    test_client, app_module = _Start(client, [ALICE] + [_Unit(2 + i) for i in range(len(extra_members))] + [LOOKALIKE, LOOKALIKE])
    alice, _ = Join(test_client, app_module, "alice", "Alice Ames")
    devices = {}
    for username in extra_members:
        test_client.get("/logout")
        _, devices[username] = Join(test_client, app_module, username, username.capitalize() + " Smith")
    bob, _, _ = _Newcomer(test_client, app_module)
    page = _Text(PracticeCheck(test_client, url="/join/retake"))
    assert "Jury Committee" in page
    topic = next(t for t in app_module.house.topics.values() if t.same_as_id)
    assert topic.target_participant_id == bob and topic.same_as_id == alice
    return test_client, app_module, alice, bob, topic, devices


def _VoteAll(test_client, topic, devices, choices):
    for (username, device), choice in zip(devices.items(), choices):
        As(test_client, username, device)
        test_client.get(f"/topic/{topic.id}")
        test_client.post(f"/topic/{topic.id}/vote", data={"choice": choice, "comment": "Looking at the photos."})


def _Close(app_module, topic):
    later = topic.last_activity_at + dt.timedelta(weeks=4)
    with app_module.LOCK:
        tick_runner.RunTick(app_module.house, app_module.conn, later)


def test_still_matching_goes_to_the_jury_and_yes_keeps_them_out(client):
    test_client, app_module, alice, bob, topic, devices = _ToJury(client)
    page = _Text(test_client.get(f"/topic/{topic.id}"))
    assert "I can't tell" in page and "Meetinghouse (the site)" in page
    # The two people it's about can't Vote on it.
    assert app_module.house.MarkRead(topic.id, alice, topic.created_at) is False
    _VoteAll(test_client, topic, devices, ["Yes", "Yes", "No"])
    _Close(app_module, topic)
    assert not app_module.security.IsActivated(app_module.conn, bob)
    assert app_module.security.GetFaceReview(app_module.conn, bob)["stage"] == "kept_out"


def test_mostly_cant_tell_lets_them_in(client):
    test_client, app_module, alice, bob, topic, devices = _ToJury(client)
    _VoteAll(test_client, topic, devices, ["Abstain", "Abstain", "Yes"])
    _Close(app_module, topic)
    assert app_module.security.IsActivated(app_module.conn, bob)


def test_yes_for_someone_who_left_lets_them_back_with_their_dismissal(client):
    test_client, app_module, alice, bob, topic, devices = _ToJury(client)
    house = app_module.house
    person = house.participants[alice]
    left = topic.created_at - dt.timedelta(days=1)
    person.left_at = left
    person.dismissals.append(Dismissal(start=left + dt.timedelta(hours=1), end=left + dt.timedelta(days=15), ordinal=1, topic_id="jury-1"))
    _VoteAll(test_client, topic, devices, ["Yes", "Yes", "Yes"])
    _Close(app_module, topic)
    newcomer = house.participants[bob]
    assert app_module.security.IsActivated(app_module.conn, bob)
    assert len(newcomer.dismissals) == 1 and newcomer.dismissals[0].end - newcomer.dismissals[0].start == dt.timedelta(days=14, hours=23)
    assert alice not in app_module.security.FacePrints(app_module.conn)
