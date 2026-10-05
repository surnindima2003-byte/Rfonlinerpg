"""Права и правила гильдий (check_write в webserver.py) — на настоящем коде, без aiohttp и базы.

Хранилище документов подменено словарём в памяти; прогоняются все действия клиента (создать, вступить,
заявка, принять, вклад, улучшить, передать лидерство, исключить, распустить) и известные обходы.
Запуск: python -m unittest test_guild_rules
"""
import asyncio, json, re, types, sys, unittest
from pathlib import Path
src = (Path(__file__).resolve().parent / "webserver.py").read_text(encoding="utf-8")
class HTTP(Exception):
    def __init__(self, text=""): self.text = text
web = types.SimpleNamespace(HTTPBadRequest=type("Bad", (HTTP,), {}), HTTPForbidden=type("Forb", (HTTP,), {}),
                            HTTPNotFound=type("NF", (HTTP,), {}))
STORE = {}          # path -> dict
class Col:
    def __eq__(self, o): return ("eq", o)
    def like(self, o): return ("like", o)
class DocT: col = Col(); path = Col(); updated = Col()
class Q:
    def __init__(self): self.conds = []
    def where(self, *c): self.conds += c; return self
    def order_by(self, *a): return self
class Res:
    def __init__(self, rows): self.rows = rows
    def scalars(self): return self
    def all(self): return self.rows
class Sess:
    async def execute(self, q):
        rows = []
        for path, d in STORE.items():
            col = path.rsplit("/", 1)[0]
            if all(c[1] == col for c in q.conds if c[0] == "eq"):
                rows.append(types.SimpleNamespace(path=path, data=json.dumps(d), col=col))
        return Res(rows)
ns = {"re": re, "json": json, "web": web, "select": lambda *a: Q(), "Doc": DocT, "time": __import__("time"), "secrets": __import__("secrets")}
a = src.index("GUILD_ICONS ="); b = src.index("async def clean_guilds")
c = src.index('SEG = re.compile'); d = src.index("def doc_view(path, data):")
exec(src[a:b] + src[c:d].replace("async def clean_guilds", "async def _unused_clean"), ns)
async def doc_get(s, path):
    return (True, json.loads(json.dumps(STORE[path]))) if path in STORE else (None, None)
async def guild_members(s, gid):
    return {p.rsplit("/", 1)[1]: v for p, v in STORE.items() if p.startswith(f"guilds/{gid}/members/")}
async def other_guild(s, uid, gid):
    for p in STORE:
        pr = p.split("/")
        if len(pr) == 4 and pr[2] == "members" and pr[3] == str(uid) and pr[1] != gid and f"guilds/{pr[1]}" in STORE:
            return pr[1]
LVL = {}
async def player_lvl(s, uid): return LVL.get(str(uid), 1)
ns.update(doc_get=doc_get, guild_members=guild_members, other_guild=other_guild, player_lvl=player_lvl)
S = Sess()

async def w(op, path, data, uid):
    """Как api_db: проверка + запись (упрощённо, с серверными действиями при удалении участника)."""
    try:
        data = await ns["check_write"](S, op, path, data, uid)
    except HTTP as e:
        return "DENY:" + type(e).__name__ + ":" + e.text
    gid, sub, did = ns["parse_path"](path)
    if op == "delete":
        cur = STORE.get(path)
        if sub is None:
            for p in [p for p in STORE if p == path or p.startswith(path + "/")]: STORE.pop(p)
        else:
            STORE.pop(path, None)
            g = STORE.get(f"guilds/{gid}")
            if sub == "members" and g:
                if g.get("leader") == did:
                    for p in [p for p in STORE if p.startswith(f"guilds/{gid}")]: STORE.pop(p)
                elif cur and cur.get("donated"):
                    g["spent"] = g.get("spent", 0) - cur["donated"]
    elif op == "update":
        if data: STORE[path] = {**STORE[path], **data}
    else:
        STORE[path] = data
    if sub == "members" and f"guilds/{gid}" in STORE:
        STORE[f"guilds/{gid}"]["count"] = len(await guild_members(S, gid))
    return "OK"

