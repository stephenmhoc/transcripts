from contextlib import asynccontextmanager, suppress, closing
from datetime import datetime,timezone
from pathlib import Path
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
import httpx

from .auth import SecurityBoundary,COOKIE,csrf_token,token_matches,set_session
from .config import Settings,quiet_http_logs
from .db import Store,Missing,Conflict
from .discovery import sync, import_export, file_lock, login, save_cookie, DiscoveryError, MAX_EXPORT
from .mcp_server import create_mcp
from .models import Lease,Failure,Result
from .overcast_qr import QRLogin

ROOT = Path(__file__).parent
log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=ROOT/"templates")


def timestamp(value):
    seconds = int(float(value or 0))
    return f"{seconds//3600}:{seconds//60%60:02}:{seconds%60:02}" if seconds>=3600 else f"{seconds//60}:{seconds%60:02}"


def date_label(value):
    if not value:
        return "Not yet"
    try:
        date = datetime.fromtimestamp(value,timezone.utc) if isinstance(value,(int,float)) else datetime.fromisoformat(value.replace("Z","+00:00"))
        return date.astimezone(ZoneInfo(os.environ.get("DISPLAY_TIMEZONE","America/New_York"))).strftime("%b %d, %Y")
    except ValueError:
        return "Unknown date"


def relative(value):
    if not value:
        return "Not yet"
    delta = max(0,int(time.time()-value))
    if delta<60:
        return "Just now"
    if delta<3600:
        return f"{delta//60}m ago"
    if delta<86400:
        return f"{delta//3600}h ago"
    return f"{delta//86400}d ago"


templates.env.filters.update(timestamp=timestamp,date_label=date_label,relative=relative)


def redirect(path,message=""):
    return RedirectResponse(path+("?"+urlencode({"notice":message}) if message else ""),303)


async def public_reachable(settings):
    if settings.share_origin == settings.origin:
        return  # Served by this process under /p/.
    try:
        async with httpx.AsyncClient(timeout=5,follow_redirects=False,trust_env=False) as client:
            response = await client.get(settings.share_origin+"/healthz")
            if response.status_code != 200 or response.json().get("service") != "transcripts-public":
                raise ValueError()
    except (httpx.HTTPError,ValueError) as exc:
        raise Conflict("Public listener is not reachable; transcript remains private") from exc


SHARE_UNAVAILABLE = "This transcript is unavailable."
OFFSET = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?")


def parse_offset(value):
    """Seconds from ?t=754, 12m, 12m30s, 1h2m3s, 12:30 or 1:02:03; None if absent/invalid."""
    value = value.strip().lower()[:16]
    if not value:
        return None
    if ":" in value:
        parts = value.split(":")
        if len(parts)>3 or not all(part.isdigit() for part in parts):
            return None
        seconds = 0
        for part in parts:
            seconds = seconds*60+int(part)
        return seconds
    match = OFFSET.fullmatch(value)
    if not match or not any(match.groups()):
        return None
    hours,minutes,seconds = (int(group or 0) for group in match.groups())
    return hours*3600+minutes*60+seconds


def anchored(segments):
    """Give each passage a time anchor (12m34s) and the minute anchors (12m) it contains."""
    out, used = [], set()
    for segment in segments:
        start = int(float(segment["start"]))
        anchor = f"{start//60}m{start%60}s"
        while anchor in used:
            anchor += "-"
        used.add(anchor)
        out.append({**segment,"anchor":anchor,"minutes":[]})
    minute = 0
    for index,segment in enumerate(out):
        following = out[index+1]["start"] if index+1<len(out) else float(segment["end"])+0.001
        # Minute m belongs to the passage being spoken at m:00 (the first passage covers 0m).
        while minute*60 < following:
            segment["minutes"].append(f"{minute}m")
            minute += 1
    return out


def shared_page(request, path, token):
    """Render one frozen public copy; never touches the private library."""
    if len(token)!=43 or not all(c.isalnum() or c in "_-" for c in token):
        return PlainTextResponse(SHARE_UNAVAILABLE,404)
    try:
        # mode=ro permits DELETE-journal readers and never creates a missing DB.
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=10)) as db:
            row = db.execute("SELECT payload FROM shares WHERE token=?",(token,)).fetchone()
    except sqlite3.Error:
        return PlainTextResponse(SHARE_UNAVAILABLE,503)
    if row is None:
        return PlainTextResponse(SHARE_UNAVAILABLE,404)
    share = json.loads(row[0])
    segments = anchored(share["segments"])
    wanted = parse_offset(request.query_params.get("t",""))
    if wanted is not None and segments:
        # Jump to the passage being spoken at that moment.
        target = next((seg for seg in reversed(segments) if seg["start"]<=wanted),segments[0])
        return RedirectResponse(f"/p/{token}#{target['anchor']}",303)
    return templates.TemplateResponse(request=request,name="public.html",context={"request":request,**share,"segments":segments})


