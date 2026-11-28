/* Data: weapons, enemies, waves, the saloon's stock, and how each prop changes when the rift hits.
   Tuning lives here so the balance bot (tools/bot.js) and a human can both read it in one place. */
'use strict';

SD.WEAPONS = {
  revolver: { name: 'PEACEMAKER', slot: 1, dmg: 30, rof: 0.2, mag: 6, reload: 1.25, spread: 0.035, pellets: 1, speed: 620, life: 0.75,
    sfx: 'revolver', kick: 2.2, knock: 50, tracer: '#ffe7a3', alt: 'fan', desc: 'Six shots. Right-click / LB fans the hammer.' },
  coach: { name: 'COACH GUN', slot: 2, dmg: 13, rof: 0.32, mag: 2, reload: 1.55, spread: 0.3, pellets: 8, speed: 520, life: 0.42,
    sfx: 'shotgun', kick: 5, knock: 170, tracer: '#ffd27a', desc: 'Two barrels of bad news up close.' },
  winchester: { name: "WINCHESTER '66", slot: 3, dmg: 68, rof: 0.5, mag: 8, reload: 2.0, spread: 0.008, pellets: 1, speed: 980, life: 0.9,
    pierce: 2, sfx: 'rifle', after: 'lever', kick: 3.4, knock: 90, tracer: '#fff6d8', desc: 'Lever action. Bullets punch through two men.' },
  arc: { name: 'ARC REPEATER', slot: 4, dmg: 17, rof: 0.085, mag: 30, reload: 1.4, spread: 0.06, pellets: 1, speed: 700, life: 0.6, auto: true,
    chain: 2, sfx: 'arc', kick: 1, knock: 20, tracer: '#3ff0ff', rift: true, desc: 'From 2089. Bolts jump to two more targets.' },
  rail: { name: 'RAIL DRIVER', slot: 5, dmg: 160, rof: 0.95, mag: 4, reload: 2.2, spread: 0, pellets: 1, speed: 1800, life: 0.5,
    pierce: 99, sfx: 'rail', kick: 7, knock: 260, tracer: '#ff3ec8', rift: true, rail: true, desc: 'Goes through everything. Everything.' },
};
SD.WEAPON_ORDER = ['revolver', 'coach', 'winchester', 'arc', 'rail'];

SD.DYNAMITE = { fuse: 1.5, radius: 74, dmg: 150, speed: 330, knock: 320 };

SD.ENEMIES = {
  outlaw:  { sheet: 'Outlaw', hp: 55, speed: 58, r: 9, ai: 'gunner', keep: [120, 210], cd: 1.7, burst: 2, burstGap: 0.18, bspeed: 245, dmg: 9, spread: 0.08, bounty: 8, score: 100 },
  rustler: { sheet: 'Rustler', hp: 40, speed: 104, r: 9, ai: 'rusher', melee: 12, meleeCd: 0.8, bounty: 6, score: 80 },
  bandit:  { sheet: 'Bandit', hp: 100, speed: 50, r: 10, ai: 'shotgunner', keep: [50, 120], cd: 2.1, pellets: 5, bspeed: 210, dmg: 6, spread: 0.38, bounty: 12, score: 150 },
  bomber:  { sheet: 'Rustler', hp: 50, speed: 66, r: 9, ai: 'bomber', keep: [140, 230], cd: 3.2, bounty: 12, score: 140, tint: '#ff7a1a' },
  reynolds:{ sheet: 'Reynolds', hp: 1800, speed: 64, r: 14, ai: 'reynolds', boss: true, keep: [110, 200], bspeed: 270, dmg: 10, bounty: 150, score: 5000,
    title: 'JIM REYNOLDS', sub: 'CONFEDERATE RAIDER, COLORADO TERRITORY 1864' },
  glitch:  { sheet: 'Glitch', hp: 70, speed: 72, r: 9, ai: 'glitch', keep: [100, 200], cd: 1.2, burst: 3, burstGap: 0.12, bspeed: 300, dmg: 10, spread: 0.06, bounty: 14, score: 180, rift: true },
  drone:   { sheet: 'HoverDrone', hp: 42, speed: 92, r: 11, ai: 'drone', fly: true, cd: 1.3, bspeed: 230, dmg: 8, bounty: 12, score: 160, rift: true, lift: 18 },
  turret:  { sheet: 'SecurityCam', hp: 120, speed: 0, r: 10, ai: 'turret', cd: 2.0, bspeed: 220, dmg: 8, bounty: 16, score: 200, rift: true, static: true },
  warden:  { sheet: 'HoverDrone', hp: 3600, speed: 55, r: 34, ai: 'warden', fly: true, boss: true, bspeed: 190, dmg: 9, bounty: 400, score: 20000, scale: 2.6, lift: 40, rift: true,
    title: 'THE RAINWIRE WARDEN', sub: 'SECURITY DAEMON, YEAR 2089' },
};

