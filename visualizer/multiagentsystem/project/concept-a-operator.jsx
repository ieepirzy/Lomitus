// Concept A — "Operator"
// Dense, dark observability console. IBM Plex Sans + Plex Mono.
// Reads from window.CBS_* globals.

const opC = {
  bg:       'oklch(0.155 0.012 250)',
  panel:    'oklch(0.195 0.014 250)',
  panel2:   'oklch(0.225 0.014 250)',
  panelHi:  'oklch(0.255 0.016 250)',
  border:   'oklch(0.295 0.016 250)',
  borderSoft:'oklch(0.245 0.014 250)',
  text:     'oklch(0.965 0.005 250)',
  textDim:  'oklch(0.78 0.012 250)',
  muted:    'oklch(0.60 0.012 250)',
  faint:    'oklch(0.42 0.012 250)',
  ok:       'oklch(0.78 0.14 165)',
  warn:     'oklch(0.82 0.15 75)',
  err:      'oklch(0.70 0.18 25)',
  info:     'oklch(0.74 0.13 240)',
  violet:   'oklch(0.74 0.16 295)',
  grid:     'oklch(0.22 0.012 250)',
};

const opStatusColor = {
  running:    opC.ok,
  planning:   opC.info,
  blocked:    opC.warn,
  replanning: opC.violet,
  idle:       opC.faint,
};
const opModColor = {
  api:     opC.info,
  core:    opC.ok,
  cbs:     opC.violet,
  agents:  opC.warn,
  graph:   'oklch(0.74 0.13 195)',
  utils:   'oklch(0.70 0.10 230)',
  schemas: 'oklch(0.74 0.12 320)',
  db:      'oklch(0.70 0.10 30)',
  tests:   opC.muted,
};

const opBaseText = { fontFamily: '"IBM Plex Sans", system-ui, sans-serif', color: opC.text };
const opMono = { fontFamily: '"IBM Plex Mono", ui-monospace, monospace' };

