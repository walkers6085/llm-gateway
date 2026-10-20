import {
  createClipboard, createHostClipboard, createRendererClipboardAdapter,
  BoxRenderable, CodeRenderable, MarkdownRenderable, ScrollBoxRenderable, SyntaxStyle, TextRenderable,
  TextTableRenderable, TextareaRenderable, RGBA, StyledText, type CliRenderer, type KeyEvent, type Renderable,
} from '@opentui/core';
import {InputHistory} from './input-history.ts';
import {analyseResponse, safeText, safeMarkdown, type Highlight, type Table} from './format.ts';
import {ChoiceList} from './choice.ts';
import {ApprovalPanel,type Decision} from './approval.ts';
import {Sidebar} from './sidebar.ts';
import {palette,chunk} from './theme.ts';
import {createPicker,menuSpecs,menuShortcuts,type PickerState,type PickerChoice} from './menus.ts';

export type Command = {type:string; [key:string]:unknown};
export type AgentEvent = {type:string; [key:string]:any};
type MenuItem={kind:'command'|'tool';command:string;label?:string;aliases?:string[];description:string;arguments:'none'|'optional'|'required';name?:string;usage?:string;permission?:string};
type NavigationFrame={picker?:PickerState;query:string;index:number;key?:string;draft:string};
const highlightColor = {important:palette.blue,warning:palette.yellow,error:palette.red,success:palette.green};
const textLimit = 131072;

function tableView(renderer:CliRenderer, table:Table, id:string):TextTableRenderable {
  return new TextTableRenderable(renderer,{id,width:'100%',flexShrink:0,wrapMode:'word',columnWidthMode:'full',columnFitter:'balanced',
    cellPaddingX:1,cellPaddingY:0,showBorders:true,outerBorder:true,borderStyle:'rounded',borderColor:palette.border,
    content:[table.columns,...table.rows].map((row,index)=>row.map(value=>[{
      __isChunk:true as const,text:safeText(value),fg:RGBA.fromHex(index===0?palette.blue:palette.fg),
    }]))});
}

export function formatMetrics(metrics:AgentEvent):string{
  const seconds=Math.max(0,Math.floor(Number(metrics.worked_ms??metrics.duration_ms)/1000));
  const duration=(seconds>=3600?`${Math.floor(seconds/3600)}h `:'')+(seconds>=60?`${Math.floor(seconds/60)%60}m `:'')+`${seconds%60}s`;
  const date=new Date(metrics.completed_at);
  const finished=Number.isNaN(date.getTime())?'—':date.toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
  const speed=typeof metrics.tokens_per_second==='number'?`${metrics.tokens_per_second.toFixed(1)} ток/с`:'скорость н/д';
  return `Worked for ${duration} • ${finished} • ${speed}`;
}

class Answer {
  readonly box:BoxRenderable;
  private markdown?:MarkdownRenderable;
  source=''; raw=false; complete=false;
  private metrics?:AgentEvent;
  constructor(private renderer:CliRenderer,private syntax:SyntaxStyle,id:string,mode:string){
    this.box=new BoxRenderable(renderer,{id,width:'100%',flexDirection:'column',flexShrink:0,padding:1,border:true,borderStyle:'rounded',borderColor:palette.border,title:'Агент '+mode,backgroundColor:palette.panel});
  }
  update(source:string,complete=false){
    this.source=safeText(source);this.complete=complete;
    if(!complete && !this.raw && this.markdown){
      this.markdown.content=safeMarkdown(this.source.slice(0,textLimit));
      return;
    }
    this.rebuild();
  }
  setMetrics(metrics?:AgentEvent){this.metrics=metrics;this.rebuild();}
  setRaw(raw:boolean){this.raw=raw;this.rebuild();}
  private rebuild(){
    for(const child of this.box.getChildren()){this.box.remove(child);child.destroyRecursively();}
    this.markdown=undefined;
    const display=this.source.slice(0,textLimit);
    if(this.raw){
      this.box.add(new TextRenderable(this.renderer,{content:display,fg:palette.fg,wrapMode:'word'}));
    } else if(!this.complete){
      this.markdown=this.makeMarkdown(safeMarkdown(display),true);this.box.add(this.markdown);
    } else {
      const analysis=analyseResponse(display);
      for(const item of analysis.highlights.slice(0,6))this.box.add(new TextRenderable(this.renderer,{content:`${item.kind==='error'?'✗':item.kind==='warning'?'⚠':'◆'} ${item.text}`,fg:highlightColor[item.kind],wrapMode:'word'}));
      for(const block of analysis.blocks){
        if(block.type==='markdown')this.box.add(this.makeMarkdown(block.text,false));
        else {
          this.box.add(new TextRenderable(this.renderer,{content:'JSON → таблица · Ctrl+R: исходный ответ',fg:palette.muted}));
          this.box.add(tableView(this.renderer,block.table,`${this.box.id}-table-${this.box.getChildrenCount()}`));
          if(block.table.omittedRows || block.table.omittedColumns)
            this.box.add(new TextRenderable(this.renderer,{content:`Скрыто строк: ${block.table.omittedRows}; столбцов: ${block.table.omittedColumns}. Ctrl+R / /export для исходных данных.`,fg:palette.yellow}));
        }
      }
    }
    if(this.metrics)this.box.add(new TextRenderable(this.renderer,{content:formatMetrics(this.metrics),fg:palette.muted,wrapMode:'word'}));
    if(this.source.length>textLimit)this.box.add(new TextRenderable(this.renderer,{content:'Отображение ограничено; полный ответ доступен через /export.',fg:palette.yellow}));
  }
  private makeMarkdown(content:string,streaming:boolean){
    return new MarkdownRenderable(this.renderer,{content,streaming,syntaxStyle:this.syntax,fg:palette.fg,width:'100%',flexShrink:0,
      renderNode:(_token,context)=>{
        const rendered=context.defaultRender();
        const visible=(node:Renderable)=>{if(node instanceof CodeRenderable)node.drawUnstyledText=true;for(const child of node.getChildren())visible(child);};
        if(rendered)visible(rendered);return rendered;
      },
      tableOptions:{style:'grid',widthMode:'full',columnFitter:'balanced',wrapMode:'word',borders:true,outerBorder:true,borderStyle:'rounded',borderColor:palette.border,selectable:true}});
  }
}

type ToolCard={name:string;box:BoxRenderable;title:TextRenderable;details:TextRenderable;result?:TextRenderable;expanded:boolean};

export class AgentView {
  readonly root:BoxRenderable;readonly log:ScrollBoxRenderable;readonly editor:TextareaRenderable;
  private planActions:BoxRenderable;private planReady=false;
  private header:TextRenderable;private status:TextRenderable;private footer:TextRenderable;
  private rail:Sidebar;private prompt:TextRenderable;private mask:TextRenderable;
  private approvalBox:BoxRenderable;private approvalText:TextRenderable;private syntax:SyntaxStyle;
  private answers:Answer[]=[];private entries:Renderable[]=[];private tools=new Map<string,ToolCard>();
  private active?:Answer;private last?:Answer;private raw=false;private ready=false;private busy=false;
  private permission:ApprovalPanel;private workspace='';
  private pending?:AgentEvent;private dialog?:AgentEvent;private secret='';private closed=false;private serial=0;
  private usage={prompt:0,completion:0};private streamingText='';private timer?:ReturnType<typeof setTimeout>;
  private expanded=false;private disposed=false;private completionStatus?:string;
  private menu:BoxRenderable;private menuList:ChoiceList;private menuHint:TextRenderable;
  private catalog:MenuItem[]=[];private menuItems:MenuItem[]=[];private menuQuery='';private menuDismissed?:string;
  private navigation:NavigationFrame[]=[];private pendingNavigation?:NavigationFrame;private restoringNavigation?:NavigationFrame;private navigationDraft?:string;private returnAfterCancel=false;
  private picker?:PickerState;
  private shortcutDraft?:string;
  private paletteOpen=false;private paletteDraft='';
  private dialogChoiceId?:string;
  private queuedMenu?:string;
  private thoughts:{box:BoxRenderable;title:TextRenderable;body:TextRenderable;text:string;expanded:boolean}[]=[];
  private thought?:typeof this.thoughts[number];private thoughtsExpanded=false;
  private activityTimer?:ReturnType<typeof setInterval>;private busySince=0;private activity='Подготовка';private retryUntil=0;private frame=0;
  private history:InputHistory;
  private clickTimer?:ReturnType<typeof setTimeout>;
  private clickStart?:{x:number;y:number};
  private requestId='';private dialogDraft?:string;
  private steeringCards=new Map<string,BoxRenderable>();private pendingSteering=new Set<string>();
  private agentMode='build';private nextAgentMode?:string;private sessionInfo?:AgentEvent;private providerName='';
  private clipboard?:ReturnType<typeof createClipboard>;private clipboardMessage='';private clipboardUntil=0;
  private notices:{box:BoxRenderable;detail:TextRenderable}[]=[];private noticesExpanded=false;
  readonly keyHandler:(event:KeyEvent)=>void;
  private pasteHandler:(event:any)=>void;
  private resizeHandler:()=>void;