/* Story: one wave per hour from 6 PM. Midnight is Zero Hour: the rift opens at wave 7. */
SD.WAVES = [
  { hour: 0, mix: { outlaw: 6, rustler: 3 }, cap: 6 },
  { hour: 1, mix: { outlaw: 8, rustler: 5, bandit: 2 }, cap: 7 },
  { hour: 2, mix: { outlaw: 8, rustler: 6, bandit: 3, bomber: 2 }, cap: 8 },
  { hour: 3, mix: { outlaw: 10, rustler: 7, bandit: 4, bomber: 3 }, cap: 9 },
  { hour: 4, mix: { outlaw: 12, rustler: 8, bandit: 5, bomber: 4 }, cap: 10 },
  { hour: 5, mix: { outlaw: 6, rustler: 5, bomber: 2 }, cap: 7, boss: 'reynolds' },
  { hour: 6, mix: { glitch: 10, drone: 6, outlaw: 4, turret: 2 }, cap: 10, rift: true },
  { hour: 7, mix: { glitch: 12, drone: 9, rustler: 5, bandit: 3, turret: 3 }, cap: 11, rift: true },
  { hour: 8, mix: { glitch: 14, drone: 10, bandit: 5, bomber: 4, turret: 3 }, cap: 12, rift: true },
  { hour: 9, mix: { drone: 6, glitch: 6 }, cap: 8, boss: 'warden', rift: true },
];
SD.ENDLESS_POOL = ['outlaw', 'rustler', 'bandit', 'bomber', 'glitch', 'drone', 'turret'];
SD.endlessWave = function (n) { // n >= 10
  const k = n - 9, mix = {}, r = SD.rng;
  const total = 26 + k * 4;
  for (let i = 0; i < total; i++) { const t = r.pick(SD.ENDLESS_POOL); mix[t] = (mix[t] || 0) + 1; }
  return { hour: 9 + k, mix, cap: Math.min(16, 10 + Math.floor(k / 2)), rift: true, boss: k % 5 === 0 ? (k % 10 === 0 ? 'warden' : 'reynolds') : null, scale: 1 + k * 0.12 };
};

/* The Last Chance Saloon's stock. max = how many times you can buy it. */
SD.SHOP = [
  { id: 'whiskey', name: 'WHISKEY', cost: 15, max: 99, desc: 'Heal 45. Burns going down.', icon: 'WhiskeyCrate' },
  { id: 'dynamite', name: 'DYNAMITE x2', cost: 20, max: 99, desc: 'Two sticks. Q / RB to throw.', icon: 'DynamiteCrate' },
  { id: 'coach', name: 'COACH GUN', cost: 55, max: 1, desc: SD.WEAPONS.coach.desc, icon: 'Crate', weapon: true },
  { id: 'winchester', name: "WINCHESTER '66", cost: 90, max: 1, desc: SD.WEAPONS.winchester.desc, icon: 'Crate', weapon: true },
  { id: 'hollow', name: 'HOLLOW POINTS', cost: 50, max: 3, desc: '+15% damage, every gun.', icon: 'Barrel' },
  { id: 'cylinder', name: 'BIG IRON', cost: 40, max: 2, desc: '+2 rounds in every gun.', icon: 'Barrel' },
  { id: 'quick', name: 'QUICK HANDS', cost: 40, max: 2, desc: 'Reload 25% faster.', icon: 'HitchingPost' },
  { id: 'duster', name: 'OILSKIN DUSTER', cost: 50, max: 2, desc: '+25 max health, healed now.', icon: 'MinerTent' },
  { id: 'boots', name: 'SPURS', cost: 35, max: 2, desc: 'Move 10% faster. Shorter dash cooldown.', icon: 'WagonWheel' },
  { id: 'lucky', name: 'LUCKY COIN', cost: 40, max: 2, desc: '+10% chance to crit for double.', icon: 'OrePile' },
  { id: 'deadeye', name: 'DEADEYE', cost: 45, max: 2, desc: 'Focus lasts longer and fills faster.', icon: 'WantedBoard' },
  { id: 'arc', name: 'ARC REPEATER', cost: 120, max: 1, desc: SD.WEAPONS.arc.desc, icon: 'JunctionBox', weapon: true, rift: true },
  { id: 'rail', name: 'RAIL DRIVER', cost: 160, max: 1, desc: SD.WEAPONS.rail.desc, icon: 'ServerRack', weapon: true, rift: true },
];

