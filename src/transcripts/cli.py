import argparse
from datetime import datetime,timezone
from pathlib import Path
import getpass
import json
import logging
import os
import secrets
import shutil
import sys

from dotenv import load_dotenv
from .config import Settings,quiet_http_logs
from .db import Store
from .discovery import import_export, sync, login, save_cookie


def init_file(path, origin="http://127.0.0.1:8765", data="data", demo=False):
    if path.exists():
        raise ValueError("Configuration already exists; refusing to overwrite")
    tokens = {key:(f"local-demo-{key.lower()}-"+"0"*32 if demo else secrets.token_urlsafe(48)) for key in ("OWNER_TOKEN","MCP_TOKEN","WORKER_TOKEN","SESSION_SECRET")}
    values = {"APP_ORIGIN":origin,"DATA_DIR":str(Path(data).resolve()),**tokens,
              "DISCOVERY_ENABLED":"false" if demo else "true","PUBLIC_BASE_URL":"","DEMO_MODE":"true" if demo else "false"}
    path.parent.mkdir(parents=True,exist_ok=True)
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as output:
        for key,value in values.items():
            output.write(key+"="+value+"\n")
    return values


def seed_demo(settings):
    from .models import Result
    store=Store(settings)
    store.initialize()
    if store.status()["inventory"]:
        raise ValueError("Demo database is not empty")
    now=datetime.now(timezone.utc).isoformat()
    shows=[("1","Field Notes","A durable place for fleeting ideas"),
           ("2","The Long View","What makes a city feel like home?"),
           ("3","Working Slowly","The useful art of paying attention"),
           ("4","Between Pages","On reading, remembering, and returning"),
           ("5","Field Notes","A walk through the everyday")]
    from xml.sax.saxutils import escape
    feeds="".join(f'<outline type="rss" overcastId="{i}" title="{escape(show)}" xmlUrl="https://example.org/{i}/feed"><outline type="podcast-episode" overcastId="{i}01" title="{escape(title)}" played="1" progress="900" enclosureUrl="https://example.org/audio.mp3" userUpdatedDate="{now}"/></outline>' for i,show,title in shows)
    raw=f'<opml version="2.0"><body><outline text="feeds">{feeds}</outline></body></opml>'.encode()
    import_export(store,raw)
    passages=[
        "This is sample content for the demo library. A good conversation often leaves us with one sentence we want to keep. The trouble is that we rarely know which sentence it is until the moment has already passed.",
        "An archive becomes useful when returning to it is easy. We can build a durable place for fleeting ideas without turning the act of listening into another task. The collection should quietly do its work in the background.",
        "Search gives us a way back. A phrase, a name, or a half-remembered question can lead to the original passage, with enough surrounding context to understand what the speaker meant.",
        "There is value in keeping the source close to the words. Timestamps make a transcript more useful because they tell us where a thought appeared in the conversation, rather than treating every sentence as a detached quotation.",
        "The best tools make room for attention. A small system that works steadily can be more useful than a complicated one that constantly asks to be managed. What matters is whether we can find the idea when we need it again.",
        "These paragraphs are written for this demonstration. They are not quotations from an actual podcast, and the fictional show names stand in for a future personal library.",
    ]
    for i in range(4):
        job=store.claim()
        store.complete(job["id"],Result(lease_token=job["lease_token"],model="demo/sample-content",language="en",audio_sha256="0"*64,software="demo",options={"synthetic":True},segments=[{"start":n*35,"end":n*35+32,"text":text} for n,text in enumerate(passages)]))


