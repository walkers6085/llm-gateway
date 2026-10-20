"""Каркас бенчмарков: базовый класс, контекст запуска, отчёт (JSON + Markdown), сравнение отчётов.

Каждый бенчмарк — подкласс Bench в bench/suites/*.py: свои умолчания и схема настроек,
`run(ctx) → Result` и `render(result) → строки Markdown`. Подробности — docs/wiki/bench/README.md.
"""

import json
import statistics
import time
import hashlib
import platform
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent.credentials import sanitize
from agent.schema import SchemaError, validate
from agent.settings import merge

from .client import BenchClient
from .textgen import filler

FALLBACK_CPT = {"ru": 3.2, "en": 4.0}


@dataclass
class Result:
    name: str
    title: str
    quality: bool = False           # входит в итоговую оценку качества
    score: float | None = None      # 0–100 для бенчмарков качества
    metrics: dict = field(default_factory=dict)
    details: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0
    infrastructure_errors: int = 0


class Bench:
    name = ""
    title = ""
    description = ""
    quality = False
    defaults: dict = {}
    quick: dict = {}                 # переопределения для --quick
    schema: dict = {}                # properties для настроек этого бенчмарка

    def result(self, **kw) -> Result:
        return Result(name=self.name, title=self.title, quality=self.quality, **kw)

    async def run(self, ctx: "Context") -> Result:
        raise NotImplementedError

    def render(self, r: Result) -> list[str]:
        return [f"- {k}: {fmt(v)}" for k, v in r.metrics.items()]


class Context:
    def __init__(self, client: BenchClient, model: str, config: dict, log: Callable[[str], None] = lambda m: None):
        self.client, self.model, self.config, self.log = client, model, config, log
        self.shared: dict = {}       # результаты одних бенчмарков для других (окно контекста, chars/token)

    def cfg(self, bench: str) -> dict:
        return self.config[bench]

    async def chars_per_token(self, lang: str = "ru") -> float:
        """Калибровка: два запроса разной длины, наклон prompt_tokens от числа символов."""
        key = f"cpt_{lang}"
        if key not in self.shared:
            sizes, points = (1500, 6000), []
            for chars in sizes:
                text = filler(int(chars / FALLBACK_CPT[lang]), FALLBACK_CPT[lang], seed=7, lang=lang)[:chars]
                r = await self.client.complete(self.model, [{"role": "user", "content": text + "\nОк?"}],
                                               max_tokens=1)
                points.append((len(text), r.prompt_tokens if r.ok and r.usage_reported else 0))
            (c1, t1), (c2, t2) = points
            if t1 and t2 and t2 > t1:
                self.shared[key] = round((c2 - c1) / (t2 - t1), 3)
            else:
                self.shared[key] = FALLBACK_CPT[lang]
                self.shared[f"{key}_fallback"] = True
            self.log(f"калибровка: {self.shared[key]} символов на токен ({lang})")
        return self.shared[key]


COMMON_DEFAULTS = {"seed": 42, "timeout": 300}
COMMON_SCHEMA = {"seed": {"type": "integer"}, "timeout": {"type": "number", "minimum": 1}}


def config_schema(benches: dict[str, Bench]) -> dict:
    props = dict(COMMON_SCHEMA)
    for name, b in benches.items():
        props[name] = {"type": "object", "properties": b.schema, "additionalProperties": False}
    return {"type": "object", "properties": props, "additionalProperties": False}


def build_config(benches: dict[str, Bench], *, quick: bool = False, file: dict | None = None,
                 sets: list[str] | None = None) -> dict:
    """Умолчания → (--quick) → файл --config → --set ключ=JSON. Проверяется схемой."""
    cfg = merge(COMMON_DEFAULTS, {n: b.defaults for n, b in benches.items()})
    if quick:
        cfg = merge(cfg, {n: b.quick for n, b in benches.items()})
    if file:
        cfg = merge(cfg, file)
    for item in sets or []:
        key, _, raw = item.partition("=")
        if not raw:
            raise ValueError(f"--set {item!r}: нужно ключ=значение")
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        node = cfg
        parts = key.split(".")
        for part in parts[:-1]:
            if not isinstance(node.get(part, {}), dict):
                raise ValueError(f"--set {key}: {part} не объект")
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    try:
        validate(cfg, config_schema(benches))
    except SchemaError as exc:
        raise ValueError(f"настройки бенчмарков: {exc}") from None
    for name, values in cfg.items():
        if isinstance(values, dict):
            for key, value in values.items():
                if isinstance(value, list) and not value:
                    raise ValueError(f"{name}.{key}: список не должен быть пустым")
    return cfg


# ---------- статистика и форматирование ----------


