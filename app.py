"""Web front end for the Meetinghouse engine.

This module owns everything meetinghouse.py deliberately does not:
wall-clock time, HTTP, sessions/login, and calling into persistence.py
after each mutation. The single in-process `house` is the same
Meetinghouse object the engine's tests exercise; this file just feeds
it real time and real people, and keeps SQLite in sync.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import secrets
import sqlite3
import threading
import time

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from werkzeug.security import check_password_hash

import idcheck
import mailer
import persistence
import security
import tick_runner
from meetinghouse import (
    RESTART_LIMIT,
    SETTING_LIMITS,
    AllowedRange,
    AllowedValues,
    SETTING_NAMES,
    DuplicateTopic,
    MeetinghouseError,
    Percent,
    RuleChange,
    Subsection,
    VoteChoice,
)

DB_PATH = os.environ.get("MEETINGHOUSE_DB", os.path.join(os.path.dirname(__file__), "meetinghouse.db"))
ADMIN_PASSWORD = os.environ.get("MEETINGHOUSE_ADMIN_PASSWORD", "admin")
TICK_INTERVAL_SECONDS = int(os.environ.get("MEETINGHOUSE_TICK_SECONDS", "300"))

# Members' verified photos live outside ~/html, next to the SQLite
# file, and are served through the MemberPhoto route.
UPLOAD_DIR = os.environ.get("MEETINGHOUSE_UPLOADS", os.path.join(os.path.dirname(DB_PATH), "uploads"))

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("MEETINGHOUSE_SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # cap uploads (the photo) at 8 MB
# Session cookies: never readable by page scripts, not sent along with
# requests started by other sites, and (on the live HTTPS site, where
# config.py sets MEETINGHOUSE_SECURE_COOKIES=1) never sent over plain HTTP.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("MEETINGHOUSE_SECURE_COOKIES") == "1"

LOCK = threading.Lock()
conn = persistence.Connect(DB_PATH)
house = persistence.LoadHouse(conn)


def Now():
    """The one place this app reads the wall clock, so it stays swappable."""
    return dt.datetime.utcnow()


security.InitSchema(conn)
security.EnsureStatusRows(conn, house, Now())
persistence.ForgetRetiredProfileFields(conn)


def FlushNewEvents(prev_len):
    """Persist any event_log entries the engine appended since `prev_len`."""
    for entry in house.event_log[prev_len:]:
        persistence.LogEvent(conn, entry)


def RunTick():
    """Close every eligible Conversation and persist the resulting changes."""
    with LOCK:
        closed = tick_runner.RunTick(house, conn, Now())
        tick_runner.SendRenewalReminders(house, conn, Now())
        return closed


def SchedulerLoop():
    while True:
        threading.Event().wait(TICK_INTERVAL_SECONDS)
        try:
            RunTick()
        except Exception as exc:  # pragma: no cover - background safety net
            app.logger.exception("Scheduled Tick() failed: %s", exc)


def StartScheduler():
    thread = threading.Thread(target=SchedulerLoop, daemon=True)
    thread.start()


def _SuggestUsername(base):
    """The next available `root2`, `root3`, ... login for a taken username.

    Strips any trailing digits from `base` first, so suggesting an
    alternative for an already-suffixed "janedoe2" offers "janedoe3"
    rather than piling on a second suffix ("janedoe22").
    """
    root = base.rstrip("0123456789") or base
    suffix = 2
    candidate = f"{root}{suffix}"
    while persistence.GetCredentialsByUsername(conn, candidate) is not None:
        suffix += 1
        candidate = f"{root}{suffix}"
    return candidate


@app.template_filter("humanage")
def HumanAge(delta: dt.timedelta):
    days = delta.days
    if days >= 7:
        return f"{days // 7}w {days % 7}d"
    hours = delta.seconds // 3600
    return f"{days}d {hours}h"


@app.template_filter("pct")
def PercentFilter(fraction):
    return Percent(fraction)


@app.template_filter("day")
def DayFilter(value):
    return "" if value is None else f"{value:%B} {value.day}, {value.year}"


@app.template_filter("fmt")
def FormatDateTime(value):
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


@app.context_processor
def InjectHelpers():
    def participant_name(participant_id):
        participant = house.participants.get(participant_id)
        return participant.name if participant else "(unknown)"

    return {
        "sandbox": _Sandbox(),
        "sandbox_members": _SandboxMembers() if _Sandbox() else [],
        "participant_name": participant_name,
        "current_participant": g.get("participant", None),
        "Subsection": Subsection,
        "VoteChoice": VoteChoice,
    }


@app.before_request
def LoadCurrentParticipant():
    g.participant = None
    participant_id = session.get("participant_id")
    if participant_id and participant_id in house.participants:
        g.participant = house.participants[participant_id]


def RequireLogin():
    if g.participant is None:
        flash("Please log in first.")
        return redirect(url_for("Login", next=request.path))
    return None


def RequireAdmin():
    if not session.get("is_admin"):
        flash("Admin login required.")
        return redirect(url_for("AdminLogin"))
    return None


@app.route("/")
def Dashboard():
    """Logged-out visitors see a plain login form here, nothing else.

    Unlike RequireLogin() (used by every other protected page), this
    doesn't redirect or flash "please log in" — "/" IS the login page
    for a logged-out visitor, not a bounce through one.
    """
    if g.participant is None:
        return render_template("login.html")
    now = Now()
    open_by_subsection = {s: [] for s in Subsection}
    for topic in house.topics.values():
        if topic.closed:
            continue
        counts = house.EffectiveVoteCounts(topic, now) if topic.HasPoll() else None
        open_by_subsection[topic.subsection].append(
            {"topic": topic, "counts": counts, "age": now - topic.created_at, "contactors": len(house.EffectiveReaders(topic, now))}
        )
    for items in open_by_subsection.values():
        items.sort(key=lambda item: item["topic"].created_at)
    return render_template(
        "dashboard.html",
        open_by_subsection=open_by_subsection,
        now=now,
        activated=security.IsActivated(conn, g.participant.id),
        paused=security.IsActivated(conn, g.participant.id) and not g.participant.IsParticipant(now),
        renew_soon=_RenewalDue(g.participant, now),
        has_passkey=security.HasPasskeys(conn, g.participant.id),
    )


def _RenewalDue(participant, now):
    """The renewal date, if it falls within the next 30 days."""
    latest = participant.LatestFormDate()
    if latest is None:
        return None
    due = latest + dt.timedelta(days=365)
    return due if 0 <= (due - now.date()).days <= 30 else None


@app.route("/login", methods=["GET", "POST"])
def Login():
    """Step one: just the Meetinghouse User Name, nothing else yet.

    A username that matches nobody is free to claim, so there's no point
    asking for a password at all — straight to Register. A username that
    matches someone goes on to LoginPassword for the actual password
    prompt, which is its own separate screen (matching the two boxes in
    the design sketch, not one combined form).
    """
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if not username:
            flash("Enter a login.")
            return render_template("login.html")
        row = persistence.GetCredentialsByUsername(conn, username)
        if row is None:
            session["pending_username"] = username
            return redirect(url_for("Register"))
        session["login_username"] = username
        if security.HasPasskeys(conn, row["participant_id"]):
            return redirect(url_for("LoginPasskey"))
        return redirect(url_for("LoginPassword"))
    return render_template("login.html")


def _UserKey(username):
    """The login_throttle key for one account."""
    return f"user:{username}"


def _LockedMessage(until):
    minutes = max(1, int((until - Now()).total_seconds() // 60) + 1)
    return f"Too many wrong tries. For safety, this login is paused for about {minutes} more minute(s)."


def _LoginBlockedReason(participant):
    """Why this (correctly identified) person still can't log in, if anything.

    People still joining, and members whose yearly check has lapsed
    (paused), CAN log in: they need to, to finish joining or renew.
    They just can't vote, post or start Topics until then.
    """
    if participant is None:
        return "That account no longer exists."
    return None


def _CompleteLogin(participant, username):
    session.pop("login_username", None)
    session.pop("pending_username", None)
    session["participant_id"] = participant.id
    security.ClearFailures(conn, _UserKey(username))
    previous = security.RecordLogin(conn, participant.id, Now())
    if previous is None:
        flash("This is your first login.")
    else:
        flash(f"You last logged in on {previous:%B} {previous.day}, {previous.year} at {previous:%H:%M} UTC. If that wasn't you, use account recovery.")


def _Activate(participant):
    """Joining counts as signing the participation form for a year."""
    with LOCK:
        security.MarkActivated(conn, participant.id, Now())
        participant.SignForm(signed_date=Now().date())
        persistence.SaveParticipant(conn, participant)


@app.route("/login/password", methods=["GET", "POST"])
def LoginPassword():
    """Step two, only reached once Login has confirmed the username exists."""
    username = session.get("login_username")
    if not username:
        return redirect(url_for("Login"))

    row = persistence.GetCredentialsByUsername(conn, username)
    if row is not None and security.HasPasskeys(conn, row["participant_id"]):
        # Once someone has a passkey, their password no longer logs them in.
        return redirect(url_for("LoginPasskey"))

    if request.method == "POST":
        locked = security.LockedUntil(conn, _UserKey(username), Now())
        if locked:
            flash(_LockedMessage(locked))
            return render_template("login_password.html", username=username)
        password = request.form.get("password", "")
        if row is not None and check_password_hash(row["password_hash"], password):
            participant = house.participants.get(row["participant_id"])
            reason = _LoginBlockedReason(participant)
            if reason:
                flash(reason)
                return render_template("login_password.html", username=username)
            _CompleteLogin(participant, username)
            flash(f"Welcome, {participant.name}.")
            return redirect(_SafeNext() or url_for("Dashboard"))
        locked = security.RecordFailure(conn, _UserKey(username), Now())
        if locked:
            flash(_LockedMessage(locked))
            return render_template("login_password.html", username=username)
        return render_template("login_retry.html", username=username)

    return render_template("login_password.html", username=username)


def _SafeNext():
    """A ?next= target, but only a path on this site (never another site)."""
    target = request.args.get("next") or ""
    return target if target.startswith("/") and not target.startswith("//") else None


def _Sandbox():
    """Whether this copy of the site is the practice sandbox (config sets MEETINGHOUSE_SANDBOX=1).

    The sandbox lets any visitor step into a pretend Member's account, so
    it must only ever run on its own database filled by sandbox.py, never
    on the live site.
    """
    return os.environ.get("MEETINGHOUSE_SANDBOX") == "1"


def _SandboxMembers():
    rows = conn.execute("SELECT username, participant_id FROM credentials ORDER BY username").fetchall()
    return [
        {"username": row["username"], "name": house.participants[row["participant_id"]].name}
        for row in rows
        if row["participant_id"] in house.participants
    ]


@app.route("/sandbox/as/<username>", methods=["POST"])
def SandboxStepIn(username):
    """In the practice sandbox only: become one of the pretend Members."""
    if not _Sandbox():
        abort(404)
    row = persistence.GetCredentialsByUsername(conn, username)
    if row is None or row["participant_id"] not in house.participants:
        abort(404)
    session.clear()
    session["participant_id"] = row["participant_id"]
    flash(f"You are now {house.participants[row['participant_id']].name}, a pretend Member. Look around, comment and Vote.")
    return redirect(url_for("Dashboard"))


@app.route("/logout")
def Logout():
    session.pop("participant_id", None)
    return redirect(url_for("Login"))


@app.route("/login/claim", methods=["POST"])
def LoginClaim():
    """"This is a new login": `username` is taken, so offer an alternative."""
    username = request.form.get("username", "").strip()
    if not username:
        return redirect(url_for("Login"))
    return render_template("username_taken.html", taken_username=username, suggestion=_SuggestUsername(username))


@app.route("/register/start", methods=["POST"])
def RegisterStart():
    """Claim `username` for a new Member, or bounce back if it's (now) taken."""
    username = request.form.get("username", "").strip()
    if not username:
        flash("Choose a login.")
        return redirect(url_for("Login"))
    if persistence.GetCredentialsByUsername(conn, username) is not None:
        return render_template("username_taken.html", taken_username=username, suggestion=_SuggestUsername(username))
    session["pending_username"] = username
    return redirect(url_for("Register"))


