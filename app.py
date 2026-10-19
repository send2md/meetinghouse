"""Web front end for the Meetinghouse engine.

This module owns everything meetinghouse.py deliberately does not:
wall-clock time, HTTP, sessions/login, and calling into persistence.py
after each mutation. The single in-process `house` is the same
Meetinghouse object the engine's tests exercise; this file just feeds
it real time and real people, and keeps SQLite in sync.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import re
import secrets
import sqlite3
import threading

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
from werkzeug.security import check_password_hash, generate_password_hash

import persistence
import security
import tick_runner
from meetinghouse import (
    RESTART_LIMIT,
    SETTING_LIMITS,
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

# Self-registration photo uploads live outside ~/html, next to
# the SQLite file, so they're never web-accessible except through the
# admin-gated AdminParticipantImage route below.
UPLOAD_DIR = os.environ.get("MEETINGHOUSE_UPLOADS", os.path.join(os.path.dirname(DB_PATH), "uploads"))
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg"}

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
    """Close every eligible Topic/Poll and persist the resulting changes."""
    with LOCK:
        return tick_runner.RunTick(house, conn, Now())


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


def _AllowedImage(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def _ImageMimeType(filename):
    """The MIME subtype for a data: URI — "jpg" isn't a registered one, "jpeg" is."""
    ext = filename.rsplit(".", 1)[1].lower()
    return "jpeg" if ext == "jpg" else ext


