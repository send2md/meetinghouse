"""Groups, and splitting one in two (see the design's "Growing: splitting a group").

The whole Meetinghouse is Group A. When A splits, its halves are AA and AB;
when AA splits, AAA and AAB, and so on. A Member belongs to their own group
and every group above it.

Splitting a group:

1. Deciding: any Member of a group with at least MIN_MEMBERS Members can start
   a Rules Conversation, "Should Group X split?". It is an ordinary Yes-or-No
   Rules Vote among the group's Members. When it starts, the map of where the
   group's Members live is frozen.
2. Suggesting: if it passes, the software draws a circle around each
   concentration of Members and proposes two ways to combine the circles into
   two halves (north/south and east/west, when those differ). Every Member of
   the group can suggest another way. Suggestions stay open until none has
   come in for a Seasoning Time; only each Member's first 5 restart the clock.
3. Rating: the rate-the-choices Vote (ranked.py), open at least a Minimum Life
   and until no ballot has changed for a Seasoning Time. No Threshold.
4. The winning way splits the group. Members are placed by the circle they're
   in; anyone who joined since the map was frozen goes to the half whose
   circle is nearest them.

A valid way gives each half between 40% and 60% of the Members, keeps each
half's circles next to each other, and has each half reach the edge of the
group's area, so neither half is an island inside the other.

Everything here gives the same answer every time, so anyone can check it.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import uuid

import persistence
import ranked
import security
from meetinghouse import MINIMUM_LIFE, SEASONING_TIME

MIN_MEMBERS = 30
MIN_SHARE, MAX_SHARE = 0.40, 0.60
CLUSTER_KM = 25.0  # Members within this distance of each other share a circle
RESTART_LIMIT = 5
EARTH_KM = 6371.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS splits (
    id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    phase TEXT NOT NULL,              -- deciding, suggesting, rating, done, abandoned
    frozen_at TEXT NOT NULL,
    spots_json TEXT NOT NULL,         -- {participant id: [lat, lon]} when the map was frozen
    circles_json TEXT NOT NULL DEFAULT '[]',
    last_change TEXT,
    voting_started TEXT,
    closed_at TEXT,
    result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS split_choices (
    id TEXT PRIMARY KEY,
    split_id TEXT NOT NULL,
    author_id TEXT,                   -- NULL: one of the software's ways
    label TEXT NOT NULL,
    first_half_json TEXT NOT NULL,    -- circle numbers in the first half
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS split_ballots (
    split_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    ranking_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL,
    changes INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (split_id, participant_id)
);
"""


class SplitError(Exception):
    """Something the splitting rules don't allow; the message says why."""


def InitSchema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _Iso(value):
    return None if value is None else value.isoformat()


def _Time(value):
    return None if value is None else dt.datetime.fromisoformat(value)


def _Log(conn, kind, now, **fields):
    persistence.LogEvent(conn, {"kind": kind, "at": now, **fields})


# --- Geometry -------------------------------------------------------------------------


def _Km(a, b):
    """Distance in km between two (lat, lon) points (close enough at these scales)."""
    lat = math.radians((a[0] + b[0]) / 2)
    dx = math.radians(b[1] - a[1]) * math.cos(lat) * EARTH_KM
    dy = math.radians(b[0] - a[0]) * EARTH_KM
    return math.hypot(dx, dy)


def _Centre(points):
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def _Circle(members, spots):
    centre = _Centre([spots[m] for m in members])
    radius = max(_Km(centre, spots[m]) for m in members) + 1.0
    return {"lat": round(centre[0], 5), "lon": round(centre[1], 5), "radius_km": round(radius, 1), "members": sorted(members)}


def Circles(spots):
    """Draw a circle around each concentration of Members.

    `spots`: {participant id: (lat, lon)}. Members within CLUSTER_KM of one
    another share a circle; a very small group of Members (under 5% of
    everyone, or a single person) counts with the nearest larger circle; and a
    circle holding more than 60% of everyone is cut in two along its longer
    side, again and again, until none does.
    """
    ids = sorted(spots)
    if not ids:
        return []
    parent = {i: i for i in ids}

    def Find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for n, a in enumerate(ids):
        for b in ids[n + 1:]:
            if _Km(spots[a], spots[b]) <= CLUSTER_KM:
                parent[Find(a)] = Find(b)
    clusters = {}
    for i in ids:
        clusters.setdefault(Find(i), []).append(i)
    groups = sorted(clusters.values(), key=lambda g: (-len(g), min(g)))
    small = max(2, math.ceil(0.05 * len(ids)))
    big = [g for g in groups if len(g) >= small] or [groups[0]]
    for g in groups:
        if any(g is b for b in big):
            continue
        centre = _Centre([spots[m] for m in g])
        nearest = min(big, key=lambda b: _Km(centre, _Centre([spots[m] for m in b])))
        nearest.extend(g)
    pieces = list(big)
    while True:
        too_big = next((p for p in pieces if len(p) > MAX_SHARE * len(ids)), None)
        if too_big is None:
            break
        halves = _Bisect(too_big, spots)
        if halves is None:
            break  # everyone in it lives at the same spot: it can't be cut
        pieces.remove(too_big)
        pieces.extend(halves)
    circles = [_Circle(p, spots) for p in pieces]
    return sorted(circles, key=lambda c: (-c["lat"], c["lon"]))  # numbered north to south


