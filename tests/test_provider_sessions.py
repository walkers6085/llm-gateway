"""Привязка сессии к каталогу и неизменному назначению подключения."""

import io
import json

from agent.cli import amain
from agent.providers import ProviderManager
from tests.fakes import SCRIPT
from tests.test_cli import gw, run_cli
from agent.store import SessionStore


async def test_resume_rejects_other_workspace_and_changed_endpoint(gw):
    cfg,ws,db=gw
    path=ws/'providers.json';manager=ProviderManager(path)
    url=json.loads(cfg.read_text())['gateway']['url']
    manager.add('local',url,'GW_TOKEN','alpha')
    args=['--settings',str(cfg),'--providers-file',str(path),'--provider','local','--cwd',str(ws)]
    SCRIPT.append({'content':'ok'})
    out,err,read=run_cli([])
    assert await amain(args+['-p','hello'],read=read,out=out,err=err)==0
    store=SessionStore(db);[session]=store.list('local');store.close()
    other=ws/'other';other.mkdir()
    out,err,read=run_cli([])
    assert await amain(args+['--cwd',str(other),'--resume',session.id,'-p','next'],out=out,err=err)==2
    assert '--cwd' in err.getvalue()
    manager.update('local',url='http://changed.invalid/v1')
    out,err,read=run_cli([])
    assert await amain(args+['--resume',session.id,'-p','next'],out=out,err=err)==2
    assert 'изменилось' in err.getvalue()
