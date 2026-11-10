"""Meetinghouse: an online emulation of a Quaker Meeting for Business.

The Meetinghouse has three subsections that share common Topic/Poll
mechanics: Forum (outside matters), Rules (how Meetinghouse itself
works, including its software), and Jury (temporarily dismissing a
Participant).

Time is passed in explicitly as a `now` argument to every action,
rather than read from the system clock inside this module. A caller
(a web handler, a periodic job, a test) supplies `now`; this keeps the
whole engine deterministic and easy to test by simply constructing
later datetimes, and keeps scheduling, persistence, and delivery of
the Daily Summary as someone else's job.

The design these rules implement is recorded in the "Meetinghouse
Design" document (October 2026). In brief:

Words. The design document's words differ from some names in this
code, which predate them. In code a `Topic` is a Conversation, a
`subsection` is a Committee, a `DiscussionPost` is a Comment, the
`poll_question` is the Question, `quiet_time` is the Seasoning Time,
`topic.readers` are the Contactors, `voter_fraction` is the Threshold
and `approval_fraction` the Approval.

* Forum votes are Yes / Abstain / No, and Abstain ("standing aside")
  counts with Yes. Rules and Jury votes are Yes / No only.
* A Forum Conversation may start without a Question; any Contactor
  may attach one later and becomes its Pollster. Rules and Jury
  Conversations always start with one, asked by their Initiator.
* A Conversation closes once its Question has been attached for its
  Minimum Life and it has then gone without new activity for its
  Seasoning Time. Each person's first 5 votes and first 5 Comments
  restart that clock; after that they still count but no longer keep
  the Conversation open. A Comment nearly identical to one the same
  person already made there uses up one of their 5 but never restarts
  it. A Conversation without a Question never closes.
* The Threshold is Voters / Contactors. A Contactor is a current
  Member who has touched the Conversation (opened it, commented or
  voted). A paused Member (yearly check lapsed) stays a Contactor
  only where they voted, and those votes count.
* A Rules Conversation changes ONE setting (Threshold or Approval) of
  one Committee, by at most 5% of its current value (always at least
  1 point), in whole percentages, never below 9% or above 100%; only
  one open Conversation per setting at a time. Seasoning Time and
  Minimum Life are fixed. A Rules Conversation may instead approve a
  software change.
* Only one Jury Topic per person may be open. A passed one dismisses
  the person (2 weeks, doubling each time, never permanently); their
  votes and Reader status come off every open Topic, whose quiet clock
  restarts and which then carry a note naming them. When the dismissal
  ends they return quietly, with all rights; old votes stay removed.
* A Topic/Poll with no Readers, or no live votes, FAILS rather than
  raising an error.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import difflib
import math
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class Subsection(Enum):
    """Which of the Meetinghouse's three subsections a Topic belongs to."""

    FORUM = "Forum"
    RULES = "Rules"
    JURY = "Jury"


class VoteChoice(Enum):
    """The ways a Participant may Vote on a Poll (Abstain: Forum only)."""

    YES = "Yes"
    ABSTAIN = "Abstain"
    NO = "No"


class PollResult(Enum):
    """The outcome of an evaluated Poll."""

    PASSED = "PASSED"
    FAILED = "FAILED"


# Each person's first RESTART_LIMIT votes, and separately their first
# RESTART_LIMIT discussion posts, restart a Topic's quiet clock.
RESTART_LIMIT = 5

# Two posts by the same person on the same Topic at least this similar
# (after ignoring case, punctuation and spacing) count as near-identical.
NEAR_IDENTICAL_RATIO = 0.9

# A Rules Conversation may move a setting by at most this share of its
# current value (5%), and always by at least one whole point.
RULE_STEP = 0.05

# The settings a Rules Conversation may change, and their names.
SETTING_NAMES = {"voter_fraction": "Threshold", "approval_fraction": "Approval"}

# Every setting, in every Committee, stays within these limits.
SETTING_MIN = 0.09
SETTING_MAX = 1.0
SETTING_LIMITS = {
    (subsection, setting): (SETTING_MIN, SETTING_MAX)
    for subsection in Subsection
    for setting in SETTING_NAMES
}


def _RoundHalfUp(x):
    return int(math.floor(x + 0.5))


def AllowedRange(current, low=SETTING_MIN, high=SETTING_MAX):
    """The whole percentages a setting at `current` may move to, as (lowest, highest).

    At most 5% of the current value either way, rounded to whole
    percentages, but always at least 1 point, and never past the limits.
    66.7% gives (63, 70); 25% gives (24, 26); 9% gives (9, 10).
    """
    now = current * 100
    down = min(_RoundHalfUp(now * (1 - RULE_STEP)), math.floor(now - 1 + _EPSILON))
    up = max(_RoundHalfUp(now * (1 + RULE_STEP)), math.ceil(now + 1 - _EPSILON))
    return max(down, _RoundHalfUp(low * 100)), min(up, _RoundHalfUp(high * 100))


def AllowedValues(current, low=SETTING_MIN, high=SETTING_MAX):
    """Every whole percentage a setting at `current` may change to, as fractions."""
    lo, hi = AllowedRange(current, low, high)
    return [p / 100 for p in range(lo, hi + 1) if abs(p / 100 - current) > _EPSILON]

