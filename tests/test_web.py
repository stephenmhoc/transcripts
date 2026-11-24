from dataclasses import replace
import json
from pathlib import Path
from fastapi.testclient import TestClient
import pytest
from transcripts.web import create_app,create_public_app,parse_offset,anchored
from transcripts.auth import COOKIE,csrf_token
from transcripts.db import Conflict
from conftest import export,result_for
from transcripts.discovery import import_export


def client_for(settings):
    settings=replace(settings,origin='http://localhost')
    return TestClient(create_app(settings),base_url=settings.origin),settings


def test_fail_closed_config_and_host(settings):
    with pytest.raises(ValueError):create_app(replace(settings,owner_token=''))
    client,settings=client_for(settings)
    with client:
        assert client.get('/',follow_redirects=False).status_code==303
        assert client.get('/api/worker/claim').status_code==401
        assert client.get('/healthz',headers={'Host':'evil.example'}).status_code==400
        assert client.get('/healthz').json()['status']=='ok'


def test_roles_cannot_impersonate_owner_and_cookies_need_csrf(settings):
    client,settings=client_for(settings)
    with client:
        for token in [settings.worker_token,settings.mcp_token]:
            assert client.get('/',headers={'Authorization':'Bearer '+token},follow_redirects=False).status_code==303
            assert client.post('/actions/sync',headers={'Authorization':'Bearer '+token}).status_code==401
        assert client.post('/api/worker/claim',headers={'Authorization':'Bearer '+settings.owner_token}).status_code==401
        assert client.post('/login',data={'token':settings.owner_token}).status_code==403
        response=client.post('/login',data={'token':settings.owner_token},headers={'Origin':settings.origin},follow_redirects=False)
        assert response.status_code==303
        assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=strict' in response.headers['set-cookie']
        assert client.get('/').status_code==200
        assert client.post('/logout').status_code==403
        csrf=csrf_token(client.cookies[COOKIE],settings)
        assert client.post('/logout',data={'csrf':csrf},headers={'Origin':'https://evil.example'}).status_code==403
        assert client.post('/logout',data={'csrf':csrf},headers={'Origin':settings.origin},follow_redirects=False).status_code==303


def test_html_reader_import_search_and_publish_revoke(settings):
    client,settings=client_for(settings)
    with client:
        auth={'Authorization':'Bearer '+settings.owner_token}
        response=client.post('/actions/import',headers=auth,files={'file':('all.opml',export(),'text/xml')},follow_redirects=False)
        assert response.status_code==303
        store=client.app.state.store
        job=store.claim();store.complete(job['id'],result_for(job,'An escaped <script>alert(1)</script> transcript about attention.'))
        for page in ['/','/?q=attention','/pipeline','/connections','/episodes/101']:
            response=client.get(page,headers=auth)
            assert response.status_code==200
            assert 'SECRET' not in response.text
        response=client.get('/episodes/101',headers=auth)
        assert '<script>alert(1)</script>' not in response.text and '&lt;script&gt;' in response.text
        assert 'Content-Security-Policy' in response.headers
        assert client.get('/episodes/101/transcript.txt',headers=auth).status_code==200
        response=client.post('/actions/share/101',headers=auth,data={'publish':'1'},follow_redirects=False)
        assert response.status_code==303
        token=store.owner_episode('101')['share_token']
        assert settings.origin+'/p/'+token in client.get('/episodes/101',headers=auth).text
        assert token in client.get('/connections',headers=auth).text
        # Same-origin public page: no session, no bearer.
        anonymous=TestClient(client.app,base_url=settings.origin)
        response=anonymous.get('/p/'+token)
        assert response.status_code==200 and 'no-store' in response.headers['cache-control']
        assert response.headers['referrer-policy']=='no-referrer' and 'noindex' in response.headers['x-robots-tag']
        assert anonymous.get('/p/public.css').status_code==200
        assert '<script>alert(1)</script>' not in response.text
        assert 'SECRET' not in response.text and 'example.org/audio' not in response.text
        assert 'href="/' not in response.text.replace('href="/p/public.css"','')  # No private navigation.
        for route in ['/','/connections','/episodes/101','/episodes/101/transcript.txt']:
            assert anonymous.get(route,follow_redirects=False).status_code==303
        assert anonymous.post('/actions/share/101',data={'publish':'0'}).status_code==401
        # Separate listener serves the identical copy and nothing private.
        public=TestClient(create_public_app(settings.public_db_path.parent))
        assert public.get('/p/'+token).status_code==200 and public.get('/p/public.css').status_code==200
        for route in ['/','/login','/connections','/api/worker/claim','/mcp','/docs','/episodes/101']:
            assert public.get(route).status_code==404
        with store.connect() as db:
            assert db.execute('SELECT count(*) FROM sharing_events').fetchone()[0]==1
        client.post('/actions/share/101',headers=auth,data={'publish':'0'})
        assert anonymous.get('/p/'+token).status_code==404 and public.get('/p/'+token).status_code==404
        new_token=store.share('101',True)
        assert new_token!=token


