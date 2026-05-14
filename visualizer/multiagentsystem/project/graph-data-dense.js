// Dense synthetic data for CBS Coordinator v2.
// ~70 nodes, ~160 edges, 50 agents distributed across hot/normal nodes.

// ─── 71 module-clustered files ──────────────────────────────
const _denseList = [
  ['srv',   'api/server.py',                'api'],
  ['rAg',   'api/routes/agents.py',         'api'],
  ['rGr',   'api/routes/graph.py',          'api'],
  ['rEv',   'api/routes/events.py',         'api'],
  ['rCx',   'api/routes/conflicts.py',      'api'],
  ['rHl',   'api/routes/health.py',         'api'],
  ['mAu',   'api/middleware/auth.py',       'api'],
  ['mRl',   'api/middleware/ratelimit.py',  'api'],

  ['coord', 'core/coordinator.py',          'core'],
  ['plan',  'core/planner.py',              'core'],
  ['sched', 'core/scheduler.py',            'core'],
  ['disp',  'core/dispatcher.py',           'core'],
  ['pol',   'core/policy.py',               'core'],
  ['cq',    'core/queue.py',                'core'],
  ['rt',    'core/runtime.py',              'core'],
  ['reg',   'core/registry.py',             'core'],
  ['tick',  'core/tick.py',                 'core'],
  ['rep',   'core/replay.py',               'core'],
  ['snap',  'core/snapshot.py',             'core'],
  ['life',  'core/lifecycle.py',            'core'],

  ['cbsS',  'core/cbs/search.py',           'cbs'],
  ['cbsE',  'core/cbs/expand.py',           'cbs'],
  ['cbsC',  'core/cbs/constraints.py',      'cbs'],
  ['cbsX',  'core/cbs/conflicts.py',        'cbs'],
  ['cbsH',  'core/cbs/heuristics.py',       'cbs'],
  ['cbsN',  'core/cbs/ct_node.py',          'cbs'],
  ['cbsL',  'core/cbs/low_level.py',        'cbs'],
  ['cbsB',  'core/cbs/bypass.py',           'cbs'],
  ['cbsD',  'core/cbs/disjoint.py',         'cbs'],
  ['cbsP',  'core/cbs/priorities.py',       'cbs'],
  ['cbsR',  'core/cbs/resolver.py',         'cbs'],
  ['cbsCa', 'core/cbs/cascade.py',          'cbs'],
  ['cbsA',  'core/cbs/admissible.py',       'cbs'],
  ['cbsW',  'core/cbs/wdg.py',              'cbs'],
  ['cbsI',  'core/cbs/icbs.py',             'cbs'],

  ['aRun',  'agents/runner.py',             'agents'],
  ['aHook', 'agents/hooks.py',              'agents'],
  ['aSt',   'agents/state.py',              'agents'],
  ['aIo',   'agents/io.py',                 'agents'],
  ['aTl',   'agents/tools.py',              'agents'],
  ['aSp',   'agents/spawn.py',              'agents'],
  ['aLf',   'agents/lifecycle.py',          'agents'],
  ['aPo',   'agents/policy.py',             'agents'],
  ['aEv',   'agents/eval.py',               'agents'],

  ['gB',    'graph/builder.py',             'graph'],
  ['gD',    'graph/diff.py',                'graph'],
  ['gC',    'graph/cache.py',               'graph'],
  ['gP',    'graph/parser.py',              'graph'],
  ['gW',    'graph/walker.py',              'graph'],
  ['gI',    'graph/index.py',               'graph'],
  ['gE',    'graph/edges.py',               'graph'],
  ['gSy',   'graph/symbols.py',             'graph'],

  ['uLk',   'utils/locks.py',               'utils'],
  ['uQ',    'utils/queue.py',               'utils'],
  ['uM',    'utils/metrics.py',             'utils'],
  ['uCk',   'utils/clock.py',               'utils'],
  ['uLg',   'utils/log.py',                 'utils'],
  ['uRb',   'utils/ringbuf.py',             'utils'],
  ['uRt',   'utils/retry.py',               'utils'],

  ['scA',   'schemas/agent.py',             'schemas'],
  ['scC',   'schemas/conflict.py',          'schemas'],
  ['scE',   'schemas/event.py',             'schemas'],
  ['scT',   'schemas/task.py',              'schemas'],
  ['scS',   'schemas/snapshot.py',          'schemas'],

  ['dbS',   'db/store.py',                  'db'],
  ['dbM',   'db/migrations.py',             'db'],
  ['dbC',   'db/cache.py',                  'db'],
  ['dbI',   'db/index.py',                  'db'],
  ['dbW',   'db/wal.py',                    'db'],

  ['tCBS',  'tests/cbs_test.py',            'tests'],
  ['tCor',  'tests/coord_test.py',          'tests'],
  ['tGr',   'tests/graph_test.py',          'tests'],
  ['tAg',   'tests/agent_test.py',          'tests'],
];

