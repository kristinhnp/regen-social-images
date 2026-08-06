#!/usr/bin/env python3
"""
Regen Holistics - publisher.

    python publish.py --facebook --dry-run     preview the month's FB schedule
    python publish.py --facebook --live        schedule every FB post, one shot
    python publish.py --instagram-due --live   publish whatever is due right now
    python publish.py --status                 table of every post

Facebook and Instagram are not the same job. Facebook accepts a future
timestamp and holds the post itself, so the whole month goes up in one
command and the Mac can be closed. Instagram has no such thing, so
something has to be awake at the moment each post is due. That is what the
launchd job in the README is for.

Nothing here knows your password. Authorisation is a token you generate on
Meta's own site, kept in .env.
"""

import argparse
import datetime
import json
import os
import sys
import time

import config
import meta_api
import state

CONTENT = os.path.join(config.HERE, "content.json")

# How long to wait for Instagram to finish preparing an image.
IG_POLL_SECONDS = 5
IG_POLL_ATTEMPTS = 24        # two minutes

# A post more than this far past its time is treated as missed rather than
# fired late. Stops a Mac that was asleep for days from dumping a backlog
# onto the feed all at once.
DEFAULT_MAX_LATE_HOURS = 12


# ----------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------

def load_posts():
    if not os.path.exists(CONTENT):
        sys.exit("Could not find content.json next to publish.py.")
    with open(CONTENT, encoding="utf-8") as f:
        posts = json.load(f)
    posts.sort(key=lambda p: (p["date"], p["id"]))
    return posts


def full_caption(post):
    caption = post.get("caption", "").strip()
    tags = post.get("hashtags", "").strip()
    return caption + "\n\n" + tags if tags else caption


def image_filename(post):
    """Mirrors the naming in generate.py so the two stay in step."""
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    months = ["jan", "feb", "mar", "apr", "may", "jun",
              "jul", "aug", "sep", "oct", "nov", "dec"]
    d = datetime.date.fromisoformat(post["date"])
    return "%s-%s-%s-%02d" % (post["id"], days[d.weekday()], months[d.month - 1], d.day)


# ----------------------------------------------------------------------
# token
# ----------------------------------------------------------------------

def token_days_left(graph):
    """Days until the token expires, or None if it does not expire."""
    app_id = config.get("META_APP_ID")
    app_secret = config.get("META_APP_SECRET")
    if not (app_id and app_secret):
        return "unknown"
    try:
        result = graph.get(
            "debug_token",
            {"input_token": graph.token, "access_token": "%s|%s" % (app_id, app_secret)},
            label="token check",
        )
    except meta_api.MetaError:
        return "unknown"
    expires = (result.get("data") or {}).get("expires_at", 0)
    if not expires:
        return None
    left = datetime.datetime.fromtimestamp(expires, tz=config.TZ) - config.now()
    return max(0, left.days)


def warn_if_token_expiring(graph):
    days = token_days_left(graph)
    if days is None or days == "unknown":
        return
    if days < 10:
        print("\n  ! Your access token expires in %d day%s."
              % (days, "" if days == 1 else "s"))
        print("    Run .venv/bin/python check_token.py for how to renew it.\n")


# ----------------------------------------------------------------------
# facebook
# ----------------------------------------------------------------------

