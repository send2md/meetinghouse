"""The server installing approved software by itself."""

from __future__ import annotations

import datetime as dt
import io
import os
import tarfile

import installer
import persistence

REF = "a" * 40


def _Tarball(files, top=f"meetinghouse-{REF}"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _Setup(tmp_path):
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "app.py").write_text("old\n")
    (app / "config.py").write_text("SECRET\n")
    conn = persistence.Connect(str(tmp_path / "log.db"))
    persistence.LogEvent(conn, {"kind": "software_change_approved", "at": dt.datetime(2027, 1, 1), "topic_id": "t", "ref": REF})
    return app, conn


def test_an_approved_version_is_tested_installed_and_logged(tmp_path):
    app, conn = _Setup(tmp_path)
    files = {"app.py": "new\n", "templates/x.html": "hi\n", "test_app.py": "tests\n", "deploy/x.sh": "no\n", "requirements.txt": ""}
    restarted = []
    results = installer.Run(conn, str(app), download=lambda ref: _Tarball(files), run_tests=lambda d: (True, "ok"),
                            restart=lambda: restarted.append(1), now=dt.datetime(2027, 1, 2))
    assert results == [(REF, "installed")]
    assert (app / "app.py").read_text() == "new\n" and (app / "templates" / "x.html").exists()
    assert not (app / "test_app.py").exists() and not (app / "deploy").exists()
    assert (app / "config.py").read_text() == "SECRET\n"
    assert (app / "VERSION").read_text().strip() == REF
    assert restarted == [1]
    assert [e["data"]["ref"] for e in persistence.LogEntriesOfKind(conn, "software_installed")] == [REF]
    # Each approved version is tried once.
    assert installer.Run(conn, str(app), download=lambda ref: 1 / 0, run_tests=lambda d: (True, ""), restart=lambda: None) == []


def test_failing_tests_change_nothing(tmp_path):
    app, conn = _Setup(tmp_path)
    results = installer.Run(conn, str(app), download=lambda ref: _Tarball({"app.py": "broken\n"}),
                            run_tests=lambda d: (False, "1 failed"), restart=lambda: None)
    assert results == [(REF, "tests failed")]
    assert (app / "app.py").read_text() == "old\n"
    (failed,) = persistence.LogEntriesOfKind(conn, "software_install_failed")
    assert "1 failed" in failed["data"]["reason"]


def test_unsafe_downloads_are_refused(tmp_path):
    app, conn = _Setup(tmp_path)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("../../evil.py")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    results = installer.Run(conn, str(app), download=lambda ref: buffer.getvalue(), run_tests=lambda d: (True, ""), restart=lambda: None)
    assert results == [(REF, "download failed")]
    assert not (tmp_path / "evil.py").exists()


def test_only_exact_versions_are_installed(tmp_path):
    app, conn = _Setup(tmp_path)
    persistence.LogEvent(conn, {"kind": "software_change_approved", "at": dt.datetime(2027, 1, 3), "ref": "main"})
    assert installer.Pending(conn) == [REF]