// ─── cluster centers for a 1100×680 viewport ───────────────
const _clusters = {
  api:     { cx: 110, cy: 195, r: 95  },
  schemas: { cx: 470, cy: 80,  r: 75  },
  core:    { cx: 470, cy: 285, r: 130 },
  cbs:     { cx: 340, cy: 430, r: 150 },
  agents:  { cx: 200, cy: 590, r: 115 },
  tests:   { cx: 540, cy: 600, r: 65  },
  db:      { cx: 720, cy: 580, r: 80  },
  utils:   { cx: 880, cy: 480, r: 100 },
  graph:   { cx: 920, cy: 220, r: 135 },
};

// sunflower-pack nodes inside their cluster
function _pack(list) {
  const grouped = {};
  list.forEach(([id, label, mod]) => (grouped[mod] = grouped[mod] || []).push({ id, label, mod }));
  const out = [];
  for (const mod in grouped) {
    const items = grouped[mod];
    const c = _clusters[mod];
    items.forEach((n, i) => {
      const t = (i + 0.5) / items.length;
      const r = c.r * Math.sqrt(t) * 0.92;
      const angle = i * 2.39996323;          // golden angle
      n.x = c.cx + Math.cos(angle) * r;
      n.y = c.cy + Math.sin(angle) * r;
      out.push(n);
    });
  }
  return out;
}

window.CBS_NODES_DENSE = _pack(_denseList);

// expose cluster centers for graph background labels
window.CBS_CLUSTERS_DENSE = Object.entries(_clusters).map(([name, c]) => ({ name, cx: c.cx, cy: c.cy, r: c.r }));

// hot / queue / agent counts on selected nodes (rest get small numbers)
const _hotMap = {
  cbsS:  { agents: 6, queue: 5, hot: true  },
  cbsX:  { agents: 5, queue: 6, hot: true  },
  cbsR:  { agents: 4, queue: 4, hot: true  },
  cbsH:  { agents: 4, queue: 3, hot: true  },
  cbsP:  { agents: 3, queue: 3, hot: false },
  cbsCa: { agents: 3, queue: 2, hot: false },
  coord: { agents: 4, queue: 3, hot: true  },
  plan:  { agents: 3, queue: 2, hot: false },
  sched: { agents: 3, queue: 4, hot: true  },
  disp:  { agents: 2, queue: 1, hot: false },
  aRun:  { agents: 3, queue: 1, hot: false },
  aHook: { agents: 2, queue: 1, hot: false },
  gB:    { agents: 2, queue: 2, hot: false },
  gI:    { agents: 2, queue: 1, hot: false },
  dbS:   { agents: 2, queue: 2, hot: false },
  scA:   { agents: 1, queue: 0, hot: false },
  scC:   { agents: 2, queue: 1, hot: false },
  uLk:   { agents: 1, queue: 3, hot: false },
  uQ:    { agents: 1, queue: 2, hot: false },
};
window.CBS_NODES_DENSE.forEach(n => {
  const h = _hotMap[n.id];
  if (h) { n.agents = h.agents; n.queue = h.queue; n.hot = h.hot; }
  else   { n.agents = 0;        n.queue = 0;       n.hot = false; }
});

