/** Уведомления без содержимого запросов; неизвестный фокус считается небезопасным. */
type Event={type:string;[key:string]:any};
type Settings={enabled:boolean;sound:boolean;background_only:boolean};
export type NotificationHost={
  title:(text:string)=>void;
  popup:(title:string,body:string,sound:boolean)=>Promise<string>;
};
export class Notifications {
  private focus?:boolean;
  private settings:Settings={enabled:true,sound:true,background_only:true};
  private session='';private label='';private state='Готово';private unread=false;
  private request='';private running=false;private seen=new Set<string>();
  status='Уведомления: фокус неизвестен';
  constructor(private host:NotificationHost){}
  setFocus(focus:boolean){this.focus=focus;if(focus)this.unread=false;this.update();}
  private update(){this.host.title(`${this.unread?'● ':''}gwagent · ${this.state}${this.label?' · '+this.label:''}`);}
  accept(event:Event){
    if(event.type==='snapshot'){
      if(this.session!==event.session.id){this.seen.clear();this.running=false;this.unread=false;this.state='Готово';}
      this.session=event.session.id;
      this.label=String(event.session.title||event.session.id).replace(/[\x00-\x1f\x7f-\x9f]/g,'').slice(0,80);
      Object.assign(this.settings,event.notifications||{});this.update();return;
    }
    if(event.type==='user'){this.running=true;this.request=event.message_id||event.request_id||String(Date.now());this.state='Работает';this.update();return;}
    let key='',body='';
    if(event.type==='permission_request'||event.type==='input_request'&&event.question){
      key=event.type+':'+event.id;body='Требуется ваш ответ';this.state='Ждёт ответа';
    }else if(event.type==='error'&&this.running||event.type==='fatal'){
      key='end:'+this.request;body='Запрос завершился с ошибкой';this.state='Ошибка';
    }else if(event.type==='done'&&this.running){
      this.running=false;
      if(event.reason==='cancelled'){this.state='Готово';this.update();return;}
      key='end:'+this.request;body=event.reason==='error'?'Запрос завершился с ошибкой':'Ответ готов';
      this.state=event.reason==='error'?'Ошибка':'Готово';
    }else if(event.type==='text'||event.type==='tool_call'||event.type==='context'){
      this.state='Работает';this.update();return;
    }
    if(!key)return;
    if(this.seen.has(key)){this.update();return;}
    this.seen.add(key);if(this.seen.size>256)this.seen.delete(this.seen.values().next().value!);
    if(this.focus===false)this.unread=true;
    this.update();
    if(!this.settings.enabled)return;
    if(this.settings.background_only&&this.focus!==false){this.status=this.focus===true?'Уведомления: терминал активен':'Уведомления: фокус неизвестен, только заголовок';return;}
    void this.host.popup('gwagent',body,this.settings.sound).then(status=>{this.status=status;}).catch(()=>{this.status='Уведомления: доставка недоступна';});
  }
}
export async function localPopup(title:string,body:string,sound:boolean,native:(body:string,title:string)=>boolean,env=process.env,spawn:(argv:string[])=>{exited:Promise<number>;kill:()=>void}=argv=>{const child=Bun.spawn(argv,{stdin:'ignore',stdout:'ignore',stderr:'ignore'});return {exited:child.exited,kill:()=>{child.kill('SIGKILL');}};},timeoutMs=1500):Promise<string>{
  if(env.SSH_CONNECTION||env.SSH_TTY){
    return native(body,title)?'Уведомление передано терминалу; доставка и звук зависят от клиента':'Терминал не сообщил поддержку уведомлений';
  }
  const termux=Boolean(env.TERMUX_VERSION||env.PREFIX?.includes('com.termux'));
  const command=termux?['termux-notification','--title',title,'--content',body,...(sound?['--sound']:[])]:['notify-send','--app-name=gwagent',...(sound?['--hint=string:sound-name:message-new-instant']:['--hint=boolean:suppress-sound:true']),title,body];
  let child:{exited:Promise<number>;kill:()=>void}|undefined;
  let timer:ReturnType<typeof setTimeout>|undefined;
  try{
    child=spawn(command);
    const timedOut=Symbol();
    const result=await Promise.race([child.exited,new Promise<symbol>(resolve=>{timer=setTimeout(()=>resolve(timedOut),timeoutMs);})]);
    if(result===timedOut){child.kill();await child.exited;return 'Уведомления: превышено время ожидания';}
    return result===0?'Уведомление передано системе; показ и звук зависят от настроек ОС':'Уведомления: системная служба недоступна';
  }catch{return 'Уведомления: установите notify-send или Termux:API';}
  finally{if(timer)clearTimeout(timer);}
}
