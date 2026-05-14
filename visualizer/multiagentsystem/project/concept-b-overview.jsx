// Concept B — "Overview"
// Calmer, lighter observability dashboard. Geist + Geist Mono.
// Reads from window.CBS_* globals.

const ovC = {
  bg:       'oklch(0.985 0.003 85)',   // warm off-white page bg
  surface:  'oklch(1 0 0)',             // white cards
  surface2: 'oklch(0.975 0.004 85)',    // hover / inset
  border:   'oklch(0.92 0.004 85)',
  borderStrong: 'oklch(0.86 0.005 85)',
  text:     'oklch(0.18 0.01 250)',
  textMid:  'oklch(0.38 0.012 250)',
  muted:    'oklch(0.56 0.008 250)',
  faint:    'oklch(0.72 0.006 250)',
  accent:   'oklch(0.52 0.16 262)',     // indigo
  accentSoft:'oklch(0.94 0.04 262)',
  ok:       'oklch(0.58 0.13 158)',
  okSoft:   'oklch(0.94 0.04 158)',
  warn:     'oklch(0.66 0.14 60)',
  warnSoft: 'oklch(0.95 0.04 70)',
  err:      'oklch(0.58 0.18 22)',
  violet:   'oklch(0.55 0.17 295)',
  grid:     'oklch(0.95 0.003 85)',
};

const ovStatusColor = {
  running:    ovC.ok,
  planning:   ovC.accent,
  blocked:    ovC.warn,
  replanning: ovC.violet,
  idle:       ovC.faint,
};
const ovModColor = {
  api:     ovC.accent,
  core:    ovC.ok,
  cbs:     ovC.violet,
  agents:  ovC.warn,
  graph:   'oklch(0.55 0.12 200)',
  utils:   'oklch(0.55 0.05 250)',
  schemas: 'oklch(0.55 0.13 320)',
  db:      'oklch(0.55 0.12 30)',
  tests:   ovC.muted,
};

const ovSans = { fontFamily: 'Geist, system-ui, sans-serif', color: ovC.text };
const ovMono = { fontFamily: '"Geist Mono", ui-monospace, monospace' };

