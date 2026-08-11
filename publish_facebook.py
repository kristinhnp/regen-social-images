#!/usr/bin/env python3
"""
Regen Holistics - Facebook publisher.

    python publish_facebook.py --live       publish anything due right now
    python publish_facebook.py --dry-run    show what would go, send nothing

Why this exists
---------------
publish.py has a --facebook mode that hands Meta the whole month at once
and lets Meta hold each post until its time. That works, but Facebook
refuses anything more than 30 days ahead, so it has to be re-run by hand
every month. That monthly command never got run, which is why Facebook sat
silent while Instagram posted every day.

This does it the same way Instagram is already done: wake up, ask "is
anything due?", publish it, write down what happened. No monthly command,
no 30-day ceiling, and both feeds go out within seconds of each other.

It deliberately does not touch publish.py, which is what Instagram depends
on and which is working. It borrows publish.py's helpers instead, so the
two can never drift apart on captions or dates.

Nothing here knows your password. Authorisation is the Page token in .env
(or, on GitHub, the FB_PAGE_TOKEN secret).
"""

import argparse
import datetime
import json
import sys

import config
import meta_api
import publish
import state

# A post more than this far past its time is treated as missed rather than
# fired late, so a job that was off for a week does not empty a backlog
# onto the Page all at once.
DEFAULT_MAX_LATE_HOURS = 12


def due_now(posts, data, live, max_late_hours, force_late):
    """Every post whose time has come and which Facebook has not done."""
    now = config.now()
    cutoff = now - datetime.timedelta(hours=max_late_hours)
    due = []

    for post in posts:
        pid = post["id"]
        entry = state.entry(data, pid)
        when = config.scheduled_dt(post["date"])
        entry["scheduled_for"] = when.isoformat()

        # already_done covers both "posted" and "scheduled", so a post
        # handed to Meta by the old --facebook route is left alone.
        if state.already_done(entry, "fb") or entry["fb_status"] == "missed":
            continue
        if when > now:
            continue
        if when < cutoff and not force_late:
            print("  - %s  was due %s and is more than %g hours late. Marked "
                  "missed." % (pid, when.strftime("%b %d %H:%M"), max_late_hours))
            if live:
                state.mark(data, pid, "fb", "missed",
                           error="more than %g hours late" % max_late_hours)
            continue
        due.append((post, entry, when))
    return due


def already_on_page(graph, page_id, caption, cache):
    """
    Ask the Page itself whether this post is already up.

    published.json is the first line of defence, but it is a file, and a
    file can be lost, reverted, or left behind by a failed push. The Page
    cannot go out of step with reality. Same belt-and-braces check
    publish.py already does for Instagram.

    Returns the existing post id, or None.
    """
    if cache.get("data") is None:
        try:
            result = graph.get(
                "%s/feed" % page_id,
                {"fields": "id,message,created_time", "limit": "50"},
                label="recent posts",
            )
            cache["data"] = result.get("data", [])
        except meta_api.MetaError:
            # Not being able to check is not a reason to refuse to post.
            # published.json still guards the common case.
            cache["data"] = []

    key = publish._caption_key(caption)
    if not key:
        return None
    for item in cache["data"]:
        if publish._caption_key(item.get("message") or "") == key:
            return item.get("id")
    return None


def publish_one(graph, page_id, entry, caption, label):
    """
    Upload the picture, then make a post that carries it.

    Two steps, and the order matters. Posting straight to /photos makes a
    photo in an album: it is published, but it is a photo story. It lands
    in the Photos tab and its link is photo.php, not /posts/. What we want
    is an ordinary post with a picture on it, which is what Business Suite
    produces.

    So the photo goes up unpublished to get its id, and then a feed post
    attaches it. That second object is the real post.
    """
    photo = graph.post(
        "%s/photos" % page_id,
        {"url": entry["image_url"], "published": "false"},
        label="%s upload" % label,
    )
    media_id = photo.get("id")
    if not media_id:
        raise RuntimeError("Facebook did not return a photo id.")

    result = graph.post(
        "%s/feed" % page_id,
        {
            "message": caption,
            "attached_media[0]": json.dumps({"media_fbid": media_id}),
            "published": "true",
        },
        label="%s post" % label,
    )
    post_id = result.get("id")
    if not post_id:
        raise RuntimeError("Facebook did not return a post id.")
    return post_id, media_id


