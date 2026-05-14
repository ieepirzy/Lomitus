// Shared synthetic data for the CBS Coordinator mock.
// Two concepts read from the same dataset so screenshots line up.

// ───────────────────────────────────────────────────────────
// Nodes — files/modules in the codebase being planned
// Positions tuned by hand for a 960×560 graph viewport.
// ───────────────────────────────────────────────────────────
window.CBS_NODES = [
  // api cluster (top-left)
  { id: 'srv',   label: 'api/server.py',          mod: 'api',     x: 150, y: 90,  agents: 2, queue: 1, hot: false },
  { id: 'rAg',   label: 'api/routes/agents.py',   mod: 'api',     x: 70,  y: 175, agents: 1, queue: 0, hot: false },
  { id: 'rGr',   label: 'api/routes/graph.py',    mod: 'api',     x: 70,  y: 270, agents: 0, queue: 0, hot: false },
  { id: 'rEv',   label: 'api/routes/events.py',   mod: 'api',     x: 150, y: 355, agents: 1, queue: 1, hot: false },

  // core/cbs cluster (center)
  { id: 'coord', label: 'core/coordinator.py',    mod: 'core',    x: 360, y: 195, agents: 5, queue: 3, hot: true  },
  { id: 'cbsS',  label: 'core/cbs/search.py',     mod: 'cbs',     x: 470, y: 285, agents: 6, queue: 4, hot: true  },
  { id: 'cbsC',  label: 'core/cbs/constraints.py',mod: 'cbs',     x: 360, y: 385, agents: 3, queue: 2, hot: false },
  { id: 'cbsX',  label: 'core/cbs/conflicts.py',  mod: 'cbs',     x: 250, y: 285, agents: 4, queue: 5, hot: true  },
  { id: 'plan',  label: 'core/planner.py',        mod: 'core',    x: 555, y: 175, agents: 3, queue: 1, hot: false },
  { id: 'sched', label: 'core/scheduler.py',      mod: 'core',    x: 575, y: 400, agents: 2, queue: 1, hot: false },

  // agents cluster (bottom-left)
  { id: 'aRun',  label: 'agents/runner.py',       mod: 'agents',  x: 240, y: 480, agents: 4, queue: 1, hot: false },
  { id: 'aHook', label: 'agents/hooks.py',        mod: 'agents',  x: 360, y: 510, agents: 2, queue: 0, hot: false },
  { id: 'aSt',   label: 'agents/state.py',        mod: 'agents',  x: 130, y: 480, agents: 2, queue: 0, hot: false },

  // graph cluster (right)
  { id: 'gB',    label: 'graph/builder.py',       mod: 'graph',   x: 720, y: 230, agents: 3, queue: 1, hot: false },
  { id: 'gD',    label: 'graph/diff.py',          mod: 'graph',   x: 820, y: 145, agents: 2, queue: 0, hot: false },
  { id: 'gC',    label: 'graph/cache.py',         mod: 'graph',   x: 830, y: 320, agents: 1, queue: 0, hot: false },

  // utils cluster (far right / bottom)
  { id: 'uLk',   label: 'utils/locks.py',         mod: 'utils',   x: 720, y: 470, agents: 1, queue: 2, hot: false },
  { id: 'uQ',    label: 'utils/queue.py',         mod: 'utils',   x: 830, y: 430, agents: 2, queue: 1, hot: false },
  { id: 'uM',    label: 'utils/metrics.py',       mod: 'utils',   x: 880, y: 245, agents: 0, queue: 0, hot: false },

  // schemas / db / tests
  { id: 'scA',   label: 'schemas/agent.py',       mod: 'schemas', x: 470, y: 60,  agents: 1, queue: 0, hot: false },
  { id: 'scC',   label: 'schemas/conflict.py',    mod: 'schemas', x: 250, y: 90,  agents: 2, queue: 0, hot: false },
  { id: 'dbS',   label: 'db/store.py',            mod: 'db',      x: 460, y: 510, agents: 2, queue: 1, hot: false },
  { id: 'tCBS',  label: 'tests/cbs_test.py',      mod: 'tests',   x: 605, y: 510, agents: 1, queue: 0, hot: false },
  { id: 'dbM',   label: 'db/migrations.py',       mod: 'db',      x: 555, y: 60,  agents: 1, queue: 0, hot: false },
];

