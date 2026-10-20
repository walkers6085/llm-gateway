/** Реестр окон: новый список добавляется описанием, без новой клавиатуры и виджетов. */
import {badgeForTag,type Badge} from './theme.ts';

export const menuSpecs={
  revert:{title:'Изменить прошлое сообщение',search:'Поиск по вашим сообщениям',empty:'Нет сообщений для изменения.',action:'выбрать'},
  model:{title:'Выбор модели · все провайдеры',search:'Поиск по провайдеру, модели и тегам',empty:'Провайдер не вернул доступных моделей. Выбор модели не изменён.',action:'переключить'},
  provider:{title:'Выбор провайдера',search:'Поиск провайдера',empty:'Нет доступных провайдеров.',action:'переключить'},
  session:{title:'Выбор сессии',search:'Поиск по названию, модели, папке или ID',empty:'Сохранённых сессий пока нет.',action:'продолжить'},
  'session-delete':{title:'Удаление сессии',search:'Поиск сессии для удаления',empty:'Нет сессий для удаления.',action:'выбрать'},
  tool:{title:'Инструменты',search:'Поиск инструмента или описания',empty:'Инструменты отключены.',action:'параметры'},
  mcp:{title:'MCP-серверы',search:'Поиск сервера или инструмента',empty:'MCP-серверы не настроены.',action:'подробнее'},
};
export const menuShortcuts:Record<string,{command:string;label:string}>={f1:{command:'/help',label:'команды'},f2:{command:'/models',label:'модели'},f3:{command:'/provider',label:'пров.'},f4:{command:'/sessions',label:'сессии'}};
export type MenuKind=keyof typeof menuSpecs;
export type PickerChoice={key:string;name:string;label:string;description?:string;group?:string;badges?:Badge[];disabled?:boolean;model?:string;provider?:string|null;target?:string;command?:string};
export type PickerState={choices:PickerChoice[];kind:MenuKind;current:string;currentProvider?:string|null;currentLabel:string;sessionId:string;draft:string;query?:string};

export function createPicker(event:any,draft:string):PickerState{
  const selection=event.selection,kind=selection.kind as MenuKind;
  const entries=Array.isArray(selection.choices)?selection.choices:undefined;
  const choices:PickerChoice[]=entries?entries.map((item:any):PickerChoice=>{
    if(kind==='provider'&&item.command)return {key:item.command,command:item.command,name:item.label,label:item.label+' '+(item.description||''),description:item.description,group:item.group||'Управление'};
    if(kind==='model'){
      const tags=(item.tags||[]).map(String);
      return {key:JSON.stringify([item.provider,item.model]),provider:item.provider,model:item.model,name:item.display_name||item.model,group:item.provider_name,
        label:`${item.provider_name} / ${item.display_name||""} ${item.model} ${tags.join(' ')} ${item.available?'':'API недоступен'}`,
        description:(item.display_name&&item.display_name!==item.model?item.model+' · ':'')+(item.available?'': 'Из конфига · API недоступен'),badges:[...tags.map(badgeForTag),...(item.available?[]:[{text:'API недоступен',tone:'warning' as const}])]};
    }
    if(kind==='provider')return {key:JSON.stringify([item.provider]),provider:item.provider,name:item.label,label:`${item.label} / ${item.model||''}`,
      description:item.model?'Модель: '+item.model:'Модель не выбрана',group:'Подключения',disabled:item.enabled===false,badges:[]};
    if(kind==='revert')return {key:item.key,target:item.target,name:item.label.split('\n')[0].slice(0,140)||'(пустое сообщение)',label:item.label,description:item.description,group:item.group};
    if(kind==='session'||kind==='session-delete'){
      const updated=typeof item.updated==='number'?new Date(item.updated*1000).toLocaleString('ru-RU',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
      return {key:item.id,target:item.id,name:item.title||'(без названия)',label:`${item.title} ${item.model} ${item.provider} ${item.workspace} ${item.id}`,
        description:`${item.model||'без модели'} · ${updated} · ${item.workspace} · ${item.id}`,group:item.enabled?'Этот проект':'Другие / недоступные',disabled:!item.enabled,
        badges:[{text:item.provider||'gateway',tone:'info'}]};
    }
    return {key:item.key,name:item.label,label:`${item.label} ${item.description||''}`,description:item.description,group:item.group||(kind==='mcp'?'Серверы':'Инструменты'),
      command:item.command,badges:(item.tags||[]).map((tag:string)=>({text:tag,tone:tag==='ошибка'?'error':tag==='ask'?'warning':tag==='allow'||tag==='OK'?'success':'info'}))};
  }):[...new Set<string>((event.rows||[]).map((row:any[])=>row[0]).filter((value:unknown)=>typeof value==='string' && value.length>0))]
    .map(model=>({key:model,name:model,label:model,model}));
  if(choices.some(choice=>choice.group)){
    const groups=new Map<string,PickerChoice[]>();
    for(const choice of choices){const key=choice.group||'';if(!groups.has(key))groups.set(key,[]);groups.get(key)!.push(choice);}
    const ordered=[...groups.values()];
    if(kind==='session'||kind==='session-delete')ordered.sort((a,b)=>Number(!!a[0]?.disabled)-Number(!!b[0]?.disabled));
    choices.splice(0,choices.length,...ordered.flatMap(group=>kind==='model'?group.sort((a,b)=>Number(!!b.badges?.some(tag=>tag.text.toLowerCase()==='free'))-Number(!!a.badges?.some(tag=>tag.text.toLowerCase()==='free'))):group));
  }
  const current=entries?(kind==='provider'?JSON.stringify([selection.provider]):kind==='model'?JSON.stringify([selection.provider,selection.current]):selection.current||''):selection.current||'';
  return {choices,kind,current,currentProvider:selection.provider,currentLabel:kind==='provider'?(selection.provider||'gateway (настройки)'):(selection.current||'не выбрана'),sessionId:selection.session_id,draft};
}
