"""The close-and-persist flow behind Meetinghouse.Tick().

Factored out of app.py so it can run two ways in production: inside
the Flask process on hosts that can keep a background thread alive,
or as a one-shot cron invocation (see tick.py) on hosts like TigerTech
shared hosting, where a FastCGI worker's lifetime isn't ours to
control and a thread inside it can't be relied on to keep running.
"""

from __future__ import annotations

import datetime as dt
import os

import mailer
import persistence
import security
import stewards
from meetinghouse import Dismissal, PollResult, Subsection

REMINDER_DAYS = (30, 7)


def SiteAddress():
    origins = [o.strip().rstrip("/") for o in os.environ.get("MEETINGHOUSE_ORIGINS", "").split(",") if o.strip()]
    return origins[0] if origins else "https://meetinghouse.ai"


def SendRenewalReminders(house, conn, now):
    """Email members 30 and 7 days before their yearly selfie is due. Safe to run often."""
    sent = []
    for participant in house.participants.values():
        latest = participant.LatestFormDate()
        if latest is None or not security.IsActivated(conn, participant.id):
            continue
        due = latest + dt.timedelta(days=365)
        days_left = (due - now.date()).days
        member = security.GetMember(conn, participant.id)
        if not member["email"]:
            continue
        already = set(filter(None, member["reminders_sent"].split(",")))
        for days in sorted(REMINDER_DAYS):  # most urgent first
            key = f"{due.isoformat()}:{days}"
            if days_left <= days and days_left >= 0 and key not in already:
                mailer.Send(
                    member["email"],
                    f"Your yearly Meetinghouse check is due {due:%B} {due.day}",
                    f"Dear {participant.name},\n\nOnce a year we ask each member to confirm they're still here "
                    f"with a quick live selfie. Yours is due by {due:%B} {due.day}, {due.year}.\n\n"
                    f"Log in at {SiteAddress()}, open Account, and choose \"I'm still here\". "
                    "If the date passes, your membership pauses (you can still read everything) until you do.\n\nMeetinghouse",
                )
                already.add(key)
                sent.append((participant.id, days))
                # Only the most urgent reminder for this date is needed.
                already.update(f"{due.isoformat()}:{d}" for d in REMINDER_DAYS if d > days)
                break
        security.UpdateMember(conn, participant.id, reminders_sent=",".join(sorted(already)))
    return sent


def RunTick(house, conn, now):
    """Close every eligible Topic/Poll, persist the results, and return them."""
    prev_len = len(house.event_log)
    closed = house.Tick(now)
    # A dismissal changes other open Topics too (votes removed, clock
    # restarted, a note added), so save every open Topic as well.
    for topic in list(closed) + [t for t in house.topics.values() if not t.closed]:
        persistence.SaveTopic(conn, topic)
        if topic.result is PollResult.PASSED and topic.subsection is Subsection.RULES and topic.proposed_change:
            persistence.SaveRules(conn, topic.proposed_change.subsection, house.current_rules[topic.proposed_change.subsection])
        if topic.result is PollResult.PASSED and topic.subsection is Subsection.JURY:
            participant = house.participants[topic.target_participant_id]
            persistence.SaveParticipant(conn, participant)
    for entry in house.event_log[prev_len:]:
        persistence.LogEvent(conn, entry)
    SettleSamePersonChecks(house, conn, closed, now)
    stewards.Tick(house, conn, now)
    LogMembershipChanges(house, conn, now)
    ForgetFacePrints(house, conn, now)
    return closed


def LogMembershipChanges(house, conn, now):
    """Put in the public log the changes that happen by the calendar alone:
    a Member pausing when their yearly check lapses, and a dismissed Member
    coming back when their Dismissal ends. Each is logged once."""
    paused = {(e["data"].get("participant_id"), e["data"].get("due")) for e in persistence.LogEntriesOfKind(conn, "member_paused")}
    returned = {(e["data"].get("participant_id"), e["data"].get("jury_topic_id")) for e in persistence.LogEntriesOfKind(conn, "member_returned")}
    for participant in house.participants.values():
        latest = participant.LatestFormDate()
        if latest is not None and not participant.HasLeft() and not participant.HasCurrentForm(now):
            due = (latest + dt.timedelta(days=365)).isoformat()
            if (participant.id, due) not in paused:
                persistence.LogEvent(conn, {"kind": "member_paused", "at": now, "participant_id": participant.id, "due": due})
        for dismissal in participant.dismissals:
            if dismissal.end <= now and (participant.id, dismissal.topic_id) not in returned:
                persistence.LogEvent(
                    conn,
                    {"kind": "member_returned", "at": now, "participant_id": participant.id, "jury_topic_id": dismissal.topic_id},
                )