// ─── edges by rule ──────────────────────────────────────────
window.CBS_EDGES_DENSE = [
  // api intra
  ['srv','rAg'],['srv','rGr'],['srv','rEv'],['srv','rCx'],['srv','rHl'],
  ['srv','mAu'],['srv','mRl'],['mAu','mRl'],
  // api → core/schemas
  ['rAg','coord'],['rAg','scA'],['rAg','disp'],
  ['rGr','gB'],['rGr','gI'],['rGr','coord'],
  ['rEv','aHook'],['rEv','scE'],['rEv','uRb'],
  ['rCx','cbsX'],['rCx','cbsR'],['rCx','scC'],
  ['rHl','uM'],['mAu','reg'],['mRl','uM'],

  // core intra
  ['coord','plan'],['coord','sched'],['coord','disp'],['coord','pol'],
  ['coord','rt'],['coord','reg'],['coord','tick'],['coord','life'],
  ['plan','sched'],['plan','cq'],['sched','disp'],['sched','cq'],
  ['rt','tick'],['rt','snap'],['rep','snap'],['reg','life'],
  ['pol','plan'],['tick','sched'],
  // core → cbs / utils / schemas / graph
  ['coord','cbsS'],['coord','uM'],['coord','aSp'],['coord','gB'],
  ['plan','cbsS'],['plan','cbsP'],['plan','gB'],['plan','scA'],['plan','scT'],
  ['sched','cbsS'],['sched','uQ'],['sched','uLk'],['sched','dbS'],
  ['disp','aSp'],['disp','aRun'],['disp','uQ'],
  ['pol','scA'],['pol','cbsP'],
  ['rt','aSp'],['rt','uCk'],['rt','uLg'],
  ['reg','dbS'],['reg','scA'],
  ['snap','dbS'],['snap','dbW'],['snap','gB'],
  ['rep','dbW'],['life','aLf'],['life','dbS'],
  ['tick','uCk'],['cq','uQ'],

  // cbs intra (tight)
  ['cbsS','cbsE'],['cbsS','cbsC'],['cbsS','cbsX'],['cbsS','cbsH'],['cbsS','cbsN'],
  ['cbsE','cbsN'],['cbsE','cbsL'],['cbsE','cbsC'],
  ['cbsX','cbsR'],['cbsX','cbsP'],['cbsX','cbsCa'],
  ['cbsH','cbsA'],['cbsH','cbsW'],
  ['cbsL','cbsC'],['cbsR','cbsB'],['cbsR','cbsD'],
  ['cbsB','cbsP'],['cbsD','cbsI'],['cbsCa','cbsR'],
  ['cbsI','cbsS'],['cbsW','cbsP'],['cbsA','cbsH'],
  ['cbsP','cbsR'],['cbsN','cbsC'],
  // cbs → utils / schemas / graph
  ['cbsS','uM'],['cbsS','uCk'],['cbsX','scC'],['cbsC','scC'],
  ['cbsH','gI'],['cbsH','uM'],['cbsR','scC'],['cbsP','scA'],
  ['cbsN','scT'],['cbsCa','aHook'],

  // agents intra
  ['aRun','aSt'],['aRun','aHook'],['aRun','aIo'],['aRun','aTl'],
  ['aSp','aRun'],['aSp','aLf'],['aLf','aSt'],['aPo','aRun'],
  ['aEv','aRun'],['aEv','aPo'],['aHook','aIo'],['aTl','aIo'],
  // agents → others
  ['aRun','coord'],['aRun','scA'],['aRun','uLg'],
  ['aHook','dbS'],['aHook','scE'],['aHook','uRb'],
  ['aSt','dbS'],['aSt','scA'],['aSt','scS'],
  ['aIo','uLg'],['aIo','uRt'],['aIo','dbS'],
  ['aTl','uRt'],['aSp','reg'],['aEv','uM'],['aLf','scA'],
  ['aPo','scA'],

  // graph intra
  ['gB','gD'],['gB','gC'],['gB','gP'],['gB','gW'],['gB','gI'],
  ['gP','gSy'],['gW','gSy'],['gD','gE'],['gI','gC'],['gE','gI'],
  // graph → schemas / utils / db
  ['gB','scT'],['gB','uM'],['gC','dbC'],['gI','dbI'],
  ['gP','uLg'],['gSy','dbS'],

  // utils intra
  ['uQ','uLk'],['uM','uCk'],['uLg','uRb'],['uRt','uLg'],['uLk','uM'],

  // db intra
  ['dbS','dbI'],['dbS','dbW'],['dbS','dbC'],['dbM','dbS'],['dbI','dbC'],

  // tests → targets
  ['tCBS','cbsS'],['tCBS','cbsX'],['tCBS','cbsR'],
  ['tCor','coord'],['tCor','plan'],['tCor','sched'],
  ['tGr','gB'],['tGr','gD'],
  ['tAg','aRun'],['tAg','aHook'],['tAg','aSt'],
];

