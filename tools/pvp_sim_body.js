// ---- PvP-симуляция по формулам игры (tools/pvp_sim.js) ----
// Классы с одинаковым снаряжением своего уровня (золото +10, умения 5 ур.), бой «стоя в радиусе»: позиционирование,
// уклонение от умений и замедления не моделируются. Учитываются: предел удара сервера (уровень × множитель класса,
// умение ×4), щиты (сервер), броня K/(K+броня), CP впереди прочности, снижение урона (до 45%), крит, перезарядки и
// усиления умений, оглушение пилотов (с потолком сервера), лечение Ремонтника (в PvP −40%, себя ×0,4), вампиризм,
// регенерация. Умения по пилотам бьют одну цель (как pvpStrike в игре), не чаще раза в 0,85 с.
const SIM = run(`(() => {
  const SLOTS = ["weapon","head","armor","module","core","legs"];
  const PK = {}; for(const [c, C] of Object.entries(CLASSES)) PK[c] = C.pk || 1;
  function build(cls, L){
    S.cls = cls; S.level = L; S.max_hp = 50 + 10*(L-1); S.attack = 6 + 2*(L-1); S.defense = 2 + (L-1);
    S.pas = {}; S.up = {}; S.pots = {}; S.inv = []; S.store = []; S.drone = S.art = S.wing = S.cloak = null; S.gemCx = {};
    S.skl = {}; for(const s of CLASSES[cls].skills) S.skl[s.id] = 5;
    S.eq = {};
    for(const sl of SLOTS){
      const cand = GEAR_IDS.filter(id => { const it = ITEMS[id]; return it.slot === sl && (it.lvl || 0) <= L && (!it.cls || it.cls === cls); });
      const top = Math.max(...cand.map(id => ITEMS[id].tier || 0));
      const id = cand.find(id => ITEMS[id].tier === top);
      if(id) S.eq[sl] = {id, n:1, g:3, e:10, r:[]};
    }
    const st = stat(), C = CLASSES[cls];
    return {cls, L, hp:st.maxhp, cp:st.maxcp, def:st.def, atk:st.atk, aps:C.rate*st.rate, crit:st.crit, critPow:st.critPow,
      red:Math.min(0.45, (st.dmgRed || 0) + (st.pvpRed || 0)), regen:st.regen || 0, skillDmg:st.skillDmg || 0, cdr:st.cdr || 0, pk:PK[cls],
      skills:C.skills.map(s => { const a = skillAt(s, 5); return {id:a.id, type:a.type, mult:a.mult || 0, n:a.n || 0, cd:a.cd, buff:a.buff, v:a.v || 0, buff2:a.buff2, v2:a.v2 || 0,
        dur:a.dur || 0, pstun:a.pstun || 0, pshield:a.pshield || 0, heal:a.heal || 0, radius:a.radius || 0, pvpAs:a.pvpAs || ""}; })};
  }
  const out = {};
  for(const L of [20, 35, 50]) out[L] = Object.keys(CLASSES).map(c => build(c, L));
  return JSON.stringify({builds:out, defK:PVP_DEF_K, medPvp:MED_PVP, medSelf:MED_PVP_SELF, cpAlly:CP_HEAL, cpSelf:CP_HEAL_PVP_SELF, shieldMax:MED_SHIELD_MAX, siphonMax:MED_SIPHON_MAX});
})()`);
module.exports = {SIM:JSON.parse(SIM)};