def test_reader_shows_whole_transcript_and_overcast_link(settings):
    client,settings=client_for(settings)
    with client:
        store=client.app.state.store
        import_export(store,export())
        with store.connect(write=True) as db:
            db.execute("UPDATE episodes SET source_url='https://overcast.fm/+abc123' WHERE id='101'")
        job=store.claim();result=result_for(job)
        result.segments=[type(result.segments[0])(start=i*5,end=i*5+4,text=f'Passage number {i}.') for i in range(650)]
        store.complete(job['id'],result)
        page=client.get('/episodes/101',headers={'Authorization':'Bearer '+settings.owner_token}).text
        assert 'Passage number 0.' in page and 'Passage number 649.' in page and '?page=' not in page
        token=store.share('101',True)
        shared=TestClient(client.app,base_url=settings.origin).get('/p/'+token).text
        assert 'Passage number 649.' in shared and 'https://overcast.fm/+abc123' in shared


def test_worker_http_receipt_fencing(settings):
    client,settings=client_for(settings)
    with client:
        store=client.app.state.store;import_export(store,export())
        auth={'Authorization':'Bearer '+settings.worker_token}
        job=client.post('/api/worker/claim',headers=auth).json()['job']
        payload=result_for(job).model_dump()
        assert client.post(f"/api/worker/jobs/{job['id']}/heartbeat",headers=auth,json={'lease_token':job['lease_token']}).status_code==200
        one=client.post(f"/api/worker/jobs/{job['id']}/complete",headers=auth,json=payload)
        two=client.post(f"/api/worker/jobs/{job['id']}/complete",headers=auth,json=payload)
        assert one.json()['receipt']==two.json()['receipt'] and two.json()['replayed']
        payload['lease_token']='x'*40
        assert client.post(f"/api/worker/jobs/{job['id']}/complete",headers=auth,json=payload).status_code==409


def test_separate_share_host_probe_failure_leaves_private(settings):
    client,settings=client_for(replace(settings,public_origin='https://share.example.org'))
    with client:
        store=client.app.state.store;import_export(store,export())
        job=store.claim();store.complete(job['id'],result_for(job))
        async def failed(_):raise Conflict('Public listener unavailable')
        client.app.state.public_probe=failed
        assert client.post('/actions/share/101',headers={'Authorization':'Bearer '+settings.owner_token},data={'publish':'1'}).status_code==409
        assert store.owner_episode('101')['share_token'] is None


def test_normal_browser_cookie_multipart_import_passes_csrf(settings):
    client,settings=client_for(settings)
    with client:
        response=client.get('/login')
        assert response.headers['referrer-policy']=='same-origin'
        client.post('/login',data={'token':settings.owner_token},headers={'Origin':settings.origin})
        csrf=csrf_token(client.cookies[COOKIE],settings)
        response=client.post('/actions/import',data={'csrf':csrf},files={'file':('all.opml',export(),'text/xml')},headers={'Origin':settings.origin},follow_redirects=False)
        assert response.status_code==303
        assert client.app.state.store.status()['inventory']==1


def test_failed_logins_slow_down_but_never_lock_out_the_owner(settings,monkeypatch):
    slept=[]
    async def fake_sleep(seconds):slept.append(seconds)
    monkeypatch.setattr('transcripts.web.asyncio.sleep',fake_sleep)
    client,settings=client_for(settings)
    with client:
        for _ in range(12):
            assert client.post('/login',data={'token':'wrong'},headers={'Origin':settings.origin}).status_code==200
        response=client.post('/login',data={'token':settings.owner_token},headers={'Origin':settings.origin},follow_redirects=False)
        assert response.status_code==303 and slept