  constructor(private renderer:CliRenderer,private send:(command:Command)=>void,private exit:()=>void,
    private writeClipboard?:(text:string)=>Promise<'written'|'attempted'|'unsupported'>){
    this.history=new InputHistory(send,text=>{this.editor.setText(text);this.editor.gotoBufferEnd();});
    this.syntax=SyntaxStyle.fromStyles({default:{fg:palette.fg},'markup.heading':{fg:palette.blue,bold:true},
      'markup.heading.1':{fg:palette.blue,bold:true},'markup.heading.2':{fg:palette.blue,bold:true},
      'markup.strong':{fg:palette.fg,bold:true},'markup.bold':{fg:palette.fg,bold:true},'markup.link':{fg:palette.blue,underline:true},
      'markup.raw':{fg:palette.green},'markup.list':{fg:palette.blue},keyword:{fg:'#c4a7e7'},string:{fg:palette.green},comment:{fg:palette.muted,italic:true}},renderer.nativeScene);
    this.root=new BoxRenderable(renderer,{id:'gw-root',width:'100%',height:'100%',backgroundColor:palette.bg,flexDirection:'column',paddingX:1});
    this.header=new TextRenderable(renderer,{id:'header',height:2,content:'gwagent · подключение…',fg:palette.blue,wrapMode:'word',flexShrink:0});
    this.root.add(this.header);
    const body=new BoxRenderable(renderer,{id:'body',flexDirection:'row',flexGrow:1,minHeight:0,gap:1});this.root.add(body);
    this.log=new ScrollBoxRenderable(renderer,{id:'conversation',flexGrow:1,minWidth:0,height:'100%',scrollY:true,scrollX:false,stickyScroll:true,stickyStart:'bottom',contentOptions:{flexDirection:'column',gap:1,paddingRight:1}});
    body.add(this.log);
    this.rail=new Sidebar(renderer,()=>{if(!this.busy&&!this.pending&&!this.dialog&&this.ready){this.shortcutDraft=this.editor.plainText;this.sendCommand('/mcp');}});body.add(this.rail);
    this.permission=new ApprovalPanel(renderer,decision=>this.answerPermission(decision));this.root.add(this.permission);
    this.approvalBox=new BoxRenderable(renderer,{id:'approval',width:'100%',flexShrink:0,border:true,borderStyle:'rounded',borderColor:palette.yellow,paddingX:1,visible:false,flexDirection:'column'});
    this.approvalText=new TextRenderable(renderer,{content:'',fg:palette.yellow,wrapMode:'word'});this.approvalBox.add(this.approvalText);this.root.add(this.approvalBox);
    this.menu=new BoxRenderable(renderer,{id:'command-menu',width:'100%',flexShrink:0,flexDirection:'column',border:true,borderStyle:'rounded',borderColor:palette.blue,paddingX:1,visible:false,title:'Команды и инструменты'});
    this.menuList=new ChoiceList(renderer,'command-options');
    this.menuHint=new TextRenderable(renderer,{id:'command-hint',height:2,content:'',fg:palette.muted,wrapMode:'word'});
    this.menu.add(this.menuList);this.menu.add(this.menuHint);this.root.add(this.menu);
    this.menuList.on('selectionChanged',()=>this.menuDescription());
    this.menuList.on('activate',()=>{if(this.dialog?.choices)this.submit();else if(this.picker)this.choosePicker();else this.chooseMenu();});
    this.status=new TextRenderable(renderer,{id:'status',height:1,content:'Подготовка сессии…',fg:palette.muted,flexShrink:0});this.root.add(this.status);
    this.planActions=new BoxRenderable(renderer,{id:'plan-actions',width:'100%',height:1,flexShrink:0,flexDirection:'row',gap:2,visible:false});
    this.planActions.add(new TextRenderable(renderer,{id:'plan-build',content:'Перейти в Build · Tab',fg:palette.blue,onMouseDown:event=>{
      if(event.button!==0||!this.planActions.visible||this.busy||this.dialog||this.pending)return;
      event.preventDefault();this.switchAgent('build');
    }}));
    this.planActions.add(new TextRenderable(renderer,{id:'plan-clarify',content:'Уточнить план',fg:palette.muted,onMouseDown:event=>{
      if(event.button!==0||!this.planActions.visible||this.busy||this.dialog||this.pending)return;
      event.preventDefault();this.prompt.content='Уточните требования или задайте вопрос по плану · Enter — отправить';this.editor.focus();
    }}));
    this.root.add(this.planActions);
    const composer=new BoxRenderable(renderer,{id:'composer',width:'100%',height:5,flexShrink:0,border:true,borderColor:palette.border,borderStyle:'rounded',paddingX:1,flexDirection:'column'});
    this.prompt=new TextRenderable(renderer,{height:1,content:'Сообщение или /help',fg:palette.blue});composer.add(this.prompt);
    this.editor=new TextareaRenderable(renderer,{id:'message',width:'100%',height:2,placeholder:'Введите задачу…',textColor:palette.fg,backgroundColor:palette.bg,focusedBackgroundColor:palette.bg,cursorColor:palette.blue,
      keyBindings:[{name:'return',action:'submit'},{name:'return',shift:true,action:'newline'},{name:'a',ctrl:true,action:'select-all'}],onSubmit:()=>this.submit(),onContentChange:()=>{if(this.editor){this.history.changed(this.editor.plainText);this.updateMenu();}}});composer.add(this.editor);
    this.mask=new TextRenderable(renderer,{content:'',height:2,fg:palette.fg,visible:false});composer.add(this.mask);this.root.add(composer);
    this.footer=new TextRenderable(renderer,{height:1,content:'Enter — отправить · Shift+Enter — строка · Esc — отмена · Ctrl+D — выход',fg:palette.muted,flexShrink:0});this.root.add(this.footer);
    renderer.root.add(this.root);
    this.keyHandler=event=>this.onKey(event);
    this.pasteHandler=event=>{if(this.pending||this.dialog?.choices){event.preventDefault();event.stopPropagation();return;}if(this.dialog?.secret){event.preventDefault();event.stopPropagation();this.secret+=safeText(new TextDecoder().decode(event.bytes)).replace(/\n/g,'');this.mask.content='•'.repeat(Math.min(60,[...this.secret].length));}};
    this.resizeHandler=()=>{this.rail.visible=renderer.width>=100;this.permission.resizePanel();this.updateMenu();this.metrics();renderer.requestRender();};
    renderer.keyInput.on('keypress',this.keyHandler);renderer.keyInput.on('paste',this.pasteHandler);renderer.on('resize',this.resizeHandler);
    this.resizeHandler();this.editor.focus();
  }