def do_facebook(posts, data, live):
    page_id = config.require("PAGE_ID", "This is your Facebook Page's numeric id.")
    token = config.page_token()
    if not token:
        sys.exit("\nNo access token yet. Run:  .venv/bin/python setup_check.py\n")
    graph = meta_api.Graph(token)
    if live:
        warn_if_token_expiring(graph)

    now = config.now()
    earliest = now + datetime.timedelta(minutes=config.FB_MIN_LEAD_MINUTES)
    latest = now + datetime.timedelta(days=config.FB_MAX_LEAD_DAYS)

    print("Facebook Page %s" % page_id)
    print("Scheduling window: %s  to  %s\n"
          % (earliest.strftime("%b %d %H:%M"), latest.strftime("%b %d %H:%M")))

    done = skipped = failed = 0

    for post in posts:
        pid = post["id"]
        entry = state.entry(data, pid)
        when = config.scheduled_dt(post["date"])
        entry["scheduled_for"] = when.isoformat()
        entry["image_file"] = entry.get("image_file") or image_filename(post) + ".png"

        if state.already_done(entry, "fb"):
            print("  = %s  already %s, leaving alone" % (pid, entry["fb_status"]))
            skipped += 1
            continue

        if when < earliest:
            reason = ("its time has already passed" if when < now
                      else "it is less than %d minutes away"
                           % config.FB_MIN_LEAD_MINUTES)
            print("  - %s  skipped, %s (%s)" % (pid, reason, when.strftime("%b %d %H:%M")))
            if live:
                state.mark(data, pid, "fb", "skipped", error=reason)
            skipped += 1
            continue

        if when > latest:
            print("  - %s  skipped, %s is more than %d days out. Facebook will "
                  "not hold it yet." % (pid, when.strftime("%b %d"),
                                        config.FB_MAX_LEAD_DAYS))
            print("        Run this command again closer to the date.")
            skipped += 1
            continue

        caption = full_caption(post)
        hosted = entry.get("image_url")
        local = os.path.join(config.OUT_IMAGES, image_filename(post) + ".png")

        if not live:
            source = "hosted image" if hosted else "local file %s" % os.path.basename(local)
            print("  > %s  would schedule for %s ET  (%s)"
                  % (pid, when.strftime("%a %b %d, %I:%M %p").replace(" 0", " "), source))
            print("        %s" % caption.splitlines()[0][:72])
            continue

        params = {
            "caption": caption,
            "published": "false",
            "scheduled_publish_time": str(int(when.timestamp())),
        }
        try:
            if hosted:
                params["url"] = hosted
                result = graph.post("%s/photos" % page_id, params, label="FB %s" % pid)
            else:
                if not os.path.exists(local):
                    raise RuntimeError(
                        "no image at %s. Run generate.py first." % os.path.basename(local)
                    )
                result = graph.post("%s/photos" % page_id, params,
                                    image_path=local, label="FB %s" % pid)
            post_id = result.get("post_id") or result.get("id")
            state.mark(data, pid, "fb", "scheduled", post_id=post_id, error=None)
            print("  + %s  scheduled for %s ET" % (pid, when.strftime("%a %b %d, %H:%M")))
            done += 1
        except (meta_api.MetaError, RuntimeError) as exc:
            message = config.redact(str(exc))
            state.mark(data, pid, "fb", "failed", error=message.splitlines()[0])
            print("  ! %s  failed: %s" % (pid, message.splitlines()[0]))
            failed += 1

    if live:
        print("\n%d scheduled, %d skipped, %d failed." % (done, skipped, failed))
    else:
        print("\nDry run complete. Nothing was sent.")
    if failed:
        print("Failed posts are recorded in published.json and can be retried "
              "by running this again.")
    return 1 if failed else 0


# ----------------------------------------------------------------------
# instagram
# ----------------------------------------------------------------------

def _caption_key(caption):
    """A short fingerprint of a caption, used to spot one already posted."""
    for line in caption.splitlines():
        line = line.strip()
        if line:
            return " ".join(line.lower().split())[:80]
    return ""


def already_on_instagram(graph, ig_id, caption, cache):
    """
    Ask Instagram directly whether this post is already up.

    published.json is the first line of defence, but it is a file, and a
    file can be lost, reverted, or left behind by a failed push. This asks
    the account itself, which cannot go out of step with reality.

    Returns the existing media id, or None.
    """
    if cache.get("data") is None:
        try:
            result = graph.get(
                "%s/media" % ig_id,
                {"fields": "id,caption,timestamp", "limit": "50"},
                label="recent media",
            )
            cache["data"] = result.get("data", [])
        except meta_api.MetaError:
            # Not being able to check is not a reason to refuse to post.
            # published.json still guards the common case.
            cache["data"] = []

    key = _caption_key(caption)
    if not key:
        return None
    for media in cache["data"]:
        if _caption_key(media.get("caption") or "") == key:
            return media.get("id")
    return None


