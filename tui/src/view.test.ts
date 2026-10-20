import {test,expect} from 'bun:test';
import {createTestRenderer} from '@opentui/core/testing';
import {AgentView,type Command} from './view.ts';

test('настоящие кадры: поток, Markdown, таблица JSON, исходник и узкий терминал',async()=>{
  const setup=await createTestRenderer({width:110,height:36});
  const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'test',model:'alpha',workspace:'/tmp'},provider:'local'});
    view.accept({type:'ready'});
    view.accept({type:'text',text:'**Важно:** сохраните результат.\n\n```json\n[{"Модель":"альфа","Балл":42}]\n```'});
    view.accept({type:'done',reason:'stop'});
    await setup.flush();
    let frame=setup.captureCharFrame();
    expect(frame).toContain('альфа');expect(frame).toContain('42');expect(frame).toContain('ВАЖНО');
    setup.mockInput.pressKey('r',{ctrl:true});await setup.flush();
    expect(setup.captureCharFrame()).toContain('```json');
    setup.mockInput.pressKey('r',{ctrl:true});
    setup.resize(65,30);await setup.flush();
    frame=setup.captureCharFrame();expect(frame).toContain('сохраните результат');expect(frame).toContain('альфа');
  }finally{view.dispose();await setup.dispose();}
});

test('подтверждения, скрытый ввод и вставка, отмена и карточки инструментов',async()=>{
  const setup=await createTestRenderer({width:100,height:36});
  const sent:Command[]=[];let exits=0;
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{exits++;});
  try{
    view.accept({type:'ready'});
    for(const [key,decision] of [['n','deny'],['y','allow'],['a','always']]){
      view.accept({type:'permission_request',id:key,name:'shell',arguments:{command:'echo ok'},reason:'ask',rule:'shell(*)'});
      setup.mockInput.pressKey(key!);expect(sent.at(-1)).toEqual({type:'approve',id:key,decision});
    }
    view.accept({type:'input_request',id:'secret',prompt:'Ключ:',secret:true});
    await setup.mockInput.typeText('секрет');await setup.mockInput.pasteBracketedText('-ключ');
    await setup.flush();expect(setup.captureCharFrame()).not.toContain('секрет');expect(setup.captureCharFrame()).not.toContain('-ключ');
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'answer',id:'secret',value:'секрет-ключ'});
    view.accept({type:'idle'});
    view.accept({type:'tool_call',id:'call',name:'read_file',arguments:{path:'x'}});
    view.accept({type:'tool_result',id:'call',content:'полный результат',ok:true,ms:2});
    await setup.flush();expect(setup.captureCharFrame()).toContain('read_file');
    setup.mockInput.pressKey('o',{ctrl:true});await setup.flush();expect(setup.captureCharFrame()).toContain('"path"');
    view.editor.setText('запрос');view.submit();expect(sent.at(-1)).toEqual({type:'submit',text:'запрос'});
    setup.mockInput.pressCtrlC();expect(sent.at(-1)).toEqual({type:'cancel'});
    setup.mockInput.pressKey('d',{ctrl:true});expect(exits).toBe(1);
  }finally{view.dispose();await setup.dispose();}
});


test('без модели можно писать, после ошибки подключения доступны команды',async()=>{
  const setup=await createTestRenderer({width:100,height:30});
  const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'offline',model:'',workspace:'/tmp'},provider:'gateway'});
    view.accept({type:'ready'});
    await setup.flush();expect(setup.captureCharFrame()).toContain('модель не выбрана');
    await setup.mockInput.typeText('привет');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'submit',text:'привет'});
    view.accept({type:'error',message:'Нет ключа. Настройте /connect'});
    view.accept({type:'done',reason:'error'});view.accept({type:'idle'});
    await setup.mockInput.typeText('/connect local');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'submit',text:'/connect local'});
    view.accept({type:'input_request',id:'url',prompt:'URL API:',secret:false});
    await setup.mockInput.typeText('http://localhost:7700/v1');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'answer',id:'url',value:'http://localhost:7700/v1'});
  }finally{view.dispose();await setup.dispose();}
});