  notificationStatus(status:string){if(this.disposed)return;this.footer.content='Enter — отправить · ↑ — история · Esc — отмена · '+status;}

  accept(event:AgentEvent){
    if(this.disposed)return;
    if(event.request_id)this.requestId=safeText(event.request_id);
    switch(event.type){
      case 'snapshot':{
        const changedSession=this.sessionInfo&&this.sessionInfo.id!==event.session.id;
        const s=event.session;this.workspace=safeText(s.workspace);this.rail.update(event);
        s.permissions_mode=event.permissions_mode||s.permissions_mode||'allow_all';this.sessionInfo=s;this.providerName=safeText(event.provider);this.agentMode=s.agent_mode==='plan'?'plan':'build';this.nextAgentMode=event.next_agent_mode||undefined;this.metrics();
        if(changedSession){this.history.reset();this.planReady=false;}
        if(changedSession || this.picker && this.picker.sessionId!==s.id){this.closePicker();this.resetNavigation();}
        if(Array.isArray(event.catalog))this.catalog=event.catalog.filter((item:any)=>
          item && ['command','tool'].includes(item.kind) && typeof item.command==='string' && item.command.startsWith('/') && typeof item.description==='string');
        this.updateHeader();
        if(Array.isArray(event.history)){
          this.clearHistory();
          if(event.history_truncated)this.notice('Показана последняя часть истории. Полная доступна через /export.');
          for(const m of event.history){
            if(m.role==='user')this.user(m.content||'',m.message_id);
            else if(m.role==='assistant'){
              if(m.reasoning_content){this.reasoning(m.reasoning_content);this.thought=undefined;}
              if(m.content){const answer=this.answer(m.content,true,m.agent_mode||'build');answer.setMetrics(m.metrics);}else if(m.metrics)this.answer('',true,m.agent_mode||'build').setMetrics(m.metrics);
              for(const call of m.tool_calls||[])this.tool(call.id,call.function?.name||'',call.function?.arguments);
            } else if(m.role==='tool')this.toolResult(m.tool_call_id,m.content||'',true);
          }
        }
        this.restoreQueuedInputs(event.pending_inputs);
        break;
      }
      case 'input_history':this.history.changed(this.editor.plainText);if(this.history.accept(event,this.sessionInfo?.id)&&event.error)this.notice(event.error,'error');break;
      case 'ready':this.ready=true;this.setBusy(false);this.status.content=this.completionStatus||'Готово к запросу';
        if(this.queuedMenu){const command=this.queuedMenu;this.queuedMenu=undefined;this.sendCommand(command);}break;
      case 'agent_mode':this.agentMode=event.mode==='plan'?'plan':'build';this.nextAgentMode=event.next_mode||undefined;this.updateHeader();this.metrics();break;
      case 'input_closed':if(this.dialog?.id===event.id&&this.dialog?.question)this.finishQuestion();break;
      case 'input_queued':this.queuedInput(event.id,event.text,event.status);this.metrics();break;
      case 'input_applied':this.queuedInput(event.id,event.text,'applied');this.metrics();break;
      case 'input_rejected':{const card=this.steeringCards.get(event.id);if(card)card.title='Вы · уточнение отклонено';this.pendingSteering.delete(event.id);this.notice(event.message,'warning');this.metrics();break;}
      case 'steering_rejected':{
        const join=(draft:string)=>event.text+(draft?'\n\n'+draft:'');
        if(this.dialogDraft!==undefined)this.dialogDraft=join(this.dialogDraft);else{this.editor.setText(join(this.editor.plainText));this.editor.gotoBufferEnd();}
        this.notice(event.message,'warning');break;
      }
      case 'dashboard':if(this.sessionInfo&&event.permissions_mode){this.sessionInfo.permissions_mode=event.permissions_mode;this.updateHeader();}this.restoreQueuedInputs(event.pending_inputs);this.rail.update(event);this.metrics();break;
      case 'context':this.retryUntil=0;this.activity='Запрос к модели';this.rail.setContext(event);this.metrics();break;
      case 'user':this.planReady=false;this.usage={prompt:0,completion:0};this.user(event.text,event.message_id);break;
      case 'reasoning':this.activity='Модель рассуждает';this.reasoning(event.text);break;
      case 'text':{
        this.activity='Модель отвечает';
        if(!this.active){this.active=this.answer('',false);this.streamingText='';}
        this.streamingText+=safeText(event.text);
        if(!this.timer)this.timer=setTimeout(()=>this.flush(),50);
        break;
      }
      case 'assistant':{const answer=this.active;this.finishAnswer();const completed=answer||this.answer(event.content||'',true);if(event.metrics)completed.setMetrics(event.metrics);this.thought=undefined;break;}
      case 'tool_call':this.activity='Инструмент: '+safeText(event.name);this.tool(event.id,event.name,event.arguments);break;
      case 'subagent_start':this.activity='Сабагент · '+(event.role||'worker');this.tool('child-'+event.session_id,'Сабагент · '+(event.role||'worker'),event.task);break;
      case 'subagent_event':{
        const child=event.event;
        if(child.type==='tool_call')this.activity='Сабагент · '+safeText(child.name);
        if(child.type==='reasoning')this.reasoning(child.text);
        if(child.type==='assistant'){this.thought=undefined;if(child.metrics){const card=this.tools.get('child-'+event.session_id);card?.box.add(new TextRenderable(this.renderer,{content:formatMetrics(child.metrics),fg:palette.muted,wrapMode:'word'}));}}
        if(child.type==='warning'){if(child.retry?.reset_response)this.resetResponse();this.notice('Сабагент: '+child.message,'warning');}
        if(child.type==='error'&&child.reset_response)this.resetResponse();
        if(child.type==='error')this.notice('Сабагент: '+child.message,'error');
        break;
      }
      case 'subagent_end':this.thought=undefined;this.toolResult('child-'+event.session_id,event.content||'Завершение: '+event.reason,event.reason==='stop');this.activity='Продолжение запроса';break;
      case 'tool_result':
        if(this.pending?.id===event.id){this.pending=undefined;this.permission.visible=false;this.prompt.content=this.busy?'Уточнение в текущий цикл · Enter — отправить':'Сообщение или /help';if(this.ready)this.editor.focus();}
        if(!this.tools.has(event.id))this.tool(event.id,event.name||event.id,'');
        this.toolResult(event.id,event.content,event.ok,event.ms);break;
      case 'permission_request':{
        this.closePicker();this.closePalette();this.pending=event;this.editor.blur();
        this.approvalBox.visible=false;this.permission.visible=false;
        this.permission.open({...event,id:event.id,name:event.name,workspace:this.workspace});
        this.prompt.content='Черновик сохранён · выберите ответ выше';
        this.status.content='Выберите, разрешить ли действие';break;
      }
      case 'input_request':if(this.dialogDraft===undefined)this.dialogDraft=this.editor.plainText;this.closePicker();this.closePalette();this.dialog=event;this.secret='';this.editor.setText('');this.prompt.content=safeText(event.prompt);this.mask.visible=!!event.secret;this.editor.visible=!event.secret;
        this.approvalBox.title=event.question?'Вопрос от '+safeText(event.agent||this.agentMode):event.secret?'ЗАЩИЩЁННЫЙ ВВОД':'ДИАЛОГ';this.approvalBox.borderColor=event.question?palette.border:palette.blue;this.approvalText.fg=event.question?palette.fg:palette.yellow;if(event.question)this.activity='Ожидаю ваш ответ';this.approvalBox.visible=true;
        this.approvalText.content=`${safeText(event.prompt)}\n${event.secret?'Содержимое скрыто · ':''}Enter — подтвердить · Esc — отменить`;
        if(event.secret){this.editor.blur();this.mask.content='';}else this.editor.focus();break;
      case 'draft':this.dialogDraft=undefined;this.editor.setText(safeText(event.text));this.editor.gotoBufferEnd();break;
      case 'usage':this.rail.usage(Number(event.prompt_tokens));this.usage.prompt+=Number(event.prompt_tokens)||0;this.usage.completion+=Number(event.completion_tokens)||0;this.metrics();break;
      case 'warning':if(event.retry?.reset_response)this.resetResponse();if(event.retry){this.retryUntil=Date.now()+Number(event.retry.delay)*1000;this.activity='Ожидание повтора · HTTP '+event.retry.status;this.animateActivity();}this.notice(event.message,'warning');break;
      case 'error':case 'fatal':if(event.reset_response)this.resetResponse();this.finishAnswer();this.notice(event.message,'error');if(event.type==='fatal'){this.closePicker();this.closePalette();this.dialog=undefined;this.pending=undefined;this.secret='';this.mask.content='';this.mask.visible=false;this.editor.visible=true;this.ready=false;this.closed=true;this.setBusy(false);this.status.content='Не удалось подключиться · Ctrl+D — выход';}break;
      case 'notice':this.notice(event.message,event.level==='error'?'error':'important');break;
      case 'table':
        if(Object.hasOwn(menuSpecs,event.selection?.kind))this.openPicker(event);
        else this.addTable(event.title,{columns:event.columns.map(safeText),rows:event.rows.map((r:unknown[])=>r.map(safeText)),omittedRows:0,omittedColumns:0});
        break;
      case 'done':this.planReady=event.reason==='stop'&&(event.agent_mode||this.agentMode)==='plan';if(event.reason==='cancelled'&&this.pendingNavigation)this.returnAfterCancel=true;this.finishAnswer();this.thought=undefined;if(this.activityTimer){clearInterval(this.activityTimer);this.activityTimer=undefined;}this.status.height=1;this.status.fg=palette.muted;this.pending=undefined;this.approvalBox.visible=false;this.permission.visible=false;this.status.content=this.completionStatus=({stop:'Готово',cancelled:'Отменено',error:'Ошибка',budget:'Достигнут бюджет',max_steps:'Лимит шагов',blocked:'Заблокировано'} as Record<string,string>)[event.reason]||safeText(event.reason);break;
      case 'idle':if(this.dialogDraft!==undefined){if(!this.picker){this.editor.setText(this.dialogDraft);this.editor.gotoBufferEnd();}this.dialogDraft=undefined;}this.dialog=undefined;this.secret='';this.mask.content='';this.mask.visible=false;this.editor.visible=true;this.prompt.content='Сообщение или /help';this.pending=undefined;this.approvalBox.visible=false;this.permission.visible=false;this.setBusy(false);this.shortcutDraft=undefined;this.status.content=this.completionStatus||'Готово к запросу';if(this.returnAfterCancel&&this.pendingNavigation){const frame=this.pendingNavigation;this.pendingNavigation=undefined;this.returnAfterCancel=false;this.restoreFrame(frame);}else if(this.pendingNavigation&&!this.picker)this.resetNavigation();break;
    }
    this.updateMenu();
    this.renderer.requestRender();
  }

