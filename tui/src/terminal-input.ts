/** Собирает служебные ответы терминала до передачи парсеру OpenTUI. */
import {Transform,type TransformCallback} from 'node:stream';

// В SSH один OSC-ответ может прийти несколькими пакетами с паузой больше таймаута клавиш.
// Печатный текст, включая похожие на RGB строки, не фильтруется.
export class TerminalInput extends Transform {
  readonly isTTY:boolean;readonly fd:number;
  private state:'normal'|'escape'|'csi'|'ss3'|'control'|'paste'='normal';
  private pending:number[]=[];private pasteEnd:number[]=[];private controlEscape=false;
  private timer?:ReturnType<typeof setTimeout>;private overflow=false;
  constructor(private source?:NodeJS.ReadStream,private responseTimeoutMs=3000){
    super();this.isTTY=source?.isTTY??false;this.fd=(source as (NodeJS.ReadStream & {fd?:number})|undefined)?.fd??0;
  }
  setRawMode(value:boolean){this.source?.setRawMode?.(value);return this;}
  private arm(ms:number){
    if(this.timer)clearTimeout(this.timer);
    this.timer=setTimeout(()=>{
      this.timer=undefined;
      if(this.state==='escape'||this.state==='csi'||this.state==='ss3')this.push(Buffer.from(this.pending));
      // Поздний хвост OSC/DCS тоже отбрасываем до терминатора или Ctrl+C/Ctrl+D.
      if(this.state==='control'){this.pending=[];this.overflow=true;return;}
      this.pending=[];this.state='normal';this.controlEscape=false;this.overflow=false;
    },ms);this.timer.unref?.();
  }
  private reset(){
    if(this.timer)clearTimeout(this.timer);this.timer=undefined;
    this.state='normal';this.pending=[];this.controlEscape=false;this.overflow=false;
  }
  _transform(chunk:Buffer,_encoding:BufferEncoding,done:TransformCallback){
    const output:number[]=[];
    for(const byte of chunk){
      if(this.state==='paste'){
        output.push(byte);this.pasteEnd.push(byte);if(this.pasteEnd.length>6)this.pasteEnd.shift();
        if(Buffer.from(this.pasteEnd).equals(Buffer.from('\x1b[201~'))){this.state='normal';this.pasteEnd=[];}
        continue;
      }
      if(this.state==='normal'){
        if(byte===27){this.state='escape';this.pending=[byte];this.arm(35);}else output.push(byte);
        continue;
      }
      if(this.state==='escape'){
        this.pending.push(byte);
        if(byte===93||byte===80||byte===95){this.state='control';this.arm(this.responseTimeoutMs);}
        else if(byte===91){this.state='csi';this.arm(250);}
        else if(byte===79){this.state='ss3';this.arm(250);}
        else{for(const value of this.pending)output.push(value);this.reset();}
        continue;
      }
      if(this.state==='ss3'){this.pending.push(byte);for(const value of this.pending)output.push(value);this.reset();continue;}
      if(this.state==='csi'){
        this.pending.push(byte);
        if(byte>=64&&byte<=126){
          const paste=Buffer.from(this.pending).equals(Buffer.from('\x1b[200~'));
          for(const value of this.pending)output.push(value);this.reset();if(paste)this.state='paste';
        }else if(this.pending.length>128){for(const value of this.pending)output.push(value);this.reset();}
        continue;
      }
      if(this.overflow&&(byte===3||byte===4||byte===13)){this.reset();output.push(byte);continue;}
      if(!this.overflow){this.pending.push(byte);if(this.pending.length>65536){this.pending=[];this.overflow=true;}}
      const ended=byte===7||this.controlEscape&&byte===92;
      this.controlEscape=byte===27;
      if(ended){if(!this.overflow)for(const value of this.pending)output.push(value);this.reset();}
    }
    if(output.length)this.push(Buffer.from(output));done();
  }
  _destroy(error:Error|null,done:(error?:Error|null)=>void){this.reset();done(error);}
}

export function terminalInput(source:NodeJS.ReadStream){
  const input=new TerminalInput(source);
  source.pipe(input);
  return {input:input as unknown as NodeJS.ReadStream,dispose:()=>{source.unpipe(input);input.destroy();source.pause();}};
}
