"""
Settings and secrets.

Everything sensitive lives in a file called .env next to this one. That
file is listed in .gitignore and its contents are never printed, not even
when something goes wrong.
"""

import datetime
import os
import sys

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 and older
    sys.exit("This needs Python 3.9 or newer.")

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
OUT = os.path.join(HERE, "output")
OUT_IMAGES = os.path.join(OUT, "images")
OUT_JPEG = os.path.join(OUT, "images-jpeg")
STATE_PATH = os.path.join(HERE, "published.json")
IMAGE_HOST_DIR = os.path.join(HERE, ".imagehost")

# Posting time. This has to match what the calendar says, or the graphics
# and the schedule drift apart.
POST_HOUR = 8
POST_MINUTE = 0

try:
    TZ = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 - a bare machine with no timezone database
    sys.exit(
        "This computer has no timezone database, so post times cannot be\n"
        "worked out reliably. On a server, install it with:\n"
        "    pip install tzdata\n"
    )

# Facebook holds a scheduled post for you, but not indefinitely, and not if
# you aim too close to now.
FB_MIN_LEAD_MINUTES = 10
FB_MAX_LEAD_DAYS = 30

# Instagram limits, checked before anything is sent.
IG_MAX_CAPTION = 2200
IG_MAX_BYTES = 8 * 1024 * 1024

DEFAULT_GRAPH_VERSION = "v25.0"


def _read_env_file():
    values = {}
    if not os.path.exists(ENV_PATH):
        return values
    with open(ENV_PATH, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            # Tolerate quotes, since it is easy to paste a token with them.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


_ENV = _read_env_file()


def reload():
    """
    Re-read .env.

    The file is read once when this module is imported. Anything that
    writes a new value into it during a run must call this, or the rest
    of that run carries on seeing the old contents.
    """
    global _ENV
    _ENV = _read_env_file()
    return _ENV


def get(name, default=None):
    """Real environment wins, so launchd can override without editing .env."""
    return os.environ.get(name) or _ENV.get(name) or default


def require(name, what):
    value = get(name)
    if not value:
        sys.exit(
            "\n%s is not set.\n\n"
            "Open the file called .env in this folder and add a line:\n"
            "    %s=...\n\n"
            "%s\n"
            "The README section \"Getting your token\" walks through it.\n"
            % (name, name, what)
        )
    return value


# ----------------------------------------------------------------------
# secret hygiene
# ----------------------------------------------------------------------

def secrets():
    """Every value that must never reach the screen or a log file."""
    out = []
    for key in ("PAGE_ACCESS_TOKEN", "FB_PAGE_TOKEN", "META_APP_SECRET",
                "GITHUB_TOKEN"):
        value = get(key)
        if value and len(value) > 6:
            out.append(value)
    return out


def redact(text):
    """
    Scrub secrets out of anything on its way to the terminal.

    Meta likes to echo the access token back inside error messages and
    URLs, so this runs over every message before it is printed.
    """
    text = str(text)
    for secret in secrets():
        text = text.replace(secret, "[hidden]")
    return text


def safe_print(*parts):
    print(" ".join(redact(p) for p in parts))


# ----------------------------------------------------------------------
# scheduling
# ----------------------------------------------------------------------

def scheduled_dt(date_str):
    """The exact moment a post should go out, as a timezone-aware datetime."""
    d = datetime.date.fromisoformat(date_str)
    return datetime.datetime(
        d.year, d.month, d.day, POST_HOUR, POST_MINUTE, tzinfo=TZ
    )


def now():
    return datetime.datetime.now(tz=TZ)


def page_token():
    """
    The token used to post to the Page.

    Facebook will not accept a User token for publishing to a Page; it
    wants a Page token, which is derived from the User token. FB_PAGE_TOKEN
    holds that. The User token is kept because it is what derives it.
    """
    return get("FB_PAGE_TOKEN") or get("PAGE_ACCESS_TOKEN")


def graph_version():
    return get("GRAPH_API_VERSION", DEFAULT_GRAPH_VERSION)
