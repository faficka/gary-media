"""Top up the Buffer queue (Instagram reel + TikTok + YouTube short) from the posting calendar.

Runs on this PC and every hour in GitHub Actions (repo faficka/gary-media, folder cloud/, secret BUFFER_KEY), so the
free Buffer plan's limit of 10 scheduled posts per channel never matters: as posts go out, the next ones are added.

    python -I tools/buffer_fill.py [schedule.json] [media.json]           (top up the queue)
    python -I tools/buffer_fill.py --check [schedule.json] [media.json]   (daily: did everything go out?)

Stateless: Buffer itself is the record. A post counts as done when the channel already has a post due at that exact
time (scheduled, sending, sent or failed), so the PC and the cloud can both run it without double posting.
schedule.json item fields: when (Europe/Prague), file, title, caption, hashtags, optional platforms.
Items whose title says "az po schvaleni" are skipped. media.json maps file -> name on GitHub Pages (written by
tools/buffer_schedule.py when it publishes the videos); a file missing there has no sound version yet.
"""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORG = "6ac9e14276bd694478b8f695"
PAGES = "https://faficka.github.io/gary-media/v/"
LIMIT = 10  # Buffer Free: scheduled posts per channel
PER_DAY = {"youtube": 2, "instagram": 3, "tiktok": 3}  # max posts per Prague day (2026-10-12: user cut Gary to 3/day IG+TikTok, YouTube 2/day)


def api_key():
    return os.environ.get("BUFFER_KEY") or (HERE.parent / "buffer-key.txt").read_text(encoding="utf-8").strip()


def gql(query, variables=None):
    req = urllib.request.Request("https://api.buffer.com",
                                 data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Authorization": "Bearer " + api_key(), "Content-Type": "application/json",
                                          "User-Agent": "gary-gyoza/1.0"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}"}]}


def last_sunday(year, month):
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def prague_to_utc(local):
    """EU rule: CEST (UTC+2) from last Sunday of March to last Sunday of October, else CET (UTC+1)."""
    start = dt.datetime.combine(last_sunday(local.year, 3), dt.time(2))
    end = dt.datetime.combine(last_sunday(local.year, 10), dt.time(3))
    return (local - dt.timedelta(hours=2 if start <= local < end else 1)).replace(tzinfo=dt.timezone.utc)


def youtube_meta(it):
    """YouTube: search-keyword title (yt_title in the calendar), Comedy category, AI label on (YouTube requires
    disclosure of realistic synthetic video; the user agreed 2026-10-10 - Instagram/TikTok stay unlabelled)."""
    return {"youtube": {"title": (it.get("yt_title") or it["caption"])[:100], "privacy": "public", "madeForKids": False,
                        "notifySubscribers": True, "categoryId": "23", "isAiGenerated": True}}  # 23 = Comedy


def prague_date(utc):
    """Calendar date in Prague for a UTC datetime (inverse of prague_to_utc)."""
    for hours in (2, 1):
        local = utc.replace(tzinfo=None) + dt.timedelta(hours=hours)
        if prague_to_utc(local) == utc:
            return local.date()
    return (utc + dt.timedelta(hours=1)).date()