// ─── primitives ─────────────────────────────────────────────
function OvCard({ title, sub, right, children, style, padding = 22, headerBorder = true }) {
  return (
    <div style={{ background: ovC.surface, border: `1px solid ${ovC.border}`, borderRadius: 14, display: 'flex', flexDirection: 'column', minHeight: 0, ...style }}>
      {(title || right) && (
        <div style={{ display: 'flex', alignItems: 'center', padding: '18px 22px 14px', gap: 10, borderBottom: headerBorder ? `1px solid ${ovC.border}` : 'none' }}>
          <div>
            <div style={{ fontSize: 13.5, fontWeight: 500, color: ovC.text, letterSpacing: -0.1 }}>{title}</div>
            {sub && <div style={{ fontSize: 11.5, color: ovC.muted, marginTop: 2, ...ovMono }}>{sub}</div>}
          </div>
          <div style={{ flex: 1 }} />
          {right}
        </div>
      )}
      <div style={{ padding, flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>{children}</div>
    </div>
  );
}
function OvDot({ color, size = 7, pulse = false }) {
  return <span style={{ display: 'inline-block', width: size, height: size, borderRadius: 999, background: color, animation: pulse ? 'ovPulse 1.8s infinite' : 'none', flexShrink: 0 }} />;
}
function OvChip({ children, color = ovC.muted, bg, strong = false }) {
  return (
    <span style={{ ...ovMono, fontSize: 10.5, color, background: bg || `color-mix(in oklch, ${color} 12%, ${ovC.surface})`, border: strong ? `1px solid color-mix(in oklch, ${color} 30%, transparent)` : 'none', padding: '3px 8px', borderRadius: 999, whiteSpace: 'nowrap', letterSpacing: 0.1 }}>{children}</span>
  );
}
function OvSpark({ data, color = ovC.accent, w = 110, h = 32, fill = true, smooth = true }) {
  const max = Math.max(...data, 1);
  const step = w / (data.length - 1);
  const pts = data.map((v, i) => [i*step, h - (v/max)*h*0.9 - 2]);
  let d = '';
  if (smooth) {
    d = pts.reduce((acc, p, i, arr) => {
      if (i === 0) return `M${p[0].toFixed(1)},${p[1].toFixed(1)}`;
      const prev = arr[i-1];
      const cx1 = (prev[0] + p[0]) / 2;
      return `${acc} C${cx1.toFixed(1)},${prev[1].toFixed(1)} ${cx1.toFixed(1)},${p[1].toFixed(1)} ${p[0].toFixed(1)},${p[1].toFixed(1)}`;
    }, '');
  } else {
    d = 'M' + pts.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' L');
  }
  return (
    <svg width={w} height={h} style={{ display: 'block', overflow: 'visible' }}>
      <defs>
        <linearGradient id={`ovG-${color.replace(/[^a-z0-9]/gi,'')}`} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22"/>
          <stop offset="100%" stopColor={color} stopOpacity="0"/>
        </linearGradient>
      </defs>
      {fill && <path d={`${d} L${w},${h} L0,${h} Z`} fill={`url(#ovG-${color.replace(/[^a-z0-9]/gi,'')})`} />}
      <path d={d} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

// ─── force graph ────────────────────────────────────────────
function OvGraph({ height = 540, selected, onSelect }) {
  const nodes = window.CBS_NODES;
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const W = 960, H = 580;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height, display: 'block' }} preserveAspectRatio="xMidYMid meet">
      <defs>
        <pattern id="ovDots" width="22" height="22" patternUnits="userSpaceOnUse">
          <circle cx="1" cy="1" r="1" fill={ovC.grid} />
        </pattern>
        <marker id="ovArr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4.5" markerHeight="4.5" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill={ovC.borderStrong} />
        </marker>
      </defs>
      <rect width={W} height={H} fill="url(#ovDots)" />

      {window.CBS_EDGES.map(([a,b], i) => {
        const A = byId[a], B = byId[b]; if (!A || !B) return null;
        const isHot = A.hot && B.hot;
        return (
          <g key={i}>
            <path id={`ovE${i}`} d={`M${A.x},${A.y} L${B.x},${B.y}`} stroke={isHot ? ovC.warn : ovC.borderStrong} strokeWidth={isHot ? 1.4 : 0.9} opacity={isHot ? 0.9 : 0.7} markerEnd="url(#ovArr)" fill="none" />
            {(i % 7 === 0) && (
              <circle r={2.6} fill={ovC.accent}>
                <animateMotion dur={`${6 + (i%5)}s`} repeatCount="indefinite">
                  <mpath xlinkHref={`#ovE${i}`} />
                </animateMotion>
              </circle>
            )}
          </g>
        );
      })}

      {nodes.map((n) => {
        const r = 11 + Math.min(n.agents, 6) * 2.6;
        const col = ovModColor[n.mod] || ovC.muted;
        const isSel = selected === n.id;
        return (
          <g key={n.id} onClick={() => onSelect && onSelect(n.id)} style={{ cursor: 'pointer' }}>
            {n.hot && <circle cx={n.x} cy={n.y} r={r + 10} fill="none" stroke={ovC.warn} strokeOpacity={0.45} strokeWidth={1.2}>
              <animate attributeName="r" values={`${r+7};${r+16};${r+7}`} dur="2.6s" repeatCount="indefinite" />
              <animate attributeName="stroke-opacity" values="0.55;0;0.55" dur="2.6s" repeatCount="indefinite" />
            </circle>}
            <circle cx={n.x} cy={n.y} r={r} fill={ovC.surface} stroke={col} strokeWidth={isSel ? 2.4 : 1.6} />
            <circle cx={n.x} cy={n.y} r={r - 4} fill={`color-mix(in oklch, ${col} 18%, ${ovC.surface})`} />
            <text x={n.x} y={n.y + 3.5} textAnchor="middle" style={{ ...ovMono, fontSize: 10, fill: ovC.text, pointerEvents: 'none', fontWeight: 500 }}>{n.agents || ''}</text>
            <text x={n.x} y={n.y + r + 14} textAnchor="middle" style={{ ...ovMono, fontSize: 10, fill: ovC.textMid, pointerEvents: 'none' }}>{n.label.split('/').pop()}</text>
          </g>
        );
      })}

      <g transform={`translate(20, ${H - 28})`}>
        {['api','core','cbs','agents','graph','utils','db'].map((m, i) => (
          <g key={m} transform={`translate(${i * 84}, 0)`}>
            <circle cx={6} cy={6} r={5} fill={ovC.surface} stroke={ovModColor[m]} strokeWidth={1.4} />
            <text x={18} y={10} style={{ ...ovMono, fontSize: 10.5, fill: ovC.muted }}>{m}</text>
          </g>
        ))}
      </g>
    </svg>
  );
}

