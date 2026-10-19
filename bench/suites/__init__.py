"""Реестр измерений; реальные запросы выполняются только при запуске CLI."""

import random
import re
import uuid

from bench.runner import Bench, median, pct
from bench.textgen import filler, haystack, insert
from .agent_tasks import Chain, Structured, Tools


def integer(minimum=1):
    return {"type": "integer", "minimum": minimum}


class Speed(Bench):
    name, title = "speed", "Скорость генерации"
    defaults = {"repeats": 5, "max_tokens": 256}
    quick = {"repeats": 2, "max_tokens": 64}
    schema = {"repeats": integer(), "max_tokens": integer()}

    async def run(self, ctx):
        cfg = ctx.cfg(self.name)
        rows = []
        for _ in range(cfg["repeats"]):
            r = await ctx.client.complete(ctx.model, [{"role": "user", "content":
                "Перечисляй целые числа начиная с 1, через пробел, пока не закончится лимит ответа."}],
                max_tokens=cfg["max_tokens"])
            rows.append(vars(r) | {"decode_tps": r.decode_tps})
        good = [r for r in rows if r["status"] == 200 and not r["error"]]
        return self.result(metrics={"successful": len(good), "requests": len(rows),
            "ttft_p50": median([r["ttft"] for r in good]),
            "ttft_p95": pct([r["ttft"] for r in good], .95),
            "decode_tps_p50": median([r["decode_tps"] for r in good])}, details=rows,
            error="Все запросы завершились ошибкой" if not good else "",
            notes=["TTFT включает сеть и очередь. Без usage скорость оценивается в чанках/с; чанк может содержать несколько токенов."])


class ContextWindow(Bench):
    name, title = "context", "Принимаемое контекстное окно"
    defaults = {"min_tokens": 1024, "max_tokens": 32768, "steps": 8}
    quick = {"max_tokens": 4096, "steps": 3}
    schema = {k: integer() for k in defaults}

    async def run(self, ctx):
        cfg = ctx.cfg(self.name)
        if cfg["min_tokens"] > cfg["max_tokens"]:
            raise ValueError("context.min_tokens больше max_tokens")
        cpt = await ctx.chars_per_token()
        low, high, rows, accepted = cfg["min_tokens"], cfg["max_tokens"], [], 0
        size = low
        for _ in range(cfg["steps"]):
            r = await ctx.client.complete(ctx.model, [{"role": "user", "content":
                filler(size, cpt, ctx.config["seed"]) + "\nОтветь ОК."}], max_tokens=1)
            rows.append({"estimated_tokens": size, "prompt_tokens": r.prompt_tokens,
                         "ok": r.ok, "status": r.status, "error": r.error})
            if r.ok:
                accepted = max(accepted, size)
                low = size + 1
                if size == cfg["max_tokens"]:
                    break
                size = high if len(rows) == 1 else (low + high) // 2
            else:
                # Ошибки сети, лимиты и неподдерживаемые параметры не означают границу окна.
                if r.status not in (400, 413, 422) or not re.search(
                        r"context|too long|too many tokens|token limit|контекст", r.error, re.I):
                    return self.result(error=r.error or f"HTTP {r.status}", details=rows)
                high = size - 1
                size = (low + high) // 2
            if low > high:
                break
        ctx.shared["context_tokens"] = accepted
        return self.result(metrics={"context_tokens": accepted,
            "ceiling_reached": accepted == cfg["max_tokens"]}, details=rows,
            notes=["Размер входа оценён через символы/токен; это нижняя наблюдаемая граница, не точный паспортный лимит. Принятие запроса не доказывает сохранение информации."])


class Needle(Bench):
    name, title, quality = "needle", "Иголка в стоге сена", True
    defaults = {"lengths": [1024, 4096, 16384], "depths": [0.0, .25, .5, .75, 1.0], "repeats": 2}
    quick = {"lengths": [1024], "depths": [.0, .5, 1.0], "repeats": 1}
    schema = {"lengths": {"type": "array", "items": integer()},
              "depths": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1}},
              "repeats": integer()}

    async def run(self, ctx):
        cfg, rows = ctx.cfg(self.name), []
        rng = random.Random(ctx.config["seed"])
        cpt = await ctx.chars_per_token()
        for length in cfg["lengths"]:
            for depth in cfg["depths"]:
                for _ in range(cfg["repeats"]):
                    code = uuid.UUID(int=rng.getrandbits(128)).hex
                    text = insert(haystack(int(length * cpt), rng.randrange(2**32)),
                                  [(depth, f"Секретный код проекта Альтаир: {code}.")])
                    r = await ctx.client.complete(ctx.model, [{"role": "user", "content":
                        text + "\nКакой секретный код проекта Альтаир? Ответь только кодом."}], max_tokens=96)
                    rows.append({"length": length, "depth": depth, "expected": code,
                                 "answer": r.text, "passed": r.ok and r.text.strip() == code,
                                 "error": r.error})
        accuracy = sum(r["passed"] for r in rows) / len(rows) if rows else 0
        return self.result(score=accuracy * 100, metrics={"accuracy": accuracy, "cases": len(rows)},
                           details=rows, error="Нет случаев" if not rows else "")


class Reasoning(Bench):
    name, title, quality = "reasoning", "Арифметика и следование инструкции", True
    defaults = {"cases": 12}
    quick = {"cases": 3}
    schema = {"cases": integer()}

    async def run(self, ctx):
        rng, rows = random.Random(ctx.config["seed"]), []
        for _ in range(ctx.cfg(self.name)["cases"]):
            a, b, c = [rng.randint(11, 99) for _ in range(3)]
            expected = str((a + b) * c)
            r = await ctx.client.complete(ctx.model, [{"role": "user", "content":
                f"Вычисли ({a} + {b}) * {c}. Ответь только целым числом без пояснений."}], max_tokens=64)
            rows.append({"expected": expected, "answer": r.text,
                         "passed": r.ok and r.text.strip() == expected, "error": r.error})
        accuracy = sum(r["passed"] for r in rows) / len(rows)
        return self.result(score=accuracy * 100, metrics={"accuracy": accuracy}, details=rows,
                           notes=["Синтетическая арифметика; оценка не характеризует все виды рассуждений."])


BENCHES = {b.name: b for b in (Speed(), ContextWindow(), Needle(), Reasoning(), Tools(), Structured(), Chain())}