def main():
    parser=argparse.ArgumentParser(description="Private podcast transcripts")
    parser.add_argument("--env",default=os.environ.get("TRANSCRIPTS_ENV",".env"),help="Private configuration file")
    sub=parser.add_subparsers(dest="command",required=True)
    p=sub.add_parser("init"); p.add_argument("--origin",default="http://127.0.0.1:8765");p.add_argument("--data-dir",default="data")
    p=sub.add_parser("serve");p.add_argument("--host",default="127.0.0.1");p.add_argument("--port",type=int,default=8765)
    p=sub.add_parser("public");p.add_argument("--host",default="127.0.0.1");p.add_argument("--port",type=int,default=8767);p.add_argument("--directory",default=os.environ.get("PUBLIC_DATA_DIR","data/public"))
    p=sub.add_parser("worker");p.add_argument("--url",default=None);p.add_argument("--once",action="store_true");p.add_argument("--work-dir",default="var/worker");p.add_argument("--model",default=None)
    p=sub.add_parser("import");p.add_argument("file",type=Path)
    p=sub.add_parser("sync");p.add_argument("--force",action="store_true",help="Explicitly bypass the local 12-hour limit; use sparingly")
    p=sub.add_parser("overcast-login");p.add_argument("--cookie",action="store_true",help="Read an existing session cookie with hidden input")
    sub.add_parser("status");sub.add_parser("doctor")
    p=sub.add_parser("backfill");p.add_argument("--limit",type=int,default=10);p.add_argument("--commit",action="store_true")
    p=sub.add_parser("backup");p.add_argument("destination",type=Path)
    p=sub.add_parser("demo");p.add_argument("--data-dir",default="var/demo");p.add_argument("--port",type=int,default=8766)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
    quiet_http_logs()
    os.umask(0o077)
    try:
        if args.command=="public":
            import uvicorn
            from .web import create_public_app
            uvicorn.run(create_public_app(args.directory),host=args.host,port=args.port,proxy_headers=False,access_log=False)
            return
        if args.command=="init":
            init_file(Path(args.env),args.origin,args.data_dir)
            print(f"Created private configuration at {Path(args.env).resolve()}. Secrets were not printed.")
            return
        if args.command=="demo":
            if args.env==".env":
                args.env=".env.demo"
            values=init_file(Path(args.env),f"http://127.0.0.1:{args.port}",args.data_dir,demo=True)
            load_dotenv(args.env,override=True)
            seed_demo(Settings.from_env())
            print("Sample library created in a separate database. It contains fictional demo content only.")
            print("Demo owner key: "+values["OWNER_TOKEN"])
            print(f"Run: transcripts --env {args.env} serve --port {args.port}")
            return
        load_dotenv(args.env,override=True)
        if args.command=="worker":
            from .worker import run_worker,DEFAULT_MODEL
            run_worker(args.url or os.environ.get("WORKER_URL","http://127.0.0.1:8765"),os.environ.get("WORKER_TOKEN",""),Path(args.work_dir).resolve(),args.once,args.model or os.environ.get("WHISPER_MODEL",DEFAULT_MODEL))
            return
        settings=Settings.from_env()
        store=Store(settings);store.initialize()
        if args.command=="serve":
            import uvicorn
            from .web import create_app
            uvicorn.run(create_app(settings),host=args.host,port=args.port,proxy_headers=False,access_log=False)
        elif args.command=="status":
            print(json.dumps(store.status(),indent=2))
        elif args.command=="doctor":
            with store.connect() as db:
                integrity=db.execute("PRAGMA integrity_check").fetchone()[0]
            import importlib.util
            print(json.dumps({"database":integrity,"disk_free_gb":round(shutil.disk_usage(settings.data_dir).free/1024**3,1),"ffmpeg":bool(shutil.which("ffmpeg")),"mlx_whisper_installed":bool(importlib.util.find_spec("mlx_whisper")),"pipeline":store.status()},indent=2))
        elif args.command=="import":
            from .discovery import file_lock, MAX_EXPORT
            with args.file.open("rb") as source:
                raw=source.read(MAX_EXPORT+1)
            with file_lock(settings.data_dir/"sync.lock"):
                print(json.dumps(import_export(store,raw),indent=2))
        elif args.command=="sync":
            print(json.dumps(sync(store,args.force),indent=2))
        elif args.command=="overcast-login":
            if args.cookie:
                save_cookie(settings.data_dir/"overcast.json",getpass.getpass("Overcast o cookie (hidden): "))
            else:
                login(store,input("Overcast email: "),getpass.getpass("Overcast password (never stored): "))
            print("Session saved privately. The server will discover automatically within a minute.")
        elif args.command=="backfill":
            print(json.dumps({"committed":args.commit,"selection":"Most recent eligible activity without an existing job","episodes":store.backfill(args.limit,args.commit)},indent=2))
            if not args.commit:
                print("Preview only. Add --commit to enqueue this bounded selection.")
        elif args.command=="backup":
            print(store.backup(args.destination))
    except (ValueError,OSError) as exc:
        # URLs/passwords from library errors are deliberately not printed.
        print(f"Cannot complete {args.command}: {str(exc) if isinstance(exc,ValueError) else type(exc).__name__}",file=sys.stderr)
        raise SystemExit(1)


if __name__=="__main__":
    main()
