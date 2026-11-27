// Симуляция PvP: дуэли всех классов и массовые бои групп по формулам игры и сервера.
// Запуск: node tools/pvp_sim.js [game.html]  → таблицы в консоль (и JSON с --json)
// Формулы берутся из game.html (stat(), skillAt) через ту же «песочницу», что tools/stat_caps.js;
// пределы удара и оглушения — как webserver.handle_pvp (PVP_HIT_K, PVP_SKILL_STUN), броня — pvpguard.def_mult.
const fs = require("fs"), path = require("path");
const harness = fs.readFileSync(path.join(__dirname, "stat_caps.js"), "utf8");
const head = harness.slice(0, harness.indexOf("\n", harness.indexOf("const run = ")));
const body = fs.readFileSync(path.join(__dirname, "pvp_sim_body.js"), "utf8");
const m = {exports:{}};
new Function("require", "process", "module", "__dirname", head + "\n" + body)(require, {...process, argv:[process.argv[0], "x", process.argv[2] || path.join(__dirname, "..", "game.html")]}, m, __dirname);
const {SIM} = m.exports;
const STUN_CAP = {guard:0.9, techno:0.7};                        // webserver.PVP_SKILL_STUN
const DAMAGE = new Set(["strike", "aoe", "leap", "volley", "burst", "phase", "siren", "gravity", "chain", "siphon"]);

let seed = 12345; const rnd = () => (seed = (seed*1103515245 + 12345) >>> 0)/4294967296;

function mk(b, team){
  return {b, team, hp:b.hp, cp:b.cp, cd:[0,0,0,0], atkT:rnd()/b.aps, skT:0, stun:0, buffs:{}, shield:0, shieldT:0, stealth:false, dead:false,
    dealt:0, healed:0, field:null};
}
const buff = (f, k) => (f.buffs[k] && f.buffs[k].t > 0) ? f.buffs[k].v : 0;
function hit(att, vic, raw, skill, crit, log){
  const L = att.b.L;
  const cap = (40 + L*8)*att.b.pk*(skill ? 4 : 1);
  let dmg = Math.max(1, Math.min(Math.round(raw), Math.floor(cap)));
  if(vic.shieldT > 0) dmg = Math.max(1, Math.floor(dmg*(1 - vic.shield)));
  const K = SIM.defK*(40 + 8*vic.b.L), eff = Math.max(1, Math.round(dmg*K/(K + Math.max(0, vic.b.def + vic.b.def*buff(vic, "def")))));
  const abs = Math.min(vic.cp, eff); vic.cp -= abs; let rest = eff - abs;
  if(rest > 0){ rest = Math.max(1, Math.round(rest*(1 - vic.b.red))); vic.hp -= rest; }
  att.dealt += eff;
  const st = buff(att, "steal"); if(st && !skill) heal(att, att, eff*st, true);
  if(vic.hp <= 0){ vic.dead = true; }
  return eff;
}
function heal(src, tgt, amount, self){
  if(tgt.dead) return;
  const a = self ? amount*SIM.medSelf : amount;
  const got = Math.min(a, tgt.b.hp - tgt.hp); tgt.hp += got; src.healed += got;
}
function pickFoe(f, foes){ return foes.filter(x => !x.dead).sort((a, b) => (a.hp + a.cp) - (b.hp + b.cp))[0]; }
function pickAlly(f, allies){ return allies.filter(x => !x.dead).sort((a, b) => a.hp/a.b.hp - b.hp/b.b.hp)[0]; }

