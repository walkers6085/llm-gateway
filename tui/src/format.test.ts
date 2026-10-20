import {test,expect} from 'bun:test';
import {analyseResponse,jsonTable,safeMarkdown,safeText} from './format.ts';

test('Markdown: явные пометки, таблицы, код без ложных выделений',()=>{
  const result=analyseResponse('**Важно:** Сохраните ключ.\n\n> [!WARNING]\n> Лимит близко\n\n| Модель | Балл |\n|---|---|\n| альфа | 42 |\n\n```text\nОшибка: это пример\n```');
  expect(result.highlights.map(h=>h.kind)).toEqual(['important','warning']);
  expect(result.tableCount).toBe(1);
  expect(result.blocks[0]?.type).toBe('markdown');
});
test('JSON: объединение столбцов, пропуски, вложенные значения и исходник',()=>{
  const source='[{"имя":"альфа","score":42},{"имя":"бета","extra":{"ok":true}}]';
  const result=analyseResponse(source);
  expect(result.source).toBe(source);
  expect(result.blocks[0]).toEqual({type:'table',table:{columns:['имя','score','extra'],rows:[['альфа','42','—'],['бета','—','{"ok":true}']],omittedRows:0,omittedColumns:0}});
  expect(analyseResponse('Тест\n\n```json\n{"балл":42}\n```').tableCount).toBe(1);
});
test('пределы таблицы обозначены явно; незавершённый JSON остаётся текстом',()=>{
  const table=jsonTable(Array.from({length:105},()=>Object.fromEntries(Array.from({length:15},(_,i)=>['k'+i,i]))))!;
  expect(table.rows).toHaveLength(100);expect(table.columns).toHaveLength(12);
  expect(table.omittedRows).toBe(5);expect(table.omittedColumns).toBe(3);
  expect(analyseResponse('[{"x":').tableCount).toBe(0);
  expect(jsonTable([])).toBeNull();
  expect(analyseResponse('Обычный ответ без пометок.').highlights).toEqual([]);
});
test('управляющие коды удалены, ссылки ограничены, код остаётся буквальным',()=>{
  expect(safeText('\x1b[31mкрасный\x1b[0m\x00')).toBe('красный');
  expect(safeMarkdown('[опасно](file:///etc/passwd)')).not.toContain('](file:');
  expect(safeMarkdown('[сайт](https://example.com)')).toContain('](https:');
  const source='`[пример](file:///tmp)`\n\n```sh\n[пример](file:///tmp)\n```\n';
  expect(safeMarkdown(source)).toBe(source);
  expect(analyseResponse('```text\nВажно: пример\n```').highlights).toEqual([]);
});