@app.route("/register", methods=["GET", "POST"])
def Register():
    """Step one of joining: name, email and town. No password, ever.

    The account is created straight away but can't take part until the
    person has added a passkey and passed the ID check (see Join).
    """
    username = session.get("pending_username")
    if not username:
        flash("Start from the login page to join.")
        return redirect(url_for("Login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        town = request.form.get("town", "").strip()
        zip_code = request.form.get("zip", "").strip()
        errors = []
        if not name:
            errors.append("Your whole real name is required.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors.append("A working email address is required: it's how you get back in if you lose your device.")
        if not town:
            errors.append("Your town is required.")
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z \-]{1,9}", zip_code):
            errors.append("Your ZIP code (or postal code) is required.")
        if not request.form.get("agree"):
            errors.append("Please tick the box to show you've read how your name, photo and ID are handled.")
        if errors:
            for error in errors:
                flash(error)
            return render_template("register.html", username=username, form=request.form, providers=idcheck.GetProviders())

        with LOCK:
            if persistence.GetCredentialsByUsername(conn, username) is not None:
                flash("That login was just taken. Please choose another.")
                return render_template(
                    "username_taken.html", taken_username=username, suggestion=_SuggestUsername(username)
                )
            participant = house.RegisterParticipant(name)
            persistence.SaveParticipant(conn, participant)
            security.CreatePendingStatus(conn, participant.id)
            try:
                persistence.SetCredentials(conn, participant.id, username, security.NO_PASSWORD)
            except sqlite3.IntegrityError:
                flash("That login was just taken. Please choose another.")
                return render_template(
                    "username_taken.html", taken_username=username, suggestion=_SuggestUsername(username)
                )
            security.UpdateMember(conn, participant.id, email=email, town=town, zip=zip_code)

        session.pop("pending_username", None)
        session["participant_id"] = participant.id
        return redirect(url_for("Join"))

    return render_template("register.html", username=username, form={}, providers=idcheck.GetProviders())


@app.route("/join")
def Join():
    """Steps two and three of joining: add a passkey, then pass the ID check."""
    guard = RequireLogin()
    if guard:
        return guard
    if security.IsActivated(conn, g.participant.id):
        return redirect(url_for("Dashboard"))
    latest = security.LatestIdSession(conn, g.participant.id, idcheck.JOIN)
    return render_template(
        "join.html",
        has_passkey=security.HasPasskeys(conn, g.participant.id),
        latest=latest,
        providers=idcheck.GetProviders(),
    )


def _StartIdCheck(purpose):
    """Start an ID check with the approved company the Member chose."""
    if not idcheck.GetProviders():
        flash("ID checks aren't available yet, so joining and renewing are paused. Please try again later.")
        return None
    provider = idcheck.GetProvider(request.form.get("provider") or None)
    if provider is None:
        flash("Choose one of the ID-check companies on the list.")
        return None
    session_id = security.StartIdSession(conn, g.participant.id, purpose, provider.name, Now())
    return_url = _SiteAddress() + url_for("IdCheckReturn", session_id=session_id)
    try:
        target, reference = provider.Start(session_id, g.participant.id, purpose, return_url, url_for)
    except idcheck.NotAvailable as exc:
        flash(str(exc))
        return None
    if reference:
        security.SetIdSessionReference(conn, session_id, reference)
    return target


@app.route("/join/idcheck", methods=["POST"])
def JoinIdCheck():
    guard = RequireLogin()
    if guard:
        return guard
    if security.IsActivated(conn, g.participant.id):
        return redirect(url_for("Dashboard"))
    if not security.HasPasskeys(conn, g.participant.id):
        flash("Add your passkey first, so only you can log in to the account you're about to verify.")
        return redirect(url_for("Join"))
    target = _StartIdCheck(idcheck.JOIN)
    return redirect(target or url_for("Join"))


@app.route("/account/renew", methods=["POST"])
def RenewIdCheck():
    """The yearly "I'm still here": a live selfie matched to the member's face."""
    guard = RequireLogin()
    if guard:
        return guard
    if not security.IsActivated(conn, g.participant.id):
        return redirect(url_for("Join"))
    target = _StartIdCheck(idcheck.RENEWAL)
    return redirect(target or url_for("Account"))


def ApplyIdResult(result: idcheck.IdResult):
    """Act on an ID-check outcome, whichever service it came from. Returns the session row."""
    row = security.GetIdSession(conn, result.session_id)
    if row is None or row["status"] != "pending":
        return row
    participant = house.participants.get(row["participant_id"])
    if participant is None:
        return row
    now = Now()
    security.FinishIdSession(conn, result.session_id, result.outcome, result.reference, now)
    if result.outcome != idcheck.APPROVED:
        return security.GetIdSession(conn, result.session_id)

    if result.selfie:
        participant_dir = os.path.join(UPLOAD_DIR, participant.id)
        os.makedirs(participant_dir, exist_ok=True)
        path = os.path.join(participant_dir, f"photo.{result.selfie_ext}")
        with open(path, "wb") as f:
            f.write(result.selfie)
        security.UpdateMember(conn, participant.id, photo_path=path)
    security.UpdateMember(
        conn, participant.id, verified_at=now, verified_country=result.country, verified_ref=result.reference
    )
    member = security.GetMember(conn, participant.id)
    if row["purpose"] == idcheck.JOIN:
        _Activate(participant)
        mailer.Send(
            member["email"],
            "Welcome to Meetinghouse",
            f"Dear {participant.name},\n\nYour ID check passed and your Meetinghouse membership is active. "
            f"You can now take part in Conversations, comment, Vote and start Conversations at {_SiteAddress()}.\n\n"
            "Once a year we'll ask you to confirm you're still here with a quick selfie.\n\nMeetinghouse",
        )
    else:
        with LOCK:
            participant.SignForm(signed_date=now.date())
            persistence.SaveParticipant(conn, participant)
        security.UpdateMember(conn, participant.id, reminders_sent="")
    return security.GetIdSession(conn, result.session_id)


@app.route("/idcheck/practice/<session_id>", methods=["GET", "POST"])
def PracticeIdCheck(session_id):
    """The stand-in ID check for local testing. Unreachable unless config chooses it."""
    provider = idcheck.GetProvider("practice")
    if provider is None:
        abort(404)
    row = security.GetIdSession(conn, session_id)
    if row is None:
        abort(404)
    if request.method == "POST":
        outcome = request.form.get("outcome")
        if outcome not in (idcheck.APPROVED, idcheck.DECLINED, idcheck.DUPLICATE):
            abort(400)
        ApplyIdResult(
            idcheck.IdResult(
                session_id=session_id,
                outcome=outcome,
                reference=f"practice-{session_id[:8]}",
                country="Practice",
                selfie=idcheck.PRACTICE_SELFIE,
                selfie_ext="png",
            )
        )
        return redirect(url_for("IdCheckReturn", session_id=session_id))
    return render_template("idcheck_practice.html", row=row)


@app.route("/idcheck/return/<session_id>")
def IdCheckReturn(session_id):
    """Where the ID-check service sends the person back to."""
    row = security.GetIdSession(conn, session_id)
    if row is None:
        abort(404)
    provider = idcheck.GetProvider(row["provider"])
    if row["status"] == "pending" and provider is not None:
        try:
            result = provider.Fetch(row)
        except Exception as exc:  # the webhook will still deliver it
            app.logger.info("Couldn't fetch ID-check result yet: %s", exc)
            result = None
        if result is not None:
            with LOCK:
                row = ApplyIdResult(result)
    return render_template("idcheck_return.html", row=row)


@app.route("/idcheck/didit/webhook", methods=["POST"])
def DiditWebhook():
    """Didit's signed notice that a check finished. Anything unsigned is refused."""
    provider = idcheck.GetProvider("didit")
    if provider is None:
        abort(404)
    body = provider.VerifyWebhook(request.get_data(), request.headers, time.time())
    if body is None:
        return jsonify(error="bad signature"), 401
    metadata = body.get("metadata") or {}
    row = security.GetIdSession(conn, metadata.get("meetinghouse_session", "")) or security.FindIdSessionByReference(
        conn, body.get("session_id", "")
    )
    if row is None:
        return jsonify(ok=True, note="unknown session")
    decision = dict(body.get("decision") or {})
    decision.setdefault("status", body.get("status", ""))
    decision.setdefault("session_id", body.get("session_id", ""))
    result = provider.Interpret(row["id"], row["purpose"], decision)
    if result is not None:
        with LOCK:
            ApplyIdResult(result)
    return jsonify(ok=True)


@app.route("/photo/<participant_id>")
def MemberPhoto(participant_id):
    """A member's verified selfie. Public, like their name."""
    if participant_id not in house.participants:
        abort(404)
    path = security.GetMember(conn, participant_id)["photo_path"]
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path)


# --- Mailed codes, passkeys and the Account page ------------------------------


def _RpId():
    """The passkey "relying party": the site's domain, e.g. meetinghouse.ai."""
    return os.environ.get("MEETINGHOUSE_RP_ID") or request.host.split(":")[0]


def _Origins():
    """Web addresses passkeys may be used from (config.py sets the live ones)."""
    raw = os.environ.get("MEETINGHOUSE_ORIGINS", "")
    origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    return origins or [request.host_url.rstrip("/")]


def _SiteAddress():
    return _Origins()[0]


@app.route("/login/recover", methods=["GET", "POST"])
def LoginRecover():
    """Lost your device? Ask for a recovery code by email; it works 48 hours later."""
    username = session.get("login_username")
    if not username:
        return redirect(url_for("Login"))
    row = persistence.GetCredentialsByUsername(conn, username)
    if row is None:
        return redirect(url_for("Login"))
    member = security.GetMember(conn, row["participant_id"])
    if request.method == "POST":
        if not member["email"]:
            flash("There's no email address on this account, so it can't be recovered by email.")
            return redirect(url_for("LoginRecover"))
        token, code = security.RequestRecovery(conn, row["participant_id"], Now())
        usable = Now() + security.RECOVERY_WAIT
        mailer.Send(
            member["email"],
            "Someone asked to recover your Meetinghouse login",
            f"Someone asked to recover the Meetinghouse login \"{username}\".\n\n"
            f"If this wasn't you, cancel it now: {_SiteAddress()}{url_for('CancelRecovery', token=token)}\n\n"
            f"If it was you: from {usable:%B} {usable.day} at {usable:%H:%M} UTC (48 hours from now, a safety wait), "
            f"go to {_SiteAddress()}, type your user name, choose \"Lost your device?\" and enter this code: "
            f"{security.FormatCode(code)}\n\nThe code works once and expires after 7 days.\n\nMeetinghouse",
        )
        flash("We've emailed you a recovery code. For your safety it starts working in 48 hours.")
        return redirect(url_for("LoginRecover"))
    return render_template(
        "login_recover.html",
        username=username,
        has_email=bool(member["email"]),
        pending=security.PendingRecovery(conn, row["participant_id"]),
    )


@app.route("/login/code", methods=["POST"])
def LoginCode():
    """Use the emailed recovery code (after the wait): logs in and clears the lost passkeys."""
    username = session.get("login_username")
    row = persistence.GetCredentialsByUsername(conn, username) if username else None
    if row is None:
        return redirect(url_for("Login"))
    key = _UserKey(username)
    locked = security.LockedUntil(conn, key, Now())
    if locked:
        flash(_LockedMessage(locked))
        return redirect(url_for("LoginRecover"))
    outcome = security.UseRecoveryCode(conn, row["participant_id"], request.form.get("code", ""), Now())
    if outcome == "too_soon":
        pending = security.PendingRecovery(conn, row["participant_id"])
        flash(f"That code is right, but it only starts working on {pending['usable_from']:%B} {pending['usable_from'].day} at {pending['usable_from']:%H:%M} UTC.")
        return redirect(url_for("LoginRecover"))
    if outcome != "ok":
        locked = security.RecordFailure(conn, key, Now())
        flash(_LockedMessage(locked) if locked else "That code didn't match. Check the email and try again.")
        return redirect(url_for("LoginRecover"))
    participant = house.participants.get(row["participant_id"])
    reason = _LoginBlockedReason(participant)
    if reason:
        flash(reason)
        return redirect(url_for("Login"))
    security.RemoveAllPasskeys(conn, participant.id)  # the lost device must not work any more
    _CompleteLogin(participant, username)
    flash("You're back in. Your old passkeys were removed: add a passkey on this device now so you can log in next time.")
    return redirect(url_for("Account"))


@app.route("/recover/cancel/<token>")
def CancelRecovery(token):
    participant_id = security.CancelRecovery(conn, token)
    if participant_id is None:
        return render_template("message.html", title="Nothing to cancel",
                               text="That recovery request was already used, cancelled or replaced.")
    return render_template("message.html", title="Recovery cancelled",
                           text="Nobody can use that recovery code now. Your login is unchanged.")


@app.route("/login/passkey")
def LoginPasskey():
    username = session.get("login_username")
    if not username:
        return redirect(url_for("Login"))
    return render_template("login_passkey.html", username=username)


@app.route("/login/passkey/options", methods=["POST"])
def LoginPasskeyOptions():
    username = session.get("login_username")
    row = persistence.GetCredentialsByUsername(conn, username) if username else None
    if row is None:
        return jsonify(error="Start again from the login page."), 400
    passkeys = security.ListPasskeys(conn, row["participant_id"])
    if not passkeys:
        return jsonify(error="This account has no passkey yet."), 400
    options = generate_authentication_options(
        rp_id=_RpId(),
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["credential_id"])) for p in passkeys],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    session["passkey_login_challenge"] = bytes_to_base64url(options.challenge)
    return app.response_class(options_to_json(options), mimetype="application/json")