def publish_one_instagram(graph, ig_id, entry, caption, label):
    """Create a container, wait for Meta to prepare it, then publish."""
    container = graph.post(
        "%s/media" % ig_id,
        {"image_url": entry["image_url"], "caption": caption},
        label="%s container" % label,
    )
    creation_id = container.get("id")
    if not creation_id:
        raise RuntimeError("Instagram did not return a container id.")

    for _ in range(IG_POLL_ATTEMPTS):
        status = graph.get(
            creation_id, {"fields": "status_code,status"}, label="%s status" % label
        )
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(
                "Instagram could not prepare the image (%s). %s"
                % (code, status.get("status", ""))
            )
        time.sleep(IG_POLL_SECONDS)
    else:
        raise RuntimeError(
            "Instagram was still preparing the image after %d seconds."
            % (IG_POLL_ATTEMPTS * IG_POLL_SECONDS)
        )

    published = graph.post(
        "%s/media_publish" % ig_id,
        {"creation_id": creation_id},
        label="%s publish" % label,
    )
    return published.get("id")


def do_instagram_due(posts, data, live, max_late_hours, force_late):
    ig_id = config.require("IG_USER_ID", "This is your Instagram Business account id.")
    # Instagram publishing also goes through the Page's token, not the
    # User one, because the Instagram account hangs off the Page.
    token = config.page_token()
    if not token:
        sys.exit("\nNo access token yet. Run:  .venv/bin/python setup_check.py\n")
    graph = meta_api.Graph(token)

    now = config.now()
    cutoff = now - datetime.timedelta(hours=max_late_hours)
    due = []

    for post in posts:
        entry = state.entry(data, post["id"])
        when = config.scheduled_dt(post["date"])
        entry["scheduled_for"] = when.isoformat()
        if state.already_done(entry, "ig") or entry["ig_status"] == "missed":
            continue
        if when > now:
            continue
        if when < cutoff and not force_late:
            print("  - %s  was due %s and is more than %d hours late. Marked "
                  "missed." % (post["id"], when.strftime("%b %d %H:%M"), max_late_hours))
            if live:
                state.mark(data, post["id"], "ig", "missed",
                           error="more than %d hours late" % max_late_hours)
            continue
        due.append((post, entry, when))

    if not due:
        print("Nothing is due right now. (%s ET)" % now.strftime("%b %d %H:%M"))
        return 0

    if live:
        warn_if_token_expiring(graph)

    # Filled in on first use, so the recent-media list is fetched at most
    # once per run rather than once per post.
    remote_cache = {"data": None}

    done = failed = 0
    for post, entry, when in due:
        pid = post["id"]
        caption = full_caption(post)

        if len(caption) > config.IG_MAX_CAPTION:
            message = ("caption is %d characters, over Instagram's %d limit"
                       % (len(caption), config.IG_MAX_CAPTION))
            print("  ! %s  %s" % (pid, message))
            if live:
                state.mark(data, pid, "ig", "failed", error=message)
            failed += 1
            continue

        if not entry.get("image_url"):
            message = "no public image address. Run deploy.py --live first."
            print("  ! %s  %s" % (pid, message))
            if live:
                state.mark(data, pid, "ig", "failed", error=message)
            failed += 1
            continue

        if not live:
            print("  > %s  would publish now (was due %s ET)"
                  % (pid, when.strftime("%a %b %d, %H:%M")))
            print("        image   %s" % entry["image_url"])
            print("        caption %s" % caption.splitlines()[0][:66])
            continue

        # Last check before sending: is it already on the account? This
        # catches the case where published.json was lost or rolled back.
        existing = already_on_instagram(graph, ig_id, caption, remote_cache)
        if existing:
            print("  = %s  already on Instagram (%s). Recording, not reposting."
                  % (pid, existing))
            state.mark(data, pid, "ig", "posted", media_id=existing,
                       error=None)
            continue

        try:
            media_id = publish_one_instagram(graph, ig_id, entry, caption, "IG %s" % pid)
            state.mark(data, pid, "ig", "posted", media_id=media_id, error=None)
            print("  + %s  published to Instagram" % pid)
            done += 1
        except (meta_api.MetaError, RuntimeError) as exc:
            message = config.redact(str(exc))
            state.mark(data, pid, "ig", "failed", error=message.splitlines()[0])
            print("  ! %s  failed: %s" % (pid, message.splitlines()[0]))
            failed += 1

    if live:
        print("\n%d published, %d failed." % (done, failed))
    else:
        print("\nDry run complete. Nothing was sent.")
    return 1 if failed else 0


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------

