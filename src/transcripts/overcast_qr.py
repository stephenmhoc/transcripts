"""Passwordless browser-style Overcast login, matching its current login page."""
import asyncio
import base64
import io
import re
import secrets
import time
from urllib.parse import urljoin,urlsplit

import httpx
import qrcode
from .discovery import DiscoveryError,save_cookie


def auth_cookie(cookies):
    # Cookies.get raises CookieConflict when hops set "o" on different paths.
    return next((c.value for c in cookies.jar if c.name=="o" and c.value),None)


class QRLogin:
    def __init__(self,cookie_path):
        self.path=cookie_path
        self.flows={}

    async def _prune(self):
        for key,flow in list(self.flows.items()):
            if time.monotonic()-flow["started"]>600:
                await flow["client"].aclose()
                self.flows.pop(key,None)

    async def start(self):
        await self._prune()
        if len(self.flows)>=3:
            raise DiscoveryError("qr_sessions_busy_try_in_ten_minutes")
        client=httpx.AsyncClient(timeout=20,follow_redirects=False,trust_env=False)
        try:
            response=await client.get("https://overcast.fm/login")
            response.raise_for_status()
            token=re.search(r'id="qrcode"\s+data-token="([A-Za-z0-9_-]{10,128})"',response.text)
            if not token or "qr" not in client.cookies:
                raise DiscoveryError("overcast_qr_schema_changed")
            token=token.group(1)
            flow_id=secrets.token_urlsafe(24)
            self.flows[flow_id]={"client":client,"token":token,"started":time.monotonic(),"checked":0}
            link=f"https://overcast.fm/main/login_app?t={token}&l=browser"
            image=qrcode.make(f"overcast:///auth?t={token}&l=browser")
            output=io.BytesIO();image.save(output,format="PNG")
            return {"flow_id":flow_id,"image":"data:image/png;base64,"+base64.b64encode(output.getvalue()).decode(),"link":link}
        except Exception as exc:
            await client.aclose()
            if isinstance(exc,DiscoveryError):
                raise
            raise DiscoveryError("overcast_qr_unavailable") from exc

    async def _finish(self,flow_id):
        flow=self.flows.pop(flow_id,None)
        if flow:
            await flow["client"].aclose()

    async def check(self,flow_id):
        await self._prune()
        flow=self.flows.get(flow_id)
        if flow is None:
            raise DiscoveryError("qr_expired_start_again")
        if time.monotonic()-flow["checked"]<4:
            return {"connected":False}
        flow["checked"]=time.monotonic()
        try:
            async with asyncio.timeout(25):
                cookie=await self._verify(flow)
            if cookie:
                save_cookie(self.path,cookie)
                await self._finish(flow_id)
                return {"connected":True}
            return {"connected":False}
        except DiscoveryError:
            await self._finish(flow_id)
            raise
        except ValueError as exc:
            await self._finish(flow_id)
            raise DiscoveryError("overcast_qr_cookie_invalid") from exc
        except (httpx.HTTPError,TimeoutError) as exc:
            raise DiscoveryError("overcast_qr_unavailable") from exc

    async def _verify(self,flow):
        client=flow["client"]
        response=await client.post("https://overcast.fm/main/login_qr_verify",data={"token":flow["token"],"then":"account"})
        if response.status_code not in {200,302,303}:
            raise DiscoveryError("overcast_qr_unavailable")
        cookie=auth_cookie(client.cookies)
        if cookie:
            return cookie
        # The current browser script navigates to the verifier's response.
        # Some sessions receive their auth cookie only at that final URL.
        if response.status_code in {302,303}:
            target=response.headers.get("location","")
        else:
            target=response.text.strip()
            if not re.fullmatch(r"(/|https?://)\S{0,2047}",target):
                return None  # pending: empty, a status word, or an HTML page
        if target in {"","/","/login"}:
            return None
        # Follow on a scratch jar so pages like /login can't replace the qr
        # cookie that binds this flow's token while approval is pending.
        async with httpx.AsyncClient(timeout=10,follow_redirects=False,trust_env=False,cookies=httpx.Cookies(client.cookies)) as scratch:
            target=urljoin("https://overcast.fm/",target)
            for _ in range(5):
                parsed=urlsplit(target)
                if parsed.scheme!="https" or parsed.hostname!="overcast.fm" or parsed.port not in {None,443} or parsed.username or parsed.password:
                    raise DiscoveryError("overcast_qr_redirect_invalid")
                if parsed.path=="/login":
                    return None
                final=await scratch.get(target)
                cookie=auth_cookie(scratch.cookies)
                if cookie or final.status_code not in {301,302,303,307,308}:
                    return cookie
                location=final.headers.get("location","")
                if not location:
                    return None
                target=urljoin(target,location)
        return None

    async def close(self):
        for flow in self.flows.values():
            await flow["client"].aclose()
        self.flows.clear()