def run(live, max_late_hours, force_late):
    page_id = config.require("PAGE_ID", "This is your Facebook Page's numeric id.")
    token = config.page_token()
    if not token:
        sys.exit("\nNo access token yet. Run:  .venv/bin/python setup_check.py\n")
    graph = meta_api.Graph(token)

    posts = publish.load_posts()
    data = state.load()

    try:
        due = due_now(posts, data, live, max_late_hours, force_late)
        if not due:
            print("Facebook: nothing due right now. (%s ET)"
                  % config.now().strftime("%b %d %H:%M"))
            return 0

        if live:
            publish.warn_if_token_expiring(graph)

        # Filled in on first use, so the Page's recent posts are fetched at
        # most once per run rather than once per post.
        remote_cache = {"data": None}
        done = failed = 0

        for post, entry, when in due:
            pid = post["id"]
            caption = publish.full_caption(post)

            if not entry.get("image_url"):
                message = "no public image address. Run deploy.py --live first."
                print("  ! %s  %s" % (pid, message))
                if live:
                    state.mark(data, pid, "fb", "failed", error=message)
                failed += 1
                continue

            if not live:
                print("  > %s  would publish to Facebook now (was due %s ET)"
                      % (pid, when.strftime("%a %b %d, %H:%M")))
                print("        image   %s" % entry["image_url"])
                print("        caption %s" % caption.splitlines()[0][:66])
                continue

            # Last check before sending: is it already on the Page? This
            # catches the case where published.json was lost or rolled back.
            existing = already_on_page(graph, page_id, caption, remote_cache)
            if existing:
                print("  = %s  already on the Page (%s). Recording, not "
                      "reposting." % (pid, existing))
                state.mark(data, pid, "fb", "posted", post_id=existing, error=None)
                continue

            try:
                post_id, media_id = publish_one(
                    graph, page_id, entry, caption, "FB %s" % pid)
                state.mark(data, pid, "fb", "posted", post_id=post_id,
                           photo_id=media_id, error=None)
                print("  + %s  published to Facebook" % pid)
                done += 1
            except (meta_api.MetaError, RuntimeError) as exc:
                message = config.redact(str(exc))
                state.mark(data, pid, "fb", "failed",
                           error=message.splitlines()[0])
                print("  ! %s  failed: %s" % (pid, message.splitlines()[0]))
                failed += 1

        if live:
            print("\nFacebook: %d published, %d failed." % (done, failed))
        else:
            print("\nDry run complete. Nothing was sent.")
        return 1 if failed else 0
    finally:
        state.save(data)


def main():
    ap = argparse.ArgumentParser(
        description="Publish any Facebook post whose time has arrived.",
        epilog="Without --live nothing is ever sent.",
    )
    ap.add_argument("--live", action="store_true",
                    help="actually send. Without this it is a dry run.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be sent and send nothing")
    ap.add_argument("--max-late-hours", type=float,
                    default=DEFAULT_MAX_LATE_HOURS,
                    help="how late a post may still go out (default %d)"
                         % DEFAULT_MAX_LATE_HOURS)
    ap.add_argument("--force-late", action="store_true",
                    help="publish overdue posts regardless of how late")
    args = ap.parse_args()

    live = args.live and not args.dry_run
    if not live:
        print("DRY RUN - nothing will be sent to Facebook.")
        print("Add --live when the preview looks right.\n")

    try:
        return run(live, args.max_late_hours, args.force_late)
    except meta_api.MetaError as exc:
        print("\n" + config.redact(str(exc)) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
