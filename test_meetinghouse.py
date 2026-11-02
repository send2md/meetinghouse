"""Tests for meetinghouse.py, one per Rule in the design spec.

Run with `python -m pytest test_meetinghouse.py -v`, or run this file
directly and it will execute every test itself, with no dependency on
pytest being installed.
"""

import dataclasses
import datetime as dt

from meetinghouse import (
    AllowedRange,
    NoPollOnTopic,
    Dismissal,
    DuplicateTopic,
    InvalidRuleChange,
    InvalidTopic,
    Meetinghouse,
    NotAParticipant,
    ParticipantDismissed,
    PollResult,
    RuleChange,
    Subsection,
    VoteChoice,
)

START = dt.datetime(2026, 1, 1, 12, 0, 0)


def Days(count):
    """Build a timedelta of the given number of days, for readability."""
    return dt.timedelta(days=count)


def CreateHouseWithParticipants(count=8, now=START):
    """Build an Meetinghouse with `count` Participants, each freshly signed."""
    house = Meetinghouse()
    people = [house.RegisterParticipant(f"Participant {i}") for i in range(count)]
    for person in people:
        person.SignForm(signed_date=now.date() - Days(1))
    return house, people


def test_form_expires_after_a_year():
    house = Meetinghouse()
    alice = house.RegisterParticipant("Alice")
    alice.SignForm(signed_date=START.date())
    assert alice.IsParticipant(START + Days(300))
    assert not alice.IsParticipant(START + Days(366))


def test_cannot_act_without_current_form():
    house = Meetinghouse()
    alice = house.RegisterParticipant("Alice")
    try:
        house.CreateTopic(alice.id, Subsection.FORUM, "Buy new chairs", START)
        assert False, "should have raised"
    except NotAParticipant:
        pass


def test_forum_topic_cannot_close_before_three_weeks():
    house, people = CreateHouseWithParticipants(4)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Repaint the hall", START,
        poll_question="Shall we repaint the hall?",
    )
    for person in people:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Sounds good.", START)

    closed = house.Tick(START + Days(14))
    assert topic not in closed
    assert not topic.closed


def test_forum_topic_closes_and_passes_after_quiet_time():
    house, people = CreateHouseWithParticipants(4)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Repaint the hall", START,
        poll_question="Shall we repaint the hall?",
    )
    for person in people:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Sounds good.", START)

    now = START + Days(21) + Days(7) + dt.timedelta(hours=1)
    closed = house.Tick(now)
    assert topic in closed
    assert topic.closed
    assert topic.result is PollResult.PASSED


def test_forum_fails_on_low_turnout():
    house, people = CreateHouseWithParticipants(8)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "New logo", START, poll_question="Adopt the new logo?"
    )
    for person in people[1:]:
        house.MarkRead(topic.id, person.id, START)
    house.CastVote(topic.id, author.id, VoteChoice.YES, "Looks nice.", START)

    now = START + Days(21) + Days(7) + dt.timedelta(hours=1)
    house.Tick(now)
    assert topic.result is PollResult.FAILED


def test_forum_fails_on_too_much_opposition():
    house, people = CreateHouseWithParticipants(6)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Change the logo", START, poll_question="Adopt the new logo?"
    )
    for person in people[:3]:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "I like it.", START)
    for person in people[3:]:
        house.CastVote(topic.id, person.id, VoteChoice.NO, "I do not like it.", START)

    now = START + Days(21) + Days(7) + dt.timedelta(hours=1)
    house.Tick(now)
    assert topic.result is PollResult.FAILED


def test_activity_restarts_quiet_clock():
    house, people = CreateHouseWithParticipants(3)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Buy a rug", START,
        poll_question="Buy a rug for the entryway?",
    )

    vote_time = START + Days(20)
    for person in people:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Fine by me.", vote_time)

    almost_quiet = vote_time + Days(6) + dt.timedelta(hours=23)
    house.Tick(almost_quiet)
    assert not topic.closed

    house.CastVote(
        topic.id, people[0].id, VoteChoice.YES, "Still in favor, adding a note.", almost_quiet
    )

    would_have_closed = almost_quiet + dt.timedelta(hours=2)
    house.Tick(would_have_closed)
    assert not topic.closed

    house.Tick(almost_quiet + Days(7) + dt.timedelta(hours=1))
    assert topic.closed