// ─── tiny ui primitives ─────────────────────────────────────
function OpPanel({ title, sub, right, children, style, padding = 14 }) {
  return (
    <div style={{ background: opC.panel, border: `1px solid ${opC.border}`, borderRadius: 8, display: 'flex', flexDirection: 'column', ...style }}>
      {(title || right) && (
        <div style={{ display: 'flex', alignItems: 'center', padding: '10px 14px', borderBottom: `1px solid ${opC.borderSoft}`, gap: 8 }}>
          <div style={{ ...opMono, fontSize: 10.5, letterSpacing: 0.8, textTransform: 'uppercase', color: opC.textDim }}>{title}</div>
          {sub && <div style={{ ...opMono, fontSize: 10.5, color: opC.muted }}>{sub}</div>}
          <div style={{ flex: 1 }} />
          {right}
        </div>
      )}
      <div style={{ padding, flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>{children}</div>
    </div>
  );
}
function OpDot({ color, size = 7, pulse = false }) {
  return (
    <span style={{ display: 'inline-block', width: size, height: size, borderRadius: 999, background: color, boxShadow: pulse ? `0 0 0 0 ${color}` : 'none', animation: pulse ? 'opPulse 1.8s infinite' : 'none', flexShrink: 0 }} />
  );
}
function OpPill({ children, color = opC.muted, bg }) {
  return (
    <span style={{ ...opMono, fontSize: 10, letterSpacing: 0.4, color, background: bg || `color-mix(in oklch, ${color} 14%, transparent)`, border: `1px solid color-mix(in oklch, ${color} 28%, transparent)`, padding: '2px 7px', borderRadius: 4, whiteSpace: 'nowrap' }}>{children}</span>
  );
}
function OpSpark({ data, color = opC.ok, w = 90, h = 22, fill = true }) {
  const max = Math.max(...data, 1);
  const step = w / (data.length - 1);
  const pts = data.map((v, i) => `${(i*step).toFixed(1)},${(h - (v/max)*h).toFixed(1)}`).join(' ');
  return (
    <svg width={w} height={h} style={{ display: 'block' }}>
      {fill && <polyline points={`0,${h} ${pts} ${w},${h}`} fill={color} opacity={0.16} />}
      <polyline points={pts} fill="none" stroke={color} strokeWidth={1.2} />
    </svg>
  );
}

// ─── force graph ────────────────────────────────────────────
function OpGraph({ height = 540, selected, onSelect }) {
  const nodes = window.CBS_NODES;
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const W = 960, H = 580;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height, display: 'block' }} preserveAspectRatio="xMidYMid meet">
      {/* subtle grid */}
      <defs>
        <pattern id="opGrid" width="32" height="32" patternUnits="userSpaceOnUse">
          <path d="M 32 0 L 0 0 0 32" fill="none" stroke={opC.grid} strokeWidth="0.6" />
        </pattern>
        <marker id="opArr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill={opC.faint} />
        </marker>
      </defs>
      <rect width={W} height={H} fill="url(#opGrid)" opacity="0.5" />

      {/* edges */}
      {window.CBS_EDGES.map(([a,b], i) => {
        const A = byId[a], B = byId[b]; if (!A || !B) return null;
        const isHot = A.hot && B.hot;
        return (
          <g key={i}>
            <path id={`opE${i}`} d={`M${A.x},${A.y} L${B.x},${B.y}`} stroke={isHot ? opC.warn : opC.borderSoft} strokeWidth={isHot ? 1.4 : 1} opacity={isHot ? 0.75 : 0.55} markerEnd="url(#opArr)" fill="none" />
            {/* traveling agent dot on a few edges */}
            {(i % 7 === 0) && (
              <circle r={2.4} fill={opC.info}>
                <animateMotion dur={`${5 + (i%5)}s`} repeatCount="indefinite" rotate="auto">
                  <mpath xlinkHref={`#opE${i}`} />
                </animateMotion>
              </circle>
            )}
          </g>
        );
      })}

      {/* nodes */}
      {nodes.map((n) => {
        const r = 9 + Math.min(n.agents, 6) * 2.4;
        const col = opModColor[n.mod] || opC.muted;
        const isSel = selected === n.id;
        return (
          <g key={n.id} onClick={() => onSelect && onSelect(n.id)} style={{ cursor: 'pointer' }}>
            {n.hot && <circle cx={n.x} cy={n.y} r={r + 9} fill="none" stroke={opC.warn} strokeOpacity={0.5} strokeWidth={1}>
              <animate attributeName="r" values={`${r+6};${r+14};${r+6}`} dur="2.4s" repeatCount="indefinite" />
              <animate attributeName="stroke-opacity" values="0.55;0;0.55" dur="2.4s" repeatCount="indefinite" />
            </circle>}
            <circle cx={n.x} cy={n.y} r={r} fill={`color-mix(in oklch, ${col} 32%, ${opC.panel2})`} stroke={isSel ? opC.text : col} strokeWidth={isSel ? 2 : 1.4} />
            <text x={n.x} y={n.y + 3.5} textAnchor="middle" style={{ ...opMono, fontSize: 9, fill: opC.text, pointerEvents: 'none' }}>{n.agents || ''}</text>
            <text x={n.x} y={n.y + r + 12} textAnchor="middle" style={{ ...opMono, fontSize: 9.5, fill: opC.textDim, pointerEvents: 'none' }}>{n.label.split('/').pop()}</text>
          </g>
        );
      })}

      {/* legend */}
      <g transform={`translate(16, ${H - 26})`}>
        {['api','core','cbs','agents','graph','utils','db'].map((m, i) => (
          <g key={m} transform={`translate(${i * 78}, 0)`}>
            <circle cx={5} cy={5} r={4} fill={opModColor[m]} opacity={0.85} />
            <text x={14} y={9} style={{ ...opMono, fontSize: 10, fill: opC.muted }}>{m}</text>
          </g>
        ))}
      </g>
    </svg>
  );
}

