/** Разбор только для представления: исходные сообщения и история агента не меняются. */
import { marked, type Token } from 'marked';

export type Highlight = { kind: 'important' | 'warning' | 'error' | 'success'; text: string };
export type Table = { columns: string[]; rows: string[][]; omittedRows: number; omittedColumns: number };
export type Block = { type: 'markdown'; text: string } | { type: 'table'; table: Table };
export type Analysis = { source: string; blocks: Block[]; highlights: Highlight[]; tableCount: number };

export function safeText(value: unknown): string {
  return String(value ?? '')
    .replace(/\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))/g, '')
    .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, '');
}

function safeURL(value: string): boolean {
  try { return ['http:', 'https:'].includes(new URL(value.replace(/^<|>$/g, '')).protocol); }
  catch { return false; }
}

function safeLinks(value: string): string {
  return value
    .replace(/\[([^\]\n]*)\]\(([^)\n]*)\)/g, (all, label: string, href: string) =>
      safeURL(href.trim().split(/\s+/)[0]!) ? all : `${label} (${href})`)
    .replace(/^([ \t]*)\[([^\]\n]+)\]:\s*(\S+)(.*)$/gm,
      (all, indent: string, label: string, href: string) => safeURL(href) ? all : `${indent}${label}: ${href}`)
    .replace(/<([a-z][a-z0-9+.-]*:[^<>\s]+)>/gi, (all, href: string) => safeURL(href) ? all : `(${href})`);
}


export function safeMarkdown(value: string): string {
  // Фрагменты кода сохраняются буквально: ссылки внутри них не активны.
  return marked.lexer(safeText(value), {gfm:true}).map(token => {
    if(token.type==='code')return token.raw;
    return token.raw.split(/(`+[^`\n]*`+)/g).map((part,index)=>index%2?part:safeLinks(part)).join('');
  }).join('');
}

function cell(value: unknown): string {
  const text = typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value ?? '—');
  const clean=safeText(text);
  return clean.length>2000?clean.slice(0,1999)+'…':clean;
}

export function jsonTable(value: unknown): Table | null {
  if (Array.isArray(value) && value.length > 0) {
    if (value.every(row => row !== null && typeof row === 'object' && !Array.isArray(row))) {
      const allColumns = [...new Set(value.flatMap(row => Object.keys(row)))];
      if (!allColumns.length) return null;
      const columns = allColumns.slice(0, 12);
      return { columns, rows: value.slice(0, 100).map(row => columns.map(key => cell(row[key]))),
        omittedRows: Math.max(0, value.length - 100), omittedColumns: Math.max(0, allColumns.length - 12) };
    }
    if (value.every(row => row === null || typeof row !== 'object'))
      return { columns: ['Значение'], rows: value.slice(0, 100).map(row => [cell(row)]), omittedRows: Math.max(0, value.length - 100), omittedColumns: 0 };
  }
  if (value !== null && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length) {
    const entries = Object.entries(value);
    return { columns: ['Поле', 'Значение'], rows: entries.slice(0, 100).map(([key,val]) => [safeText(key),cell(val)]),
      omittedRows: Math.max(0, entries.length - 100), omittedColumns: 0 };
  }
  return null;
}

function parsedTable(text: string): Table | null {
  if (text.length > 262144) return null;
  try { return jsonTable(JSON.parse(text)); } catch { return null; }
}

function plain(value: string): string {
  return value.replace(/\*\*|__|`/g, '').replace(/^\s*>\s?/gm, '').trim();
}

function highlight(value: string): Highlight | null {
  const text = plain(value);
  const match = text.match(/^(?:\[!(IMPORTANT|WARNING|CAUTION|NOTE|TIP)\]\s*|(?:Важно|Important|Внимание|Предупреждение|Warning|Осторожно|Ошибка|Error|Итог|Результат|Готово|Успех|Summary|Result|Success)\s*[:!—–-]\s*)/i);
  if (!match) return null;
  const prefix = match[0].toLowerCase();
  const kind = /ошибка|error|caution/.test(prefix) ? 'error'
    : /warning|предупреждение|внимание|осторожно/.test(prefix) ? 'warning'
    : /итог|результат|готово|успех|summary|result|success|tip/.test(prefix) ? 'success' : 'important';
  return { kind, text: text.slice(match[0].length).trim().slice(0, 400) || text.slice(0, 400) };
}

export function analyseResponse(value: string): Analysis {
  const source = safeText(value);
  const whole = parsedTable(source.trim());
  if (whole) return { source, blocks: [{type:'table',table:whole}], highlights: [], tableCount: 1 };
  const blocks: Block[] = [], highlights: Highlight[] = [];
  let tableCount = 0;
  const tokens = marked.lexer(source, {gfm:true});
  function note(token: Token): void {
    if (token.type === 'code' || token.type === 'codespan') return;
    if (token.type === 'table') { tableCount++; return; }
    if (token.type === 'paragraph' || token.type === 'blockquote' || token.type === 'heading') {
      const found = highlight(token.type === 'heading' ? token.text + ':' : token.text);
      if (found && !highlights.some(h => h.kind === found.kind && h.text === found.text)) highlights.push(found);
    }
    if (token.type === 'list') for (const item of token.items) for (const child of item.tokens) note(child);
  }
  for (const token of tokens) {
    note(token);
    const table = token.type === 'code' && /^json\b/i.test(token.lang || '') ? parsedTable(token.text) : null;
    if (table) { blocks.push({type:'table',table}); tableCount++; }
    else {
      const previous = blocks.at(-1);
      const text = safeMarkdown(token.raw);
      if (previous?.type === 'markdown') previous.text += text;
      else blocks.push({type:'markdown',text});
    }
  }
  return {source,blocks,highlights:highlights.slice(0,12),tableCount};
}