def check(label, got, want):
    if not got.startswith(want):
        check.fails.append(f"{label}: ждали {want}, получили {got}")
check.fails = []
member = lambda uid, role="member", **k: {"uid": uid, "nick": "n" + uid, "lvl": 15, "fac": "aegis", "role": role, "joined": 1, "donated": 0, "xp": 0, "bm": 100, **k}

async def main():
    G = "g1"; P = f"guilds/{G}"
    # --- нормальные действия клиента ---
    check("create", await w("set", P, {"name": "Стальной легион", "tag": "СЛ", "emblem": {"icon": "gear", "color": "#F2A93B"}, "desc": "x", "open": True,
                                        "minLvl": 1, "level": 99, "spent": 12345, "count": 50, "leader": "1", "leaderNick": "A", "created": 1}, "1"), "OK")
    check("create forced level/spent", json.dumps([STORE[P]["level"], STORE[P]["spent"]]), "[1, 0]")
    check("leader member", await w("set", P + "/members/1", member("1", "leader", donated=999999), "1"), "OK")
    check("leader donated reset", str(STORE[P + "/members/1"]["donated"]), "0")
    LVL["2"] = 15
    check("open join", await w("set", P + "/members/2", member("2", donated=5_000_000), "2"), "OK")
    check("join donated reset", str(STORE[P + "/members/2"]["donated"]), "0")
    check("count by server", str(STORE[P]["count"]), "2")
    # вклад со страницы больше не проходит — его делает сервер (/api/guild/donate), списав лом из сохранения
    check("client cannot write donated", await w("update", P + "/members/2", {"donated": 3000}, "2"), "DENY")
    check("client cannot lower donated", await w("update", P + "/members/2", {"donated": 0, "xp": 1}, "2"), "DENY")
    S_ = {"scrap": 5000}
    amt, err = ns["donate_in_save"](S_, 3000)
    check("server donate", f"{amt}/{S_['scrap']}/{S_['craftN']}/{err}", "3000/2000/1/None")
    check("server donate capped by scrap", str(ns["donate_in_save"](S_, "all")[0]), "2000")
    check("server donate nothing left", str(ns["donate_in_save"](S_, 10)[1]), "Нет лома")
    check("server donate bad amount", str(ns["donate_in_save"]({"scrap": 9}, -5)[1]), "Неверная")
    STORE[P + "/members/2"]["donated"] = 3000                 # как записал бы сервер
    check("flushXp", await w("update", P + "/members/2", {"xp": 50, "lvl": 15, "nick": "B", "bm": 120}, "2"), "OK")
    check("log add by member", await w("add", P + "/log/a1", {"text": "hi", "ts": 1}, "2"), "OK")
    # улучшение: казна 3000, цена 500
    check("upgrade valid", await w("update", P, {"level": 2, "spent": 500}, "1"), "OK")
    check("upgrade skip level", await w("update", P, {"level": 5, "spent": 1000}, "1"), "DENY")
    check("upgrade wrong price", await w("update", P, {"level": 3, "spent": 500}, "1"), "DENY")
    check("upgrade by member", await w("update", P, {"level": 3, "spent": 1500}, "2"), "DENY")
    STORE[P + "/members/2"]["donated"] = 600     # казна 600-500=100 < 1000
    check("upgrade without bank", await w("update", P, {"level": 3, "spent": 1500}, "1"), "DENY")
    STORE[P + "/members/2"]["donated"] = 3000
    # --- обходы ---
    check("member sets spent (ignored)", await w("update", P, {"spent": -1e9}, "2"), "OK")
    check("spent unchanged", str(STORE[P]["spent"]), "500")
    check("outsider sets count (ignored)", await w("update", P, {"count": 999}, "9"), "OK")
    check("count unchanged", str(STORE[P]["count"]), "2")
    check("leader sets level directly", await w("update", P, {"level": 50}, "1"), "DENY")
    check("transfer to non-member", await w("update", P, {"leader": "9", "leaderNick": "X"}, "1"), "DENY")
    check("bad emblem", await w("update", P, {"emblem": {"icon": "gear", "color": 'red"><x>'}}, "1"), "DENY:Bad")
    check("settings", await w("update", P, {"desc": "new", "minLvl": 10}, "1"), "OK")
    check("settings by member", await w("update", P, {"desc": "hack"}, "2"), "DENY")
    # второй гильдии
    check("create 2nd guild while in guild", await w("set", "guilds/g2", {"name": "Другая", "tag": "DR", "leader": "2", "leaderNick": "B", "open": True}, "2"), "DENY")
    STORE["guilds/g3"] = {"name": "Третья", "tag": "TR", "leader": "7", "open": True, "level": 1, "minLvl": 1}
    check("join 2nd guild", await w("set", "guilds/g3/members/2", member("2"), "2"), "DENY")
    # порог уровня и места
    LVL["3"] = 5
    check("join below minLvl", await w("set", P + "/members/3", member("3"), "3"), "DENY")
    check("request below minLvl", await w("set", P + "/requests/3", {"uid": "3", "nick": "c", "lvl": 5, "ts": 1}, "3"), "DENY")
    LVL["3"] = 12
    STORE[P]["open"] = False
    check("join closed", await w("set", P + "/members/3", member("3"), "3"), "DENY")
    check("request", await w("set", P + "/requests/3", {"uid": "3", "nick": "c", "lvl": 12, "ts": 1, "bm": 5}, "3"), "OK")
    check("accept by leader", await w("set", P + "/members/3", member("3", donated=77777), "1"), "OK")
    check("accepted donated reset", str(STORE[P + "/members/3"]["donated"]), "0")
    check("promote", await w("update", P + "/members/3", {"role": "officer"}, "1"), "OK")
    check("officer kicks leader", await w("delete", P + "/members/1", {}, "3"), "DENY")
    check("officer kicks member", await w("delete", P + "/members/2", {}, "3"), "OK")
    check("kicked donation stays in bank", str(STORE[P]["spent"]), "-2500")
    check("upgrade after kick (spent < 0)", await w("update", P, {"level": 3, "spent": -2500 + 1000}, "1"), "OK")
    # передача лидерства, как в клиенте: роль новому, себе officer, затем guild.leader
    check("transfer: role leader", await w("update", P + "/members/3", {"role": "leader"}, "1"), "OK")
    check("transfer: self officer", await w("update", P + "/members/1", {"role": "officer"}, "1"), "OK")
    check("transfer: guild leader", await w("update", P, {"leader": "3", "leaderNick": "n3"}, "1"), "OK")
    check("old leader lost rights", await w("update", P, {"desc": "x"}, "1"), "DENY")
    # переполнение
    STORE[P]["open"] = True; STORE[P]["minLvl"] = 1
    for i in range(10, 30):
        LVL[str(i)] = 20
        await w("set", P + f"/members/{i}", member(str(i)), str(i))
    check("capacity respected", str(STORE[P]["count"]), str(ns["guild_max"](STORE[P])))
    # лидер удаляет свою запись -> роспуск целиком
    check("leader self-delete", await w("delete", P + "/members/3", {}, "3"), "OK")
    check("guild fully removed", str(sorted(p for p in STORE if p.startswith(P))), "[]")


class GuildRules(unittest.TestCase):
    def test_all_flows(self):
        STORE.clear(); LVL.clear(); check.fails = []
        asyncio.run(main())
        self.assertEqual(check.fails, [])


if __name__ == "__main__":
    unittest.main()
