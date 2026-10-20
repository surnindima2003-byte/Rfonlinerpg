"""Загрузка игры на медленном интернете: из game.html собираются маленькая первая страница и части кода.

Раньше телефон качал всю страницу (2 МБ, ~630 КБ в gzip) одним куском, и пока она не пришла целиком, игрок
видел белый экран: экран загрузки рисует сам код игры в конце файла, а Telegram.WebApp.ready() вызывается из него же.
На медленном мобильном интернете Telegram не дожидался и обрывал загрузку (ERR_CONNECTION_ABORTED).

Теперь (game.html правится как раньше — всё собирает сервер при запуске):
* первой приходит оболочка (~35 КБ в gzip): разметка, стили и маленький загрузчик. Экран загрузки с полоской
  виден сразу, Telegram получает ready() сразу после своего скрипта, шрифты Google экран больше не держат;
* код игры качается частями (~25 КБ в gzip каждая, по 3 одновременно). Оборвалась связь — повторяется
  только эта часть, а не всё заново;
* часть называется по хэшу содержимого и кэшируется навсегда (кэш браузера + Cache Storage): следующий вход
  берёт код с телефона, а после обновления игры скачиваются только изменившиеся части.
"""
import hashlib
import json
import math
import re

PART_BYTES = 80_000          # примерно столько байт кода в одной части (~25 КБ в gzip)
TG_SRC = "https://telegram.org/js/telegram-web-app.js"
PART_RE = re.compile(r"^[0-9a-f]{16}\.(js|css)$")

BOOT_CSS = """#boot{position:fixed;inset:0;z-index:2147483000;background:#07090D;color:#E4DED1;display:flex;flex-direction:column;
align-items:center;justify-content:center;gap:14px;padding:24px;font:500 15px/1.4 system-ui,-apple-system,Roboto,sans-serif;text-align:center}
#boot b{font:900 30px/1 system-ui,sans-serif;letter-spacing:6px;color:#F2A93B}
#boot .bt-bar{width:min(260px,70vw);height:8px;border-radius:4px;background:#1B232C;overflow:hidden}
#boot .bt-bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#E0694A,#F2A93B);transition:width .3s}
#boot .bt-pct{font-size:13px;color:#93A0AC}
#boot .bt-msg{min-height:20px;color:#93A0AC;font-size:13px;max-width:300px}"""

BOOT_HTML = ('<div id="boot"><b>METALWAR</b><div class="bt-bar"><i id="btFill"></i></div>'
             '<div class="bt-pct" id="btPct">0%</div><div class="bt-msg" id="btMsg"></div></div>')

# Сторож в самом начале страницы: если сама страница пришла обрезанной (связь оборвалась на ней) и загрузчик в её
# конце так и не запустился — перезагружаемся (не больше 8 раз подряд, с паузой). Без него игрок оставался
# на экране загрузки с 0% навсегда.
BOOT_GUARD = """<script>(function(){function chk(){if(window.MW_BOOT)return;var n=0;try{n=+(sessionStorage.getItem("mw-boot-rl")||0);
sessionStorage.setItem("mw-boot-rl",n+1)}catch(e){}if(n<8)setTimeout(function(){location.reload()},1000+1000*n)}
window.addEventListener("load",chk);setTimeout(chk,12000)})();</script>"""