// ─── KPI hero card ──────────────────────────────────────────
function OvKpi({ label, value, unit, delta, deltaColor, sub, spark, sparkColor = ovC.accent, accent }) {
  return (
    <div style={{ flex: 1, background: ovC.surface, border: `1px solid ${ovC.border}`, borderRadius: 14, padding: '22px 24px', display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0, position: 'relative', overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        {accent && <span style={{ width: 8, height: 8, borderRadius: 999, background: accent }} />}
        <div style={{ ...ovMono, fontSize: 11.5, color: ovC.muted, letterSpacing: 0.2 }}>{label}</div>
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <div style={{ fontSize: 40, fontWeight: 500, color: ovC.text, lineHeight: 1, letterSpacing: -1.2 }}>{value}</div>
        {unit && <div style={{ ...ovMono, fontSize: 12, color: ovC.muted }}>{unit}</div>}
        {delta && <OvChip color={deltaColor || ovC.ok} strong>{delta}</OvChip>}
      </div>
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 12, marginTop: 'auto', minHeight: 34 }}>
        <div style={{ ...ovMono, fontSize: 11, color: ovC.muted, lineHeight: 1.35, maxWidth: '50%' }}>{sub}</div>
        {spark && <OvSpark data={spark} color={sparkColor} w={120} h={34} />}
      </div>
    </div>
  );
}

