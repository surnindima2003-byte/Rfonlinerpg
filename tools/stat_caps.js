// Пределы прочности, CP и брони для PvP (pvpguard.HP_AB / DEF_AB) — настоящей формулой stat() из game.html.
//
// Запуск: node tools/stat_caps.js [путь к game.html] > caps.json
// Скрипт выполняет игровой код в node с заглушками браузера и для каждого уровня перебирает классы, лучшее
// снаряжение (золотое +15, обычное и уникальный сет), руны (по 3 одинаковых), лучшие дрона, артефакт, крылья,
// плащ, зелье прочности, пассивки «Живучесть» 10 и «Монолит» 10 (Глифоносец) и полный кодекс. Очки улучшений u — отдельно: на выходе
// для уровня L пара [a, b], где наибольшая прочность при u очках в «Прочность» не больше a + b·u
// (то же для брони и очков в «Броню»). Сервер знает уровень и купленные очки, а остальное берёт по максимуму.
// После правок баланса в game.html перезапусти и перенеси числа в pvpguard.py (test_pvp_caps.py это проверяет).
const fs = require("fs"), vm = require("vm"), path = require("path");
const html = fs.readFileSync(process.argv[2] || path.join(__dirname, "..", "game.html"), "utf8");
const m = [...html.matchAll(/<script(?![^>]*src)[^>]*>([\s\S]*?)<\/script>/g)];
const code = m[0][1];
const handler = {
  get(t, k){ if(k === Symbol.toPrimitive) return () => 0; if(k === "then") return undefined; if(k === "length") return 0;
    if(k === Symbol.iterator) return function*(){}; if(k === "toString" || k === "valueOf") return () => "";
    if(!(k in t.__s)) t.__s[k] = mk(); return t.__s[k]; },
  set(t, k, v){ t.__s[k] = v; return true; },
  apply(){ return mk(); }, construct(){ return mk(); }, has(){ return true; }
};
function mk(){ const f = function(){}; f.__s = {}; return new Proxy(f, handler); }
const store = {};
const ctx = {
  console, Math, Date, JSON, Object, Array, String, Number, Boolean, Symbol, Map, Set, WeakMap, WeakSet, Promise, RegExp, Error, TypeError,
  parseInt, parseFloat, isFinite, isNaN, Intl, encodeURIComponent, decodeURIComponent, Proxy, Reflect, Uint8Array, Float32Array, Int32Array, ArrayBuffer, BigInt,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {}, requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  queueMicrotask: () => {}, fetch: () => new Promise(() => {}), WebSocket: function(){ return mk(); }, Image: function(){ return mk(); }, Audio: function(){ return mk(); },
  AudioContext: function(){ return mk(); }, MutationObserver: function(){ return {observe(){}}; }, ResizeObserver: function(){ return {observe(){}}; },
  IntersectionObserver: function(){ return {observe(){}}; }, performance: {now: () => Date.now()}, navigator: mk(), location: {protocol:"https:", host:"x", search:"", hash:"", reload(){}, href:"https://x/"},
  localStorage: {getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; }},
  sessionStorage: {getItem: () => null, setItem(){}, removeItem(){}}, document: mk(), Telegram: mk(), screen: mk(), devicePixelRatio: 1,
  innerWidth: 400, innerHeight: 800, addEventListener(){}, removeEventListener(){}, matchMedia: () => ({matches:false, addEventListener(){}}),
  getComputedStyle: () => mk(), CSS: mk(), Blob: function(){}, URL: mk(), btoa: s => s, atob: s => s, alert(){}, confirm: () => false, prompt: () => null,
  TextEncoder, TextDecoder, NodeFilter: mk(), Node: mk(), HTMLElement: function(){}, Event: function(){}, CustomEvent: function(){}, KeyboardEvent: function(){}, FormData: function(){}, Headers: function(){}, XMLHttpRequest: function(){ return mk(); }, BroadcastChannel: function(){ return mk(); }, Worker: function(){ return mk(); }, OffscreenCanvas: function(){ return mk(); }, crypto: {getRandomValues: a => a, randomUUID: () => "u"}, Path2D: function(){ return mk(); }, DOMParser: function(){ return mk(); },
};
ctx.document.createTreeWalker = () => ({nextNode: () => null, currentNode: null});
ctx.document.querySelectorAll = () => [];
ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);
try { vm.runInContext(code, ctx, {filename:"game.js"}); } catch(e){ console.error("LOAD ERROR:", e && e.stack ? e.stack.split("\n").slice(0, 4).join("\n") : e); }
const run = s => vm.runInContext(s, ctx);