_EPSILON = 1e-9


def UtcNow():
    """The current time in UTC, without a time-zone label (how every time here is stored)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def Percent(fraction):
    """Format a fraction the way people read it: 0.667 -> '66.7%', 0.5 -> '50%'."""
    text = f"{fraction * 100:.1f}"
    return (text[:-2] if text.endswith(".0") else text) + "%"


class MeetinghouseError(Exception):
    """Base class for every domain error this module raises."""


class NotAParticipant(MeetinghouseError):
    """Raised when a person without a current, signed form tries to act."""


class ParticipantDismissed(MeetinghouseError):
    """Raised when a currently-dismissed Participant tries to act."""


class TopicClosed(MeetinghouseError):
    """Raised when an action targets a Topic that has already closed."""


class NoPollOnTopic(MeetinghouseError):
    """Raised when a Vote is attempted on a Topic that carries no Poll."""


class InvalidTopic(MeetinghouseError):
    """Raised when a Topic is created without the fields its subsection requires."""


class NotInGroup(InvalidTopic):
    """Raised when someone outside a Conversation's group tries to take part in it."""


class InvalidRuleChange(InvalidTopic):
    """Raised when a proposed Rules change is out of bounds or too big a step."""


class DuplicateTopic(InvalidTopic):
    """Raised when an open Topic about the same thing already exists."""

    def __init__(self, message, existing_topic_id):
        super().__init__(message)
        self.existing_topic_id = existing_topic_id


@dataclass(frozen=True)
class RuleConfig:
    """The Rule governing one subsection.

    A Topic captures a snapshot of its subsection's RuleConfig at the
    moment it is created, and follows that snapshot for its entire
    lifetime, even if the Rule changes while the Topic is still open.
    """

    quiet_time: dt.timedelta
    min_life: dt.timedelta
    voter_fraction: float
    approval_fraction: float

    def __post_init__(self):
        if not 0 <= self.voter_fraction <= 1:
            raise ValueError("voter_fraction must be in [0, 1]")
        if not 0 <= self.approval_fraction <= 1:
            raise ValueError("approval_fraction must be in [0, 1]")


@dataclass(frozen=True)
class RuleChange:
    """What a Rules Topic proposes: one setting of one subsection, to one value."""

    subsection: Subsection
    setting: str
    value: float

    def Describe(self):
        return f"{self.subsection.value} {SETTING_NAMES.get(self.setting, self.setting)}"


# Fixed for every Committee: no Rules vote can change these.
# Who starts the Conversations the site starts by itself (same-person checks).
MEETINGHOUSE_ID = "meetinghouse"
# A same-person Question passes with 51% of its Yes and No Votes ("I can't
# tell" counts toward the Threshold but not toward Approval).
SAME_PERSON_APPROVAL = 0.51

SEASONING_TIME = dt.timedelta(weeks=3)
MINIMUM_LIFE = dt.timedelta(weeks=3)


def DefaultRules():
    """Build the starting RuleConfig for each subsection, per the spec."""
    return {
        Subsection.FORUM: RuleConfig(
            quiet_time=SEASONING_TIME,
            min_life=MINIMUM_LIFE,
            voter_fraction=1 / 4,
            approval_fraction=2 / 3,
        ),
        Subsection.RULES: RuleConfig(
            quiet_time=SEASONING_TIME,
            min_life=MINIMUM_LIFE,
            voter_fraction=1 / 2,
            approval_fraction=51 / 100,
        ),
        Subsection.JURY: RuleConfig(
            quiet_time=SEASONING_TIME,
            min_life=MINIMUM_LIFE,
            voter_fraction=1 / 2,
            approval_fraction=1 / 5,
        ),
    }


@dataclass
class ParticipationForm:
    """A signed participation form (or its online equivalent)."""

    signed_date: dt.date
    scan_ref: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class Dismissal:
    """One period of temporary dismissal, imposed by a passed Jury Poll."""

    start: dt.datetime
    end: dt.datetime
    ordinal: int
    topic_id: str


@dataclass
class Participant:
    """A person who has signed a participation form for the Meetinghouse."""

    name: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    forms: List[ParticipationForm] = field(default_factory=list)
    dismissals: List[Dismissal] = field(default_factory=list)
    # When they left Meetinghouse, if they did. Leaving is for good; to come
    # back they join again as a new Member.
    left_at: Optional[dt.datetime] = None

    def HasLeft(self):
        return self.left_at is not None

    def SignForm(self, signed_date, scan_ref=""):
        """Record a freshly signed participation form."""
        form = ParticipationForm(signed_date=signed_date, scan_ref=scan_ref)
        self.forms.append(form)
        return form

    def LatestFormDate(self):
        """Return the date of this Participant's most recently signed form."""
        if not self.forms:
            return None
        return max(form.signed_date for form in self.forms)

    def HasCurrentForm(self, now):
        """Return whether the latest signed form is less than a year old."""
        latest_date = self.LatestFormDate()
        if latest_date is None:
            return False
        return now.date() - latest_date <= dt.timedelta(days=365)

    def IsDismissed(self, now):
        """Return whether this Participant is currently under dismissal."""
        return any(dismissal.start <= now < dismissal.end for dismissal in self.dismissals)

    def CurrentDismissal(self, now):
        """Return the Dismissal in effect right now, if any."""
        for dismissal in self.dismissals:
            if dismissal.start <= now < dismissal.end:
                return dismissal
        return None

    def NextDismissalDuration(self):
        """Return how long a new dismissal would last: 2 weeks, doubling each time."""
        if not self.dismissals:
            return dt.timedelta(weeks=2)
        most_recent = self.dismissals[-1]
        return (most_recent.end - most_recent.start) * 2

    def IsParticipant(self, now):
        """Return whether this person currently holds valid Participant status."""
        return not self.HasLeft() and self.HasCurrentForm(now)

    def IsActive(self, now):
        """Return whether this person may currently vote, comment, or author."""
        return self.IsParticipant(now) and not self.IsDismissed(now)


