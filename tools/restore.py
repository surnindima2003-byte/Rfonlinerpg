"""Восстановление базы из резервной копии (backup.py: full-*.jsonl.gz, wipe-*.jsonl.gz).

Раньше копии делались каждый день, но вернуть данные из них было нечем. Запуск — в Railway:
сервис Rfonlinerpg → вкладка Console (там уже есть DATABASE_URL и папка /data/backups):

  python tools/restore.py --list                         # какие копии есть
  python tools/restore.py /data/backups/full-….jsonl.gz  # что внутри и сколько строк сейчас (ничего не меняет)
  python tools/restore.py ФАЙЛ --tables game_saves,docs --apply   # вернуть только эти таблицы
  python tools/restore.py ФАЙЛ --all --apply              # вернуть все таблицы из копии

С --apply выбранные таблицы ОЧИЩАЮТСЯ и заполняются строками из копии — одной транзакцией: при ошибке не
меняется ничего. Перед этим текущее состояние этих таблиц выгружается в /data/backups/pre-restore-*.jsonl.gz,
так что восстановление можно отменить тем же скриптом. Лучше делать при остановленном сервисе
(или без игроков онлайн): иначе сервер может перезаписать восстановленное своими сохранениями.
"""
import argparse
import datetime
import gzip
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def read_dump(path):
    """{таблица: [строки]} из файла копии."""
    out, cur = {}, None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if set(row) == {"table"}:
                cur = row["table"]
                out.setdefault(cur, [])
            elif cur is not None:
                out[cur].append(row)
    return out


def coerce(table, row):
    """Значения из JSON (даты и Decimal выгружались строкой) — к типам колонок; лишние ключи отбрасываются."""
    out = {}
    for col in table.columns:
        if col.name not in row:
            continue
        v = row[col.name]
        if isinstance(v, str):
            try:
                pt = col.type.python_type
            except NotImplementedError:
                pt = str
            try:
                if pt is bool:
                    v = v.lower() in ("1", "true", "t")
                elif pt is int:
                    v = int(v)
                elif pt is float:
                    v = float(v)
                elif pt is datetime.datetime:
                    v = datetime.datetime.fromisoformat(v)
            except ValueError:
                pass
        out[col.name] = v
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Восстановление базы MetalWar из резервной копии")
    ap.add_argument("file", nargs="?", help="файл копии *.jsonl.gz")
    ap.add_argument("--list", action="store_true", help="показать копии в BACKUP_DIR")
    ap.add_argument("--tables", default="", help="таблицы через запятую")
    ap.add_argument("--all", action="store_true", help="все таблицы из копии")
    ap.add_argument("--apply", action="store_true", help="записать в базу (без него — только показать)")
    a = ap.parse_args(argv)

    from config import BACKUP_DIR
    if a.list or not a.file:
        names = sorted(os.listdir(BACKUP_DIR)) if os.path.isdir(BACKUP_DIR) else []
        for n in names:
            p = os.path.join(BACKUP_DIR, n)
            print(f"{n:45} {os.path.getsize(p) / 1e6:8.1f} МБ  {datetime.datetime.utcfromtimestamp(os.path.getmtime(p)):%d.%m %H:%M} UTC")
        if not names:
            print("Копий нет в", BACKUP_DIR)
        return 0

    from sqlalchemy import delete, func, inspect as sa_inspect, select
    from db import engine
    from models import Base
    import backup

    dump = read_dump(a.file)
    tables = {t.name: t for t in Base.metadata.sorted_tables}
    want = list(dump) if a.all else [t.strip() for t in a.tables.split(",") if t.strip()]
    unknown = [t for t in want if t not in tables or t not in dump]
    with engine.connect() as conn:
        have = set(sa_inspect(conn).get_table_names())
        print(f"{'таблица':24} {'в копии':>10} {'сейчас':>10}")
        for name in dump:
            now = conn.execute(select(func.count()).select_from(tables[name])).scalar() if name in tables and name in have else "—"
            mark = " ←" if name in want else ""
            print(f"{name:24} {len(dump[name]):>10} {now:>10}{mark}")
    if unknown:
        print("Нет в копии или в схеме:", ", ".join(unknown))
        return 2
    if not want:
        print("\nНичего не выбрано: укажи --tables … или --all, и --apply, чтобы записать.")
        return 0
    if not a.apply:
        print("\nПроверка без изменений. Чтобы записать, добавь --apply.")
        return 0
    safety = backup.dump_all("pre-restore", tables=want)
    print("Текущее состояние выбранных таблиц сохранено:", safety)
    order = [t for t in Base.metadata.sorted_tables if t.name in want]
    with engine.begin() as conn:                     # одна транзакция: всё или ничего
        for t in reversed(order):
            conn.execute(delete(t))
        for t in order:
            rows = [coerce(t, r) for r in dump[t.name]]
            for i in range(0, len(rows), 500):
                if rows[i:i + 500]:
                    conn.execute(t.insert(), rows[i:i + 500])
            print(f"{t.name}: записано строк {len(rows)}")
    print("Готово. Перезапусти сервис (Deployments → Restart), чтобы сервер перечитал данные.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