def test_rules_topic_requires_target_subsection_and_poll():
    house, people = CreateHouseWithParticipants(4)
    try:
        house.CreateTopic(people[0].id, Subsection.RULES, "Loosen Forum quiet time", START)
        assert False, "should require a poll and target subsection"
    except InvalidTopic:
        pass


def test_passed_rule_change_only_affects_future_topics():
    house, people = CreateHouseWithParticipants(4)
    author = people[0]

    rules_topic = house.CreateTopic(
        author.id, Subsection.RULES, None, START,
        proposed_change=RuleChange(Subsection.FORUM, "approval_fraction", 0.70),
    )
    assert rules_topic.title == "Change Forum Approval from 66.7% to 70%"
    old_forum_topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Old regime topic", START, poll_question="Approve?"
    )

    for person in people:
        house.CastVote(rules_topic.id, person.id, VoteChoice.YES, "Agreed.", START)

    close_time = START + Days(21) + Days(14) + dt.timedelta(hours=1)
    house.Tick(close_time)
    assert rules_topic.closed and rules_topic.result is PollResult.PASSED
    assert house.current_rules[Subsection.FORUM].approval_fraction == 0.70

    new_forum_topic = house.CreateTopic(
        author.id, Subsection.FORUM, "New regime topic", close_time, poll_question="Approve?"
    )
    assert new_forum_topic.rules.approval_fraction == 0.70
    assert abs(old_forum_topic.rules.approval_fraction - 2 / 3) < 1e-9


def test_jury_topic_dismisses_participant_and_removes_their_votes():
    house, people = CreateHouseWithParticipants(6)
    author = people[0]
    target = people[1]

    jury_topic = house.CreateTopic(
        author.id, Subsection.JURY, target.name, START, target_participant_id=target.id
    )
    assert jury_topic.poll_question == "Do we dismiss them?"

    forum_topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Buy new chairs", START, poll_question="Approve?"
    )
    house.CastVote(forum_topic.id, target.id, VoteChoice.NO, "I disagree.", START)

    for person in people[:3]:
        house.CastVote(jury_topic.id, person.id, VoteChoice.YES, "Time for a break.", START)

    close_time = START + Days(21) + Days(14) + dt.timedelta(hours=1)
    house.Tick(close_time)

    assert jury_topic.closed and jury_topic.result is PollResult.PASSED
    assert not forum_topic.closed
    assert target.IsDismissed(close_time)
    assert len(target.dismissals) == 1
    assert target.dismissals[0].end - target.dismissals[0].start == dt.timedelta(weeks=2)

    counts = house.EffectiveVoteCounts(forum_topic, close_time)
    assert counts[VoteChoice.NO] == 0
    assert forum_topic.CurrentVote(target.id) is None
    assert target.id not in forum_topic.readers
    assert forum_topic.last_activity_at == close_time  # quiet clock restarted
    (note,) = forum_topic.dismissal_notes
    assert note.participant_id == target.id and note.jury_topic_id == jury_topic.id
    assert note.removed_choice is VoteChoice.NO
    assert house.DismissedCount(forum_topic, close_time) == 1
    assert jury_topic.dismissal_effects[0]["topic_id"] == forum_topic.id

    try:
        house.CastVote(forum_topic.id, target.id, VoteChoice.YES, "Trying anyway.", close_time)
        assert False, "should have raised"
    except ParticipantDismissed:
        pass
    try:
        house.CreateTopic(target.id, Subsection.FORUM, "Sneaky new topic", close_time)
        assert False, "should have raised"
    except ParticipantDismissed:
        pass

    # They come back quietly: Dismissed count drops, old vote stays gone,
    # quiet clock is not restarted, and they can vote again.
    back = target.dismissals[0].end + dt.timedelta(minutes=1)
    assert house.DismissedCount(forum_topic, back) == 0
    assert forum_topic.CurrentVote(target.id) is None
    assert forum_topic.last_activity_at == close_time
    house.CastVote(forum_topic.id, target.id, VoteChoice.YES, "Back, and changed my mind.", back)
    assert house.EffectiveVoteCounts(forum_topic, back)[VoteChoice.YES] == 1


def test_second_dismissal_doubles_duration():
    house, people = CreateHouseWithParticipants(4)
    target = people[1]
    target.dismissals.append(
        Dismissal(start=START, end=START + dt.timedelta(weeks=2), ordinal=1, topic_id="fake")
    )
    assert target.NextDismissalDuration() == dt.timedelta(weeks=4)