@dataclass
class VoteRecord:
    """One Vote, with its required explanatory comment.

    Revoking a Vote sets `revoked`; a dismissal sets `removed_by` (the
    Jury Topic's id). Either way the record stays in the archive.
    """

    participant_id: str
    choice: VoteChoice
    comment: str
    timestamp: dt.datetime
    revoked: bool = False
    revoked_at: Optional[dt.datetime] = None
    removed_by: Optional[str] = None

    def IsLive(self):
        return not self.revoked and self.removed_by is None


@dataclass
class DiscussionPost:
    """One contribution to a Topic's discussion, separate from voting.

    `extended` records whether it restarted the quiet clock;
    `near_identical_to` is the time of the same person's earlier post
    it nearly repeats, if any.
    """

    participant_id: str
    text: str
    timestamp: dt.datetime
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    extended: bool = False
    near_identical_to: Optional[dt.datetime] = None


@dataclass
class DismissalNote:
    """Shown on a Topic whose tally changed because someone was dismissed."""

    participant_id: str
    jury_topic_id: str
    at: dt.datetime
    removed_choice: Optional[VoteChoice] = None


@dataclass
class Topic:
    """A named subject of discussion, with an optional Poll to vote on."""

    id: str
    subsection: Subsection
    author_id: str
    title: str
    description: Optional[str]
    poll_question: Optional[str]
    created_at: dt.datetime
    rules: RuleConfig
    target_subsection: Optional[Subsection] = None
    target_participant_id: Optional[str] = None
    proposed_change: Optional[RuleChange] = None
    software_ref: Optional[str] = None
    readers: Dict[str, dt.datetime] = field(default_factory=dict)
    votes: Dict[str, List[VoteRecord]] = field(default_factory=dict)
    posts: List[DiscussionPost] = field(default_factory=list)
    restarts: Dict[str, Dict[str, int]] = field(default_factory=dict)
    dismissal_notes: List[DismissalNote] = field(default_factory=list)
    dismissal_effects: List[dict] = field(default_factory=list)
    closed: bool = False
    closed_at: Optional[dt.datetime] = None
    result: Optional[PollResult] = None
    last_activity_at: dt.datetime = field(default=None)
    pollster_id: Optional[str] = None
    question_attached_at: Optional[dt.datetime] = None
    # Members already asked "Would you like to vote?" here (asked once each).
    asked_to_vote: List[str] = field(default_factory=list)
    # A same-person check: "Is target_participant_id (joining) the same person
    # as same_as_id?" Yes / No / "I can't tell" (stored as ABSTAIN).
    same_as_id: Optional[str] = None
    # The group a Conversation belongs to: "A" is all of Meetinghouse; after a
    # split, "AA" and "AB" are its halves, and so on. Only the group's Members
    # can be its Contactors and Vote. A split Conversation ("Should Group X
    # split?") names the group in split_group.
    group: str = "A"
    split_group: Optional[str] = None
    group_flags: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.last_activity_at is None:
            self.last_activity_at = self.created_at
        if self.poll_question is not None and self.question_attached_at is None:
            self.question_attached_at = self.created_at
        if self.poll_question is not None and self.pollster_id is None:
            self.pollster_id = self.author_id

    def HasPoll(self):
        """Return whether this Topic carries a Poll question."""
        return self.poll_question is not None

    def AllowedChoices(self):
        """Forum keeps Abstain ("standing aside"); Rules and Jury are Yes or No."""
        if self.subsection is Subsection.FORUM or self.same_as_id:
            return [VoteChoice.YES, VoteChoice.ABSTAIN, VoteChoice.NO]
        return [VoteChoice.YES, VoteChoice.NO]

    def CurrentVote(self, participant_id):
        """Return the given Participant's live Vote (not revoked, not removed), if any."""
        for record in reversed(self.votes.get(participant_id, [])):
            if record.IsLive():
                return record
            if record.removed_by is not None:
                return None
        return None

    def RawVotes(self):
        """Return every Participant's live Vote, ignoring dismissal status."""
        results = []
        for participant_id in self.votes:
            record = self.CurrentVote(participant_id)
            if record is not None:
                results.append(record)
        return results

    def RestartsUsed(self, participant_id, kind):
        """How many of this person's votes ('votes') or posts ('posts') restarted the clock."""
        return self.restarts.get(participant_id, {}).get(kind, 0)

    def RestartsLeft(self, participant_id, kind):
        return max(0, RESTART_LIMIT - self.RestartsUsed(participant_id, kind))


