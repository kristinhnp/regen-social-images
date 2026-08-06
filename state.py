"""
published.json - the record of what has actually gone out.

This file is the safety net against double posting. Every publish step
reads it first and writes to it immediately after Meta confirms, so an
interrupted run can be restarted without posting anything twice.
"""

import datetime
import json
import os
import tempfile

import config

BLANK_POST = {
    "image_file": None,
    "image_url": None,
    "scheduled_for": None,
    "fb_status": "pending",     # pending | scheduled | posted | failed | skipped
    "fb_post_id": None,
    "fb_error": None,
    "fb_updated_at": None,
    "ig_status": "pending",     # pending | posted | failed | missed
    "ig_media_id": None,
    "ig_error": None,
    "ig_updated_at": None,
}


def _stamp():
    return config.now().isoformat(timespec="seconds")


def load():
    if not os.path.exists(config.STATE_PATH):
        return {"posts": {}}
    with open(config.STATE_PATH, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError:
            raise SystemExit(
                "published.json is damaged and cannot be read.\n"
                "Rename it to published-broken.json and run the publisher "
                "again to start a fresh record.\n"
                "Note that doing so loses track of what has already been "
                "posted, so check your Page first."
            )
    data.setdefault("posts", {})
    return data


def save(data):
    """Write via a temp file so an interrupted save cannot corrupt state."""
    data["updated_at"] = _stamp()
    directory = os.path.dirname(config.STATE_PATH)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".published-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, config.STATE_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def entry(data, post_id):
    post = data["posts"].setdefault(post_id, dict(BLANK_POST))
    for key, value in BLANK_POST.items():
        post.setdefault(key, value)
    return post


def mark(data, pid, platform, status, **fields):
    """
    Record the outcome of one platform for one post, then save at once.

    The id parameter is deliberately not called post_id: callers pass
    post_id=... as one of the fields to store, and the two would collide.
    """
    post = entry(data, pid)
    post["%s_status" % platform] = status
    post["%s_updated_at" % platform] = _stamp()
    for key, value in fields.items():
        post["%s_%s" % (platform, key)] = value
    save(data)
    return post


def already_done(post, platform):
    """True when this platform is finished and must not be touched again."""
    status = post.get("%s_status" % platform)
    if platform == "fb":
        return status in ("scheduled", "posted")
    return status == "posted"
