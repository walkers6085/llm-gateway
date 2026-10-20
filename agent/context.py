"""Консервативная очистка копии результатов инструментов перед отправкой модели."""

import copy
import json
import re

# Полные OSC/DCS/APC/PM и CSI; печатные экранированные строки вроде \\x1b не затрагиваются.
CONTROL = re.compile(
    r'(?:\x1b\]|\x9d)[\s\S]*?(?:\x07|\x1b\\|\x9c|$)'
    r'|(?:\x1b[P_^]|\x90|\x9e|\x9f)[\s\S]*?(?:\x1b\\|\x9c|$)'
    r'|(?:\x1b\[|\x9b)[0-?]*[ -/]*(?:[@-~]|$)'
    r'|\x1b[@-Z\\-_]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')
CODE_TOOLS = {'read_file', 'edit_file', 'write_file'}


def clean_text(text, config, *, code=False):
    if code:
        return text
    text = CONTROL.sub('', text).replace('\r\n', '\n').replace('\r', '\n')
    if config['compact_json']:
        try:
            value = json.loads(text, parse_float=str, parse_int=str)
            # Строки и числа сами по себе не являются структурированным выводом.
            if isinstance(value, (dict, list)):
                # Убираем только пробелы снаружи строк: числа, порядок и дубликаты ключей остаются побайтно.
                output=[];quoted=False;escape=False
                for char in text:
                    if quoted:
                        output.append(char)
                        if escape:escape=False
                        elif char=='\\':escape=True
                        elif char=='"':quoted=False
                    elif char=='"':quoted=True;output.append(char)
                    elif not char.isspace():output.append(char)
                return ''.join(output)
        except (ValueError, RecursionError):
            pass
    if config['collapse_blank_lines'] and '```' not in text and '~~~' not in text:
        text = re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', text)
    if config['deduplicate_lines'] and '```' not in text and '~~~' not in text:
        lines = text.split('\n');result=[];index=0
        while index < len(lines):
            end=index+1
            while end < len(lines) and lines[end] == lines[index]:end+=1
            count=end-index
            if count>=4 and lines[index].strip():
                result.extend((lines[index], f'[Строка встречалась подряд {count} раз, показан один экземпляр]'))
            else:result.extend(lines[index:end])
            index=end
        text='\n'.join(result)
    return text


def clean_messages(messages, config):
    if not config['enabled']:
        return messages, {'before_bytes': 0, 'after_bytes': 0, 'changed_tools': 0}
    result=copy.deepcopy(messages);tools={};before=after=changed=0
    for message in result:
        for call in message.get('tool_calls') or []:
            tools[call['id']]=call.get('function',{}).get('name','')
        if message.get('role') != 'tool' or not isinstance(message.get('content'),str):
            continue
        original=message['content']
        cleaned=clean_text(original,config,code=tools.get(message.get('tool_call_id')) in CODE_TOOLS)
        before+=len(original.encode());after+=len(cleaned.encode());changed+=cleaned!=original
        message['content']=cleaned
    return result, {'before_bytes':before,'after_bytes':after,'changed_tools':changed}