function step(f, allies, foes, dt, t){
  for(const k in f.buffs) f.buffs[k].t -= dt;
  if(f.shieldT > 0) f.shieldT -= dt;
  for(let i = 0; i < 4; i++) f.cd[i] = Math.max(0, f.cd[i] - dt);
  f.skT = Math.max(0, f.skT - dt);
  if(f.field){ f.field.t -= dt; f.field.acc += dt; if(f.field.acc >= 1){ f.field.acc -= 1; for(const a of allies) heal(f, a, a.b.hp*f.field.v*(a === f ? 1 : SIM.medPvp), a === f); } if(f.field.t <= 0) f.field = null; }
  if(f.b.regen) heal(f, f, f.b.regen*dt, false);
  if(f.stun > 0){ f.stun -= dt; return; }
  const foe = pickFoe(f, foes); if(!foe) return;
  const atk = f.b.atk*(1 + buff(f, "atk"));
  // умения (не чаще раза в 0,85 с — как PVP_SK_GAP)
  if(f.skT <= 0){
    for(let i = 0; i < 4; i++){
      const s = f.b.skills[i]; if(f.cd[i] > 0) continue;
      let used = true;
      const ty = s.pvpAs || s.type;
      if(DAMAGE.has(ty)){
        const hits = ty === "burst" ? (s.n || 3) : s.type === "chain" ? 2 : 1;
        let mult = s.mult*hits; let crit = false;
        if(s.type === "phase" && (foe.hp + foe.cp)/(foe.b.hp + foe.b.cp) < 0.5){ mult *= f.b.critPow; crit = true; }
        if(f.stealth){ mult *= 2.5; f.stealth = false; crit = true; }
        const dealt = hit(f, foe, atk*(1 + f.b.skillDmg)*mult, true, crit);
        if(s.pstun && STUN_CAP[f.b.cls]) foe.stun = Math.max(foe.stun, Math.min(s.pstun, STUN_CAP[f.b.cls]));
        if(s.heal) heal(f, f, f.b.hp*s.heal, false);
        if(s.type === "siphon"){ const t2 = pickAlly(f, allies); heal(f, t2, dealt*Math.min(SIM.siphonMax, s.v)*(t2 === f ? 1 : SIM.medPvp), t2 === f); }
      } else if(s.type === "buff"){
        if(f.buffs[s.buff] && f.buffs[s.buff].t > 0){ used = false; }
        else { f.buffs[s.buff] = {v:s.v, t:s.dur}; if(s.buff2) f.buffs[s.buff2] = {v:s.v2, t:s.dur}; if(s.pshield){ f.shield = Math.max(f.shieldT > 0 ? f.shield : 0, s.pshield); f.shieldT = s.dur; } }
      } else if(s.type === "mend"){
        const a = pickAlly(f, allies); if(!a || a.hp/a.b.hp > 0.85) used = false; else heal(f, a, a.b.hp*s.v*(a === f ? 1 : SIM.medPvp), a === f);
      } else if(s.type === "nanofield"){
        if(allies.every(a => a.dead || a.hp/a.b.hp > 0.85)) used = false; else f.field = {v:s.v, t:s.dur, acc:0};
      } else if(s.type === "shieldcast"){
        const a = pickAlly(f, allies); if(!a || a.shieldT > 0) used = false; else { a.shield = Math.min(SIM.shieldMax, s.v); a.shieldT = s.dur; }
      } else if(s.type === "stealth"){ f.stealth = true; }
      else if(s.type === "recall"){ if(f.hp/f.b.hp > 0.6) used = false; else heal(f, f, f.b.hp*s.v, false); }
      else used = false;                                     // рывки, телепорт, двойник — без позиционирования не моделируем
      if(used){ f.cd[i] = s.cd*(1 - f.b.cdr); f.skT = 0.85; break; }
    }
  }
  f.atkT -= dt*(1 + buff(f, "rate"));
  if(f.atkT <= 0){ f.atkT += 1/f.b.aps;
    const crit = rnd()*100 < f.b.crit;
    let mult = crit ? f.b.critPow : 1; if(f.stealth){ mult *= 2.5; f.stealth = false; }
    hit(f, foe, (atk + Math.floor(rnd()*3))*mult, false, crit);
  }
}
function fight(teamA, teamB, maxT = +(process.env.MAXT || 120)){
  const A = teamA.map(b => mk(b, 0)), B = teamB.map(b => mk(b, 1));
  let t = 0; const dt = 0.05;
  while(t < maxT && A.some(x => !x.dead) && B.some(x => !x.dead)){
    const order = [...A, ...B].filter(x => !x.dead).sort(() => rnd() - 0.5);
    for(const f of order){ if(f.dead) continue; step(f, f.team ? B : A, f.team ? A : B, dt, t); }
    t += dt;
  }
  const aAlive = A.filter(x => !x.dead).length, bAlive = B.filter(x => !x.dead).length;
  const hpLeft = l => l.reduce((s, x) => s + Math.max(0, x.hp) + Math.max(0, x.cp), 0)/l.reduce((s, x) => s + x.b.hp + x.b.cp, 0);
  return {win:aAlive && !bAlive ? 0 : bAlive && !aAlive ? 1 : (hpLeft(A) >= hpLeft(B) ? 0 : 1), t, timeout:!!(aAlive && bAlive), margin:hpLeft(A) - hpLeft(B),
    A:A.map(x => ({cls:x.b.cls, dealt:Math.round(x.dealt), healed:Math.round(x.healed), dead:x.dead})), B:B.map(x => ({cls:x.b.cls, dealt:Math.round(x.dealt), healed:Math.round(x.healed), dead:x.dead}))};
}
function duelMatrix(L, n = 60){
  const bs = SIM.builds[L], cls = bs.map(b => b.cls), M = {};
  for(const a of bs){ M[a.cls] = {}; for(const b of bs){ if(a === b){ M[a.cls][b.cls] = null; continue; }
    let w = 0, tt = 0, to = 0, mg = 0; for(let i = 0; i < n; i++){ const r = fight([a], [b]); if(r.win === 0) w++; tt += r.t; if(r.timeout) to++; mg += r.margin; }
    M[a.cls][b.cls] = {win:Math.round(w/n*100), t:Math.round(tt/n), timeouts:to, margin:Math.round(mg/n*100)}; } }
  return {cls, M, stats:Object.fromEntries(bs.map(b => [b.cls, {hp:b.hp, cp:b.cp, def:Math.round(b.def), atk:Math.round(b.atk), aps:+b.aps.toFixed(2), red:+b.red.toFixed(2)}]))};
}
function teams(L, comps, n = 40){
  const by = Object.fromEntries(SIM.builds[L].map(b => [b.cls, b]));
  return comps.map(([a, b]) => { let w = 0, tt = 0; const dealt = {}, healed = {};
    for(let i = 0; i < n; i++){ const r = fight(a.map(c => by[c]), b.map(c => by[c])); if(r.win === 0) w++; tt += r.t;
      for(const x of [...r.A, ...r.B]){ dealt[x.cls] = (dealt[x.cls] || 0) + x.dealt/n; healed[x.cls] = (healed[x.cls] || 0) + x.healed/n; } }
    return {a:a.join("+"), b:b.join("+"), winA:Math.round(w/n*100), t:Math.round(tt/n),
      healed:Object.fromEntries(Object.entries(healed).filter(([, v]) => v > 0).map(([k, v]) => [k, Math.round(v)]))}; });
}
const COMPS = [
  [["guard", "sniper", "medic"], ["reaper", "techno", "glyph"]],
  [["ghost", "sniper", "medic"], ["guard", "reaper", "techno"]],
  [["guard", "glyph", "medic"], ["ghost", "sniper", "techno"]],
  [["reaper", "reaper", "medic"], ["sniper", "sniper", "techno"]],
  [["guard", "reaper", "sniper", "techno", "medic"], ["ghost", "glyph", "sniper", "techno", "reaper"]],
  [["guard", "reaper", "sniper", "techno", "medic"], ["guard", "reaper", "sniper", "techno", "ghost"]],
  [["medic", "medic", "guard"], ["reaper", "ghost", "sniper"]],
];
const res = {duels:{}, teams:{}};
for(const L of [20, 35, 50]){ res.duels[L] = duelMatrix(L); res.teams[L] = teams(L, COMPS); }
if(process.argv.includes("--json")){ process.stdout.write(JSON.stringify(res)); }
else {
  for(const L of [20, 35, 50]){
    const D = res.duels[L];
    console.log(`\n=== Уровень ${L}: дуэли (% побед строки над столбцом, 60 боёв) ===`);
    console.log("            " + D.cls.map(c => c.padStart(7)).join(""));
    for(const a of D.cls) console.log(a.padEnd(12) + D.cls.map(b => (D.M[a][b] ? String(D.M[a][b].win) : "—").padStart(7)).join("") + "   средн. " + Math.round(D.cls.filter(b => b !== a).reduce((s, b) => s + D.M[a][b].win, 0)/6) + "%");
    console.log("Запас в конце дуэли (% прочности+CP строки минус столбца; >0 — строка сильнее):");
    for(const a of D.cls) console.log(a.padEnd(12) + D.cls.map(b => (D.M[a][b] ? String(D.M[a][b].margin) : "—").padStart(7)).join("") + "   средн. " + Math.round(D.cls.filter(b => b !== a).reduce((s, b) => s + D.M[a][b].margin, 0)/6));
    if(process.argv.includes("--stats")) console.log("Характеристики:", JSON.stringify(D.stats));
    console.log(`--- Группы (уровень ${L}, 40 боёв) ---`);
    for(const r of res.teams[L]) console.log(`${r.a}  vs  ${r.b}:  победы первой ${r.winA}% · ${r.t} с · лечение ${JSON.stringify(r.healed)}`);
  }
}
