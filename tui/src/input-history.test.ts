import {test,expect} from 'bun:test';
import {InputHistory} from './input-history.ts';
test('история: границы, полный текст, черновик и устаревшие ответы',()=>{
  const sent:any[]=[];let text='';
  const history=new InputHistory(m=>sent.push(m),value=>{text=value;history.reset();});
  expect(history.move('previous','s','черновик')).toBe(false);
  expect(history.move('previous','s','')).toBe(true);
  history.move('previous','s','');expect(sent.length).toBe(1);
  history.accept({...sent[0],message_id:'m',text:'Первая\nВторая'},'s');expect(text).toBe('Первая\nВторая');
  history.move('next','s',text);
  history.accept({...sent[1],boundary:'draft'},'s');expect(text).toBe('');
  history.move('previous','s','');history.reset();
  expect(history.accept({...sent[2],text:'устарело'},'s')).toBe(false);expect(text).toBe('');
  history.move('previous','s','');
  expect(history.accept({...sent[3],text:'другая сессия'},'other')).toBe(false);
});
