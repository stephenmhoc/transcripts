"""Overcast's unofficial extended export is a state snapshot, not a listen log."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import fcntl
import hashlib
import json
import math
import os
import time

from defusedxml import ElementTree
import httpx

from .db import Store, Conflict

EXPORT_URL = "https://overcast.fm/account/export_opml/extended"
MAX_EXPORT = 20_000_000


class DiscoveryError(ValueError):
    pass


def parse_date(value):
    if not value:
        return None
    try:
        if str(value).replace(".","",1).isdigit():
            stamp = float(value)
        else:
            try:
                date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None  # Do not guess the user's timezone.
            stamp = date.timestamp()
        if not math.isfinite(stamp) or stamp < 946684800 or stamp > time.time() + 300:
            return None
        return stamp
    except (ValueError, TypeError, OverflowError):
        return None


def parse_export(raw: bytes):
    if len(raw) > MAX_EXPORT:
        raise DiscoveryError("export_too_large")
    try:
        root = ElementTree.fromstring(raw)
    except Exception as exc:
        raise DiscoveryError("invalid_xml") from exc
    if root.tag != "opml" or root.find("body") is None:
        raise DiscoveryError("not_overcast_export")
    all_episodes = root.findall(".//outline[@type='podcast-episode']")
    feeds_container = root.find("./body/outline[@text='feeds']")
    if not all_episodes and feeds_container is None:
        raise DiscoveryError("extended_export_required")
    feeds, episodes, skipped = [], [], 0
    seen = set()
    for feed in root.findall(".//outline[@type='rss']"):
        attr = feed.attrib
        if not attr.get("overcastId", "").isdigit() or not attr.get("xmlUrl"):
            skipped += 1 + len(feed.findall("./outline[@type='podcast-episode']"))
            continue
        fid = attr["overcastId"]
        feeds.append({"id":fid,"title":attr.get("title") or attr.get("text") or "Untitled podcast",
                      "feed_url":attr["xmlUrl"],"subscribed":attr.get("subscribed","1")=="1"})
        for episode in feed.findall("./outline[@type='podcast-episode']"):
            ep = episode.attrib
            try:
                eid = ep["overcastId"]
                progress = float(ep.get("progress", "0"))
                if not eid.isdigit() or eid in seen or not ep.get("enclosureUrl") or not math.isfinite(progress) or not 0 <= progress <= 172800:
                    raise ValueError()
                if ep.get("played","0") not in {"0","1"}:
                    raise ValueError()
            except (ValueError, KeyError):
                skipped += 1
                continue
            seen.add(eid)
            published = parse_date(ep.get("pubDate"))
            episodes.append({"id":eid,"feed_id":fid,"title":ep.get("title") or ep.get("text") or "Untitled episode",
                "audio_url":ep["enclosureUrl"],  # Signed queries must remain byte-for-byte intact.
                "source_url":ep.get("overcastUrl",""),
                "published":datetime.fromtimestamp(published,timezone.utc).isoformat() if published else None,
                "activity_at":parse_date(ep.get("userUpdatedDate")),
                "played":ep.get("played","0")=="1","progress":progress,
                "deleted":ep.get("userDeleted","0")=="1"})
    if all_episodes and not episodes:
        raise DiscoveryError("episode_schema_changed")
    return feeds, episodes, skipped


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open("a") as handle:
        os.chmod(path,0o600)
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Conflict("Discovery already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN)


def import_export(store: Store, raw: bytes, source="file", run_id=None):
    run_id = run_id or store.start_run(source)
    try:
        feeds, episodes, skipped = parse_export(raw)
        now = time.time()
        recent_cutoff = now - store.settings.initial_days*86400
        with store.connect(write=True) as db:
            first = db.execute("SELECT value FROM state WHERE key='baseline_at'").fetchone() is None
            for feed in feeds:
                db.execute("""INSERT INTO feeds(id,title,feed_url,subscribed) VALUES(:id,:title,:feed_url,:subscribed)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,feed_url=excluded.feed_url,subscribed=excluded.subscribed""",feed)
            for ep in episodes:
                previous = db.execute("SELECT * FROM episodes WHERE id=?",(ep["id"],)).fetchone()
                eligible = ep["played"] or ep["progress"] >= store.settings.min_progress
                recent = ep["activity_at"] is not None and recent_cutoff <= ep["activity_at"] <= now+300
                transitioned = previous is not None and ((ep["played"] and not previous["played"]) or ep["progress"] >= previous["progress"]+30)
                auto = eligible and ((not previous and recent) or (not first and transitioned))
                values = {**ep,"now":now,"eligible":int(eligible),"auto_queue":int(auto)}
                db.execute("""INSERT INTO episodes(id,feed_id,title,audio_url,source_url,published,activity_at,played,
                    progress,deleted,first_seen,last_seen,eligible,auto_queue)
                    VALUES(:id,:feed_id,:title,:audio_url,:source_url,:published,:activity_at,:played,
                    :progress,:deleted,:now,:now,:eligible,:auto_queue)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,audio_url=excluded.audio_url,
                    source_url=excluded.source_url,published=excluded.published,activity_at=excluded.activity_at,
                    played=excluded.played,progress=excluded.progress,deleted=excluded.deleted,last_seen=excluded.last_seen,
                    eligible=excluded.eligible,auto_queue=max(episodes.auto_queue,excluded.auto_queue)""",values)
                fingerprint = hashlib.sha256(json.dumps({k:ep[k] for k in ("id","played","progress","deleted","activity_at")},sort_keys=True).encode()).hexdigest()
                db.execute("""INSERT OR IGNORE INTO observations(episode_id,observed_at,activity_at,played,progress,deleted,fingerprint)
                    VALUES(?,?,?,?,?,?,?)""",(ep["id"],now,ep["activity_at"],ep["played"],ep["progress"],ep["deleted"],fingerprint))
                has_search = db.execute("SELECT 1 FROM search WHERE episode_id=? LIMIT 1",(ep["id"],)).fetchone()
                if not has_search:
                    podcast = db.execute("SELECT title FROM feeds WHERE id=?",(ep["feed_id"],)).fetchone()[0]
                    db.execute("INSERT INTO search VALUES('',?,?,?,?,?)",(ep["title"],podcast,ep["id"],0,-1))
                else:
                    db.execute("UPDATE search SET title=?,podcast=(SELECT title FROM feeds WHERE id=?) WHERE episode_id=? AND sequence=-1",(ep["title"],ep["feed_id"],ep["id"]))
            if first:
                db.execute("INSERT INTO state VALUES('baseline_at',?)",(str(now),))
            candidates = db.execute("""SELECT e.id FROM episodes e LEFT JOIN jobs j ON j.episode_id=e.id
                WHERE e.auto_queue=1 AND e.eligible=1 AND j.id IS NULL
                ORDER BY coalesce(e.activity_at,e.first_seen) DESC,e.id LIMIT ?""",(store.settings.queue_limit,)).fetchall()
            queued = sum(store._queue(db,row[0],now) for row in candidates)
            held = db.execute("SELECT count(*) FROM episodes e LEFT JOIN jobs j ON j.episode_id=e.id WHERE e.eligible=1 AND j.id IS NULL").fetchone()[0]
            db.execute("""UPDATE discovery_runs SET finished_at=?,status='ok',episodes=?,queued=?,held=?,
                skipped=?,snapshot_sha256=? WHERE id=?""",(now,len(episodes),queued,held,skipped,hashlib.sha256(raw).hexdigest(),run_id))
        return {"episodes":len(episodes),"queued":queued,"held":held,"skipped":skipped,"baseline":first}
    except Exception as exc:
        store.fail_run(run_id,str(exc) if isinstance(exc,DiscoveryError) else "import_failed")
        raise


def save_cookie(path: Path, cookie: str):
    if not cookie.strip() or len(cookie)>4096 or any(c in cookie for c in "\r\n;"):
        raise ValueError("Invalid Overcast cookie")
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w") as f:
        json.dump({"o":cookie.strip()},f)


def login(store: Store, email: str, password: str):
    try:
        with httpx.Client(timeout=30,follow_redirects=False) as client:
            response = client.post("https://overcast.fm/login?then=account",data={"email":email,"password":password})
            cookie = client.cookies.get("o")
            if not cookie or response.status_code not in {200,302,303}:
                raise DiscoveryError("overcast_login_failed")
            save_cookie(store.settings.data_dir / "overcast.json",cookie)
    except httpx.HTTPError as exc:
        raise DiscoveryError("overcast_connection_failed") from exc


def sync(store: Store, force=False):
    with file_lock(store.settings.data_dir / "sync.lock"):
        with store.connect() as db:
            previous = db.execute("SELECT started_at,code FROM discovery_runs WHERE source='overcast' AND (code IS NULL OR code!='overcast_not_connected') ORDER BY id DESC LIMIT 1").fetchone()
        # Even expired sessions and failures respect the provider's polling limit,
        # except that one check is allowed right after the owner reconnects.
        path = store.settings.data_dir / "overcast.json"
        reconnected = previous and previous[1]=="overcast_session_expired" and path.exists() and path.stat().st_mtime>previous[0]
        if previous and time.time()-previous[0] < store.settings.poll_seconds and not force and not reconnected:
            return {"status":"not_due"}
        run_id = store.start_run("overcast")
        try:
            if not path.exists():
                raise DiscoveryError("overcast_not_connected")
            cookies = json.loads(path.read_text())
            if not cookies.get("o"):
                raise DiscoveryError("overcast_not_connected")
            with httpx.Client(timeout=60,follow_redirects=False,cookies={"o":cookies["o"],"qr":"-"}) as client:
                with client.stream("GET",EXPORT_URL) as response:
                    if response.status_code in {301,302,303,401,403}:
                        raise DiscoveryError("overcast_session_expired")
                    if response.status_code == 429:
                        raise DiscoveryError("overcast_rate_limited")
                    if response.status_code != 200:
                        raise DiscoveryError("overcast_unavailable")
                    chunks, size = [],0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_EXPORT:
                            raise DiscoveryError("export_too_large")
                        chunks.append(chunk)
            return import_export(store,b"".join(chunks),source="overcast",run_id=run_id)
        except Exception as exc:
            store.fail_run(run_id,str(exc) if isinstance(exc,DiscoveryError) else "overcast_fetch_failed")
            raise