// ─── 50 agents with discrete (non-curve) metadata ──────────
const _ph2 = ['ALF','BRV','CHR','DLT','ECH','FOX','GLF','HTL','IND','JLT','KIL','LMA','MIK','NOV','OSC','PAP','QBC','RMO','SRA','TGO','UNI','VIC','WSK','XRY','YNK','ZLU'];
const _statuses2 = [
  ...Array(30).fill('running'),
  ...Array(9).fill('planning'),
  ...Array(6).fill('blocked'),
  ...Array(3).fill('replanning'),
  ...Array(2).fill('idle'),
];
function _r2(seed) { let x = seed; return () => { x = (x*1664525+1013904223)>>>0; return x/0xffffffff; }; }
const _rd = _r2(73);

// weight node selection by activity so hot nodes get more agents
const _nodeIds2 = window.CBS_NODES_DENSE.map(n => n.id);
const _nodeWeights = window.CBS_NODES_DENSE.map(n => 1 + (n.agents || 0) * 3 + (n.hot ? 4 : 0));
const _wSum = _nodeWeights.reduce((a,b) => a+b, 0);
function _pickNode(r) {
  let v = r() * _wSum;
  for (let i = 0; i < _nodeIds2.length; i++) {
    v -= _nodeWeights[i];
    if (v <= 0) return _nodeIds2[i];
  }
  return _nodeIds2[_nodeIds2.length-1];
}

const _verbs2 = ['refactor','impl','test','doc','fix','optimize','migrate','review','extend','wire','port'];
const _targets2 = ['conflict resolver','heuristic','priority queue','router','adapter','schema','cache layer','hook handler','migration','search node','constraint','dispatcher','disjoint splitter','bypass check','admissible h','low-level A*'];

window.CBS_AGENTS_DENSE = _statuses2.map((status, i) => {
  const id = `${_ph2[i % _ph2.length]}-${String(Math.floor(i / _ph2.length) * 10 + (i % 10) + 1).padStart(2,'0')}`;
  const ofSteps = 8 + Math.floor(_rd() * 22);
  const step = Math.floor(_rd() * (ofSteps + 1));
  const blocked = status === 'blocked';
  return {
    id,
    status,
    node: _pickNode(_rd),
    task: `${_verbs2[Math.floor(_rd()*_verbs2.length)]} · ${_targets2[Math.floor(_rd()*_targets2.length)]}`,
    step, ofSteps,
    prio: Math.floor(_rd() * 5) + 1,
    ageS: Math.floor(_rd() * 1200) + 5,
    waitMs: blocked ? Math.floor(_rd() * 4000) + 200 : 0,
    blockedBy: blocked ? `${_ph2[Math.floor(_rd()*_ph2.length)]}-${String(Math.floor(_rd()*30)+1).padStart(2,'0')}` : null,
    ctDepth: status === 'replanning' || status === 'planning' ? Math.floor(_rd() * 8) + 2 : 0,
  };
});

