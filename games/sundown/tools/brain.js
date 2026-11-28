// A simple autoplayer. It only produces input snapshots, the same shape a keyboard or pad makes,
// so whatever it does a person could do. Used by the tests, the balance report and the screenshots.
'use strict';

function makeBrain(SD, o) {
  o = o || {};
  const skill = o.skill == null ? 1 : o.skill;   // 0..1: reaction and aim
  const rng = SD.makeRng(o.seed || 99);
  let tick = 0, strafe = 1, strafeT = 0, fireToggle = false, aimErr = 0;
  return {
    think(R) {
      const inp = SD.blankInput(); tick++;
      const p = R.p, W = R.world;
      if (R.dead || R.phase === 'shop') return inp;
      // pick a target: the nearest live enemy, preferring ones in line of sight
      let best = null, bd = 1e9;
      for (const e of R.enemies) {
        if (e.dead || e.spawnT > 0.2) continue;
        let d = SD.dist(p.x, p.y, e.x, e.y);
        if (!e.d.fly && !W.lineClear(p.x, p.y - 4, e.x, e.y - 4)) d += 220;
        if (e.boss) d -= 60;
        if (d < bd) { bd = d; best = e; }
      }
      // threats: enemy bullets heading at us
      let danger = null, dd = 1e9;
      for (const b of R.bullets) {
        if (b.owner !== 'e') continue;
        const dx = p.x - b.x, dy = p.y - b.y, d = Math.hypot(dx, dy);
        if (d > 90) continue;
        const closing = (dx * b.vx + dy * b.vy) / (d * Math.hypot(b.vx, b.vy) + 1e-6);
        if (closing > 0.9 && d < dd) { dd = d; danger = b; }
      }
      for (const bomb of R.bombs) if (bomb.owner === 'e' && SD.dist(p.x, p.y, bomb.x, bomb.y) < bomb.radius + 10 && bomb.fuse < 1.0) { danger = { vx: bomb.x - p.x, vy: bomb.y - p.y, bomb }; dd = 0; }
      // movement: keep a fighting distance, strafe, and sidestep danger
      strafeT -= SD.DT; if (strafeT <= 0) { strafe = -strafe; strafeT = rng.range(0.8, 2.2); }
      let mx = 0, my = 0;
      if (best) {
        const a = SD.ang(p.x, p.y, best.x, best.y), want = best.d.ai === 'rusher' ? 170 : p.cur === 'coach' ? 70 : 150;
        const radial = SD.clamp((bd - want) / 80, -1, 1);
        mx = Math.cos(a) * radial - Math.sin(a) * strafe * 0.8; my = Math.sin(a) * radial + Math.cos(a) * strafe * 0.8;
      } else {
        // wander toward the middle of the street
        const a = SD.ang(p.x, p.y, 820, 540); if (SD.dist(p.x, p.y, 820, 540) > 60) { mx = Math.cos(a); my = Math.sin(a); }
      }
      // stay away from walls of props by nudging toward open ground (the flow field points to us, so use the map centre)
      if (p.x < 60 || p.x > W.w - 60 || p.y < 80 || p.y > W.h - 60) { const a = SD.ang(p.x, p.y, W.w / 2, W.h / 2); mx += Math.cos(a); my += Math.sin(a); }
      if (danger) {
        const a = Math.atan2(danger.vy, danger.vx) + Math.PI / 2 * strafe;
        mx = Math.cos(a); my = Math.sin(a);
        if (danger.bomb) { const a2 = SD.ang(danger.bomb.x, danger.bomb.y, p.x, p.y); mx = Math.cos(a2); my = Math.sin(a2); }
        if (p.dashCd <= 0 && rng() < 0.35 * skill + (danger.bomb ? 0.3 : 0)) inp.dash = true;
      }
      const m = Math.hypot(mx, my); if (m > 1) { mx /= m; my /= m; }
      inp.mx = mx; inp.my = my;
      // aim and fire
      if (best) {
        if (tick % 20 === 0) aimErr = rng.range(-0.12, 0.12) * (1.2 - skill);
        const lead = SD.dist(p.x, p.y, best.x, best.y) / (SD.WEAPONS[p.cur].speed || 600);
        const ty = best.y - (best.d.fly ? (best.d.lift || 0) * 0.5 : 0);
        inp.aimAngle = SD.ang(p.x, p.y, best.x + best.vx * lead, ty + best.vy * lead) + aimErr;
        const los = best.d.fly || W.lineClear(p.x, p.y - 4, best.x, best.y - 4) || SD.WEAPONS[p.cur].rail;
        if (los && bd < 330) {
          const w = SD.WEAPONS[p.cur];
          if (w.auto) inp.fire = true; else { fireToggle = !fireToggle; inp.fire = fireToggle; }
          if (p.cur === 'revolver' && bd < 70 && p.ammo.revolver >= 4 && rng() < 0.05) inp.alt = true;
        }
        // dynamite into a crowd or at a boss
        let near = 0; for (const e of R.enemies) if (!e.dead && SD.dist(best.x, best.y, e.x, e.y) < 70) near++;
        if (p.dynamite > 0 && bd > 90 && bd < 220 && (near >= 3 || (best.boss && rng() < 0.01)) && tick % 30 === 0) inp.dyn = true;
        // focus when it is getting hairy
        inp.focus = p.focus > 25 && (p.hp < 40 || near >= 4 || !!R.boss);
        // weapon choice: the strongest owned gun for the range
        const own = SD.WEAPON_ORDER.filter(k => p.weapons[k]);
        let want = own[own.length - 1];
        if (p.weapons.coach && bd < 80) want = 'coach';
        if (want !== p.cur && tick % 15 === 0 && p.reloadT <= 0) inp.slot = SD.WEAPONS[want].slot;
      }
      if (!best && p.ammo[p.cur] < R.magSize(p.cur) && p.reloadT <= 0) inp.reload = true;
      return inp;
    },
    shop(R) { // spend like a sensible player: guns first, then health, then upgrades
      const order = ['whiskey', 'rail', 'arc', 'winchester', 'coach', 'hollow', 'duster', 'cylinder', 'quick', 'dynamite', 'lucky', 'boots', 'deadeye', 'hollow', 'duster'];
      const bought = [];
      for (let pass = 0; pass < 3; pass++) for (const id of order) {
        const it = SD.SHOP.find(s => s.id === id);
        if (id === 'whiskey' && R.p.hp > R.p.maxHp * 0.7) continue;
        if (id === 'dynamite' && R.p.dynamite >= 4) continue;
        if (!R.canBuy(it) && R.buy(it)) bought.push(id);
      }
      return bought;
    },
  };
}
module.exports = { makeBrain };
