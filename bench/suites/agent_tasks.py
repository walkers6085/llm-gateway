"""Синтетические проверки агентских навыков без исполнения внешних инструментов."""

import json
import random

from agent.schema import validate
from bench.runner import Bench


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def tool(name, description, properties):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": object_schema(properties)}}


STR = {"type": "string"}
TOOLS = [tool("weather", "Текущая погода в городе", {"city": STR}),
         tool("multiply", "Умножение двух целых чисел", {"a": {"type": "integer"}, "b": {"type": "integer"}})]


def strict_json(text):
    """JSON без NaN/Infinity и повторяющихся ключей; Markdown-обёртки не принимаются."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"повторяющийся ключ: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"недопустимая константа: {value}")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def check_call(calls, expected_name, expected_args, definitions):
    if not isinstance(calls, list) or len(calls) != 1:
        raise ValueError("ожидается ровно один вызов инструмента")
    call = calls[0]
    if not isinstance(call, dict):
        raise ValueError("вызов должен быть объектом")
    if call.get("type") != "function" or not isinstance(call.get("id"), str) or not call["id"]:
        raise ValueError("нет id или неверный тип вызова")
    fn = call.get("function", {})
    if not isinstance(fn, dict) or fn.get("name") != expected_name:
        raise ValueError(f"ожидается инструмент {expected_name}")
    args = strict_json(fn.get("arguments", ""))
    definition = next(t["function"] for t in definitions if t["function"]["name"] == expected_name)
    validate(args, definition["parameters"])
    if args != expected_args:
        raise ValueError("аргументы не совпали с ожидаемыми")
    return call


class TaskBench(Bench):
    quality = True
    defaults = {"repeats": 2, "max_tokens": 256}
    quick = {"repeats": 1}
    schema = {k: {"type": "integer", "minimum": 1} for k in defaults}

    def summarize(self, rows, **metrics):
        passed = sum(row["passed"] for row in rows)
        return self.result(score=100 * passed / len(rows),
                           metrics={"passed": passed, "cases": len(rows), **metrics}, details=rows,
                           notes=["Ошибки API считаются провалами; детали содержат ответы и причину каждого провала."])


class Tools(TaskBench):
    name, title = "tools", "Выбор инструментов и аргументы"

    async def run(self, ctx):
        rng, rows = random.Random(ctx.config["seed"]), []
        cfg = ctx.cfg(self.name)
        for _ in range(cfg["repeats"]):
            city = rng.choice(["Казань", "Омск", "Томск", "Пермь"])
            a, b = rng.randint(12, 99), rng.randint(12, 99)
            cases = [(f"Узнай текущую погоду: город {city}. Используй инструмент weather.", "weather", {"city": city}),
                     (f"Вычисли {a} * {b} инструментом multiply.", "multiply", {"a": a, "b": b}),
                     ("Ответь только словом ПРИВЕТ. Инструменты не используй.", None, {})]
            for prompt, name, arguments in cases:
                r = await ctx.client.complete(ctx.model, [{"role": "user", "content": prompt}],
                                              tools=TOOLS, max_tokens=cfg["max_tokens"])
                error = r.error or (f"HTTP {r.status}" if not r.ok else "")
                if not error:
                    try:
                        if name:
                            check_call(r.tool_calls, name, arguments, TOOLS)
                        elif r.tool_calls or r.text.strip() != "ПРИВЕТ":
                            raise ValueError("ожидается ПРИВЕТ без вызова инструмента")
                    except (ValueError, TypeError, KeyError, StopIteration) as exc:
                        error = str(exc)
                rows.append({"prompt": prompt, "expected_tool": name, "expected_arguments": arguments,
                             "answer": r.text, "tool_calls": r.tool_calls, "passed": not error, "error": error})
        return self.summarize(rows)


class Structured(TaskBench):
    name, title = "structured", "JSON: схема и точность данных"

    async def run(self, ctx):
        rng, rows = random.Random(ctx.config["seed"]), []
        cfg = ctx.cfg(self.name)
        for _ in range(cfg["repeats"]):
            count = rng.randint(2, 50)
            cases = [({"name": "Анна", "count": count, "active": True},
                      object_schema({"name": STR, "count": {"type": "integer", "minimum": 1},
                                     "active": {"type": "boolean"}})),
                     ({"items": [{"sku": "A-17", "quantity": count}], "comment": None},
                      object_schema({"items": {"type": "array", "items": object_schema({"sku": STR,
                                      "quantity": {"type": "integer", "minimum": 1}})},
                                     "comment": {"type": ["string", "null"]}}))]
            for expected, schema in cases:
                prompt = ("Верни только JSON без Markdown и пояснений. Сохрани все значения исходных данных.\n"
                          f"Данные: {json.dumps(expected, ensure_ascii=False)}\n"
                          f"Схема: {json.dumps(schema, ensure_ascii=False)}")
                r = await ctx.client.complete(ctx.model, [{"role": "user", "content": prompt}],
                                              max_tokens=cfg["max_tokens"])
                valid = False
                error = r.error or (f"HTTP {r.status}" if not r.ok else "")
                if not error:
                    try:
                        data = strict_json(r.text)
                        validate(data, schema)
                        valid = True
                        if data != expected:
                            raise ValueError("JSON соответствует схеме, но значения неверны")
                        if r.tool_calls:
                            raise ValueError("неожиданный вызов инструмента")
                    except (ValueError, TypeError) as exc:
                        error = str(exc)
                rows.append({"expected": expected, "schema": schema, "answer": r.text,
                             "schema_valid": valid, "passed": not error, "error": error})
        return self.summarize(rows, schema_valid=sum(r["schema_valid"] for r in rows))


CHAIN_TOOLS = [tool("find_product", "Найти товар по названию; возвращает product_id", {"name": STR}),
               tool("get_price", "Получить цену по product_id", {"product_id": STR})]


class Chain(TaskBench):
    name, title = "chain", "Цепочка: поиск товара → цена → ответ"

    async def run(self, ctx):
        rng, rows = random.Random(ctx.config["seed"]), []
        cfg = ctx.cfg(self.name)
        for _ in range(cfg["repeats"]):
            name = f"Лампа-{rng.randint(100, 999)}"
            product_id = f"p-{rng.getrandbits(64):016x}"
            price = rng.randint(1000, 9999)
            messages = [{"role": "user", "content":
                f"Найди товар «{name}» через find_product, получи его цену через get_price. "
                "Вызывай инструменты по одному. В конце ответь только целым числом — ценой."}]
            trace, error, used_ids = [], "", set()
            for step, (expected_name, arguments, output) in enumerate([
                    ("find_product", {"name": name}, {"product_id": product_id}),
                    ("get_price", {"product_id": product_id}, {"price": price}),
                    (None, {}, None)]):
                r = await ctx.client.complete(ctx.model, messages, tools=CHAIN_TOOLS,
                                              max_tokens=cfg["max_tokens"])
                trace.append({"step": step + 1, "answer": r.text, "tool_calls": r.tool_calls,
                              "status": r.status, "error": r.error})
                error = r.error or (f"HTTP {r.status}" if not r.ok else "")
                if error:
                    break
                try:
                    if expected_name is None:
                        if r.tool_calls or r.text.strip() != str(price):
                            raise ValueError("итоговый ответ должен точно совпасть с ценой из инструмента")
                    else:
                        call = check_call(r.tool_calls, expected_name, arguments, CHAIN_TOOLS)
                        if call["id"] in used_ids:
                            raise ValueError("повторный id вызова")
                        used_ids.add(call["id"])
                        messages.append({"role": "assistant", "content": r.text or None, "tool_calls": r.tool_calls})
                        messages.append({"role": "tool", "tool_call_id": call["id"],
                                         "content": json.dumps(output, ensure_ascii=False)})
                        trace[-1]["tool_result"] = output
                except (ValueError, TypeError, KeyError, StopIteration) as exc:
                    error = str(exc)
                    break
            rows.append({"product": name, "expected_price": price, "trace": trace,
                         "passed": not error, "error": error})
        return self.summarize(rows)