def _Bisect(members, spots):
    lats = [spots[m][0] for m in members]
    lons = [spots[m][1] for m in members]
    centre = _Centre([spots[m] for m in members])
    ns = _Km((min(lats), centre[1]), (max(lats), centre[1]))
    ew = _Km((centre[0], min(lons)), (centre[0], max(lons)))
    if ns == 0 and ew == 0:
        return None
    axis = 0 if ns >= ew else 1
    ordered = sorted(members, key=lambda m: (spots[m][axis], m))
    middle = len(ordered) // 2
    # Keep Members at the same spot together.
    while 0 < middle < len(ordered) and spots[ordered[middle]][axis] == spots[ordered[middle - 1]][axis]:
        middle += 1
    if middle >= len(ordered):
        middle = len(ordered) // 2
        while middle > 0 and spots[ordered[middle]][axis] == spots[ordered[middle - 1]][axis]:
            middle -= 1
        if middle == 0:
            return None
    return ordered[:middle], ordered[middle:]


def Neighbours(circles):
    """Which circles are next to each other: two circles are neighbours if no
    other circle sits inside the circle drawn with the two of them as its
    diameter (a Gabriel graph, so the neighbours never cross)."""
    near = {i: set() for i in range(len(circles))}
    points = [(c["lat"], c["lon"]) for c in circles]
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            mid = ((points[i][0] + points[j][0]) / 2, (points[i][1] + points[j][1]) / 2)
            r = _Km(points[i], points[j]) / 2
            if all(k in (i, j) or _Km(mid, points[k]) > r for k in range(len(points))):
                near[i].add(j)
                near[j].add(i)
    return near


def EdgeCircles(circles):
    """The circles on the edge of the group's area (its convex hull)."""
    points = sorted(set((c["lon"], c["lat"], i) for i, c in enumerate(circles)))
    if len(points) <= 3:
        return set(range(len(circles)))

    def Cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in points:
        while len(lower) >= 2 and Cross(lower[-2], lower[-1], p) < 0:
            lower.pop()
        lower.append(p)
    for p in reversed(points):
        while len(upper) >= 2 and Cross(upper[-2], upper[-1], p) < 0:
            upper.pop()
        upper.append(p)
    return {p[2] for p in lower + upper}


def _Connected(part, near):
    part = set(part)
    if not part:
        return False
    seen, todo = set(), [min(part)]
    while todo:
        i = todo.pop()
        if i in seen:
            continue
        seen.add(i)
        todo.extend(j for j in near[i] if j in part and j not in seen)
    return seen == part


def CheckWay(first_half, circles):
    """None if this way of splitting is valid, else why not (in plain words)."""
    first = set(first_half)
    everyone = set(range(len(circles)))
    if not first or first >= everyone or not first <= everyone:
        return "Put at least one circle in each half."
    second = everyone - first
    total = sum(len(c["members"]) for c in circles)
    share = sum(len(circles[i]["members"]) for i in first) / total if total else 0
    if not (MIN_SHARE - 1e-9 <= share <= MAX_SHARE + 1e-9):
        return f"Each half needs between 40% and 60% of the Members; this way, one half has {round(share * 100)}% and the other {round((1 - share) * 100)}%."
    near = Neighbours(circles)
    if not _Connected(first, near) or not _Connected(second, near):
        return "Each half's circles need to be next to each other."
    edge = EdgeCircles(circles)
    if not (first & edge) or not (second & edge):
        return "Each half needs to reach the edge of the group's area, so neither half is an island inside the other."
    return None