/* Rift swaps: when Zero Hour hits, the 1864 prop at a spot glitches into its 2089 counterpart. */
SD.RIFT_SWAP = {
  Barrel: 'TrashPile', Crate: 'CargoCrate', DynamiteCrate: 'CargoCrate', TelegraphPole: 'CablePole', HitchingPost: 'StreetLamp',
  WaterTrough: 'Barrier', WantedBoard: 'HoloBillboard', Campfire: 'SteamVent', MinerTent: 'NoodleStall', CoveredWagon: 'Dumpster',
  HayBales: 'ACUnits', RainBarrel: 'VendingMachine', SluiceBox: 'StreetKiosk', MineCart: 'JunctionBox', RockSet: 'SatelliteDish',
  OrePile: 'TrashPile', WagonWheel: 'LEDCone', SplitRailFence: 'Barrier',
};

/* Props: footprint (collision box at the feet, in world px), how they take bullets, and drops. */
SD.PROPS = {
  Barrel: { w: 20, h: 12, hp: 60, mat: 'wood', drop: 0.35 },
  Crate: { w: 26, h: 16, hp: 80, mat: 'wood', drop: 0.5 },
  DynamiteCrate: { w: 28, h: 16, hp: 25, mat: 'wood', explode: true },
  HayBales: { w: 36, h: 18, hp: 140, mat: 'hay' },
  CoveredWagon: { w: 120, h: 36, mat: 'wood' },
  MinerTent: { w: 76, h: 30, mat: 'cloth', soft: true },
  MineEntrance: { w: 110, h: 34, mat: 'rock' },
  RockSet: { w: 44, h: 18, mat: 'rock' },
  OrePile: { w: 38, h: 14, mat: 'rock' },
  WaterTrough: { w: 46, h: 14, mat: 'wood' },
  RainBarrel: { w: 22, h: 14, hp: 70, mat: 'wood' },
  HitchingPost: { w: 44, h: 8, mat: 'wood', thin: true },
  TelegraphPole: { w: 8, h: 8, mat: 'wood' },
  WantedBoard: { w: 36, h: 8, mat: 'wood' },
  SplitRailFence: { w: 60, h: 10, mat: 'wood', thin: true },
  SluiceBox: { w: 64, h: 18, mat: 'wood' },
  MineCart: { w: 40, h: 18, mat: 'metal' },
  WagonWheel: { w: 22, h: 10, mat: 'wood' },
  Campfire: { w: 24, h: 14, mat: 'fire', walk: false, light: 'fire' },
  SaloonDoors: { w: 40, h: 10, mat: 'wood' },
  Boardwalk: { w: 0, h: 0, mat: 'floor', flat: true },
  // 2089
  TrashPile: { w: 34, h: 14, mat: 'metal' }, CargoCrate: { w: 36, h: 18, hp: 160, mat: 'metal', drop: 0.5 }, CablePole: { w: 8, h: 8, mat: 'metal', light: 'pink' },
  StreetLamp: { w: 10, h: 8, mat: 'metal', light: 'cyan' }, Barrier: { w: 46, h: 14, mat: 'metal' }, HoloBillboard: { w: 44, h: 10, mat: 'metal', light: 'pink' },
  SteamVent: { w: 34, h: 18, mat: 'metal' }, NoodleStall: { w: 66, h: 26, mat: 'metal', light: 'warm' }, Dumpster: { w: 56, h: 26, mat: 'metal' },
  ACUnits: { w: 36, h: 18, mat: 'metal' }, VendingMachine: { w: 30, h: 14, mat: 'metal', light: 'cyan' }, StreetKiosk: { w: 40, h: 18, mat: 'metal', light: 'pink' },
  JunctionBox: { w: 32, h: 16, mat: 'metal' }, SatelliteDish: { w: 32, h: 16, mat: 'metal' }, LEDCone: { w: 16, h: 10, mat: 'metal', light: 'pink' },
};

SD.TIPS = [
  'Dash through bullets: you cannot be hit mid-dash.',
  'Dynamite crates blow up when shot. Mind your boots.',
  'Hold F / LT to focus: time slows while the meter lasts.',
  'Kills fill the focus meter. Crits fill it faster.',
  'Wooden cover breaks. Rock does not.',
  'At midnight, keep moving. The drones do not care about cover.',
  'Fan the hammer (right-click / LB) to empty the cylinder in a heartbeat.',
  'Spend between waves. Bounty does not follow you to the grave.',
];
