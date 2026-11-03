"""Fill a fresh database with a pretend Meeting, for practice and screenshots.

    python3 sandbox.py /path/to/sandbox.db [/path/to/uploads]

Everyone in it is made up: eight Members in and around Ithaca, New York,
a few Conversations in each Committee with Comments and Votes, one
Conversation still waiting for its Question, and one already decided.
Nobody can log in to these accounts with a passkey; the sandbox site
lets visitors step into them instead (see MEETINGHOUSE_SANDBOX in app.py).

Never point this at the live database: it refuses to touch a database
that already has Members in it.
"""

from __future__ import annotations

import datetime as dt
import os
import struct
import sys
import zlib

import persistence
import security
import tick_runner
from meetinghouse import RuleChange, Subsection, VoteChoice

MEMBERS = [
    # user name, name, town, ZIP, photo color
    ("ruth", "Ruth Alvarez", "Ithaca", "14850", (196, 120, 92)),
    ("samuel", "Samuel Okafor", "Ithaca", "14850", (92, 130, 170)),
    ("dorothy", "Dorothy Chen", "Trumansburg", "14886", (150, 110, 160)),
    ("thomas", "Thomas Whitfield", "Dryden", "13053", (110, 150, 100)),
    ("grace", "Grace Park", "Ithaca", "14850", (200, 160, 80)),
    ("elijah", "Elijah Moore", "Groton", "13073", (90, 120, 120)),
    ("hannah", "Hannah Lindqvist", "Lansing", "14882", (170, 100, 110)),
    ("marcus", "Marcus Bell", "Newfield", "14867", (120, 110, 90)),
]


def _Photo(color, size=160):
    """A plain colored square PNG standing in for a Member's selfie."""

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes(color) * size
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


