"""Связывание фактов, отвлекающие записи и вопрос без ответа по сетке длин."""

import random

from bench.runner import Bench
from bench.textgen import haystack, insert


class Retrieval(Bench):
    name, title, quality = 'retrieval', 'Полезное окно: связанные факты и отсутствие ответа', True
    defaults = {'lengths': [1024, 4096, 16384], 'repeats': 3}
    quick = {'lengths': [1024], 'repeats': 1}
    schema = {'lengths': {'type': 'array', 'items': {'type': 'integer', 'minimum': 128}},
              'repeats': {'type': 'integer', 'minimum': 1}}

    async def run(self, ctx):
        rng, rows = random.Random(ctx.config['seed']), []
        cfg = ctx.cfg(self.name)
        cpt = await ctx.chars_per_token()
        for length in cfg['lengths']:
            for _ in range(cfg['repeats']):
                code, other = f'{rng.getrandbits(64):016x}', f'{rng.getrandbits(64):016x}'
                project = f'Альтаир-{rng.randint(1000,9999)}'
                for missing in (False, True):
                    facts = [(0.05, f'В архиве проект {project} связан с записью R-17.'),
                             (.5, f'В архиве запись R-71 имеет код {other}.')]
                    if not missing:
                        facts.append((.95, f'В архиве запись R-17 имеет код {code}.'))
                    expected = 'НЕТ ДАННЫХ' if missing else code
                    text = insert(haystack(int(length*cpt), rng.randrange(2**32)), facts)
                    prompt = text + f'\nНазови код записи, связанной с проектом {project}. Ответь только кодом; если данных нет — НЕТ ДАННЫХ.'
                    r = await ctx.client.complete(ctx.model, [{'role': 'user', 'content': prompt}], max_tokens=96)
                    rows.append({'length': length, 'missing': missing, 'expected': expected, 'answer': r.text,
                                 'passed': r.ok and r.text.strip() == expected,
                                 'infrastructure_error': not r.ok, 'error': r.error})
        per_length = {str(n): sum(r['passed'] for r in rows if r['length']==n) / sum(r['length']==n for r in rows) for n in cfg['lengths']}
        accurate = sum(r['passed'] for r in rows) / len(rows)
        return self.result(score=accurate*100, metrics={'accuracy': accurate, 'cases': len(rows),
            'per_length': per_length, 'useful_context_tokens': max((int(n) for n, a in per_length.items() if a>=.8), default=0)},
            details=rows, infrastructure_errors=sum(r['infrastructure_error'] for r in rows),
            notes=['Полезное окно — наибольшая проверенная оценка длины с точностью >=80%; это не паспортный лимит.',
                   'Для вывода об устойчивости увеличьте repeats и сравните результаты нескольких seed.'])
