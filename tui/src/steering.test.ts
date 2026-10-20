import {test,expect} from 'bun:test';
import {createTestRenderer} from '@opentui/core/testing';
import {AgentView,type Command} from './view.ts';

test('Enter отправляет уточнение в текущий цикл, подтверждение не дублируется, поздний отказ возвращает текст',async()=>{
  const setup=await createTestRenderer({width:120,height:45});const sent:Command[]=[];
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'ready'});view.editor.setText('Исходная задача');view.submit();
    view.accept({type:'text',text:'Работаю'});view.editor.setText('Учти Unicode');view.submit();
    const command=sent.at(-1)!;expect(command.type).toBe('steer');expect(command.text).toBe('Учти Unicode');expect(view.editor.plainText).toBe('');
    view.accept({type:'input_queued',id:command.id,text:command.text,status:'pending'});
    await setup.flush();let frame=setup.captureCharFrame();expect(frame).toContain('Уточнений: 1');expect(frame).toContain('ожидает безопасного шага');
    view.accept({type:'assistant',content:'Работаю'});view.accept({type:'input_applied',id:command.id,text:command.text});
    await setup.flush();frame=setup.captureCharFrame();expect(frame).toContain('уточнение учтено');expect(frame).not.toContain('Уточнений: 1');
    expect(frame.split('Учти Unicode').length-1).toBe(1);
    view.editor.setText('Позднее уточнение');view.submit();const late=sent.at(-1)!;
    view.editor.setText('Уже печатаю дальше');view.accept({type:'steering_rejected',id:late.id,text:late.text,message:'Цикл завершён'});
    expect(view.editor.plainText).toBe('Позднее уточнение\n\nУже печатаю дальше');
    view.editor.setText('/compact');const count=sent.length;view.submit();expect(sent.length).toBe(count);expect(view.editor.plainText).toBe('/compact');
    view.accept({type:'done',reason:'stop'});view.accept({type:'idle'});view.editor.setText('Следующий запрос');view.submit();expect(sent.at(-1)).toEqual({type:'submit',text:'Следующий запрос'});
  }finally{view.dispose();await setup.dispose();}
});

test('сохранённые уточнения восстанавливаются из snapshot и исчезают из счётчика после блокировки',async()=>{
  const setup=await createTestRenderer({width:120,height:35});const view=new AgentView(setup.renderer,()=>{},()=>{});
  try{
    view.accept({type:'snapshot',session:{id:'s',workspace:'/tmp'},provider:'test',history:[],pending_inputs:[{id:'old',text:'Сохранённое уточнение',status:'pending'}]});view.accept({type:'ready'});
    await setup.flush();expect(setup.captureCharFrame()).toContain('Сохранённое уточнение');expect(setup.captureCharFrame()).toContain('Уточнений: 1');
    view.accept({type:'input_rejected',id:'old',message:'Запрещено хуком'});await setup.flush();expect(setup.captureCharFrame()).not.toContain('Уточнений: 1');
  }finally{view.dispose();await setup.dispose();}
});


test('Esc отменяет работу, сохраняет черновик и сессию, в покое не выходит',async()=>{
  const setup=await createTestRenderer({width:120,height:40});const sent:Command[]=[];let exits=0;
  const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{exits++;});
  try{
    view.accept({type:'ready'});view.editor.setText('Задача');view.submit();
    view.accept({type:'text',text:'Работаю'});view.editor.setText('Следующий черновик');
    setup.mockInput.pressEscape();await Bun.sleep(50);
    expect(sent.at(-1)).toEqual({type:'cancel'});expect(view.editor.plainText).toBe('Следующий черновик');expect(exits).toBe(0);
    view.accept({type:'done',reason:'cancelled'});view.accept({type:'idle'});
    expect(view.editor.focused).toBe(true);const count=sent.length;
    setup.mockInput.pressEscape();await Bun.sleep(50);expect(sent.length).toBe(count);expect(exits).toBe(0);
    view.submit();expect(sent.at(-1)).toEqual({type:'submit',text:'Следующий черновик'});
  }finally{view.dispose();await setup.dispose();}
});
