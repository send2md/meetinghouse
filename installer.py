#!/usr/bin/env python3
"""The server installs approved software by itself.

A software change goes live only when a Rules Vote passes on it. Each such
Rules Conversation names an exact version of the code on GitHub (its
40-character commit id). When one passes, the site writes
"software_change_approved" in the public log. This script, run by cron on the
server, then:

1. downloads exactly that version from GitHub,
2. runs all its tests on the server; if any fail, nothing changes and the
   failure goes in the public log,
3. installs it in place of the running code, keeping the data, config.py and
   the Members' photos, and restarts the site,
4. writes "software_installed" in the public log; the site then logs the new
   code version as it starts.

Nothing else is ever installed this way, and each approved version is tried
once. Run it with the app's own Python:

    venv/bin/python3 installer.py
"""

from __future__ import annotations

import fcntl
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import config  # noqa: F401  (sets os.environ on the server)
except ImportError:
    pass

import persistence
from meetinghouse import UtcNow

REPO = os.environ.get("MEETINGHOUSE_REPO", "send2md/meetinghouse")
DB_PATH = os.environ.get("MEETINGHOUSE_DB", os.path.join(HERE, "meetinghouse.db"))
# Files that are the code. Everything else in the app folder (data, config.py,
# venv, uploads, VERSION) is left alone; tests and deploy scripts aren't installed.
NOT_INSTALLED = re.compile(r"^(test_.*\.py|conftest\.py|testkit\.py|deploy(_local)?/.*|\.github/.*|\.gitignore|README.*)$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def Download(ref):
    """The exact version `ref` from GitHub, as a .tar.gz (bytes)."""
    url = f"https://codeload.github.com/{REPO}/tar.gz/{ref}"
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def Unpack(tarball, folder):
    """Unpack into `folder`, refusing anything that would land outside it. Returns the code's own folder."""
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        for member in archive.getmembers():
            target = os.path.realpath(os.path.join(folder, member.name))
            if not target.startswith(os.path.realpath(folder) + os.sep) or member.issym() or member.islnk():
                raise ValueError(f"unsafe path in the download: {member.name}")
        if hasattr(tarfile, "data_filter"):
            archive.extractall(folder, filter="data")
        else:
            archive.extractall(folder)
    (top,) = [d for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d))]
    return os.path.join(folder, top)


def TestPython():
    """A Python that has pytest: the deploy's test folder, or the app's own."""
    for candidate in (
        os.path.join(os.path.dirname(HERE), "meetinghouse-check", "venv", "bin", "python3"),
        os.path.join(HERE, "venv", "bin", "python3"),
        sys.executable,
    ):
        if os.path.exists(candidate) and subprocess.run([candidate, "-c", "import pytest"], capture_output=True).returncode == 0:
            return candidate
    return None


def RunTests(code_dir):
    """(passed, last lines of the output)."""
    python = TestPython()
    if python is None:
        return False, "No Python with pytest on the server to test it with."
    subprocess.run([python, "-m", "pip", "install", "-q", "-r", os.path.join(code_dir, "requirements.txt")], capture_output=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for key in ("MEETINGHOUSE_DB", "MEETINGHOUSE_UPLOADS", "MEETINGHOUSE_GEO", "MEETINGHOUSE_MODELS"):
        env.pop(key, None)  # the tests make their own, never touching the live data
    run = subprocess.run(
        [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=code_dir, env=env, capture_output=True, text=True, timeout=1800,
    )
    tail = "\n".join((run.stdout + run.stderr).strip().splitlines()[-5:])
    return run.returncode == 0, tail


def Install(code_dir, app_dir, ref):
    """Copy the new code over the running code, then write VERSION."""
    for root, dirs, files in os.walk(code_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        for name in files:
            source = os.path.join(root, name)
            relative = os.path.relpath(source, code_dir).replace(os.sep, "/")
            if NOT_INSTALLED.match(relative) or name.endswith(".pyc"):
                continue
            target = os.path.join(app_dir, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target + ".new")
            os.replace(target + ".new", target)
    pip = os.path.join(app_dir, "venv", "bin", "pip")
    if os.path.exists(pip):
        subprocess.run([pip, "install", "-q", "-r", os.path.join(app_dir, "requirements.txt")], capture_output=True)
    with open(os.path.join(app_dir, "VERSION"), "w") as f:
        f.write(ref + "\n")


def Restart():
    """TigerTech restarts the site when its FastCGI script changes."""
    fcgi = os.path.join(os.path.dirname(HERE), "html", "index.fcgi")
    if os.path.exists(fcgi):
        os.utime(fcgi)


def Pending(conn):
    """Approved versions not yet tried, oldest first."""
    tried = {e["data"].get("ref") for kind in ("software_installed", "software_install_failed") for e in persistence.LogEntriesOfKind(conn, kind)}
    pending = []
    for entry in persistence.LogEntriesOfKind(conn, "software_change_approved"):
        ref = (entry["data"].get("ref") or "").lower()
        if SHA.match(ref) and ref not in tried and ref not in pending:
            pending.append(ref)
    return pending


def Run(conn, app_dir=HERE, download=Download, run_tests=RunTests, restart=Restart, now=None):
    """Install each approved version not yet tried. Returns [(ref, outcome)]."""
    results = []
    for ref in Pending(conn):
        when = now or UtcNow()
        with tempfile.TemporaryDirectory() as work:
            try:
                code_dir = Unpack(download(ref), work)
            except Exception as exc:
                persistence.LogEvent(conn, {"kind": "software_install_failed", "at": when, "ref": ref, "reason": f"Couldn't download it: {exc}"})
                results.append((ref, "download failed"))
                continue
            passed, tail = run_tests(code_dir)
            if not passed:
                persistence.LogEvent(conn, {"kind": "software_install_failed", "at": when, "ref": ref, "reason": "Its tests failed on the server: " + tail})
                results.append((ref, "tests failed"))
                continue
            Install(code_dir, app_dir, ref)
        persistence.LogEvent(conn, {"kind": "software_installed", "at": when, "ref": ref})
        restart()
        results.append((ref, "installed"))
    return results


def main():
    lock_path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "installer.lock")
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another run is still going
        conn = persistence.Connect(DB_PATH)
        for ref, outcome in Run(conn):
            print(f"{ref}: {outcome}")


if __name__ == "__main__":
    main()
