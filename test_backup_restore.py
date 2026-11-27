"""Резервные копии: не чаще раза в сутки после перезапусков, не больше KEEP_FULL_MAX полных выгрузок,
и восстановление tools/restore.py читает тот же формат, что пишет backup.dump_all.
Запуск: python -m unittest test_backup_restore
"""
import datetime
import gzip
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
for name in ("sqlalchemy", "db", "models", "config"):
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = mock.MagicMock()

import backup   # noqa: E402
import restore  # noqa: E402


class Pruning(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig = backup.BACKUP_DIR
        backup.BACKUP_DIR = self.dir

    def tearDown(self):
        backup.BACKUP_DIR = self._orig

    def touch(self, name, hours_ago):
        p = os.path.join(self.dir, name)
        open(p, "w").close()
        t = time.time() - hours_ago * 3600
        os.utime(p, (t, t))
        return p

    def test_fresh_backup_is_not_repeated(self):
        self.assertIsNone(backup.last_backup_age())
        self.touch("money-old.json.gz", 30)
        self.touch("money-new.json.gz", 2)
        self.assertAlmostEqual(backup.last_backup_age() / 3600, 2, delta=0.1)
        src = (ROOT / "backup.py").read_text(encoding="utf-8")
        loop = src[src.index("async def backup_loop"):]
        self.assertIn("if age is not None and age < FRESH_HOURS * 3600:", loop)

    def test_full_dumps_limited_by_count_and_age(self):
        for i in range(7):
            self.touch(f"full-{i}.jsonl.gz", i + 1)              # 1…7 часов назад
        self.touch("full-ancient.jsonl.gz", 24 * 5)
        self.touch("money-a.json.gz", 24 * 5)                    # денежные — 14 дней
        self.touch("wipe-a.jsonl.gz", 24 * 5)                    # копия перед вайпом — по сроку полных
        backup.prune()
        left = sorted(os.listdir(self.dir))
        self.assertEqual([n for n in left if n.startswith("full-")], [f"full-{i}.jsonl.gz" for i in range(backup.KEEP_FULL_MAX)])
        self.assertIn("money-a.json.gz", left)
        self.assertNotIn("wipe-a.jsonl.gz", left)


class Restore(unittest.TestCase):
    def test_reads_dump_format(self):
        p = os.path.join(tempfile.mkdtemp(), "full-x.jsonl.gz")
        with gzip.open(p, "wt", encoding="utf-8") as f:
            for line in ({"table": "game_saves"}, {"tg_id": 1, "data": "{}"}, {"tg_id": 2, "data": "{}"},
                         {"table": "docs"}, {"table": "gram_wallets"}, {"tg_id": 1, "nano": 5}):
                f.write(json.dumps(line) + "\n")
        d = restore.read_dump(p)
        self.assertEqual({k: len(v) for k, v in d.items()}, {"game_saves": 2, "docs": 0, "gram_wallets": 1})

    def test_coerce_types(self):
        def col(name, pt):
            t = types.SimpleNamespace(python_type=pt)
            return types.SimpleNamespace(name=name, type=t)
        table = types.SimpleNamespace(columns=[col("tg_id", int), col("ok", bool), col("ts", datetime.datetime), col("v", float), col("s", str)])
        row = {"tg_id": "7", "ok": "true", "ts": "2026-11-27 10:00:00", "v": "1.5", "s": "x", "junk": 1}
        self.assertEqual(restore.coerce(table, row),
                         {"tg_id": 7, "ok": True, "ts": datetime.datetime(2026, 11, 27, 10, 0), "v": 1.5, "s": "x"})

    def test_safe_by_default(self):
        src = (ROOT / "tools" / "restore.py").read_text(encoding="utf-8")
        self.assertIn('if not a.apply:', src)
        self.assertIn('backup.dump_all("pre-restore", tables=want)', src)       # перед записью — копия текущего
        self.assertIn("with engine.begin() as conn:", src)                      # одна транзакция


if __name__ == "__main__":
    unittest.main()