const catalog=[
  {kind:'command',command:'/help',description:'Справка',arguments:'none'},
  {kind:'command',command:'/connect',description:'Настройка провайдера',arguments:'optional'},
  {kind:'command',command:'/provider add',description:'Добавить подключение',arguments:'required',usage:'/provider add NAME URL [MODEL]'},
  {kind:'command',command:'/models',description:'Доступные модели',arguments:'none'},
  {kind:'command',command:'/tools',description:'Доступные инструменты',arguments:'optional'},
  {kind:'tool',command:'/tools read_file',name:'read_file',description:'Чтение файла',permission:'allow',arguments:'none'},
  {kind:'tool',command:'/tools shell',name:'shell',description:'Запуск команды',permission:'ask',arguments:'none'},
];
function readyWithCatalog(view:AgentView){
  view.accept({type:'snapshot',session:{id:'offline',model:'',workspace:'/tmp'},provider:'gateway',catalog});
  view.accept({type:'ready'});
}

test('слеш открывает меню, фильтр выбирает команды и инструменты, Enter и Tab работают',async()=>{
  const setup=await createTestRenderer({width:100,height:36});
  const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    readyWithCatalog(view);
    await setup.mockInput.typeText('/');await setup.flush();
    let frame=setup.captureCharFrame();
    expect(frame).toContain('Команды и инструменты');expect(frame).toContain('/connect');expect(frame).toContain('read_file');
    setup.mockInput.pressArrow('down');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'submit',text:'/connect'});
    view.accept({type:'idle'});
    await setup.mockInput.typeText('/rea');await setup.flush();
    expect(setup.captureCharFrame()).toContain('/tools read_file');
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'submit',text:'/tools read_file'});
    view.accept({type:'idle'});
    await setup.mockInput.typeText('/provider');setup.mockInput.pressEnter();
    expect(view.editor.plainText).toBe('/provider add ');
    expect(sent).toHaveLength(2); // обязательные аргументы не отправляются автоматически
    view.editor.setText('');await setup.mockInput.typeText('/con');setup.mockInput.pressTab();
    expect(view.editor.plainText).toBe('/connect ');expect(sent).toHaveLength(2);
    await setup.mockInput.typeText('local');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'submit',text:'/connect local'});
  }finally{view.dispose();await setup.dispose();}
});

test('Esc, пустой фильтр, вставка, узкий экран и диалоги не ломают меню',async()=>{
  const setup=await createTestRenderer({width:65,height:24,exitOnCtrlC:false});
  const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    readyWithCatalog(view);
    await setup.mockInput.pasteBracketedText('/');await setup.flush();
    expect(setup.captureCharFrame()).toContain('Команды и инструменты');
    setup.mockInput.pressEscape();await Bun.sleep(50);await setup.flush();
    expect(view.editor.plainText).toBe('/');expect(setup.captureCharFrame()).not.toContain('Команды и инструменты');
    view.editor.setText('');await setup.mockInput.typeText('/read');await setup.flush();
    expect(setup.captureCharFrame()).toContain('/tools read_file');
    setup.mockInput.pressTab();expect(view.editor.plainText).toBe('/tools read_file');expect(sent).toHaveLength(0);
    view.editor.setText('/несуществующее');await setup.flush();expect(setup.captureCharFrame()).toContain('Совпадений нет');
    view.editor.setText('http://example.com/');await setup.flush();expect(setup.captureCharFrame()).not.toContain('Команды и инструменты');
    view.editor.setText('/');setup.resize(65,20);await setup.flush();
    expect(setup.captureCharFrame()).toContain('Команды и инструменты');
    view.accept({type:'input_request',id:'url',prompt:'URL:',secret:false});
    await setup.mockInput.typeText('/models');await setup.flush();
    expect(setup.captureCharFrame()).not.toContain('Команды и инструменты');
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'answer',id:'url',value:'/models'});
    view.accept({type:'idle'});view.editor.setText('/');
    view.accept({type:'input_request',id:'key',prompt:'Ключ:',secret:true});
    await setup.mockInput.typeText('/secret');await setup.flush();
    expect(setup.captureCharFrame()).not.toContain('Команды и инструменты');expect(setup.captureCharFrame()).not.toContain('/secret');
  }finally{view.dispose();await setup.dispose();}
});

