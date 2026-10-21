#!/usr/bin/env python3
"""Выдача и отзыв токенов LLM Gateway.

  gwtoken create <имя> [--days 30] [--models m1,m2] [--rpm 30] [--agent <профиль>]
  gwtoken agent <имя|префикс> <профиль> | --off
  gwtoken list
  gwtoken revoke <имя|префикс>
  gwtoken usage [<имя>] [--days 7]
"""

import argparse
from contextlib import closing
import sqlite3
import sys
import time
from datetime import datetime

from agent import __version__
from gateway.config import Settings  # noqa: E402
from gateway.tokens import TokenStore  # noqa: E402


def fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "—"


def status(t) -> str:
    if t.revoked:
        return "отозван"
    if t.expires and t.expires <= time.time():
        return "истёк"
    return "активен"


def main() -> int:
    p = argparse.ArgumentParser(prog="gwtoken", description="Токены LLM Gateway")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="выдать токен")
    c.add_argument("name")
    c.add_argument("--days", type=float, help="срок действия в днях (по умолчанию бессрочно)")
    c.add_argument("--models", default="", help="разрешённые модели через запятую (по умолчанию все)")
    c.add_argument("--rpm", type=int, default=0, help="лимит запросов в минуту (0 — без лимита)")
    c.add_argument("--agent", default="", help="профиль агента из config/agent.json (по умолчанию агент недоступен)")
    a = sub.add_parser("agent", help="выдать или забрать доступ к агенту")
    a.add_argument("name")
    a.add_argument("profile", nargs="?", default="")
    a.add_argument("--off", action="store_true", help="забрать доступ")
    sub.add_parser("list", help="список токенов")
    r = sub.add_parser("revoke", help="отозвать токен")
    r.add_argument("name")
    u = sub.add_parser("usage", help="расход")
    u.add_argument("name", nargs="?")
    u.add_argument("--days", type=float, default=7)
    args = p.parse_args()

    with closing(TokenStore(Settings.from_env().db_path)) as store:
        return _run(args, store)


def _run(args, store):
    if args.cmd == "create":
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        try:
            raw, t = store.create(args.name, days=args.days, models=models, rpm=args.rpm,
                                  agent_profile=args.agent)
        except sqlite3.IntegrityError:
            print(f"Токен с именем {args.name!r} уже есть", file=sys.stderr)
            return 1
        print(raw)
        print(f"# {t.name}: до {fmt_ts(t.expires)}, модели: {','.join(t.models) or 'все'}, "
              f"rpm: {t.rpm or '∞'}, агент: {t.agent_profile or 'нет'}. Токен показан один раз — сохраните его.",
              file=sys.stderr)
    elif args.cmd == "agent":
        t = store.find(args.name)
        if t is None:
            print(f"Не найден (или неоднозначен): {args.name}", file=sys.stderr)
            return 1
        if not args.off and not args.profile:
            print("Укажите профиль или --off", file=sys.stderr)
            return 1
        store.set_agent_profile(t.id, "" if args.off else args.profile)
        print(f"{t.name}: агент {'выключен' if args.off else 'профиль ' + args.profile}")
    elif args.cmd == "list":
        stats = {row["name"]: row for row in _totals(store, 30)}
        print(f"{'имя':<20} {'префикс':<12} {'статус':<8} {'создан':<10} {'до':<10} {'rpm':>4} "
              f"{'запр.30д':>8} {'токенов30д':>10} {'агент':<10} модели")
        for t in store.all():
            s = stats.get(t.name)
            print(f"{t.name:<20} {t.prefix:<12} {status(t):<8} {fmt_ts(t.created):<10} {fmt_ts(t.expires):<10} "
                  f"{t.rpm or '∞':>4} {s['requests'] if s else 0:>8} {s['tokens'] if s else 0:>10} "
                  f"{t.agent_profile or '—':<10} "
                  f"{','.join(t.models) or 'все'}")
    elif args.cmd == "revoke":
        t = store.find(args.name)
        if t is None:
            print(f"Не найден (или неоднозначен): {args.name}", file=sys.stderr)
            return 1
        store.revoke(t.id)
        print(f"Отозван: {t.name} ({t.prefix}…)")
    elif args.cmd == "usage":
        token_id = None
        if args.name:
            t = store.find(args.name)
            if t is None:
                print(f"Не найден: {args.name}", file=sys.stderr)
                return 1
            token_id = t.id
        print(f"{'имя':<20} {'модель':<30} {'запр.':>6} {'ошиб.':>6} {'вход':>10} {'выход':>10}")
        for row in store.usage(token_id, args.days):
            print(f"{row['name']:<20} {row['model'] or '—':<30} {row['requests']:>6} {row['errors']:>6} "
                  f"{row['prompt_tokens'] or 0:>10} {row['completion_tokens'] or 0:>10}")
    return 0


def _totals(store: TokenStore, days: float):
    since = time.time() - days * 86400
    return store.db.execute(
        "SELECT t.name, COUNT(*) AS requests, "
        "COALESCE(SUM(u.prompt_tokens + u.completion_tokens), 0) AS tokens "
        "FROM usage u JOIN tokens t ON t.id = u.token_id WHERE u.ts >= ? GROUP BY t.name",
        (since,),
    ).fetchall()


if __name__ == "__main__":
    sys.exit(main())
