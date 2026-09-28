import ast
import json
import unittest
from pathlib import Path
from types import SimpleNamespace


SOURCE = (Path(__file__).resolve().parents[1] / "webserver.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FUNCTIONS = {
    node.name: node
    for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name in {"set_saved_nick", "valid_nick"}
}
namespace = {"json": json, "time": __import__("time")}
exec(compile(ast.Module(body=list(FUNCTIONS.values()), type_ignores=[]), "webserver.py", "exec"), namespace)
set_saved_nick = namespace["set_saved_nick"]
valid_nick = namespace["valid_nick"]


class AdminNicknameTests(unittest.TestCase):
    def test_set_saved_nick_updates_index_and_saved_state(self):
        row = SimpleNamespace(
            nick="Старый",
            data=json.dumps({"S": {"name": "Старый", "_ts": 10}, "ts": 10}),
            updated=0,
        )

        set_saved_nick(row, "Новый", now_ms=123_000)

        saved = json.loads(row.data)
        self.assertEqual(row.nick, "Новый")
        self.assertEqual(saved["S"]["name"], "Новый")
        self.assertEqual(saved["S"]["_ts"], 123_000)
        self.assertEqual(saved["ts"], 123_000)
        self.assertEqual(row.updated, 123)

    def test_set_saved_nick_keeps_empty_save_empty(self):
        row = SimpleNamespace(nick="Старый", data="", updated=7)
        set_saved_nick(row, "Новый", now_ms=123_000)
        self.assertEqual((row.nick, row.data, row.updated), ("Новый", "", 7))

    def test_nickname_validation_matches_player_rules(self):
        self.assertTrue(valid_nick("Новый-7"))
        self.assertFalse(valid_nick("ab"))
        self.assertFalse(valid_nick("bad!name"))


if __name__ == "__main__":
    unittest.main()
