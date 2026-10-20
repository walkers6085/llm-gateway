/** Состояние сессии: контекст, цель, задачи и MCP независимо от разбора ответа. */
import {BoxRenderable,ScrollBoxRenderable,TextRenderable,StyledText,type CliRenderer} from '@opentui/core';
import {safeText,type Highlight} from './format.ts';
import {palette,chunk} from './theme.ts';

export class Sidebar extends BoxRenderable{
  private scroll:ScrollBoxRenderable;
  private contextText:TextRenderable;private goalText:TextRenderable;private tasks:TextRenderable;
  private goalHeading:TextRenderable;private todoHeading:TextRenderable;private goalExpanded=false;private todosExpanded=false;
  private mcpText:TextRenderable;private notes:TextRenderable;
  private context:any;private planning:any={goal:'',todos:[]};private servers:any[]=[];
  private sessionId='';private lastPrompt?:number;
  constructor(private renderer:CliRenderer,openMcp:()=>void){
    super(renderer,{id:'insights',width:32,height:'100%',flexShrink:0,flexDirection:'column',border:true,borderColor:palette.border,borderStyle:'rounded',paddingX:1,title:'Сессия'});
    this.scroll=new ScrollBoxRenderable(renderer,{width:'100%',height:'100%',scrollY:true,scrollX:false,contentOptions:{flexDirection:'column',gap:1}});this.add(this.scroll);
    const text=(id:string)=>{const item=new TextRenderable(renderer,{id,width:'100%',content:'',fg:palette.muted,wrapMode:'word',flexShrink:0,selectable:true});this.scroll.add(item);return item;};
    this.contextText=text('sidebar-context');this.goalHeading=text('sidebar-goal-heading');this.goalHeading.fg=palette.blue;
    this.goalHeading.onMouseDown=event=>{if(event.button===0){event.preventDefault();this.goalExpanded=!this.goalExpanded;this.renderState();}};
    this.goalText=text('sidebar-goal');this.todoHeading=text('sidebar-todo-heading');this.todoHeading.fg=palette.blue;
    this.todoHeading.onMouseDown=event=>{if(event.button===0){event.preventDefault();this.todosExpanded=!this.todosExpanded;this.renderState();}};
    this.tasks=text('sidebar-todo');
    const mcpHeading=text('sidebar-mcp-heading');mcpHeading.content='MCP · нажмите для списка';mcpHeading.fg=palette.blue;this.mcpText=text('sidebar-mcp');mcpHeading.onMouseDown=event=>{if(event.button===0){event.preventDefault();openMcp();}};
    this.notes=text('sidebar-notes');this.renderState();
  }
  update(event:any){
    const id=event.session_id||event.session?.id;
    if(id&&id!==this.sessionId){this.sessionId=id;this.goalExpanded=false;this.todosExpanded=false;this.lastPrompt=undefined;this.context=undefined;this.planning={goal:'',todos:[]};this.servers=[];this.notes.content='';}
    if('context' in event)this.context=event.context;
    if(event.planning)this.planning=event.planning;
    if(Array.isArray(event.mcp))this.servers=event.mcp;
    this.renderState();
  }
  setContext(context:any){this.context=context;if(context.planning)this.planning=context.planning;this.renderState();}
  usage(prompt:number){this.lastPrompt=Number.isFinite(prompt)&&prompt>0?prompt:undefined;this.renderState();}
  compactLabel(){const limit=Number(this.context?.max_input_tokens),used=Number(this.context?.estimated_tokens);return limit>0&&Number.isFinite(used)?`Контекст ~${Math.round(used/limit*100)}%`:'Контекст —';}
  highlights(items:Highlight[]){this.notes.content=items.slice(0,6).map(item=>`${({important:'ВАЖНО',warning:'ВНИМАНИЕ',error:'ОШИБКА',success:'РЕЗУЛЬТАТ'})[item.kind]}\n${safeText(item.text)}`).join('\n\n');}
  private renderState(){
    const limit=Number(this.context?.max_input_tokens),used=Number(this.context?.estimated_tokens);
    if(limit>0&&Number.isFinite(used)){
      const window=Number(this.context?.context_window)||limit;
      const sources:Record<string,string>={config:'настройки модели',api:'API провайдера',catalog:'каталог моделей',fallback:'неизвестно · резервный лимит'};
      const origin=sources[this.context?.context_source]||'настройки';
      const percent=used/limit*100,filled=Math.max(0,Math.min(16,Math.round(percent/100*16)));
      const color=percent>=90?'#b58b92':percent>=75?'#afa07c':palette.blue;
      this.contextText.content=new StyledText([chunk('Контекст · ~'+percent.toLocaleString('ru-RU',{maximumFractionDigits:1})+'%\n',color),
        chunk('━'.repeat(filled)+'─'.repeat(16-filled)+'\n',color),
        chunk(`Окно: ${window.toLocaleString('ru-RU')} ток.\n${origin}\n~${Math.round(used).toLocaleString('ru-RU')} / ${limit.toLocaleString('ru-RU')} ток.\nОценка следующего входа`,palette.muted),
        chunk(this.context?.input_limit_source==='config'?'\nВход ограничен настройками':'',palette.muted),
        chunk(this.lastPrompt!==undefined?`\nПоследний вход: ${this.lastPrompt.toLocaleString('ru-RU')} ток.`:'',palette.muted),
        chunk(`\nРезерв ответа: ${Number(this.context?.max_output_tokens||0)>0?Number(this.context?.max_output_tokens).toLocaleString('ru-RU'):'без ограничения'}`,palette.muted)]);
    }else this.contextText.content='Контекст\nЛимит пока неизвестен';
    const goal=safeText(this.planning.goal||'Появится после первого запроса');
    this.goalHeading.content='Цель'+(goal.length>112?(this.goalExpanded?' · свернуть':' · раскрыть'):'');
    this.goalText.content=new StyledText([chunk(!this.goalExpanded&&goal.length>112?goal.slice(0,112)+'…':goal,palette.fg)]);
    const items=Array.isArray(this.planning.todos)?this.planning.todos:[];
    const complete=items.filter((item:any)=>item.status==='completed').length;
    const symbols:Record<string,string>={pending:'○',in_progress:'◉',completed:'✓',cancelled:'–'};
    this.todoHeading.content=`To do · ${complete}/${items.length}`+(items.length>3||items.some((item:any)=>String(item.text).length>64)?(this.todosExpanded?' · свернуть':' · раскрыть'):'');
    const priority:Record<string,number>={in_progress:0,pending:1,completed:2,cancelled:3};
    const visible=this.todosExpanded?items:[...items].sort((a:any,b:any)=>(priority[a.status]??4)-(priority[b.status]??4)).slice(0,3);
    const itemText=(value:string)=>{const text=safeText(value);return !this.todosExpanded&&text.length>64?text.slice(0,64)+'…':text;};
    this.tasks.content=new StyledText([
      ...visible.flatMap((item:any)=>[chunk(`${symbols[item.status]||'○'} ${itemText(item.text)}\n`,item.status==='in_progress'?palette.fg:item.status==='completed'?palette.green:palette.muted)]),
      ...(!this.todosExpanded&&items.length>3?[chunk(`Ещё ${items.length-3} · нажмите To do`,palette.muted)]:[]),
      ...(!items.length?[chunk('Задач пока нет',palette.muted)]:[])]);
    const statuses:Record<string,string>={connected:'подключён',error:'ошибка',disabled:'выключен'};
    this.mcpText.content=new StyledText([
      ...this.servers.flatMap(server=>[chunk(`${server.state==='disabled'?'–':server.ok?'●':'×'} ${safeText(server.name)} · ${statuses[server.state]||(server.ok?'подключён':'ошибка')}${server.ok?` · ${(server.tools||[]).length} инстр.`:''}\n`,server.state==='disabled'?palette.muted:server.ok?palette.green:'#b58b92'),
        ...(server.error?[chunk('  '+safeText(server.error).slice(0,300)+'\n',palette.muted)]:[])]),
      ...(!this.servers.length?[chunk('Серверы не настроены',palette.muted)]:[])]);
    this.renderer.requestRender();
  }
}