@app.route("/login/passkey/verify", methods=["POST"])
def LoginPasskeyVerify():
    username = session.get("login_username")
    challenge = session.pop("passkey_login_challenge", None)
    row = persistence.GetCredentialsByUsername(conn, username) if username else None
    if row is None or challenge is None:
        return jsonify(error="Start again from the login page."), 400
    credential = (request.get_json(silent=True) or {}).get("credential") or {}
    stored = security.GetPasskey(conn, str(credential.get("id", "")))
    if stored is None or stored["participant_id"] != row["participant_id"]:
        return jsonify(error="That passkey isn't registered for this account."), 400
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=_RpId(),
            expected_origin=_Origins(),
            credential_public_key=stored["public_key"],
            credential_current_sign_count=stored["sign_count"],
            require_user_verification=True,
        )
    except Exception as exc:  # any verification failure means: not let in
        app.logger.info("Passkey login refused for %s: %s", username, exc)
        return jsonify(error="That passkey couldn't be verified. Please try again."), 400
    participant = house.participants.get(row["participant_id"])
    reason = _LoginBlockedReason(participant)
    if reason:
        return jsonify(error=reason), 403
    security.TouchPasskey(conn, stored["credential_id"], verified.new_sign_count, Now())
    _CompleteLogin(participant, username)
    flash(f"Welcome, {participant.name}.")
    return jsonify(ok=True, redirect=url_for("Dashboard"))


