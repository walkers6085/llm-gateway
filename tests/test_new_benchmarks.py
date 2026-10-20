"""Бенчмарки без явных подсказок: выбор, восстановление и отсутствующий факт."""

import json
import re

from bench.client import Completion
from bench.runner import Context, build_config
from bench.suites import BENCHES


class Model:
    async def complete(self, model, messages, **kwargs):
        prompt = messages[0]['content']
        def call(name,args,index=1):
            return Completion(status=200,tool_calls=[{'id':f'c{index}','type':'function','function':{'name':name,'arguments':json.dumps(args)}}])
        if 'В архиве проект' in prompt:
            project=re.search(r'Назови код записи, связанной с проектом (.+?)\.',prompt)[1]
            record=re.search(r'проект '+re.escape(project)+r' связан с записью ([\w-]+)',prompt)[1]
            value=re.search(r'запись '+record+r' имеет код ([0-9a-f]+)',prompt)
            return Completion(status=200,text=value[1] if value else 'НЕТ ДАННЫХ')
        if prompt.startswith('Найди товар'):
            if len(messages)==1:
                return call('find_product',{'name':re.search('«(.+?)»',prompt)[1]})
            output=json.loads(messages[-1]['content'])
            if len(messages)==3:
                return call('get_price',{'product_id':output['product_id']},2)
            if 'error' in output:
                return call('get_price',json.loads(messages[-2]['tool_calls'][0]['function']['arguments']),3)
            return Completion(status=200,text=str(output['price']))
        if 'ПРИВЕТ' in prompt:
            return Completion(status=200,text='ПРИВЕТ')
        assert 'weather' not in prompt and 'multiply' not in prompt
        if 'погоду' in prompt:
            return call('weather',{'city':prompt.split('город ')[1].split('.')[0]})
        a,b=map(int,re.search(r'(\d+) \* (\d+)',prompt).groups())
        return call('multiply',{'a':a,'b':b})


async def test_new_suites():
    config=build_config(BENCHES,quick=True)
    ctx=Context(Model(),'model',config);ctx.shared['cpt_ru']=3.2
    for name in ('tool_choice','recovery','retrieval'):
        result=await BENCHES[name].run(ctx)
        assert result.score==100, result.details
        assert not result.infrastructure_errors
        if name=='recovery': assert len(result.details[0]['trace'])==4
        if name=='retrieval':
            assert result.metrics['useful_context_tokens']==1024
            assert any(row['missing'] for row in result.details)
