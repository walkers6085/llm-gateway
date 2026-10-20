/** Общий список выбора: группы, цветные метки, мышь и клавиатура без отдельного фокуса. */
import {BoxRenderable,ScrollBoxRenderable,TextRenderable,StyledText,type CliRenderer} from '@opentui/core';
import {palette,tagged,chunk,type Badge} from './theme.ts';
import {safeText} from './format.ts';

export type ChoiceOption={name:string;description?:string;group?:string;badges?:Badge[];disabled?:boolean;current?:boolean;value:any};

export class ChoiceList extends BoxRenderable{
  private scroll:ScrollBoxRenderable;
  private items:ChoiceOption[]=[];
  private selected=0;
  private fingerprint='';
  private rows:{box:BoxRenderable;text:TextRenderable}[]=[];
  private scrollPending=false;
  private count=0;
  constructor(private renderer:CliRenderer,id:string,private appearance:{quiet?:boolean}={}){
    super(renderer,{id,width:'100%',height:6,flexShrink:0,flexDirection:'column',renderBefore:()=>{
      if(this.scrollPending){const row=this.rows[this.selected];if(row)this.scroll.scrollChildIntoView(row.box.id);this.scrollPending=false;}
    }});
    this.scroll=new ScrollBoxRenderable(renderer,{id:id+'-scroll',width:'100%',height:'100%',scrollX:false,scrollY:true,contentOptions:{flexDirection:'column'}});
    this.add(this.scroll);
  }
  get options(){return this.items;}
  set options(items:ChoiceOption[]){
    this.items=items;
    const signature=JSON.stringify(items.map(({value,...view})=>view));
    if(signature===this.fingerprint)return;
    this.fingerprint=signature;
    for(const child of this.scroll.getChildren()){this.scroll.remove(child);child.destroyRecursively();}
    this.rows=[];let group:string|undefined;
    const counts=new Map<string,number>();for(const item of items)if(item.group)counts.set(item.group,(counts.get(item.group)||0)+1);
    items.forEach((item,index)=>{
      if(item.group && item.group!==group){
        group=item.group;
        this.scroll.add(new TextRenderable(this.renderer,{height:1,flexShrink:0,content:`─ ${safeText(group)} · ${counts.get(group)} ─`,fg:palette.muted,bg:palette.bg}));
      }
      const box=new BoxRenderable(this.renderer,{id:`${this.id}-row-${++this.count}`,height:item.description?2:1,width:'100%',flexShrink:0,flexDirection:'column',paddingX:1,onMouseDown:event=>{
        if(event.button!==0||item.disabled)return;event.preventDefault();this.setSelectedIndex(index);this.emit('activate');
      }});
      const text=new TextRenderable(this.renderer,{height:1,width:'100%',content:'',wrapMode:'none'});box.add(text);
      if(item.description)box.add(new TextRenderable(this.renderer,{height:1,content:'  '+safeText(item.description).replace(/\s+/g,' '),fg:palette.muted,wrapMode:'none'}));
      this.scroll.add(box);this.rows.push({box,text});
    });
    this.paint();this.scrollPending=true;this.requestRender();
  }
  getSelectedIndex(){return this.selected;}
  getSelectedOption(){const item=this.items[this.selected];return item && !item.disabled?item:null;}
  setSelectedIndex(index:number){
    const count=this.items.length;let next=Math.max(0,Math.min(index,count-1));
    if(count && this.items[next]?.disabled){const enabled=this.items.findIndex(item=>!item.disabled);next=enabled<0?0:enabled;}
    this.selected=next;this.paint();this.scrollPending=true;this.emit('selectionChanged');this.requestRender();
  }
  moveUp(steps=1){this.move(-1,steps);}
  moveDown(steps=1){this.move(1,steps);}
  moveGroup(direction=1){
    if(!this.items.length)return;
    const group=this.items[this.selected]?.group;
    for(let step=1;step<=this.items.length;step++){
      const index=(this.selected+direction*step+this.items.length*2)%this.items.length;
      if(!this.items[index].disabled && this.items[index].group!==group){this.setSelectedIndex(index);return;}
    }
  }
  private move(direction:number,steps:number){
    if(!this.items.length)return;
    let index=this.selected;
    for(let move=0;move<steps;move++)for(let tries=0;tries<this.items.length;tries++){
      index=(index+direction+this.items.length)%this.items.length;if(!this.items[index].disabled)break;
    }
    this.setSelectedIndex(index);
  }
  private paint(){
    this.rows.forEach((row,index)=>{
      const item=this.items[index];if(!item)return;
      row.box.backgroundColor=this.appearance.quiet?(index===this.selected?'#28313d':palette.panel):(index===this.selected?palette.selected:palette.panel);
      const badges=[...(item.badges||[]),...(item.current?[{text:'текущая',tone:'success' as const}]:[]),...(item.disabled?[{text:'недоступно',tone:'muted' as const}]:[])];
      row.text.content=this.appearance.quiet?new StyledText([chunk(`${index===this.selected?'›':' '} ${item.name}`,index===this.selected?palette.fg:palette.muted)]):tagged(`${index===this.selected?'›':' '} ${item.name}`,badges,index===this.selected);
      row.box.opacity=item.disabled?0.6:1;
    });
  }
}
