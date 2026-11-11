"""Tests for logging in and joining: user names, the join form, the front page.

Each test gets a fresh copy of the app on its own database (see conftest.py).
"""

from __future__ import annotations

import importlib

from testkit import PASSWORD, LegacyMember, PasswordLogin, Pid, Register


def test_unknown_username_goes_straight_to_joining(client):
    test_client, _ = client
    resp = test_client.post("/login", data={"username": "janedoe"}, follow_redirects=True)
    assert b"Join Meetinghouse as janedoe" in resp.data


def test_join_form_asks_for_name_email_town_zip_and_agreement(client):
    test_client, app_module = client
    test_client.post("/login", data={"username": "janedoe"})
    form = test_client.get("/register").data
    for field in (b'name="name"', b'name="email"', b'name="town"', b'name="zip"', b'name="country"', b'name="agree"'):
        assert field in form
    assert b'name="password"' not in form  # passkeys only, never a password
    for word in (b"thumbprint", b"maiden"):
        assert word not in form.lower()

    resp = test_client.post("/register", data={"name": "", "email": "nope", "town": "", "zip": ""}, follow_redirects=True)
    text = resp.data.decode()
    assert "Your whole real name is required." in text
    assert "A working email address is required" in text
    assert "Your town is required." in text
    assert "Your postal code is required." in text
    assert "Your country is required." in text
    assert "tick the box" in text
    assert not app_module.house.participants


def test_joining_step_one_creates_the_account_and_goes_to_finish_joining(client):
    test_client, app_module = client
    resp = Register(test_client, "janedoe")
    assert b"Finish joining" in resp.data
    pid = Pid(app_module)
    member = app_module.security.GetMember(app_module.conn, pid)
    assert (member["email"], member["town"], member["zip"], member["country"]) == (
        "janedoe@example.com", "Ithaca", "14850", "United States")
    creds = app_module.persistence.GetCredentialsByUsername(app_module.conn, "janedoe")
    assert creds["password_hash"] == app_module.security.NO_PASSWORD


def test_legacy_password_login_is_two_separate_steps(client):
    test_client, app_module = client
    LegacyMember(app_module, "olduser")
    username_page = test_client.get("/login")
    assert b"Meetinghouse User Name" in username_page.data
    assert b'name="password"' not in username_page.data
    step1 = test_client.post("/login", data={"username": "olduser"})
    assert step1.status_code == 302 and step1.headers["Location"].endswith("/login/password")
    assert b'name="password"' in test_client.get("/login/password").data
    assert b"Welcome, Old User" in PasswordLogin(test_client, "olduser", PASSWORD).data


def test_wrong_password_offers_retry_or_new_login(client):
    test_client, app_module = client
    LegacyMember(app_module, "olduser")
    resp = PasswordLogin(test_client, "olduser", "nope")
    assert b"that password didn" in resp.data.lower()
    assert b"I made a mistake, try again" in resp.data
    assert b"This is a new login" in resp.data
    retry = test_client.post("/login/password", data={"password": PASSWORD}, follow_redirects=True)
    assert b"Welcome, Old User" in retry.data


def test_claiming_a_taken_username_offers_a_free_alternative(client):
    test_client, _ = client
    Register(test_client, "janedoe")
    test_client.get("/logout")
    resp = test_client.post("/login/claim", data={"username": "janedoe"}, follow_redirects=True)
    assert b"&#34;janedoe&#34; is taken" in resp.data or b'"janedoe" is taken' in resp.data
    assert b"janedoe2" in resp.data
    start = test_client.post("/register/start", data={"username": "janedoe2"}, follow_redirects=True)
    assert b"Join Meetinghouse as janedoe2" in start.data


def test_register_start_loops_back_if_the_alternative_is_also_taken(client):
    test_client, _ = client
    Register(test_client, "janedoe")
    test_client.get("/logout")
    Register(test_client, "janedoe2", name="Jane Two")
    test_client.get("/logout")
    resp = test_client.post("/register/start", data={"username": "janedoe2"}, follow_redirects=True)
    assert b"janedoe3" in resp.data


def test_admin_registration_and_activation_routes_are_gone(client):
    test_client, app_module = client
    Register(test_client, "janedoe")
    pid = Pid(app_module)
    test_client.get("/logout")
    test_client.post("/admin/login", data={"password": "test-admin-password"})
    assert test_client.post("/admin/register", data={"name": "Someone"}).status_code == 404
    assert test_client.post(f"/admin/participant/{pid}/letter").status_code == 404
    assert test_client.post(f"/admin/participant/{pid}/activate").status_code == 404


def test_front_page_is_a_plain_login_for_a_logged_out_visitor(client):
    test_client, _ = client
    resp = test_client.get("/")
    assert resp.status_code == 200
    assert b'<form method="post" action="/login">' in resp.data
    assert b"Please log in first" not in resp.data


def test_front_page_is_the_dashboard_once_logged_in(client):
    test_client, _ = client
    Register(test_client, "janedoe")
    resp = test_client.get("/")
    assert b"Forum Committee" in resp.data
    assert b"haven't finished joining" in resp.data