def public_stylesheet():
    # Served under /p/ so one path rule exposes everything a shared page needs.
    return FileResponse(ROOT/"static"/"public.css",media_type="text/css")


def create_app(settings=None):
    quiet_http_logs()
    settings = settings or Settings.from_env()
    settings.validate()
    store = Store(settings)
    store.initialize()
    server,mcp_app = create_mcp(store)
    qr_login = QRLogin(settings.data_dir/"overcast.json")

    async def poll():
        while True:
            if (settings.data_dir/"overcast.json").exists():
                try:
                    await asyncio.to_thread(sync,store)
                except Exception:
                    log.warning("Overcast discovery failed; see private pipeline status")
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app):
        lock = file_lock(settings.data_dir/"scheduler.lock") if settings.discovery_enabled else None
        if lock:
            lock.__enter__()  # Fail startup if a second scheduler would be created.
        task = None
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                if settings.discovery_enabled:
                    task = asyncio.create_task(poll())
                yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await qr_login.close()
            if lock:
                lock.__exit__(None,None,None)

    app = FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store = store
    app.state.settings = settings
    app.state.public_probe = public_reachable
    app.add_middleware(SecurityBoundary,settings=settings)
    app.mount("/static",StaticFiles(directory=ROOT/"static"),name="static")
    failed_logins = []

    def render(request,name,**values):
        session = request.cookies.get(COOKIE,"")
        base = {"request":request,"csrf":csrf_token(session,settings),"notice":request.query_params.get("notice",""),
                "origin":settings.origin,"demo":settings.demo}
        return templates.TemplateResponse(request=request,name=name,context={**base,**values})

    @app.exception_handler(Missing)
    async def missing(request,exc):
        return JSONResponse({"detail":str(exc)},404)

    @app.exception_handler(Conflict)
    async def conflict(request,exc):
        return JSONResponse({"detail":str(exc)},409)

    @app.exception_handler(DiscoveryError)
    async def discovery_error(request,exc):
        return JSONResponse({"detail":str(exc)},422)

    @app.get("/healthz")
    def health():
        with store.connect() as db:
            db.execute("SELECT 1")
        return {"service":"transcripts-private","status":"ok"}

    @app.get("/favicon.ico")
    def favicon():
        return PlainTextResponse("",status_code=204)

    @app.get("/login",response_class=HTMLResponse)
    def login_page(request: Request):
        return render(request,"login.html",error="")

    @app.post("/login")
    async def login_owner(request: Request):
        form = await request.form()
        failed_logins[:] = [t for t in failed_logins if time.time()-t<900]
        # Behind a proxy every client shares one address, so a hard lockout
        # would let anyone lock out the owner. Past the limit, attempts are
        # slowed instead; a correct key still signs in. Keys are 32+ random
        # characters, so the delay is a speed bump, not the protection.
        if len(failed_logins)>=10:
            await asyncio.sleep(2)
        if not token_matches(str(form.get("token","")),settings.owner_token):
            failed_logins.append(time.time())
            del failed_logins[:-100]
            return render(request,"login.html",error="That access key was not recognized.")
        return set_session(redirect("/"),settings)

    @app.post("/logout")
    def logout():
        # Behind Access the identity would sign straight back in; end that session too.
        response = redirect("/cdn-cgi/access/logout" if settings.access_enabled else "/login")
        response.delete_cookie(COOKIE)
        return response

    @app.get("/",response_class=HTMLResponse)
    def library(request: Request,q: str="",podcast: str="",status: str="",page: int=1):
        page = max(1,page)
        try:
            episodes = store.search(q,podcast,30,(page-1)*30,status=status) if q.strip() else store.list_episodes(podcast,status,30,(page-1)*30)
        except ValueError:
            episodes=[]
        return render(request,"library.html",active="library",episodes=episodes,podcasts=store.list_podcasts(),
            stats=store.status(),q=q,podcast=podcast,status=status,page=page,
            previous_url="/?"+urlencode({"q":q,"podcast":podcast,"status":status,"page":page-1}),
            next_url="/?"+urlencode({"q":q,"podcast":podcast,"status":status,"page":page+1}))

    @app.get("/episodes/{episode_id}",response_class=HTMLResponse)
    def reader(request: Request,episode_id: str):
        content = store.reader(episode_id)
        owner = store.owner_episode(episode_id)
        share_url = settings.share_origin+"/p/"+owner["share_token"] if owner["share_token"] else None
        return render(request,"reader.html",active="library",**content,share_url=share_url)

    @app.get("/episodes/{episode_id}/transcript.txt")
    def download(episode_id: str):
        ep = store.episode(episode_id)
        with store.connect() as db:
            rows = db.execute("SELECT start,text FROM segments WHERE episode_id=? ORDER BY sequence",(episode_id,)).fetchall()
        if not rows:
            raise Missing("Transcript not ready")
        text = ep["title"]+"\n"+ep["podcast"]+"\nMachine transcript · may contain errors\n\n"
        text += "\n\n".join(f"[{timestamp(row['start'])}] {row['text']}" for row in rows)
        return PlainTextResponse(text,headers={"Content-Disposition":f'attachment; filename="transcript-{episode_id}.txt"'})

    @app.get("/pipeline",response_class=HTMLResponse)
    def pipeline(request: Request):
        with store.connect() as db:
            runs=[dict(r) for r in db.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 10")]
        return render(request,"pipeline.html",active="pipeline",stats=store.status(),runs=runs,
            jobs=store.list_episodes(status="working")+store.list_episodes(status="queued")+store.list_episodes(status="failed"))

    @app.get("/connections",response_class=HTMLResponse)
    def connections(request: Request):
        return render(request,"connections.html",active="connections",stats=store.status(),
            shared=store.list_shared(),share_origin=settings.share_origin)

    @app.post("/actions/sync")
    async def sync_now():
        result = await run_in_threadpool(sync,store)
        message = "Next Overcast check is not due yet (twice daily)." if result.get("status")=="not_due" else f"Discovered {result['episodes']} episodes; queued {result['queued']}."
        return redirect("/pipeline",message)

    @app.post("/actions/import")
    async def upload(request: Request,file: UploadFile=File(...)):
        raw = await file.read(MAX_EXPORT+1)
        await file.close()
        with file_lock(settings.data_dir/"sync.lock"):
            result = await run_in_threadpool(import_export,store,raw)
        return redirect("/pipeline",f"Imported {result['episodes']} episodes; queued {result['queued']}; held {result['held']} for optional backfill.")

    @app.post("/actions/overcast/qr")
    async def start_qr():
        return await qr_login.start()

    @app.post("/actions/overcast/qr/{flow_id}")
    async def check_qr(flow_id: str):
        return await qr_login.check(flow_id)

    @app.post("/actions/overcast")
    async def connect_overcast(request: Request):
        form = await request.form()
        cookie = str(form.get("cookie",""))
        if cookie:
            try:
                save_cookie(settings.data_dir/"overcast.json",cookie)
            except ValueError:
                raise Conflict("Invalid session cookie")
        else:
            await run_in_threadpool(login,store,str(form.get("email","")),str(form.get("password","")))
        return redirect("/connections","Overcast session saved. Discovery will check automatically within a minute.")

    @app.post("/actions/queue/{episode_id}")
    def queue_episode(episode_id: str):
        store.queue_episode(episode_id)
        return redirect(f"/episodes/{episode_id}","Queued for local transcription.")

    @app.post("/actions/share/{episode_id}")
    async def share_episode(request: Request,episode_id: str):
        form = await request.form()
        publish = form.get("publish")=="1"
        if publish:
            await app.state.public_probe(settings)
        await run_in_threadpool(store.share,episode_id,publish)
        back = "/connections#sharing" if form.get("return")=="connections" else f"/episodes/{episode_id}"
        return redirect(back,"Public link created. Anyone with the link can read it." if publish else "Public link revoked.")

    @app.get("/p/public.css")
    def shared_style():
        return public_stylesheet()

    @app.get("/p/{token}",response_class=HTMLResponse)
    def shared(request: Request,token: str):
        return shared_page(request,settings.public_db_path,token)

    @app.post("/api/worker/claim")
    def claim():
        return {"job":store.claim()}

    @app.post("/api/worker/jobs/{job_id}/heartbeat")
    def heartbeat(job_id: str,lease: Lease):
        store.heartbeat(job_id,lease.lease_token)
        return {"ok":True}

    @app.post("/api/worker/jobs/{job_id}/fail")
    def fail_job(job_id: str,failure: Failure):
        store.fail_job(job_id,failure.lease_token,failure.code)
        return {"ok":True}

    @app.post("/api/worker/jobs/{job_id}/complete")
    def complete(job_id: str,result: Result):
        return store.complete(job_id,result)

    app.mount("/",mcp_app)
    return app


def create_public_app(directory=None):
    # No Settings.from_env(), private secrets, private DB, owner routes or MCP.
    directory = Path(directory or os.environ.get("PUBLIC_DATA_DIR","data/public")).resolve()
    path = directory/"shares.sqlite3"
    public = FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    public.mount("/static",StaticFiles(directory=ROOT/"static"),name="static")

    @public.middleware("http")
    async def headers(request,call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control":"no-store, max-age=0","X-Robots-Tag":"noindex, nofollow, noarchive",
            "Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY",
            "Content-Security-Policy":"default-src 'none'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @public.get("/healthz")
    def health():
        try:
            with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=10)) as db:
                db.execute("SELECT count(*) FROM shares").fetchone()
            return {"service":"transcripts-public","status":"ok"}
        except sqlite3.Error:
            return JSONResponse({"service":"transcripts-public","status":"unavailable"},503)

    @public.get("/p/public.css")
    def shared_style():
        return public_stylesheet()

    @public.get("/p/{token}",response_class=HTMLResponse)
    def shared(request: Request,token: str):
        return shared_page(request,path,token)

    return public