// ─── extended event stream ─────────────────────────────────
window.CBS_EVENTS_DENSE = [
  { t: '12:48:03.221', kind: 'CONFLICT',    agent: 'ALF-03', target: 'core/cbs/search.py',     msg: 'vertex conflict @ step 17 with BRV-07' },
  { t: '12:48:02.984', kind: 'PreToolUse',  agent: 'CHR-11', target: 'core/coordinator.py',    msg: 'Edit { lines: 248-291 }' },
  { t: '12:48:02.701', kind: 'REPLAN',      agent: 'BRV-07', target: 'core/cbs/conflicts.py',  msg: 'priority-down · h=14 → h=22 · ct-depth=7' },
  { t: '12:48:02.508', kind: 'RESOLVED',    agent: 'DLT-02', target: 'graph/builder.py',       msg: 'merged · 0 conflicts remaining' },
  { t: '12:48:02.319', kind: 'PostToolUse', agent: 'ECH-09', target: 'agents/hooks.py',        msg: 'patch applied · +24 / −11' },
  { t: '12:48:02.044', kind: 'PreToolUse',  agent: 'FOX-04', target: 'core/cbs/search.py',     msg: 'Read { file_path }' },
  { t: '12:48:01.812', kind: 'LOCK',        agent: 'GLF-06', target: 'utils/locks.py',         msg: 'acquired exclusive on coordinator.py:run' },
  { t: '12:48:01.667', kind: 'CONFLICT',    agent: 'HTL-12', target: 'core/cbs/conflicts.py',  msg: 'edge conflict · waiting on FOX-04' },
  { t: '12:48:01.401', kind: 'SPAWN',       agent: 'IND-03', target: 'agents/runner.py',       msg: 'priority=3 · task=refactor' },
  { t: '12:48:01.205', kind: 'PostToolUse', agent: 'JLT-01', target: 'schemas/conflict.py',    msg: 'patch applied · +8 / −2' },
  { t: '12:48:00.978', kind: 'REPLAN',      agent: 'KIL-08', target: 'core/cbs/search.py',     msg: 'CT-node expanded · depth=7' },
  { t: '12:48:00.612', kind: 'RESOLVED',    agent: 'LMA-05', target: 'agents/state.py',        msg: 'merged · auto-rebased' },
  { t: '12:48:00.404', kind: 'PreToolUse',  agent: 'MIK-02', target: 'core/scheduler.py',      msg: 'Write { line_count: 41 }' },
  { t: '12:48:00.117', kind: 'CONFLICT',    agent: 'NOV-07', target: 'core/coordinator.py',    msg: 'vertex conflict @ step 9 with MIK-02' },
  { t: '12:47:59.881', kind: 'BYPASS',      agent: 'OSC-04', target: 'core/cbs/bypass.py',     msg: 'BC found · skip CT expansion' },
  { t: '12:47:59.624', kind: 'PostToolUse', agent: 'PAP-09', target: 'core/cbs/conflicts.py',  msg: 'patch applied · +52 / −19' },
  { t: '12:47:59.318', kind: 'CONFLICT',    agent: 'QBC-01', target: 'utils/locks.py',         msg: 'edge conflict · GLF-06 holds' },
];

// ─── stats v2 ───────────────────────────────────────────────
window.CBS_STATS_DENSE = {
  active: 50,
  byStatus: { running: 30, planning: 9, blocked: 6, replanning: 3, idle: 2 },
  conflictsOpen: 7,
  conflictsByKind: { vertex: 3, edge: 3, swap: 1 },
  conflictsResolved24h: 184,
  conflictsResolvedKind: { 'priority-shift': 92, 'reroute': 47, 'wait': 32, 'bypass-cut': 9, 'abort': 4 },
  nodes: window.CBS_NODES_DENSE.length,
  edges: window.CBS_EDGES_DENSE.length,
  throughputPerMin: 41.2,
  replans1h: 312,
  tokensBurnK: 1840,
  tokensBurnUsd: 24.71,
  queueDepthMax: 6,
  p50resolveMs: 410,
  p95resolveMs: 2140,
  uptime: '4d 17h 22m',
  ctDepth: 9,
  ctExpanded: 47118,
  ctOpen: 1204,
};