@app.route("/account")
def Account():
    guard = RequireLogin()
    if guard:
        return guard
    participant_id = g.participant.id
    creds = persistence.GetCredentialsForParticipant(conn, participant_id)
    member = security.GetMember(conn, participant_id)
    latest = g.participant.LatestFormDate()
    return render_template(
        "account.html",
        username=creds["username"] if creds else "",
        activated=security.IsActivated(conn, participant_id),
        passkeys=security.ListPasskeys(conn, participant_id),
        has_password=bool(creds) and creds["password_hash"] != security.NO_PASSWORD,
        member=member,
        renew_by=(latest + dt.timedelta(days=365)) if latest else None,
        paused=security.IsActivated(conn, participant_id) and not g.participant.IsParticipant(Now()),
        providers=idcheck.GetProviders(),
    )


@app.route("/account/details", methods=["POST"])
def AccountDetails():
    guard = RequireLogin()
    if guard:
        return guard
    email = request.form.get("email", "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Please give a working email address.")
        return redirect(url_for("Account"))
    security.UpdateMember(conn, g.participant.id, email=email)
    flash("Saved.")
    return redirect(url_for("Account"))


@app.route("/account/passkey/options", methods=["POST"])
def PasskeyRegisterOptions():
    if g.participant is None:
        return jsonify(error="Please log in first."), 401
    creds = persistence.GetCredentialsForParticipant(conn, g.participant.id)
    options = generate_registration_options(
        rp_id=_RpId(),
        rp_name="Meetinghouse",
        user_id=g.participant.id.encode("utf-8"),
        user_name=creds["username"],
        user_display_name=g.participant.name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["credential_id"]))
            for p in security.ListPasskeys(conn, g.participant.id)
        ],
    )
    session["passkey_register_challenge"] = bytes_to_base64url(options.challenge)
    return app.response_class(options_to_json(options), mimetype="application/json")