function modelList(models:string[],current='beta',sessionId='offline'){
  return {type:'table',title:'/models',columns:['Модель'],rows:models.map(model=>[model]),selection:{kind:'model',current,session_id:sessionId}};
}

test('/models: отмечена текущая модель, стрелки и Enter действительно переключают',async()=>{
  const setup=await createTestRenderer({width:100,height:34});
  const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    readyWithCatalog(view);
    await setup.mockInput.typeText('/models');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'submit',text:'/models'});
    view.accept(modelList(['alpha','beta','gamma']));view.accept({type:'idle'});
    await setup.flush();let frame=setup.captureCharFrame();
    expect(frame).toContain('Выбор модели');expect(frame).toMatch(/beta\s+текущая/);
    setup.mockInput.pressArrow('up');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'submit',text:'/model alpha'});
    view.accept({type:'snapshot',session:{id:'offline',model:'alpha',workspace:'/tmp'},provider:'gateway',catalog});
    view.accept({type:'idle'});await setup.flush();frame=setup.captureCharFrame();
    expect(frame).not.toContain('Выбор модели');expect(frame).toContain('gateway / alpha');
    expect(view.editor.plainText).toBe('');
  }finally{view.dispose();await setup.dispose();}
});

test('выбор модели: поиск, пустой результат, Esc, Ctrl+C и прокрутка на узком экране',async()=>{
  const setup=await createTestRenderer({width:65,height:24,exitOnCtrlC:false});
  const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    readyWithCatalog(view);
    const models=Array.from({length:50},(_,i)=>'model-'+String(i).padStart(2,'0'));
    view.accept(modelList(models,'model-45'));await setup.flush();
    expect(setup.captureCharFrame()).toMatch(/model-45\s+текущая/);
    await setup.mockInput.typeText('нет-такой');setup.mockInput.pressEnter();await setup.flush();
    expect(setup.captureCharFrame()).toContain('совпадений нет');expect(sent).toHaveLength(0);
    view.editor.setText('');await setup.mockInput.pasteBracketedText('model-31');await setup.flush();
    expect(setup.captureCharFrame()).toContain('model-31');
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'submit',text:'/model model-31'});
    view.accept({type:'idle'});view.accept(modelList(models,'model-31'));
    await setup.mockInput.typeText('model-4');setup.mockInput.pressEscape();await Bun.sleep(50);await setup.flush();
    expect(setup.captureCharFrame()).not.toContain('Выбор модели');expect(view.editor.plainText).toBe('');expect(sent).toHaveLength(1);
    view.accept(modelList(models,'model-31'));setup.mockInput.pressCtrlC();await setup.flush();
    expect(setup.captureCharFrame()).not.toContain('Выбор модели');expect(sent).toHaveLength(1);
    await setup.mockInput.typeText('/help');setup.mockInput.pressEnter();await setup.flush();expect(setup.captureCharFrame()).toContain('Команды и инструменты');expect(sent).toHaveLength(1);
  }finally{view.dispose();await setup.dispose();}
});

