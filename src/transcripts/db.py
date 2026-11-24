"""Short transactions; only the server opens the private database."""
from contextlib import closing, contextmanager
from pathlib import Path
import hashlib
import json
import re
import secrets
import sqlite3
import time

from .config import Settings
from .models import Result


class Conflict(ValueError):
    pass


class Missing(LookupError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, feed_url TEXT NOT NULL,
 subscribed INTEGER NOT NULL DEFAULT 1, share_allowed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS episodes (
 id TEXT PRIMARY KEY, feed_id TEXT NOT NULL REFERENCES feeds(id), title TEXT NOT NULL,
 audio_url TEXT NOT NULL, source_url TEXT, published TEXT, activity_at REAL,
 played INTEGER NOT NULL, progress REAL NOT NULL, deleted INTEGER NOT NULL,
 first_seen REAL NOT NULL, last_seen REAL NOT NULL, eligible INTEGER NOT NULL,
 auto_queue INTEGER NOT NULL DEFAULT 0, share_token TEXT
);
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id),
 observed_at REAL NOT NULL, activity_at REAL, played INTEGER, progress REAL,
 deleted INTEGER, fingerprint TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_runs (
 id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
 source TEXT NOT NULL, status TEXT NOT NULL, code TEXT,
 episodes INTEGER DEFAULT 0, queued INTEGER DEFAULT 0, held INTEGER DEFAULT 0,
 skipped INTEGER DEFAULT 0, snapshot_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, episode_id TEXT UNIQUE NOT NULL REFERENCES episodes(id),
 status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL, lease_token TEXT, lease_until REAL,
 last_error TEXT, created_at REAL NOT NULL, completed_at REAL, result_digest TEXT
);
CREATE INDEX IF NOT EXISTS jobs_available ON jobs(status, next_at);
CREATE TABLE IF NOT EXISTS transcripts (
 episode_id TEXT PRIMARY KEY REFERENCES episodes(id), model TEXT NOT NULL,
 language TEXT NOT NULL, audio_sha256 TEXT NOT NULL, software TEXT NOT NULL,
 options_json TEXT NOT NULL, completed_at REAL NOT NULL, duration REAL NOT NULL,
 words INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
 id INTEGER PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES transcripts(episode_id),
 sequence INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL,
 UNIQUE(episode_id, sequence)
);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
 text, title, podcast, episode_id UNINDEXED, start UNINDEXED, sequence UNINDEXED,
 tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS sharing_events (
 id INTEGER PRIMARY KEY, episode_id TEXT NOT NULL, action TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, seen_at REAL NOT NULL);
PRAGMA user_version=1;
"""

EPISODE_SELECT = """
SELECT e.id, e.feed_id, e.title, f.title AS podcast, e.published, e.activity_at,
 e.played, e.progress, e.deleted, e.eligible, e.first_seen, e.last_seen,
 CASE j.status WHEN 'done' THEN 'ready' WHEN 'leased' THEN 'working'
 ELSE coalesce(j.status, 'history') END AS status,
 j.attempt, j.last_error, j.next_at, t.duration, t.words, t.completed_at,
 t.model, t.language