def test_resuming_after_dismissal_requires_new_form():
    house, people = CreateHouseWithParticipants(4)
    target = people[1]
    dismissal_end = START + dt.timedelta(weeks=2)
    target.dismissals.append(
        Dismissal(start=START, end=dismissal_end, ordinal=1, topic_id="x")
    )

    just_after = dismissal_end + dt.timedelta(minutes=1)
    assert not target.IsDismissed(just_after)
    assert target.IsActive(just_after)


def test_search_archive_finds_topics_by_comment_text():
    house, people = CreateHouseWithParticipants(3)
    topic = house.CreateTopic(
        people[0].id, Subsection.FORUM, "Kitchen renovation", START,
        poll_question="Approve the renovation?",
    )
    house.CastVote(
        topic.id, people[1].id, VoteChoice.YES,
        "The dishwasher has been broken for months.", START,
    )
    hits = house.SearchArchive("dishwasher")
    assert topic in hits
    assert house.SearchArchive("nonexistent-term") == []


def test_daily_summary_lists_open_and_recently_closed():
    house, people = CreateHouseWithParticipants(3)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Buy a kettle", START, poll_question="Approve?"
    )
    for person in people:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Sure.", START)

    close_time = START + Days(21) + Days(7) + dt.timedelta(hours=1)
    house.Tick(close_time)

    summary = house.DailySummary(author.id, close_time)
    assert summary["closed_today"][0]["topic_id"] == topic.id
    assert summary["open"] == []


def _Close(house, topic, extra_days=0):
    """Tick far enough past a Topic's quiet time to close it."""
    when = topic.last_activity_at + topic.rules.quiet_time + Days(extra_days) + dt.timedelta(hours=1)
    when = max(when, topic.created_at + topic.rules.min_life + dt.timedelta(hours=1))
    house.Tick(when)
    return when


def test_abstain_only_in_forum():
    house, people = CreateHouseWithParticipants(4)
    rules_topic = house.CreateTopic(
        people[0].id, Subsection.RULES, None, START,
        proposed_change=RuleChange(Subsection.FORUM, "voter_fraction", 0.26),
    )
    jury_topic = house.CreateTopic(
        people[0].id, Subsection.JURY, "", START, target_participant_id=people[3].id
    )
    forum_topic = house.CreateTopic(people[0].id, Subsection.FORUM, "Chairs", START, poll_question="Buy?")
    for topic in (rules_topic, jury_topic):
        try:
            house.CastVote(topic.id, people[1].id, VoteChoice.ABSTAIN, "Unsure.", START)
            assert False, "should have raised"
        except InvalidTopic:
            pass
    house.CastVote(forum_topic.id, people[1].id, VoteChoice.ABSTAIN, "Standing aside.", START)


def test_only_one_open_jury_topic_per_person():
    house, people = CreateHouseWithParticipants(4)
    first = house.CreateTopic(people[0].id, Subsection.JURY, "", START, target_participant_id=people[3].id)
    try:
        house.CreateTopic(people[1].id, Subsection.JURY, "", START, target_participant_id=people[3].id)
        assert False, "should have raised"
    except DuplicateTopic as exc:
        assert exc.existing_topic_id == first.id
        assert "already an open Jury Conversation about Participant 3" in str(exc)
    house.CreateTopic(people[1].id, Subsection.JURY, "", START, target_participant_id=people[2].id)


def test_rule_changes_are_one_small_step_within_limits():
    house, people = CreateHouseWithParticipants(4)
    pid = people[0].id

    def Propose(subsection, setting, value):
        return house.CreateTopic(pid, Subsection.RULES, None, START, proposed_change=RuleChange(subsection, setting, value))

    house.current_rules[Subsection.RULES] = dataclasses.replace(house.current_rules[Subsection.RULES], voter_fraction=0.09)
    for subsection, setting, value, words in [
        (Subsection.FORUM, "approval_fraction", 0.62, "can propose 63% to 70%"),  # 66.7%: 5% of it is 3.3 points
        (Subsection.FORUM, "approval_fraction", 0.71, "can propose 63% to 70%"),
        (Subsection.FORUM, "voter_fraction", 0.23, "can propose 24% to 26%"),
        (Subsection.FORUM, "approval_fraction", 0.655, "whole percentages"),
        (Subsection.RULES, "voter_fraction", 0.08, "between 9% and 100%"),
        (Subsection.RULES, "approval_fraction", 0.51, "already 51%"),
        (Subsection.FORUM, "quiet_time", 0.5, "fixed"),
    ]:
        try:
            Propose(subsection, setting, value)
            assert False, f"should have refused {subsection} {setting} {value}"
        except InvalidRuleChange as exc:
            assert words in str(exc), str(exc)

    Propose(Subsection.FORUM, "approval_fraction", 0.63)
    Propose(Subsection.RULES, "voter_fraction", 0.10)  # at 9%, a whole point is allowed even though 5% is less
    first = Propose(Subsection.JURY, "approval_fraction", 0.21)
    try:
        Propose(Subsection.JURY, "approval_fraction", 0.19)
        assert False, "should have raised"
    except DuplicateTopic as exc:
        assert exc.existing_topic_id == first.id
        assert "already an open Conversation about Jury Approval" in str(exc)
    Propose(Subsection.JURY, "voter_fraction", 0.52)  # a different setting is fine