def SoftwareWays(circles):
    """Up to two valid ways: a north/south split and an east/west split."""
    ways = []
    total = sum(len(c["members"]) for c in circles)
    for label, key in (("North and south", lambda i: (-circles[i]["lat"], circles[i]["lon"])),
                       ("East and west", lambda i: (circles[i]["lon"], -circles[i]["lat"]))):
        order = sorted(range(len(circles)), key=key)
        candidates = []
        for k in range(1, len(order)):
            first = order[:k]
            if CheckWay(first, circles) is None:
                share = sum(len(circles[i]["members"]) for i in first) / total
                candidates.append((abs(share - 0.5), k, sorted(first)))
        # The most even split along this direction that isn't the same as a way already found.
        for _, _, first in sorted(candidates):
            if not any(_SamePartition(first, w[1], len(circles)) for w in ways):
                ways.append((label, first))
                break
    # If the two directions give the same way (or none), look at every way of
    # grouping the circles (when there are few enough to try them all) for the
    # most even valid one not already found.
    n = len(circles)
    if len(ways) < 2 and 2 <= n <= 14:
        candidates = []
        for mask in range(1, 2 ** (n - 1)):  # circle n-1 always in the second half: each split counted once
            first = [i for i in range(n) if mask >> i & 1]
            if CheckWay(first, circles) is None:
                share = sum(len(circles[i]["members"]) for i in first) / total
                candidates.append((abs(share - 0.5), len(first), first))
        for _, _, first in sorted(candidates):
            if not any(_SamePartition(first, w[1], n) for w in ways):
                ways.append(("Another even split", first))
                if len(ways) == 2:
                    break
    return ways


def _SamePartition(a, b, n):
    a, b = frozenset(a), frozenset(b)
    return a == b or a == frozenset(range(n)) - b


# --- Reading -------------------------------------------------------------------------


def Split(conn, split_id):
    row = conn.execute("SELECT * FROM splits WHERE id = ?", (split_id,)).fetchone()
    return _SplitDict(row) if row else None


def Splits(conn):
    return [_SplitDict(r) for r in conn.execute("SELECT * FROM splits ORDER BY frozen_at DESC")]


def SplitForTopic(conn, topic_id):
    row = conn.execute("SELECT * FROM splits WHERE topic_id = ?", (topic_id,)).fetchone()
    return _SplitDict(row) if row else None


def _SplitDict(row):
    d = dict(row)
    for key in ("frozen_at", "last_change", "voting_started", "closed_at"):
        d[key] = _Time(d[key])
    d["spots"] = {k: tuple(v) for k, v in json.loads(d.pop("spots_json")).items()}
    d["circles"] = json.loads(d.pop("circles_json") or "[]")
    d["result"] = json.loads(d.pop("result_json") or "{}")
    return d


def Choices(conn, split_id):
    rows = conn.execute("SELECT * FROM split_choices WHERE split_id = ? ORDER BY at, rowid", (split_id,)).fetchall()
    return [dict(r, at=_Time(r["at"]), first_half=json.loads(r["first_half_json"])) for r in rows]


def Ballots(conn, split_id):
    rows = conn.execute("SELECT * FROM split_ballots WHERE split_id = ? ORDER BY at", (split_id,)).fetchall()
    return [dict(r, at=_Time(r["at"]), ranking=json.loads(r["ranking_json"])) for r in rows]


def Groups(house):
    """Every group that exists, with its Members' ids."""
    names = {"A"}
    for g in house.member_groups.values():
        names.update(g[:n] for n in range(1, len(g) + 1))
    return {name: [pid for pid in house.participants if house.InGroup(pid, name)] for name in sorted(names)}


def ActiveMembers(house, conn, group, now):
    return [
        pid
        for pid, p in house.participants.items()
        if house.InGroup(pid, group) and p.IsActive(now) and security.IsActivated(conn, pid)
    ]


def CanProposeSplit(house, conn, group, participant_id, now):
    """None if they can propose splitting `group`, else why not."""
    if not house.InGroup(participant_id, group):
        return f"Only Members of Group {group} can propose splitting it."
    count = len(ActiveMembers(house, conn, group, now))
    if count < MIN_MEMBERS:
        return f"A group needs at least {MIN_MEMBERS} Members to split; Group {group} has {count}."
    if house.OpenSplitTopicFor(group) is not None or any(
        s["group_name"] == group and s["phase"] in ("deciding", "suggesting", "rating") for s in Splits(conn)
    ):
        return f"Group {group} is already deciding whether, or how, to split."
    return None


def HalfSummary(circles, first_half):
    """Members and towns on each side of a way, for showing it."""
    first = set(first_half)
    sides = []
    for side in (sorted(first), sorted(set(range(len(circles))) - first)):
        sides.append({"circles": side, "members": sum(len(circles[i]["members"]) for i in side)})
    return sides


# --- The process ------------------------------------------------------------------------


