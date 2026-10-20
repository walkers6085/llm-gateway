"""Явный обмен поддерживаемыми настройками OpenCode без SDK и выполнения конфигурации."""

import copy
import hashlib
import json
from pathlib import Path
import re
import uuid
import os
import tempfile

from .credentials import protect, register
from .storage import atomic_write
from .data import expand_home


def read_config(path):
    path = Path(expand_home(path)).expanduser()
    protect(path)
    with path.open('rb') as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError('Конфиг OpenCode превышает 1 МиБ')
    text = raw.decode('utf-8-sig');output=[];i=0;quoted=False;escape=False
    while i < len(text):
        char=text[i]
        if quoted:
            output.append(char)
            if escape:escape=False
            elif char=='\\':escape=True
            elif char=='"':quoted=False
            i+=1;continue
        if char=='"':quoted=True;output.append(char);i+=1;continue
        if text.startswith('//',i):
            end=text.find('\n',i);i=len(text) if end<0 else end;output.append(' ');continue
        if text.startswith('/*',i):
            end=text.find('*/',i+2)
            if end<0:raise ValueError('Незакрытый комментарий JSONC')
            output.append(' ');i=end+2;continue
        if char==',':
            # После удаления комментариев запятые обрабатываются вторым проходом.
            output.append(char)
        else:output.append(char)
        i+=1
    text=''.join(output);output=[];quoted=False;escape=False
    for i,char in enumerate(text):
        if quoted:
            output.append(char)
            if escape:escape=False
            elif char=='\\':escape=True
            elif char=='"':quoted=False
        elif char=='"':quoted=True;output.append(char)
        elif char==',':
            end=i+1
            while end<len(text) and text[end].isspace():end+=1
            if end>=len(text) or text[end] not in '}]':output.append(char)
        else:output.append(char)
    value=json.loads(''.join(output))
    if not isinstance(value,dict):raise ValueError('OpenCode: нужен JSON-объект')
    return value


def supported(value, keys, path):
    if not isinstance(value,dict):raise ValueError(path+': нужен объект')
    extra=set(value)-set(keys)
    if extra:raise ValueError(path+': неподдерживаемые поля: '+', '.join(sorted(extra)))