# Загрузчик. Только ES5-синтаксис и проверки наличия API: должен работать на любом телефоне.
BOOT_JS = r"""(function(){
window.MW_BOOT = 1;                                                                            // сторож: загрузчик на месте
if(!window.fetch || !window.Promise){ location.replace("/?full=1" + location.hash); return; }   // очень старый браузер — страница целиком
var PARTS = __PARTS__, TG = __TG__, CACHE = "mw-game";
var ru = /^(ru|uk|be|kk)/i.test(navigator.language || "ru");
var TXT = ru ? {load:"Загрузка игры…", retry:"Связь прерывается — докачиваю…", slow:"Медленный интернет: первый запуск может занять минуту. Дальше игра откроется сразу.", tg:"Подключаюсь к Telegram…"}
             : {load:"Loading the game…", retry:"Connection is unstable — resuming…", slow:"Slow internet: the first launch may take a minute. Next time the game opens instantly.", tg:"Connecting to Telegram…"};
var $ = function(id){ return document.getElementById(id); };
var total = 0, i; for(i = 0; i < PARTS.length; i++) total += PARTS[i].b;
var got = [], texts = [], left = PARTS.length, next = 0, ran = false, tgDone = !TG, t0 = Date.now(), retried = false;
for(i = 0; i < PARTS.length; i++) got.push(0);
function msg(t){ var m = $("btMsg"); if(m) m.textContent = t; }
function show(){
  var s = 0; for(var k = 0; k < got.length; k++) s += got[k];
  var v = Math.min(100, Math.floor(s/Math.max(1, total)*100)), f = $("btFill"), p = $("btPct");
  if(f) f.style.width = v + "%"; if(p) p.textContent = v + "%";
}
msg(TXT.load);
setTimeout(function(){ if(!ran) msg(retried ? TXT.retry + " " + TXT.slow : TXT.slow); }, 15000);
// скрипт Telegram — параллельно с кодом игры; ready() сразу: Telegram убирает свою заглушку и показывает загрузку
function loadTg(n){
  var s = document.createElement("script"); s.src = TG;
  s.onload = function(){ tgDone = true; try{ Telegram.WebApp.ready(); Telegram.WebApp.expand(); }catch(e){} run(); };
  s.onerror = function(){ if(s.parentNode) s.parentNode.removeChild(s);
    if(n < 4) setTimeout(function(){ loadTg(n + 1); }, 1500*n); else { tgDone = true; run(); } };
  document.head.appendChild(s);
}
if(TG) loadTg(1);
var cacheP = Promise.resolve(null);
try{ if(window.caches && caches.open) cacheP = caches.open(CACHE).then(null, function(){ return null; }); }catch(e){}
function wait(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
function readText(r, k){
  if(!r.body || !r.body.getReader || !window.TextDecoder) return r.text();
  var rd = r.body.getReader(), dec = new TextDecoder("utf-8"), out = [], n = 0;
  function pump(){ return rd.read().then(function(x){
    if(x.done){ out.push(dec.decode()); return out.join(""); }
    n += x.value.length; got[k] = Math.min(n, PARTS[k].b); show(); out.push(dec.decode(x.value, {stream:true})); return pump(); }); }
  return pump();
}
function fromNet(k, c){
  var P = PARTS[k], ctl = window.AbortController ? new AbortController() : null;
  var timer = ctl ? setTimeout(function(){ ctl.abort(); }, 60000) : 0;
  return fetch(P.u, ctl ? {signal:ctl.signal} : {}).then(function(r){
    if(r.status === 404) throw {gone:true};
    if(!r.ok) throw new Error("HTTP " + r.status);
    var copy = null; try{ copy = c ? r.clone() : null; }catch(e){}
    return readText(r, k).then(function(t){
      clearTimeout(timer);
      if(t.length !== P.n) throw new Error("size");
      if(copy) c.put(P.u, copy).then(null, function(){});
      return t;
    });
  }, function(e){ clearTimeout(timer); throw e; });
}
function getPart(k, attempt){
  var P = PARTS[k];
  return cacheP.then(function(c){
    var hit = c ? c.match(P.u).then(function(r){ return r ? r.text() : null; }, function(){ return null; }) : Promise.resolve(null);
    return hit.then(function(t){
      if(t !== null && t.length === P.n) return t;
      if(t !== null && c) c["delete"](P.u).then(null, function(){});
      return fromNet(k, c);
    });
  }).then(function(t){ got[k] = P.b; show(); return t; }, function(e){
    got[k] = 0; show();
    // сервер обновился, пока шла загрузка: частей этой версии больше нет — берём новую страницу (один раз)
    if(e && e.gone){ var f = "mw-boot-reload"; try{ if(!sessionStorage.getItem(f)){ sessionStorage.setItem(f, "1"); location.reload(); return new Promise(function(){}); } }catch(x){} }
    retried = true; msg(TXT.retry + (Date.now() - t0 > 15000 ? " " + TXT.slow : ""));
    return wait(Math.min(10000, 800*attempt)).then(function(){ return getPart(k, attempt + 1); });
  });
}
function worker(){
  if(next >= PARTS.length) return;
  var k = next++;
  getPart(k, 1).then(function(t){ texts[k] = t; left--; if(left === 0) run(); else worker(); });
}
function run(){
  if(ran || left > 0) return;
  if(!tgDone){ msg(TXT.tg); return; }
  if(document.readyState === "loading"){ document.addEventListener("DOMContentLoaded", run); return; }
  ran = true;
  try{ sessionStorage.removeItem("mw-boot-reload"); sessionStorage.removeItem("mw-boot-rl"); }catch(e){}
  // стили игры — до её кода (код при запуске уже меряет экран)
  var css = [], js = [];
  for(var k = 0; k < PARTS.length; k++) (PARTS[k].css ? css : js).push(texts[k]);
  texts = null;
  if(css.length){ var st = document.createElement("style"); st.textContent = css.join(""); document.head.appendChild(st); }
  var s = document.createElement("script"); s.textContent = js.join(""); js = null;
  document.body.appendChild(s);
  var b = $("boot"); if(b && b.parentNode) b.parentNode.removeChild(b);
  // старые части (от прошлых версий игры) из Cache Storage — удаляем
  cacheP.then(function(c){ if(!c) return; var keep = {}; for(var j = 0; j < PARTS.length; j++) keep[PARTS[j].u] = 1;
    c.keys().then(function(ks){ ks.forEach(function(q){ var u = q.url.replace(/^https?:\/\/[^\/]+/, ""); if(!keep[u]) c["delete"](q); }); }, function(){}); });
}
if(!PARTS.length){ left = 0; run(); }
for(i = 0; i < 3; i++) worker();
})();"""