@app.route("/account/passkey/verify", methods=["POST"])
def PasskeyRegisterVerify():
    if g.participant is None:
        return jsonify(error="Please log in first."), 401
    challenge = session.pop("passkey_register_challenge", None)
    if challenge is None:
        return jsonify(error="Please start again."), 400
    body = request.get_json(silent=True) or {}
    try:
        verified = verify_registration_response(
            credential=body.get("credential") or {},
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=_RpId(),
            expected_origin=_Origins(),
            require_user_verification=True,
        )
    except Exception as exc:
        app.logger.info("Passkey registration refused: %s", exc)
        return jsonify(error="That passkey couldn't be set up. Please try again."), 400
    label = str(body.get("label") or "").strip()[:60] or f"Passkey added {Now():%Y-%m-%d}"
    security.AddPasskey(
        conn,
        g.participant.id,
        bytes_to_base64url(verified.credential_id),
        verified.credential_public_key,
        verified.sign_count,
        label,
        Now(),
    )
    if not security.IsActivated(conn, g.participant.id):
        flash(f"Passkey \u201c{label}\u201d added. Next: the ID check.")
        return jsonify(ok=True, redirect=url_for("Join"))
    flash(f"Passkey \u201c{label}\u201d added. From now on you log in with it.")
    return jsonify(ok=True, redirect=url_for("Account"))