def test_verified_cloudflare_access_identity_replaces_the_owner_key(settings,monkeypatch):
    import time,jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    team='https://team.cloudflareaccess.com'
    real,forged=(rsa.generate_private_key(public_exponent=65537,key_size=2048) for _ in range(2))
    class Keys:
        def __init__(self,*args,**kwargs):pass
        def get_signing_key_from_jwt(self,token):return type('Key',(),{'key':real.public_key()})
    monkeypatch.setattr('transcripts.auth.jwt.PyJWKClient',Keys)
    def assertion(key=real,email='Owner@Example.com',aud='app-aud'):
        now=int(time.time())
        return jwt.encode({'email':email,'aud':[aud],'iss':team,'iat':now,'exp':now+300},key,algorithm='RS256')
    client,settings=client_for(replace(settings,access_team=team,access_aud='app-aud',access_email='owner@example.com'))
    with client:
        for bad in [assertion(email='someone@example.com'),assertion(aud='other-app'),assertion(key=forged),'not-a-jwt']:
            response=client.get('/',headers={'Cf-Access-Jwt-Assertion':bad},follow_redirects=False)
            assert response.headers['location']=='/login' and COOKIE not in client.cookies
        assert client.get('/api/status',headers={'Cf-Access-Jwt-Assertion':assertion()}).status_code==401
        response=client.get('/login',headers={'Cf-Access-Jwt-Assertion':assertion()},follow_redirects=False)
        assert response.status_code==303 and response.headers['location']=='/'
        # Browsers withhold the Strict cookie inside the cross-site Access redirect
        # chain, so a page must render and sign in on the same response, not loop.
        for _ in range(2):
            client.cookies.clear()
            response=client.get('/',headers={'Cf-Access-Jwt-Assertion':assertion()},follow_redirects=False)
            assert response.status_code==200 and 'SameSite=strict' in response.headers['set-cookie']
        session=client.cookies[COOKIE]
        assert csrf_token(session,settings) in response.text
        assert client.get('/').status_code==200
        response=client.post('/logout',data={'csrf':csrf_token(session,settings)},headers={'Origin':settings.origin},follow_redirects=False)
        assert response.headers['location']=='/cdn-cgi/access/logout'


def test_origin_is_normalized_like_browsers_send_it(monkeypatch,settings):
    from transcripts.config import Settings,origin_only
    assert origin_only('https://Transcripts.example.com:443/')=='https://transcripts.example.com'
    assert origin_only('http://localhost:8765/')=='http://localhost:8765'
    for key,value in {'APP_ORIGIN':'http://localhost/','OWNER_TOKEN':settings.owner_token,'MCP_TOKEN':settings.mcp_token,
            'WORKER_TOKEN':settings.worker_token,'SESSION_SECRET':settings.session_secret,'DATA_DIR':str(settings.data_dir),
            'TRANSCRIPTS_ENV':'/nonexistent'}.items():
        monkeypatch.setenv(key,value)
    loaded=Settings.from_env()
    assert loaded.origin=='http://localhost'
    client=TestClient(create_app(replace(loaded,discovery_enabled=False)),base_url=loaded.origin)
    with client:
        response=client.post('/login',data={'token':settings.owner_token},headers={'Origin':'http://localhost'},follow_redirects=False)
        assert response.status_code==303


def test_time_offsets_parse():
    for value,seconds in [('754',754),('12m',720),('12m30s',750),('1h2m3s',3723),('12:30',750),('1:02:03',3723),('90s',90),(' 5M ',300)]:
        assert parse_offset(value)==seconds,value
    for value in ['','abc','12x','1:2:3:4','-5','1:aa']:
        assert parse_offset(value) is None,value


def test_minute_anchors_land_on_the_passage_being_spoken():
    segments=anchored([{'start':0,'end':50,'text':'a'},{'start':55,'end':130,'text':'b'},{'start':130,'end':131,'text':'c'},{'start':130.5,'end':185,'text':'d'}])
    assert [s['anchor'] for s in segments]==['0m0s','0m55s','2m10s','2m10s-']
    assert [s['minutes'] for s in segments]==[['0m'],['1m','2m'],[],['3m']]


def test_public_links_to_a_moment(settings):
    client,settings=client_for(settings)
    with client:
        store=client.app.state.store
        import_export(store,export())
        job=store.claim();result=result_for(job)
        result.segments=[type(result.segments[0])(start=i*20,end=i*20+19,text=f'Passage {i}.') for i in range(60)]
        store.complete(job['id'],result)
        token=store.share('101',True)
        anonymous=TestClient(client.app,base_url=settings.origin)
        page=anonymous.get('/p/'+token).text
        assert 'id="12m"' in page and 'id="12m0s"' in page and 'href="#12m20s"' in page
        for query,anchor in [('12m30s','12m20s'),('750','12m20s'),('12:30','12m20s'),('0','0m0s'),('99m','19m40s')]:
            response=anonymous.get(f'/p/{token}?t={query}',follow_redirects=False)
            assert response.status_code==303 and response.headers['location']==f'/p/{token}#{anchor}',query
        assert anonymous.get(f'/p/{token}?t=nonsense').status_code==200