def test_software_change_topic():
    house, people = CreateHouseWithParticipants(4)
    topic = house.CreateTopic(
        people[0].id, Subsection.RULES, "Add a map", START, software_ref="a" * 40
    )
    for person in people:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Yes.", START)
    _Close(house, topic)
    assert topic.result is PollResult.PASSED
    assert any(e["kind"] == "software_change_approved" and e["ref"] == "a" * 40 for e in house.event_log)
    try:
        house.CreateTopic(people[0].id, Subsection.RULES, "Both", START, software_ref="b" * 40,
                          proposed_change=RuleChange(Subsection.FORUM, "voter_fraction", 0.3))
        assert False, "should have raised"
    except InvalidTopic:
        pass


def test_only_first_five_votes_restart_the_clock():
    house, people = CreateHouseWithParticipants(2)
    topic = house.CreateTopic(people[0].id, Subsection.FORUM, "Paint", START, poll_question="Paint?")
    when = START
    for i in range(5):
        when = START + Days(i)
        house.CastVote(topic.id, people[1].id, VoteChoice.YES, f"Vote {i}", when)
        assert topic.last_activity_at == when
    assert topic.RestartsLeft(people[1].id, "votes") == 0
    later = when + Days(1)
    house.CastVote(topic.id, people[1].id, VoteChoice.NO, "Changed my mind.", later)
    assert topic.last_activity_at == when  # the 6th vote counts but doesn't restart
    assert topic.CurrentVote(people[1].id).choice is VoteChoice.NO


def test_discussion_is_separate_and_limited_in_restarts():
    house, people = CreateHouseWithParticipants(2)
    topic = house.CreateTopic(people[0].id, Subsection.FORUM, "Paint", START, poll_question="Paint?")
    when = START
    thoughts = [
        "Blue would calm the room.",
        "The budget allows two coats of paint.",
        "We could ask the youth group to help on a Saturday.",
        "Let's check whether the old paint contains lead first.",
        "I can bring ladders and drop cloths.",
    ]
    for i, thought in enumerate(thoughts):
        when = START + Days(i)
        post = house.AddPost(topic.id, people[1].id, thought, when)
        assert post.extended and topic.last_activity_at == when
    later = when + Days(1)
    post = house.AddPost(topic.id, people[1].id, "A sixth thought, still welcome.", later)
    assert not post.extended and topic.last_activity_at == when
    assert len(topic.posts) == 6
    assert topic.RestartsLeft(people[1].id, "votes") == 5  # posts don't use up votes


def test_near_identical_post_uses_a_restart_but_does_not_extend():
    house, people = CreateHouseWithParticipants(3)
    topic = house.CreateTopic(people[0].id, Subsection.FORUM, "Paint", START, poll_question="Paint?")
    first = house.AddPost(topic.id, people[1].id, "We should paint the hall blue, it is calming.", START)
    again = house.AddPost(topic.id, people[1].id, "We should paint the hall blue - it is calming!", START + Days(2))
    assert again.near_identical_to == first.timestamp
    assert not again.extended
    assert topic.last_activity_at == START
    assert topic.RestartsUsed(people[1].id, "posts") == 2
    other = house.AddPost(topic.id, people[2].id, "We should paint the hall blue, it is calming.", START + Days(3))
    assert other.extended and other.near_identical_to is None  # someone else may agree in the same words


def test_mark_read_reports_first_visit_only():
    house, people = CreateHouseWithParticipants(2)
    topic = house.CreateTopic(people[0].id, Subsection.FORUM, "Paint", START, poll_question="Paint?")
    assert house.MarkRead(topic.id, people[1].id, START) is True
    assert house.MarkRead(topic.id, people[1].id, START + Days(1)) is False


