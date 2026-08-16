#!/usr/bin/env python3
"""
Make a spreadsheet Buffer can import, so Facebook needs no Meta app at all.

Buffer publishes through its OWN Meta app, which Meta has already reviewed.
That sidesteps the whole problem this project ran into: no app review, no
business verification, no access levels, and posts that are actually
visible to the public.

Your pictures already live at public web addresses on GitHub, which is
exactly what Buffer's bulk import wants, so nothing needs uploading by hand.

    python buffer_csv.py            # writes output/buffer-import.csv

Columns are the ones Buffer's importer expects:
    Text, Image URL, Posting Time
"""

import argparse
import csv
import datetime
import json
import os
import sys

import config
import state

CONTENT = os.path.join(config.HERE, "content.json")
OUT = os.path.join(config.OUT, "buffer-import.csv")

# Buffer's free plan accepts 10 rows per channel per upload and silently
# drops anything past that, so the file is split rather than truncated.
FREE_TIER_ROWS = 10


def main():
    ap = argparse.ArgumentParser(
        description="Build a Buffer bulk-import spreadsheet from content.json.")
    ap.add_argument("--split", type=int, default=0,
                    help="rows per file (use 10 for Buffer's free plan)")
    ap.add_argument("--all", action="store_true",
                    help="include every post, even ones Facebook already has")
    args = ap.parse_args()

    with open(CONTENT, encoding="utf-8") as f:
        posts = sorted(json.load(f), key=lambda p: (p["date"], p["id"]))

    data = state.load()
    rows, missing, done = [], [], []
    for post in posts:
        entry = state.entry(data, post["id"])

        # Anything Facebook has already had - published, or deliberately
        # passed over as too late - stays out, or Buffer would post it a
        # second time. published.json is the same record the Instagram job
        # keeps, so the two cannot disagree about what has gone out.
        if not args.all and entry.get("fb_status") not in (None, "pending", "failed"):
            done.append("%s (%s)" % (post["id"], entry.get("fb_status")))
            continue

        url = entry.get("image_url")
        if not url:
            missing.append(post["id"])
            continue
        caption = post.get("caption", "").strip()
        tags = post.get("hashtags", "").strip()
        text = caption + "\n\n" + tags if tags else caption
        when = config.scheduled_dt(post["date"])
        rows.append({
            "Text": text,
            "Image URL": url,
            # Buffer wants local wall-clock time, no timezone suffix.
            "Posting Time": when.strftime("%Y-%m-%d %H:%M"),
        })

    if done:
        print("  Skipped, Facebook already has these: %s" % ", ".join(done))
        print("  (add --all to include them anyway)\n")

    if missing:
        print("  ! No image address for: %s" % ", ".join(missing))
        print("    Run deploy.py --live first.\n")

    if not rows:
        sys.exit("Nothing to write.")

    # A posting time in the past would make Buffer refuse the row or fire
    # it immediately. Move any such post to the first free weekday after
    # the end of the run, keeping the 8am rhythm.
    times = [datetime.datetime.strptime(r["Posting Time"], "%Y-%m-%d %H:%M") for r in rows]
    now_naive = config.now().replace(tzinfo=None)
    last = max(times)
    moved = []
    for r, t in zip(list(rows), times):
        if t <= now_naive:
            nxt = last + datetime.timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += datetime.timedelta(days=1)
            r["Posting Time"] = nxt.strftime("%Y-%m-%d %H:%M")
            last = nxt
            moved.append((t.strftime("%d %b"), nxt.strftime("%a %d %b")))
            rows.remove(r); rows.append(r)
    for was, now_ in moved:
        print("  moved a past slot (%s) to the end: %s" % (was, now_))

    def write(path, chunk):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["Text", "Image URL", "Posting Time"])
            w.writeheader()
            w.writerows(chunk)

    if args.split:
        made = []
        for i in range(0, len(rows), args.split):
            part = OUT.replace(".csv", "-part%d.csv" % (i // args.split + 1))
            write(part, rows[i:i + args.split])
            made.append((part, len(rows[i:i + args.split])))
        print("Wrote %d files:" % len(made))
        for path, n in made:
            print("   %s  (%d posts)" % (os.path.relpath(path, config.HERE), n))
    else:
        write(OUT, rows)
        print("Wrote %s  (%d posts)" % (os.path.relpath(OUT, config.HERE), len(rows)))
        if len(rows) > FREE_TIER_ROWS:
            print("\n  Buffer's free plan takes %d rows per upload and silently"
                  % FREE_TIER_ROWS)
            print("  ignores the rest. Either upload in batches:")
            print("      .venv/bin/python buffer_csv.py --split 10")
            print("  or use Buffer Essentials, which lifts the limit.")

    print("\nFirst row looks like:")
    r = rows[0]
    print("   Text        : %s" % r["Text"].splitlines()[0][:58])
    print("   Image URL   : %s" % r["Image URL"])
    print("   Posting Time: %s" % r["Posting Time"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
