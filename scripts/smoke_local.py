"""Real local download -> MLX -> fenced receipt -> FTS round trip, isolated data."""
from datetime import datetime,timezone
from pathlib import Path
from threading import Thread
import json
import resource
import secrets
import socket
import tempfile
import time

import uvicorn
from transcripts.config import Settings
from transcripts.web import create_app
from transcripts.discovery import import_export
from transcripts.worker import run_worker,DEFAULT_MODEL


def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='transcripts-smoke-') as temporary:
        folder=Path(temporary)
        settings=Settings(data_dir=folder/'data',origin=f'http://127.0.0.1:{port}',
            owner_token=secrets.token_urlsafe(40),worker_token=secrets.token_urlsafe(40),
            mcp_token=secrets.token_urlsafe(40),session_secret=secrets.token_urlsafe(40),discovery_enabled=False)
        app=create_app(settings)
        store=app.state.store
        now=datetime.now(timezone.utc).isoformat()
        export=f'''<opml><body><outline text="feeds"><outline type="rss" overcastId="1" title="Public-domain speech test" xmlUrl="https://example.org/feed"><outline type="podcast-episode" overcastId="101" title="JFK speech test" played="1" progress="11" userUpdatedDate="{now}" enclosureUrl="https://raw.githubusercontent.com/openai/whisper/main/tests/jfk.flac"/></outline></outline></body></opml>'''.encode()
        import_export(store,export)
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning',access_log=False,loop='asyncio'))
        thread=Thread(target=server.run,daemon=True);thread.start()
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:time.sleep(.02)
        assert server.started
        started=time.monotonic()
        try:
            run_worker(settings.origin,settings.worker_token,folder/'worker',once=True,model=DEFAULT_MODEL)
            content=store.reader('101')
            assert content['episode']['status']=='ready',store.status()
            assert content['segments'] and store.search('country')
            elapsed=time.monotonic()-started
            result={'at':datetime.now(timezone.utc).isoformat(),'model':DEFAULT_MODEL,
                'elapsed_seconds_including_model_acquisition':round(elapsed,2),
                'child_peak_rss_mb':round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/1024**2,1),
                'segments':len(content['segments']),'words':content['episode']['words'],
                'audio_sha256':content['provenance']['audio_sha256'],'search_country_matches':len(store.search('country')),
                'spool_empty':not list((folder/'worker'/'results').iterdir()),'private_test_database_only':True}
            output=Path('var');output.mkdir(exist_ok=True)
            (output/'local-smoke.json').write_text(json.dumps(result,indent=2)+'\n')
            (output/'local-smoke-transcript.txt').write_text('\n'.join(s['text'] for s in content['segments'])+'\n')
            print(json.dumps(result,indent=2))
        finally:
            server.should_exit=True;thread.join(15)


if __name__=='__main__':main()