def _SaveUpload(file_storage, participant_id, field_name):
    """Save an uploaded photo under UPLOAD_DIR and return (path, raw bytes).

    Returning the bytes too lets the caller embed the same image inline
    (as a data: URI) on the one-time printable confirmation page, without
    a second disk read.
    """
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    data = file_storage.read()
    participant_dir = os.path.join(UPLOAD_DIR, participant_id)
    os.makedirs(participant_dir, exist_ok=True)
    dest = os.path.join(participant_dir, f"{field_name}.{ext}")
    with open(dest, "wb") as f:
        f.write(data)
    return dest, data


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
        counts = house.EffectiveVoteCounts(topic, now)
        open_by_subsection[topic.subsection].append(
            {"topic": topic, "counts": counts, "age": now - topic.created_at, "readers": len(house.EffectiveReaders(topic, now))}
        )
    for items in open_by_subsection.values():
        items.sort(key=lambda item: item["topic"].created_at)
    return render_template(
        "dashboard.html",
        open_by_subsection=open_by_subsection,
        now=now,
        activated=security.IsActivated(conn, g.participant.id),
        pending_code=security.PendingCode(conn, g.participant.id),
        has_passkey=security.HasPasskeys(conn, g.participant.id),
    )


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

    Someone still waiting for their activation letter CAN log in — they
    need to, to type in the code — they just can't take part until then.
    """
    if participant is None:
        return "That account no longer exists."
    if security.IsActivated(conn, participant.id) and not participant.IsParticipant(Now()):
        return "Your participation form has lapsed. Please send a fresh one."
    return None


def _CompleteLogin(participant, username):
    session.pop("login_username", None)
    session.pop("pending_username", None)
    session["participant_id"] = participant.id
    security.ClearFailures(conn, _UserKey(username))


def _Activate(participant):
    """Activation counts as signing the participation form, as registering used to."""
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
    """Claim `username` for a new Participant, or bounce back if it's (now) taken."""
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
    """The self-service "you are a new Participant" form: no admin involved.

    Collects everything the printed, mailed-in form needs (see
    register_complete.html): a name, a password the Participant chooses,
    and the identity details a Quaker Meeting wants on that paper form
    (address, age) plus a photo, uploaded here and printed back out on
    the confirmation page so the same image goes in the envelope. The
    address matters most: the activation letter is mailed there.
    """
    username = session.get("pending_username")
    if not username:
        flash("Start from the login page to register.")
        return redirect(url_for("Login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        address = request.form.get("address", "").strip()
        age_raw = request.form.get("age", "").strip()
        photo = request.files.get("photo")

        errors = []
        if not name:
            errors.append("Whole real name is required.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if not address:
            errors.append("Address is required.")
        if not age_raw.isdigit():
            errors.append("Age must be a whole number.")
        if not photo or not photo.filename or not _AllowedImage(photo.filename):
            errors.append("A photo (jpg or png) is required.")
        if errors:
            for error in errors:
                flash(error)
            return render_template("register.html", username=username)

        # The availability check and the insert both happen under LOCK, so a
        # second request racing on the same username can't slip past the
        # check between here and SetCredentials; the UNIQUE constraint is
        # still a backstop in case this ever runs with more than one worker.
        with LOCK:
            if persistence.GetCredentialsByUsername(conn, username) is not None:
                flash("That login was just taken. Please choose another.")
                return render_template(
                    "username_taken.html", taken_username=username, suggestion=_SuggestUsername(username)
                )
            participant = house.RegisterParticipant(name)
            # No form is signed yet: that happens when they type in the code
            # from the activation letter mailed to the address below.
            persistence.SaveParticipant(conn, participant)
            security.CreatePendingStatus(conn, participant.id)
            try:
                persistence.SetCredentials(
                    conn, participant.id, username, generate_password_hash(password, method="pbkdf2:sha256")
                )
            except sqlite3.IntegrityError:
                flash("That login was just taken. Please choose another.")
                return render_template(
                    "username_taken.html", taken_username=username, suggestion=_SuggestUsername(username)
                )
            photo_path, photo_bytes = _SaveUpload(photo, participant.id, "photo")
            persistence.SaveParticipantProfile(
                conn, participant.id, address, int(age_raw), photo_path, Now()
            )

        session.pop("pending_username", None)
        session["participant_id"] = participant.id
        return render_template(
            "register_complete.html",
            participant=participant,
            username=username,
            address=address,
            age=age_raw,
            photo_data_uri=f"data:image/{_ImageMimeType(photo.filename)};base64,"
            + base64.b64encode(photo_bytes).decode("ascii"),
        )

    return render_template("register.html", username=username)


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


@app.route("/login/code", methods=["GET", "POST"])
def LoginCode():
    """Log in (and activate, or recover) with the one-time code from a letter."""
    username = session.get("login_username")
    if not username:
        return redirect(url_for("Login"))
    row = persistence.GetCredentialsByUsername(conn, username)
    if row is None:
        return redirect(url_for("Login"))

    if request.method == "POST":
        key = _UserKey(username)
        locked = security.LockedUntil(conn, key, Now())
        if locked:
            flash(_LockedMessage(locked))
            return render_template("login_code.html", username=username)
        participant = house.participants.get(row["participant_id"])
        purpose = security.RedeemCode(conn, row["participant_id"], request.form.get("code", ""), Now())
        if purpose is None or participant is None:
            locked = security.RecordFailure(conn, key, Now())
            flash(_LockedMessage(locked) if locked else "That code didn't match. Check the letter and try again.")
            return render_template("login_code.html", username=username)
        if purpose == security.ACTIVATION and not security.IsActivated(conn, participant.id):
            _Activate(participant)
        reason = _LoginBlockedReason(participant)
        if reason:
            flash(reason)
            return render_template("login_code.html", username=username)
        _CompleteLogin(participant, username)
        if purpose == security.ACTIVATION:
            flash(f"Welcome, {participant.name}. Your account is activated.")
        else:
            flash(f"Welcome back, {participant.name}.")
        flash("Next, add a passkey below so that only you can log in from now on.")
        return redirect(url_for("Account"))

    return render_template("login_code.html", username=username)


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
    return render_template(
        "account.html",
        username=creds["username"] if creds else "",
        activated=security.IsActivated(conn, participant_id),
        pending_code=security.PendingCode(conn, participant_id),
        passkeys=security.ListPasskeys(conn, participant_id),
        has_password=bool(creds) and creds["password_hash"] != security.NO_PASSWORD,
    )


@app.route("/account/activate", methods=["POST"])
def AccountActivate():
    guard = RequireLogin()
    if guard:
        return guard
    creds = persistence.GetCredentialsForParticipant(conn, g.participant.id)
    key = _UserKey(creds["username"])
    locked = security.LockedUntil(conn, key, Now())
    if locked:
        flash(_LockedMessage(locked))
        return redirect(request.referrer or url_for("Account"))
    purpose = security.RedeemCode(conn, g.participant.id, request.form.get("code", ""), Now())
    if purpose is None:
        locked = security.RecordFailure(conn, key, Now())
        flash(_LockedMessage(locked) if locked else "That code didn't match. Check the letter and try again.")
        return redirect(request.referrer or url_for("Account"))
    security.ClearFailures(conn, key)
    if not security.IsActivated(conn, g.participant.id):
        _Activate(g.participant)
    flash("Your account is activated. Next, add a passkey so that only you can log in.")
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
    flash(f"Passkey “{label}” added. From now on you log in with it; your password no longer works.")
    return jsonify(ok=True, redirect=url_for("Account"))


@app.route("/account/passkey/remove", methods=["POST"])
def PasskeyRemove():
    guard = RequireLogin()
    if guard:
        return guard
    security.RemovePasskey(conn, g.participant.id, request.form.get("credential_id", ""))
    if security.HasPasskeys(conn, g.participant.id):
        flash("Passkey removed.")
    else:
        flash("Passkey removed. You have no passkeys left, so your password logs you in again.")
    return redirect(url_for("Account"))


@app.route("/account/password", methods=["POST"])
def AccountPassword():
    """Set a new password — only meaningful while the account has no passkey."""
    guard = RequireLogin()
    if guard:
        return guard
    if security.HasPasskeys(conn, g.participant.id):
        flash("You log in with a passkey, so there's no password to change.")
        return redirect(url_for("Account"))
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("Password must be at least 8 characters.")
        return redirect(url_for("Account"))
    if password != request.form.get("confirm", ""):
        flash("The two passwords didn't match.")
        return redirect(url_for("Account"))
    creds = persistence.GetCredentialsForParticipant(conn, g.participant.id)
    persistence.SetCredentials(
        conn, g.participant.id, creds["username"], generate_password_hash(password, method="pbkdf2:sha256")
    )
    flash("New password saved.")
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
                title = None  # the engine names it, e.g. "Change Forum approval from 66.7% to 70%"
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
    """Every changeable setting, its current value, and how far one Topic may move it."""
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
                    "low": low,
                    "high": high,
                    "step_low": max(low, current - 0.05),
                    "step_high": min(high, current + 0.05),
                }
            )
    return choices


def _ParseRuleChange(form):
    """Read "which setting" and "new value (%)" from the form into a RuleChange."""
    key = form.get("setting", "")
    try:
        subsection_name, setting = key.split(":", 1)
        subsection = Subsection(subsection_name)
    except ValueError:
        return None, "Choose which setting this Topic changes."
    if setting not in SETTING_NAMES:
        return None, "Choose which setting this Topic changes."
    raw = form.get("value", "").strip().rstrip("%")
    try:
        value = round(float(raw) / 100, 4)
    except ValueError:
        return None, "Give the new value as a percentage, for example 70."
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

    first_visit = False
    if g.participant.IsActive(now) and not topic.closed:
        with LOCK:
            try:
                first_visit = house.MarkRead(topic_id, g.participant.id, now)
                if first_visit:
                    persistence.SaveTopic(conn, topic)
            except MeetinghouseError:
                pass

    # Votes (with their comments) and discussion posts, in the order they happened.
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
        readers=house.EffectiveReaders(topic, now),
        now=now,
        can_act=can_act,
        ask_to_vote=first_visit and topic.HasPoll() and my_vote is None and can_act,
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
            flash("Vote recorded. It counts, but it no longer keeps the Topic open.")
        elif left == 0:
            flash("Vote recorded. That was the last of your votes that can keep this Topic open.")
        else:
            flash(f"Vote recorded. Your votes can keep this Topic open {left} more time{'s' if left != 1 else ''}.")
    except (MeetinghouseError, KeyError) as exc:
        flash(str(exc) if isinstance(exc, MeetinghouseError) else "No such Topic.")
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
                "It's posted, but it won't keep the Topic open."
            )
        elif not post.extended:
            flash("Posted. Your posts no longer keep this Topic open, but everyone can read it.")
    except (MeetinghouseError, KeyError) as exc:
        flash(str(exc) if isinstance(exc, MeetinghouseError) else "No such Topic.")
    return redirect(url_for("TopicDetail", topic_id=topic_id) + "#discussion")


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
    """Every setting, its current level, its fixed limits, and any open Topic about it."""
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
    login_info = {
        p.id: {
            "activated": security.IsActivated(conn, p.id),
            "pending_code": security.PendingCode(conn, p.id),
            "passkeys": len(security.ListPasskeys(conn, p.id)),
        }
        for p in participants
    }
    waiting = [p for p in participants if not login_info[p.id]["activated"]]
    addresses = {}
    for p in waiting:
        profile = persistence.GetParticipantProfile(conn, p.id)
        addresses[p.id] = profile["address"] if profile else ""
    return render_template(
        "admin.html",
        participants=participants,
        now=now,
        rules=house.current_rules,
        login_info=login_info,
        waiting=waiting,
        addresses=addresses,
    )


