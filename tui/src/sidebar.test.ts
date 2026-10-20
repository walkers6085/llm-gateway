import {test,expect} from 'bun:test';
import {createTestRenderer} from '@opentui/core/testing';
import {AgentView,type Command} from './view.ts';

function find(node:any,id:string):any{if(node.id===id)return node;for(const child of node.getChildren()){const found=find(child,id);if(found)return found;}}

test('правая панель: контекст, последний вход, цель, статусы задач и MCP; заметки не стирают состояние',async()=>{
  const setup=await createTestRenderer({width:125,height:52});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,command=>sent.push(command),()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'first',model:'alpha',workspace:'/tmp'},provider:'test',
      context:{estimated_tokens:16000,max_input_tokens:32000,max_output_tokens:1024},
      planning:{goal:'Исправить ввод Unicode',todos:[{id:'a',text:'Найти причину',status:'completed'},{id:'b',text:'Проверить SSH',status:'in_progress'},{id:'c',text:'Обновить вики',status:'pending'}]},
      mcp:[{name:'local',ok:true,state:'connected',tools:['echo']},{name:'remote',ok:false,state:'error',error:'Соединение закрыто'},{name:'off',ok:false,state:'disabled',tools:[]}]});
    view.accept({type:'ready'});view.accept({type:'usage',prompt_tokens:1234,completion_tokens:10});
    view.accept({type:'text',text:'**Важно:** изменения сохранены'});view.accept({type:'done',reason:'stop'});
    await setup.flush();let frame=setup.captureCharFrame();
    for(const text of ['~50%','Последний вход:','Исправить ввод Unicode','To do · 1/3','✓ Найти причину','◉ Проверить SSH','○ Обновить вики','local · подключён','remote · ошибка','off · выключен','ВАЖНО'])expect(frame).toContain(text);
    view.accept({type:'context',estimated_tokens:36000,max_input_tokens:32000,max_output_tokens:1024});
    await setup.flush();expect(setup.captureCharFrame()).toContain('112,5%');
    view.accept({type:'idle'});await setup.mockInput.typeText('Сохранённый черновик');
    await setup.flush();const heading=find(view.root,'sidebar-mcp-heading');
    await setup.mockMouse.click(heading.x+1,heading.y);
    expect(sent.at(-1)).toEqual({type:'submit',text:'/mcp'});
    expect(view.editor.plainText).toBe('Сохранённый черновик');
    setup.resize(70,30);await setup.flush();expect(find(view.root,'insights').visible).toBe(false);
    expect(setup.captureCharFrame()).toContain('Контекст ~113%');
    setup.resize(125,52);
    view.accept({type:'snapshot',session:{id:'second',workspace:'/tmp'},provider:'test',context:{estimated_tokens:1000,max_input_tokens:10000},planning:{goal:'Новая цель',todos:[]},mcp:[]});
    await setup.flush();frame=setup.captureCharFrame();expect(frame).toContain('~10%');expect(frame).toContain('Новая цель');
    expect(frame).not.toContain('Последний вход:');expect(frame).not.toContain('Проверить SSH');expect(frame).not.toContain('remote · ошибка');
  }finally{view.dispose();await setup.dispose();}
});

test('длинные цель и задачи раскрываются заголовками; активный пункт виден в свёрнутом списке',async()=>{
  const setup=await createTestRenderer({width:125,height:52});const view=new AgentView(setup.renderer,()=>{},()=>{});
  try{
    const goal='Длинная цель '.repeat(40)+'конец цели';
    view.accept({type:'snapshot',session:{id:'long',workspace:'/tmp'},provider:'test',
      context:{estimated_tokens:1000,max_input_tokens:10000},planning:{goal,todos:Array.from({length:5},(_,i)=>({id:String(i),text:'Задача '+i,status:i===4?'in_progress':'completed'}))},
      mcp:[{name:'local',ok:true,state:'connected',tools:[]}]});view.accept({type:'ready'});
    await setup.flush();let frame=setup.captureCharFrame();
    expect(frame).toContain('Цель · раскрыть');expect(frame).not.toContain('конец цели');
    expect(frame).toContain('◉ Задача 4');expect(frame).not.toContain('✓ Задача 3');expect(frame).toContain('local · подключён');
    let heading=find(view.root,'sidebar-todo-heading');await setup.mockMouse.click(heading.x+1,heading.y);
    await setup.flush();expect(setup.captureCharFrame()).toContain('✓ Задача 3');
    heading=find(view.root,'sidebar-goal-heading');await setup.mockMouse.click(heading.x+1,heading.y);
    await setup.flush();expect(setup.captureCharFrame()).toContain('конец цели');expect(view.editor.focused).toBe(true);
    await setup.mockMouse.click(heading.x+1,heading.y);await setup.flush();expect(setup.captureCharFrame()).not.toContain('конец цели');
  }finally{view.dispose();await setup.dispose();}
});

test('окно API отделено от лимита входа, а неизвестный размер явно помечен',async()=>{
  const setup=await createTestRenderer({width:125,height:52});const view=new AgentView(setup.renderer,()=>{},()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'limits',model:'m',workspace:'/tmp'},provider:'test',
      context:{estimated_tokens:25000,max_input_tokens:50000,max_output_tokens:1024,context_window:262144,context_source:'api',input_limit_source:'config'}});
    await setup.flush();let frame=setup.captureCharFrame();
    expect(frame).toContain('~50%');expect(frame).toContain('262');expect(frame).toContain('144');expect(frame).toContain('API провайдера');expect(frame).toContain('Вход ограничен настройками');
    view.accept({type:'context',estimated_tokens:1000,max_input_tokens:130048,max_output_tokens:1024,context_window:131072,context_source:'fallback'});
    await setup.flush();frame=setup.captureCharFrame();expect(frame).toContain('неизвестно');expect(frame).toContain('резервный лимит');
  }finally{view.dispose();await setup.dispose();}
});
