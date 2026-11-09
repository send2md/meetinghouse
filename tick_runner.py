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
from meetinghouse import PollResult, Subsection

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
    LogMembershipChanges(house, conn, now)
    return closed


def LogMembershipChanges(house, conn, now):
    """Put in the public log the changes that happen by the calendar alone:
    a Member pausing when their yearly check lapses, and a dismissed Member
    coming back when their Dismissal ends. Each is logged once."""
    paused = {(e["data"].get("participant_id"), e["data"].get("due")) for e in persistence.LogEntriesOfKind(conn, "member_paused")}
    returned = {(e["data"].get("participant_id"), e["data"].get("jury_topic_id")) for e in persistence.LogEntriesOfKind(conn, "member_returned")}
    for participant in house.participants.values():
        latest = participant.LatestFormDate()
        if latest is not None and not participant.HasCurrentForm(now):
            due = (latest + dt.timedelta(days=365)).isoformat()
            if (participant.id, due) not in paused:
                persistence.LogEvent(conn, {"kind": "member_paused", "at": now, "participant_id": participant.id, "due": due})
        for dismissal in participant.dismissals:
            if dismissal.end <= now and (participant.id, dismissal.topic_id) not in returned:
                persistence.LogEvent(
                    conn,
                    {"kind": "member_returned", "at": now, "participant_id": participant.id, "jury_topic_id": dismissal.topic_id},
                )


def CodeVersion():
    """The version of the code running here, from the VERSION file the deploy writes."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return ""