@app.route("/admin/participant/<participant_id>")
def AdminParticipantProfile(participant_id):
    """The self-registration details for one Participant, for manual cross-checking.

    Compare these against whatever arrives in the mail (see Register in
    this module and register_complete.html) — the admin never had to
    type in a password to get here, and still can't see one.
    """
    guard = RequireAdmin()
    if guard:
        return guard
    participant = house.participants.get(participant_id)
    if participant is None:
        abort(404)
    profile = persistence.GetParticipantProfile(conn, participant_id)
    creds = persistence.GetCredentialsForParticipant(conn, participant_id)
    return render_template(
        "admin_participant.html",
        participant=participant,
        profile=profile,
        username=creds["username"] if creds else "",
        activated=security.IsActivated(conn, participant_id),
        pending_code=security.PendingCode(conn, participant_id),
        passkeys=security.ListPasskeys(conn, participant_id),
    )


@app.route("/admin/participant/<participant_id>/letter", methods=["POST"])
def AdminLetter(participant_id):
    """Make a one-time code and show the printable letter that carries it.

    purpose=activation: for someone who registered but isn't activated yet.
    purpose=recovery: resets their login first — removes their passkeys and
    turns off their password — so only whoever holds the letter gets back in.
    The code is shown only on this page; printing a new letter replaces it.
    """
    guard = RequireAdmin()
    if guard:
        return guard
    participant = house.participants.get(participant_id)
    creds = persistence.GetCredentialsForParticipant(conn, participant_id)
    if participant is None or creds is None:
        abort(404)
    purpose = request.form.get("purpose")
    if purpose not in (security.ACTIVATION, security.RECOVERY):
        abort(400)
    if purpose == security.ACTIVATION and security.IsActivated(conn, participant_id):
        flash(f"{participant.name} is already activated.")
        return redirect(url_for("AdminParticipantProfile", participant_id=participant_id))
    if purpose == security.RECOVERY:
        security.RemoveAllPasskeys(conn, participant_id)
        persistence.SetCredentials(conn, participant_id, creds["username"], security.NO_PASSWORD)
        security.ClearFailures(conn, _UserKey(creds["username"]))
    code = security.IssueCode(conn, participant_id, purpose, Now())
    profile = persistence.GetParticipantProfile(conn, participant_id)
    return render_template(
        "letter.html",
        participant=participant,
        username=creds["username"],
        address=profile["address"] if profile else "",
        purpose=purpose,
        code=security.FormatCode(code),
        expires=security.PendingCode(conn, participant_id)["expires_at"],
        today=Now().date(),
        site=_SiteAddress(),
    )