// ─── stats strip ────────────────────────────────────────────
function OpStatCard({ label, value, unit, sub, color = opC.text, spark, sparkColor }) {
  return (
    <div style={{ flex: 1, background: opC.panel, border: `1px solid ${opC.border}`, borderRadius: 8, padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 6, minWidth: 0 }}>
      <div style={{ ...opMono, fontSize: 10, letterSpacing: 0.6, textTransform: 'uppercase', color: opC.muted, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{label}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
        <div style={{ fontFamily: '"IBM Plex Sans"', fontSize: 28, fontWeight: 500, color, lineHeight: 1, letterSpacing: -0.5 }}>{value}</div>
        {unit && <div style={{ ...opMono, fontSize: 11, color: opC.muted }}>{unit}</div>}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginTop: 'auto' }}>
        <div style={{ ...opMono, fontSize: 10.5, color: opC.muted }}>{sub}</div>
        {spark && <OpSpark data={spark} color={sparkColor || color} w={70} h={18} />}
      </div>
    </div>
  );
}

// ─── agent roster row ───────────────────────────────────────
function OpAgentRow({ a }) {
  const col = opStatusColor[a.status];
  const node = window.CBS_NODES.find(n => n.id === a.node);
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '14px 64px 1fr 70px 38px', alignItems: 'center', gap: 8, padding: '5px 10px', borderBottom: `1px solid ${opC.borderSoft}`, fontSize: 11.5 }}>
      <OpDot color={col} size={7} pulse={a.status === 'running' || a.status === 'replanning'} />
      <span style={{ ...opMono, color: opC.text, fontSize: 11 }}>{a.id}</span>
      <span style={{ ...opMono, color: opC.muted, fontSize: 10.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{node ? node.label.replace(/^.*\//,'') : '—'}</span>
      <OpSpark data={a.spark} color={col} w={62} h={14} fill={false} />
      <span style={{ ...opMono, fontSize: 10, color: opC.faint, textAlign: 'right' }}>{a.tokensK}k</span>
    </div>
  );
}

// ─── histogram ──────────────────────────────────────────────
function OpHist({ data, color = opC.info, height = 110 }) {
  const max = Math.max(...data.map(d => d.v));
  return (
    <div style={{ display: 'grid', gridTemplateColumns: `repeat(${data.length}, 1fr)`, alignItems: 'end', gap: 10, height, padding: '8px 4px 22px' }}>
      {data.map((d, i) => (
        <div key={i} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, height: '100%' }}>
          <div style={{ ...opMono, fontSize: 10, color: opC.muted }}>{d.v}</div>
          <div style={{ width: '100%', height: `${(d.v / max) * 100}%`, background: `linear-gradient(180deg, ${color} 0%, color-mix(in oklch, ${color} 50%, transparent) 100%)`, border: `1px solid color-mix(in oklch, ${color} 60%, transparent)`, borderRadius: 2 }} />
          <div style={{ ...opMono, fontSize: 9.5, color: opC.faint, position: 'absolute', marginTop: 'calc(100% + 4px)' }}>{d.label}</div>
        </div>
      ))}
    </div>
  );
}