test('пустой каталог и смена сессии не меняют выбранную модель',async()=>{
  const setup=await createTestRenderer({width:100,height:30});
  const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    readyWithCatalog(view);view.accept(modelList([]));await setup.flush();
    expect(setup.captureCharFrame()).toContain('не вернул доступных моделей');expect(sent).toHaveLength(0);
    view.accept(modelList(['alpha','beta']));
    view.accept({type:'snapshot',session:{id:'another-session',model:'gamma',workspace:'/tmp'},provider:'other',catalog});
    await setup.flush();expect(setup.renderer.root.findDescendantById('command-menu')?.visible).toBe(false);expect(sent).toHaveLength(0);
  }finally{view.dispose();await setup.dispose();}
});


test('общий каталог различает одинаковые модели провайдеров и переключает пару',async()=>{
  const setup=await createTestRenderer({width:100,height:34,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    readyWithCatalog(view);
    view.accept({type:'table',title:'/models',rows:[],selection:{kind:'model',provider:'glm',current:'shared',session_id:'offline',choices:[
      {provider:'glm',provider_name:'glm',model:'shared',available:true},
      {provider:'mistral',provider_name:'mistral',model:'shared',available:true},
      {provider:'glm',provider_name:'glm',model:'free-model',available:true,tags:['free']},
      {provider:'broken',provider_name:'broken',model:'cached',available:false},
    ]}});
    await setup.flush();let frame=setup.captureCharFrame();
    expect(frame).toContain('─ glm · 2 ─');expect(frame).toMatch(/shared\s+текущая/);expect(frame).toContain('─ mistral · 1 ─');
    expect(frame).toMatch(/free-model\s+free/);expect(frame).toContain('API недоступен');
    await setup.mockInput.typeText('mistral');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'select',kind:'model',provider:'mistral',model:'shared',session_id:'offline'});
    view.accept({type:'snapshot',provider:'mistral',session:{id:'new-session',model:'shared',workspace:'/tmp'},catalog});view.accept({type:'idle'});
    await setup.flush();expect(setup.captureCharFrame()).toContain('mistral / shared');
  }finally{view.dispose();await setup.dispose();}
});

test('/provider открывает выбор с текущим провайдером и отменяется без переключения',async()=>{
  const setup=await createTestRenderer({width:80,height:28,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  const event={type:'table',title:'/provider',rows:[],selection:{kind:'provider',provider:'glm',session_id:'offline',choices:[
    {provider:'glm',label:'glm',model:'glm-5.1'}, {provider:'mistral',label:'mistral',model:'mistral-small-latest'},
  ]}};
  try{
    readyWithCatalog(view);view.accept(event);await setup.flush();
    expect(setup.captureCharFrame()).toContain('Выбор провайдера');expect(setup.captureCharFrame()).toMatch(/glm\s+текущая/);expect(setup.captureCharFrame()).toContain('Модель: glm-5.1');
    setup.mockInput.pressEscape();await Bun.sleep(50);await setup.flush();expect(sent).toHaveLength(0);
    view.accept(event);setup.mockInput.pressArrow('down');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'select',kind:'provider',provider:'mistral',session_id:'offline'});
  }finally{view.dispose();await setup.dispose();}
});


test('единые сессии: поиск, недоступная папка, выбор и сохранение черновика через F4',async()=>{
  const setup=await createTestRenderer({width:110,height:36,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  const event={type:'table',title:'/sessions',rows:[],selection:{kind:'session',current:'offline',session_id:'offline',choices:[
    {id:'offline',title:'Текущая работа',model:'alpha',provider:'glm',workspace:'/tmp',updated:1,enabled:true},
    {id:'other',title:'Другая папка',model:'beta',provider:'glm',workspace:'/elsewhere',updated:2,enabled:false},
    {id:'old',title:'Доработка CLI',model:'gamma',provider:'mistral',workspace:'/tmp',updated:3,enabled:true},
  ]}};
  try{
    readyWithCatalog(view);view.editor.setText('Сохранить этот черновик');setup.mockInput.pressKey('F4');
    expect(sent.at(-1)).toEqual({type:'submit',text:'/sessions'});
    view.accept(event);view.accept({type:'idle'});await setup.flush();
    expect(setup.captureCharFrame()).toContain('Выбор сессии');expect(setup.captureCharFrame()).toContain('Доработка CLI');
    expect(setup.captureCharFrame()).toContain('недоступно');
    setup.mockInput.pressEscape();await Bun.sleep(50);expect(view.editor.plainText).toBe('Сохранить этот черновик');
    setup.mockInput.pressKey('F4');view.accept(event);view.accept({type:'idle'});
    await setup.mockInput.typeText('CLI');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'select',kind:'session',target:'old',session_id:'offline'});
    expect(view.editor.plainText).toBe('Сохранить этот черновик');
  }finally{view.dispose();await setup.dispose();}
});

