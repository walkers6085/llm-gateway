/** Понятный запрос разрешения: действие, точная команда и спокойный выбор ответа. */
import {BoxRenderable,ScrollBoxRenderable,TextRenderable,type CliRenderer} from '@opentui/core';
import {ChoiceList} from './choice.ts';
import {safeText} from './format.ts';
import {palette} from './theme.ts';

export type Decision='deny'|'allow'|'always';
type Request={id:string;name:string;arguments?:Record<string,unknown>;reason?:string;rule?:string;workspace?:string};

export function permissionPresentation(request:Request){
  const args=request.arguments||{};
  let title='Вызов инструмента',label='Параметры',content=JSON.stringify(args,null,2);
  const context:string[]=[];
  if(request.name==='shell'){
    title='Запуск команды';label='Команда';content=String(args.command??'');
    if(request.workspace)context.push('Рабочая папка: '+request.workspace);
    if(args.timeout!==undefined)context.push('Таймаут: '+args.timeout+' с');
  }else if(request.name==='write_file'){
    title='Запись файла';label='Новое содержимое';content=String(args.content??'');context.push('Файл: '+String(args.path??''));
  }else if(request.name==='edit_file'){
    title='Изменение файла';label='Замена';content='Было:\n'+String(args.old_string??'')+'\n\nСтанет:\n'+String(args.new_string??'');context.push('Файл: '+String(args.path??''));
  }else if(request.name==='subagent'){
    title='Запуск сабагента';label='Задача';content=String(args.task??'');
  }else if(['web_fetch','http_request'].includes(request.name)){
    title='Сетевой запрос';label='Запрос';content=String(args.method||'GET')+' '+String(args.url||'');
    if(args.headers&&typeof args.headers==='object')content+='\n'+Object.entries(args.headers).map(([key,value])=>key+': '+String(value)).join('\n');
    if('json' in args)content+='\n\n'+JSON.stringify(args.json,null,2);
    else if(args.body!==undefined)content+='\n\n'+String(args.body);
  }
  const reason=request.reason==='ask'||!request.reason?'Инструмент ожидает вашего подтверждения.':request.reason;
  return {title,label,content:safeText(content),context:safeText(context.join('\n')),reason:safeText(reason)};
}

export class ApprovalPanel extends BoxRenderable{
  readonly choices:ChoiceList;
  private summary:TextRenderable;private commandBox:BoxRenderable;private command:TextRenderable;
  private details:TextRenderable;private hint:TextRenderable;private commandScroll:ScrollBoxRenderable;
  private request?:Request;private content='';
  private options=[
    {name:'Не разрешать',description:'N / Esc · Пропустить действие и продолжить диалог',value:'deny'},
    {name:'Разрешить один раз',description:'Y · Выполнить только это действие',value:'allow'},
    {name:'Разрешать в этой сессии',description:'A · Запомнить правило до конца этой сессии',value:'always'},
  ];
  constructor(private renderer:CliRenderer,private decide:(value:Decision)=>void){
    super(renderer,{id:'permission-panel',width:'100%',flexShrink:0,flexDirection:'column',border:true,borderStyle:'rounded',
      borderColor:palette.border,backgroundColor:palette.panel,paddingX:1,visible:false,title:'Нужно ваше разрешение'});
    this.summary=new TextRenderable(renderer,{content:'',fg:palette.fg,wrapMode:'word',flexShrink:0});this.add(this.summary);
    this.commandBox=new BoxRenderable(renderer,{id:'permission-command',width:'100%',flexShrink:0,flexDirection:'column',border:true,borderStyle:'single',borderColor:palette.border,backgroundColor:palette.bg,title:'Команда'});
    this.commandScroll=new ScrollBoxRenderable(renderer,{width:'100%',height:'100%',scrollX:false,scrollY:true,contentOptions:{flexDirection:'column'}});
    this.command=new TextRenderable(renderer,{id:'permission-command-text',content:'',fg:palette.fg,wrapMode:'word',flexShrink:0,selectable:true});
    this.commandScroll.add(this.command);this.commandBox.add(this.commandScroll);this.add(this.commandBox);
    this.details=new TextRenderable(renderer,{content:'',fg:palette.muted,wrapMode:'word',flexShrink:0});this.add(this.details);
    this.choices=new ChoiceList(renderer,'permission-options',{quiet:true});
    this.choices.options=this.options;
    this.choices.on('activate',()=>this.confirm());this.choices.on('selectionChanged',()=>this.showDetails());this.add(this.choices);
    this.hint=new TextRenderable(renderer,{height:1,content:'↑/↓ или ←/→ · Enter — выбрать · Esc — отказать · PgUp/PgDn — текст',fg:palette.muted,wrapMode:'none',flexShrink:0});this.add(this.hint);
  }
  open(request:Request){
    this.request=request;
    const view=permissionPresentation(request);this.content=view.content;
    this.summary.content=view.title+' · '+safeText(request.name);
    this.commandBox.title=view.label;this.command.content=view.content;
    this.visible=true;this.choices.setSelectedIndex(0);this.commandScroll.scrollTo(0);this.resizePanel();this.showDetails();
  }
  resizePanel(){
    const narrow=this.renderer.height<26;
    const width=Math.max(15,this.renderer.width-8);
    const lines=this.content.split('\n').reduce((sum,line)=>sum+Math.max(1,Math.ceil([...line].length/width)),0);
    this.commandBox.height=Math.min(narrow?4:8,Math.max(3,lines+2));
    this.choices.options=this.options.map(item=>narrow?{...item,description:undefined}:item);
    this.choices.height=narrow?3:6;
    this.details.height=narrow?2:3;
    this.showDetails();
    this.renderer.requestRender();
  }
  private showDetails(){
    if(!this.request)return;
    const view=permissionPresentation(this.request);
    const selected=this.choices.getSelectedOption()?.value;
    this.details.content=[selected==='always'?'Правило этой сессии: '+safeText(this.request.rule||this.request.name):'Причина: '+view.reason,view.context].filter(Boolean).join('\n');
    this.renderer.requestRender();
  }
  move(direction:number){direction<0?this.choices.moveUp():this.choices.moveDown();}
  scroll(direction:number){this.commandScroll.scrollBy(direction*4);}
  confirm(){const value=this.choices.getSelectedOption()?.value as Decision|undefined;if(this.visible&&value)this.decide(value);}
}