  backendClosed(code:number){
    this.closePicker();
    this.finishAnswer();this.ready=false;this.closed=true;this.setBusy(false);
    this.dialog=undefined;this.secret='';this.mask.content='';this.mask.visible=false;this.editor.visible=true;
    this.pending=undefined;this.approvalBox.visible=false;this.permission.visible=false;
    this.status.content=`Подключение закрыто (${code}) · Ctrl+D — выход`;
    this.updateMenu();
  }

  private updateMenu(){
    if(!this.editor || this.disposed)return;
    if(this.planActions)this.planActions.visible=this.planReady&&this.agentMode==='plan'&&!this.nextAgentMode&&this.ready&&!this.closed&&!this.busy&&!this.pending&&!this.dialog&&!this.picker&&!this.paletteOpen&&!this.editor.plainText.startsWith('/');
    if(this.dialog?.choices){this.updateDialogChoices();return;}
    if(this.picker){this.updatePicker();return;}
    this.menu.title=this.catalog.some(item=>item.label)?'Главное меню':'Команды и инструменты';
    const text=this.editor.plainText;
    if(this.menuDismissed!==undefined && text!==this.menuDismissed)this.menuDismissed=undefined;
    const query=this.paletteOpen?text.toLowerCase():text.startsWith('/')?text.slice(1).toLowerCase():'';
    if((!this.paletteOpen && !text.startsWith('/')) || text.includes('\n') || this.dialog || this.pending || this.busy || !this.ready || this.closed || this.menuDismissed===text || this.renderer.height<16){
      this.menu.visible=false;return;
    }
    // Аргументы обычной команды редактируются без перехвата Enter меню.
    const argumentInput=!this.paletteOpen && /\s/.test(query) && !this.catalog.some(item=>item.command.slice(1).startsWith(query.trimStart()) && (item.command!==text.trimEnd() || item.arguments==='required'));
    if(argumentInput){this.menu.visible=false;return;}
    const changed=query!==this.menuQuery;this.menuQuery=query;
    const selected=this.menuList.getSelectedIndex();
    const score=(item:MenuItem)=>item.command.slice(1)===query||item.aliases?.includes(query)?0:(item.label||item.name||item.command.slice(1)).toLowerCase().startsWith(query)?1:2;
    this.menuItems=this.catalog.filter(item=>!query || `${item.command} ${item.label||''} ${item.name||''} ${(item.aliases||[]).join(' ')} ${item.description}`.toLowerCase().includes(query))
      .sort((a,b)=>score(a)-score(b));
    this.menuList.options=this.menuItems.map(item=>({name:item.label||item.command+' — '+safeText(item.description).replace(/\s+/g,' '),group:query||item.label?'':item.kind==='tool'?'Инструменты':'Команды',badges:item.kind==='tool'?[{text:'инструмент',tone:'info' as const}]:[],value:item}));
    this.menuList.height=Math.min(12,Math.max(1,this.renderer.height-15),Math.max(1,this.menuItems.length+(query?0:2)));
    this.menuList.setSelectedIndex(changed?0:Math.max(0,Math.min(selected,this.menuItems.length-1)));
    this.menu.visible=true;this.menuDescription();this.renderer.requestRender();
  }

  private menuDescription(){
    if(this.dialog?.choices){this.menuHint.content='↑/↓ — выбор · Enter — подтвердить · Esc — отменить';return;}
    if(this.picker){
      const picker=this.picker;
      const count=this.menuList.options.length;
      const choice=this.menuList.getSelectedOption()?.value as PickerChoice|undefined;
      const crossProvider=choice?.provider!==undefined && choice.provider!==picker.currentProvider;
      const active=picker.choices.find(item=>item.key===picker.current);
      const current=active?(active.group?active.group+' / ':'')+active.name:picker.currentLabel;
      this.menuHint.content=`${picker.kind==='provider'?'Провайдер':'Текущая'}: ${safeText(current)} · найдено: ${count}${count?'':' · '+(picker.choices.length?'совпадений нет':menuSpecs[picker.kind].empty)}${crossProvider?' · новая сессия':''}\n↑/↓ — выбор · Enter — ${menuSpecs[picker.kind].action} · Tab — раздел · Esc — отмена`;
      return;
    }
    const item=this.menuItems[this.menuList.getSelectedIndex()];
    this.menuHint.content=item
      ? `${safeText(item.label?item.description:item.usage||item.command)}${item.kind==='tool'?` · по умолчанию: ${safeText(item.permission)} · открыть параметры`:''}\n↑/↓ — выбор · Enter — открыть · Tab — подставить · Esc — закрыть (${this.menuItems.length})`
      : 'Совпадений нет. Измените запрос или нажмите Esc.';
  }