const out = JSON.parse(run(`(() => {
  const now0 = Date.now();
  cxBonusCache = {atk:0, def:CODEX.reduce((a,c)=>a+(c.bon.def||0),0), hp:CODEX.reduce((a,c)=>a+(c.bon.hp||0),0), crit:0};
  const SLOTS = ["weapon","head","armor","module","core","legs"];
  const UMAX = 300;
  const hpAB = [], defAB = [];
  for(let L = 1; L <= LEVEL_CAP; L++){
    let a = 0, b = 0, da = 0, db = 0;
    for(const cls of Object.keys(CLASSES)){
      const opts = {};
      for(const sl of SLOTS){
        const cand = [...GEAR_IDS, ...UNIQ_IDS].filter(id => { const it = ITEMS[id]; return it.slot === sl && (it.lvl || 0) <= L && (!it.cls || it.cls === cls) && (!it.uniq || it.set === cls); });
        const pick = f => cand.slice().sort((x, y) => (gearStats(y, 3, 15)[f] || 0) - (gearStats(x, 3, 15)[f] || 0))[0];
        const top = Math.max(0, ...cand.filter(id => !ITEMS[id].uniq).map(id => ITEMS[id].tier || 0));
        opts[sl] = [...new Set([pick("hp"), pick("def"), cand.find(id => ITEMS[id].tier === top && !ITEMS[id].uniq), ...cand.filter(id => ITEMS[id].uniq)].filter(Boolean))];
      }
      let combos = [{}];
      for(const sl of SLOTS){ const next = []; for(const c of combos) for(const id of opts[sl]) next.push({...c, [sl]:id}); combos = next; }
      const runeSets = [["r_bastion","r_bastion","r_bastion","r_hp","r_hp","r_hp"], ["r_bastion","r_bastion","r_bastion","r_def","r_def","r_def"]];
      const acc = [["d_sol","a_heart","wg_phoenix","ck_aurora"], ["d_aegis","a_plate","wg_titan","ck_bastion"], ["d_aegis","a_heart","wg_titan","ck_aurora"]];
      for(const c of combos) for(const runes of runeSets) for(const [dr, ar, wg, ck] of acc){
        S.cls = cls; S.level = L; S.max_hp = 50 + 10*(L-1); S.attack = 6 + 2*(L-1); S.defense = 2 + (L-1);
        S.pas = {vital:PAS_MAX, monolith:PAS_MAX}; S.pots = {hp: now0 + 1e9};     // «Монолит» действует только у Глифоносца (pasLv проверяет класс)
        S.inv = [dr, ar, wg, ck].map(id => ({id, n:1})); S.store = [];
        S.eq = {}; SLOTS.forEach((sl, i) => { S.eq[sl] = {id:c[sl], n:1, g:3, e:15, r:[runes[i]]}; });
        S.drone = dr; S.art = ar; S.wing = wg; S.cloak = ck;
        S.up = {}; const s0 = stat();
        S.up = {hp:UMAX, def:UMAX}; const s1 = stat();
        a = Math.max(a, s0.maxhp); b = Math.max(b, (s1.maxhp - s0.maxhp)/UMAX);
        da = Math.max(da, s0.def); db = Math.max(db, (s1.def - s0.def)/UMAX);
      }
    }
    hpAB.push([a, Math.ceil(b*1000)/1000]); defAB.push([da, Math.ceil(db*1000)/1000]);
  }
  return JSON.stringify({hp:hpAB, def:defAB, upMax:UMAX, cpPer:UPGRADES.find(u => u.id === "cpup").per, levelCap:LEVEL_CAP,
    ptsPerLevel:3, packPts:Object.fromEntries(PACKS.filter(p => (p.rew||[]).some(r => r.id === "pts")).map(p => [p.id, p.rew.find(r => r.id === "pts").n]))});
})()`));
process.stdout.write(JSON.stringify(out) + "\n");