@app.route("/account/passkey/remove", methods=["POST"])
def PasskeyRemove():
    guard = RequireLogin()
    if guard:
        return guard
    creds = persistence.GetCredentialsForParticipant(conn, g.participant.id)
    has_password = creds["password_hash"] != security.NO_PASSWORD
    if len(security.ListPasskeys(conn, g.participant.id)) <= 1 and not has_password:
        flash("That's your only passkey, so it's your only way to log in. Add another passkey first.")
        return redirect(url_for("Account"))
    security.RemovePasskey(conn, g.participant.id, request.form.get("credential_id", ""))
    flash("Passkey removed.")
    return redirect(url_for("Account"))


@app.route("/subsection/<name>")
def SubsectionView(name):
    guard = RequireLogin()
    if guard:
        return guard
    try:
        subsection = Subsection(name.capitalize())
    except ValueError:
        abort(404)
    now = Now()
    topics = [t for t in house.topics.values() if t.subsection is subsection]
    topics.sort(key=lambda t: t.created_at, reverse=True)
    rows = []
    for topic in topics:
        counts = house.EffectiveVoteCounts(topic, now) if topic.HasPoll() else None
        rows.append({"topic": topic, "counts": counts})
    return render_template(
        "subsection.html", subsection=subsection, rows=rows, rule=house.current_rules[subsection]
    )


@app.route("/topic/new/<name>", methods=["GET", "POST"])
def NewTopic(name):
    guard = RequireLogin()
    if guard:
        return guard
    try:
        subsection = Subsection(name.capitalize())
    except ValueError:
        abort(404)

    def Form(**extra):
        return render_template(
            "new_topic.html",
            subsection=subsection,
            participants=sorted(house.participants.values(), key=lambda p: p.name),
            settings=_SettingChoices(),
            form=request.form,
            **extra,
        )

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip() or None
        poll_question = request.form.get("poll_question", "").strip() or None
        target_participant_id = None
        proposed_change = None
        software_ref = None

        if subsection is Subsection.RULES:
            if request.form.get("kind") == "software":
                software_ref = request.form.get("software_ref", "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{40}", software_ref):
                    flash("Give the full 40-character commit id from GitHub, so exactly that code is what gets approved.")
                    return Form()
                if not title:
                    flash("Give the software change a title.")
                    return Form()
            else:
                proposed_change, error = _ParseRuleChange(request.form)
                if error:
                    flash(error)
                    return Form()
                title = None  # the engine names it, e.g. "Change Forum Approval from 66.7% to 70%"
        elif subsection is Subsection.JURY:
            target_participant_id = request.form.get("target_participant_id") or None

        try:
            with LOCK:
                prev_len = len(house.event_log)
                topic = house.CreateTopic(
                    g.participant.id,
                    subsection,
                    title,
                    Now(),
                    description=description,
                    poll_question=poll_question,
                    target_participant_id=target_participant_id,
                    proposed_change=proposed_change,
                    software_ref=software_ref,
                )
                persistence.SaveTopic(conn, topic)
                FlushNewEvents(prev_len)
        except DuplicateTopic as exc:
            return Form(duplicate={"message": str(exc), "topic_id": exc.existing_topic_id})
        except MeetinghouseError as exc:
            flash(str(exc))
            return Form()

        return redirect(url_for("TopicDetail", topic_id=topic.id))

    return Form()