@app.route("/admin/participant/<participant_id>/activate", methods=["POST"])
def AdminActivate(participant_id):
    """Activate without a letter, for someone the admin has verified in person."""
    guard = RequireAdmin()
    if guard:
        return guard
    participant = house.participants.get(participant_id)
    if participant is None:
        abort(404)
    if not security.IsActivated(conn, participant_id):
        _Activate(participant)
    flash(f"{participant.name} is activated.")
    return redirect(url_for("Admin"))


@app.route("/admin/participant/<participant_id>/<field>")
def AdminParticipantImage(participant_id, field):
    guard = RequireAdmin()
    if guard:
        return guard
    if field != "photo":
        abort(404)
    profile = persistence.GetParticipantProfile(conn, participant_id)
    if profile is None:
        abort(404)
    path = profile["photo_path"]
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path)


@app.route("/admin/sign_form", methods=["POST"])
def AdminSignForm():
    guard = RequireAdmin()
    if guard:
        return guard
    participant_id = request.form.get("participant_id")
    scan_ref = request.form.get("scan_ref", "").strip()
    participant = house.participants.get(participant_id)
    if participant is None:
        flash("No such Participant.")
        return redirect(url_for("Admin"))
    if not security.IsActivated(conn, participant_id):
        flash(f"{participant.name} isn't activated yet. Mail them an activation letter, or activate them in person, first.")
        return redirect(url_for("Admin"))
    with LOCK:
        participant.SignForm(signed_date=Now().date(), scan_ref=scan_ref)
        persistence.SaveParticipant(conn, participant)
    flash(f"Recorded a fresh signed form for {participant.name}.")
    return redirect(url_for("Admin"))


@app.route("/admin/tick", methods=["POST"])
def AdminTick():
    guard = RequireAdmin()
    if guard:
        return guard
    closed = RunTick()
    flash(f"Ran Tick(): closed {len(closed)} Topic(s).")
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
