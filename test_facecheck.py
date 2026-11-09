"""Meetinghouse's own duplicate-face check: face prints, matching, and how long they're kept."""

from __future__ import annotations

import datetime as dt

import facecheck
import tick_runner
from testkit import Join


def test_similarity_and_matches():
    a = [1.0, 0.0, 0.0]
    near = [0.9, 0.3, 0.0]
    far = [0.0, 1.0, 0.0]
    assert facecheck.Similarity(a, a) == 1.0
    assert facecheck.Similarity(a, far) == 0.0
    found = facecheck.Matches(a, {"near": near, "far": far, "none": None})
    assert [pid for pid, _ in found] == ["near"]


def test_printer_without_models_says_so(tmp_path):
    printer = facecheck.FacePrinter(str(tmp_path))
    assert not printer.Available()
    assert printer.problem
    assert printer.Print(b"not a photo") is None


def test_face_print_goes_when_a_member_leaves(client):
    test_client, app_module = client
    jane, _ = Join(test_client, app_module, "janedoe", "Jane Doe")
    security, conn = app_module.security, app_module.conn
    security.SetFacePrint(conn, jane, facecheck.Encode([0.1, 0.2, 0.3]))
    test_client.post("/account/leave", data={"confirm": "yes"})
    assert jane not in security.FacePrints(conn)


def test_face_print_kept_while_a_jury_conversation_about_them_is_open(client):
    test_client, app_module = client
    jane, _ = Join(test_client, app_module, "janedoe", "Jane Doe")
    house, conn, security = app_module.house, app_module.conn, app_module.security
    from meetinghouse import Subsection

    other = house.RegisterParticipant("Bob Roe")
    other.SignForm(signed_date=dt.date.today())
    now = app_module.Now()
    house.CreateTopic(other.id, Subsection.JURY, "About Jane", now, target_participant_id=jane, poll_question="Dismiss?")
    security.SetFacePrint(conn, jane, facecheck.Encode([0.1, 0.2, 0.3]))
    test_client.post("/account/leave", data={"confirm": "yes"})
    assert jane in security.FacePrints(conn)
    for topic in house.topics.values():
        topic.closed = True  # the Jury Conversation ends without a Dismissal
    tick_runner.ForgetFacePrints(house, conn, now)
    assert jane not in security.FacePrints(conn)
