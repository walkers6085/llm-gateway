/** Курсор истории; устаревшие ответы не перезаписывают набранный текст. */
export class InputHistory {
  private cursor?:string;
  private pending?:string;
  private serial=0;
  private applying=false;
  private expected?:string;
  constructor(private send:(message:any)=>void,private put:(text:string)=>void){}
  changed(text:string){if(text!==this.expected)this.reset();}
  reset(){if(!this.applying){this.cursor=undefined;this.pending=undefined;this.expected=undefined;}}
  move(direction:'previous'|'next',session:string,text:string){
    if(this.pending)return true;
    if(!this.cursor&&(text!==''||direction==='next'))return false;
    const id=String(++this.serial);this.pending=id;this.expected=text;
    this.send({type:'input_history',id,session_id:session,cursor:this.cursor,direction});return true;
  }
  accept(event:any,session:string){
    if(event.id!==this.pending||event.session_id!==session)return false;
    this.pending=undefined;
    if(event.error)return true;
    if(event.boundary==='oldest')return true;
    this.cursor=event.message_id;
    this.expected=event.text??'';
    this.applying=true;try{this.put(this.expected!);}finally{this.applying=false;}
    return true;
  }
}
