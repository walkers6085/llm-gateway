"""Регрессии лимитов, жизненного цикла и изменения прав серверного агента."""

import asyncio
import copy

import httpx

from agent.llm import LLMClient
from agent.settings import DEFAULTS
from gateway.agent_api import AgentService, Run
from gateway.app import create_app
from gateway.config import Settings
from gateway.tokens import TokenStore


async def test_cancel_during_send_releases_gateway_slot(tmp_path):
    entered=asyncio.Event()
    calls=[]
    async def handler(req):
        calls.append(req)
        if len(calls)==1:
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200,json={'choices':[]})
    store=TokenStore(str(tmp_path/'gateway.db'));raw,_=store.create('audit')
    app=create_app(Settings(db_path=str(tmp_path/'gateway.db'),upstream_base_url='http://up/v1',model_max_connections_default=1),store,httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(base_url='http://gw',transport=httpx.ASGITransport(app=app)) as client:
            async def request():return await client.post('/v1/chat/completions',json={'model':'model'},headers={'authorization':f'Bearer {raw}'})
            task=asyncio.create_task(request());await entered.wait();task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            assert (await asyncio.wait_for(request(),2)).status_code==200
    assert len(calls)==2
    store.close()


async def test_health_requires_success_and_live_does_not(tmp_path):
    app=create_app(Settings(db_path=str(tmp_path/'gateway.db'),upstream_base_url='http://up/v1'),transport=httpx.MockTransport(lambda _:httpx.Response(401)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(base_url='http://gw',transport=httpx.ASGITransport(app=app)) as client:
            assert (await client.get('/health')).status_code==503
            assert (await client.get('/live')).status_code==200


async def test_chunked_body_limit_before_full_read(tmp_path):
    consumed=[]
    async def chunks():
        for i in range(100):
            consumed.append(i)
            yield b'x'*10
    app=create_app(Settings(db_path=str(tmp_path/'gateway.db'),max_request_body_bytes=20),transport=httpx.MockTransport(lambda _:httpx.Response(200)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(base_url='http://gw',transport=httpx.ASGITransport(app=app)) as client:
            assert (await client.post('/agent/sessions',content=chunks())).status_code==413
    assert len(consumed)==3


async def test_profile_cache_is_rebuilt_and_queue_is_bounded(tmp_path):
    store=TokenStore(str(tmp_path/'gateway.db'));_,token=store.create('audit',agent_profile='default')
    async with httpx.AsyncClient(base_url='http://up/v1') as client:
        service=AgentService(Settings(db_path=str(tmp_path/'gateway.db')),store,lambda:client,lambda:{})
        session=service.sessions.create(str(token.id),workspace=str(tmp_path),model='alpha')
        old=copy.deepcopy(DEFAULTS);old['tools']['enabled']=['shell']
        first=await service.agent_for(session,token,old,Run())
        old['tools']['enabled']=[] # Даже изменение того же объекта не меняет снимок агента.
        second=await service.agent_for(session,token,old,Run())
        assert first is not second and second.registry.names()==["subagent"]
        assert 0 < Run().queue.maxsize <= 128
        await service.close()
    store.close()


async def test_llm_shared_semaphore_released_after_error():
    sem=asyncio.Semaphore(1)
    async with httpx.AsyncClient(base_url='http://up',transport=httpx.MockTransport(lambda _:httpx.Response(503))) as http:
        llm=LLMClient(http,limiter=lambda _:sem)
        for _ in range(2):
            try:
                _=[e async for e in llm.stream('model',[])]
            except Exception:pass
            assert sem._value==1
