"""Template for the private config TigerTech's index.fcgi and tick.py load.

Copy this to ~/meetinghouse/config.py (i.e. next to app.py, OUTSIDE
~/html so it's never web-accessible) and fill in real values. It's
imported for its side effect of populating os.environ before app.py
is imported, so app.py itself needs no host-specific changes.

Do not commit the filled-in config.py to git.
"""

import os

# Outside ~/html — the SQLite file must never be reachable over HTTP.
os.environ["MEETINGHOUSE_DB"] = "/home/USERNAME/meetinghouse/data/meetinghouse.db"

# Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
os.environ["MEETINGHOUSE_SECRET_KEY"] = "REPLACE-ME"

# A password for /admin, separate from any Participant's login.
os.environ["MEETINGHOUSE_ADMIN_PASSWORD"] = "REPLACE-ME"

# Passkeys only work on the exact web address they were made on, so tell
# the app the live site's address rather than letting it guess.
os.environ["MEETINGHOUSE_RP_ID"] = "meetinghouse.ai"
os.environ["MEETINGHOUSE_ORIGINS"] = "https://meetinghouse.ai,https://www.meetinghouse.ai"

# The live site is HTTPS: never send the login cookie over plain HTTP.
os.environ["MEETINGHOUSE_SECURE_COOKIES"] = "1"


# --- ID checks ---------------------------------------------------------------
# The approved ID-check companies Members may choose from, separated by
# commas. Leave empty until the keys below are filled in: joining is paused
# until then. NEVER list "practice" here on the live site: it approves anyone.
os.environ["MEETINGHOUSE_IDCHECK"] = ""   # e.g. "didit"

# Didit (business.didit.me): API & Webhooks, and Workflows.
os.environ["MEETINGHOUSE_DIDIT_API_KEY"] = ""
os.environ["MEETINGHOUSE_DIDIT_WEBHOOK_SECRET"] = ""
os.environ["MEETINGHOUSE_DIDIT_WORKFLOW_JOIN"] = ""      # ID document + live selfie + face search + proof of address
os.environ["MEETINGHOUSE_DIDIT_WORKFLOW_RENEWAL"] = ""   # live selfie matched to the stored face

# --- Email (welcome, yearly reminders, login recovery) ------------------------
# Set MEETINGHOUSE_MAIL to "smtp" once the server details below are filled in.
os.environ["MEETINGHOUSE_MAIL"] = ""
os.environ["MEETINGHOUSE_SMTP_HOST"] = ""
os.environ["MEETINGHOUSE_SMTP_PORT"] = "587"
os.environ["MEETINGHOUSE_SMTP_USER"] = ""
os.environ["MEETINGHOUSE_SMTP_PASSWORD"] = ""
os.environ["MEETINGHOUSE_MAIL_FROM"] = "Meetinghouse <hello@meetinghouse.ai>"