test('системные роли, Ctrl+P, быстрые вызовы и списки инструментов используют общие окна',async()=>{
  const setup=await createTestRenderer({width:110,height:42,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    readyWithCatalog(view);
    view.accept({type:'notice',message:'Подключение выбрано'});view.accept({type:'warning',message:'Ограничение API'});view.accept({type:'error',message:'Не удалось прочитать файл'});
    await setup.flush();let frame=setup.captureCharFrame();
    expect(frame).toContain('· система');expect(frame).toContain('! внимание');expect(frame).toContain('× ошибка');
    view.editor.setText('Черновик');setup.mockInput.pressKey('p',{ctrl:true});await setup.flush();
    expect(setup.captureCharFrame()).toContain('Команды и инструменты');
    setup.mockInput.pressEscape();await Bun.sleep(50);expect(view.editor.plainText).toBe('Черновик');
    setup.mockInput.pressKey('F2');expect(sent.at(-1)).toEqual({type:'submit',text:'/models'});view.accept({type:'idle'});
    setup.mockInput.pressKey('F3');expect(sent.at(-1)).toEqual({type:'submit',text:'/provider'});view.accept({type:'idle'});
    view.accept({type:'table',title:'/tools',rows:[],selection:{kind:'tool',session_id:'offline',choices:[
      {key:'read_file',label:'read_file',description:'Прочитать файл',group:'Встроенные инструменты',tags:['allow'],command:'/tools read_file'},
      {key:'mcp__test__echo',label:'mcp__test__echo',description:'Повторить текст',group:'MCP',tags:['ask'],command:'/tools mcp__test__echo'},
    ]}});await setup.flush();frame=setup.captureCharFrame();
    expect(frame).toContain('Встроенные инструменты');expect(frame).toContain('MCP');
    setup.mockInput.pressTab();setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'submit',text:'/tools mcp__test__echo'});
  }finally{view.dispose();await setup.dispose();}
});

test('диалоги: авторизация стрелками, отмена секретов, Esc отклоняет разрешение',async()=>{
  const setup=await createTestRenderer({width:90,height:32,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    readyWithCatalog(view);
    view.accept({type:'input_request',id:'auth',prompt:'Авторизация',secret:false,choices:[{value:'bearer',label:'Bearer'},{value:'basic',label:'Basic'}]});
    setup.mockInput.pressArrow('down');setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'answer',id:'auth',value:'basic'});
    view.accept({type:'input_request',id:'secret-esc',prompt:'Ключ',secret:true});
    await setup.mockInput.typeText('private-secret');setup.mockInput.pressEscape();await Bun.sleep(50);await setup.flush();
    expect(sent.at(-1)).toEqual({type:'cancel'});expect(setup.captureCharFrame()).not.toContain('private-secret');
    view.accept({type:'idle'});view.accept({type:'permission_request',id:'deny-esc',name:'shell',arguments:{command:'echo test'},reason:'ask',rule:'shell(*)'});
    setup.mockInput.pressEscape();await Bun.sleep(50);
    expect(sent.at(-1)).toEqual({type:'approve',id:'deny-esc',decision:'deny'});
  }finally{view.dispose();await setup.dispose();}
});