def test_paused_member_counts_only_where_they_voted():
    house, people = CreateHouseWithParticipants(3)
    topic = house.CreateTopic(people[0].id, Subsection.FORUM, "Paint", START, poll_question="Paint?")
    other = house.CreateTopic(people[0].id, Subsection.FORUM, "Rug", START, poll_question="Rug?")
    paused = people[2]
    house.CastVote(topic.id, paused.id, VoteChoice.YES, "Yes.", START)
    house.MarkRead(other.id, paused.id, START)
    lapsed = START + Days(400)
    assert not paused.IsParticipant(lapsed)
    assert paused.id in house.EffectiveReaders(topic, lapsed)
    assert house.EffectiveVoteCounts(topic, lapsed)[VoteChoice.YES] == 1
    assert paused.id not in house.EffectiveReaders(other, lapsed)


def test_daily_summary_announces_dismissal_effects():
    house, people = CreateHouseWithParticipants(4)
    target = people[3]
    jury = house.CreateTopic(people[0].id, Subsection.JURY, "", START, target_participant_id=target.id)
    forum = house.CreateTopic(people[0].id, Subsection.FORUM, "Chairs", START, poll_question="Buy?")
    house.CastVote(forum.id, target.id, VoteChoice.YES, "Yes.", START)
    for person in people[:3]:
        house.CastVote(jury.id, person.id, VoteChoice.YES, "Yes.", START)
    when = _Close(house, jury)
    summary = house.DailySummary(people[0].id, when)
    (dismissal,) = summary["dismissals_today"]
    assert dismissal["participant_id"] == target.id
    assert dismissal["effects"][0]["title"] == "Chairs"
    assert dismissal["effects"][0]["dismissed"] == 1


def RunAllTests():
    """Run every test function in this module and report pass/fail counts."""
    import sys
    import traceback

    tests = [
        (name, value) for name, value in list(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failure_count = 0
    for name, test_function in tests:
        try:
            test_function()
            print(f"PASS  {name}")
        except Exception:
            failure_count += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failure_count}/{len(tests)} tests passed")
    sys.exit(1 if failure_count else 0)


if __name__ == "__main__":
    RunAllTests()


def test_allowed_range_matches_the_design_examples():
    assert AllowedRange(0.667) == (63, 70)
    assert AllowedRange(0.25) == (24, 26)
    assert AllowedRange(0.09) == (9, 10)  # never below 9%, but can still move up a point
    assert AllowedRange(1.0) == (95, 100)  # never above 100%


def test_question_attached_later_starts_minimum_life_then():
    house, people = CreateHouseWithParticipants(4)
    topic = house.CreateTopic(people[0].id, Subsection.FORUM, "Chairs for the hall", START)
    assert not topic.HasPoll() and topic.pollster_id is None
    try:
        house.CastVote(topic.id, people[1].id, VoteChoice.YES, "Yes.", START)
        assert False, "should have raised"
    except NoPollOnTopic:
        pass
    house.Tick(START + Days(60))
    assert not topic.closed  # without a Question it never closes

    attached = START + Days(10)
    house.AttachQuestion(topic.id, people[2].id, "Shall we buy 40 chairs?", attached)
    assert topic.pollster_id == people[2].id and topic.question_attached_at == attached
    assert people[2].id in topic.readers
    try:
        house.AttachQuestion(topic.id, people[3].id, "Another question?", attached)
        assert False, "only one Question per Conversation"
    except InvalidTopic:
        pass
    for person in people:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Good idea.", attached)
    house.Tick(attached + Days(20))  # Seasoning Time is over, but not 3 weeks since the Question
    assert not topic.closed
    house.Tick(attached + Days(21) + dt.timedelta(hours=1))
    assert topic.closed and topic.result is PollResult.PASSED


def test_initiator_is_pollster_when_starting_with_a_question():
    house, people = CreateHouseWithParticipants(4)
    forum = house.CreateTopic(people[0].id, Subsection.FORUM, "Chairs", START, poll_question="Buy?")
    jury = house.CreateTopic(people[1].id, Subsection.JURY, "", START, target_participant_id=people[3].id)
    assert forum.pollster_id == people[0].id and forum.question_attached_at == START
    assert jury.pollster_id == people[1].id