def StartDeciding(house, conn, topic, now, spot_of):
    """A "Should Group X split?" Conversation has started: freeze the map."""
    spots = {}
    for pid in ActiveMembers(house, conn, topic.split_group, now):
        spot = spot_of(pid)
        if spot is not None:
            spots[pid] = spot
    split_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO splits (id, group_name, topic_id, phase, frozen_at, spots_json) VALUES (?, ?, ?, 'deciding', ?, ?)",
        (split_id, topic.split_group, topic.id, _Iso(now), json.dumps(spots)),
    )
    conn.commit()
    _Log(conn, "split_map_frozen", now, group=topic.split_group, topic_id=topic.id, members=len(spots))
    return split_id


def Suggest(house, conn, split_id, participant_id, first_half, now):
    split = Split(conn, split_id)
    if split is None or split["phase"] != "suggesting":
        raise SplitError("Suggestions are closed.")
    if not house.InGroup(participant_id, split["group_name"]) or participant_id not in ActiveMembers(house, conn, split["group_name"], now):
        raise SplitError(f"Only Members of Group {split['group_name']} can suggest a way to split it.")
    first_half = sorted({int(i) for i in first_half})
    why_not = CheckWay(first_half, split["circles"])
    if why_not:
        raise SplitError(why_not)
    for n, choice in enumerate(Choices(conn, split_id), start=1):
        if _SamePartition(first_half, choice["first_half"], len(split["circles"])):
            raise SplitError(f"This divides the Members exactly as Choice {n} does.")
    mine = conn.execute(
        "SELECT COUNT(*) FROM split_choices WHERE split_id = ? AND author_id = ?", (split_id, participant_id)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO split_choices (id, split_id, author_id, label, first_half_json, at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), split_id, participant_id, "Suggested by a Member", json.dumps(first_half), _Iso(now)),
    )
    restarted = mine < RESTART_LIMIT
    if restarted:
        conn.execute("UPDATE splits SET last_change = ? WHERE id = ?", (_Iso(now), split_id))
    conn.commit()
    _Log(conn, "split_suggested", now, split_id=split_id, participant_id=participant_id)
    return max(0, RESTART_LIMIT - mine - 1)


def CastBallot(house, conn, split_id, participant_id, ranking, reason, now):
    split = Split(conn, split_id)
    if split is None or split["phase"] != "rating":
        raise SplitError("This Vote isn't taking ballots now.")
    if participant_id not in ActiveMembers(house, conn, split["group_name"], now):
        raise SplitError(f"Only Members of Group {split['group_name']} can Vote on how to split it.")
    if not (reason or "").strip():
        raise SplitError("Every Vote needs its reason. Please write why you're Voting this way.")
    ids = {c["id"] for c in Choices(conn, split_id)}
    ranking = [c for c in ranking if c in ids]
    if not ranking:
        raise SplitError("Rate at least one way: 1 for your first choice.")
    if len(set(ranking)) != len(ranking):
        raise SplitError("Give each way a different number.")
    previous = conn.execute(
        "SELECT changes FROM split_ballots WHERE split_id = ? AND participant_id = ?", (split_id, participant_id)
    ).fetchone()
    changes = previous["changes"] + 1 if previous else 1
    conn.execute(
        """INSERT INTO split_ballots (split_id, participant_id, ranking_json, reason, at, changes) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(split_id, participant_id) DO UPDATE SET ranking_json = excluded.ranking_json,
             reason = excluded.reason, at = excluded.at, changes = excluded.changes""",
        (split_id, participant_id, json.dumps(ranking), reason.strip(), _Iso(now), changes),
    )
    if changes <= RESTART_LIMIT:
        conn.execute("UPDATE splits SET last_change = ? WHERE id = ?", (_Iso(now), split_id))
    conn.commit()
    _Log(conn, "split_ballot", now, split_id=split_id, participant_id=participant_id)
    return max(0, RESTART_LIMIT - changes)


def Tick(house, conn, now, spot_of=None):
    """Move each split along as its dates come. Returns the splits that finished."""
    InitSchema(conn)
    finished = []
    for split in Splits(conn):
        if split["phase"] == "deciding":
            topic = house.topics.get(split["topic_id"])
            if topic is None or not topic.closed:
                continue
            if topic.result is None or topic.result.value != "PASSED":
                conn.execute("UPDATE splits SET phase = 'abandoned', closed_at = ? WHERE id = ?", (_Iso(now), split["id"]))
                conn.commit()
                continue
            _StartSuggesting(house, conn, split, now)
        elif split["phase"] == "suggesting" and now >= split["last_change"] + SEASONING_TIME:
            conn.execute(
                "UPDATE splits SET phase = 'rating', voting_started = ?, last_change = ? WHERE id = ?",
                (_Iso(now), _Iso(now), split["id"]),
            )
            conn.commit()
            _Log(conn, "split_vote_started", now, split_id=split["id"], group=split["group_name"], ways=len(Choices(conn, split["id"])))
        elif split["phase"] == "rating" and now >= split["voting_started"] + MINIMUM_LIFE and now >= split["last_change"] + SEASONING_TIME:
            _Finish(house, conn, split, now, spot_of)
            finished.append(Split(conn, split["id"]))
    return finished