def post_input(service, channel_id, it, url, due):
    text = it["caption"] + ("\n\n" + it["hashtags"] if it.get("hashtags") else "")
    inp = {"channelId": channel_id, "schedulingType": "automatic", "mode": "customScheduled", "needsApproval": False,
           "dueAt": due, "assets": [{"video": {"url": url}}], "text": text}
    if service == "instagram":
        inp["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    elif service == "youtube":
        inp["text"] = text + " #shorts"
        inp["metadata"] = youtube_meta(it)
    return inp


def channels():
    r = gql("query($o: OrganizationId!){ channels(input:{organizationId:$o}) { id service isDisconnected } }", {"o": ORG})
    if "errors" in r:
        sys.exit(f"Buffer error: {r['errors']}")
    return {c["service"]: c["id"] for c in r["data"]["channels"] if not c["isDisconnected"]}


def posts_since(start):
    """All posts due after start (scheduled, sending, sent, failed); Buffer pages are max 100 posts."""
    nodes, after = [], None
    while True:
        r = gql("""query($o: OrganizationId!, $s: DateTime!, $a: String){ posts(first: 100, after: $a, input:{organizationId:$o,
                   filter:{status:[scheduled, sending, sent, error], dueAt:{start:$s}}}) {
                   pageInfo { hasNextPage endCursor } edges { node { channelId dueAt status error { message } } } } }""",
                {"o": ORG, "s": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "a": after})
        if "errors" in r:
            sys.exit(f"Buffer error: {r['errors']}")
        nodes += [e["node"] for e in r["data"]["posts"]["edges"]]
        if not r["data"]["posts"]["pageInfo"]["hasNextPage"]:
            return nodes
        after = r["data"]["posts"]["pageInfo"]["endCursor"]


def check(schedule_path, media_path):
    """Daily check: every calendar post due in the last 26 hours went out, nothing failed. Exit 1 on a problem
    (GitHub then e-mails the account owner about the failed workflow run)."""
    items = json.loads(Path(schedule_path).read_text(encoding="utf-8"))
    media = json.loads(Path(media_path).read_text(encoding="utf-8"))
    chans = channels()
    names = {cid: s for s, cid in chans.items()}
    now = dt.datetime.now(dt.timezone.utc)
    posts = {(n["channelId"], n["dueAt"][:16]): n for n in posts_since(now - dt.timedelta(hours=26))}
    problems, ok = [], 0
    for n in posts.values():
        if n["status"] == "error":
            problems.append(f"FAILED  {n['dueAt'][:16]} UTC {names.get(n['channelId'])}: {(n.get('error') or {}).get('message')}")
    for it in items:
        due = prague_to_utc(dt.datetime.strptime(it["when"], "%Y-%m-%d %H:%M"))
        if not now - dt.timedelta(hours=26) <= due <= now - dt.timedelta(minutes=20):
            continue
        if "az po schvaleni" in it["title"]:  # (media.json only lists videos still to come, so it is not checked here)
            continue
        for service, cid in chans.items():
            if service not in it.get("platforms", chans):
                continue
            n = posts.get((cid, due.strftime("%Y-%m-%dT%H:%M")))
            if n and n["status"] == "sent":
                ok += 1
            elif not n or n["status"] != "error":
                problems.append(f"MISSING {it['when']} {service} {it['file']}: {n['status'] if n else 'not in Buffer'}")
    print(f"{now:%Y-%m-%d %H:%M} UTC check: {ok} post(s) went out in the last 26 h, {len(problems)} problem(s)")
    for line in problems:
        print(line)
    if problems:
        sys.exit(1)


def fill(schedule_path, media_path):
    items = json.loads(Path(schedule_path).read_text(encoding="utf-8"))
    media = json.loads(Path(media_path).read_text(encoding="utf-8"))
    chans = channels()
    now = dt.datetime.now(dt.timezone.utc)
    done, queued, per_day = set(), {cid: 0 for cid in chans.values()}, {}
    for n in posts_since(now - dt.timedelta(days=3)):
        done.add((n["channelId"], n["dueAt"][:16]))
        if n["status"] == "scheduled":
            queued[n["channelId"]] = queued.get(n["channelId"], 0) + 1
        day = prague_date(dt.datetime.strptime(n["dueAt"][:16], "%Y-%m-%dT%H:%M").replace(tzinfo=dt.timezone.utc))
        per_day[(n["channelId"], day)] = per_day.get((n["channelId"], day), 0) + 1
    created = 0
    for it in sorted(items, key=lambda i: i["when"]):
        due = prague_to_utc(dt.datetime.strptime(it["when"], "%Y-%m-%d %H:%M"))
        if due < now + dt.timedelta(minutes=15) or "az po schvaleni" in it["title"] or it["file"] not in media:
            continue
        stamp = due.strftime("%Y-%m-%dT%H:%M")
        for service, cid in chans.items():
            if service not in it.get("platforms", chans) or (cid, stamp) in done or queued[cid] >= LIMIT:
                continue
            day = prague_date(due)
            if per_day.get((cid, day), 0) >= PER_DAY.get(service, 99):
                continue
            res = (gql("""mutation($i: CreatePostInput!){ createPost(input:$i){ ... on PostActionSuccess { post { id } }
                          ... on MutationError { message } } }""",
                       {"i": post_input(service, cid, it, PAGES + media[it["file"]], stamp + ":00.000Z")})
                   .get("data") or {}).get("createPost") or {}
            if "post" in res:
                queued[cid] += 1
                per_day[(cid, day)] = per_day.get((cid, day), 0) + 1
                created += 1
                print(f"OK    {it['when']}  {service:9s} {it['file']}")
            else:
                print(f"FAIL  {it['when']}  {service:9s} {it['file']}: {res.get('message')}")
                if "limit" in (res.get("message") or "").lower():
                    queued[cid] = LIMIT
    print(f"{now:%Y-%m-%d %H:%M} UTC: {created} new post(s); queued per channel:",
          {s: queued[c] for s, c in chans.items()})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if a != "--check"]
    paths = (args[0] if args else HERE / "schedule.json",
             args[1] if len(args) > 1 else HERE.parent / "media_repo" / "cloud" / "media.json")
    (check if "--check" in sys.argv else fill)(*paths)
