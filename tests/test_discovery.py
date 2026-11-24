from dataclasses import replace
from datetime import datetime,timezone,timedelta
import pytest
from transcripts.db import Store
from transcripts.discovery import import_export,parse_export,parse_date,DiscoveryError,sync
from conftest import export,result_for


def test_first_baseline_recent_only_signed_urls_exact_and_repeat(store):
    old=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    future=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat()
    raw=export({'id':'101'}, {'id':'102','date':old}, {'id':'103','date':None},
        {'id':'104','played':'0','progress':'310'}, {'id':'105','played':'0','progress':'25'},
        {'id':'106','date':future},{'id':'107','date':'not-a-date'})
    result=import_export(store,raw)
    assert result['baseline'] and result['queued']==2 and result['held']==4
    assert store.status()['inventory']==7
    job=store.claim()
    assert job['audio_url']=='https://example.org/audio.mp3?token=keep%2Bexact&part=1'
    store.complete(job['id'],result_for(job))
    assert import_export(store,raw)['queued']==0
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==7
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==2
    # A missing episode in a later snapshot never deletes retained transcripts.
    import_export(store,export({'id':'105','played':'0','progress':'25'}))
    assert store.reader(job['episode_id'])['total_segments']==2


def test_progress_transition_queues_historic_unknown_date_without_duplicates(store):
    import_export(store,export({'id':'101','date':None,'played':'0','progress':'20'}))
    assert not store.claim()
    assert import_export(store,export({'id':'101','date':None,'played':'0','progress':'400'}))['queued']==1
    job=store.claim();store.complete(job['id'],result_for(job))
    import_export(store,export({'id':'101','date':None,'played':'0','progress':'0'}))
    import_export(store,export({'id':'101','date':None,'played':'1','progress':'400'}))
    assert store.claim() is None


def test_metadata_change_does_not_queue_old_played_episode(store):
    import_export(store,export({'id':'101','date':None}))
    import_export(store,export({'id':'101','date':None,'title':'Changed title'}))
    assert store.claim() is None
    assert store.backfill(1,False)[0]['id']=='101'
    assert store.claim() is None
    store.backfill(1,True)
    assert store.claim()['episode_id']=='101'


def test_failed_import_does_not_advance_baseline_and_plain_opml_rejected(store):
    with pytest.raises(DiscoveryError):
        import_export(store,b'<opml><body><outline type="rss" xmlUrl="https://example.org"/></body></opml>')
    with store.connect() as db:
        assert db.execute("SELECT * FROM state WHERE key='baseline_at'").fetchone() is None
    assert import_export(store,export())['baseline']
    with pytest.raises(DiscoveryError):
        parse_export(b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><opml><body>&x;</body></opml>')


def test_invalid_ids_are_reported_not_url_deduplicated(store):
    result=import_export(store,export({'id':'101'},{'id':None},{'id':'102','played':'nonsense'}))
    assert result['skipped']==2 and result['episodes']==1


def test_first_queue_has_a_bound_and_backfill_preview(store):
    store.settings=replace(store.settings,queue_limit=2)
    result=import_export(store,export(*({'id':str(i)} for i in range(10))))
    assert result['queued']==2 and result['held']==8
    assert len(store.backfill(3))==3
    assert store.status()['jobs']=={'queued':2}
    with pytest.raises(ValueError):store.backfill(1000,True)


def test_valid_dates_require_timezone_and_plausible_range():
    assert parse_date('2026-10-01T10:00:00') is None
    assert parse_date('999999999999999') is None
    assert parse_date('Tue, 20 Oct 2026 10:00:00 GMT')


def test_discovery_poll_throttled_even_when_failed(store,monkeypatch):
    run=store.start_run('overcast');store.fail_run(run,'overcast_session_expired')
    assert sync(store)=={'status':'not_due'}


def test_reconnecting_after_expiry_allows_one_check(store,monkeypatch):
    import os
    from transcripts.discovery import save_cookie
    path=store.settings.data_dir/'overcast.json'
    save_cookie(path,'old');os.utime(path,(1,1))
    run=store.start_run('overcast');store.fail_run(run,'overcast_session_expired')
    assert store.status()['overcast_expired']
    assert sync(store)=={'status':'not_due'}
    save_cookie(path,'new')
    assert not store.status()['overcast_expired']
    monkeypatch.setattr('transcripts.discovery.httpx.Client',lambda **kw:(_ for _ in ()).throw(RuntimeError('fetched')))
    with pytest.raises(RuntimeError,match='fetched'):sync(store)
    assert sync(store)=={'status':'not_due'}