// ───────────────────────────────────────────────────────────
// Edges — directed deps between nodes
// ───────────────────────────────────────────────────────────
window.CBS_EDGES = [
  ['srv','rAg'], ['srv','rGr'], ['srv','rEv'],
  ['rAg','coord'], ['rGr','gB'], ['rEv','aHook'],
  ['coord','cbsS'], ['coord','plan'], ['coord','sched'],
  ['cbsS','cbsC'], ['cbsS','cbsX'], ['cbsX','cbsC'],
  ['plan','cbsS'], ['plan','gB'], ['plan','scA'],
  ['sched','cbsS'], ['sched','uQ'], ['sched','dbS'],
  ['aRun','coord'], ['aRun','aSt'], ['aRun','aHook'],
  ['aHook','dbS'], ['aSt','dbS'],
  ['gB','gD'], ['gB','gC'], ['gD','gC'],
  ['cbsX','scC'], ['cbsC','scC'],
  ['uQ','uLk'], ['sched','uLk'], ['dbS','dbM'],
  ['coord','uM'], ['cbsS','uM'],
  ['tCBS','cbsS'], ['tCBS','cbsX'],
  ['scA','coord'], ['dbS','aRun'],
];

// ───────────────────────────────────────────────────────────
// 50 agents
// ───────────────────────────────────────────────────────────
const _phonetics = ['ALF','BRV','CHR','DLT','ECH','FOX','GLF','HTL','IND','JLT','KIL','LMA','MIK','NOV','OSC','PAP','QBC','RMO','SRA','TGO','UNI','VIC','WSK','XRY','YNK','ZLU'];
const _statuses = [
  // 30 running, 9 planning, 6 blocked, 3 replanning, 2 idle = 50
  ...Array(30).fill('running'),
  ...Array(9).fill('planning'),
  ...Array(6).fill('blocked'),
  ...Array(3).fill('replanning'),
  ...Array(2).fill('idle'),
];
const _nodeIds = window.CBS_NODES.map(n => n.id);
function _rand(seed) { // tiny deterministic prng
  let x = seed;
  return () => { x = (x * 1664525 + 1013904223) >>> 0; return x / 0xffffffff; };
}
const _r = _rand(42);
window.CBS_AGENTS = _statuses.map((status, i) => {
  const id = `${_phonetics[i % _phonetics.length]}-${String(Math.floor(i / _phonetics.length) * 10 + (i % 10) + 1).padStart(2,'0')}`;
  const nodeId = _nodeIds[Math.floor(_r() * _nodeIds.length)];
  return {
    id,
    status,
    node: nodeId,
    task: pickTask(_r),
    tokensK: +(2 + _r() * 38).toFixed(1),
    sinceS: Math.floor(_r() * 600),
    spark: Array.from({length: 16}, () => Math.floor(_r() * 100)),
    prio: Math.floor(_r() * 5) + 1,
  };
});
function pickTask(r) {
  const verbs = ['refactor','impl','test','doc','fix','optimize','migrate','review','extend'];
  const targets = ['conflict resolver','heuristic','priority queue','router','adapter','schema','cache layer','hook handler','migration','search node','constraint','dispatcher'];
  return `${verbs[Math.floor(r()*verbs.length)]} · ${targets[Math.floor(r()*targets.length)]}`;
}

