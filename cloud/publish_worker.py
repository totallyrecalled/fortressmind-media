"""FortressMind CLOUD PUBLISHER — runs on GitHub Actions, not on Hadi's PC.

Every 10 minutes the workflow checks out this repo and runs this script. Any
job in jobs/*.json whose UTC time has arrived is published to Instagram
straight from the runner: the media files already live in this repo (public
raw URLs — the same hop NEXUS uses locally), so the PC can be OFF entirely.

Contract with the PC side (fortressmind/cloud.py):
  jobs/<id>.json      {id, when_utc, type: "post"|"reel", caption, tags[],
                       story: bool, media: {feed, story_video?}, quote, author,
                       slug, queue_id}
  jobs/<id>/...       the media files the job points at (repo-relative paths)
  results/<id>.json   written here after an attempt: {ok, media_id, permalink,
                       story_id, story_reel, error?, published_at}
  failed jobs move their json to failed/<id>.json (no infinite retries) —
  media is kept so the PC can inspect; successful jobs delete their media.

Secrets (repo -> Settings -> Secrets and variables -> Actions):
  IG_ACCESS_TOKEN, IG_USER_ID
Stdlib only — no pip installs on the runner.
"""
import json
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

IG_API = "https://graph.instagram.com/v21.0"
TOK = os.environ.get("IG_ACCESS_TOKEN", "")
UID = os.environ.get("IG_USER_ID", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"


def _get_json(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post_json(url, timeout=60):
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _wait_finished(cid, tries=36):
    for _ in range(tries):
        try:
            st = _get_json(f"{IG_API}/{cid}?fields=status_code"
                           f"&access_token={TOK}").get("status_code")
            if st == "FINISHED":
                return True
            if st == "ERROR":
                return False
        except Exception:
            pass
        time.sleep(5)
    return False


def _publish(create_url, tries=6, pause=5):
    cid = _post_json(create_url, timeout=120).get("id")
    if not cid:
        return None, None
    return cid, None


def _media_publish(cid):
    pub = (f"{IG_API}/{UID}/media_publish?"
           + urllib.parse.urlencode({"creation_id": cid, "access_token": TOK}))
    for _ in range(6):
        try:
            mid = _post_json(pub).get("id")
            if mid:
                return mid
        except urllib.error.HTTPError:
            time.sleep(5)
    return None


def _story_video(raw_url):
    try:
        sc = (f"{IG_API}/{UID}/media?"
              + urllib.parse.urlencode({"media_type": "STORIES",
                                        "video_url": raw_url,
                                        "access_token": TOK}))
        scid = _post_json(sc, timeout=120).get("id")
        if scid and _wait_finished(scid, tries=24):
            return _media_publish(scid)
    except Exception as e:
        print(f"  story video failed: {e}")
    return None


def _story_image(raw_url):
    try:
        sc = (f"{IG_API}/{UID}/media?"
              + urllib.parse.urlencode({"media_type": "STORIES",
                                        "image_url": raw_url,
                                        "access_token": TOK}))
        scid = _post_json(sc).get("id")
        if scid:
            return _media_publish(scid)
    except Exception as e:
        print(f"  story image failed: {e}")
    return None


def _caption(job):
    cap = (job.get("caption") or "").rstrip()
    tags = job.get("tags") or []
    if tags:
        cap += "\n\n" + " ".join("#" + str(t).lstrip("#") for t in tags)
    return cap


def run_job(job):
    """Publish one due job. Returns the result dict (ok True/False)."""
    media = job.get("media") or {}
    feed_rel = media.get("feed")
    if not feed_rel or not os.path.exists(feed_rel):
        return {"ok": False, "error": f"media missing in repo: {feed_rel}"}
    feed_url = f"{RAW}/{feed_rel}"
    story_rel = media.get("story_video")
    story_url = (f"{RAW}/{story_rel}"
                 if story_rel and os.path.exists(story_rel) else None)
    cap = _caption(job)

    if job.get("type") == "reel":
        create = (f"{IG_API}/{UID}/media?"
                  + urllib.parse.urlencode({"media_type": "REELS",
                                            "video_url": feed_url,
                                            "caption": cap,
                                            "access_token": TOK}))
        cid = _post_json(create, timeout=120).get("id")
        if not cid or not _wait_finished(cid):
            return {"ok": False, "error": "reel container failed processing"}
        mid = _media_publish(cid)
        if not mid:
            return {"ok": False, "error": "reel publish failed"}
        sid = _story_video(story_url or feed_url) if job.get("story") else None
        story_reel = bool(sid)
    else:
        create = (f"{IG_API}/{UID}/media?"
                  + urllib.parse.urlencode({"image_url": feed_url,
                                            "caption": cap,
                                            "access_token": TOK}))
        cid = _post_json(create).get("id")
        if not cid:
            return {"ok": False, "error": "no creation id"}
        mid = None
        for _ in range(6):                    # IG needs a moment to fetch
            try:
                mid = _media_publish(cid)
                if mid:
                    break
            except Exception:
                pass
            time.sleep(4)
        if not mid:
            return {"ok": False, "error": "image publish failed"}
        sid, story_reel = None, False
        if job.get("story"):
            if story_url:
                sid = _story_video(story_url)
                story_reel = bool(sid)
            if not sid:                        # fallback: image story
                sid = _story_image(f"{RAW}/{media.get('master', feed_rel)}"
                                   if media.get("master") else feed_url)
    permalink = None
    try:
        permalink = _get_json(f"{IG_API}/{mid}?fields=permalink"
                              f"&access_token={TOK}").get("permalink")
    except Exception:
        pass
    return {"ok": True, "media_id": mid, "permalink": permalink,
            "story_id": sid, "story_reel": story_reel}


def main():
    if not TOK or not UID:
        print("IG_ACCESS_TOKEN / IG_USER_ID secrets not set — nothing to do.")
        return 0
    os.makedirs("results", exist_ok=True)
    os.makedirs("failed", exist_ok=True)
    now = datetime.now(timezone.utc)
    ran = 0
    for f in sorted(os.listdir("jobs")) if os.path.isdir("jobs") else []:
        if not f.endswith(".json"):
            continue
        path = os.path.join("jobs", f)
        try:
            job = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        due = job.get("when_utc", "")
        try:
            due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
            if due_dt.tzinfo is None:
                due_dt = due_dt.replace(tzinfo=timezone.utc)
        except Exception:
            print(f"skipping {f}: bad when_utc {due!r}")
            continue
        if due_dt > now:
            continue
        jid = job.get("id") or os.path.splitext(f)[0]
        print(f"publishing job {jid} (due {due}, type {job.get('type')})…")
        try:
            res = run_job(job)
        except Exception as e:
            res = {"ok": False, "error": str(e)[:300]}
        res["id"] = jid
        res["published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(os.path.join("results", jid + ".json"), "w", encoding="utf-8") as out:
            json.dump(res, out, ensure_ascii=False, indent=2)
        media_dir = os.path.join("jobs", jid)
        if res.get("ok"):
            os.remove(path)
            if os.path.isdir(media_dir):
                shutil.rmtree(media_dir, ignore_errors=True)
            print(f"  OK {res.get('permalink')}")
        else:
            shutil.move(path, os.path.join("failed", jid + ".json"))
            print(f"  FAILED: {res.get('error')} (media kept, no retry)")
        ran += 1
    print(f"done — {ran} job(s) attempted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
