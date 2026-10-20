/** Общие цвета, метки и подписи всех окон терминала. */
import {RGBA, StyledText, type TextChunk} from '@opentui/core';
import {safeText} from './format.ts';

export const palette={bg:'#10151f',panel:'#171f2c',fg:'#d9e2ef',muted:'#92a4ba',blue:'#79b8ff',green:'#7dd3a0',yellow:'#f2cc60',red:'#ff8585',border:'#34465c',system:'#192636',selected:'#243b55'};
export type Badge={text:string;tone?:'info'|'success'|'warning'|'error'|'muted'};
export const tones={info:palette.blue,success:palette.green,warning:palette.yellow,error:palette.red,muted:palette.muted};
export function chunk(text:string,fg=palette.fg,bg?:string):TextChunk{
  return {__isChunk:true,text:safeText(text),fg:RGBA.fromHex(fg),...(bg?{bg:RGBA.fromHex(bg)}:{})};
}
export function tagged(title:string,badges:Badge[]=[],selected=false):StyledText{
  return new StyledText([chunk(title,selected?palette.blue:palette.fg),...badges.flatMap(badge=>[
    chunk(' '),chunk(` ${badge.text} `,tones[badge.tone||'info'],badge.tone==='success'?'#173c30':badge.tone==='warning'?'#40351b':badge.tone==='error'?'#40242c':'#223247'),
  ])]);
}
export function badgeForTag(text:string):Badge{
  return {text,tone:text.toLowerCase()==='free'?'success':'info'};
}
