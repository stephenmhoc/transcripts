import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlsplit,parse_qs

import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

COOKIE = "transcripts_session"
MAX_AGE = 30*86400
ACCESS_HEADER = "cf-access-jwt-assertion"


def signature(value, secret):
    return hmac.new(secret.encode(),value.encode(),hashlib.sha256).hexdigest()


def make_session(settings):
    payload = f"{int(time.time())}.{secrets.token_urlsafe(24)}.{hashlib.sha256(settings.owner_token.encode()).hexdigest()[:16]}"
    return payload+"."+signature(payload,settings.session_secret)


def valid_session(value, settings):
    try:
        payload, sig = value.rsplit(".",1)
        issued,nonce,owner = payload.split(".")
        return (len(nonce)>=20 and 0<=time.time()-int(issued)<MAX_AGE
                and hmac.compare_digest(owner,hashlib.sha256(settings.owner_token.encode()).hexdigest()[:16])
                and hmac.compare_digest(sig,signature(payload,settings.session_secret)))
    except (ValueError,AttributeError):
        return False


def set_session(response, settings, value=None):
    response.set_cookie(COOKIE,value or make_session(settings),max_age=MAX_AGE,httponly=True,secure=settings.cookie_secure,samesite="strict",path="/")
    return response


class AccessVerifier:
    """Checks the signed Cloudflare Access assertion; a bare header proves nothing."""
    def __init__(self,settings):
        self.settings = settings
        self.keys = jwt.PyJWKClient(settings.access_team+"/cdn-cgi/access/certs",cache_keys=True,lifespan=3600)

    def check(self,token):
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token,key,algorithms=["RS256"],audience=self.settings.access_aud,
                issuer=self.settings.access_team,options={"require":["exp","iat","aud","iss"]})
        except jwt.PyJWTError:
            return False
        return str(claims.get("email","")).lower()==self.settings.access_email

    async def verify(self,token):
        return bool(token) and await asyncio.to_thread(self.check,token)


def csrf_token(session, settings):
    return signature("csrf:"+session,settings.session_secret)


def token_matches(actual, expected):
    return bool(actual) and secrets.compare_digest(actual,expected)


# Shared transcript pages: read-only copies from the share store, readable by anyone.
PUBLIC_HEADERS = [
    (b"cache-control",b"no-store, max-age=0"),(b"x-robots-tag",b"noindex, nofollow, noarchive"),
    (b"x-content-type-options",b"nosniff"),(b"referrer-policy",b"no-referrer"),(b"x-frame-options",b"DENY"),
    (b"content-security-policy",b"default-src 'none'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"),
]
PRIVATE_HEADERS = [
    (b"cache-control",b"no-store"),(b"x-content-type-options",b"nosniff"),
    (b"referrer-policy",b"same-origin"),(b"x-frame-options",b"DENY"),
    (b"content-security-policy",b"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
]


def is_public_path(path):
    return path.startswith("/p/")


class SecurityBoundary:
    def __init__(self,app,settings):
        self.app,self.settings = app,settings
        self.allowed_hosts = {urlsplit(settings.origin).netloc}
        self.access = AccessVerifier(settings) if settings.access_enabled else None

    async def __call__(self,scope,receive,send):
        if scope["type"] != "http":
            return await self.app(scope,receive,send)
        req = Request(scope,receive)
        path,method = req.url.path,req.method
        host = req.headers.get("host","")
        if host not in self.allowed_hosts:
            return await JSONResponse({"detail":"Unrecognized host"},status_code=400)(scope,receive,send)
        authorization = req.headers.get("authorization","")
        bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
        session = req.cookies.get(COOKIE,"")
        is_session = valid_session(session,self.settings)
        # A verified Access identity replaces the owner key. Mint a normal session on
        # this response rather than redirecting: arriving from the Access login is a
        # cross-site redirect chain, where browsers withhold SameSite=Strict cookies,
        # so a redirect to "set then reread" the cookie loops forever.
        minted = []
        if (self.access and not is_session and method=="GET" and path not in {"/healthz","/favicon.ico"}
                and not path.startswith(("/api/","/mcp","/static/")) and not is_public_path(path)
                and await self.access.verify(req.headers.get(ACCESS_HEADER,""))):
            if path=="/login":
                return await RedirectResponse("/",status_code=303)(scope,receive,send)
            session,is_session = make_session(self.settings),True
            # Downstream rendering reads the cookie for the CSRF token; show it the new one.
            cookie = "; ".join(filter(None,[req.headers.get("cookie",""),f"{COOKIE}={session}"]))
            scope = {**scope,"headers":[(k,v) for k,v in scope["headers"] if k!=b"cookie"]+[(b"cookie",cookie.encode())]}
            minted = [(k,v) for k,v in set_session(Response(),self.settings,session).raw_headers if k==b"set-cookie"]
        required = None
        if path=="/mcp" or path.startswith("/mcp/"):
            required = self.settings.mcp_token
        elif path.startswith("/api/worker/"):
            required = self.settings.worker_token
        elif path not in {"/login","/healthz","/favicon.ico"} and not path.startswith("/static/") and not is_public_path(path):
            required = self.settings.owner_token
        if required and not token_matches(bearer,required) and not (required==self.settings.owner_token and is_session):
            response = (RedirectResponse("/login",status_code=303) if method=="GET" and not path.startswith(("/api/","/mcp"))
                        else JSONResponse({"detail":"Authentication required"},401,headers={"WWW-Authenticate":"Bearer"}))
            return await response(scope,receive,send)
        if req.headers.get("origin") and req.headers["origin"] != self.settings.origin:
            return await JSONResponse({"detail":"Origin rejected"},403)(scope,receive,send)
        body = b""
        if method in {"POST","PUT","PATCH","DELETE"}:
            maximum = 20_000_000 if path=="/actions/import" else 12_000_000 if path.endswith("/complete") else 1_000_000
            async for chunk in req.stream():
                body += chunk
                if len(body)>maximum:
                    return await JSONResponse({"detail":"Request too large"},413)(scope,receive,send)
            if path=="/login" and req.headers.get("origin") != self.settings.origin:
                return await JSONResponse({"detail":"Login requires same origin"},403)(scope,receive,send)
            if required==self.settings.owner_token and not token_matches(bearer,required):
                supplied = req.headers.get("x-csrf-token","")
                if not supplied:
                    ctype = req.headers.get("content-type","")
                    if ctype.startswith("application/x-www-form-urlencoded"):
                        supplied = parse_qs(body.decode(errors="replace")).get("csrf",[""])[0]
                    elif ctype.startswith("multipart/form-data"):
                        async def form_receive():
                            return {"type":"http.request","body":body,"more_body":False}
                        parsed = await Request(scope,form_receive).form(max_part_size=20_000_000)
                        supplied = str(parsed.get("csrf",""))
                        await parsed.close()
                if not token_matches(supplied,csrf_token(session,self.settings)):
                    return await JSONResponse({"detail":"CSRF check failed"},403)(scope,receive,send)
        used = False
        async def replay():
            nonlocal used
            if method not in {"POST","PUT","PATCH","DELETE"}:
                return await receive()
            if not used:
                used=True
                return {"type":"http.request","body":body,"more_body":False}
            return await receive()
        async def secure_send(message):
            if message["type"]=="http.response.start":
                message["headers"] = list(message.get("headers",[])) + minted + (PUBLIC_HEADERS if is_public_path(path) else PRIVATE_HEADERS)
            await send(message)
        await self.app(scope,replay,secure_send)
