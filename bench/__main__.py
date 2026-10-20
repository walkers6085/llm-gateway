"""CLI: python -m bench --help."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from agent.providers import ProviderManager

from .client import BenchClient
from .runner import Context, build_config, compare, make_report, run_all, save_report
from .suites import BENCHES


async def execute(args):
    config = build_config(BENCHES, quick=args.quick,
                          file=json.loads(Path(args.config).read_text()) if args.config else None,
                          sets=args.set)
    names = args.suites.split(",")
    if any(n not in BENCHES for n in names):
        raise ValueError("Неизвестный набор: " + args.suites)
    provider = None
    connection = None
    token = os.environ.get("GW_TOKEN", "")
    manager = ProviderManager(args.providers_file) if args.provider or args.providers_file or not args.url else None
    if args.provider or args.providers_file or (manager is not None and manager.selected and not args.url):
        if args.url:
            raise ValueError("--url и --provider/--providers-file нельзя использовать вместе")
        connection = manager.connection(args.provider, args.model)
        provider, args.url, args.model, token = connection.name, connection.url, connection.model, connection.token
    args.url = args.url or "http://127.0.0.1:7700/v1"
    if not args.model:
        raise ValueError("нужен --model или модель по умолчанию у провайдера")
    client = BenchClient(args.url, token=token, timeout=config["timeout"], **({"connection": connection} if connection else {}))
    try:
        ctx = Context(client, args.model, config, lambda s: print(s, file=sys.stderr))
        results = await run_all(ctx, [BENCHES[n] for n in names])
        report = make_report(args.model, args.url, config, results, ctx.shared)
        if provider:
            report["provider"] = provider
        for path in save_report(report, BENCHES, Path(args.output)):
            print(path)
        return 1 if any(r.error or r.infrastructure_errors for r in results) else 0
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="Бенчмарки модели (выполняют реальные запросы к API)")
    parser.add_argument("--model")
    parser.add_argument("--url", help="API URL; по умолчанию http://127.0.0.1:7700/v1")
    parser.add_argument("--provider")
    parser.add_argument("--providers-file")
    parser.add_argument("--suites", default=",".join(BENCHES))
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--set", action="append", default=[])
    parser.add_argument("--output", default="data/bench")
    parser.add_argument("--compare", nargs="+", metavar="REPORT.json")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    try:
        if args.list:
            for b in BENCHES.values():
                print(f"{b.name}: {b.title}")
            return 0
        if args.compare:
            print(compare([json.loads(Path(p).read_text()) for p in args.compare]))
            return 0
        return asyncio.run(execute(args))
    except (ValueError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