def test_previously_collected_thumbprints_and_maiden_names_are_erased(client):
    """Data collected under the old form is deleted the next time the app starts."""
    test_client, app_module = client
    pid = LegacyMember(app_module)
    folder = app_module.os.path.join(app_module.UPLOAD_DIR, pid)
    app_module.os.makedirs(folder, exist_ok=True)
    old_thumb = app_module.os.path.join(folder, "thumbprint.jpg")
    with open(old_thumb, "wb") as f:
        f.write(b"old thumbprint")
    app_module.persistence.SaveParticipantProfile(app_module.conn, pid, "123 Main St", 30, "", app_module.Now())
    app_module.conn.execute(
        "UPDATE participant_profiles SET mothers_maiden_name = 'Smith', thumbprint_path = ? WHERE participant_id = ?",
        (old_thumb, pid),
    )
    app_module.conn.commit()

    importlib.reload(app_module)
    row = app_module.persistence.GetParticipantProfile(app_module.conn, pid)
    assert row["mothers_maiden_name"] == "" and row["thumbprint_path"] == ""
    assert not app_module.os.path.exists(old_thumb)
    assert row["address"] == "123 Main St"


def test_sandbox_step_in_only_works_in_the_sandbox(client, monkeypatch):
    test_client, app_module = client
    LegacyMember(app_module, "olduser", "Old User")
    assert test_client.post("/sandbox/as/olduser").status_code == 404
    assert b"Try it as a pretend Member" not in test_client.get("/login").data

    monkeypatch.setenv("MEETINGHOUSE_SANDBOX", "1")
    page = test_client.get("/login").data
    assert b"Try it as a pretend Member" in page and b"Old User" in page
    resp = test_client.post("/sandbox/as/olduser", follow_redirects=True)
    assert b"You are now Old User, a pretend Member" in resp.data
    assert b"practice Meetinghouse" in resp.data
    assert test_client.post("/sandbox/as/nobody").status_code == 404


def test_sandbox_seed_fills_an_empty_database_only(tmp_path):
    import sandbox

    people = sandbox.Seed(str(tmp_path / "s.db"), str(tmp_path / "u"))
    assert len(people) == 8
    import persistence

    house = persistence.LoadHouse(persistence.Connect(str(tmp_path / "s.db")))
    titles = {t.title for t in house.topics.values()}
    assert "Changing the clocks twice a year" in titles and "Vaccines" in titles
    assert any(t.closed for t in house.topics.values())
    assert any(not t.HasPoll() for t in house.topics.values())
    try:
        sandbox.Seed(str(tmp_path / "s.db"), str(tmp_path / "u"))
        assert False, "should refuse a database that already has Members"
    except SystemExit:
        pass


def test_stored_seasoning_time_follows_the_fixed_value(tmp_path):
    import datetime as dt

    import persistence
    from meetinghouse import RuleConfig, Subsection

    conn = persistence.Connect(str(tmp_path / "old.db"))
    old = RuleConfig(quiet_time=dt.timedelta(weeks=1), min_life=dt.timedelta(weeks=3), voter_fraction=0.25, approval_fraction=0.63)
    persistence.SaveRules(conn, Subsection.FORUM, old)
    house = persistence.LoadHouse(conn)
    assert house.current_rules[Subsection.FORUM].quiet_time == dt.timedelta(weeks=3)
    assert house.current_rules[Subsection.FORUM].approval_fraction == 0.63  # voted settings are kept
    reloaded = persistence.LoadHouse(persistence.Connect(str(tmp_path / "old.db")))
    assert reloaded.current_rules[Subsection.FORUM].quiet_time == dt.timedelta(weeks=3)


def test_front_page_links_to_the_note_about_the_cover_art(client):
    test_client, _ = client
    front = test_client.get("/").data.decode()
    assert 'href="/about-the-art"' in front and "Margaret H. Favour" in front
    page = test_client.get("/about-the-art").data.decode()
    assert "About Meetinghouse and the cover art" in page and "The School of" in page
    assert "eVote" not in page


def test_design_page_is_public_and_linked_from_the_front_page(client):
    test_client, _ = client
    front = test_client.get("/").data.decode()
    assert 'href="/design"' in front
    page = test_client.get("/design")
    assert page.status_code == 200
    text = page.data.decode()
    assert "Meetinghouse Design" in text and "Stewards and the treasury" in text
    assert "A Question passes only after its Seasoning Time" in text


def test_tutorial_is_public_and_linked_from_the_front_page(client):
    test_client, _ = client
    assert 'href="/tutorial"' in test_client.get("/").data.decode()
    page = test_client.get("/tutorial")
    assert page.status_code == 200
    text = page.data.decode()
    assert "How to use Meetinghouse" in text and "tutorial/votes.png" in text
    assert test_client.get("/static/tutorial/votes.png").status_code == 200


def test_always_the_welcome_page_never_a_plain_log_in(client):
    test_client, _ = client
    for url in ("/", "/", "/login"):
        page = test_client.get(url).data.decode()
        assert "Just look around?" in page and "How to use Meetinghouse" in page and "How Meetinghouse works" in page
    page = test_client.post("/login", data={"username": ""}).data.decode()
    assert "Just look around?" in page