def prepare(manager, path, *, replace=False):
    source=read_config(path)
    incoming=source.get('provider')
    if not isinstance(incoming,dict) or not incoming:raise ValueError('OpenCode: нужен непустой provider с явными baseURL')
    for provider in incoming.values():
        if isinstance(provider,dict) and isinstance(provider.get('options'),dict):
            key=provider['options'].get('apiKey')
            if isinstance(key,str) and not key.startswith('{env:'):register(key)
    supported(source, ('$schema','provider','model','disabled_providers','enabled_providers'), 'OpenCode')
    for key in ('disabled_providers','enabled_providers'):
        if key in source and (not isinstance(source[key],list) or not all(isinstance(n,str) for n in source[key])):
            raise ValueError(key+': нужен список ID провайдеров')
    candidate=copy.deepcopy(manager.config);secrets={};names=[]
    for name, provider in incoming.items():
        supported(provider, ('name','npm','options','models'), 'provider.'+name)
        if provider.get('npm','@ai-sdk/openai-compatible')!='@ai-sdk/openai-compatible':
            raise ValueError(name+': поддерживается только @ai-sdk/openai-compatible (chat/completions), без SDK/Responses/OAuth')
        options=provider.get('options',{})
        supported(options,('baseURL','apiKey'),'provider.'+name+'.options')
        if not isinstance(options.get('baseURL'),str):raise ValueError(name+': задайте явный options.baseURL')
        existing=candidate['providers'].get(name)
        if existing and not replace:raise ValueError(name+': провайдер уже существует; для объединения используйте --replace')
        entry=copy.deepcopy(existing or {'id':uuid.uuid4().hex,'model':'','models':[],'token_env':'','auth':'none'})
        if existing and existing.get('tls'):raise ValueError(name+': у существующего подключения есть mTLS; импорт не изменяет его автоматически')
        url=options['baseURL'].rstrip('/')
        if existing and existing['url'].rstrip('/')!=url and existing.get('auth','env' if existing['token_env'] else 'none') in ('stored','env') and not options.get('apiKey'):
            raise ValueError(name+': для нового URL нужен явно заданный apiKey или env-ссылка')
        entry['url']=url
        if 'name' in provider:entry['name']=provider['name']
        if 'apiKey' in options:
            key=options['apiKey']
            if not isinstance(key,str) or not key:raise ValueError(name+': apiKey должен быть непустой строкой')
            env=re.fullmatch(r'\{env:([A-Za-z_][A-Za-z0-9_]*)\}',key)
            if env:entry.update(auth='env',token_env=env[1])
            elif '{env:' in key or '{file:' in key:raise ValueError(name+': поддерживается только целая ссылка {env:NAME}, file-ссылки не читаются')
            else:
                identity=entry.get('id') or 'legacy:'+name
                secrets[identity]={'type':'bearer','token':key,'endpoint_hash':hashlib.sha256(url.encode()).hexdigest()}
                entry.update(auth='stored',token_env='')
        models=provider.get('models',{})
        if not isinstance(models,dict):raise ValueError(name+'.models: нужен объект')
        for model, spec in models.items():
            supported(spec,('id','name','limit','options','cost'),name+'.models.'+model)
            if 'cost' in spec:
                supported(spec['cost'],('input','output'),name+'.models.'+model+'.cost')
                if spec['cost']!={'input':0,'output':0}:raise ValueError(name+'/'+model+': поддерживается только нулевая стоимость free')
                tags=entry.setdefault('tags',{}).setdefault(model,[])
                if 'free' not in tags:tags.append('free')
            limit=spec.get('limit',{});supported(limit,('context','output'),name+'.models.'+model+'.limit')
            params=spec.get('options',{});supported(params,('temperature','topP','reasoningEffort'),name+'.models.'+model+'.options')
            profile=copy.deepcopy(entry.get('model_profiles',{}).get(model,{}))
            if 'output' in limit:profile['max_output_tokens']=limit['output']
            if 'context' in limit:
                context=limit['context'];output=limit.get('output',profile.get('max_output_tokens',1024))
                if type(context) is not int or context<1 or output is not None and (type(output) is not int or output<1 or output>=context):
                    raise ValueError(name+'/'+model+': нужны положительные лимиты, output меньше context')
                # Без явного output сохраняем резерв штатного лимита ответа 1024.
                profile['max_input_tokens']=context-(output or 1024)
                if profile['max_input_tokens']<1:raise ValueError(name+'/'+model+': context слишком мал для резерва ответа')
            native=profile.setdefault('options',{})
            for external,internal in (('temperature','temperature_value'),('topP','top_p'),('reasoningEffort','reasoning_effort')):
                if external in params:native[internal]=params[external]
            if 'id' in spec:native['api_model']=spec['id']
            entry.setdefault('model_profiles',{})[model]=profile
            if 'name' in spec:entry.setdefault('model_names',{})[model]=spec['name']
        entry['models']=list(dict.fromkeys([*entry['models'],*models]))
        entry['enabled']=name not in source.get('disabled_providers',[]) and ('enabled_providers' not in source or name in source['enabled_providers'])
        candidate['providers'][name]=entry;names.append(name)
    if 'model' in source:
        selection=source['model']
        if not isinstance(selection,str) or '/' not in selection:raise ValueError('model: ожидается provider/model')
        name,model=selection.split('/',1)
        if name not in incoming or model not in candidate['providers'][name]['models'] or not candidate['providers'][name]['enabled']:
            raise ValueError('model: выберите включённый провайдер и модель из импортируемого каталога')
        candidate['default']=name;candidate['selected']=True;candidate['providers'][name]['model']=model
    manager._validate_candidate(candidate)
    return candidate,secrets,names


