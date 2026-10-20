/** Один локальный Python-процесс на сессию; закрытие терминала завершает его работу. */
import {createCliRenderer} from '@opentui/core';
import {terminalInput} from './terminal-input.ts';
import {Notifications,localPopup} from './notifications.ts';
import {AgentView} from './view.ts';

export async function main(args=process.argv.slice(2)):Promise<number>{
  const input=terminalInput(process.stdin);
  let renderer;
  try{renderer=await createCliRenderer({stdin:input.input,exitOnCtrlC:false,exitSignals:[],consoleMode:'disabled',openConsoleOnError:false,useMouse:true});}catch(error){input.dispose();throw error;}
  let child:ReturnType<typeof Bun.spawn>|undefined;
  let stopping=false;let requestedExit=false;
  let resolveExit!:()=>void;
  const exit=new Promise<void>(resolve=>{resolveExit=resolve;});
  const send=(value:Record<string,unknown>)=>{
    if(value.type==='submit' && ['/exit','/quit','/q'].includes(String(value.text).trim()))requestedExit=true;
    if(child && !stopping && typeof child.stdin==='object' && child.stdin!==null && 'write' in child.stdin)
      child.stdin.write(JSON.stringify(value)+'\n');
  };
  const notifications=new Notifications({title:text=>renderer.setTerminalTitle(text),
    popup:async(title,body,sound)=>{const status=await localPopup(title,body,sound,(body,title)=>renderer.triggerNotification(body,title));view.notificationStatus(status);return status;}});
  const focus=()=>notifications.setFocus(true),blur=()=>notifications.setFocus(false);
  renderer.on('focus',focus);renderer.on('blur',blur);
  const view=new AgentView(renderer,send,resolveExit);
  const signal=()=>resolveExit();
  process.on('SIGTERM',signal);process.on('SIGINT',signal);process.on('SIGHUP',signal);
  let code=0;
  try{
    child=Bun.spawn([process.env.GWAGENT_PYTHON||'python3','-m','agent.tui_backend',...args],{stdin:'pipe',stdout:'pipe',stderr:'pipe'});
    const worker=child;
    // stderr не выводится: ошибки интерфейса идут структурированными и очищенными событиями.
    const drain=(async()=>{if(worker.stderr && typeof worker.stderr!=='number')for await(const _ of worker.stderr){ /* drain */ }})();
    const pump=(async()=>{
      let buffer='';const decoder=new TextDecoder();
      if(!worker.stdout || typeof worker.stdout==='number')throw new Error('нет потока событий');
      for await(const chunk of worker.stdout){
        buffer+=decoder.decode(chunk,{stream:true});
        let index:number;
        while((index=buffer.indexOf('\n'))>=0){
          if(index>1048576)throw new Error('слишком большое событие');
          const line=buffer.slice(0,index);buffer=buffer.slice(index+1);
          if(!line.trim())continue;
          let event:unknown;
          try{event=JSON.parse(line);}catch{throw new Error('неверный JSON события');}
          if(!event || typeof event!=='object' || !('type' in event) || typeof event.type!=='string')throw new Error('неверное событие');
          notifications.accept(event as any);view.accept(event as any);
          view.notificationStatus(notifications.status);
        }
        if(buffer.length>1048576)throw new Error('слишком большое событие');
      }
      if(buffer.trim())throw new Error('оборванное событие');
    })().catch(()=>{code=2;view.accept({type:'fatal',message:'Поток событий OpenTUI повреждён; завершите интерфейс и повторите запрос.'});worker.kill();});
    void worker.exited.then(async status=>{await pump;await drain;code=code||status;if(!stopping){if(requestedExit)resolveExit();else view.backendClosed(code);}});
    await exit;
  }catch{
    code=2;
  }finally{
    if(child){
      try{send({type:'shutdown'});}catch{ /* процесс уже закрыт */ }
      stopping=true;
      const wait=async(ms:number)=>Promise.race([child!.exited.then(()=>true),Bun.sleep(ms).then(()=>false)]);
      if(!await wait(2000)){child.kill('SIGTERM');if(!await wait(1000))child.kill('SIGKILL');}
      await child.exited;
    }
    renderer.off('focus',focus);renderer.off('blur',blur);
    try{await view.dispose();}finally{try{renderer.destroy();await renderer.closed;}finally{input.dispose();}}
    process.off('SIGTERM',signal);process.off('SIGINT',signal);process.off('SIGHUP',signal);
  }
  return code;
}
if(import.meta.main)process.exitCode=await main();
