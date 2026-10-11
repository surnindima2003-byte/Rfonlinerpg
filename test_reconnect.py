"""Переподключение к живому миру идёт со случайным разбросом, а не всей толпой в одну секунду.

Запуск: python -m unittest test_reconnect
Функция берётся из game.html и запускается в node (если node нет — тест пропускается).
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")


def run(expr):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("нет node")
    a = GAME.index("function reconnectDelay(")
    fn = GAME[a:GAME.index("\n  function connect()", a)]
    js = "let retry = 1000;\n" + fn + "\nconsole.log(JSON.stringify(" + expr + "));"
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class Reconnect(unittest.TestCase):
    def test_first_attempts_are_spread(self):
        # 300 телефонов после обрыва: первая попытка каждого (retry = 1 с)
        ds = run("Array.from({length:300}, () => { retry = 1000; return reconnectDelay(1006); })")
        self.assertGreaterEqual(min(ds), 500)
        self.assertLessEqual(max(ds), 1500)
        self.assertGreater(len(set(d // 100 for d in ds)), 8)          # не одна секунда на всех

    def test_restart_spread_wider(self):
        ds = run("Array.from({length:300}, () => { retry = 1000; return reconnectDelay(1012); })")
        self.assertGreaterEqual(min(ds), 2000)
        self.assertLessEqual(max(ds), 12000)
        self.assertGreater(max(ds) - min(ds), 6000)

    def test_backoff_grows_and_caps(self):
        r = run("(() => { retry = 1000; const out = []; for(let i = 0; i < 8; i++){ reconnectDelay(1006); out.push(retry); } return out; })()")
        self.assertEqual(r[:4], [2000, 4000, 8000, 15000])
        self.assertEqual(max(r), 15000)

    def test_wired_into_onclose(self):
        a = GAME.index("ws.onclose = ev =>")
        h = GAME[a:GAME.index("};", a)]
        self.assertIn("setTimeout(connect, reconnectDelay(ev && ev.code))", h)
        self.assertNotIn("setTimeout(connect, retry)", GAME)


if __name__ == "__main__":
    unittest.main()