def import_config(manager,path,*,replace=False,dry_run=True):
    candidate,secrets,names=prepare(manager,path,replace=replace)
    if not dry_run:manager.save_connections(candidate,secrets)
    return names


def export_config(manager,path,*,providers=None):
    document={'$schema':'https://opencode.ai/config.json','provider':{}};variables=[];disabled=[]
    selected=providers or list(manager.config['providers'])
    for name in selected:
        p=manager.get(name)
        if p.get('tls'):raise ValueError(name+': mTLS не переносится в OpenCode автоматически')
        if any(tags!=['free'] for tags in p.get('tags',{}).values()) or p.get('options'):
            raise ValueError(name+': теги/параметры протокола gwagent не имеют точного отображения; используйте отдельный реестр для экспорта поддерживаемого поднабора')
        options={'baseURL':p['url']}
        mode=p.get('auth','env' if p['token_env'] else 'none')
        if mode=='stored':
            if manager.credentials.get(manager.identity(name)).get('type') not in ('bearer','none'):
                raise ValueError(name+': Basic-авторизация не экспортируется')
            if manager.credentials.get(manager.identity(name)).get('type')=='bearer':
                variable='GW_'+name.upper().replace('-','_')+'_API_KEY';variables.append(variable);options['apiKey']='{env:'+variable+'}'
        elif mode=='env':options['apiKey']='{env:'+p['token_env']+'}'
        models={}
        for model in dict.fromkeys([*p['models'],*p.get('model_profiles',{}),*p.get('model_names',{}),*p.get('tags',{}),*([p['model']] if p['model'] else [])]):
            profile=p.get('model_profiles',{}).get(model,{})
            if 'max_turn_tokens' in profile:raise ValueError(name+'/'+model+': бюджет хода gwagent не экспортируется')
            params=profile.get('options',{})
            if set(params)-{'api_model','temperature_value','top_p','reasoning_effort'}:
                raise ValueError(name+'/'+model+': флаги протокола не имеют точного отображения в OpenCode')
            spec={'name':p.get('model_names',{}).get(model,model)}
            if 'free' in p.get('tags',{}).get(model,[]):spec['cost']={'input':0,'output':0}
            if 'api_model' in params:spec['id']=params['api_model']
            if 'max_input_tokens' in profile:spec.setdefault('limit',{})['context']=profile['max_input_tokens']+profile.get('max_output_tokens',1024)
            if 'max_output_tokens' in profile:spec.setdefault('limit',{})['output']=profile['max_output_tokens']
            values={external:params[internal] for external,internal in (('temperature','temperature_value'),('topP','top_p'),('reasoningEffort','reasoning_effort')) if internal in params}
            if values:spec['options']=values
            models[model]=spec
        document['provider'][name]={'npm':'@ai-sdk/openai-compatible','name':p.get('name',name),'options':options,'models':models}
        if not p.get('enabled',True):disabled.append(name)
    default=manager.config['default'];model=manager.get(default)['model'] if default in selected else ''
    if model and not manager.get(default).get('enabled',True):raise ValueError('Провайдер модели по умолчанию отключён; выберите включённый перед экспортом')
    if model:document['model']=default+'/'+model
    if disabled:document['disabled_providers']=disabled
    path=Path(expand_home(path)).expanduser()
    if path.exists() or path.is_symlink():raise ValueError('Файл экспорта уже существует; выберите новый путь')
    if path.resolve() in (manager.path.resolve(),manager.credentials.path.resolve()):raise ValueError('Нельзя заменить реестр или ключи экспортом')
    # O_EXCL защищает от появления файла между проверкой и публикацией.
    data=(json.dumps(document,ensure_ascii=False,indent=2)+'\n').encode()
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temporary=tempfile.mkstemp(prefix='.opencode-',dir=path.parent)
    os.close(fd)
    try:
        atomic_write(temporary,data)
        os.link(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return variables