SYMBOL = {
    "pending": ".", "scheduled": "S", "posted": "*",
    "failed": "!", "skipped": "-", "missed": "x",
}


def do_status(posts, data):
    print("%-4s %-12s %-6s %-11s %-11s %s"
          % ("id", "date", "time", "facebook", "instagram", "image"))
    print("-" * 74)

    counts = {}
    for post in posts:
        pid = post["id"]
        entry = state.entry(data, pid)
        when = config.scheduled_dt(post["date"])
        fb = entry["fb_status"]
        ig = entry["ig_status"]
        counts[("fb", fb)] = counts.get(("fb", fb), 0) + 1
        counts[("ig", ig)] = counts.get(("ig", ig), 0) + 1
        print("%-4s %-12s %-6s %s %-9s %s %-9s %s"
              % (pid, post["date"], when.strftime("%H:%M"),
                 SYMBOL.get(fb, "?"), fb,
                 SYMBOL.get(ig, "?"), ig,
                 "yes" if entry.get("image_url") else "no"))

    print("-" * 74)
    fb_summary = ", ".join("%d %s" % (n, s) for (p, s), n in sorted(counts.items())
                           if p == "fb")
    ig_summary = ", ".join("%d %s" % (n, s) for (p, s), n in sorted(counts.items())
                           if p == "ig")
    print("Facebook : %s" % fb_summary)
    print("Instagram: %s" % ig_summary)

    problems = [(pid, e) for pid, e in sorted(data["posts"].items())
                if e.get("fb_error") or e.get("ig_error")]
    if problems:
        print("\nProblems:")
        for pid, entry in problems:
            for platform in ("fb", "ig"):
                err = entry.get("%s_error" % platform)
                if err and entry.get("%s_status" % platform) in ("failed", "missed", "skipped"):
                    print("  %s %s: %s" % (pid, platform.upper(), config.redact(err)))
    return 0


# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Schedule the month to Facebook and publish due posts to "
                    "Instagram.",
        epilog="Without --live nothing is ever sent.",
    )
    ap.add_argument("--facebook", action="store_true",
                    help="schedule every unscheduled post to the Facebook Page")
    ap.add_argument("--instagram-due", action="store_true",
                    help="publish any Instagram post whose time has arrived")
    ap.add_argument("--status", action="store_true",
                    help="show what has been scheduled, posted or failed")
    ap.add_argument("--live", action="store_true",
                    help="actually send. Without this it is a dry run.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be sent and send nothing")
    ap.add_argument("--max-late-hours", type=float, default=DEFAULT_MAX_LATE_HOURS,
                    help="how late an Instagram post may still go out "
                         "(default %d)" % DEFAULT_MAX_LATE_HOURS)
    ap.add_argument("--force-late", action="store_true",
                    help="publish overdue Instagram posts regardless of how late")
    args = ap.parse_args()

    if not (args.facebook or args.instagram_due or args.status):
        ap.print_help()
        return 2

    live = args.live and not args.dry_run
    posts = load_posts()
    data = state.load()

    if args.status:
        return do_status(posts, data)

    if not live:
        print("DRY RUN - nothing will be sent to Facebook or Instagram.")
        print("Add --live when the preview looks right.\n")

    try:
        if args.facebook:
            code = do_facebook(posts, data, live)
        else:
            code = do_instagram_due(posts, data, live,
                                    args.max_late_hours, args.force_late)
    except meta_api.MetaError as exc:
        print("\n" + config.redact(str(exc)) + "\n")
        return 1
    finally:
        state.save(data)
    return code


if __name__ == "__main__":
    sys.exit(main())