def _SettingChoices():
    """Every changeable setting, its current value, and every value one vote may move it to."""
    choices = []
    for subsection in Subsection:
        for setting, label in SETTING_NAMES.items():
            low, high = SETTING_LIMITS[(subsection, setting)]
            current = getattr(house.current_rules[subsection], setting)
            choices.append(
                {
                    "key": f"{subsection.value}:{setting}",
                    "label": f"{subsection.value} {label}",
                    "current": current,
                    "options": [{"percent": round(v * 100)} for v in AllowedValues(current, low, high)],
                }
            )
    return choices


def _ParseRuleChange(form):
    """Read the chosen setting and new whole-percent value, e.g. "Forum:approval_fraction:63"."""
    try:
        subsection_name, setting, percent = form.get("change", "").split(":")
        subsection = Subsection(subsection_name)
        value = int(percent) / 100
    except ValueError:
        return None, "Choose which setting this Conversation changes, and its new value."
    if setting not in SETTING_NAMES:
        return None, "Choose which setting this Conversation changes, and its new value."
    return RuleChange(subsection=subsection, setting=setting, value=value), None


@app.route("/topic/<topic_id>")
def TopicDetail(topic_id):
    guard = RequireLogin()
    if guard:
        return guard
    topic = house.topics.get(topic_id)
    if topic is None:
        abort(404)
    now = Now()

    # Ask each Member once per Conversation, the first time they open it
    # with a Question they haven't voted on, whether they'd like to vote.
    ask_to_vote = False
    if g.participant.IsActive(now) and not topic.closed:
        with LOCK:
            try:
                changed = house.MarkRead(topic_id, g.participant.id, now)
                if (
                    topic.HasPoll()
                    and topic.CurrentVote(g.participant.id) is None
                    and g.participant.id not in topic.asked_to_vote
                ):
                    topic.asked_to_vote.append(g.participant.id)
                    ask_to_vote = changed = True
                if changed:
                    persistence.SaveTopic(conn, topic)
            except MeetinghouseError:
                pass

    # Comments and votes (each with its reason), in the order they happened.
    entries = []
    for history in topic.votes.values():
        for record in history:
            entries.append({"kind": "vote", "at": record.timestamp, "record": record})
    for post in topic.posts:
        entries.append({"kind": "post", "at": post.timestamp, "post": post})
    entries.sort(key=lambda e: e["at"])

    my_vote = topic.CurrentVote(g.participant.id) if topic.HasPoll() else None
    can_act = g.participant.IsActive(now) and not topic.closed
    return render_template(
        "topic.html",
        topic=topic,
        entries=entries,
        my_vote=my_vote,
        counts=house.EffectiveVoteCounts(topic, now) if topic.HasPoll() else None,
        dismissed_count=house.DismissedCount(topic, now),
        contactors=house.EffectiveReaders(topic, now),
        now=now,
        can_act=can_act,
        ask_question=can_act and not topic.HasPoll(),
        ask_to_vote=ask_to_vote,
        votes_left=topic.RestartsLeft(g.participant.id, "votes"),
        posts_left=topic.RestartsLeft(g.participant.id, "posts"),
        restart_limit=RESTART_LIMIT,
        setting_now=getattr(house.current_rules[topic.proposed_change.subsection], topic.proposed_change.setting)
        if topic.proposed_change
        else None,
    )


@app.route("/topic/<topic_id>/vote", methods=["POST"])
def CastVote(topic_id):
    guard = RequireLogin()
    if guard:
        return guard
    choice_raw = request.form.get("choice", "")
    comment = request.form.get("comment", "")
    try:
        choice = VoteChoice(choice_raw)
    except ValueError:
        flash("Choose Yes, Abstain, or No.")
        return redirect(url_for("TopicDetail", topic_id=topic_id))

    try:
        with LOCK:
            prev_len = len(house.event_log)
            topic = house.topics[topic_id]
            before = topic.RestartsLeft(g.participant.id, "votes")
            house.CastVote(topic_id, g.participant.id, choice, comment, Now())
            persistence.SaveTopic(conn, topic)
            FlushNewEvents(prev_len)
        left = topic.RestartsLeft(g.participant.id, "votes")
        if before == 0:
            flash("Vote recorded. It counts, but it no longer keeps the Conversation open.")
        elif left == 0:
            flash("Vote recorded. That was the last of your Votes that can keep this Conversation open.")
        else:
            flash(f"Vote recorded. Your Votes can keep this Conversation open {left} more time{'s' if left != 1 else ''}.")
    except (MeetinghouseError, KeyError) as exc:
        flash(str(exc) if isinstance(exc, MeetinghouseError) else "No such Conversation.")
    return redirect(url_for("TopicDetail", topic_id=topic_id))


@app.route("/topic/<topic_id>/post", methods=["POST"])
def AddPost(topic_id):
    guard = RequireLogin()
    if guard:
        return guard
    text = request.form.get("text", "")
    try:
        with LOCK:
            prev_len = len(house.event_log)
            topic = house.topics[topic_id]
            post = house.AddPost(topic_id, g.participant.id, text, Now())
            persistence.SaveTopic(conn, topic)
            FlushNewEvents(prev_len)
        if post.near_identical_to is not None:
            flash(
                f"This sounds close to what you said on {DayFilter(post.near_identical_to)}. "
                "It's posted, but it won't keep the Conversation open."
            )
        elif not post.extended:
            flash("Posted. Your Comments no longer keep this Conversation open, but everyone can read them.")
    except (MeetinghouseError, KeyError) as exc:
        flash(str(exc) if isinstance(exc, MeetinghouseError) else "No such Conversation.")
    return redirect(url_for("TopicDetail", topic_id=topic_id) + "#discussion")


