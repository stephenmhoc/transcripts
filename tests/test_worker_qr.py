import json
from pathlib import Path
import httpx
from transcripts.worker import atomic_json,submit_spool
from transcripts.overcast_qr import QRLogin


def test_spooled_result_is_preserved_on_outage_and_removed_on_receipt(tmp_path):
    path=tmp_path/'a.pending.json';atomic_json(path,{'job_id':'a','result':{'segments':[]}})
    def offline(request):raise httpx.ConnectError('offline',request=request)
    try:
        with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(offline)) as client:submit_spool(client,tmp_path)
    except httpx.ConnectError:pass
    assert path.exists() and path.stat().st_mode&0o777==0o600
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'receipt':'abc'}))) as client:submit_spool(client,tmp_path)
    assert not path.exists()


def test_stale_spool_is_quarantined(tmp_path):
    atomic_json(tmp_path/'a.pending.json',{'job_id':'a','result':{}})
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(lambda r:httpx.Response(409))) as client:submit_spool(client,tmp_path)
    assert (tmp_path/'a.stale.json').exists()


async def test_qr_session_saves_only_cookie_after_explicit_phone_approval(monkeypatch,tmp_path):
    original=httpx.AsyncClient
    approved=False
    logins=[]
    def handler(request):
        if request.url.path=='/login':
            logins.append(1)
            return httpx.Response(200,text='<div id="qrcode" data-token="abcd-efgh_ijklmn1234"></div>',headers={'Set-Cookie':f'qr=anon{len(logins)}; Path=/; Secure; HttpOnly'})
        if request.url.path=='/account':
            return httpx.Response(302,headers={'Location':'/login'})
        assert request.url.path=='/main/login_qr_verify'
        return httpx.Response(200,text='/account',headers={'Set-Cookie':'o=approved-cookie; Path=/; Secure; HttpOnly'} if approved else {})
    monkeypatch.setattr('transcripts.overcast_qr.httpx.AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    flow=QRLogin(tmp_path/'overcast.json')
    start=await flow.start()
    assert start['image'].startswith('data:image/png;base64,')
    assert not (tmp_path/'overcast.json').exists()
    assert await flow.check(start['flow_id'])=={'connected':False}
    assert flow.flows[start['flow_id']]['client'].cookies.get('qr')=='anon1' and len(logins)==1
    approved=True
    flow.flows[start['flow_id']]['checked']=0
    assert await flow.check(start['flow_id'])=={'connected':True}
    assert json.loads((tmp_path/'overcast.json').read_text())=={'o':'approved-cookie'}
    assert (tmp_path/'overcast.json').stat().st_mode&0o777==0o600
    await flow.close()


async def test_qr_finalizes_same_origin_browser_redirect(monkeypatch,tmp_path):
    original=httpx.AsyncClient
    def handler(request):
        if request.url.path=='/login':
            return httpx.Response(200,text='<div id="qrcode" data-token="abcd-efgh_ijklmn1234"></div>',headers={'Set-Cookie':'qr=anon; Path=/; Secure'})
        if request.url.path=='/main/login_qr_verify':
            return httpx.Response(200,text='/main/finalize?code=opaque')
        assert request.url.host=='overcast.fm' and request.url.path=='/main/finalize'
        return httpx.Response(302,headers={'Set-Cookie':'o=final-cookie; Path=/; Secure','Location':'/account'})
    monkeypatch.setattr('transcripts.overcast_qr.httpx.AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    flow=QRLogin(tmp_path/'overcast.json');start=await flow.start()
    assert await flow.check(start['flow_id'])=={'connected':True}
    assert json.loads((tmp_path/'overcast.json').read_text())=={'o':'final-cookie'}
    await flow.close()


async def test_qr_rejects_external_auth_redirect(monkeypatch,tmp_path):
    import pytest
    from transcripts.discovery import DiscoveryError
    original=httpx.AsyncClient
    def handler(request):
        assert request.url.host=='overcast.fm'
        if request.url.path=='/login':
            return httpx.Response(200,text='<div id="qrcode" data-token="abcd-efgh_ijklmn1234"></div>',headers={'Set-Cookie':'qr=anon; Path=/; Secure'})
        return httpx.Response(200,text='https://untrusted.example/capture')
    monkeypatch.setattr('transcripts.overcast_qr.httpx.AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    flow=QRLogin(tmp_path/'overcast.json');start=await flow.start()
    with pytest.raises(DiscoveryError):await flow.check(start['flow_id'])
    assert not (tmp_path/'overcast.json').exists()
    assert start['flow_id'] not in flow.flows
    await flow.close()


def qr_flow(monkeypatch,tmp_path,verify,other=None):
    original=httpx.AsyncClient
    def handler(request):
        if request.url.path=='/login':
            return httpx.Response(200,text='<div id="qrcode" data-token="abcd-efgh_ijklmn1234"></div>',headers={'Set-Cookie':'qr=anon; Path=/; Secure'})
        if request.url.path=='/main/login_qr_verify':
            return verify(request)
        return other(request)
    monkeypatch.setattr('transcripts.overcast_qr.httpx.AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    return QRLogin(tmp_path/'overcast.json')


async def test_qr_pending_html_page_is_not_an_error(monkeypatch,tmp_path):
    flow=qr_flow(monkeypatch,tmp_path,lambda r:httpx.Response(200,text='<html>'+'x'*5000+'</html>'))
    start=await flow.start()
    assert await flow.check(start['flow_id'])=={'connected':False}
    await flow.close()


async def test_qr_follows_verifier_http_redirect(monkeypatch,tmp_path):
    def finalize(request):
        assert request.url.path=='/main/finalize'
        return httpx.Response(200,headers={'Set-Cookie':'o=final-cookie; Path=/; Secure'})
    flow=qr_flow(monkeypatch,tmp_path,lambda r:httpx.Response(303,headers={'Location':'/main/finalize?code=opaque'}),finalize)
    start=await flow.start()
    assert await flow.check(start['flow_id'])=={'connected':True}
    assert json.loads((tmp_path/'overcast.json').read_text())=={'o':'final-cookie'}
    await flow.close()


async def test_qr_tolerates_duplicate_auth_cookies(monkeypatch,tmp_path):
    finalize=lambda r:httpx.Response(200,headers={'Set-Cookie':'o=second; Path=/'})
    flow=qr_flow(monkeypatch,tmp_path,lambda r:httpx.Response(200,text='/main/finalize',headers={'Set-Cookie':'o=; Path=/main'}),finalize)
    start=await flow.start()
    assert await flow.check(start['flow_id'])=={'connected':True}
    assert json.loads((tmp_path/'overcast.json').read_text())=={'o':'second'}
    await flow.close()


async def test_qr_invalid_cookie_closes_flow(monkeypatch,tmp_path):
    import pytest
    from transcripts.discovery import DiscoveryError
    flow=qr_flow(monkeypatch,tmp_path,lambda r:httpx.Response(200,headers={'Set-Cookie':'o='+'x'*5000+'; Path=/'}))
    start=await flow.start()
    with pytest.raises(DiscoveryError):await flow.check(start['flow_id'])
    assert start['flow_id'] not in flow.flows and not (tmp_path/'overcast.json').exists()


def test_rejected_and_unreadable_spool_files_do_not_block_claims(tmp_path):
    atomic_json(tmp_path/'a.pending.json',{'job_id':'a','result':{}})
    (tmp_path/'b.pending.json').write_text('{not json')
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(lambda r:httpx.Response(413))) as client:submit_spool(client,tmp_path)
    assert (tmp_path/'a.rejected.json').exists() and (tmp_path/'b.rejected.json').exists()
    assert not list(tmp_path.glob('*.pending.json'))
    atomic_json(tmp_path/'c.pending.json',{'job_id':'c','result':{}})
    import pytest
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(lambda r:httpx.Response(503))) as client:
        with pytest.raises(httpx.HTTPStatusError):submit_spool(client,tmp_path)
    assert (tmp_path/'c.pending.json').exists()