def _StartSuggesting(house, conn, split, now):
    still = set(ActiveMembers(house, conn, split["group_name"], now))
    spots = {pid: spot for pid, spot in split["spots"].items() if pid in still}
    circles = Circles(spots)
    conn.execute(
        "UPDATE splits SET phase = 'suggesting', circles_json = ?, last_change = ? WHERE id = ?",
        (json.dumps(circles), _Iso(now), split["id"]),
    )
    for label, first in SoftwareWays(circles):
        conn.execute(
            "INSERT INTO split_choices (id, split_id, author_id, label, first_half_json, at) VALUES (?, ?, NULL, ?, ?, ?)",
            (str(uuid.uuid4()), split["id"], f"The software's way: {label.lower()}", json.dumps(first), _Iso(now)),
        )
    conn.commit()
    _Log(conn, "split_suggestions_open", now, split_id=split["id"], group=split["group_name"], circles=len(circles))


def _Finish(house, conn, split, now, spot_of):
    choices = Choices(conn, split["id"])
    voters = set(ActiveMembers(house, conn, split["group_name"], now))
    ballots = [b["ranking"] for b in Ballots(conn, split["id"]) if b["participant_id"] in voters]
    result = {}
    if not choices:
        result["winner"] = None
        conn.execute(
            "UPDATE splits SET phase = 'abandoned', closed_at = ?, result_json = ? WHERE id = ?",
            (_Iso(now), json.dumps(result), split["id"]),
        )
        conn.commit()
        _Log(conn, "split_not_possible", now, split_id=split["id"], group=split["group_name"])
        return
    count = ranked.Count(ballots, [c["id"] for c in choices], seats=1)
    winner = next(c for c in choices if c["id"] == count["winners"][0])
    result.update(winner=winner["id"], rounds=count["rounds"])
    circles = split["circles"]
    first = set(winner["first_half"])
    northmost = 0  # circles are numbered north to south
    names = {}
    for i in range(len(circles)):
        names[i] = split["group_name"] + ("A" if (i in first) == (northmost in first) else "B")
    placed = {}
    for i, circle in enumerate(circles):
        for pid in circle["members"]:
            placed[pid] = names[i]
    for pid, participant in house.participants.items():
        if participant.HasLeft() or not house.InGroup(pid, split["group_name"]) or pid in placed:
            continue
        spot = spot_of(pid) if spot_of else None
        if spot is not None and circles:
            nearest = min(range(len(circles)), key=lambda i: _Km(spot, (circles[i]["lat"], circles[i]["lon"])))
            placed[pid] = names[nearest]
    for pid, group in placed.items():
        house.member_groups[pid] = group
        persistence.SetMemberGroup(conn, pid, group)
    result["sizes"] = {g: sum(1 for v in placed.values() if v == g) for g in sorted(set(names.values()))}
    conn.execute(
        "UPDATE splits SET phase = 'done', closed_at = ?, result_json = ? WHERE id = ?",
        (_Iso(now), json.dumps(result), split["id"]),
    )
    conn.commit()
    _Log(conn, "group_split", now, split_id=split["id"], group=split["group_name"], sizes=result["sizes"])


def PlaceNewMember(house, conn, participant_id, spot):
    """A new Member joins: walk down the groups that have split, to the half
    whose circle is nearest them."""
    group = "A"
    while True:
        done = next((s for s in Splits(conn) if s["group_name"] == group and s["phase"] == "done"), None)
        if done is None or spot is None or not done["circles"]:
            break
        circles = done["circles"]
        winner = next((c for c in Choices(conn, done["id"]) if c["id"] == done["result"].get("winner")), None)
        if winner is None:
            break
        nearest = min(range(len(circles)), key=lambda i: _Km(spot, (circles[i]["lat"], circles[i]["lon"])))
        first = set(winner["first_half"])
        group = group + ("A" if (nearest in first) == (0 in first) else "B")
    if group != "A":
        house.member_groups[participant_id] = group
        persistence.SetMemberGroup(conn, participant_id, group)
    return group
