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
from meetinghouse import RuleChange, Subsection, UtcNow, VoteChoice

MEMBERS = [
    # user name, name, town, postal code, photo color
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
    now = now or UtcNow().replace(microsecond=0)
    conn = persistence.Connect(db_path)
    security.InitSchema(conn)
    house = persistence.LoadHouse(conn)
    if house.participants:
        raise SystemExit(f"{db_path} already has Members in it; the sandbox only fills an empty database.")

    def Ago(days, hours=0):
        return now - dt.timedelta(days=days, hours=hours)

    people = {}
    joins = []
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
            conn, person.id, email=f"{username}@example.com", town=town, zip=zip_code, country="United States", photo_path=photo,
            verified_at=joined, verified_country="United States", verified_ref="sandbox",
        )
        people[username] = person.id
        joins.append({"kind": "member_joined", "at": joined, "participant_id": person.id})

    def Comment(topic, who, text, when):
        house.AddPost(topic.id, people[who], text, when)

    def Vote(topic, who, choice, reason, when):
        house.CastVote(topic.id, people[who], choice, reason, when)

    def Look(topic, who, when):
        house.MarkRead(topic.id, people[who], when)

    yes, no, abstain = VoteChoice.YES, VoteChoice.NO, VoteChoice.ABSTAIN

    # 1. Decided a while ago (passed): plastic bags.
    bags = house.CreateTopic(
        people["grace"], Subsection.FORUM, "Plastic bags at the grocery store", Ago(70),
        description="Plastic bags blow into the creek every spring. Some towns have asked their stores to stop handing them out.",
        poll_question="Shall we ask our grocery stores to stop giving out plastic bags?",
    )
    Comment(bags, "samuel", "Most of us already bring cloth bags. The stores might welcome being asked.", Ago(69))
    Comment(bags, "hannah", "Paper bags tear in the rain, though. Could the stores keep a few for people who forget?", Ago(68))
    Vote(bags, "grace", yes, "The creek cleanup found more bags than anything else.", Ago(68))
    Vote(bags, "samuel", yes, "It's a request, not a law. Easy to try.", Ago(67))
    Vote(bags, "hannah", yes, "Yes, as long as there are spares for people who forget.", Ago(66))
    Vote(bags, "thomas", abstain, "I'm not sure it will change much, but I won't stand in the way.", Ago(66))
    Vote(bags, "ruth", yes, "Small changes add up.", Ago(65))
    Look(bags, "elijah", Ago(65))
    house.Tick(Ago(30))  # closes it; saved and logged with everything else below

    # 2. Open, with a Question attached later by someone else: daylight saving time.
    clocks = house.CreateTopic(
        people["samuel"], Subsection.FORUM, "Changing the clocks twice a year", Ago(12),
        description="We just turned the clocks back again. Does anyone else find the change hard?",
    )
    Comment(clocks, "ruth", "It takes me a week to get used to it, every time.", Ago(11, 20))
    Comment(clocks, "dorothy", "I'd keep one time all year.\n\nThe question is which one: lighter evenings, or lighter mornings?", Ago(11, 2))
    Comment(clocks, "thomas", "Lighter mornings matter for children waiting for the school bus in December.", Ago(10))
    house.AttachQuestion(clocks.id, people["dorothy"], "Do we want to change to no daylight saving time?", Ago(9))
    Vote(clocks, "dorothy", yes, "One time all year, and the mornings stay light for the school bus.", Ago(9))
    Vote(clocks, "samuel", yes, "No more changing the clocks.", Ago(8))
    Vote(clocks, "thomas", yes, "For the children's mornings.", Ago(8))
    Vote(clocks, "ruth", abstain, "I'd rather keep summer evenings, but I won't stand in the way.", Ago(7))
    Vote(clocks, "marcus", no, "Summer evenings are when I'm outdoors. I'd lose an hour of light.", Ago(6))
    Comment(clocks, "grace", "Marcus has a point. Could we ask which people care about more, mornings or evenings?", Ago(5))
    Look(clocks, "hannah", Ago(5))

    # 3. Open, with a Question from the start: vaccines.
    vaccines = house.CreateTopic(
        people["hannah"], Subsection.FORUM, "Vaccines", Ago(5),
        description="People here feel strongly about this, on both sides. Let's hear each other out.",
        poll_question="Do we want to allow vaccines in our bodies?",
    )
    Comment(vaccines, "elijah", "I think each person should decide what goes into their own body, with their doctor.", Ago(4, 12))
    Comment(vaccines, "grace", "Vaccines also protect people who can't have them, like newborns. That's why I care what my neighbors do.", Ago(4))
    Comment(vaccines, "marcus", "Can we agree on something narrower? For example, that nobody is pressured either way.", Ago(3))
    Vote(vaccines, "grace", yes, "They protect the people around us, not just ourselves.", Ago(3))
    Vote(vaccines, "elijah", no, "Not as a group decision. It belongs to each person.", Ago(2))
    Vote(vaccines, "marcus", abstain, "I'd rather we keep talking before anyone Votes.", Ago(2))

    # 4. Open, Discussion only (no Question yet): trees.
    trees = house.CreateTopic(
        people["hannah"], Subsection.FORUM, "Trees along Main Street", Ago(4),
        description="Main Street is hot and bare in summer. Should we plant shade trees?",
    )
    Comment(trees, "elijah", "I'd help plant them. Maples do well here.", Ago(3, 12))
    Comment(trees, "grace", "Who would water them for the first two summers?", Ago(3))
    Comment(trees, "hannah", "Good question. Maybe each shop could look after the tree in front of it?", Ago(2))

    # 5. Open in the Rules Committee: a small change to the Forum Approval.
    rules = house.CreateTopic(
        people["thomas"], Subsection.RULES, None, Ago(6),
        description="Two Forum Questions failed this year with 64% in favor. A slightly lower bar would have let those pass.",
        proposed_change=RuleChange(Subsection.FORUM, "approval_fraction", 0.64),
    )
    Comment(rules, "ruth", "I'd rather we talk until more of us agree. Two-thirds is a Quaker kind of number.", Ago(5))
    Vote(rules, "thomas", yes, "It's a small step, and we can always step back.", Ago(6))
    Vote(rules, "ruth", no, "Keep 66.7%. Discussion should do the work, not a lower bar.", Ago(5))
    Vote(rules, "marcus", yes, "64% is still a clear majority.", Ago(4))

    for topic in house.topics.values():
        persistence.SaveTopic(conn, topic)
    for entry in sorted(joins + house.event_log, key=lambda e: e["at"]):
        persistence.LogEvent(conn, entry)
    return people


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    db = sys.argv[1]
    uploads = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(db)), "uploads")
    made = Seed(db, uploads)
    print(f"Made a sandbox Meeting with {len(made)} Members in {db}")