// ───────────────────────────────────────────────────────────
// Hook event stream (most recent first)
// ───────────────────────────────────────────────────────────
window.CBS_EVENTS = [
  { t: '12:48:03.221', kind: 'CONFLICT',    agent: 'ALF-03', target: 'core/cbs/search.py',      msg: 'vertex conflict @ step 17 with BRV-07' },
  { t: '12:48:02.984', kind: 'PreToolUse',  agent: 'CHR-11', target: 'core/coordinator.py',     msg: 'Edit { lines: 248-291 }' },
  { t: '12:48:02.701', kind: 'REPLAN',      agent: 'BRV-07', target: 'core/cbs/conflicts.py',   msg: 'priority-down · h=14 → h=22' },
  { t: '12:48:02.508', kind: 'RESOLVED',    agent: 'DLT-02', target: 'graph/builder.py',        msg: 'merged · 0 conflicts remaining' },
  { t: '12:48:02.319', kind: 'PostToolUse', agent: 'ECH-09', target: 'agents/hooks.py',         msg: 'patch applied · +24 / −11' },
  { t: '12:48:02.044', kind: 'PreToolUse',  agent: 'FOX-04', target: 'core/cbs/search.py',      msg: 'Read { file_path }' },
  { t: '12:48:01.812', kind: 'LOCK',        agent: 'GLF-06', target: 'utils/locks.py',          msg: 'acquired exclusive on `coordinator.py:run`' },
  { t: '12:48:01.667', kind: 'CONFLICT',    agent: 'HTL-12', target: 'core/cbs/conflicts.py',   msg: 'edge conflict · waiting on FOX-04' },
  { t: '12:48:01.401', kind: 'SPAWN',       agent: 'IND-03', target: 'agents/runner.py',        msg: 'priority=3 · task=refactor' },
  { t: '12:48:01.205', kind: 'PostToolUse', agent: 'JLT-01', target: 'schemas/conflict.py',     msg: 'patch applied · +8 / −2' },
  { t: '12:48:00.978', kind: 'REPLAN',      agent: 'KIL-08', target: 'core/cbs/search.py',      msg: 'CT-node expanded · depth=7' },
  { t: '12:48:00.612', kind: 'RESOLVED',    agent: 'LMA-05', target: 'agents/state.py',         msg: 'merged · auto-rebased' },
  { t: '12:48:00.404', kind: 'PreToolUse',  agent: 'MIK-02', target: 'core/scheduler.py',       msg: 'Write { line_count: 41 }' },
  { t: '12:48:00.117', kind: 'CONFLICT',    agent: 'NOV-07', target: 'core/coordinator.py',     msg: 'vertex conflict @ step 9 with MIK-02' },
];

// ───────────────────────────────────────────────────────────
// Stats
// ───────────────────────────────────────────────────────────
window.CBS_STATS = {
  active: 50,
  byStatus: { running: 30, planning: 9, blocked: 6, replanning: 3, idle: 2 },
  conflictsOpen: 7,
  conflictsResolved24h: 184,
  nodes: window.CBS_NODES.length,
  edges: window.CBS_EDGES.length,
  throughputPerMin: 41.2,
  replans1h: 312,
  tokensBurnK: 1840,
  tokensBurnUsd: 24.71,
  queueDepthMax: 5,
  p50resolveMs: 410,
  p95resolveMs: 2140,
  uptime: '4d 17h 22m',
  ctDepth: 9,
};

// histogram bins for time-to-resolve (ms)
window.CBS_HIST = [
  { label: '0-100',  v: 18 },
  { label: '100-250',v: 41 },
  { label: '250-500',v: 64 },
  { label: '.5-1s',  v: 38 },
  { label: '1-2s',   v: 21 },
  { label: '2-5s',   v: 9  },
  { label: '5s+',    v: 3  },
];

// throughput sparkline (last 60 min, tasks/min)
window.CBS_THRU = [12,18,22,19,27,31,24,28,33,29,35,38,32,40,44,39,41,46,42,48,51,44,49,53,47,51,55,49,52,57,54,58,62,55,59,64,61,57,53,49,52,55,58,61,64,67,63,59,55,52,49,46,43,41,44,47,50,46,42,41];
