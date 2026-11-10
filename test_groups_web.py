"""Splitting a group, on the site."""

from __future__ import annotations

import datetime as dt
import html
import io
import os
import re
import zipfile

import geo
import groups
from meetinghouse import VoteChoice
from testkit import Join

CODES = {"10001": ("North", 42.90, -76.50), "10002": ("South", 42.10, -76.50), "10003": ("East", 42.50, -75.90), "10004": ("West", 42.50, -77.10)}


def _Text(resp):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(resp.data.decode())))


def _Geo(app_module):
    rows = "".join(f"US\t{code}\t{town}\tState\tST\t\t\t\t\t{lat}\t{lon}\t4\n" for code, (town, lat, lon) in CODES.items())
    rows += "US\t14850\tIthaca\tNew York\tNY\t\t\t\t\t42.90\t-76.49\t4\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("allCountries.txt", rows)
    geo.Build(os.path.dirname(app_module.GEO_PATH), buffer.getvalue(), "US\tUSA\t840\tUS\tUnited States\n")


def test_splitting_on_the_site(client):
    test_client, app_module = client
    _Geo(app_module)
    house, conn, security = app_module.house, app_module.conn, app_module.security
    jane, _ = Join(test_client, app_module, "janedoe", "Jane Doe")  # Ithaca, next to "North"
    now = app_module.Now()
    others = []
    for n in range(35):
        code = list(CODES)[n % 4]
        p = house.RegisterParticipant(f"Member {n}")
        p.SignForm(signed_date=now.date())
        security.CreatePendingStatus(conn, p.id)
        security.MarkActivated(conn, p.id, now)
        security.UpdateMember(conn, p.id, town=CODES[code][0], zip=code, country="United States")
        others.append(p.id)
    page = _Text(test_client.get("/groups"))
    assert "Why you think Group A should split" in page
    resp = test_client.post("/groups/A/split", data={"description": "We're too many to know each other."})
    topic_id = resp.headers["Location"].rsplit("/", 1)[-1]
    topic = house.topics[topic_id]
    assert topic.split_group == "A"
    assert "See the frozen map" in _Text(test_client.get(f"/topic/{topic_id}"))
    for pid in others[:25]:
        house.CastVote(topic_id, pid, VoteChoice.YES, "Yes.", now)
    later = now + dt.timedelta(weeks=4)
    with app_module.LOCK:
        app_module.tick_runner.RunTick(house, conn, later, app_module._SpotOf)
    (split,) = groups.Splits(conn)
    assert split["phase"] == "suggesting"
    page = _Text(test_client.get(f"/split/{split['id']}"))
    assert "Suggest a way to split" in page and "The software's way" in page and "Circle 1" in page
    refused = _Text(test_client.post(f"/split/{split['id']}/suggest", data={"first": ["0"]}, follow_redirects=True))
    assert "between 40% and 60%" in refused
    later += dt.timedelta(weeks=3)
    with app_module.LOCK:
        app_module.tick_runner.RunTick(house, conn, later, app_module._SpotOf)
    choices = groups.Choices(conn, split["id"])
    page = _Text(test_client.post(
        f"/split/{split['id']}/ballot", data={f"rank_{choices[0]['id']}": "1", "reason": "North and south feels natural."}, follow_redirects=True
    ))
    assert "Vote recorded" in page
    later += dt.timedelta(weeks=4)
    with app_module.LOCK:
        app_module.tick_runner.RunTick(house, conn, later, app_module._SpotOf)
    assert groups.Split(conn, split["id"])["phase"] == "done"
    assert house.GroupOf(jane) in ("AA", "AB")
    page = _Text(test_client.get("/topic/new/forum"))
    assert "Which group is this for?" in page and f"Group {house.GroupOf(jane)}" in page
    assert "Group A split" in _Text(test_client.get("/log"))