def AdmitMember(house, conn, participant, now, extra=""):
    """Make someone who passed their checks a Member, log it, and welcome them."""
    security.MarkActivated(conn, participant.id, now)
    participant.SignForm(signed_date=now.date())
    persistence.SaveParticipant(conn, participant)
    persistence.LogEvent(conn, {"kind": "member_joined", "at": now, "participant_id": participant.id})
    member = security.GetMember(conn, participant.id)
    if member is not None and member["email"]:
        mailer.Send(
            member["email"],
            "Welcome to Meetinghouse",
            f"Dear {participant.name},\n\n{extra}Your membership is active. "
            f"You can now take part in Conversations, comment, Vote and start Conversations at {SiteAddress()}.\n\n"
            "Once a year we'll ask you to confirm you're still here with a quick selfie.\n\nMeetinghouse",
        )


def SettleSamePersonChecks(house, conn, closed, now):
    """Act on same-person Jury Conversations that just closed.

    Yes, and the match is a current Member (even one on a Dismissal): one
    person trying to hold two accounts, so the newcomer is not let in.
    Yes, and the match is someone who left: they've come back, which is
    allowed, so they're let in and any Dismissal they still owe starts now.
    No, too few Votes, or mostly "I can't tell": they're let in.
    """
    for topic in closed:
        if not topic.same_as_id:
            continue
        newcomer = house.participants.get(topic.target_participant_id)
        match = house.participants.get(topic.same_as_id)
        if newcomer is None or newcomer.HasLeft() or security.IsActivated(conn, newcomer.id):
            continue
        review = security.GetFaceReview(conn, newcomer.id)
        similarity = review["similarity"] if review else 0.0
        same = topic.result is PollResult.PASSED
        if same and match is not None and not match.HasLeft():
            security.SetFaceReview(conn, newcomer.id, match.id, similarity, "kept_out", now, topic.id)
            persistence.LogEvent(conn, {"kind": "join_refused_same_person", "at": now, "participant_id": newcomer.id, "topic_id": topic.id})
            member = security.GetMember(conn, newcomer.id)
            if member is not None and member["email"]:
                mailer.Send(
                    member["email"],
                    "Your Meetinghouse membership",
                    f"Dear {newcomer.name},\n\nThe Jury Committee decided that you are already a Meetinghouse Member "
                    "under another account, so this one can't become a Member. Each person can be a Member only once.\n\n"
                    f"You can read the Conversation at {SiteAddress()}/topic/{topic.id}\n\nMeetinghouse",
                )
            continue
        if same and match is not None:
            # They came back: earlier Dismissals carry over (so the next one
            # still doubles), and any they still owe starts now.
            for old in match.dismissals:
                if old.start >= match.left_at:
                    old = Dismissal(start=now, end=now + (old.end - old.start), ordinal=old.ordinal, topic_id=old.topic_id)
                newcomer.dismissals.append(old)
            match.dismissals = [d for d in match.dismissals if d.start < match.left_at]
            persistence.SaveParticipant(conn, match)
            security.SetFacePrint(conn, match.id, "")  # nothing is owed under the old account now
        security.SetFaceReview(conn, newcomer.id, topic.same_as_id, similarity, "admitted", now, topic.id)
        AdmitMember(house, conn, newcomer, now, extra="The Jury Committee has finished looking at your photo. ")


def KeepFacePrint(house, participant_id):
    """Whether to keep someone's face print. Always for a Member; for someone
    who left, only while a Jury Conversation about them is open, or a
    Dismissal decided after they left is still to be served."""
    participant = house.participants.get(participant_id)
    if participant is None:
        return False
    if not participant.HasLeft():
        return True
    if any(t.target_participant_id == participant_id and not t.closed for t in house.topics.values()):
        return True
    return any(d.start >= participant.left_at for d in participant.dismissals)


def ForgetFacePrints(house, conn, now):
    """Delete the face prints nothing needs any more (see KeepFacePrint)."""
    for participant_id in list(security.FacePrints(conn)):
        if not KeepFacePrint(house, participant_id):
            security.SetFacePrint(conn, participant_id, "")


def CodeVersion():
    """The version of the code running here, from the VERSION file the deploy writes."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return ""