def Seed(db_path, upload_dir, now=None):
    now = now or dt.datetime.utcnow().replace(microsecond=0)
    conn = persistence.Connect(db_path)
    security.InitSchema(conn)
    house = persistence.LoadHouse(conn)
    if house.participants:
        raise SystemExit(f"{db_path} already has Members in it; the sandbox only fills an empty database.")

    def Ago(days, hours=0):
        return now - dt.timedelta(days=days, hours=hours)

    people = {}
    for username, name, town, zip_code, color in MEMBERS:
        joined = Ago(120)
        person = house.RegisterParticipant(name)
        person.SignForm(signed_date=Ago(100).date())
        persistence.SaveParticipant(conn, person)
        persistence.SetCredentials(conn, person.id, username, security.NO_PASSWORD)
        security.CreatePendingStatus(conn, person.id)
        security.MarkActivated(conn, person.id, joined)
        folder = os.path.join(upload_dir, person.id)
        os.makedirs(folder, exist_ok=True)
        photo = os.path.join(folder, "photo.png")
        with open(photo, "wb") as f:
            f.write(_Photo(color))
        security.UpdateMember(
            conn, person.id, email=f"{username}@example.com", town=town, zip=zip_code, photo_path=photo,
            verified_at=joined, verified_country="United States", verified_ref="sandbox",
        )
        people[username] = person.id

    def Comment(topic, who, text, when):
        house.AddPost(topic.id, people[who], text, when)

    def Vote(topic, who, choice, reason, when):
        house.CastVote(topic.id, people[who], choice, reason, when)

    def Look(topic, who, when):
        house.MarkRead(topic.id, people[who], when)

    yes, no, abstain = VoteChoice.YES, VoteChoice.NO, VoteChoice.ABSTAIN

    # 1. Decided a while ago: a cleanup day (passed).
    cleanup = house.CreateTopic(
        people["grace"], Subsection.FORUM, "Spring cleanup day", Ago(70),
        description="The yard and the shed need a good clearing out before summer.",
        poll_question="Shall we hold a cleanup day on the first Saturday in May?",
    )
    Comment(cleanup, "samuel", "I can bring two rakes and the wheelbarrow.", Ago(69))
    Comment(cleanup, "hannah", "Could we start at 10 rather than 9? The bus from Lansing gets in at 9:40.", Ago(68))
    Vote(cleanup, "grace", yes, "The shed is a hazard as it is.", Ago(68))
    Vote(cleanup, "samuel", yes, "Happy to help.", Ago(67))
    Vote(cleanup, "hannah", yes, "Yes, if we start at 10.", Ago(66))
    Vote(cleanup, "thomas", abstain, "I'll be away that weekend, but I don't stand in the way.", Ago(66))
    Vote(cleanup, "ruth", yes, "A good way to meet the newer Members, too.", Ago(65))
    Look(cleanup, "elijah", Ago(65))
    tick_runner.RunTick(house, conn, Ago(30))

    # 2. Open, with a Question attached later by someone else: chairs.
    chairs = house.CreateTopic(
        people["samuel"], Subsection.FORUM, "Chairs for the hall", Ago(12),
        description="Three of our folding chairs broke at the last meeting, and more are wobbly. What should we do?",
    )
    Comment(chairs, "ruth", "I looked at repairing them. New hinges cost almost as much as a new chair.", Ago(11, 20))
    Comment(chairs, "dorothy", "The library is giving away its old wooden chairs in December.\n\nThey're heavier, but they last. We could take 30 of those.", Ago(11, 2))
    Comment(chairs, "thomas", "Heavy chairs are hard for some of us to set up. Could we buy a mix?", Ago(10))
    house.AttachQuestion(chairs.id, people["dorothy"], "Shall we take 30 of the library's wooden chairs and buy 10 light folding ones?", Ago(9))
    Vote(chairs, "dorothy", yes, "Free chairs that last, plus a few light ones for anyone who needs them.", Ago(9))
    Vote(chairs, "samuel", yes, "A sensible mix.", Ago(8))
    Vote(chairs, "thomas", yes, "Thank you for including light ones.", Ago(8))
    Vote(chairs, "ruth", abstain, "I'd rather buy all new, but I won't stand in the way.", Ago(7))
    Vote(chairs, "marcus", no, "Where would we store 40 chairs? The closet holds 25.", Ago(6))
    Comment(chairs, "grace", "Marcus is right about the closet. The shed has room since the cleanup.", Ago(5))
    Look(chairs, "hannah", Ago(5))

    # 3. Open, Discussion only (no Question yet): a garden.
    garden = house.CreateTopic(
        people["hannah"], Subsection.FORUM, "A shared garden behind the meetinghouse", Ago(4),
        description="The strip of lawn behind the building gets full sun. Would anyone like to grow vegetables there together?",
    )
    Comment(garden, "elijah", "I'd help. I have seeds left over from this year.", Ago(3, 12))
    Comment(garden, "grace", "Who would water it in August, when half of us are away?", Ago(3))
    Comment(garden, "hannah", "Good question. Maybe a sign-up sheet, a week each?", Ago(2))

    # 4. Open in the Rules Committee: a small change to the Forum Approval.
    rules = house.CreateTopic(
        people["thomas"], Subsection.RULES, None, Ago(6),
        description="Two good Forum proposals failed this year with 64% in favor. A slightly lower bar would let those pass.",
        proposed_change=RuleChange(Subsection.FORUM, "approval_fraction", 0.64),
    )
    Comment(rules, "ruth", "I'd rather we talk until more of us agree. Two-thirds is a Quaker kind of number.", Ago(5))
    Vote(rules, "thomas", yes, "It's a small step, and we can always step back.", Ago(6))
    Vote(rules, "ruth", no, "Keep 66.7%. Discussion should do the work, not a lower bar.", Ago(5))
    Vote(rules, "marcus", yes, "64% is still a clear majority.", Ago(4))

    for topic in house.topics.values():
        persistence.SaveTopic(conn, topic)
    for entry in house.event_log:
        persistence.LogEvent(conn, entry)
    return people


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    db = sys.argv[1]
    uploads = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(db)), "uploads")
    made = Seed(db, uploads)
    print(f"Made a sandbox Meeting with {len(made)} Members in {db}")
