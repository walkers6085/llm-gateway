import {test,expect} from 'bun:test';
import {createTestRenderer} from '@opentui/core/testing';
import {AgentView,type Command} from './view.ts';

test('настоящие кадры: поток, Markdown, таблица JSON, исходник и узкий терминал',async()=>{
  const setup=await createTestRenderer({width:110,height:36});
  const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'snapshot',version:'1.0.0',session:{id:'test',model:'alpha',workspace:'/tmp'},provider:'local'});
    view.accept({type:'ready'});
    view.accept({type:'text',text:'**Важно:** сохраните результат.\n\n```json\n[{"Модель":"альфа","Балл":42}]\n```'});
    view.accept({type:'done',reason:'stop'});
    await setup.flush();
    let frame=setup.captureCharFrame();
    expect(frame.split('\n')[0]!.trimEnd()).toEndWith('v1.0.0');
    expect(frame).toContain('альфа');expect(frame).toContain('42');expect(frame).toContain('ВАЖНО');
    setup.mockInput.pressKey('r',{ctrl:true});await setup.flush();
    expect(setup.captureCharFrame()).toContain('```json');
    setup.mockInput.pressKey('r',{ctrl:true});
    setup.resize(65,30);await setup.flush();
    frame=setup.captureCharFrame();expect(frame.split('\n')[0]!.trimEnd()).toEndWith('v1.0.0');expect(frame).toContain('сохраните результат');expect(frame).toContain('альфа');
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
    expect(sent.at(-1)).toEqual({type:'submit',text:'/provider add'});
    view.accept({type:'idle'});
    view.editor.setText('');await setup.mockInput.typeText('/con');setup.mockInput.pressTab();
    expect(view.editor.plainText).toBe('/connect ');expect(sent).toHaveLength(3);
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

test('все команды открываются по Enter независимо от обязательных аргументов; /providers и /permissions доступны без модели',async()=>{
  const setup=await createTestRenderer({width:110,height:34,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  const commands=[
    {kind:'command',command:'/providers',description:'Провайдеры',arguments:'optional'},
    {kind:'command',command:'/permissions',description:'Разрешения',arguments:'optional'},
    {kind:'command',command:'/export',description:'Экспорт',arguments:'required'},
  ];
  try{
    view.accept({type:'snapshot',session:{id:'offline',model:'',workspace:'/tmp'},provider:'gateway',catalog:commands});
    view.accept({type:'ready'});
    for(const item of commands){
      view.editor.setText('Черновик');setup.mockInput.pressKey('p',{ctrl:true});
      await setup.mockInput.typeText(item.command.slice(1));setup.mockInput.pressEnter();
      expect(sent.at(-1)).toEqual({type:'submit',text:item.command});
      view.accept({type:'table',title:'Действия',columns:[],rows:[],selection:{kind:'command',title:'Действия',session_id:'offline',choices:[
        {key:'action',target:'action',label:'Выбрать действие'},
      ]}});view.accept({type:'idle'});await setup.flush();
      expect(setup.renderer.root.findDescendantById('command-menu')?.visible).toBe(true);
      setup.mockInput.pressEscape();await Bun.sleep(30);
      expect(view.editor.plainText).toBe('Черновик');
    }
    expect(sent).toHaveLength(3);
  }finally{view.dispose();await setup.dispose();}
});

test('общее меню действий: заголовок, недоступные пункты, выбор и пустые окна остаются интерактивными',async()=>{
  const setup=await createTestRenderer({width:110,height:34,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    readyWithCatalog(view);view.editor.setText('Черновик');
    view.accept({type:'table',title:'Экспорт сессии',columns:[],rows:[],selection:{kind:'command',title:'Экспорт сессии',session_id:'offline',choices:[
      {key:'unavailable',target:'unavailable',label:'Недоступно',disabled:true},
      {key:'export',target:'export',label:'Сохранить историю в JSON'},
    ]}});await setup.flush();
    expect(setup.captureCharFrame()).toContain('Экспорт сессии');
    setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'select',kind:'command',target:'export',session_id:'offline'});
    view.accept({type:'idle'});
    view.accept({type:'table',title:'MCP',columns:[],rows:[],selection:{kind:'mcp',session_id:'offline',choices:[]}});
    await setup.flush();expect(setup.renderer.root.findDescendantById('command-menu')?.visible).toBe(true);
    expect(setup.captureCharFrame()).toContain('MCP-серверы не настроены');
    setup.mockInput.pressEnter();expect(sent).toHaveLength(1);
    setup.mockInput.pressEscape();await Bun.sleep(30);expect(view.editor.plainText).toBe('Черновик');
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
    expect(setup.captureCharFrame()).toContain('Сессии · история работы');expect(setup.captureCharFrame()).toContain('Доработка CLI');
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
    ]}});view.accept({type:'idle'});await setup.flush();expect(setup.captureCharFrame()).toContain('Сессии · история работы');
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