FROM episodes e JOIN feeds f ON f.id=e.feed_id
LEFT JOIN jobs j ON j.episode_id=e.id
LEFT JOIN transcripts t ON t.episode_id=e.id
"""


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.db_path

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings.public_db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("Database schema is newer than this application")
            db.executescript(SCHEMA)
        with sqlite3.connect(self.settings.public_db_path) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("CREATE TABLE IF NOT EXISTS shares (token TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.path.chmod(0o600)
        self.settings.public_db_path.chmod(0o600)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def list_podcasts(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""
                SELECT f.id, f.title, f.subscribed, f.share_allowed,
                count(e.id) AS episodes, count(t.episode_id) AS transcripts
                FROM feeds f LEFT JOIN episodes e ON e.feed_id=f.id
                LEFT JOIN transcripts t ON t.episode_id=e.id
                GROUP BY f.id ORDER BY f.title COLLATE NOCASE
            """)]

    def list_episodes(self, podcast_id="", status="", limit=50, offset=0, include_unplayed=False):
        conditions, args = [], []
        if not include_unplayed:
            conditions.append("(e.eligible=1 OR j.id IS NOT NULL)")
        if podcast_id:
            conditions.append("e.feed_id=?")
            args.append(podcast_id)
        if status:
            statuses = {"ready": "done", "working": "leased", "queued": "queued", "failed": "failed", "history": None}
            if status not in statuses:
                raise ValueError("Unknown episode status")
            conditions.append("j.status IS NULL" if status == "history" else "j.status=?")
            if status != "history":
                args.append(statuses[status])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as db:
            return [dict(r) for r in db.execute(EPISODE_SELECT + where +
                " ORDER BY coalesce(e.activity_at,e.first_seen) DESC, e.id LIMIT ? OFFSET ?",
                [*args, max(1, min(100, limit)), max(0, offset)])]

    def episode(self, episode_id):
        with self.connect() as db:
            row = db.execute(EPISODE_SELECT + " WHERE e.id=?", (episode_id,)).fetchone()
            if row is None:
                raise Missing("Episode not found")
            return dict(row)

    def reader(self, episode_id, offset=0, limit=None):
        """Without a limit, return the whole transcript (the web reader is one page)."""
        episode = self.episode(episode_id)
        offset = max(0, offset)
        with self.connect() as db:
            if limit is None:
                rows = db.execute("SELECT sequence,start,end,text FROM segments WHERE episode_id=? AND sequence>=? ORDER BY sequence",
                    (episode_id, offset))
            else:
                rows = db.execute("SELECT sequence,start,end,text FROM segments WHERE episode_id=? ORDER BY sequence LIMIT ? OFFSET ?",
                    (episode_id, max(1, min(500, limit)), offset))
            segments = [dict(r) for r in rows]
            count = db.execute("SELECT count(*) FROM segments WHERE episode_id=?", (episode_id,)).fetchone()[0]
            meta = db.execute("SELECT * FROM transcripts WHERE episode_id=?", (episode_id,)).fetchone()
        return {"episode": episode, "provenance": dict(meta) if meta else None,
                "segments": segments, "total_segments": count,
                "next_offset": offset + len(segments) if offset + len(segments) < count else None}

    def owner_episode(self, episode_id):
        with self.connect() as db:
            row = db.execute("SELECT share_token FROM episodes WHERE id=?", (episode_id,)).fetchone()
            if row is None:
                raise Missing("Episode not found")
            return dict(row)

    def list_shared(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""
                SELECT e.id, e.title, f.title AS podcast, e.share_token FROM episodes e
                JOIN feeds f ON f.id=e.feed_id WHERE e.share_token IS NOT NULL
                ORDER BY e.title COLLATE NOCASE""")]

    def search(self, query, podcast_id="", limit=40, offset=0, status=""):
        terms = re.findall(r"\w+", query[:500], re.UNICODE)[:20]
        if not terms:
            return []
        expression = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
        where, args = "", [expression]
        if podcast_id:
            where = " AND e.feed_id=?"
            args.append(podcast_id)
        if status:
            statuses = {"ready":"done","working":"leased","queued":"queued","failed":"failed","history":None}
            if status not in statuses:
                raise ValueError("Unknown episode status")
            where += " AND j.status IS NULL" if status=="history" else " AND j.status=?"
            if status!="history": args.append(statuses[status])
        with self.connect() as db:
            return [dict(r) for r in db.execute("""
                SELECT search.episode_id AS id, e.title, f.title AS podcast,
                search.start AS start, search.sequence AS sequence,
                snippet(search,0,'[',']',' … ',36) AS excerpt,
                CASE j.status WHEN 'done' THEN 'ready' WHEN 'leased' THEN 'working' ELSE coalesce(j.status,'history') END AS status
                FROM search JOIN episodes e ON e.id=search.episode_id
                JOIN feeds f ON f.id=e.feed_id
                LEFT JOIN transcripts t ON t.episode_id=e.id
                LEFT JOIN jobs j ON j.episode_id=e.id
                WHERE search MATCH ? AND (e.eligible=1 OR t.episode_id IS NOT NULL)
                """ + where + " ORDER BY bm25(search,1,4,2) LIMIT ? OFFSET ?",
                [*args, max(1, min(100, limit)), max(0, offset)])]

    def history(self, limit=100, offset=0):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""
                SELECT o.id,o.episode_id,e.title,f.title AS podcast,o.observed_at,
                o.activity_at,o.played,o.progress,o.deleted FROM observations o
                JOIN episodes e ON e.id=o.episode_id JOIN feeds f ON f.id=e.feed_id
                ORDER BY o.observed_at DESC,o.id DESC LIMIT ? OFFSET ?
            """, (max(1,min(100,limit)),max(0,offset)))]

    def status(self):
        now = time.time()
        cookie = self.settings.data_dir / "overcast.json"
        with self.connect() as db:
            counts = dict(db.execute("SELECT status,count(*) FROM jobs GROUP BY status").fetchall())
            latest = db.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 1").fetchone()
            last_overcast = db.execute("SELECT started_at,code FROM discovery_runs WHERE source='overcast' AND status!='running' ORDER BY id DESC LIMIT 1").fetchone()
            successful = db.execute("SELECT finished_at FROM discovery_runs WHERE status='ok' AND source='overcast' ORDER BY id DESC LIMIT 1").fetchone()
            worker = db.execute("SELECT max(seen_at) FROM workers").fetchone()[0]
            totals = db.execute("SELECT count(*),coalesce(sum(words),0),coalesce(sum(duration),0) FROM transcripts").fetchone()
            inventory = db.execute("SELECT count(*) FROM episodes").fetchone()[0]
            held = db.execute("SELECT count(*) FROM episodes e LEFT JOIN jobs j ON j.episode_id=e.id WHERE e.eligible=1 AND j.id IS NULL").fetchone()[0]
        return {"jobs": counts, "transcripts":totals[0], "words":totals[1], "audio_seconds":totals[2],
                "inventory":inventory, "unprocessed_history": held,
                "latest_discovery":dict(latest) if latest else None,
                "last_overcast_success":successful[0] if successful else None,
                "discovery_stale":not successful or now-successful[0]>100800,
                "worker_seen_at":worker, "worker_online":worker is not None and now-worker<180,
                "overcast_connected":cookie.exists(),
                "overcast_expired":cookie.exists() and last_overcast is not None and last_overcast[1]=="overcast_session_expired" and cookie.stat().st_mtime<=last_overcast[0]}

    def start_run(self, source):
        with self.connect(write=True) as db:
            return db.execute("INSERT INTO discovery_runs(started_at,source,status) VALUES(?,?,'running')", (time.time(),source)).lastrowid

    def fail_run(self, run_id, code):
        with self.connect(write=True) as db:
            db.execute("UPDATE discovery_runs SET status='error',code=?,finished_at=? WHERE id=?", (code,time.time(),run_id))

    def _queue(self, db, episode_id, now):
        return db.execute("""INSERT OR IGNORE INTO jobs(id,episode_id,status,next_at,created_at)
            VALUES(?,?,'queued',?,?)""", (secrets.token_hex(16),episode_id,now,now)).rowcount

    def backfill(self, limit=10, commit=False):
        if not 1 <= limit <= 100:
            raise ValueError("Backfill limit must be between 1 and 100")
        with self.connect(write=commit) as db:
            rows = db.execute("""SELECT e.id,e.title FROM episodes e LEFT JOIN jobs j ON j.episode_id=e.id
                WHERE e.eligible=1 AND j.id IS NULL ORDER BY coalesce(e.activity_at,0) DESC,e.id LIMIT ?""", (limit,)).fetchall()
            if commit:
                for row in rows:
                    self._queue(db,row["id"],time.time())
            return [dict(r) for r in rows]

    def queue_episode(self, episode_id):
        with self.connect(write=True) as db:
            if db.execute("SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone() is None:
                raise Missing("Episode not found")
            row = db.execute("SELECT status FROM jobs WHERE episode_id=?", (episode_id,)).fetchone()
            if row and row[0] != "failed":
                raise Conflict("Episode already queued or transcribed")
            if row:
                db.execute("UPDATE jobs SET status='queued',attempt=0,next_at=?,lease_token=NULL,lease_until=NULL,last_error=NULL WHERE episode_id=?", (time.time(),episode_id))
            else:
                self._queue(db,episode_id,time.time())

    def claim(self):
        now = time.time()
        with self.connect(write=True) as db:
            db.execute("INSERT INTO workers VALUES('mini',?) ON CONFLICT(id) DO UPDATE SET seen_at=excluded.seen_at", (now,))
            # Recover a worker that died without reporting a failure.
            db.execute("""UPDATE jobs SET status=CASE WHEN attempt>=? THEN 'failed' ELSE 'queued' END,
                next_at=?,last_error='lease_expired',lease_until=NULL,lease_token=NULL
                WHERE status='leased' AND lease_until<=?""", (self.settings.max_attempts,now,now))
            # One active transcription across all clients.
            if db.execute("SELECT 1 FROM jobs WHERE status='leased' LIMIT 1").fetchone():
                return None
            row = db.execute("""SELECT j.id,j.episode_id,e.audio_url,e.title FROM jobs j
                JOIN episodes e ON e.id=j.episode_id WHERE j.status='queued' AND j.next_at<=?
                ORDER BY j.created_at,j.id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            db.execute("UPDATE jobs SET status='leased',attempt=attempt+1,lease_token=?,lease_until=? WHERE id=?", (token,now+self.settings.lease_seconds,row["id"]))
            return {**dict(row), "lease_token":token,"lease_seconds":self.settings.lease_seconds}

    def _assert_lease(self, db, job_id, token):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "leased" or not secrets.compare_digest(row["lease_token"] or "",token) or row["lease_until"]<=time.time():
            raise Conflict("Lease expired or replaced")
        return row

    def heartbeat(self, job_id, token):
        with self.connect(write=True) as db:
            self._assert_lease(db,job_id,token)
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time()+self.settings.lease_seconds,job_id))
            db.execute("INSERT INTO workers VALUES('mini',?) ON CONFLICT(id) DO UPDATE SET seen_at=excluded.seen_at", (time.time(),))

    def fail_job(self, job_id, token, code):
        with self.connect(write=True) as db:
            row = self._assert_lease(db,job_id,token)
            # Silence or music won't transcribe differently on a retry.
            status = "failed" if code=="no_speech" or row["attempt"] >= self.settings.max_attempts else "queued"
            db.execute("UPDATE jobs SET status=?,next_at=?,last_error=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                (status,time.time()+60*2**row["attempt"],code,job_id))

    def complete(self, job_id, result: Result):
        content = result.model_dump(exclude={"lease_token"})
        digest = hashlib.sha256(json.dumps(content,sort_keys=True,separators=(",", ":")).encode()).hexdigest()
        now = time.time()
        with self.connect(write=True) as db:
            previous = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if previous and previous["status"] == "done":
                if secrets.compare_digest(previous["lease_token"] or "",result.lease_token) and previous["result_digest"] == digest:
                    return {"receipt":digest,"episode_id":previous["episode_id"],"replayed":True}
                raise Conflict("A different result has already completed this episode")
            row = self._assert_lease(db,job_id,result.lease_token)
            eid = row["episode_id"]
            meta = db.execute("SELECT e.title,f.title AS podcast FROM episodes e JOIN feeds f ON f.id=e.feed_id WHERE e.id=?", (eid,)).fetchone()
            duration = max(s.end for s in result.segments)
            words = sum(len(s.text.split()) for s in result.segments)
            db.execute("INSERT INTO transcripts VALUES(?,?,?,?,?,?,?,?,?)", (eid,result.model,result.language,result.audio_sha256,result.software,json.dumps(result.options),now,duration,words))
            db.execute("DELETE FROM search WHERE episode_id=?", (eid,))
            db.execute("INSERT INTO search VALUES('',?,?,?,?,?)", (meta["title"],meta["podcast"],eid,0,-1))
            for i,s in enumerate(result.segments):
                db.execute("INSERT INTO segments(episode_id,sequence,start,end,text) VALUES(?,?,?,?,?)", (eid,i,s.start,s.end,s.text.strip()))
                db.execute("INSERT INTO search VALUES(?,?,?,?,?,?)", (s.text.strip(),"","",eid,s.start,i))
            db.execute("UPDATE jobs SET status='done',completed_at=?,result_digest=?,lease_until=NULL,last_error=NULL WHERE id=?", (now,digest,job_id))
            return {"receipt":digest,"episode_id":eid,"replayed":False}

    def share(self, episode_id, publish):
        with self.connect(write=True) as db:
            db.execute("ATTACH DATABASE ? AS public", (str(self.settings.public_db_path),))
            row = db.execute("SELECT e.*, f.title AS podcast FROM episodes e JOIN feeds f ON f.id=e.feed_id WHERE e.id=?", (episode_id,)).fetchone()
            if row is None:
                raise Missing("Episode not found")
            if row["share_token"]:
                db.execute("DELETE FROM public.shares WHERE token=?", (row["share_token"],))
            token = None
            if publish:
                if not db.execute("SELECT 1 FROM transcripts WHERE episode_id=?", (episode_id,)).fetchone():
                    raise Conflict("Only completed transcripts can be shared")
                token = secrets.token_urlsafe(32)
                segments = [dict(r) for r in db.execute("SELECT start,end,text FROM segments WHERE episode_id=? ORDER BY sequence", (episode_id,))]
                # Only Overcast's public episode page is copied; feed/audio URLs can carry credentials.
                source = row["source_url"] or ""
                link = source if source.startswith("https://overcast.fm/") else None
                payload = {"title":row["title"],"podcast":row["podcast"],"published":row["published"],"link":link,"segments":segments}
                db.execute("INSERT INTO public.shares VALUES(?,?)", (token,json.dumps(payload)))
            db.execute("UPDATE episodes SET share_token=? WHERE id=?", (token,episode_id))
            db.execute("INSERT INTO sharing_events(episode_id,action,at) VALUES(?,?,?)", (episode_id,"publish" if publish else "revoke",time.time()))
            return token

    def backup(self, destination: Path):
        destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if destination.resolve() in {self.path.resolve(), self.settings.public_db_path.resolve()}:
            raise ValueError("Backup destination must be a new file")
        if destination.exists():
            raise ValueError("Refusing to overwrite an existing backup")
        try:
            with self.connect() as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        destination.chmod(0o600)
        return destination