def pct(values: list[float], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def median(values) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def fmt(v, digits: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(fmt(c) for c in row) + " |" for row in rows]
    return out


# ---------- запуск и отчёт ----------


async def run_all(ctx: Context, benches: list[Bench]) -> list[Result]:
    results = []
    for b in benches:
        ctx.log(f"▶ {b.name}: {b.title}")
        started = time.perf_counter()
        try:
            r = await b.run(ctx)
        except Exception as exc:  # бенчмарк упал — остальные всё равно выполняются
            r = b.result(error=f"{type(exc).__name__}: {exc}")
        r.seconds = round(time.perf_counter() - started, 2)
        summary = f"оценка {r.score:.0f}/100" if r.score is not None else "готово"
        ctx.log(f"✗ {b.name}: {r.error}" if r.error else f"✓ {b.name}: {summary} за {r.seconds:.0f} с")
        results.append(r)
    return results


def overall(results: list[Result]) -> float | None:
    scores = [r.score for r in results if r.quality and r.score is not None and not r.error]
    return round(sum(scores) / len(scores), 1) if scores else None


def make_report(model: str, url: str, config: dict, results: list[Result], shared: dict) -> dict:
    try:
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        revision = ''
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    complete = all(not r.error and not r.infrastructure_errors for r in results)
    return {
        "schema_version": 2, "method_version": "2", "revision": revision,
        "python": platform.python_version(), "config_hash": fingerprint, "complete": complete,
        "model": model, "url": url,
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall": overall(results) if complete else None,
        "shared": shared,
        "config": config,
        "results": [asdict(r) for r in results],
    }


def render_report(report: dict, benches: dict[str, Bench]) -> str:
    lines = [f"# Бенчмарк модели `{report['model']}`", "",
             f"{report['finished']} · {report['url']}", ""]
    lines += ["Полнота: " + ("полный" if report.get('complete', True) else "НЕПОЛНЫЙ: есть ошибки инфраструктуры"), ""]
    if report["overall"] is not None:
        lines += [f"**Итоговая оценка качества: {report['overall']:.1f}/100** "
                  "(среднее по бенчмаркам качества)", ""]
    rows = []
    for r in report["results"]:
        rows.append([r["title"], f"{r['score']:.0f}" if r["score"] is not None else "—",
                     "ошибка" if r["error"] else "ок", f"{r['seconds']:.0f} с"])
    lines += table(["Бенчмарк", "Оценка", "Статус", "Время"], rows) + [""]
    for r in report["results"]:
        lines += [f"## {r['title']} (`{r['name']}`)", ""]
        if r["error"]:
            lines += [f"Ошибка: {r['error']}", ""]
            continue
        bench = benches.get(r["name"])
        if bench:
            lines += bench.render(Result(**r))
        lines += [""] + [f"> {n}" for n in r["notes"]] + ([""] if r["notes"] else [])
    return "\n".join(lines).rstrip() + "\n"


def save_report(report: dict, benches: dict[str, Bench], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in report["model"])
    base = out_dir / f"{stamp}_{safe}"
    json_path, md_path = Path(str(base) + ".json"), Path(str(base) + ".md")
    report = sanitize(report)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_report(report, benches), encoding="utf-8")
    return json_path, md_path


KEY_METRICS = {
    "speed": ["ttft_p50", "decode_tps_p50", "prefill_tps"],
    "concurrency": ["max_aggregate_tps", "saturation_level"],
    "context": ["context_tokens", "declared"],
    "needle": ["accuracy"],
    "tools": ["passed"],
    "structured": ["passed", "schema_valid"],
    "chain": ["passed"],
    "recovery": ["passed"], "tool_choice": ["passed"],
    "retrieval": ["accuracy", "useful_context_tokens"],
    "reasoning": ["accuracy"],
    "stability": ["identical"],
}


def compare(reports: list[dict]) -> str:
    """Таблица «бенчмарк/метрика × модель» по нескольким отчётам."""
    head = ["Метрика"] + [f"{r['model']} ({r['finished'][:10]})" for r in reports]
    rows = [["Итог качества"] + [r["overall"] for r in reports]]
    names = []
    for r in reports:
        for res in r["results"]:
            if res["name"] not in names:
                names.append(res["name"])
    for name in names:
        per = [{x["name"]: x for x in r["results"]}.get(name) for r in reports]
        title = next(x["title"] for x in per if x)
        if any(x and x["score"] is not None for x in per):
            rows.append([f"{title}: оценка"] + [x["score"] if x else None for x in per])
        for key in KEY_METRICS.get(name, []):
            rows.append([f"{title}: {key}"] + [x["metrics"].get(key) if x else None for x in per])
    versions = {(r.get('method_version'), r.get('config_hash'), tuple(x['name'] for x in r['results'])) for r in reports}
    warning = "Внимание: различаются версии методики, конфигурации или наборы; оценки напрямую несопоставимы.\n\n" if len(versions) > 1 else ''
    return warning + "\n".join(table(head, rows)) + "\n"