test('горячая клавиша при запуске не теряется и открывает окно после ready',async()=>{
  const setup=await createTestRenderer({width:90,height:28,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    setup.mockInput.pressKey('F4');expect(sent).toHaveLength(0);
    readyWithCatalog(view);expect(sent.at(-1)).toEqual({type:'submit',text:'/sessions'});
    view.accept({type:'table',rows:[],selection:{kind:'session',session_id:'offline',current:'offline',choices:[
      {id:'offline',title:'Текущая',model:'alpha',provider:'gateway',workspace:'/tmp',updated:1,enabled:true},
    ]}});view.accept({type:'idle'});await setup.flush();expect(setup.captureCharFrame()).toContain('Выбор сессии');
  }finally{view.dispose();await setup.dispose();}
});

test('reasoning скрыт по умолчанию, раскрывается и восстанавливается из истории; индикатор виден',async()=>{
  const setup=await createTestRenderer({width:110,height:40});
  const view=new AgentView(setup.renderer,()=>{},()=>{});
  try{
    view.accept({type:'ready'});view.editor.setText('задача');view.submit();
    view.accept({type:'reasoning',text:'Проверяю варианты',request_id:'request-123'});
    await Bun.sleep(200);await setup.flush();let frame=setup.captureCharFrame();
    expect(frame).toContain('Reasoning');expect(frame).not.toContain('Проверяю варианты');
    expect(frame).toContain('Модель рассуждает');expect(frame).toContain('request-123');
    const thoughtRow=frame.split('\n').findIndex(line=>line.includes('Reasoning'));
    await setup.mockMouse.click(6,thoughtRow);await setup.flush();expect(setup.captureCharFrame()).toContain('Проверяю варианты');
    await setup.mockMouse.click(6,thoughtRow);await setup.flush();expect(setup.captureCharFrame()).not.toContain('Проверяю варианты');
    setup.mockInput.pressKey('t',{ctrl:true});await setup.flush();expect(setup.captureCharFrame()).toContain('Проверяю варианты');
    setup.mockInput.pressKey('t',{ctrl:true});view.accept({type:'done',reason:'stop'});view.accept({type:'idle'});
    view.accept({type:'snapshot',provider:'test',session:{id:'s',model:'m',workspace:'/tmp'},history:[{role:'assistant',content:'Итог',reasoning_content:'Сохранённые рассуждения'}]});
    await setup.flush();expect(setup.captureCharFrame()).not.toContain('Сохранённые рассуждения');
    setup.mockInput.pressKey('t',{ctrl:true});await setup.flush();expect(setup.captureCharFrame()).toContain('Сохранённые рассуждения');
    view.accept({type:'error',message:'Краткая ошибка\nПолная диагностика'});await setup.flush();
    expect(setup.captureCharFrame()).not.toContain('Полная диагностика');
    setup.mockInput.pressKey('e',{ctrl:true});await setup.flush();expect(setup.captureCharFrame()).toContain('Полная диагностика');
    view.accept({type:'tool_call',id:'failure',name:'read_file',arguments:{path:'missing'}});
    view.accept({type:'tool_result',id:'failure',ok:false,content:'Файл не найден\nДетали инструмента',ms:1});
    await setup.flush();expect(setup.captureCharFrame()).not.toContain('Детали инструмента');
    setup.mockInput.pressKey('o',{ctrl:true});await setup.flush();expect(setup.captureCharFrame()).toContain('Детали инструмента');
  }finally{view.dispose();await setup.dispose();}
});

test('метрики ответа сохраняются при raw и snapshot, /revert выбирает сообщение и возвращает черновик',async()=>{
  const setup=await createTestRenderer({width:115,height:42});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  const metrics={duration_ms:819000,completed_at:'2026-10-19T19:25:00Z',tokens_per_second:21.7};
  try{
    readyWithCatalog(view);
    view.accept({type:'text',text:'Результат'});view.accept({type:'assistant',content:'Результат',metrics});view.accept({type:'done',reason:'stop'});
    await setup.flush();let frame=setup.captureCharFrame();
    expect(frame).toContain('Worked for 13m 39s');expect(frame).toContain('21.7 ток/с');
    setup.mockInput.pressKey('r',{ctrl:true});await setup.flush();expect(setup.captureCharFrame()).toContain('Worked for 13m 39s');
    view.accept({type:'snapshot',provider:'local',session:{id:'offline',title:'Название задачи',model:'test',workspace:'/tmp'},history:[{role:'assistant',content:'Сохранённый ответ',metrics}]});
    await setup.flush();expect(setup.captureCharFrame()).toContain('Название задачи');expect(setup.captureCharFrame()).toContain('21.7 ток/с');
    view.accept({type:'table',selection:{kind:'revert',session_id:'offline',choices:[{key:'1',target:'1',label:'Первоначальная задача',description:'Сообщение №1',group:'Ваши сообщения'}]}});
    await setup.flush();expect(setup.captureCharFrame()).toContain('Изменить прошлое сообщение');
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'select',kind:'revert',target:'1',session_id:'offline'});
    view.accept({type:'input_request',id:'mode',prompt:'Выберите режим',choices:[{value:'message',label:'Исправить текст'},{value:'files',label:'Исправить текст и отменить все изменения'}]});
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'answer',id:'mode',value:'message'});
    view.accept({type:'snapshot',provider:'local',session:{id:'offline',title:'Сессия',model:'test',workspace:'/tmp'},history:[]});
    view.accept({type:'draft',text:'Первоначальная задача'});view.accept({type:'idle'});await setup.flush();
    expect(view.editor.plainText).toBe('Первоначальная задача');
    view.editor.setText('Отредактированная задача');view.submit();expect(sent.at(-1)).toEqual({type:'submit',text:'Отредактированная задача'});
  }finally{view.dispose();await setup.dispose();}
});