def _utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


def split_code(code, part_bytes=PART_BYTES):
    """Код игры — на части примерно равного размера в байтах (по символам: многобайтовые буквы не режутся)."""
    total = len(code.encode("utf-8"))
    n = max(1, math.ceil(total / part_bytes))
    step = math.ceil(len(code) / n)
    return [code[i:i + step] for i in range(0, len(code), step)]


def build(html):
    """(оболочка, {имя части: текст части}) или None, если страница устроена не так, как ждём (тогда отдаём её целиком)."""
    start, end = html.rfind("<script>"), html.rfind("</script>")
    body_at = html.find("<body>")
    if start < 0 or end < start or body_at < 0 or start < body_at:
        return None
    code = html[start + len("<script>"):end]
    if "<script" in code or len(code) < 10_000:
        return None
    head, body, tail = html[:body_at], html[body_at + len("<body>"):start], html[end + len("</script>"):]
    # скрипт Telegram теперь грузит загрузчик (параллельно с игрой), а не блокирует страницу
    tg_tag = f'<script src="{TG_SRC}"></script>'
    tg = TG_SRC if tg_tag in head else ""
    head = head.replace(tg_tag, "")
    # шрифты Google: не держат первый показ (раньше без них браузер не рисовал ничего)
    head = re.sub(r'<link (href="https://fonts\.googleapis\.com/[^"]*") rel="stylesheet">',
                  lambda m: f'<link {m.group(1)} rel="stylesheet" media="print" onload="this.media=' + "'all'" + '">', head)
    # большой блок стилей (~150 КБ) — тоже часть, кэшируемая навсегда: первая страница остаётся крошечной
    # (чем она меньше, тем меньше шанс, что связь оборвётся прямо на ней)
    styles = ""
    m = re.search(r"<style>(.*?)</style>", head, re.S)
    if m:
        styles, head = m.group(1), head[:m.start()] + head[m.end():]
    if "</head>" not in head:
        return None
    head = head.replace("</head>", f"<style>{BOOT_CSS}</style>\n</head>", 1)
    parts, manifest = {}, []

    def add(text, ext):
        name = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16] + "." + ext
        parts[name] = text
        item = {"u": "/g/" + name, "n": _utf16_len(text), "b": len(text.encode("utf-8"))}
        if ext == "css":
            item["css"] = 1
        manifest.append(item)
    if styles:
        add(styles, "css")
    for text in split_code(code):
        add(text, "js")
    loader = BOOT_JS.replace("__PARTS__", json.dumps(manifest, separators=(",", ":"))).replace("__TG__", json.dumps(tg))
    shell = f"{head}<body>\n{BOOT_HTML}\n{BOOT_GUARD}{body}<script>{loader}</script>{tail}"
    return shell, parts