// recent conflict ledger (last resolved)
window.CBS_LEDGER = [
  { id: 'C-7421', kind: 'vertex', pair: 'DLT-02 ↔ ECH-09', node: 'graph/builder.py',      via: 'priority-shift', ms: 280  },
  { id: 'C-7420', kind: 'edge',   pair: 'PAP-09 → JLT-01', node: 'core/cbs/conflicts.py', via: 'reroute',        ms: 612  },
  { id: 'C-7419', kind: 'vertex', pair: 'LMA-05 ↔ KIL-08', node: 'agents/state.py',       via: 'bypass-cut',     ms: 94   },
  { id: 'C-7418', kind: 'swap',   pair: 'CHR-11 ↔ NOV-07', node: 'core/coordinator.py',   via: 'wait',           ms: 1820 },
  { id: 'C-7417', kind: 'edge',   pair: 'GLF-06 → IND-03', node: 'utils/locks.py',        via: 'priority-shift', ms: 410  },
  { id: 'C-7416', kind: 'vertex', pair: 'ALF-03 ↔ BRV-07', node: 'core/cbs/search.py',    via: 'reroute',        ms: 730  },
  { id: 'C-7415', kind: 'edge',   pair: 'OSC-04 → QBC-01', node: 'core/cbs/bypass.py',    via: 'bypass-cut',     ms: 58   },
];

// open conflicts list (richer than v1)
window.CBS_OPEN_CONFLICTS = [
  { kind: 'vertex', pair: 'ALF-03 ↔ BRV-07', node: 'core/cbs/search.py',     age: '4s',  ctd: 7 },
  { kind: 'edge',   pair: 'HTL-12 → FOX-04', node: 'core/cbs/conflicts.py',  age: '7s',  ctd: 5 },
  { kind: 'vertex', pair: 'NOV-07 ↔ MIK-02', node: 'core/coordinator.py',    age: '11s', ctd: 4 },
  { kind: 'swap',   pair: 'CHR-11 ↔ DLT-02', node: 'graph/builder.py',       age: '22s', ctd: 3 },
  { kind: 'edge',   pair: 'KIL-08 → JLT-01', node: 'schemas/conflict.py',    age: '38s', ctd: 6 },
  { kind: 'vertex', pair: 'OSC-04 ↔ PAP-09', node: 'core/cbs/conflicts.py',  age: '52s', ctd: 4 },
  { kind: 'edge',   pair: 'GLF-06 → IND-03', node: 'utils/locks.py',         age: '1m',  ctd: 2 },
];

// ─── time-series · aggregate rate metrics (60 min windows) ──
// generated deterministically; realistic shapes:
//  throughput  — slow ramp-up, currently 41 t/min
//  conflicts/min — noisy around 5-9, occasional spikes
//  ct-expansion — fairly flat, recent uptick (worth alerting on)
//  $/min burn   — smooth oscillation around 0.7-0.9
const _ts = _r2(91);
function _series(base, amp, drift, spikes) {
  const out = [];
  let v = base;
  for (let i = 0; i < 60; i++) {
    v += (_ts() - 0.5) * amp + drift;
    if (spikes && _ts() < spikes.p) v += (_ts() - 0.3) * spikes.a;
    out.push(Math.max(0, v));
  }
  return out;
}

window.CBS_TS = {
  throughput: {
    label: 'Throughput',
    unit:  't/min',
    value: 41.2,
    delta: '+12%',
    deltaDir: 'up',
    deltaGood: true,
    data:  _series(28, 3, 0.22, null),
  },
  conflicts: {
    label: 'Conflicts',
    unit:  '/min',
    value: 6.2,
    delta: '−8%',
    deltaDir: 'down',
    deltaGood: true,
    data:  _series(6, 2.6, 0, { p: 0.10, a: 6 }),
  },
  ctExpand: {
    label: 'CT-tree expansion',
    unit:  'nodes/min',
    value: 312,
    delta: '+22%',
    deltaDir: 'up',
    deltaGood: false,
    data:  (() => {
      const arr = _series(220, 22, 0, null);
      // recent uptick — last 12 points climb
      for (let i = 48; i < 60; i++) arr[i] += (i - 48) * 8;
      return arr;
    })(),
  },
  tokens: {
    label: 'Burn rate',
    unit:  '$/min',
    value: 0.86,
    delta: 'steady',
    deltaDir: 'flat',
    deltaGood: true,
    data:  _series(0.78, 0.10, 0, null),
  },
};
