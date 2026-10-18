"""存量股票代码后缀化迁移：前缀 SH600036 → 后缀 600036.SH（幂等，可重跑）。

迁移面：
  PG：public schema 下所有字符型 symbol/stock_code 列，仅改严格匹配
       ^(SH|SZ|BJ)[0-9]{6}$（大写）的值 → 600036.SH；小写/指数/港美股只报告不碰。
  Redis：market:snapshot:(sh|SH)600036、market:series:SH600036 等前缀键
       → market:snapshot:600036.SH / market:series:600036.SH（DUMP/RESTORE 全类型拷贝，
       默认保留老键；--delete-old 在代码翻转批次落地后做最终清理）。

用法（服务器上，先 dry-run）：
    docker exec quantmind python3 /app/backend/scripts/migrate_stock_code_to_suffix.py --dry-run
    docker exec quantmind python3 /app/backend/scripts/migrate_stock_code_to_suffix.py
    docker exec quantmind python3 /app/backend/scripts/migrate_stock_code_to_suffix.py --delete-old  # 最终清理老键

注意：Qlib 专用小写 sh600036 不在迁移范围（仅内存转换，不落库）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

PREFIX_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$")

SKIP_TABLES = {"alembic_version", "spatial_ref_sys", "geography_columns", "geometry_columns"}


def to_suffix(value: str) -> str | None:
    m = PREFIX_RE.match(str(value or "").strip())
    if not m:
        return None
    return f"{m.group(2)}.{m.group(1)}"


async def migrate_pg(dry_run: bool) -> dict[str, int]:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    updated: dict[str, int] = {}
    async with get_session() as session:
        cols = (await session.execute(text("""
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND column_name IN ('symbol', 'stock_code')
              AND data_type IN ('character varying', 'varchar', 'character', 'char', 'text')
        """))).mappings().all()
        for row in cols:
            table, col = str(row["table_name"]), str(row["column_name"])
            if table in SKIP_TABLES:
                continue
            rows = (await session.execute(text(f"""
                SELECT DISTINCT "{col}" AS v FROM "{table}"
                WHERE "{col}" ~ '^(SH|SZ|BJ)[0-9]{{6}}$'
            """))).mappings().all()
            if not rows:
                continue
            n = 0
            for r in rows:
                old = str(r["v"])
                new = to_suffix(old)
                if not new:
                    continue
                cnt = (await session.execute(
                    text(f'SELECT COUNT(*) AS c FROM "{table}" WHERE "{col}" = :old'),
                    {"old": old},
                )).mappings().first()
                n += int(cnt["c"]) if cnt else 0
                if not dry_run:
                    await session.execute(
                        text(f'UPDATE "{table}" SET "{col}" = :new WHERE "{col}" = :old'),
                        {"new": new, "old": old},
                    )
            updated[f"{table}.{col}"] = n
            print(f"  PG {table}.{col}: {len(rows)} 个不同值，共 {n} 行 -> 后缀")
        if not dry_run:
            await session.commit()
    return updated


def _new_redis_key(key: str) -> str | None:
    for prefix in ("market:snapshot:", "market:series:", "stock:"):
        if key.startswith(prefix):
            rest = key[len(prefix):]
            suf = to_suffix(rest)
            if suf:
                return prefix + suf
            # 小写前缀 sh600036
            m = re.match(r"^(sh|sz|bj)(\d{6})$", rest)
            if m:
                return f"{prefix}{m.group(2)}.{m.group(1).upper()}"
            # 已是后缀则无需迁移
            if re.match(r"^\d{6}\.(SH|SZ|BJ)$", rest.upper()):
                return None
    return None


def _redis_targets() -> list[tuple[str, int, str | None, list[int]]]:
    """(label, host, port, password, dbs)。行情快照实际在远端全市场库（REMOTE_QUOTE_REDIS_*）。"""
    targets = [(
        "local",
        os.getenv("REDIS_HOST", "localhost"),
        int(os.getenv("REDIS_PORT", "6379")),
        os.getenv("REDIS_PASSWORD") or None,
        list(range(6)),
    )]
    remote_host = os.getenv("REMOTE_QUOTE_REDIS_HOST")
    if remote_host:
        targets.append((
            "remote-quote",
            remote_host,
            int(os.getenv("REMOTE_QUOTE_REDIS_PORT", "6379")),
            os.getenv("REMOTE_QUOTE_REDIS_PASSWORD") or None,
            [int(os.getenv("REMOTE_QUOTE_REDIS_DB", "3"))],
        ))
    return targets


def migrate_redis(dry_run: bool, delete_old: bool) -> dict[str, int]:
    stats = {"copied": 0, "deleted": 0, "skipped": 0}
    try:
        import redis
    except ImportError:
        print("redis 包不可用，跳过 Redis 键迁移")
        return stats
    for label, host, port, password, dbs in _redis_targets():
        for db in dbs:
            try:
                client = redis.Redis(
                    host=host, port=port, db=db, password=password,
                    socket_connect_timeout=5, decode_responses=False,
                )
                for pattern in ("market:snapshot:*", "market:series:*", "stock:*"):
                    for raw in client.scan_iter(match=pattern, count=1000):
                        key = raw.decode() if isinstance(raw, bytes) else str(raw)
                        new_key = _new_redis_key(key)
                        if not new_key or new_key == key:
                            stats["skipped"] += 1
                            continue
                        if client.exists(new_key):
                            stats["skipped"] += 1
                            continue
                        print(f"  Redis {label}/db{db}: {key} -> {new_key}")
                        stats["copied"] += 1
                        if dry_run:
                            continue
                        ttl = client.pttl(key)
                        blob = client.dump(key)
                        client.restore(new_key, max(ttl, 0), blob, replace=False)
                        if delete_old:
                            client.delete(key)
                            stats["deleted"] += 1
                client.close()
            except Exception as exc:
                print(f"Redis {label}/db{db} 跳过: {exc}")
    return stats


async def main() -> int:
    ap = argparse.ArgumentParser(description="股票代码后缀化迁移（幂等）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    ap.add_argument("--skip-pg", action="store_true", help="跳过 PG 迁移")
    ap.add_argument("--skip-redis", action="store_true", help="跳过 Redis 迁移")
    ap.add_argument("--delete-old", action="store_true", help="拷贝后删除老键（最终清理用）")
    args = ap.parse_args()

    if not args.skip_pg:
        try:
            await migrate_pg(args.dry_run)
        except Exception as exc:
            print(f"PG 迁移失败: {exc}", file=sys.stderr)
            return 1
    if not args.skip_redis:
        stats = migrate_redis(args.dry_run, args.delete_old)
        print(f"Redis: 拷贝 {stats['copied']}，删除老键 {stats['deleted']}，跳过 {stats['skipped']}")
    print("[dry-run] 未写入任何数据" if args.dry_run else "完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