// ─── event row ──────────────────────────────────────────────
const opKindColor = {
  CONFLICT: opC.warn, RESOLVED: opC.ok, REPLAN: opC.violet,
  PreToolUse: opC.info, PostToolUse: opC.info, SPAWN: opC.text,
  LOCK: opC.muted,
};
function OpEventRow({ e, dense = true }) {
  const col = opKindColor[e.kind] || opC.muted;
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '92px 92px 100px 1fr', gap: 12, padding: dense ? '4px 14px' : '7px 14px', borderBottom: `1px solid ${opC.borderSoft}`, alignItems: 'center' }}>
      <span style={{ ...opMono, fontSize: 10.5, color: opC.faint }}>{e.t}</span>
      <span style={{ ...opMono, fontSize: 10.5, color: col, letterSpacing: 0.3 }}>{e.kind}</span>
      <span style={{ ...opMono, fontSize: 10.5, color: opC.textDim }}>{e.agent}</span>
      <span style={{ ...opMono, fontSize: 10.5, color: opC.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        <span style={{ color: opC.text }}>{e.target}</span> · {e.msg}
      </span>
    </div>
  );
}

// ─── main ───────────────────────────────────────────────────
function ConceptAOperator() {
  const [sel, setSel] = React.useState('cbsS');
  const node = window.CBS_NODES.find(n => n.id === sel);
  const onNode = window.CBS_AGENTS.filter(a => a.node === sel);
  const s = window.CBS_STATS;

  return (
    <div style={{ width: 1480, minHeight: 1880, background: opC.bg, color: opC.text, ...opBaseText, padding: 18, display: 'flex', flexDirection: 'column', gap: 14, fontSize: 12 }}>
      <style>{`@keyframes opPulse{0%{box-shadow:0 0 0 0 currentColor}70%{box-shadow:0 0 0 6px transparent}100%{box-shadow:0 0 0 0 transparent}}`}</style>

      {/* top bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '0 4px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <svg width="22" height="22" viewBox="0 0 22 22"><circle cx="11" cy="11" r="9.5" fill="none" stroke={opC.ok} strokeWidth="1.4"/><circle cx="11" cy="11" r="3" fill={opC.ok}/><circle cx="11" cy="3.5" r="1.5" fill={opC.ok}/><circle cx="18.5" cy="11" r="1.5" fill={opC.ok}/><circle cx="11" cy="18.5" r="1.5" fill={opC.ok}/><circle cx="3.5" cy="11" r="1.5" fill={opC.ok}/></svg>
          <div style={{ ...opMono, fontSize: 12.5, letterSpacing: 0.8, color: opC.text }}>CBS<span style={{ color: opC.muted }}> · coordinator</span></div>
        </div>
        <div style={{ width: 1, height: 18, background: opC.border }} />
        <OpPill color={opC.ok}><OpDot color={opC.ok} size={5} pulse /> &nbsp;RUNNING</OpPill>
        <OpPill color={opC.muted}>uptime {s.uptime}</OpPill>
        <OpPill color={opC.muted}>ws · 12 sub</OpPill>
        <div style={{ flex: 1 }} />
        <div style={{ display: 'flex', gap: 6 }}>
          {['agents','nodes','conflicts','events','search-tree','metrics'].map((k,i)=>(
            <span key={k} style={{ ...opMono, fontSize: 10.5, padding: '5px 9px', borderRadius: 4, background: i===0?opC.panelHi:'transparent', color: i===0?opC.text:opC.muted, border: `1px solid ${i===0?opC.border:'transparent'}` }}>{k}</span>
          ))}
        </div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <OpPill color={opC.muted}>repo · cbs-coord/main</OpPill>
          <OpPill color={opC.muted}>tick 250ms</OpPill>
        </div>
      </div>

      {/* stat strip */}
      <div style={{ display: 'flex', gap: 10 }}>
        <OpStatCard label="active agents" value={s.active} sub={`${s.byStatus.running} running · ${s.byStatus.blocked} blocked`} color={opC.text} spark={window.CBS_THRU.slice(-20)} sparkColor={opC.ok} />
        <OpStatCard label="conflicts" value={s.conflictsOpen} unit="open" sub={`${s.conflictsResolved24h} resolved · 24h`} color={opC.warn} />
        <OpStatCard label="graph nodes" value={s.nodes} sub={`${s.edges} edges`} color={opC.text} />
        <OpStatCard label="throughput" value={s.throughputPerMin} unit="tasks/min" sub="+12% vs 1h" color={opC.ok} spark={window.CBS_THRU} sparkColor={opC.ok} />
        <OpStatCard label="replans · 1h" value={s.replans1h} sub={`CT depth ${s.ctDepth}`} color={opC.violet} />
        <OpStatCard label="queue max" value={s.queueDepthMax} unit="ops" sub="core/cbs/conflicts.py" color={opC.warn} />
        <OpStatCard label="tokens · 24h" value={`${(s.tokensBurnK/1000).toFixed(2)}M`} sub={`$${s.tokensBurnUsd} · burn`} color={opC.text} />
        <OpStatCard label="p95 resolve" value={s.p95resolveMs} unit="ms" sub={`p50 ${s.p50resolveMs}ms`} color={opC.info} />
      </div>

      {/* main 3-col grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '300px 1fr 320px', gap: 12, height: 720 }}>

        {/* LEFT — agent roster */}
        <OpPanel
          title="Agents"
          sub={`· ${window.CBS_AGENTS.length}`}
          right={
            <div style={{ display: 'flex', gap: 6 }}>
              {Object.entries(s.byStatus).map(([k,v]) => (
                <span key={k} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                  <OpDot color={opStatusColor[k]} size={5} />
                  <span style={{ ...opMono, fontSize: 9.5, color: opC.muted }}>{v}</span>
                </span>
              ))}
            </div>
          }
          padding={0}
        >
          <div style={{ display: 'grid', gridTemplateColumns: '14px 64px 1fr 70px 38px', gap: 8, padding: '7px 10px 6px', borderBottom: `1px solid ${opC.border}`, ...opMono, fontSize: 9.5, color: opC.faint, letterSpacing: 0.5, textTransform: 'uppercase' }}>
            <span></span><span>id</span><span>node</span><span style={{ textAlign: 'center' }}>load</span><span style={{ textAlign: 'right' }}>tok</span>
          </div>
          <div style={{ overflow: 'hidden', flex: 1 }}>
            {window.CBS_AGENTS.slice(0, 40).map(a => <OpAgentRow key={a.id} a={a} />)}
          </div>
        </OpPanel>

        {/* CENTER — graph + node bar */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
          <OpPanel
            title="Dependency graph"
            sub="· force-layout · live"
            right={
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <OpPill color={opC.warn}>3 hot</OpPill>
                <OpPill color={opC.muted}>filter: module</OpPill>
                <OpPill color={opC.muted}>fit</OpPill>
              </div>
            }
            padding={0}
            style={{ flex: 1, minHeight: 0 }}
          >
            <OpGraph height="100%" selected={sel} onSelect={setSel} />
          </OpPanel>

          {/* per-node contention chart */}
          <OpPanel title="Queue depth · per node" sub="last 60s" padding={12} style={{ height: 150 }}>
            <div style={{ display: 'flex', alignItems: 'end', gap: 6, height: '100%' }}>
              {window.CBS_NODES.map(n => {
                const v = n.queue + (n.agents > 4 ? 1 : 0);
                const max = 7;
                const col = v >= 4 ? opC.warn : v >= 2 ? opC.info : opC.borderSoft;
                return (
                  <div key={n.id} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, minWidth: 0 }}>
                    <div style={{ ...opMono, fontSize: 9, color: v ? opC.text : opC.faint }}>{v || ''}</div>
                    <div title={n.label} style={{ width: '100%', height: `${Math.max(2, (v / max) * 80)}px`, background: col, borderRadius: 2, opacity: v ? 1 : 0.4 }} />
                    <div style={{ ...opMono, fontSize: 8.5, color: opC.faint, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', width: '100%', textAlign: 'center' }}>{n.label.replace(/^.*\//,'').replace('.py','')}</div>
                  </div>
                );
              })}
            </div>
          </OpPanel>
        </div>

        {/* RIGHT — node detail + conflicts */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
          <OpPanel
            title="Selected node"
            right={<OpPill color={opModColor[node.mod]}>{node.mod}</OpPill>}
            style={{ flex: '0 0 auto' }}
            padding={14}
          >
            <div style={{ ...opMono, fontSize: 13, color: opC.text, marginBottom: 8, wordBreak: 'break-all' }}>{node.label}</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 12 }}>
              <div>
                <div style={{ ...opMono, fontSize: 9.5, color: opC.faint, textTransform: 'uppercase', letterSpacing: 0.5 }}>agents on file</div>
                <div style={{ ...opMono, fontSize: 20, color: opC.text }}>{node.agents}</div>
              </div>
              <div>
                <div style={{ ...opMono, fontSize: 9.5, color: opC.faint, textTransform: 'uppercase', letterSpacing: 0.5 }}>queue depth</div>
                <div style={{ ...opMono, fontSize: 20, color: node.queue > 2 ? opC.warn : opC.text }}>{node.queue}</div>
              </div>
              <div>
                <div style={{ ...opMono, fontSize: 9.5, color: opC.faint, textTransform: 'uppercase', letterSpacing: 0.5 }}>in-degree</div>
                <div style={{ ...opMono, fontSize: 20, color: opC.text }}>{window.CBS_EDGES.filter(([,b])=>b===node.id).length}</div>
              </div>
              <div>
                <div style={{ ...opMono, fontSize: 9.5, color: opC.faint, textTransform: 'uppercase', letterSpacing: 0.5 }}>out-degree</div>
                <div style={{ ...opMono, fontSize: 20, color: opC.text }}>{window.CBS_EDGES.filter(([a])=>a===node.id).length}</div>
              </div>
            </div>
            <div style={{ ...opMono, fontSize: 9.5, color: opC.faint, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>occupants</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {onNode.slice(0, 6).map(a => (
                <div key={a.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 8px', background: opC.panel2, border: `1px solid ${opC.borderSoft}`, borderRadius: 5 }}>
                  <OpDot color={opStatusColor[a.status]} size={6} pulse={a.status==='running'} />
                  <span style={{ ...opMono, fontSize: 10.5, color: opC.text }}>{a.id}</span>
                  <span style={{ ...opMono, fontSize: 10, color: opC.muted, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.task}</span>
                  <OpPill color={opStatusColor[a.status]}>{a.status}</OpPill>
                </div>
              ))}
              {onNode.length === 0 && <div style={{ ...opMono, fontSize: 10.5, color: opC.faint, padding: '8px 0' }}>— no agents on this node</div>}
            </div>
          </OpPanel>

          <OpPanel title="Open conflicts" sub={`· ${s.conflictsOpen}`} right={<OpPill color={opC.warn}>action req</OpPill>} padding={0} style={{ flex: 1, minHeight: 0 }}>
            <div style={{ overflow: 'hidden', flex: 1 }}>
              {[
                { kind: 'vertex',  pair: 'ALF-03 ↔ BRV-07', node: 'core/cbs/search.py',     age: '4s'  },
                { kind: 'edge',    pair: 'HTL-12 → FOX-04', node: 'core/cbs/conflicts.py',  age: '7s'  },
                { kind: 'vertex',  pair: 'NOV-07 ↔ MIK-02', node: 'core/coordinator.py',    age: '11s' },
                { kind: 'swap',    pair: 'CHR-11 ↔ DLT-02', node: 'graph/builder.py',       age: '22s' },
                { kind: 'edge',    pair: 'KIL-08 → JLT-01', node: 'schemas/conflict.py',    age: '38s' },
                { kind: 'vertex',  pair: 'OSC-04 ↔ PAP-09', node: 'core/cbs/conflicts.py',  age: '52s' },
                { kind: 'edge',    pair: 'GLF-06 → IND-03', node: 'utils/locks.py',         age: '1m'  },
              ].map((c,i) => (
                <div key={i} style={{ padding: '8px 14px', borderBottom: `1px solid ${opC.borderSoft}`, display: 'flex', flexDirection: 'column', gap: 3 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <OpPill color={opC.warn}>{c.kind}</OpPill>
                    <span style={{ ...opMono, fontSize: 10.5, color: opC.text }}>{c.pair}</span>
                    <div style={{ flex: 1 }} />
                    <span style={{ ...opMono, fontSize: 10, color: opC.faint }}>{c.age}</span>
                  </div>
                  <span style={{ ...opMono, fontSize: 10, color: opC.muted, paddingLeft: 2 }}>{c.node}</span>
                </div>
              ))}
            </div>
          </OpPanel>
        </div>
      </div>

      {/* lower row: event stream + histograms */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 360px 320px', gap: 12, height: 320 }}>
        <OpPanel
          title="Hook event stream"
          sub="· live tail · PreToolUse / PostToolUse / CBS"
          right={
            <div style={{ display: 'flex', gap: 6 }}>
              <OpPill color={opC.ok}><OpDot color={opC.ok} size={5} pulse /> &nbsp;tailing</OpPill>
              <OpPill color={opC.muted}>14 ev/s</OpPill>
              <OpPill color={opC.muted}>pause</OpPill>
            </div>
          }
          padding={0}
        >
          <div style={{ display: 'grid', gridTemplateColumns: '92px 92px 100px 1fr', gap: 12, padding: '6px 14px', borderBottom: `1px solid ${opC.border}`, ...opMono, fontSize: 9.5, color: opC.faint, letterSpacing: 0.5, textTransform: 'uppercase' }}>
            <span>time</span><span>kind</span><span>agent</span><span>target · message</span>
          </div>
          <div style={{ flex: 1, overflow: 'hidden' }}>
            {window.CBS_EVENTS.map((e, i) => <OpEventRow key={i} e={e} />)}
          </div>
        </OpPanel>

        <OpPanel title="Time-to-resolve" sub="histogram · last 1h">
          <OpHist data={window.CBS_HIST} color={opC.info} height={180} />
          <div style={{ display: 'flex', justifyContent: 'space-between', ...opMono, fontSize: 10, color: opC.muted, marginTop: 18 }}>
            <span>p50 · {s.p50resolveMs}ms</span>
            <span>p95 · {s.p95resolveMs}ms</span>
            <span>max · 4.1s</span>
          </div>
        </OpPanel>

        <OpPanel title="Throughput" sub="tasks/min · last 60m">
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
            <div style={{ fontSize: 32, fontWeight: 500, color: opC.text, lineHeight: 1 }}>{s.throughputPerMin}</div>
            <div style={{ ...opMono, fontSize: 11, color: opC.muted }}>tasks/min</div>
            <div style={{ flex: 1 }} />
            <OpPill color={opC.ok}>▲ 12%</OpPill>
          </div>
          <div style={{ marginTop: 14, flex: 1, minHeight: 0 }}>
            <OpSpark data={window.CBS_THRU} color={opC.ok} w={280} h={140} />
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', ...opMono, fontSize: 10, color: opC.faint }}>
            <span>−60m</span><span>−30m</span><span>now</span>
          </div>
        </OpPanel>
      </div>

      {/* footer */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '6px 4px 0', color: opC.faint, ...opMono, fontSize: 10.5, borderTop: `1px solid ${opC.border}` }}>
        <span>cbs-coord/0.4.2</span>
        <span>·</span>
        <span>haiku-4.5 · 8 workers</span>
        <span>·</span>
        <span>ct-nodes 1.2k · expanded 47k</span>
        <span style={{ flex: 1 }} />
        <span>last replan 318ms ago</span>
        <span>·</span>
        <span>memory 412 MB</span>
      </div>
    </div>
  );
}

window.ConceptAOperator = ConceptAOperator;
