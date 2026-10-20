import {test,expect} from 'bun:test';
import {Notifications} from './notifications.ts';
test('только фон: неизвестный фокус, завершение, дедупликация и отмена',async()=>{
  const popups:string[]=[];const titles:string[]=[];
  const n=new Notifications({title:s=>titles.push(s),popup:async(_title,body)=>{popups.push(body);return 'передано';}});
  n.accept({type:'snapshot',session:{id:'s',title:'Сессия'}});
  n.accept({type:'user',message_id:'a'});n.accept({type:'done',reason:'stop'});
  expect(popups).toEqual([]);
  n.setFocus(false);n.accept({type:'user',message_id:'b'});
  n.accept({type:'error',message:'секрет'});n.accept({type:'done',reason:'error'});
  expect(popups).toEqual(['Запрос завершился с ошибкой']);
  expect(titles.at(-1)).toContain('●');n.setFocus(true);expect(titles.at(-1)).not.toContain('●');
  n.setFocus(false);n.accept({type:'user',message_id:'c'});n.accept({type:'done',reason:'cancelled'});
  expect(popups.length).toBe(1);
  n.accept({type:'input_request',question:true,id:'q'});n.accept({type:'input_request',question:true,id:'q'});
  expect(popups.length).toBe(2);
});
test('ошибка адаптера не прерывает интерфейс, активный терминал молчит',async()=>{
  const n=new Notifications({title:()=>{},popup:async()=>{throw Error('нет службы');}});
  n.setFocus(true);n.accept({type:'fatal'});expect(n.status).toContain('активен');
  n.setFocus(false);n.accept({type:'input_request',question:true,id:'q'});
  await new Promise(resolve=>setTimeout(resolve,0));expect(n.status).toContain('недоступна');
});

test('адаптеры: SSH, отсутствие утилиты, звук и таймаут без настоящих уведомлений',async()=>{
  const {localPopup}=await import('./notifications.ts');
  expect(await localPopup('t','b',true,()=>false,{SSH_TTY:'tty'})).toContain('не сообщил');
  expect(await localPopup('t','b',true,()=>true,{SSH_CONNECTION:'ssh'})).toContain('передано терминалу');
  expect(await localPopup('t','b',true,()=>false,{},()=>{throw Error('нет утилиты');})).toContain('установите');
  let args:string[]=[];
  await localPopup('t','b',false,()=>false,{TERMUX_VERSION:'1'},argv=>{args=argv;return {exited:Promise.resolve(0),kill:()=>{}};});
  expect(args[0]).toBe('termux-notification');expect(args).not.toContain('--sound');
  let killed=false;let finish!:(code:number)=>void;
  const result=await localPopup('t','b',true,()=>false,{},()=>({exited:new Promise(resolve=>{finish=resolve;}),kill:()=>{killed=true;finish(1);}}),1);
  expect(killed).toBe(true);expect(result).toContain('ожидания');
});