test('режим доступа виден в нижней панели, меню переключается стрелками и сохраняет черновик',async()=>{
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

test('управление провайдером: недоступные действия пропускаются, авторизация выбирает сохранённый способ',async()=>{
  const setup=await createTestRenderer({width:115,height:42});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'providers',workspace:'/tmp'},provider:'local'});view.accept({type:'ready'});
    view.editor.setText('Черновик');
    view.accept({type:'table',title:'local',columns:[],rows:[],selection:{kind:'provider-actions',session_id:'providers',choices:[
      {key:'remove',label:'Удалить',disabled:true,command:'/provider remove local',group:'local'},
      {key:'edit',label:'Изменить URL и авторизацию',command:'/provider edit local',group:'local'},
    ]}});
    await setup.flush();setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'submit',text:'/provider edit local'});
    view.accept({type:'input_request',id:'auth-env',prompt:'Способ подключения (Enter — env)',default_value:'env',choices:[
      {label:'Bearer',value:'bearer'},{label:'Ключ из окружения',value:'env'},
    ]});
    await setup.flush();setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'answer',id:'auth-env',value:'env'});
    view.accept({type:'idle'});expect(view.editor.plainText).toBe('Черновик');
  }finally{view.dispose();await setup.dispose();}
});

test('↑ возвращает историю, ручной ввод отменяет запоздалый ответ',async()=>{
  const setup=await createTestRenderer({width:100,height:30});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'s',workspace:'/tmp'}});view.accept({type:'ready'});
    setup.mockInput.pressArrow('up');await setup.flush();
    expect(sent.at(-1)?.type).toBe('input_history');
    const request=sent.at(-1)!;
    view.accept({...request,type:'input_history',message_id:'m',text:'Полный\nтекст'});
    expect(view.editor.plainText).toBe('Полный\nтекст');
    setup.mockInput.pressArrow('down');await setup.flush();
    view.accept({...sent.at(-1),type:'input_history',boundary:'draft'});expect(view.editor.plainText).toBe('');
    setup.mockInput.pressArrow('up');await setup.flush();const stale=sent.at(-1)!;
    view.editor.setText('Новый текст');view.accept({...stale,type:'input_history',text:'Старый текст'});
    expect(view.editor.plainText).toBe('Новый текст');
  }finally{await view.dispose();setup.renderer.destroy();}
});

test('клик по своему сообщению: задержка, двойной клик, выделение и занятость',async()=>{
  const setup=await createTestRenderer({width:100,height:30});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  const pause=()=>new Promise(resolve=>setTimeout(resolve,300));
  try{
    view.accept({type:'snapshot',session:{id:'s',workspace:'/tmp'},history:[{role:'user',content:'Текст пользователя',message_id:'m'}]});view.accept({type:'ready'});
    await setup.flush();
    const lines=setup.captureCharFrame().split('\n');const row=lines.findIndex(line=>line.includes('Текст пользователя'));
    expect(row).toBeGreaterThan(0);const column=lines[row].indexOf('Текст');
    await setup.mockMouse.click(column+1,row);await pause();
    expect(sent.at(-1)).toEqual({type:'edit_message',session_id:'s',message_id:'m'});
    view.accept({type:'idle'});sent.length=0;
    await setup.mockMouse.click(column+1,row);await setup.mockMouse.click(column+1,row);await pause();expect(sent).toHaveLength(0);
    setup.renderer.clearSelection();
    await setup.mockMouse.drag(column,row,column+5,row);await pause();expect(sent).toHaveLength(0);
    setup.renderer.clearSelection();
    view.editor.setText('Следующий запрос');view.submit();sent.length=0;
    await setup.mockMouse.click(column+1,row);await pause();expect(sent).toHaveLength(0);
  }finally{await view.dispose();setup.renderer.destroy();}
});