  private chooseMenu(completeOnly=false){
    const item=this.menuItems[this.menuList.getSelectedIndex()];
    if(!item)return;
    const savedDraft=this.paletteOpen?this.paletteDraft:undefined;
    if(item.label&&!completeOnly)this.pendingNavigation={query:this.editor.plainText.replace(/^\//,''),index:this.menuList.getSelectedIndex(),draft:this.paletteOpen?this.paletteDraft:''};
    const text=item.command+(completeOnly && item.arguments!=='none'?' ':'');
    this.closePalette();this.menuDismissed=text;this.menu.visible=false;
    if(savedDraft!==undefined && !completeOnly){
      this.shortcutDraft=savedDraft;this.sendCommand(item.command);return;
    }
    this.editor.setText(text);this.editor.gotoBufferEnd();this.editor.focus();
    if(!completeOnly)this.submit();
  }

  private sendCommand(text:string){
    if(!this.ready||this.busy||this.closed||this.pending||this.dialog)return;
    if(text==='/help'){this.shortcutDraft=undefined;this.openPalette();return;}
    this.setBusy(true);this.send({type:'submit',text});
  }

  private cancelInput(){
    this.send({type:'cancel'});this.dialog=undefined;this.pending=undefined;this.secret='';
    this.mask.content='';this.mask.visible=false;this.editor.visible=true;this.editor.blur();
    this.menu.visible=false;this.approvalBox.visible=false;this.permission.visible=false;this.status.content='Отмена…';
  }

  private openPalette(){
    this.resetNavigation();
    this.closePicker();this.paletteDraft=this.editor.plainText;this.paletteOpen=true;this.menuDismissed=undefined;
    this.editor.setText('');this.prompt.content='Поиск команды или инструмента';this.updateMenu();
  }

  private closePalette(){
    if(!this.paletteOpen)return;
    this.paletteOpen=false;this.menu.visible=false;this.editor.setText(this.paletteDraft);this.editor.gotoBufferEnd();
    this.prompt.content='Сообщение или /help';
    if(!this.pendingNavigation)this.resetNavigation();
  }

  private updateDialogChoices(){
    const dialog=this.dialog;if(!dialog?.choices)return;
    const initial=this.dialogChoiceId!==dialog.id;this.dialogChoiceId=dialog.id;
    this.menuList.options=dialog.choices.map((choice:any)=>({name:choice.label,description:choice.description,
      disabled:choice.disabled,badges:choice.danger?[{text:'необратимо',tone:'error'}]:[],value:choice}));
    if(initial){
      const index=dialog.default_value===undefined?0:dialog.choices.findIndex((choice:any)=>choice.value===dialog.default_value);
      this.menuList.setSelectedIndex(Math.max(0,index));
    }
    this.menuList.height=Math.min(6,Math.max(1,this.renderer.height-19),dialog.choices.reduce((count:number,choice:any)=>count+(choice.description?2:1),0));
    this.menu.title=dialog.question?'Ответьте на вопрос':'Выберите действие';this.menu.visible=true;this.menuDescription();
  }

  private openPicker(event:AgentEvent){
    this.closePalette();
    const parent=this.pendingNavigation;
    if(parent&&!event.selection.replace){this.navigation.push(parent);if(this.navigation.length>32)this.navigation.shift();}
    if(!event.selection.route&&!parent)this.navigationDraft=undefined;
    this.navigationDraft??=parent?.draft??this.shortcutDraft??this.editor.plainText;
    this.pendingNavigation=undefined;
    const picker=createPicker(event,this.navigationDraft);this.shortcutDraft=undefined;
    const restore=this.restoringNavigation;this.restoringNavigation=undefined;
    if(restore){picker.current=restore.key||picker.current;picker.restoreIndex=restore.index;}
    this.picker=picker;this.editor.setText(restore?.query||'');this.updatePicker();
  }

  private updatePicker(){
    const picker=this.picker;if(!picker)return;
    if(!this.ready || this.closed || this.busy || this.pending || this.dialog){this.menu.visible=false;return;}
    const query=this.editor.plainText.toLowerCase().trim();
    const previous=(this.menuList.getSelectedOption()?.value as PickerChoice|undefined)?.key;
    const options=picker.choices.filter(choice=>query.split(/\s+/).every(term=>choice.label.toLowerCase().includes(term))).map(choice=>({
      name:choice.name,description:choice.description,group:choice.group,badges:choice.badges,disabled:choice.disabled,current:choice.key===picker.current,value:choice,
    }));
    const initial=picker.query===undefined;
    const changed=picker.query!==query;picker.query=query;
    this.menuList.options=options;
    const index=options.findIndex(option=>option.value.key===(initial?picker.current:previous));
    this.menuList.setSelectedIndex(initial?Math.max(0,index<0?picker.restoreIndex||0:index):changed?0:Math.max(0,index));
    this.menuList.height=Math.min(20,Math.max(3,this.renderer.height-15),Math.max(1,options.reduce((sum,item)=>sum+(item.description?2:1),0)+new Set(options.map(item=>item.group).filter(Boolean)).size));
    this.menu.title=picker.title||menuSpecs[picker.kind].title;this.menu.visible=true;
    this.prompt.content=menuSpecs[picker.kind].search;
    this.menuDescription();this.renderer.requestRender();
  }

  private closePicker(){
    const picker=this.picker;if(!picker)return;
    this.picker=undefined;this.menu.visible=false;
    this.editor.setText(picker.draft);this.editor.gotoBufferEnd();
    this.prompt.content='Сообщение или /help';
    if(this.ready && !this.busy)this.editor.focus();
  }

  private choosePicker(){
    const picker=this.picker;
    const choice=this.menuList.getSelectedOption()?.value as PickerChoice|undefined;
    if(!picker || !choice || !picker.choices.some(item=>item.key===choice.key))return;
    if(picker.route)this.pendingNavigation={picker,query:this.editor.plainText,index:this.menuList.getSelectedIndex(),key:choice.key,draft:picker.draft};
    this.closePicker();
    if(picker.kind==='command'||picker.kind==='session'||picker.kind==='session-delete'||picker.kind==='revert'){
      this.setBusy(true);this.send({type:'select',kind:picker.kind,target:choice.target,session_id:picker.sessionId});
    }else if(choice.command){
      this.sendCommand(choice.command);
    }else if(choice.provider!==undefined){
      this.setBusy(true);this.send({type:'select',kind:picker.kind,provider:choice.provider,...(choice.model?{model:choice.model}:{}),session_id:picker.sessionId});
    }else{
      this.editor.setText('/model '+choice.model);this.menuDismissed=this.editor.plainText;
      this.submit();
    }
  }

  private resetNavigation(){this.navigation=[];this.pendingNavigation=undefined;this.restoringNavigation=undefined;this.navigationDraft=undefined;this.returnAfterCancel=false;}

  private restoreFrame(frame:NavigationFrame){
    this.closePicker();
    if(!frame.picker){
      this.paletteDraft=frame.draft;this.paletteOpen=true;this.editor.setText(frame.query);this.menuDismissed=undefined;this.updateMenu();this.menuList.setSelectedIndex(frame.index);return;
    }
    this.restoringNavigation=frame;this.setBusy(true);
    this.send({type:'navigate',route:frame.picker.route,session_id:frame.picker.sessionId});
  }

  private backPicker(){
    const parent=this.navigation.pop();
    if(parent)this.restoreFrame(parent);else{this.closePicker();this.resetNavigation();}
  }

  private append(item:Renderable){
    this.log.add(item);this.entries.push(item);
    while(this.entries.length>80){
      const old=this.entries.shift()!;this.log.remove(old);old.destroyRecursively();
      this.answers=this.answers.filter(a=>a.box!==old);this.thoughts=this.thoughts.filter(a=>a.box!==old);this.notices=this.notices.filter(a=>a.box!==old);
      for(const [id,card] of this.tools)if(card.box===old)this.tools.delete(id);
    }
  }
  private user(text:string,messageId?:string){
    const box=new BoxRenderable(this.renderer,{width:'100%',flexShrink:0,border:true,borderStyle:'rounded',borderColor:palette.blue,title:'Вы',paddingX:1});
    if(messageId)box.onMouse=event=>{
      const cancel=()=>{if(this.clickTimer)clearTimeout(this.clickTimer);this.clickTimer=undefined;};
      if(event.type==='down'){
        if(this.clickTimer){cancel();this.clickStart=undefined;return;}
        this.clickStart=event.button===0&&!Object.values(event.modifiers).some(Boolean)?{x:event.x,y:event.y}:undefined;
      }
      if(event.type==='drag'){cancel();this.clickStart=undefined;}
      if(event.type==='up'&&this.clickStart){
        const start=this.clickStart;this.clickStart=undefined;
        if(event.x!==start.x||event.y!==start.y)return;
        const session=this.sessionInfo?.id;
        this.clickTimer=setTimeout(()=>{
          this.clickTimer=undefined;
          if(this.disposed||box.isDestroyed||session!==this.sessionInfo?.id||this.renderer.getSelection()?.getSelectedText())return;
          if(this.busy){this.notice('Редактирование доступно после завершения или отмены запроса');return;}
          if(this.pendingSteering.has(messageId)){this.notice('Уточнение ещё не применено');return;}
          if(this.dialog||this.pending)return;
          this.send({type:'edit_message',session_id:session,message_id:messageId});this.setBusy(true);
        },250);
      }
    };
    box.add(new TextRenderable(this.renderer,{content:safeText(text).slice(0,textLimit),fg:palette.fg,wrapMode:'word'}));this.append(box);return box;
  }
  private queuedInput(id:string,text:string,status='pending'){
    let card=this.steeringCards.get(id);if(!card){card=this.user(text,id);this.steeringCards.set(id,card);}
    card.title=status==='pending'?'Вы · уточнение ожидает безопасного шага':'Вы · уточнение учтено';
    if(status==='pending')this.pendingSteering.add(id);else this.pendingSteering.delete(id);
  }
  private restoreQueuedInputs(inputs:any){
    if(!Array.isArray(inputs))return;
    this.pendingSteering.clear();for(const item of inputs)this.queuedInput(item.id,item.text,item.status);
  }
  private answer(text:string,complete:boolean,mode=this.agentMode){
    const answer=new Answer(this.renderer,this.syntax,`answer-${++this.serial}`,mode);answer.raw=this.raw;answer.update(text,complete);this.answers.push(answer);this.last=answer;this.append(answer.box);
    if(complete)this.highlights(analyseResponse(text.slice(0,textLimit)).highlights);
    return answer;
  }
  private flush(){
    if(this.timer){clearTimeout(this.timer);this.timer=undefined;}
    if(!this.active)return;
    this.active.update(this.streamingText,false);this.highlights(analyseResponse(this.streamingText.slice(0,textLimit)).highlights);this.renderer.requestRender();
  }
  private resetResponse(){
    if(this.timer){clearTimeout(this.timer);this.timer=undefined;}
    const discarded=[this.active?.box,this.thought?.box].filter((box):box is BoxRenderable=>!!box);
    this.active=undefined;this.thought=undefined;this.streamingText='';
    for(const box of discarded){
      this.entries=this.entries.filter(entry=>entry!==box);this.answers=this.answers.filter(answer=>answer.box!==box);
      this.thoughts=this.thoughts.filter(thought=>thought.box!==box);this.log.remove(box);box.destroyRecursively();
    }
    this.last=this.answers.at(-1);this.highlights([]);
  }
  private finishAnswer(){
    this.flush();if(this.active){this.active.update(this.streamingText,true);this.active=undefined;}this.streamingText='';
  }
  private highlights(items:Highlight[]){
    this.rail.highlights(items);
  }

  private notice(text:string,kind:Highlight['kind']='important'){
    const full=safeText(text).slice(0,textLimit);
    const lines=full.split('\n');let expanded=this.noticesExpanded;
    const labelColor=kind==='error'?'#b58b92':kind==='warning'?'#afa07c':palette.muted;
    const label=({important:'· система',warning:'! внимание',error:'× ошибка',success:'✓ готово'})[kind];
    const box=new BoxRenderable(this.renderer,{width:'100%',flexShrink:0,flexDirection:'column',paddingX:1});
    const title=new TextRenderable(this.renderer,{content:new StyledText([chunk(label+' · ',labelColor),chunk(lines[0]?.slice(0,160)||'',palette.muted)]),wrapMode:'none',height:1,fg:palette.muted});
    const detail=new TextRenderable(this.renderer,{content:full,fg:palette.muted,wrapMode:'word',visible:expanded});
    title.onMouseDown=()=>{expanded=!expanded;detail.visible=expanded;this.renderer.requestRender();};
    if(lines.length>1||full.length>Math.max(20,this.renderer.width-(this.renderer.width>=100?70:35))){
      title.content=new StyledText([chunk(label+' · ',labelColor),chunk((lines[0]?.slice(0,130)||'')+' … [клик / Ctrl+E]',palette.muted)]);
    }
    box.add(title);box.add(detail);this.notices.push({box,detail});this.append(box);
  }
  private reasoning(text:string){
    if(!this.thought){
      const card={} as typeof this.thoughts[number];card.text='';card.expanded=this.thoughtsExpanded;
      card.box=new BoxRenderable(this.renderer,{width:'100%',flexShrink:0,flexDirection:'column',paddingX:1});
      card.title=new TextRenderable(this.renderer,{content:'',fg:palette.muted});
      card.body=new TextRenderable(this.renderer,{content:'',fg:palette.muted,wrapMode:'word',visible:card.expanded});
      card.title.onMouseDown=()=>{card.expanded=!card.expanded;card.body.visible=card.expanded;this.updateThoughtTitle(card);};
      card.box.add(card.title);card.box.add(card.body);this.thoughts.push(card);this.thought=card;this.append(card.box);
    }
    this.thought.text=(this.thought.text+safeText(text)).slice(0,textLimit);
    this.thought.body.content=this.thought.text;this.updateThoughtTitle(this.thought);
  }
  private updateThoughtTitle(card:typeof this.thoughts[number]){
    card.title.content=`${card.expanded?'▾':'▸'} Reasoning · ${card.text.length} симв. · клик / Ctrl+T`;
    this.renderer.requestRender();
  }
  private animateActivity(){
    if(!this.busy||this.pending||this.dialog||this.completionStatus)return;
    if(this.retryUntil&&this.retryUntil<=Date.now()){this.retryUntil=0;this.activity='Повторный запрос к модели';}
    const waiting=this.retryUntil?' · ещё '+Math.ceil((this.retryUntil-Date.now())/1000)+' с':'';
    const frames=['● ○ ○','○ ● ○','○ ○ ●','○ ● ○'];
    this.status.height=2;this.status.fg=palette.blue;
    this.status.content=`${frames[this.frame++%frames.length]}  ${this.activity}${waiting} · ${Math.floor((Date.now()-this.busySince)/1000)} с\n${Date.now()<this.clipboardUntil?this.clipboardMessage:'Esc / Ctrl+C — отменить'+(this.requestId?' · запрос '+this.requestId:'')}`;
    this.renderer.requestRender();
  }
  private addTable(title:string,table:Table){
    const box=new BoxRenderable(this.renderer,{width:'100%',flexShrink:0,flexDirection:'column',border:true,borderStyle:'rounded',borderColor:palette.border,title:safeText(title),paddingX:1});
    box.add(tableView(this.renderer,table,`table-${++this.serial}`));this.append(box);
  }
  private tool(id:string,name:string,args:unknown){
    const existing=this.tools.get(id);if(existing)return existing;
    const card={} as ToolCard;
    card.name=safeText(name);card.expanded=this.expanded;
    card.box=new BoxRenderable(this.renderer,{width:'100%',flexShrink:0,flexDirection:'column',border:true,borderStyle:'rounded',borderColor:palette.border,paddingX:1});
    card.title=new TextRenderable(this.renderer,{content:`… ${safeText(name)} · нажмите для подробностей`,fg:palette.blue,wrapMode:'word'});
    card.title.onMouseDown=()=>{card.expanded=!card.expanded;card.details.visible=card.expanded;if(card.result)card.result.visible=card.expanded;};
    card.details=new TextRenderable(this.renderer,{content:safeText(typeof args==='string'?args:JSON.stringify(args,null,2)).slice(0,10000),fg:palette.muted,visible:card.expanded,wrapMode:'word'});
    card.box.add(card.title);card.box.add(card.details);this.tools.set(id,card);this.append(card.box);return card;
  }
  private toolResult(id:string,text:string,ok:boolean,ms?:number){
    const card=this.tools.get(id)||this.tool(id,id,'');
    card.title.content=new StyledText([chunk(ok?'✓ ':'× ',ok?palette.green:'#b58b92'),
      chunk(`${card.name}${ms!=null?` · ${ms} мс`:''} · ${safeText(text).split('\n')[0]?.slice(0,100)}`,palette.muted)]);
    if(card.result){card.box.remove(card.result);card.result.destroyRecursively();}
    card.result=new TextRenderable(this.renderer,{content:safeText(text).slice(0,textLimit),fg:palette.fg,visible:card.expanded,wrapMode:'word'});card.box.add(card.result);
  }
  private updateHeader(){
    const s=this.sessionInfo;if(!s)return;
    const mode=this.agentMode.toUpperCase()+(this.nextAgentMode&&this.nextAgentMode!==this.agentMode?' → '+this.nextAgentMode.toUpperCase():'');
    const access=({allow_all:'Полный доступ',default:'С подтверждениями',read_only:'Только чтение'} as Record<string,string>)[s.permissions_mode]||s.permissions_mode;
    this.header.content=`gwagent · ${mode} · ${safeText(access)} · ${this.providerName} / ${safeText(s.model||'модель не выбрана')} · ${safeText(s.title||s.id)}\n${safeText(s.workspace)} · ${safeText(s.id)}`;
  }
  private finishQuestion(){
    this.dialog=undefined;this.menu.visible=false;this.approvalBox.visible=false;this.secret='';this.mask.visible=false;this.editor.visible=true;
    if(this.dialogDraft!==undefined){this.editor.setText(this.dialogDraft);this.editor.gotoBufferEnd();this.dialogDraft=undefined;}
    this.prompt.content=this.busy?'Уточнение в текущий цикл · Enter — отправить':'Сообщение или /help';
    this.activity='Продолжение запроса';if(this.ready)this.editor.focus();
  }
  private metrics(){
    const keys=this.renderer.width>=140?Object.entries(menuShortcuts).map(([key,item])=>(key==='f1'?'F1/Ctrl+P':key.toUpperCase())+' '+item.label).join(' · '):'F1/Ctrl+P команды';
    const usage=this.renderer.width>=110?`Токены: ${this.usage.prompt} → ${this.usage.completion} · `:'';
    this.footer.content=(this.renderer.width<100?this.rail.compactLabel()+' · ':'')+this.agentMode.toUpperCase()+(this.nextAgentMode&&this.nextAgentMode!==this.agentMode?' → '+this.nextAgentMode.toUpperCase()+' со следующего запроса':'')+' · Tab — агент · '+(this.pendingSteering.size?`Уточнений: ${this.pendingSteering.size} · `:'')+usage+'Esc отмена · Ctrl+C копия/отмена · Ctrl+Y ответ · '+keys+' · Ctrl+D выход';
  }
  private setBusy(value:boolean){
    this.busy=value;
    if(value&&this.planActions)this.planActions.visible=false;
    if(!this.pending&&!this.dialog)this.prompt.content=value?'Уточнение в текущий цикл · Enter — отправить':'Сообщение или /help';
    if(this.activityTimer){clearInterval(this.activityTimer);this.activityTimer=undefined;}
    if(!value && this.ready)this.editor.focus();else if(value && !this.pending && !this.dialog && this.ready)this.editor.focus();
    if(value){this.retryUntil=0;this.completionStatus=undefined;this.busySince=Date.now();this.activity='Подготовка запроса';this.requestId='';this.animateActivity();this.activityTimer=setInterval(()=>this.animateActivity(),180);}
    else{this.status.height=1;this.status.fg=palette.muted;}
    this.updateMenu();this.metrics();
  }
  submit(){
    if(this.dialog){
      if(this.dialog.question&&this.dialog.choices&&this.menuList.getSelectedOption()?.value?.custom){
        this.dialog={...this.dialog,choices:undefined};this.menu.visible=false;this.editor.setText('');this.editor.focus();this.prompt.content='Ваш ответ · Enter — отправить · Esc — пропустить';return;
      }
      const value=this.dialog.secret?this.secret:this.dialog.choices?this.menuList.getSelectedOption()?.value?.value:this.editor.plainText;
      if(typeof value!=='string')return;
      if(this.dialog.question){
        if(!value.trim())return;
        if(value.length>16000){this.notice('Ответ длиннее 16000 символов','error');return;}
        this.send({type:'answer',id:this.dialog.id,value});this.finishQuestion();return;
      }
      this.send({type:'answer',id:this.dialog.id,value});this.dialog=undefined;this.secret='';this.mask.content='';this.menu.visible=false;this.approvalBox.visible=false;this.permission.visible=false;this.editor.setText('');this.editor.blur();return;
    }
    if(this.picker){if(!this.busy)this.choosePicker();return;}
    const text=this.editor.plainText.trim();
    if(!text||!this.ready||this.pending)return;
    if(this.busy){
      if(text.startsWith('/')){this.notice('Команды доступны после завершения; уточнение отправляется обычным текстом');return;}
      if(new TextEncoder().encode(text).length>65536){this.notice('Уточнение длиннее 65536 байт','error');return;}
      const id=`input-${crypto.randomUUID()}`;this.editor.setText('');this.send({type:'steer',id,text});return;
    }
    if(text==='/help'){this.editor.setText('');this.openPalette();return;}
    this.editor.setText('');this.setBusy(true);this.send({type:'submit',text});
  }
  private answerPermission(decision:Decision){
    const request=this.pending;if(!request)return;
    this.send({type:'approve',id:request.id,decision});this.pending=undefined;this.permission.visible=false;
    this.status.content='Решение отправлено';this.prompt.content=this.busy?'Уточнение в текущий цикл · Enter — отправить':'Сообщение или /help';if(this.ready)this.editor.focus();this.renderer.requestRender();
  }
  private selectedText(){
    if(this.editor.focused&&this.editor.hasSelection())return this.editor.getSelectedText();
    return this.renderer.getSelection()?.getSelectedText() || this.editor.getSelectedText() || '';
  }
  private lastAnswer(){
    if(this.active)return this.streamingText;
    return [...this.answers].reverse().find(answer=>answer.source.trim())?.source||'';
  }
  private async copyText(text:string){
    if(this.dialog?.secret)return;
    let message='Нет текста для копирования';
    if(text){
      try{
        let status:'written'|'attempted'|'unsupported';
        if(this.writeClipboard)status=await this.writeClipboard(text);
        else{
          this.clipboard??=createClipboard({host:createHostClipboard({timeoutMs:1500,maxWriteBytes:1048576}),terminal:createRendererClipboardAdapter(this.renderer)});
          const result=await this.clipboard.writeText(text,{destination:'best-available'});
          status=result.host.status==='written'?'written':result.terminal.status==='attempted'?'attempted':'unsupported';
        }
        message=status==='written'?'Скопировано':status==='attempted'?'Копирование отправлено в терминал (OSC 52)':'Буфер недоступен · используйте Shift + выделение мышью';
      }catch{message='Не удалось скопировать · используйте Shift + выделение мышью';}
    }
    if(this.disposed)return;
    this.clipboardMessage=message;this.clipboardUntil=Date.now()+3000;
    if(this.busy)this.animateActivity();else this.status.content=message;
    this.renderer.requestRender();
  }
  private switchAgent(mode:'build'|'plan'){
    if(!this.ready||this.closed||this.dialog||this.pending)return;
    this.nextAgentMode=mode;this.send({type:'set_agent',mode});this.updateHeader();this.metrics();this.updateMenu();
  }
  private onKey(event:KeyEvent){
    const stop=()=>{event.preventDefault();event.stopPropagation();};
    if(event.ctrl && event.name==='c' && (event.shift||!this.dialog?.secret&&this.selectedText())){stop();void this.copyText(this.selectedText()||((this.editor.focused&&this.editor.visible)?this.editor.plainText:'')||this.lastAnswer());return;}
    if(event.ctrl && event.name==='y'){stop();void this.copyText(this.lastAnswer());return;}
    if(event.ctrl && event.name==='d'){stop();this.exit();return;}
    if(event.ctrl && event.name==='c'){
      if(this.paletteOpen && !this.busy && !this.pending && !this.dialog){stop();this.closePalette();return;}
      if(this.picker && !this.busy && !this.pending && !this.dialog){stop();this.closePicker();this.resetNavigation();return;}
      stop();if(this.busy||this.pending||this.dialog)this.cancelInput();else this.exit();return;
    }
    if(!this.ready && !this.closed && !this.dialog && !this.pending){
      const command=event.ctrl&&event.name==='p'?'/help':menuShortcuts[event.name]?.command;
      if(command){stop();this.queuedMenu=command;this.status.content='Открою после запуска: '+command;return;}
    }
    if(this.pending){
      stop();
      if(['up','down','left','right','tab'].includes(event.name)){
        this.permission.move(event.name==='up'||event.name==='left'||event.name==='tab'&&event.shift?-1:1);return;
      }
      if(event.name==='pageup'||event.name==='pagedown'){this.permission.scroll(event.name==='pageup'?-1:1);return;}
      if(event.name==='return'){this.permission.confirm();return;}
      const decision=({escape:'deny',y:'allow',n:'deny',a:'always'} as Record<string,Decision>)[event.name.toLowerCase()];
      if(decision)this.answerPermission(decision);return;
    }
    if(this.dialog && event.name==='escape'){stop();if(this.dialog.question){this.send({type:'answer',id:this.dialog.id,value:null,cancelled:true});this.finishQuestion();}else this.cancelInput();return;}
    if(this.dialog?.choices){
      if(['up','down','left','right','tab'].includes(event.name)){stop();event.name==='up'||event.name==='left'||event.name==='tab'&&event.shift?this.menuList.moveUp():this.menuList.moveDown();return;}
      if(event.name==='return'){stop();this.submit();return;}
      stop();return;
    }
    if(this.dialog?.secret){
      stop();if(event.name==='return'){this.submit();return;}
      if(event.name==='backspace'){this.secret=[...this.secret].slice(0,-1).join('');}
      else if(!event.ctrl && !event.meta && !event.sequence.startsWith('\x1b'))this.secret+=safeText(event.sequence).replace(/\n/g,'');
      this.mask.content='•'.repeat(Math.min(60,[...this.secret].length));return;
    }
    if(this.picker && !this.dialog && !this.pending && this.ready && !this.closed && !this.busy && !event.ctrl && !event.meta && !event.shift){
      if(event.name==='up'||event.name==='down'||event.name==='pageup'||event.name==='pagedown'){
        stop();if(event.name==='up'||event.name==='pageup')this.menuList.moveUp(event.name==='pageup'?5:1);else this.menuList.moveDown(event.name==='pagedown'?5:1);this.menuDescription();return;
      }
      if(event.name==='escape'){stop();this.backPicker();return;}
      if(event.name==='return'){stop();this.choosePicker();return;}
      if(event.name==='tab'){stop();this.menuList.moveGroup(event.shift?-1:1);this.menuDescription();return;}
    }
    if(this.menu.visible && !event.ctrl && !event.meta && !event.shift){
      if(event.name==='up'||event.name==='down'||event.name==='pageup'||event.name==='pagedown'){stop();if(event.name==='up'||event.name==='pageup')this.menuList.moveUp(event.name==='pageup'?5:1);else this.menuList.moveDown(event.name==='pagedown'?5:1);this.menuDescription();return;}
      if(event.name==='escape'){stop();if(this.paletteOpen)this.closePalette();else{this.menuDismissed=this.editor.plainText;this.menu.visible=false;}return;}
      if((event.name==='return'||event.name==='tab') && this.menuItems.length){stop();this.chooseMenu(event.name==='tab');return;}
    }
    if(!this.dialog&&!this.pending&&!this.picker&&!this.menu.visible&&this.ready&&!this.closed&&this.editor.focused&&!event.ctrl&&!event.meta&&!event.shift&&(event.name==='up'||event.name==='down')){
      if(this.history.move(event.name==='up'?'previous':'next',this.sessionInfo?.id,this.editor.plainText)){stop();return;}
    }
    if(event.name==='escape'&&!event.ctrl&&!event.meta&&this.busy&&!this.dialog&&!this.pending){
      stop();this.cancelInput();return;
    }
    if(!this.dialog && !this.pending && this.ready && !this.busy && !this.closed){
      if(event.ctrl && event.name==='p'){stop();if(this.paletteOpen)this.closePalette();else this.openPalette();return;}
      const command=menuShortcuts[event.name]?.command;
      if(command){stop();this.closePicker();this.closePalette();this.resetNavigation();this.shortcutDraft=this.editor.plainText;this.sendCommand(command);return;}
    }
    if(event.name==='tab'&&!event.ctrl&&!event.meta&&!this.dialog&&!this.picker&&!this.menu.visible&&this.ready&&!this.closed){
      stop();this.switchAgent((this.nextAgentMode||this.agentMode)==='build'?'plan':'build');return;
    }
    if(event.ctrl && event.name==='e'){stop();this.noticesExpanded=!this.noticesExpanded;for(const card of this.notices)card.detail.visible=this.noticesExpanded;this.renderer.requestRender();return;}
    if(event.ctrl && event.name==='t'){stop();this.thoughtsExpanded=!this.thoughtsExpanded;for(const card of this.thoughts){card.expanded=this.thoughtsExpanded;card.body.visible=card.expanded;this.updateThoughtTitle(card);}return;}
    if(event.ctrl && event.name==='r'){stop();this.raw=!this.raw;for(const answer of this.answers)answer.setRaw(this.raw);this.metrics();return;}
    if(event.ctrl && event.name==='o'){stop();this.expanded=!this.expanded;for(const card of this.tools.values()){card.expanded=this.expanded;card.details.visible=this.expanded;if(card.result)card.result.visible=this.expanded;}return;}
    if(event.name==='pageup'||event.name==='pagedown'){stop();this.log.scrollBy(event.name==='pageup'?-10:10);return;}
    if(!this.ready){stop();if(this.closed && event.name==='q')this.exit();}
  }
  private clearHistory(){
    if(this.clickTimer)clearTimeout(this.clickTimer);this.history.reset();
    this.finishAnswer();for(const entry of this.entries){this.log.remove(entry);entry.destroyRecursively();}
    this.steeringCards.clear();this.pendingSteering.clear();
    this.entries=[];this.answers=[];this.tools.clear();this.thoughts=[];this.thought=undefined;this.notices=[];this.active=undefined;this.last=undefined;this.highlights([]);
  }
  async dispose(){
    if(this.clickTimer)clearTimeout(this.clickTimer);
    this.disposed=true;if(this.timer)clearTimeout(this.timer);if(this.activityTimer)clearInterval(this.activityTimer);this.secret='';
    this.renderer.keyInput.off('keypress',this.keyHandler);this.renderer.keyInput.off('paste',this.pasteHandler);this.renderer.off('resize',this.resizeHandler);
    await this.clipboard?.dispose();
  }
}
