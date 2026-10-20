"""Ограниченный парсер data-событий SSE с CRLF, многострочными данными и хвостом EOF."""


async def data_events(response, limit=1048576):
    tail, data, size = b'', [], 0
    async for chunk in response.aiter_bytes():
        tail += chunk
        while b'\n' in tail:
            line, tail = tail.split(b'\n', 1)
            line = line.rstrip(b'\r')
            if len(line) + size > limit:
                raise ValueError('слишком большое SSE-событие')
            if line.startswith(b'data:'):
                value = line[5:].lstrip(b' ')
                data.append(value)
                size += len(value)
            elif not line and data:
                yield b'\n'.join(data).decode('utf-8')
                data, size = [], 0
        if len(tail) + size > limit:
            raise ValueError('слишком большое SSE-событие')
    if tail.startswith(b'data:'):
        data.append(tail[5:].strip())
    if data:
        yield b'\n'.join(data).decode('utf-8')
