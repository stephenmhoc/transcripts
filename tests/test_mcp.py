from dataclasses import replace
from threading import Thread
import socket
import time

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from transcripts.web import create_app
from transcripts.discovery import import_export
from conftest import export,result_for


@pytest.fixture
def live(settings):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    settings=replace(settings,origin=f'http://127.0.0.1:{port}')
    app=create_app(settings)
    store=app.state.store
    import_export(store,export());job=store.claim();store.complete(job['id'],result_for(job))
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False,loop='asyncio'))
    thread=Thread(target=server.run,daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and time.monotonic()<deadline:time.sleep(.02)
    assert server.started
    yield settings
    server.should_exit=True;thread.join(10)
    assert not thread.is_alive()


async def test_real_sdk_initialization_tools_search_and_pagination(live):
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+live.mcp_token}) as http:
        async with streamable_http_client(live.origin+'/mcp',http_client=http) as (read,write,*_):
            async with ClientSession(read,write) as session:
                init=await session.initialize()
                assert init.server_info.name=='Transcripts'
                tools=await session.list_tools()
                assert {x.name for x in tools.tools}=={'search_transcripts','list_podcasts','list_episodes','get_transcript','listening_history','pipeline_status'}
                assert all(t.annotations.read_only_hint for t in tools.tools)
                found=await session.call_tool('search_transcripts',{'query':'durable'})
                assert not found.is_error and 'durable' in str(found.content).lower()
                page=await session.call_tool('get_transcript',{'episode_id':'101','limit':1})
                assert not page.is_error and 'next_offset' in str(page.content)
                podcasts=await session.call_tool('list_podcasts',{})
                assert 'SECRET' not in str(podcasts.content)
                templates=await session.list_resource_templates()
                assert templates.resource_templates


async def test_mcp_rejects_owner_worker_and_missing_token(live):
    async with httpx2.AsyncClient() as client:
        for token in ['',live.owner_token,live.worker_token]:
            headers={'Authorization':'Bearer '+token} if token else {}
            response=await client.post(live.origin+'/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{}})
            assert response.status_code==401