def _Normalize(text):
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def NearlyIdentical(a, b):
    """Whether two posts say essentially the same thing in essentially the same words."""
    na, nb = _Normalize(a), _Normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= NEAR_IDENTICAL_RATIO


class Meetinghouse:
    """The engine coordinating Participants, Topics, Polls, and Rules."""

    def __init__(self, rules=None):
        self.participants: Dict[str, Participant] = {}
        self.topics: Dict[str, Topic] = {}
        self.current_rules: Dict[Subsection, RuleConfig] = rules or DefaultRules()
        self.event_log: List[dict] = []
        # Which group each Member is in (their own smallest group); "A" if not listed.
        self.member_groups: Dict[str, str] = {}

    # --- Groups -------------------------------------------------------------------

    def GroupOf(self, participant_id):
        return self.member_groups.get(participant_id, "A")

    def InGroup(self, participant_id, group):
        """A Member belongs to their own group and every group above it."""
        return self.GroupOf(participant_id).startswith(group or "A")

    def GroupsOf(self, participant_id):
        """Every group this Member belongs to, the whole Meetinghouse first."""
        own = self.GroupOf(participant_id)
        return [own[:n] for n in range(1, len(own) + 1)]

    def _RequireInGroup(self, topic, participant_id):
        if not self.InGroup(participant_id, topic.group):
            raise NotInGroup(f"This Conversation is for Group {topic.group}; only its Members can take part.")

    def OpenSplitTopicFor(self, group):
        for topic in self.topics.values():
            if not topic.closed and topic.split_group == group:
                return topic
        return None

    def RegisterParticipant(self, name):
        """Create and track a new Participant, with no signed form yet."""
        participant = Participant(name=name)
        self.participants[participant.id] = participant
        return participant

    def Leave(self, participant_id, now):
        """A Member leaves Meetinghouse, for good.

        Their Comments and Votes come off every open Conversation, with no
        note naming them, and Seasoning Time starts over in each one where
        they had a Comment or a Vote, so everyone can see the change and
        respond. That holds even if they had used up their 5.

        A Conversation they started, or attached the Question to, goes on:
        its Title, opening words and Question stay, under their name.
        Closed Conversations are untouched. A Jury Conversation about them
        goes on to its result.

        Returns the open Conversations that changed.
        """
        participant = self.participants.get(participant_id)
        if participant is None or participant.HasLeft():
            return []
        participant.left_at = now
        changed = []
        for topic in self.topics.values():
            if topic.closed:
                continue
            had_vote = participant_id in topic.votes
            posts = [post for post in topic.posts if post.participant_id == participant_id]
            topic.votes.pop(participant_id, None)
            topic.posts = [post for post in topic.posts if post.participant_id != participant_id]
            topic.readers.pop(participant_id, None)
            topic.restarts.pop(participant_id, None)
            if had_vote or posts:
                topic.last_activity_at = now
                changed.append(topic)
        self._Log("member_left", now, participant_id=participant_id, changed_topic_ids=[t.id for t in changed])
        return changed

    def _RequireActive(self, participant_id, now):
        """Return the named Participant, raising if they may not currently act."""
        participant = self.participants.get(participant_id)
        if participant is None or not participant.IsParticipant(now):
            raise NotAParticipant(f"{participant_id} is not a current Participant")
        if participant.IsDismissed(now):
            dismissal = participant.CurrentDismissal(now)
            raise ParticipantDismissed(
                f"{participant.name} is dismissed until {dismissal.end.isoformat()}"
            )
        return participant

    # --- Rules changes ----------------------------------------------------------

    def CheckRuleChange(self, change):
        """Raise InvalidRuleChange unless `change` may be proposed right now."""
        if change.setting not in SETTING_NAMES:
            raise InvalidRuleChange(
                "Only the Threshold and the Approval can be changed; Seasoning Time and Minimum Life are fixed."
            )
        low, high = SETTING_LIMITS[(change.subsection, change.setting)]
        current = getattr(self.current_rules[change.subsection], change.setting)
        name = change.Describe()
        if not low - _EPSILON <= change.value <= high + _EPSILON:
            raise InvalidRuleChange(f"{name} must stay between {Percent(low)} and {Percent(high)}.")
        if abs(change.value * 100 - round(change.value * 100)) > 1e-6:
            raise InvalidRuleChange(f"{name} is changed in whole percentages, such as 63% or 64%.")
        if abs(change.value - current) < _EPSILON:
            raise InvalidRuleChange(f"{name} is already {Percent(current)}.")
        lo, hi = AllowedRange(current, low, high)
        if not lo - 1e-6 <= change.value * 100 <= hi + 1e-6:
            raise InvalidRuleChange(
                f"{name} can change by at most 5% of its value at a time: it is {Percent(current)} now, "
                f"so this Conversation can propose {lo}% to {hi}%."
            )

    def OpenRulesTopicFor(self, subsection, setting):
        for topic in self.topics.values():
            change = topic.proposed_change
            if (
                not topic.closed
                and topic.subsection is Subsection.RULES
                and change is not None
                and change.subsection is subsection
                and change.setting == setting
            ):
                return topic
        return None

    def OpenJuryTopicFor(self, participant_id):
        for topic in self.topics.values():
            if not topic.closed and topic.subsection is Subsection.JURY and topic.target_participant_id == participant_id:
                return topic
        return None

    # --- Topics, reading, voting, discussion ------------------------------------

    def CreateTopic(
        self,
        author_id,
        subsection,
        title,
        now,
        description=None,
        poll_question=None,
        target_subsection=None,
        target_participant_id=None,
        proposed_change=None,
        software_ref=None,
        group="A",
        split_group=None,
    ):
        """Author a new Topic, enforcing each subsection's required fields."""
        author = self._RequireActive(author_id, now)
        group = group or "A"
        if not self.InGroup(author.id, group):
            raise NotInGroup(f"You can start a Conversation only in your own groups, not Group {group}.")

        if subsection is Subsection.RULES and split_group:
            # "Should Group X split?" Each group decides its own split.
            if not self.InGroup(author.id, split_group):
                raise NotInGroup(f"Only Members of Group {split_group} can propose splitting it.")
            existing = self.OpenSplitTopicFor(split_group)
            if existing is not None:
                raise DuplicateTopic(
                    f"There's already an open Conversation about splitting Group {split_group} — join that one instead.",
                    existing.id,
                )
            group = split_group
            title = f"Should Group {split_group} split?"
            poll_question = f"Do we split Group {split_group} into two groups?"
            proposed_change = software_ref = None
        elif subsection is Subsection.RULES:
            group = "A"  # the Rules are global, decided by all of Meetinghouse
            if (proposed_change is None) == (not software_ref):
                raise InvalidTopic(
                    "A Rules Conversation either changes one setting or approves a software change."
                )
            if proposed_change is not None:
                existing = self.OpenRulesTopicFor(proposed_change.subsection, proposed_change.setting)
                if existing is not None:
                    raise DuplicateTopic(
                        f"There's already an open Conversation about {proposed_change.Describe()} "
                        "— join that one instead.",
                        existing.id,
                    )
                self.CheckRuleChange(proposed_change)
                target_subsection = proposed_change.subsection
                current = getattr(self.current_rules[proposed_change.subsection], proposed_change.setting)
                title = title or (
                    f"Change {proposed_change.Describe()} from {Percent(current)} to {Percent(proposed_change.value)}"
                )
            else:
                target_subsection = None
            poll_question = poll_question or "Adopt this change?"
        if subsection is Subsection.JURY:
            if target_participant_id is None or target_participant_id not in self.participants:
                raise InvalidTopic("A Jury Conversation must name an existing Member.")
            existing = self.OpenJuryTopicFor(target_participant_id)
            if existing is not None:
                raise DuplicateTopic(
                    f"There's already an open Jury Conversation about {self.participants[target_participant_id].name} "
                    "— join that one instead.",
                    existing.id,
                )
            poll_question = "Do we dismiss them?"
            title = self.participants[target_participant_id].name
        if poll_question is not None and not poll_question.strip():
            poll_question = None
        if not title or not title.strip():
            raise InvalidTopic("Every Conversation needs a Title.")

        topic = Topic(
            id=str(uuid.uuid4()),
            subsection=subsection,
            author_id=author.id,
            title=title,
            description=description,
            poll_question=poll_question,
            created_at=now,
            rules=self.current_rules[subsection],
            target_subsection=target_subsection,
            target_participant_id=target_participant_id,
            proposed_change=proposed_change,
            software_ref=software_ref or None,
            pollster_id=author.id if poll_question else None,
            question_attached_at=now if poll_question else None,
            group=group,
            split_group=split_group if subsection is Subsection.RULES else None,
        )
        self.topics[topic.id] = topic
        self._Log("topic_created", now, topic_id=topic.id, author_id=author.id, group=group)
        self.MarkRead(topic.id, author.id, now)
        return topic

    def OpenSamePersonCheck(self, newcomer_id):
        for topic in self.topics.values():
            if not topic.closed and topic.same_as_id and topic.target_participant_id == newcomer_id:
                return topic
        return None

    def CreateSamePersonCheck(self, newcomer_id, member_id, now):
        """The site's own Jury Conversation when a newcomer's face still matches
        someone's after a new selfie: "Are these the same person?"."""
        existing = self.OpenSamePersonCheck(newcomer_id)
        if existing is not None:
            return existing
        newcomer = self.participants[newcomer_id]
        member = self.participants[member_id]
        question = f"Are {newcomer.name} (joining) and {member.name} the same person?"
        topic = Topic(
            id=str(uuid.uuid4()),
            subsection=Subsection.JURY,
            author_id=MEETINGHOUSE_ID,
            title=f"Same person? {newcomer.name} and {member.name}",
            description=(
                f"{newcomer.name} passed their ID check, but their face closely matches {member.name}'s, "
                "even after a new selfie. Software can't tell twins or look-alikes from one person joining twice, "
                "so the Meeting decides. Yes: they are the same person. No: they are different people. "
                "If you can't tell, say so: “I can't tell” counts toward the Threshold, not toward Approval."
            ),
            poll_question=question,
            created_at=now,
            rules=self.current_rules[Subsection.JURY],
            target_participant_id=newcomer_id,
            same_as_id=member_id,
            pollster_id=MEETINGHOUSE_ID,
            question_attached_at=now,
        )
        self.topics[topic.id] = topic
        self._Log("topic_created", now, topic_id=topic.id, author_id=MEETINGHOUSE_ID, same_person=True)
        return topic

    def FlagWrongGroup(self, topic_id, participant_id, now):
        """A Contactor says this Conversation is in the wrong group (first week only)."""
        topic = self._GetOpenTopic(topic_id)
        if now - topic.created_at > dt.timedelta(weeks=1):
            raise InvalidTopic("A Conversation can be flagged as being in the wrong group only during its first week.")
        if participant_id not in topic.readers:
            raise InvalidTopic("Only its Contactors can flag a Conversation.")
        if participant_id not in topic.group_flags:
            topic.group_flags.append(participant_id)
            self._Log("group_flagged", now, topic_id=topic_id, participant_id=participant_id)

    def MoveToGroup(self, topic_id, participant_id, group, now):
        """The Initiator moves a flagged Forum or Jury Conversation to another of their own groups (first week only)."""
        topic = self._GetOpenTopic(topic_id)
        if participant_id != topic.author_id:
            raise InvalidTopic("Only the Initiator can move a Conversation to another group.")
        if topic.subsection is Subsection.RULES:
            raise InvalidTopic("Rules Conversations belong to all of Meetinghouse.")
        if now - topic.created_at > dt.timedelta(weeks=1):
            raise InvalidTopic("A Conversation can be moved only during its first week.")
        if not self.InGroup(participant_id, group):
            raise NotInGroup(f"You can move it only to one of your own groups, not Group {group}.")
        old = topic.group
        topic.group = group
        topic.group_flags = []
        topic.last_activity_at = now  # everyone in the new group gets a full Seasoning Time
        self._Log("topic_moved", now, topic_id=topic_id, from_group=old, to_group=group)

    def MarkRead(self, topic_id, participant_id, now):
        """Record that the given Participant has read this Topic.

        Returns True the first time they touch it.
        """
        topic = self._GetOpenTopic(topic_id)
        self._RequireActive(participant_id, now)
        if participant_id in topic.readers:
            return False
        if topic.same_as_id and participant_id in (topic.target_participant_id, topic.same_as_id):
            return False  # the two people it's about aren't its Contactors
        if not self.InGroup(participant_id, topic.group):
            return False  # only the group's Members are its Contactors
        topic.readers[participant_id] = now
        return True

    def AttachQuestion(self, topic_id, participant_id, question, now):
        """Attach the Question to a Conversation that has none; the asker becomes its Pollster.

        Only Forum Conversations start without a Question. Attaching one
        starts its Minimum Life, so it is shown to everyone for at least
        that long before it can close.
        """
        if not question or not question.strip():
            raise InvalidTopic("Write the Question you'd like the Meeting to decide.")
        topic = self._GetOpenTopic(topic_id)
        if topic.HasPoll():
            raise InvalidTopic("This Conversation already has its Question. There can be only one.")
        self._RequireActive(participant_id, now)
        self._RequireInGroup(topic, participant_id)
        topic.readers.setdefault(participant_id, now)
        topic.poll_question = question.strip()
        topic.pollster_id = participant_id
        topic.question_attached_at = now
        topic.last_activity_at = now
        self._Log("question_attached", now, topic_id=topic_id, participant_id=participant_id)
        return topic

    def _UseRestart(self, topic, participant_id, kind):
        """Use one of this person's restarts of `kind` if any are left; say whether it was."""
        used = topic.restarts.setdefault(participant_id, {"votes": 0, "posts": 0})
        if used.get(kind, 0) >= RESTART_LIMIT:
            return False
        used[kind] = used.get(kind, 0) + 1
        return True

    def CastVote(self, topic_id, participant_id, choice, comment, now):
        """Cast, or replace, a Participant's Vote on this Topic's Poll.

        Every Vote must carry a comment explaining it. Only the person's
        first RESTART_LIMIT votes on this Topic restart its quiet clock.
        """
        if not comment or not comment.strip():
            raise InvalidTopic("Every Vote needs its reason. Please write why you're Voting this way.")
        topic = self._GetOpenTopic(topic_id)
        if not topic.HasPoll():
            raise NoPollOnTopic("This Conversation has no Question to Vote on yet.")
        if choice not in topic.AllowedChoices():
            raise InvalidTopic(f"Abstain isn't an option in the {topic.subsection.value} Committee: Vote Yes or No.")
        self._RequireActive(participant_id, now)
        if topic.same_as_id and participant_id in (topic.target_participant_id, topic.same_as_id):
            raise InvalidTopic("This Conversation is about you, so it's for the rest of the Meeting to decide.")
        self._RequireInGroup(topic, participant_id)

        topic.readers.setdefault(participant_id, now)
        record = VoteRecord(
            participant_id=participant_id, choice=choice, comment=comment, timestamp=now
        )
        topic.votes.setdefault(participant_id, []).append(record)
        if self._UseRestart(topic, participant_id, "votes"):
            topic.last_activity_at = now
        self._Log(
            "vote_cast", now, topic_id=topic_id, participant_id=participant_id, choice=choice.value
        )
        return record

    def RevokeVote(self, topic_id, participant_id, now):
        """Revoke a Participant's current Vote, returning them to Reader status.

        The revoked record stays in the archive, marked revoked, and does
        not restart the Quiet-time clock.
        """
        topic = self._GetOpenTopic(topic_id)
        self._RequireActive(participant_id, now)
        record = topic.CurrentVote(participant_id)
        if record is None:
            return
        record.revoked = True
        record.revoked_at = now
        self._Log("vote_revoked", now, topic_id=topic_id, participant_id=participant_id)

    def AddPost(self, topic_id, participant_id, text, now):
        """Add a discussion post. Unlimited, but only the first few keep the Topic open.

        A post nearly identical to one this person already made on this
        Topic is posted and uses up one of their restarts, but never
        restarts the quiet clock.
        """
        if not text or not text.strip():
            raise InvalidTopic("A Comment can't be empty.")
        topic = self._GetOpenTopic(topic_id)
        self._RequireActive(participant_id, now)
        self._RequireInGroup(topic, participant_id)
        topic.readers.setdefault(participant_id, now)

        earlier = next(
            (
                p
                for p in reversed(topic.posts)
                if p.participant_id == participant_id and NearlyIdentical(p.text, text)
            ),
            None,
        )
        used_restart = self._UseRestart(topic, participant_id, "posts")
        post = DiscussionPost(
            participant_id=participant_id,
            text=text.strip(),
            timestamp=now,
            extended=used_restart and earlier is None,
            near_identical_to=earlier.timestamp if earlier else None,
        )
        topic.posts.append(post)
        if post.extended:
            topic.last_activity_at = now
        self._Log("post_added", now, topic_id=topic_id, participant_id=participant_id, extended=post.extended)
        return post

    def _GetOpenTopic(self, topic_id):
        """Return the named Topic, raising if it does not exist or has closed."""
        topic = self.topics.get(topic_id)
        if topic is None:
            raise InvalidTopic(f"No such Conversation: {topic_id}")
        if topic.closed:
            raise TopicClosed("This Conversation has already closed.")
        return topic

    # --- Counting -----------------------------------------------------------------

    def EffectiveReaders(self, topic, now):
        """This Topic's Readers who count toward turnout right now.

        A current Participant counts. A paused one (form lapsed) counts
        only where they still have a live vote. A dismissed one doesn't.
        """
        readers = set()
        for participant_id in topic.readers:
            participant = self.participants.get(participant_id)
            if participant is None or participant.IsDismissed(now) or not self.InGroup(participant_id, topic.group):
                continue
            if participant.IsParticipant(now) or topic.CurrentVote(participant_id) is not None:
                readers.add(participant_id)
        return readers

    def EffectiveVoteCounts(self, topic, now):
        """Return live Vote counts, excluding currently-dismissed Participants."""
        counts = {choice: 0 for choice in VoteChoice}
        for participant_id in topic.votes:
            record = topic.CurrentVote(participant_id)
            if record is None:
                continue
            participant = self.participants.get(participant_id)
            if participant is None or participant.IsDismissed(now) or not self.InGroup(participant_id, topic.group):
                continue
            counts[record.choice] += 1
        return counts

    def DismissedCount(self, topic, now):
        """How many people this Topic lost to a dismissal that is still in effect."""
        people = {
            note.participant_id
            for note in topic.dismissal_notes
            if note.participant_id in self.participants and self.participants[note.participant_id].IsDismissed(now)
        }
        return len(people)

    def _EligibleForClosure(self, topic, now):
        """Return whether this Topic has satisfied its minimum life and Quiet time."""
        if topic.closed or not topic.HasPoll():
            return False
        if now - (topic.question_attached_at or topic.created_at) < topic.rules.min_life:
            return False
        return now - topic.last_activity_at >= topic.rules.quiet_time

    def Evaluate(self, topic, now):
        """Compute PASSED or FAILED for this Topic's Poll, per its Rule snapshot."""
        readers = self.EffectiveReaders(topic, now)
        counts = self.EffectiveVoteCounts(topic, now)
        voters = sum(counts.values())

        if not readers:
            return PollResult.FAILED
        if voters / len(readers) < topic.rules.voter_fraction:
            return PollResult.FAILED

        required = topic.rules.approval_fraction
        if topic.same_as_id:
            # "I can't tell" counts toward the Threshold, not toward Approval;
            # if most Voters can't tell, the newcomer gets the benefit of the doubt.
            if counts[VoteChoice.ABSTAIN] * 2 > voters:
                return PollResult.FAILED
            favorable = counts[VoteChoice.YES]
            required = SAME_PERSON_APPROVAL
        else:
            favorable = counts[VoteChoice.YES] + counts[VoteChoice.ABSTAIN]
        total_decided = favorable + counts[VoteChoice.NO]
        if total_decided == 0:
            return PollResult.FAILED

        approval = favorable / total_decided
        if approval >= required - _EPSILON:
            return PollResult.PASSED
        return PollResult.FAILED

    # --- Closing ------------------------------------------------------------------

    def Tick(self, now):
        """Close and evaluate every Topic whose Quiet time has elapsed.

        Jury Topics go first, so a dismissal's restart of other Topics'
        quiet clocks takes effect before those Topics are considered.
        Returns the list of Topics closed by this call.
        """
        newly_closed = []
        ordered = sorted(self.topics.values(), key=lambda t: t.subsection is not Subsection.JURY)
        for topic in ordered:
            if self._EligibleForClosure(topic, now):
                self._CloseTopic(topic, now)
                newly_closed.append(topic)
        return newly_closed

    def _CloseTopic(self, topic, now):
        """Evaluate, close, and apply the side effects of a decided Topic."""
        topic.result = self.Evaluate(topic, now)
        topic.closed = True
        topic.closed_at = now
        self._Log("topic_closed", now, topic_id=topic.id, result=topic.result.value)

        if topic.result is not PollResult.PASSED:
            return
        if topic.subsection is Subsection.RULES:
            self._ApplyRuleChange(topic, now)
        if topic.subsection is Subsection.JURY and not topic.same_as_id:
            self._ApplyDismissal(topic, now)

    def _ApplyRuleChange(self, topic, now):
        """Install a passed Rules Topic's change, re-checking it against today's rules."""
        if topic.software_ref:
            self._Log("software_change_approved", now, topic_id=topic.id, ref=topic.software_ref)
            return
        change = topic.proposed_change
        if change is None:
            return
        try:
            self.CheckRuleChange(change)
        except InvalidRuleChange as exc:
            self._Log("rule_change_rejected", now, topic_id=topic.id, reason=str(exc))
            return
        current = self.current_rules[change.subsection]
        self.current_rules[change.subsection] = dataclasses.replace(current, **{change.setting: change.value})
        self._Log(
            "rule_changed",
            now,
            topic_id=topic.id,
            target_subsection=change.subsection.value,
            setting=change.setting,
            old=getattr(current, change.setting),
            new=change.value,
        )

    def _ApplyDismissal(self, topic, now):
        """Dismiss a passed Jury Topic's target, and take them off every open Topic."""
        participant = self.participants[topic.target_participant_id]
        ordinal = len(participant.dismissals) + 1
        duration = participant.NextDismissalDuration()
        dismissal = Dismissal(start=now, end=now + duration, ordinal=ordinal, topic_id=topic.id)
        participant.dismissals.append(dismissal)

        affected = []
        for other in self.topics.values():
            if other.closed or other.id == topic.id:
                continue
            pid = participant.id
            if pid not in other.readers and other.CurrentVote(pid) is None:
                continue
            live = other.CurrentVote(pid)
            for record in other.votes.get(pid, []):
                if record.IsLive():
                    record.removed_by = topic.id
            other.readers.pop(pid, None)
            other.restarts.pop(pid, None)
            other.dismissal_notes.append(
                DismissalNote(
                    participant_id=pid,
                    jury_topic_id=topic.id,
                    at=now,
                    removed_choice=live.choice if live else None,
                )
            )
            other.last_activity_at = now
            affected.append(other)

        topic.dismissal_effects = []
        for other in affected:
            counts = self.EffectiveVoteCounts(other, now)
            topic.dismissal_effects.append(
                {
                    "topic_id": other.id,
                    "title": other.title,
                    "subsection": other.subsection.value,
                    "votes": {choice.value: count for choice, count in counts.items()},
                    "dismissed": self.DismissedCount(other, now),
                }
            )
        self._Log(
            "participant_dismissed",
            now,
            topic_id=topic.id,
            participant_id=participant.id,
            ordinal=ordinal,
            duration_days=duration.days,
            affected_topic_ids=[t.id for t in affected],
        )

    # --- Reading the record ---------------------------------------------------------

    def SearchArchive(self, query):
        """Return every Topic whose title, description, Poll, comments or posts match."""
        needle = query.lower()
        hits = []
        for topic in self.topics.values():
            comments = (record.comment for history in topic.votes.values() for record in history)
            posts = (post.text for post in topic.posts)
            haystack = " ".join(
                filter(None, [topic.title, topic.description or "", topic.poll_question or "", *comments, *posts])
            ).lower()
            if needle in haystack:
                hits.append(topic)
        return hits

    def DailySummary(self, participant_id, now):
        """Build one Participant's Daily Summary: open Topics, closings and dismissals today."""
        self._RequireActive(participant_id, now)
        today = now.date()

        open_items = []
        for topic in self.topics.values():
            if topic.closed:
                continue
            counts = self.EffectiveVoteCounts(topic, now)
            open_items.append(
                {
                    "topic_id": topic.id,
                    "subsection": topic.subsection.value,
                    "title": topic.title,
                    "age": now - topic.created_at,
                    "has_question": topic.HasPoll(),
                    "votes": {choice.value: count for choice, count in counts.items()},
                    "dismissed": self.DismissedCount(topic, now),
                }
            )

        closed_today = []
        dismissals_today = []
        for topic in self.topics.values():
            if topic.closed and topic.closed_at.date() == today:
                closed_today.append(
                    {
                        "topic_id": topic.id,
                        "subsection": topic.subsection.value,
                        "title": topic.title,
                        "result": topic.result.value,
                    }
                )
                if topic.subsection is Subsection.JURY and topic.result is PollResult.PASSED:
                    dismissals_today.append(
                        {
                            "topic_id": topic.id,
                            "participant_id": topic.target_participant_id,
                            "effects": topic.dismissal_effects,
                        }
                    )

        return {
            "date": today.isoformat(),
            "open": open_items,
            "closed_today": closed_today,
            "dismissals_today": dismissals_today,
        }

    def _Log(self, kind, now, **fields):
        """Append one entry to the in-memory event log, for archival/audit use."""
        self.event_log.append({"kind": kind, "at": now, **fields})