test('сессии: активная группа, понятное название, метрики и даты',async()=>{
  const setup=await createTestRenderer({width:120,height:35});const view=new AgentView(setup.renderer,()=>{},()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'active',workspace:'/tmp'}});view.accept({type:'ready'});
    view.accept({type:'table',selection:{kind:'session',current:'active',session_id:'active',choices:[
      {id:'active',title:'Исправить подключение GLM',model:'glm-4.7',workspace:'/tmp',provider:'zai',updated:Date.now()/1000,messages:5,tokens:12000,tokens_known:true,current:true,enabled:true},
      {id:'old',title:'Проверить статистику',model:'alpha',workspace:'/tmp',provider:'local',updated:Date.now()/1000,messages:2,enabled:true},
    ]}});view.accept({type:'idle'});await setup.flush();
    const frame=setup.captureCharFrame();expect(frame).toContain('Активная сессия');expect(frame).toContain('Сегодня');
    expect(frame).toContain('Исправить подключение GLM');expect(frame).toContain('5 сообщ.');expect(frame).toContain('12 000 ток.');
  }finally{await view.dispose();setup.renderer.destroy();}
});

test('после Plan: уточнение сохраняет черновик, переход меняет только режим',async()=>{
  const setup=await createTestRenderer({width:110,height:34});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  const find=(id:string):any=>view.root.findDescendantById(id);
  try{
    view.accept({type:'snapshot',session:{id:'s',workspace:'/tmp',agent_mode:'plan'}});view.accept({type:'ready'});
    expect(find('plan-actions').visible).toBe(false);
    view.accept({type:'user',text:'Составь план'});view.accept({type:'assistant',content:'План готов'});
    view.accept({type:'done',reason:'stop',agent_mode:'plan'});view.accept({type:'idle'});
    view.editor.setText('Нужно сохранить совместимость');await setup.flush();
    expect(setup.captureCharFrame()).toContain('Перейти в Build');expect(setup.captureCharFrame()).toContain('Уточнить план');
    const clarify=find('plan-clarify');await setup.mockMouse.click(clarify.x+1,clarify.y);await setup.flush();
    expect(view.editor.plainText).toBe('Нужно сохранить совместимость');expect(view.editor.focused).toBe(true);expect(sent).toHaveLength(0);
    const build=find('plan-build');await setup.mockMouse.click(build.x+1,build.y);await setup.flush();
    expect(sent).toEqual([{type:'set_agent',mode:'build'}]);expect(view.editor.plainText).toBe('Нужно сохранить совместимость');
    view.accept({type:'agent_mode',mode:'build'});await setup.flush();expect(find('plan-actions').visible).toBe(false);
    view.accept({type:'agent_mode',mode:'plan'});view.accept({type:'done',reason:'error'});view.accept({type:'idle'});
    expect(find('plan-actions').visible).toBe(false);
  }finally{await view.dispose();setup.renderer.destroy();}
});

test('переход Plan скрыт в вопросе и при смене сессии',async()=>{
  const setup=await createTestRenderer({width:110,height:34});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'s',workspace:'/tmp',agent_mode:'plan'}});view.accept({type:'ready'});
    view.accept({type:'done',reason:'stop',agent_mode:'plan'});view.accept({type:'idle'});
    expect(view.root.findDescendantById('plan-actions')?.visible).toBe(true);
    view.accept({type:'input_request',id:'q',question:true,prompt:'Какой вариант?',choices:[{label:'Первый',value:'one'}]});
    expect(view.root.findDescendantById('plan-actions')?.visible).toBe(false);
    view.accept({type:'idle'});view.accept({type:'snapshot',session:{id:'other',workspace:'/tmp',agent_mode:'plan'}});
    expect(view.root.findDescendantById('plan-actions')?.visible).toBe(false);
  }finally{await view.dispose();setup.renderer.destroy();}
});

