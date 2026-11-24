from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import pytest
from transcripts.db import Conflict,Store
from transcripts.discovery import import_export
from conftest import export,result_for


def test_atomic_single_claim_and_restart_recovery(store):
    import_export(store,export({'id':'101'},{'id':'102'}))
    with ThreadPoolExecutor(2) as pool:
        jobs=list(pool.map(lambda _:store.claim(),range(2)))
    assert sum(job is not None for job in jobs)==1
    first=next(job for job in jobs if job)
    restarted=Store(store.settings)
    assert restarted.claim() is None
    with store.connect(write=True) as db:db.execute("UPDATE jobs SET lease_until=0 WHERE id=?",(first['id'],))
    second=restarted.claim()
    assert second and second['lease_token']!=first['lease_token']
    with pytest.raises(Conflict):restarted.heartbeat(first['id'],first['lease_token'])
    with pytest.raises(Conflict):restarted.complete(first['id'],result_for(first))
    with pytest.raises(Conflict):restarted.fail_job(first['id'],first['lease_token'],'download_failed')


def test_lost_response_replay_is_a_successful_receipt_and_no_overwrite(ready):
    store,job,result=ready
    receipt=store.complete(job['id'],result)
    assert receipt['replayed']
    assert store.reader(job['episode_id'])['total_segments']==2
    changed=result_for(job,'A different transcript must not overwrite the accepted one.')
    with pytest.raises(Conflict):store.complete(job['id'],changed)
    assert store.reader(job['episode_id'])['segments'][0]['text']==result.segments[0].text


def test_failed_completion_rolls_back_transcript_fts_and_job(store):
    import_export(store,export());job=store.claim()
    with store.connect(write=True) as db:
        db.execute("CREATE TRIGGER break_segments BEFORE INSERT ON segments BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):store.complete(job['id'],result_for(job))
    assert store.reader('101')['total_segments']==0
    assert store.episode('101')['status']=='working'
    assert not store.search('durable')


def test_retries_are_delayed_bounded_and_manual_retry_possible(store):
    import_export(store,export())
    for attempt in range(3):
        job=store.claim();assert job
        store.fail_job(job['id'],job['lease_token'],'download_failed')
        assert store.claim() is None
        if attempt<2:
            with store.connect(write=True) as db:db.execute('UPDATE jobs SET next_at=0')
    assert store.episode('101')['status']=='failed'
    store.queue_episode('101')
    assert store.claim()


def test_fts_has_timestamp_and_metadata_survives_refresh(ready):
    store,job,result=ready
    found=store.search('context')[0]
    assert found['start']==12 and found['sequence']==1
    assert not store.search('" OR * - (')
    import_export(store,export({'id':'101','title':'A new title'}))
    assert store.search('context')[0]['title']=='A new title'
    assert store.reader('101',1,1)['segments'][0]['start']==12


def test_backup_restores_to_a_fresh_store_and_completed_history(ready,tmp_path):
    store,job,result=ready
    backup=store.backup(tmp_path/'restore'/'library.sqlite3')
    restored=Store(replace(store.settings,data_dir=backup.parent))
    assert restored.reader('101')['total_segments']==2
    assert restored.search('durable')
    assert restored.claim() is None


def test_title_matches_once_instead_of_every_passage(ready):
    store,job,result=ready
    assert len(store.search('good conversation'))==1
    assert store.search('good conversation')[0]['sequence']==-1
    assert store.search('attention',status='queued')==[]
    assert store.search('attention',status='ready')


def test_no_speech_fails_without_retries(store):
    import_export(store,export())
    job=store.claim()
    store.fail_job(job['id'],job['lease_token'],'no_speech')
    assert store.episode('101')['status']=='failed'


def test_negative_offset_pages_from_the_start(ready):
    store,job,result=ready
    page=store.reader('101',offset=-1,limit=1)
    assert page['segments'][0]['sequence']==store.reader('101',offset=0,limit=1)['segments'][0]['sequence'] and page['next_offset']==1


def test_failed_backup_leaves_no_file(ready,tmp_path,monkeypatch):
    store,job,result=ready
    target=tmp_path/'bad'/'library.sqlite3'
    real=sqlite3.connect
    class Corrupt(sqlite3.Connection):
        def execute(self,sql,*a):
            return super().execute("SELECT 'corrupt'" if 'integrity' in sql else sql,*a)
    monkeypatch.setattr('transcripts.db.sqlite3.connect',lambda path,*a,**k:real(path,factory=Corrupt) if str(path)==str(target) else real(path,*a,**k))
    with pytest.raises(RuntimeError):store.backup(target)
    assert not target.exists()
    monkeypatch.undo()
    assert store.backup(target).exists()
