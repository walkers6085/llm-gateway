import {test,expect} from 'bun:test';
import {createTestRenderer} from '@opentui/core/testing';
import {AgentView,type Command} from './view.ts';

test('Tab переключает build/plan, сохраняет черновик и ставит смену в очередь во время работы',async()=>{
  const setup=await createTestRenderer({width:110,height:38});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'m',workspace:'/tmp',agent_mode:'build'},provider:'local'});view.accept({type:'ready'});
    await setup.mockInput.typeText('Черновик');setup.mockInput.pressTab();expect(sent.at(-1)).toEqual({type:'set_agent',mode:'plan'});
    expect(view.editor.plainText).toBe('Черновик');view.accept({type:'agent_mode',mode:'plan',next_mode:null});
    await setup.flush();expect(setup.captureCharFrame()).toContain('PLAN');
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'submit',text:'Черновик'});
    setup.mockInput.pressTab();expect(sent.at(-1)).toEqual({type:'set_agent',mode:'build'});
    view.accept({type:'agent_mode',mode:'plan',next_mode:'build'});await setup.flush();expect(setup.captureCharFrame()).toContain('PLAN → BUILD');
    view.accept({type:'permission_request',id:'p',name:'web_fetch',arguments:{url:'https://example.test'},reason:'ask'});
    const count=sent.length;setup.mockInput.pressTab();expect(sent.length).toBe(count);
    setup.mockInput.pressEscape();await Bun.sleep(50);expect(sent.at(-1)?.type).toBe('approve');
    view.accept({type:'done',reason:'stop'});view.accept({type:'agent_mode',mode:'build',next_mode:null});view.accept({type:'idle'});
    await setup.flush();expect(setup.captureCharFrame()).not.toContain('PLAN → BUILD');
  }finally{view.dispose();await setup.dispose();}
});

test('вопрос: выбор, свой текст, пропуск и таймаут сохраняют следующий черновик',async()=>{
  const setup=await createTestRenderer({width:105,height:40});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'ready'});await setup.mockInput.typeText('Запрос');setup.mockInput.pressEnter();
    await setup.mockInput.typeText('Следующий черновик');
    view.accept({type:'input_request',id:'q',question:true,agent:'plan',prompt:'Какой формат?',choices:[{label:'JSON',value:'JSON'},{label:'CSV',value:'CSV'},{label:'Написать свой ответ',custom:true}]});
    await setup.flush();expect(setup.captureCharFrame()).toContain('Вопрос от plan');
    const count=sent.length;setup.mockInput.pressTab();expect(sent.length).toBe(count);setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'answer',id:'q',value:'CSV'});expect(view.editor.plainText).toBe('Следующий черновик');expect(view.editor.focused).toBe(true);
    view.accept({type:'input_request',id:'custom',question:true,prompt:'Свой вариант?',choices:[{label:'Написать свой ответ',custom:true}]});
    setup.mockInput.pressEnter();await setup.mockInput.typeText('Таблица Markdown');setup.mockInput.pressEnter();
    expect(sent.at(-1)).toEqual({type:'answer',id:'custom',value:'Таблица Markdown'});expect(view.editor.plainText).toBe('Следующий черновик');
    view.accept({type:'input_request',id:'skip',question:true,prompt:'Уточнить?'});setup.mockInput.pressEscape();await Bun.sleep(50);
    expect(sent.at(-1)).toEqual({type:'answer',id:'skip',value:null,cancelled:true});expect(view.editor.plainText).toBe('Следующий черновик');
    view.accept({type:'input_request',id:'timeout',question:true,prompt:'Долгий вопрос?'});view.accept({type:'input_closed',id:'timeout'});
    expect(view.editor.plainText).toBe('Следующий черновик');expect(view.editor.focused).toBe(true);
  }finally{view.dispose();await setup.dispose();}
});
