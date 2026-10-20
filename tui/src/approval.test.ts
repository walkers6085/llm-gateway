import {test,expect} from 'bun:test';
import {createTestRenderer} from '@opentui/core/testing';
import {BoxRenderable,RGBA,TextRenderable,type Renderable} from '@opentui/core';
import {AgentView,type Command} from './view.ts';
import {permissionPresentation} from './approval.ts';
import {palette} from './theme.ts';

function textNodes(node:Renderable):TextRenderable[]{return [...(node instanceof TextRenderable?[node]:[]),...node.getChildren().flatMap(textNodes)];}
function permission(id:string,command='printf "%s\\n" "hello world"\npwd'){
  return {type:'permission_request',id,name:'shell',arguments:{command,timeout:30},reason:'требует подтверждения',rule:'shell('+command+')'};
}

test('понятное разрешение: точная команда, спокойные варианты, стрелки и правило сессии',async()=>{
  const setup=await createTestRenderer({width:110,height:40,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'snapshot',provider:'local',session:{id:'s',workspace:'/project',model:'test'}});view.accept({type:'ready'});
    view.editor.setText('сохранить черновик');view.accept(permission('first'));await setup.flush();
    let frame=setup.captureCharFrame();
    for(const text of ['Нужно ваше разрешение','Запуск команды','Команда','hello world','pwd','Рабочая папка: /project','Разрешить один раз','Разрешать в этой сессии','Не разрешать'])expect(frame).toContain(text);
    expect(frame).not.toContain('"command":');expect(sent).toHaveLength(0);
    const panel=view.root.findDescendantById('permission-panel') as BoxRenderable;
    expect(panel.borderColor.equals(RGBA.fromHex(palette.border))).toBe(true);
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'approve',id:'first',decision:'deny'});
    view.accept(permission('second'));setup.mockInput.pressArrow('down');setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'approve',id:'second',decision:'allow'});
    view.accept(permission('third'));setup.mockInput.pressArrow('right');setup.mockInput.pressArrow('right');await setup.flush();
    expect(setup.captureCharFrame()).toContain('Правило этой сессии: shell(');
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'approve',id:'third',decision:'always'});
    expect(view.editor.plainText).toBe('сохранить черновик');
    // Один и тот же запрос не подтверждается второй раз.
    const count=sent.length;setup.mockInput.pressEnter();expect(sent.length).toBe(count+1);
    expect(sent.at(-1)).toEqual({type:'submit',text:'сохранить черновик'});
  }finally{await view.dispose();await setup.dispose();}
});

test('мышь выбирает ответ, правая кнопка не разрешает; короткий экран и прокрутка команды',async()=>{
  const setup=await createTestRenderer({width:60,height:24,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'ready'});view.accept(permission('mouse'));await setup.flush();
    const label=textNodes(view.root).find(node=>node.chunks.some(chunk=>chunk.text.includes('Разрешить один раз')))!;
    expect(setup.captureCharFrame()).toContain('Разрешать в этой сессии');
    await setup.mockMouse.click(label.x+3,label.y,2);expect(sent).toHaveLength(0);
    await setup.mockMouse.click(label.x+3,label.y);expect(sent.at(-1)).toEqual({type:'approve',id:'mouse',decision:'allow'});expect(view.editor.focused).toBe(true);
    const command=Array.from({length:30},(_,i)=>`echo command-line-${i}`).join('\n');
    view.accept(permission('scroll',command));await setup.flush();expect(setup.captureCharFrame()).toContain('command-line-0');
    for(let i=0;i<10;i++)setup.mockInput.pressKey('\x1b[6~');await setup.flush();
    expect(setup.captureCharFrame()).toContain('command-line-29');
    expect(sent).toHaveLength(1);setup.mockInput.pressEscape();await Bun.sleep(50);
    expect(sent.at(-1)).toEqual({type:'approve',id:'scroll',decision:'deny'});
  }finally{await view.dispose();await setup.dispose();}
});

test('вид запроса объясняет изменения файла и задачу сабагента',()=>{
  const file=permissionPresentation({id:'file',name:'edit_file',arguments:{path:'main.py',old_string:'before',new_string:'after'}});
  expect(file.title).toBe('Изменение файла');expect(file.context).toBe('Файл: main.py');expect(file.content).toContain('Было:\nbefore\n\nСтанет:\nafter');
  expect(permissionPresentation({id:'child',name:'subagent',arguments:{task:'Проверить проект'}}).content).toBe('Проверить проект');
});

test('стрелки редактируют Unicode, несколько строк и черновик во время ответа; Enter отправляет уточнение',async()=>{
  const setup=await createTestRenderer({width:100,height:35,exitOnCtrlC:false});
  const sent:Command[]=[];const view=new AgentView(setup.renderer,c=>sent.push(c),()=>{});
  try{
    view.accept({type:'ready'});view.editor.setText('абв');view.editor.gotoBufferEnd();
    setup.mockInput.pressArrow('left');await setup.mockInput.typeText('Х');expect(view.editor.plainText).toBe('абХв');
    setup.mockInput.pressArrow('right');await setup.mockInput.typeText('Я');expect(view.editor.plainText).toBe('абХвЯ');
    view.editor.setText('первая\nвторая');view.editor.gotoBufferEnd();setup.mockInput.pressArrow('up');
    await setup.mockInput.typeText('!');expect(view.editor.plainText).toBe('первая!\nвторая');
    setup.mockInput.pressArrow('down');await setup.mockInput.typeText('?');expect(view.editor.plainText).toBe('первая!\nвторая?');
    view.editor.setText('запрос');view.submit();expect(sent).toHaveLength(1);
    await setup.mockInput.typeText('следующий');setup.mockInput.pressArrow('left');await setup.mockInput.typeText('!');
    const draft=view.editor.plainText;expect(draft).toBe('следующи!й');setup.mockInput.pressEnter();expect(sent).toHaveLength(2);expect(sent.at(-1)?.type).toBe('steer');expect(sent.at(-1)?.text).toBe(draft);expect(view.editor.plainText).toBe('');view.editor.setText(draft);
    view.accept(permission('draft'));await setup.mockInput.pasteBracketedText('не вставлять');expect(view.editor.plainText).toBe(draft);
    setup.mockInput.pressArrow('down');setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'approve',id:'draft',decision:'allow'});
    view.accept({type:'done',reason:'stop'});view.accept({type:'idle'});expect(view.editor.plainText).toBe(draft);
    setup.mockInput.pressEnter();expect(sent.at(-1)).toEqual({type:'submit',text:draft});
    view.accept(permission('expired'));view.accept({type:'tool_result',id:'expired',name:'shell',ok:false,content:'Время ожидания истекло',ms:1});
    expect(view.root.findDescendantById('permission-panel')!.visible).toBe(false);expect(view.editor.focused).toBe(true);
  }finally{await view.dispose();await setup.dispose();}
});