test('повтор после обрыва убирает только текущий ответ и reasoning',async()=>{
  const setup=await createTestRenderer({width:110,height:34});const view=new AgentView(setup.renderer,()=>{},()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'s',workspace:'/tmp'}});view.accept({type:'ready'});
    view.accept({type:'assistant',content:'Сохранённый ответ'});
    view.accept({type:'reasoning',text:'Оборванная мысль'});view.accept({type:'text',text:'Оборванный фрагмент'});await setup.flush();
    view.accept({type:'warning',message:'Повтор',retry:{attempt:1,delay:0,status:502,reset_response:true}});
    view.accept({type:'text',text:'Восстановленный ответ'});view.accept({type:'assistant',content:'Восстановленный ответ'});await setup.flush();
    const frame=setup.captureCharFrame();expect(frame).toContain('Сохранённый ответ');expect(frame).toContain('Восстановленный ответ');
    expect(frame).not.toContain('Оборванный фрагмент');expect(frame).not.toContain('Reasoning');
  }finally{await view.dispose();setup.renderer.destroy();}
});


test('название и версия сверху, состояние сессии под вводом; узкий экран и уведомления не скрывают контекст',async()=>{
  const setup=await createTestRenderer({width:130,height:32});
  const view=new AgentView(setup.renderer,()=>{},()=>{});
  try{
    view.accept({type:'snapshot',version:'1.0.0',provider:'glm',permissions_mode:'read_only',
      session:{id:'layout',title:'Доработка интерфейса',model:'glm-4.5-flash',workspace:'/home/deb/project',agent_mode:'build'},
      context:{estimated_tokens:25000,max_input_tokens:100000}});
    view.accept({type:'ready'});view.editor.setText('Сохранённый черновик');
    for(const width of [130,65,40]){
      setup.resize(width,32);await setup.flush();
      const lines=setup.captureCharFrame().split('\n');
      expect(lines[0]).toContain('Доработка');expect(lines[0]!.trimEnd()).toEndWith('v1.0.0');
      expect(lines[0]).not.toContain('BUILD');expect(lines[0]).not.toContain('glm');
      const modeRow=lines.findIndex(line=>line.includes('BUILD'));
      const directoryRow=lines.findIndex(line=>line.includes('Папка:'));
      expect(modeRow).toBeGreaterThan(view.editor.y+view.editor.height);
      expect(directoryRow).toBe(modeRow+1);expect(lines[modeRow]).toContain('glm');
      expect(lines[directoryRow]).toContain('project');
      if(width<100)expect(lines[directoryRow]).toContain('Контекст ~25%');
      expect(view.editor.plainText).toBe('Сохранённый черновик');
    }
    view.accept({type:'agent_mode',mode:'plan',next_mode:'build'});view.notificationStatus('Уведомления доступны');
    await setup.flush();let frame=setup.captureCharFrame();
    expect(frame).toContain('PLAN → BUILD');expect(frame).toContain('glm');expect(frame).toContain('Контекст ~25%');
    setup.resize(130,32);await setup.flush();frame=setup.captureCharFrame();
    expect(frame).toContain('Только чтение');expect(frame).toContain('/home/deb/project');expect(frame).toContain('Уведомления доступны');
  }finally{view.dispose();await setup.dispose();}
});

test('счётчики шагов и новые блоки обновляются во время работы, включая узкий экран',async()=>{
  const setup=await createTestRenderer({width:110,height:36});
  const view=new AgentView(setup.renderer,()=>{},()=>{});
  try{
    view.accept({type:'snapshot',provider:'glm',session:{id:'steps',workspace:'/tmp'}});view.accept({type:'ready'});
    view.editor.setText('Задача');view.submit();
    view.accept({type:'step',current:25,limit:25,total:25,extended:false});
    await setup.flush();expect(setup.captureCharFrame()).toContain('Шаг 25/25 · всего 25');
    view.accept({type:'step',current:1,limit:25,total:26,extended:true});
    for(const width of [110,65,40]){
      setup.resize(width,36);await setup.flush();expect(setup.captureCharFrame()).toContain('Шаг 1/25 · всего 26');
    }
    view.accept({type:'subagent_start',session_id:'child',role:'explorer',task:'Проверка'});
    view.accept({type:'subagent_event',session_id:'child',event:{type:'step',current:1,limit:8,total:9,extended:true}});
    setup.resize(110,36);await setup.flush();expect(setup.captureCharFrame()).toContain('Шаг 1/8 · всего 9');
    expect(setup.captureCharFrame()).toContain('Шаг 1/25 · всего 26');
    view.accept({type:'done',reason:'loop_detected'});view.accept({type:'idle'});
    view.editor.setText('Продолжить иначе');view.submit();await setup.flush();
    expect(setup.captureCharFrame()).not.toContain('Шаг 1/25 · всего 26');
  }finally{view.dispose();await setup.dispose();}
});