test('копирование: выделение редактора и вывода, полный ответ, Ctrl+C сохраняет отмену без выделения',async()=>{
  const setup=await createTestRenderer({width:110,height:42,exitOnCtrlC:false,kittyKeyboard:true});
  const sent:Command[]=[];const copied:string[]=[];let exits=0;
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{exits++;},async text=>{copied.push(text);return 'written';});
  try{
    view.accept({type:'ready'});view.editor.setText('Русский текст 🐈');
    setup.mockInput.pressKey('a',{ctrl:true});setup.mockInput.pressKey('c',{ctrl:true});await setup.flush();
    expect(copied.at(-1)).toBe('Русский текст 🐈');expect(exits).toBe(0);expect(view.editor.plainText).toBe('Русский текст 🐈');
    view.editor.clearSelection();view.editor.setText('Весь черновик');
    setup.mockInput.pressKey('c',{ctrl:true,shift:true});await setup.flush();expect(copied.at(-1)).toBe('Весь черновик');
    view.accept({type:'text',text:'Ответ для выделения'});view.accept({type:'assistant',content:'Ответ для выделения'});view.accept({type:'done',reason:'stop'});
    setup.renderer.clearSelection();await setup.flush();const frame=setup.captureCharFrame();expect(frame).toContain('Ответ для выделения');const row=frame.split('\n').findIndex(line=>line.includes('Ответ для выделения'));
    const column=frame.split('\n')[row]!.indexOf('Ответ');
    await setup.mockMouse.drag(column,row,column+5,row);setup.mockInput.pressKey('c',{ctrl:true});await setup.flush();
    expect(copied.at(-1)).toContain('Ответ');expect(copied.at(-1)).not.toContain('черновик');expect(exits).toBe(0);
    setup.renderer.clearSelection();view.editor.clearSelection();
    setup.mockInput.pressKey('y',{ctrl:true});await setup.flush();expect(copied.at(-1)).toBe('Ответ для выделения');
    view.accept({type:'idle'});view.editor.setText('новый запрос');view.submit();setup.mockInput.pressKey('c',{ctrl:true});
    expect(sent.at(-1)).toEqual({type:'cancel'});
    view.accept({type:'idle'});view.accept({type:'input_request',id:'password',prompt:'Ключ',secret:true});
    const count=copied.length;await setup.mockInput.typeText('секрет');setup.mockInput.pressKey('y',{ctrl:true});setup.mockInput.pressKey('c',{ctrl:true,shift:true});
    await setup.flush();expect(copied.length).toBe(count);
  }finally{await view.dispose();await setup.dispose();}
});