// ─── main ───────────────────────────────────────────────────
function ConceptBOverview() {
  const [sel, setSel] = React.useState('cbsS');
  const node = window.CBS_NODES.find(n => n.id === sel);
  const onNode = window.CBS_AGENTS.filter(a => a.node === sel);
  const s = window.CBS_STATS;
  const histMax = Math.max(...window.CBS_HIST.map(d => d.v));

  return (
    <div style={{ width: 1480, minHeight: 1880, background: ovC.bg, color: ovC.text, ...ovSans, padding: 28, display: 'flex', flexDirection: 'column', gap: 20, fontSize: 13 }}>
      <style>{`@keyframes ovPulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.55;transform:scale(.92)}}`}</style>

      {/* top nav */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 18, padding: '4px 4px 0' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <svg width="26" height="26" viewBox="0 0 22 22">
            <circle cx="11" cy="11" r="9.5" fill="none" stroke={ovC.accent} strokeWidth="1.6"/>
            <circle cx="11" cy="11" r="3.2" fill={ovC.accent}/>
            <circle cx="11" cy="3.5" r="1.6" fill={ovC.accent}/>
            <circle cx="18.5" cy="11" r="1.6" fill={ovC.accent}/>
            <circle cx="11" cy="18.5" r="1.6" fill={ovC.accent}/>
            <circle cx="3.5" cy="11" r="1.6" fill={ovC.accent}/>
          </svg>
          <div style={{ fontSize: 16, fontWeight: 500, letterSpacing: -0.3 }}>CBS Coordinator</div>
          <span style={{ ...ovMono, fontSize: 11.5, color: ovC.muted }}>· cbs-coord/main</span>
        </div>
        <div style={{ display: 'flex', gap: 4, marginLeft: 20 }}>
          {['Overview','Graph','Agents','Conflicts','Events','Settings'].map((t,i) => (
            <span key={t} style={{ padding: '8px 12px', borderRadius: 8, fontSize: 12.5, color: i===0 ? ovC.text : ovC.muted, background: i===0 ? ovC.surface : 'transparent', border: i===0 ? `1px solid ${ovC.border}` : '1px solid transparent', fontWeight: i===0 ? 500 : 400 }}>{t}</span>
          ))}
        </div>
        <div style={{ flex: 1 }} />
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 12px', background: ovC.surface, border: `1px solid ${ovC.border}`, borderRadius: 999 }}>
            <OvDot color={ovC.ok} size={7} pulse />
            <span style={{ ...ovMono, fontSize: 11.5, color: ovC.text }}>Live</span>
            <span style={{ ...ovMono, fontSize: 11, color: ovC.muted }}>· {s.uptime}</span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '7px 12px', background: ovC.surface, border: `1px solid ${ovC.border}`, borderRadius: 999, ...ovMono, fontSize: 11.5, color: ovC.muted }}>
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none"><circle cx="7" cy="7" r="5" stroke={ovC.muted} strokeWidth="1.4"/><path d="M14 14L11 11" stroke={ovC.muted} strokeWidth="1.4" strokeLinecap="round"/></svg>
            search agents, files, conflicts
            <span style={{ marginLeft: 12, fontSize: 10.5, color: ovC.faint }}>⌘K</span>
          </div>
          <div style={{ width: 32, height: 32, borderRadius: 999, background: ovC.accentSoft, color: ovC.accent, display: 'grid', placeItems: 'center', fontSize: 12, fontWeight: 500, ...ovMono }}>cb</div>
        </div>
      </div>

      {/* heading */}
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 12, padding: '14px 4px 0' }}>
        <div>
          <div style={{ fontSize: 28, fontWeight: 500, letterSpacing: -0.8, color: ovC.text }}>Coordinator overview</div>
          <div style={{ ...ovMono, fontSize: 12, color: ovC.muted, marginTop: 6 }}>50 agents · {s.nodes} nodes · {s.edges} edges · {s.conflictsOpen} open conflicts · MAPF · CBS</div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button style={{ padding: '8px 14px', background: ovC.surface, border: `1px solid ${ovC.border}`, borderRadius: 8, ...ovMono, fontSize: 11.5, color: ovC.textMid }}>Last 1h ▾</button>
          <button style={{ padding: '8px 14px', background: ovC.surface, border: `1px solid ${ovC.border}`, borderRadius: 8, ...ovMono, fontSize: 11.5, color: ovC.textMid }}>All modules ▾</button>
          <button style={{ padding: '8px 14px', background: ovC.accent, color: 'white', border: 'none', borderRadius: 8, ...ovMono, fontSize: 11.5 }}>Spawn agent</button>
        </div>
      </div>

      {/* KPI hero row */}
      <div style={{ display: 'flex', gap: 14 }}>
        <OvKpi
          label="Active agents"
          value={s.active}
          delta="+4"
          sub={`${s.byStatus.running} running · ${s.byStatus.planning} planning · ${s.byStatus.blocked} blocked`}
          spark={window.CBS_THRU.slice(-24).map(v=>v*0.8+12)}
          sparkColor={ovC.accent}
          accent={ovC.accent}
        />
        <OvKpi
          label="Conflicts · open"
          value={s.conflictsOpen}
          delta="−2"
          deltaColor={ovC.ok}
          sub={`${s.conflictsResolved24h} resolved in last 24h · avg p95 ${(s.p95resolveMs/1000).toFixed(1)}s`}
          spark={[8,12,11,9,14,17,15,12,10,13,9,7,11,8,6,9,7,10,8,7]}
          sparkColor={ovC.warn}
          accent={ovC.warn}
        />
        <OvKpi
          label="Throughput"
          value={s.throughputPerMin}
          unit="t/min"
          delta="▲ 12%"
          sub="rolling 60 minute mean · steady ramp"
          spark={window.CBS_THRU}
          sparkColor={ovC.ok}
          accent={ovC.ok}
        />
        <OvKpi
          label="Token burn · 24h"
          value="1.84"
          unit="M"
          delta={`$${s.tokensBurnUsd}`}
          deltaColor={ovC.muted}
          sub={`${s.replans1h} replans · 1h · CT depth ${s.ctDepth}`}
          spark={[40,55,62,58,71,80,77,85,92,88,96,104,98,108,114]}
          sparkColor={ovC.violet}
          accent={ovC.violet}
        />
      </div>

      {/* main 2-col split: graph + sidebar */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 460px', gap: 14, minHeight: 720 }}>
        <OvCard
          title="Dependency graph"
          sub={`Force-directed · ${s.nodes} nodes · ${s.edges} edges · live`}
          right={
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <OvChip color={ovC.warn} strong>3 hot</OvChip>
              <div style={{ display: 'flex', gap: 4, padding: 3, background: ovC.surface2, border: `1px solid ${ovC.border}`, borderRadius: 8 }}>
                {['force','dag','radial'].map((m,i)=>(<span key={m} style={{ padding: '4px 10px', borderRadius: 6, ...ovMono, fontSize: 11, background: i===0 ? ovC.surface : 'transparent', color: i===0 ? ovC.text : ovC.muted, boxShadow: i===0 ? `0 1px 2px rgba(15,23,42,.06)` : 'none' }}>{m}</span>))}
              </div>
            </div>
          }
          padding={20}
        >
          <OvGraph height={620} selected={sel} onSelect={setSel} />
        </OvCard>

        {/* RIGHT sidebar */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minHeight: 0 }}>
          {/* node detail */}
          <OvCard
            title="Selected node"
            sub={node.label}
            right={<OvChip color={ovModColor[node.mod]} strong>{node.mod}</OvChip>}
            padding={18}
          >
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 18 }}>
              {[
                { l: 'agents', v: node.agents, c: ovC.text },
                { l: 'queue',  v: node.queue,  c: node.queue > 2 ? ovC.warn : ovC.text },
                { l: 'in',     v: window.CBS_EDGES.filter(([,b])=>b===node.id).length, c: ovC.text },
                { l: 'out',    v: window.CBS_EDGES.filter(([a])=>a===node.id).length, c: ovC.text },
              ].map(b => (
                <div key={b.l} style={{ padding: '10px 12px', background: ovC.surface2, borderRadius: 8, border: `1px solid ${ovC.border}` }}>
                  <div style={{ ...ovMono, fontSize: 10, color: ovC.muted, textTransform: 'uppercase', letterSpacing: 0.3 }}>{b.l}</div>
                  <div style={{ fontSize: 22, fontWeight: 500, color: b.c, lineHeight: 1.1, marginTop: 4, letterSpacing: -0.5 }}>{b.v}</div>
                </div>
              ))}
            </div>
            <div style={{ ...ovMono, fontSize: 10.5, color: ovC.muted, textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 10 }}>Occupants</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {onNode.slice(0, 5).map(a => (
                <div key={a.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', background: ovC.surface2, border: `1px solid ${ovC.border}`, borderRadius: 8 }}>
                  <OvDot color={ovStatusColor[a.status]} size={7} pulse={a.status === 'running'} />
                  <span style={{ ...ovMono, fontSize: 11.5, color: ovC.text }}>{a.id}</span>
                  <span style={{ fontSize: 12, color: ovC.muted, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.task}</span>
                  <OvChip color={ovStatusColor[a.status]}>{a.status}</OvChip>
                </div>
              ))}
              {onNode.length === 0 && <div style={{ fontSize: 12, color: ovC.muted, padding: '8px 0' }}>No agents currently on this node.</div>}
            </div>
          </OvCard>

          {/* agent status breakdown */}
          <OvCard title="Agent status" sub="50 total · live" padding={18}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {Object.entries(s.byStatus).map(([k,v]) => (
                <div key={k} style={{ display: 'grid', gridTemplateColumns: '14px 90px 1fr 32px', alignItems: 'center', gap: 12 }}>
                  <OvDot color={ovStatusColor[k]} size={9} />
                  <span style={{ fontSize: 12.5, color: ovC.text, textTransform: 'capitalize' }}>{k}</span>
                  <div style={{ height: 6, background: ovC.surface2, borderRadius: 999, overflow: 'hidden', position: 'relative' }}>
                    <div style={{ position: 'absolute', inset: 0, width: `${(v/50)*100}%`, background: ovStatusColor[k], borderRadius: 999 }} />
                  </div>
                  <span style={{ ...ovMono, fontSize: 12, color: ovC.textMid, textAlign: 'right' }}>{v}</span>
                </div>
              ))}
            </div>
          </OvCard>
        </div>
      </div>

      {/* secondary row: contention chart + histogram + open conflicts */}
      <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1fr 1fr', gap: 14, minHeight: 280 }}>

        <OvCard title="Queue depth · per node" sub="last 60s" padding={20}>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 6, flex: 1, paddingTop: 6 }}>
            {window.CBS_NODES.map(n => {
              const v = n.queue + (n.agents > 4 ? 1 : 0);
              const max = 7;
              const col = v >= 4 ? ovC.warn : v >= 2 ? ovC.accent : ovC.borderStrong;
              return (
                <div key={n.id} title={`${n.label}: ${v}`} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, minWidth: 0 }}>
                  <div style={{ ...ovMono, fontSize: 10, color: v ? ovC.textMid : ovC.faint }}>{v || ''}</div>
                  <div style={{ width: '100%', height: `${Math.max(3, (v / max) * 130)}px`, background: col, borderRadius: '4px 4px 2px 2px', opacity: v ? 1 : 0.45 }} />
                  <div style={{ ...ovMono, fontSize: 8.5, color: ovC.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', width: '100%', textAlign: 'center', transform: 'rotate(-30deg) translate(-6px, 8px)', transformOrigin: 'center' }}>{n.label.replace(/^.*\//,'').replace('.py','')}</div>
                </div>
              );
            })}
          </div>
        </OvCard>

        <OvCard title="Time-to-resolve" sub="histogram · last 1h · ms" padding={20}>
          <div style={{ display: 'grid', gridTemplateColumns: `repeat(${window.CBS_HIST.length}, 1fr)`, alignItems: 'end', gap: 10, flex: 1, paddingTop: 6 }}>
            {window.CBS_HIST.map((d,i) => (
              <div key={i} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, height: '100%', justifyContent: 'flex-end' }}>
                <div style={{ ...ovMono, fontSize: 10.5, color: ovC.textMid }}>{d.v}</div>
                <div style={{ width: '100%', height: `${(d.v / histMax) * 140}px`, background: `linear-gradient(180deg, ${ovC.accent} 0%, color-mix(in oklch, ${ovC.accent} 30%, ${ovC.surface}) 100%)`, borderRadius: '4px 4px 2px 2px' }} />
                <div style={{ ...ovMono, fontSize: 9.5, color: ovC.muted }}>{d.label}</div>
              </div>
            ))}
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', ...ovMono, fontSize: 11, color: ovC.muted, marginTop: 12, paddingTop: 12, borderTop: `1px solid ${ovC.border}` }}>
            <span>p50 · <span style={{ color: ovC.text }}>{s.p50resolveMs}ms</span></span>
            <span>p95 · <span style={{ color: ovC.text }}>{s.p95resolveMs}ms</span></span>
            <span>max · <span style={{ color: ovC.text }}>4.1s</span></span>
          </div>
        </OvCard>

        <OvCard title="Open conflicts" sub={`${s.conflictsOpen} awaiting resolution`} right={<OvChip color={ovC.warn} strong>action</OvChip>} padding={0}>
          <div style={{ overflow: 'hidden', flex: 1 }}>
            {[
              { kind: 'vertex', pair: 'ALF-03 ↔ BRV-07', node: 'core/cbs/search.py',    age: '4s'  },
              { kind: 'edge',   pair: 'HTL-12 → FOX-04', node: 'core/cbs/conflicts.py', age: '7s'  },
              { kind: 'vertex', pair: 'NOV-07 ↔ MIK-02', node: 'core/coordinator.py',   age: '11s' },
              { kind: 'swap',   pair: 'CHR-11 ↔ DLT-02', node: 'graph/builder.py',      age: '22s' },
              { kind: 'edge',   pair: 'KIL-08 → JLT-01', node: 'schemas/conflict.py',   age: '38s' },
            ].map((c,i) => (
              <div key={i} style={{ padding: '14px 22px', borderBottom: i < 4 ? `1px solid ${ovC.border}` : 'none', display: 'flex', alignItems: 'center', gap: 12 }}>
                <OvChip color={c.kind === 'vertex' ? ovC.warn : c.kind === 'edge' ? ovC.accent : ovC.violet} strong>{c.kind}</OvChip>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: 1 }}>
                  <div style={{ ...ovMono, fontSize: 11.5, color: ovC.text }}>{c.pair}</div>
                  <div style={{ ...ovMono, fontSize: 10.5, color: ovC.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.node}</div>
                </div>
                <span style={{ ...ovMono, fontSize: 11, color: ovC.muted }}>{c.age}</span>
              </div>
            ))}
          </div>
        </OvCard>
      </div>

      {/* events + agent roster row */}
      <div style={{ display: 'grid', gridTemplateColumns: '1.1fr 1fr', gap: 14, minHeight: 460 }}>

        <OvCard
          title="Hook events"
          sub="live tail · 14 ev/s"
          right={
            <div style={{ display: 'flex', gap: 6 }}>
              {['all','CONFLICT','REPLAN','PreToolUse','PostToolUse'].map((t,i) => (
                <span key={t} style={{ ...ovMono, fontSize: 11, padding: '5px 10px', borderRadius: 999, background: i===0 ? ovC.text : ovC.surface2, color: i===0 ? ovC.surface : ovC.muted, border: i===0 ? 'none' : `1px solid ${ovC.border}` }}>{t}</span>
              ))}
            </div>
          }
          padding={0}
        >
          <div style={{ display: 'grid', gridTemplateColumns: '90px 100px 80px 1fr', gap: 12, padding: '12px 22px', borderBottom: `1px solid ${ovC.border}`, ...ovMono, fontSize: 10.5, color: ovC.muted, letterSpacing: 0.2, textTransform: 'uppercase' }}>
            <span>time</span><span>kind</span><span>agent</span><span>target · message</span>
          </div>
          {window.CBS_EVENTS.slice(0, 13).map((e, i) => {
            const colMap = { CONFLICT: ovC.warn, RESOLVED: ovC.ok, REPLAN: ovC.violet, PreToolUse: ovC.accent, PostToolUse: ovC.accent, SPAWN: ovC.text, LOCK: ovC.muted };
            const col = colMap[e.kind];
            return (
              <div key={i} style={{ display: 'grid', gridTemplateColumns: '90px 100px 80px 1fr', gap: 12, padding: '10px 22px', borderBottom: `1px solid ${ovC.border}`, alignItems: 'center' }}>
                <span style={{ ...ovMono, fontSize: 11, color: ovC.muted }}>{e.t.slice(0,8)}<span style={{ color: ovC.faint }}>{e.t.slice(8)}</span></span>
                <span><OvChip color={col} strong>{e.kind}</OvChip></span>
                <span style={{ ...ovMono, fontSize: 11.5, color: ovC.text }}>{e.agent}</span>
                <span style={{ ...ovMono, fontSize: 11.5, color: ovC.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  <span style={{ color: ovC.text }}>{e.target}</span> · {e.msg}
                </span>
              </div>
            );
          })}
        </OvCard>

        <OvCard
          title="Agent roster"
          sub={`${window.CBS_AGENTS.length} total · sorted by activity`}
          right={
            <div style={{ display: 'flex', gap: 6 }}>
              <OvChip color={ovC.ok}>30 running</OvChip>
              <OvChip color={ovC.warn}>6 blocked</OvChip>
              <OvChip color={ovC.violet}>3 replan</OvChip>
            </div>
          }
          padding={0}
        >
          <div style={{ display: 'grid', gridTemplateColumns: '14px 70px 1fr 100px 50px', gap: 12, padding: '12px 22px', borderBottom: `1px solid ${ovC.border}`, ...ovMono, fontSize: 10.5, color: ovC.muted, letterSpacing: 0.2, textTransform: 'uppercase' }}>
            <span></span><span>id</span><span>working on</span><span style={{ textAlign: 'center' }}>load</span><span style={{ textAlign: 'right' }}>tok</span>
          </div>
          <div style={{ overflow: 'hidden' }}>
            {window.CBS_AGENTS.slice(0, 11).map(a => {
              const nodeLabel = (window.CBS_NODES.find(n => n.id === a.node) || {}).label || '—';
              return (
                <div key={a.id} style={{ display: 'grid', gridTemplateColumns: '14px 70px 1fr 100px 50px', alignItems: 'center', gap: 12, padding: '8px 22px', borderBottom: `1px solid ${ovC.border}` }}>
                  <OvDot color={ovStatusColor[a.status]} size={8} pulse={a.status === 'running' || a.status === 'replanning'} />
                  <span style={{ ...ovMono, fontSize: 11.5, color: ovC.text }}>{a.id}</span>
                  <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                    <span style={{ fontSize: 12, color: ovC.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.task}</span>
                    <span style={{ ...ovMono, fontSize: 10.5, color: ovC.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{nodeLabel}</span>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'center' }}>
                    <OvSpark data={a.spark} color={ovStatusColor[a.status]} w={90} h={22} fill={false} smooth />
                  </div>
                  <span style={{ ...ovMono, fontSize: 11, color: ovC.muted, textAlign: 'right' }}>{a.tokensK}k</span>
                </div>
              );
            })}
          </div>
        </OvCard>
      </div>

      {/* footer */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '14px 4px 0', color: ovC.faint, ...ovMono, fontSize: 11, borderTop: `1px solid ${ovC.border}` }}>
        <span>cbs-coord 0.4.2</span>
        <span>·</span>
        <span>haiku-4.5 · 8 workers</span>
        <span>·</span>
        <span>CT-nodes 1,204 · expanded 47,118</span>
        <span style={{ flex: 1 }} />
        <span>last replan 318ms ago</span>
        <span>·</span>
        <span>memory 412 MB</span>
        <span>·</span>
        <span>tick 250ms</span>
      </div>
    </div>
  );
}

window.ConceptBOverview = ConceptBOverview;
