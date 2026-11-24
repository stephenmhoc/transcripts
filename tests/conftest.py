from dataclasses import replace
from datetime import datetime,timezone,timedelta
from xml.sax.saxutils import quoteattr
import secrets
import pytest
from transcripts.config import Settings
from transcripts.db import Store
from transcripts.discovery import import_export
from transcripts.models import Result


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path/'data',origin='http://testserver',
        owner_token=secrets.token_urlsafe(40),mcp_token=secrets.token_urlsafe(40),
        worker_token=secrets.token_urlsafe(40),session_secret=secrets.token_urlsafe(40),
        discovery_enabled=False)


@pytest.fixture
def store(settings):
    store=Store(settings);store.initialize();return store


def export(*items):
    rows=[]
    for item in items or ({'id':'101'},):
        attrs={'type':'podcast-episode','overcastId':item.get('id','101'),'title':item.get('title','A good conversation'),
            'played':item.get('played','1'),'progress':item.get('progress','500'),
            'enclosureUrl':item.get('audio','https://example.org/audio.mp3?token=keep%2Bexact&part=1'),
            'userUpdatedDate':item.get('date',datetime.now(timezone.utc).isoformat()),
            'pubDate':'Tue, 20 Oct 2026 10:00:00 GMT','userDeleted':item.get('deleted','0')}
        rows.append('<outline '+' '.join(k+'='+quoteattr(v) for k,v in attrs.items() if v is not None)+'/>' )
    return ('<opml version="2.0"><body><outline text="feeds"><outline type="rss" overcastId="1" title="Field Notes" xmlUrl="https://example.org/private?key=SECRET">'+''.join(rows)+'</outline></outline></body></opml>').encode()


def result_for(job,text='A durable searchable conversation about attention.'):
    return Result(lease_token=job['lease_token'],model='test/local',language='en',audio_sha256='a'*64,
        software='test/1',options={},segments=[{'start':0,'end':10,'text':text},{'start':12,'end':20,'text':'Returning to an idea with context matters.'}])


@pytest.fixture
def ready(store):
    import_export(store,export())
    job=store.claim(); result=result_for(job);store.complete(job['id'],result)
    return store,job,result
