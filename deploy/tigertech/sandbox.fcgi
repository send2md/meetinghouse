#!/home/USERNAME/meetinghouse/venv/bin/python3
"""FastCGI entry point for the practice sandbox, sandbox.meetinghouse.ai.

It runs the same code as the live site (~/meetinghouse) but reads its
settings from ~/meetinghouse-sandbox/config.py, which points at its own
pretend database and turns on MEETINGHOUSE_SANDBOX. On TigerTech the
folder ~/html/sandbox/ is served as sandbox.meetinghouse.ai; this file
goes there as index.fcgi, with the same .htaccess as the main site.
"""

import os
import sys

APP_DIR = os.path.expanduser("~/meetinghouse")
SANDBOX_DIR = os.path.expanduser("~/meetinghouse-sandbox")
sys.path.insert(0, APP_DIR)
sys.path.insert(0, SANDBOX_DIR)  # first, so `import config` finds the sandbox's settings

import config  # noqa: E402  (sets os.environ for the sandbox)

assert os.environ.get("MEETINGHOUSE_SANDBOX") == "1", "sandbox.fcgi must load the sandbox's config.py"

from app import app as flask_app  # noqa: E402
from flup.server.fcgi import WSGIServer  # noqa: E402


class ScriptNameStripper:
    """Flask needs SCRIPT_NAME empty for url_for() to build correct paths."""

    def __init__(self, wrapped):
        self.wrapped = wrapped

    def __call__(self, environ, start_response):
        environ["SCRIPT_NAME"] = ""
        return self.wrapped(environ, start_response)


if __name__ == "__main__":
    WSGIServer(ScriptNameStripper(flask_app)).run()