test('меню провайдеров открывает мастер добавления и редактирования',async()=>{
  const setup=await createTestRenderer({width:115,height:40});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'ready'});
    view.accept({type:'table',title:'/provider',selection:{kind:'provider',provider:'local',session_id:'s',choices:[
      {provider:'local',label:'local',enabled:true},
      {label:'Добавить провайдера',command:'/provider add',group:'Управление'},
      {label:'Изменить local',command:'/provider edit local',group:'Управление'},
    ]}});
    await setup.mockInput.typeText('Добавить');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'submit',text:'/provider add'});
    view.accept({type:'idle'});
    view.accept({type:'table',title:'/provider',selection:{kind:'provider',provider:'local',choices:[
      {label:'Изменить local',command:'/provider edit local',group:'Управление'},
    ]}});
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'submit',text:'/provider edit local'});
  }finally{view.dispose();await setup.dispose();}
});


test('429 показывает ожидание повтора и позволяет Esc без выхода из сессии',async()=>{
  const setup=await createTestRenderer({width:115,height:40});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'ready'});view.editor.setText('Задача');view.submit();
    view.accept({type:'warning',message:'Повтор через 30 с',retry:{attempt:1,max_retries:5,delay:30,status:429}});
    await setup.flush();expect(setup.captureCharFrame()).toContain('Ожидание повтора');expect(setup.captureCharFrame()).toContain('HTTP 429');
    view.editor.setText('Черновик');setup.mockInput.pressEscape();await Bun.sleep(50);
    expect(sent.at(-1)).toEqual({type:'cancel'});expect(view.editor.plainText).toBe('Черновик');
    view.accept({type:'done',reason:'cancelled'});view.accept({type:'idle'});
    view.submit();view.accept({type:'context',estimated_tokens:100,max_input_tokens:32000});
    await setup.flush();expect(setup.captureCharFrame()).not.toContain('Ожидание повтора · HTTP');
  }finally{view.dispose();await setup.dispose();}
});

test('режим доступа виден в заголовке, меню переключается стрелками и сохраняет черновик',async()=>{
  const setup=await createTestRenderer({width:130,height:38});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'access',workspace:'/tmp',model:'alpha',agent_mode:'build'},provider:'local',permissions_mode:'allow_all'});
    view.accept({type:'ready'});view.editor.setText('Мой черновик');
    view.accept({type:'table',title:'Доступ',columns:[],rows:[],selection:{kind:'permissions',session_id:'access',current:'allow_all',choices:[
      {key:'allow_all',label:'Полный доступ',command:'/permissions allow_all'},
      {key:'read_only',label:'Только чтение',command:'/permissions read_only'},
    ]}});
    await setup.flush();expect(setup.captureCharFrame()).toContain('Полный доступ');
    setup.mockInput.pressArrow('down');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'submit',text:'/permissions read_only'});
    view.accept({type:'snapshot',session:{id:'access',workspace:'/tmp',model:'alpha',agent_mode:'build'},provider:'local',permissions_mode:'read_only'});
    view.accept({type:'idle'});await setup.flush();
    expect(setup.captureCharFrame()).toContain('Только чтение');expect(view.editor.plainText).toBe('Мой черновик');
  }finally{view.dispose();await setup.dispose();}
});
