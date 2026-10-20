import {test,expect} from 'bun:test';
import {TerminalInput} from './terminal-input.ts';
import {createTestRenderer} from '@opentui/core/testing';
import {AgentView} from './view.ts';

function collect(){const input=new TerminalInput();const chunks:Buffer[]=[];input.on('data',chunk=>chunks.push(Buffer.from(chunk)));return {input,chunks,text:()=>Buffer.concat(chunks).toString()};}

test('OSC RGB собирается после задержек SSH и не попадает в восстановленный редактор',async()=>{
  const input=new TerminalInput();
  const setup=await createTestRenderer({stdin:input as unknown as NodeJS.ReadStream,width:100,height:32,exitOnCtrlC:false});
  const view=new AgentView(setup.renderer,()=>{},()=>{});
  try{
    view.accept({type:'snapshot',provider:'glm',session:{id:'old',title:'Прошлая сессия',workspace:'/tmp'},history:[{role:'assistant',content:'Прошлый ответ'}]});
    view.accept({type:'ready'});view.editor.setText('Мой черновик');
    for(const part of ['\x1b]12;rgb',':7979/b8b8/ffff','\x1b','\\']){input.write(Buffer.from(part));await Bun.sleep(100);}
    await setup.flush();expect(view.editor.plainText).toBe('Мой черновик');
    input.write(Buffer.from(':7979/b8b8/ffff'));await setup.flush();expect(view.editor.plainText).toContain(':7979/b8b8/ffff');
  }finally{await view.dispose();await setup.dispose();input.destroy();}
});

test('любая граница разбиения OSC, BEL/ST и DCS сохраняет целый служебный пакет',()=>{
  for(const sequence of ['\x1b]10;rgb:7979/b8b8/ffff\x07','\x1b]12;rgb:7979/b8b8/ffff\x1b\\','\x1bP>|terminal\x1b\\']){
    for(let split=1;split<sequence.length;split++){
      const {input,chunks,text}=collect();input.write(Buffer.from(sequence.slice(0,split)));
      expect(chunks.length).toBe(0);input.write(Buffer.from(sequence.slice(split)));expect(text()).toBe(sequence);expect(chunks.length).toBe(1);input.destroy();
    }
  }
});

test('печатный Unicode и bracketed paste не фильтруются, Escape и стрелки работают',async()=>{
  const {input,text}=collect();
  const content='текст 🐈 :7979/b8b8/ffff';for(const byte of Buffer.from(content))input.write(Buffer.from([byte]));
  const paste='\x1b[200~пример \x1b]12;rgb:7979/b8b8/ffff\x07\x1b[201~';
  for(const byte of Buffer.from(paste))input.write(Buffer.from([byte]));
  input.write(Buffer.from('\x1b[A'));input.write(Buffer.from('\x1b'));await Bun.sleep(80);
  expect(text()).toBe(content+paste+'\x1b[A\x1b');input.destroy();
});

test('переполненный служебный ответ отбрасывается, следующий текст сохраняется',()=>{
  const {input,text}=collect();input.write(Buffer.from('\x1b]10;'+ 'a'.repeat(70000)+'\x07'));
  expect(text()).toBe('');input.write(Buffer.from('Новый ввод'));expect(text()).toBe('Новый ввод');input.destroy();
});


test('поздний хвост ответа после таймаута не становится сообщением, Ctrl+C освобождает ввод',async()=>{
  const input=new TerminalInput(undefined,30);const chunks:Buffer[]=[];input.on('data',chunk=>chunks.push(Buffer.from(chunk)));
  input.write(Buffer.from('\x1b]12;rgb'));await Bun.sleep(60);input.write(Buffer.from(':7979/b8b8/ffff\x07Новый ввод'));
  expect(Buffer.concat(chunks).toString()).toBe('Новый ввод');
  input.write(Buffer.from('\x1b]12;rgb'));await Bun.sleep(60);input.write(Buffer.from('\x03После отмены'));
  expect(Buffer.concat(chunks).toString()).toBe('Новый ввод\x03После отмены');input.destroy();
});

test('CSI и SS3 стрелки после задержки SSH редактируют строку и не вставляют буквенные хвосты',async()=>{
  const input=new TerminalInput();const setup=await createTestRenderer({stdin:input as unknown as NodeJS.ReadStream,width:80,height:28,exitOnCtrlC:false});
  const view=new AgentView(setup.renderer,()=>{},()=>{});
  try{
    view.accept({type:'ready'});view.editor.setText('abc');view.editor.gotoBufferEnd();
    input.write(Buffer.from('\x1b['));await Bun.sleep(80);input.write(Buffer.from('D'));input.write(Buffer.from('X'));
    await setup.flush();expect(view.editor.plainText).toBe('abXc');
    input.write(Buffer.from('\x1bO'));await Bun.sleep(80);input.write(Buffer.from('D'));input.write(Buffer.from('Y'));
    await setup.flush();expect(view.editor.plainText).toBe('abYXc');
    input.write(Buffer.from('\x1bOC'));input.write(Buffer.from('Z'));await setup.flush();expect(view.editor.plainText).toBe('abYXZc');
  }finally{await view.dispose();await setup.dispose();input.destroy();}
});