@app.route("/topic/<topic_id>/question", methods=["POST"])
def AttachQuestion(topic_id):
    """Attach the one Question to a Conversation that has none. The asker is its Pollster."""
    guard = RequireLogin()
    if guard:
        return guard
    try:
        with LOCK:
            prev_len = len(house.event_log)
            topic = house.topics[topic_id]
            house.AttachQuestion(topic_id, g.participant.id, request.form.get("question", ""), Now())
            persistence.SaveTopic(conn, topic)
            FlushNewEvents(prev_len)
        flash(
            f"Your Question is attached and you are its Pollster. Everyone can Vote on it now, "
            f"and the Conversation stays open at least {topic.rules.min_life.days // 7} weeks."
        )
    except (MeetinghouseError, KeyError) as exc:
        flash(str(exc) if isinstance(exc, MeetinghouseError) else "No such Conversation.")
    return redirect(url_for("TopicDetail", topic_id=topic_id))


@app.route("/topic/<topic_id>/revoke", methods=["POST"])
def Revoke(topic_id):
    guard = RequireLogin()
    if guard:
        return guard
    try:
        with LOCK:
            prev_len = len(house.event_log)
            topic = house.topics[topic_id]
            house.RevokeVote(topic_id, g.participant.id, Now())
            persistence.SaveTopic(conn, topic)
            FlushNewEvents(prev_len)
    except MeetinghouseError as exc:
        flash(str(exc))
    return redirect(url_for("TopicDetail", topic_id=topic_id))


@app.route("/current-rules")
def CurrentRules():
    """Every setting, its current level, how far one vote may move it, and any open Conversation about it."""
    rows = []
    last_changes = persistence.LastRuleChanges(conn)
    for subsection in Subsection:
        rule = house.current_rules[subsection]
        for setting, label in SETTING_NAMES.items():
            low, high = SETTING_LIMITS[(subsection, setting)]
            rows.append(
                {
                    "subsection": subsection,
                    "label": label,
                    "current": getattr(rule, setting),
                    "low": low,
                    "high": high,
                    "step_low": AllowedRange(getattr(rule, setting), low, high)[0],
                    "step_high": AllowedRange(getattr(rule, setting), low, high)[1],
                    "open_topic": house.OpenRulesTopicFor(subsection, setting),
                    "last_changed": last_changes.get((subsection.value, setting)),
                }
            )
    return render_template("current_rules.html", rows=rows, rules=house.current_rules)


@app.route("/archive")
def Archive():
    guard = RequireLogin()
    if guard:
        return guard
    query = request.args.get("q", "").strip()
    results = house.SearchArchive(query) if query else []
    return render_template("archive.html", query=query, results=results)


@app.route("/summary")
def Summary():
    guard = RequireLogin()
    if guard:
        return guard
    summary = house.DailySummary(g.participant.id, Now())
    return render_template("summary.html", summary=summary)


@app.route("/admin/login", methods=["GET", "POST"])
def AdminLogin():
    if request.method == "POST":
        locked = security.LockedUntil(conn, "admin", Now())
        if locked:
            flash(_LockedMessage(locked))
            return render_template("admin_login.html")
        if secrets.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            security.ClearFailures(conn, "admin")
            session["is_admin"] = True
            return redirect(url_for("Admin"))
        locked = security.RecordFailure(conn, "admin", Now())
        flash(_LockedMessage(locked) if locked else "Incorrect admin password.")
    return render_template("admin_login.html")


@app.route("/admin", methods=["GET"])
def Admin():
    guard = RequireAdmin()
    if guard:
        return guard
    now = Now()
    participants = sorted(house.participants.values(), key=lambda p: p.name)
    status = {}
    for p in participants:
        if not security.IsActivated(conn, p.id):
            latest = security.LatestIdSession(conn, p.id, idcheck.JOIN)
            status[p.id] = "joining" + (f" (ID check {latest['status']})" if latest else "")
        elif not p.IsParticipant(now):
            status[p.id] = "paused"
        else:
            status[p.id] = "member"
    return render_template(
        "admin.html",
        participants=participants,
        now=now,
        rules=house.current_rules,
        status=status,
        passkeys={p.id: len(security.ListPasskeys(conn, p.id)) for p in participants},
    )


@app.route("/admin/tick", methods=["POST"])
def AdminTick():
    guard = RequireAdmin()
    if guard:
        return guard
    closed = RunTick()
    flash(f"Ran Tick(): closed {len(closed)} Conversation(s).")
    return redirect(url_for("Admin"))


@app.route("/admin/logout")
def AdminLogout():
    session.pop("is_admin", None)
    return redirect(url_for("AdminLogin"))


if __name__ == "__main__":
    if ADMIN_PASSWORD == "admin":
        app.logger.warning(
            "MEETINGHOUSE_ADMIN_PASSWORD not set; using the insecure default 'admin'. "
            "Set it before exposing this app beyond localhost."
        )
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" or not app.debug:
        StartScheduler()
    app.run(debug=os.environ.get("MEETINGHOUSE_DEBUG") == "1", host=os.environ.get("MEETINGHOUSE_HOST", "127.0.0.1"), port=int(os.environ.get("MEETINGHOUSE_PORT", "5000")))
