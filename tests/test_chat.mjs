// Проверки сценариев страницы без браузерной зависимости: DOM и fetch подставляются.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const script=readFileSync(new URL('../chat/index.html',import.meta.url),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
  constructor(){this.value='';this.children=[];this.listeners={};this.style={};this.dataset={};this.checked=false;this.classList={add:()=>{},contains:()=>false};this.parts={};}
  appendChild(el){this.children.push(el);return el;}
  querySelector(selector){return this.parts[selector]??=new Element();}
  querySelectorAll(){return [new Element(),new Element(),new Element()];}
  addEventListener(kind,handler){this.listeners[kind]=handler;}
  scrollIntoView(){} remove(){} focus(){}
}
function setup(saved={}) {
  const elements={}, storage=new Map(Object.entries(saved));
  const context=vm.createContext({console,TextDecoder,AbortController,performance,Response,
    localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    document:{getElementById:id=>elements[id]??=new Element(),createElement:()=>new Element(),querySelector:()=>null},
    fetch:async()=>new Response(JSON.stringify({data:[]}),{headers:{'content-type':'application/json'}})});
  vm.runInContext(script,context);
  return {context,elements,storage,run:s=>vm.runInContext(s,context)};
}
function sse(events) {return new Response(events.map(x=>'data: '+(typeof x==='string'?x:JSON.stringify(x))+'\r\n\r\n').join(''));}

test('HTML экранируется, токен сохраняется только после выбора',()=>{
  const {run,elements,storage}=setup({gw_token:'old-secret'});
  assert.equal(elements.token.value,'');assert.equal(storage.has('gw_token'),false);
  assert(!run('renderMarkdown("<img src=x onerror=alert(1)>")').includes('<img'));
  elements.token.value='new-key';elements.remember.checked=true;elements.remember.listeners.change();
  assert.equal(storage.get('gw_token'),'new-key');
  elements.remember.checked=false;elements.remember.listeners.change();assert(!storage.has('gw_token'));
});
test('успешный поток попадает в историю, оборванный — нет',async()=>{
  const {context,elements,run}=setup();elements.token.value='dummy';elements.model.value='model';
  context.fetch=async()=>sse([{choices:[{delta:{content:'готово'},finish_reason:'stop'}]},'[DONE]']);
  await run('send("привет")');assert.equal(run('history.length'),2);
  context.fetch=async()=>sse([{choices:[{delta:{content:'часть'}}]}]);
  await run('send("второй")');assert.equal(run('history.length'),2);assert.equal(elements.status.textContent,'ошибка');
});
test('агент: карточка, подтверждение и завершение',async()=>{
  const {context,elements,run}=setup();elements.model.value='model';elements.token.value='dummy';
  const requests=[];
  context.fetch=async(url,options)=>{
    requests.push([url,options]);
    if(url.endsWith('/messages'))return sse([{type:'tool_call',id:'c1',name:'shell',arguments:{}},
      {type:'permission_request',id:'c1',name:'shell',arguments:{},rule:'shell'}, {type:'done',reason:'stop'}]);
    if(options?.method==='POST' && url==='/agent/sessions')return Response.json({id:'s1'});
    return Response.json({data:[{id:'s1'}]});
  };
  await run('sendAgent("задача")');
  assert.equal(run('Object.keys(cards).length'),1);
  const box=run('cards.c1.children[0]');await box.listeners.click({target:{dataset:{d:'allow'}}});
  assert(requests.some(([url,options])=>url.endsWith('/approve') && JSON.parse(options.body).id==='c1'));
  assert.equal(elements.input.disabled,false);
});
test('SSE принимает многострочный JSON и завершающий блок без пустой строки',async()=>{
  const {context,run}=setup();context.response=new Response('data: {"value":\ndata: 42}\n\ndata: [DONE]');
  const values=await run('(async()=>{const out=[];for await(const e of readSSE(response))out.push(e);return out})()');
  assert.equal(values[0].value,42);assert.equal(values[1].stream_done,true);
});
