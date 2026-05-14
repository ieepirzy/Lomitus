// Concept A v3 — graph-first, curated 4-color palette, pan + zoom,
// compact side panels, aggregate time-series for actionable rates.

// ─── curated palette · neutral grayscale + 4 functional accents ─
const c = {
  bg:        'oklch(0.135 0.011 250)',
  surface:   'oklch(0.185 0.013 250)',
  surface2:  'oklch(0.225 0.014 250)',
  surface3:  'oklch(0.27  0.016 250)',
  border:    'oklch(0.32  0.018 250)',
  borderSoft:'oklch(0.255 0.014 250)',
  text:      'oklch(0.97  0.005 250)',
  textDim:   'oklch(0.82  0.012 250)',
  muted:     'oklch(0.65  0.014 250)',
  faint:     'oklch(0.48  0.014 250)',

  // 4 functional accents, all chroma ≈ 0.16
  accent:    'oklch(0.72 0.16 240)',   // blue · selected, info, interactive
  ok:        'oklch(0.76 0.16 158)',   // green · running, resolved
  warn:      'oklch(0.80 0.16 75)',    // amber · blocked, hot, conflicts
  violet:    'oklch(0.70 0.16 295)',   // violet · CBS, replanning, CT-tree

  grid:      'oklch(0.19 0.011 250)',
};

const statusColor = {
  running:    c.ok,
  planning:   c.accent,
  blocked:    c.warn,
  replanning: c.violet,
  idle:       c.faint,
};

const fontSans = { fontFamily: '"IBM Plex Sans", system-ui, sans-serif', color: c.text };
const fontMono = { fontFamily: '"IBM Plex Mono", ui-monospace, monospace' };

// ─── primitives ────────────────────────────────────────────
function Panel({ title, sub, right, children, style, padding = 12, flex }) {
  return (
    <div style={{ background: c.surface, border: `1px solid ${c.border}`, borderRadius: 8, display: 'flex', flexDirection: 'column', minHeight: 0, flex, ...style }}>
      {(title || right) && (
        <div style={{ display: 'flex', alignItems: 'center', padding: '8px 12px', borderBottom: `1px solid ${c.borderSoft}`, gap: 8, minHeight: 36 }}>
          <div style={{ ...fontMono, fontSize: 10, letterSpacing: 0.7, textTransform: 'uppercase', color: c.textDim }}>{title}</div>
          {sub && <div style={{ ...fontMono, fontSize: 10, color: c.muted }}>{sub}</div>}
          <div style={{ flex: 1 }} />
          {right}
        </div>
      )}
      <div style={{ padding, flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>{children}</div>
    </div>
  );
}
function Dot({ color, size = 6, pulse = false }) {
  return <span style={{ display: 'inline-block', width: size, height: size, borderRadius: 999, background: color, animation: pulse ? 'pulse 1.8s infinite' : 'none', flexShrink: 0, boxShadow: pulse ? `0 0 5px ${color}` : 'none' }} />;
}
function Pill({ children, color = c.muted, strong = false }) {
  const bg = strong ? `color-mix(in oklch, ${color} 22%, ${c.surface})` : `color-mix(in oklch, ${color} 12%, transparent)`;
  return <span style={{ ...fontMono, fontSize: 9.5, letterSpacing: 0.2, color, background: bg, border: `1px solid color-mix(in oklch, ${color} ${strong?40:25}%, transparent)`, padding: '1px 6px', borderRadius: 3, whiteSpace: 'nowrap', display: 'inline-flex', alignItems: 'center', gap: 4 }}>{children}</span>;
}

// ─── force graph · zoomable + pannable ─────────────────────
function Graph({ selected, onSelect }) {
  const W = 1100, H = 680;
  const nodes = window.CBS_NODES_DENSE;
  const byId = React.useMemo(() => Object.fromEntries(nodes.map(n => [n.id, n])), [nodes]);

  const [zoom, setZoom] = React.useState(1);
  const [pan, setPan] = React.useState({ x: 0, y: 0 });
  const [dragging, setDragging] = React.useState(false);
  const dragRef = React.useRef(null);
  const svgRef = React.useRef(null);

  // non-passive wheel listener (React's onWheel is passive, can't preventDefault)
  React.useEffect(() => {
    const el = svgRef.current; if (!el) return;
    const onWheel = (e) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const sX = ((e.clientX - rect.left) / rect.width) * W;
      const sY = ((e.clientY - rect.top) / rect.height) * H;
      setZoom(z0 => {
        const factor = e.deltaY < 0 ? 1.15 : 1/1.15;
        const z = Math.max(0.6, Math.min(4.5, z0 * factor));
        setPan(p => ({
          x: sX - ((sX - p.x) / z0) * z,
          y: sY - ((sY - p.y) / z0) * z,
        }));
        return z;
      });
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);

  const onPointerDown = (e) => {
    if (e.target.closest('[data-node-hit]')) return;   // let node clicks through
    setDragging(true);
    dragRef.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e) => {
    if (!dragging || !dragRef.current) return;
    const rect = svgRef.current.getBoundingClientRect();
    const r = rect.width / W;
    setPan({
      x: dragRef.current.panX + (e.clientX - dragRef.current.x) / r,
      y: dragRef.current.panY + (e.clientY - dragRef.current.y) / r,
    });
  };
  const endDrag = () => setDragging(false);
  const reset = () => { setZoom(1); setPan({ x: 0, y: 0 }); };

  // edges: dim by default; hot or selected-adjacent get emphasis
  const sel = selected;
  const labelSet = new Set([sel, ...nodes.filter(n => n.agents >= 2).map(n => n.id)]);

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', overflow: 'hidden', background: `radial-gradient(ellipse at 60% 40%, ${c.surface2}, ${c.surface})`, borderRadius: 8 }}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        style={{ width: '100%', height: '100%', display: 'block', cursor: dragging ? 'grabbing' : 'grab', touchAction: 'none', userSelect: 'none' }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        <defs>
          <pattern id="gGrid" width="32" height="32" patternUnits="userSpaceOnUse" patternTransform={`translate(${pan.x % 32}, ${pan.y % 32}) scale(${zoom})`}>
            <path d="M 32 0 L 0 0 0 32" fill="none" stroke={c.grid} strokeWidth="0.7" />
          </pattern>
          <marker id="gArr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="3.5" markerHeight="3.5" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill={c.warn} />
          </marker>
          <radialGradient id="gHotHalo">
            <stop offset="0%" stopColor={c.warn} stopOpacity="0.5"/>
            <stop offset="100%" stopColor={c.warn} stopOpacity="0"/>
          </radialGradient>
          <radialGradient id="gSelHalo">
            <stop offset="0%" stopColor={c.accent} stopOpacity="0.45"/>
            <stop offset="100%" stopColor={c.accent} stopOpacity="0"/>
          </radialGradient>
        </defs>

        <rect width={W} height={H} fill="url(#gGrid)" opacity="0.7" />

        <g transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}>
          {/* cluster background labels — give the graph implicit structure */}
          {window.CBS_CLUSTERS_DENSE.map((cl) => (
            <text key={cl.name} x={cl.cx} y={cl.cy} textAnchor="middle"
              style={{ ...fontMono, fontSize: 48, fill: c.text, opacity: 0.05, letterSpacing: 4, textTransform: 'uppercase', pointerEvents: 'none', fontWeight: 600 }}>
              {cl.name}
            </text>
          ))}

          {/* edges */}
          {window.CBS_EDGES_DENSE.map(([a,b], i) => {
            const A = byId[a], B = byId[b]; if (!A || !B) return null;
            const isHot = A.hot && B.hot;
            const isSel = a === sel || b === sel;
            const stroke = isHot ? c.warn : isSel ? c.accent : c.borderSoft;
            const sw = (isHot ? 1.4 : isSel ? 1.2 : 0.6) / zoom;
            const op = isHot ? 0.85 : isSel ? 0.8 : 0.4;
            return (
              <g key={i}>
                <path id={`gE${i}`} d={`M${A.x},${A.y} L${B.x},${B.y}`} stroke={stroke} strokeWidth={sw} opacity={op} markerEnd={isHot ? 'url(#gArr)' : undefined} fill="none" />
                {(isHot || (isSel && i % 4 === 0)) && (
                  <circle r={2.4 / zoom} fill={isHot ? c.warn : c.accent}>
                    <animateMotion dur={`${4 + (i%4)}s`} repeatCount="indefinite">
                      <mpath xlinkHref={`#gE${i}`} />
                    </animateMotion>
                  </circle>
                )}
              </g>
            );
          })}

          {/* nodes — all share a single neutral palette; hot/sel via outline */}
          {nodes.map((n) => {
            const baseR = 4.5 + Math.min(n.agents || 0, 6) * 1.6 + (n.hot ? 2.5 : 0);
            const r = baseR;
            const isSel = sel === n.id;
            const fill = `color-mix(in oklch, ${c.text} 18%, ${c.surface2})`;
            const stroke = isSel ? c.accent : n.hot ? c.warn : c.muted;
            const sw = (isSel ? 2 : n.hot ? 1.4 : 1) / zoom;
            const showLabel = labelSet.has(n.id) || zoom >= 1.6;
            return (
              <g key={n.id} data-node-hit onClick={() => onSelect && onSelect(n.id)} style={{ cursor: 'pointer' }}>
                <title>{n.label} · {n.agents} agents · queue {n.queue}</title>
                {n.hot && <circle cx={n.x} cy={n.y} r={(r + 12)/1} fill="url(#gHotHalo)">
                  <animate attributeName="r" values={`${r+7};${r+14};${r+7}`} dur="2.4s" repeatCount="indefinite" />
                </circle>}
                {isSel && <circle cx={n.x} cy={n.y} r={r + 8} fill="url(#gSelHalo)" />}
                <circle cx={n.x} cy={n.y} r={r} fill={fill} stroke={stroke} strokeWidth={sw} />
                {n.agents >= 3 && (
                  <text x={n.x} y={n.y + 3} textAnchor="middle"
                    style={{ ...fontMono, fontSize: 9 / Math.max(1, zoom*0.7), fill: c.text, pointerEvents: 'none', fontWeight: 600 }}>
                    {n.agents}
                  </text>
                )}
                {showLabel && (
                  <text x={n.x} y={n.y + r + 8} textAnchor="middle"
                    style={{ ...fontMono, fontSize: 8.5 / Math.max(1, zoom*0.6), fill: isSel ? c.text : c.textDim, pointerEvents: 'none' }}>
                    {n.label.split('/').pop()}
                  </text>
                )}
              </g>
            );
          })}
        </g>
      </svg>

      {/* zoom controls — overlay, NOT in transformed group */}
      <div style={{ position: 'absolute', top: 10, right: 10, display: 'flex', flexDirection: 'column', gap: 4, background: c.surface, border: `1px solid ${c.border}`, borderRadius: 6, padding: 3 }}>
        <button onClick={() => setZoom(z => Math.min(4.5, z * 1.2))} title="Zoom in" style={btn}>+</button>
        <button onClick={() => setZoom(z => Math.max(0.6, z / 1.2))} title="Zoom out" style={btn}>−</button>
        <button onClick={reset} title="Reset view" style={{...btn, ...fontMono, fontSize: 9}}>⟲</button>
      </div>

      {/* zoom + drag readout */}
      <div style={{ position: 'absolute', bottom: 10, right: 10, ...fontMono, fontSize: 9.5, color: c.muted, background: `color-mix(in oklch, ${c.surface} 80%, transparent)`, border: `1px solid ${c.borderSoft}`, borderRadius: 4, padding: '3px 7px' }}>
        {(zoom * 100).toFixed(0)}% · drag to pan · wheel to zoom
      </div>

      {/* legend */}
      <div style={{ position: 'absolute', bottom: 10, left: 10, display: 'flex', gap: 12, background: `color-mix(in oklch, ${c.surface} 80%, transparent)`, border: `1px solid ${c.borderSoft}`, borderRadius: 4, padding: '4px 10px', ...fontMono, fontSize: 9.5, color: c.muted }}>
        <span><Dot color={c.warn} size={6} /> &nbsp;hot</span>
        <span><Dot color={c.accent} size={6} /> &nbsp;selected</span>
        <span><Dot color={c.muted} size={6} /> &nbsp;node</span>
        <span><span style={{ display: 'inline-block', width: 14, height: 1.5, background: c.warn, verticalAlign: 'middle' }}></span> &nbsp;conflict edge</span>
      </div>
    </div>
  );
}
const btn = {
  width: 22, height: 22, background: 'transparent', border: 'none',
  color: c.textDim, cursor: 'pointer', fontSize: 14, lineHeight: 1, borderRadius: 4,
  display: 'grid', placeItems: 'center',
};

// ─── time-series mini chart ────────────────────────────────
function TS({ ts, color, w = 100, h = 36 }) {
  const data = ts.data;
  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const step = w / (data.length - 1);
  const pts = data.map((v, i) => {
    const norm = max === min ? 0.5 : (v - min) / (max - min);
    return [i * step, h - norm * (h - 6) - 3];
  });
  // smooth curve
  let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 1; i < pts.length; i++) {
    const p = pts[i], pp = pts[i-1];
    const cx = (pp[0] + p[0]) / 2;
    d += ` C${cx.toFixed(1)},${pp[1].toFixed(1)} ${cx.toFixed(1)},${p[1].toFixed(1)} ${p[0].toFixed(1)},${p[1].toFixed(1)}`;
  }
  const gid = `tsG-${ts.label.replace(/\W/g,'')}`;
  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ display: 'block', overflow: 'visible' }}>
      <defs>
        <linearGradient id={gid} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.32"/>
          <stop offset="100%" stopColor={color} stopOpacity="0"/>
        </linearGradient>
      </defs>
      <path d={`${d} L${w},${h} L0,${h} Z`} fill={`url(#${gid})`} />
      <path d={d} fill="none" stroke={color} strokeWidth={1.3} strokeLinejoin="round" strokeLinecap="round" />
      {/* last point dot */}
      <circle cx={pts[pts.length-1][0]} cy={pts[pts.length-1][1]} r={2.4} fill={color} />
    </svg>
  );
}
function TSCard({ ts, color }) {
  const dColor = ts.deltaGood ? c.ok : c.warn;
  const arrow = ts.deltaDir === 'up' ? '▲' : ts.deltaDir === 'down' ? '▼' : '◆';
  return (
    <div style={{ flex: 1, background: c.surface, border: `1px solid ${c.border}`, borderRadius: 8, padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 6, minWidth: 0 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Dot color={color} size={5} />
        <span style={{ ...fontMono, fontSize: 10, color: c.muted, letterSpacing: 0.4, textTransform: 'uppercase', flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{ts.label}</span>
        <span style={{ ...fontMono, fontSize: 9.5, color: dColor }}>{arrow} {ts.delta}</span>
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
        <span style={{ ...fontSans, fontSize: 22, fontWeight: 500, color: c.text, lineHeight: 1, letterSpacing: -0.4 }}>{typeof ts.value === 'number' && ts.value < 10 ? ts.value.toFixed(2) : ts.value}</span>
        <span style={{ ...fontMono, fontSize: 10, color: c.muted }}>{ts.unit}</span>
      </div>
      <div style={{ flex: 1, minHeight: 36 }}>
        <TS ts={ts} color={color} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', ...fontMono, fontSize: 9, color: c.faint }}>
        <span>−60m</span><span>now</span>
      </div>
    </div>
  );
}

// ─── compact stat tile ─────────────────────────────────────
function Stat({ label, value, unit, sub, color = c.text }) {
  return (
    <div style={{ flex: 1, background: c.surface, border: `1px solid ${c.border}`, borderRadius: 8, padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
      <div style={{ ...fontMono, fontSize: 9.5, letterSpacing: 0.5, textTransform: 'uppercase', color: c.muted, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{label}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
        <span style={{ ...fontSans, fontSize: 22, fontWeight: 500, color, lineHeight: 1, letterSpacing: -0.4 }}>{value}</span>
        {unit && <span style={{ ...fontMono, fontSize: 10, color: c.muted }}>{unit}</span>}
      </div>
      <div style={{ ...fontMono, fontSize: 9.5, color: c.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sub}</div>
    </div>
  );
}

// ─── compact agent row (single line) ───────────────────────
function AgentRow({ a, onSelect, isSel }) {
  const col = statusColor[a.status];
  const node = window.CBS_NODES_DENSE.find(n => n.id === a.node);
  const nodeLabel = node ? node.label.split('/').pop() : '—';
  const prioCol = a.prio >= 4 ? c.warn : a.prio >= 3 ? c.accent : c.muted;
  const pct = a.ofSteps ? (a.step / a.ofSteps) * 100 : 0;
  const showProgress = a.status === 'running' || a.status === 'planning';

  let aux = null;
  if (a.status === 'blocked')      aux = <span style={{ ...fontMono, fontSize: 9.5, color: c.warn }}>wait {(a.waitMs/1000).toFixed(1)}s</span>;
  else if (a.status === 'replanning') aux = <span style={{ ...fontMono, fontSize: 9.5, color: c.violet }}>ct {a.ctDepth}</span>;
  else if (a.status === 'idle')     aux = <span style={{ ...fontMono, fontSize: 9.5, color: c.faint }}>idle</span>;
  else                              aux = <span style={{ ...fontMono, fontSize: 9.5, color: c.muted }}>{a.step}/{a.ofSteps}</span>;

  return (
    <div
      onClick={() => onSelect && onSelect(a.node)}
      style={{
        display: 'grid',
        gridTemplateColumns: '10px 50px 1fr 40px 18px',
        alignItems: 'center', gap: 6,
        padding: '4px 10px',
        borderBottom: `1px solid ${c.borderSoft}`,
        background: isSel ? c.surface2 : 'transparent',
        cursor: 'pointer',
        position: 'relative',
      }}
    >
      <Dot color={col} size={6} pulse={a.status === 'running' || a.status === 'replanning'} />
      <span style={{ ...fontMono, color: c.text, fontSize: 10.5 }}>{a.id}</span>
      <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, gap: 0 }}>
        <span style={{ fontSize: 11, color: c.textDim, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', lineHeight: 1.3 }}>{a.task}</span>
        <span style={{ ...fontMono, fontSize: 9, color: c.faint, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', lineHeight: 1.2 }}>{nodeLabel}</span>
      </div>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>{aux}</div>
      <span style={{ ...fontMono, fontSize: 9, color: prioCol, textAlign: 'center', border: `1px solid color-mix(in oklch, ${prioCol} 40%, transparent)`, borderRadius: 2, padding: '0', lineHeight: '14px', background: `color-mix(in oklch, ${prioCol} 12%, transparent)` }}>{a.prio}</span>
      {showProgress && (
        <div style={{ position: 'absolute', left: 0, bottom: 0, height: 1.5, width: `${pct}%`, background: col, opacity: 0.55 }} />
      )}
    </div>
  );
}

// ─── event row ─────────────────────────────────────────────
const kindColor = {
  CONFLICT: c.warn, RESOLVED: c.ok, REPLAN: c.violet,
  PreToolUse: c.accent, PostToolUse: c.accent, SPAWN: c.text,
  LOCK: c.muted, BYPASS: c.violet,
};
function EventRow({ e }) {
  const col = kindColor[e.kind] || c.muted;
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '80px 88px 64px 1fr', gap: 10, padding: '4px 12px', borderBottom: `1px solid ${c.borderSoft}`, alignItems: 'center' }}>
      <span style={{ ...fontMono, fontSize: 10, color: c.faint }}>{e.t.slice(0,12)}</span>
      <span style={{ ...fontMono, fontSize: 10, color: col, letterSpacing: 0.3, fontWeight: 500 }}>{e.kind}</span>
      <span style={{ ...fontMono, fontSize: 10, color: c.textDim }}>{e.agent}</span>
      <span style={{ ...fontMono, fontSize: 10, color: c.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        <span style={{ color: c.text }}>{e.target}</span> · {e.msg}
      </span>
    </div>
  );
}

// ─── main ──────────────────────────────────────────────────
function ConceptA2() {
  const [sel, setSel] = React.useState('cbsS');
  const node = window.CBS_NODES_DENSE.find(n => n.id === sel);
  const onNode = window.CBS_AGENTS_DENSE.filter(a => a.node === sel);
  const s = window.CBS_STATS_DENSE;
  const hotNodes = [...window.CBS_NODES_DENSE].sort((a,b) => (b.queue + b.agents) - (a.queue + a.agents)).slice(0, 6);

  return (
    <div style={{ minHeight: '100vh', background: c.bg, color: c.text, ...fontSans, display: 'flex', flexDirection: 'column', fontSize: 12 }}>
      <style>{`
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.4} }
      `}</style>

      {/* top bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '10px 20px', borderBottom: `1px solid ${c.border}`, background: c.surface, height: 44 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
          <svg width="20" height="20" viewBox="0 0 22 22"><circle cx="11" cy="11" r="9.5" fill="none" stroke={c.accent} strokeWidth="1.5"/><circle cx="11" cy="11" r="3" fill={c.accent}/><circle cx="11" cy="3.5" r="1.5" fill={c.accent}/><circle cx="18.5" cy="11" r="1.5" fill={c.accent}/><circle cx="11" cy="18.5" r="1.5" fill={c.accent}/><circle cx="3.5" cy="11" r="1.5" fill={c.accent}/></svg>
          <div style={{ ...fontMono, fontSize: 12, letterSpacing: 0.7, color: c.text }}>CBS<span style={{ color: c.muted }}> · coordinator</span></div>
        </div>
        <div style={{ width: 1, height: 16, background: c.border }} />
        <Pill color={c.ok} strong><Dot color={c.ok} size={5} pulse /> &nbsp;RUNNING</Pill>
        <Pill color={c.muted}>uptime {s.uptime}</Pill>
        <Pill color={c.muted}>tick 250ms</Pill>
        <Pill color={c.muted}>ws · 12 sub</Pill>
        <div style={{ flex: 1 }} />
        <div style={{ display: 'flex', gap: 2 }}>
          {['graph','agents','conflicts','events','search-tree','metrics'].map((k,i)=>(
            <span key={k} style={{ ...fontMono, fontSize: 10.5, padding: '5px 9px', borderRadius: 4, background: i===0?c.surface3:'transparent', color: i===0?c.text:c.muted, border: `1px solid ${i===0?c.border:'transparent'}`, cursor: 'pointer' }}>{k}</span>
          ))}
        </div>
        <div style={{ width: 1, height: 16, background: c.border }} />
        <Pill color={c.accent} strong>repo · cbs-coord/main</Pill>
      </div>

      <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 12 }}>

        {/* compact stat strip — 6 KPIs only */}
        <div style={{ display: 'flex', gap: 10 }}>
          <Stat label="active agents"   value={s.active}                                       color={c.text}    sub={`${s.byStatus.running}r · ${s.byStatus.blocked}b · ${s.byStatus.planning}p`} />
          <Stat label="conflicts · open" value={s.conflictsOpen}                               color={c.warn}    sub={`${s.conflictsByKind.vertex}v · ${s.conflictsByKind.edge}e · ${s.conflictsByKind.swap}s`} />
          <Stat label="resolved · 24h"   value={s.conflictsResolved24h}                        color={c.ok}      sub={`p95 ${(s.p95resolveMs/1000).toFixed(1)}s · p50 ${s.p50resolveMs}ms`} />
          <Stat label="ct-tree open"     value={s.ctOpen.toLocaleString()}                     color={c.violet}  sub={`${(s.ctExpanded/1000).toFixed(0)}k expanded · d ${s.ctDepth}`} />
          <Stat label="graph"            value={s.nodes} unit="nodes"                          color={c.text}    sub={`${s.edges} edges · 9 mods`} />
          <Stat label="tokens · 24h"     value="1.84" unit="M"                                 color={c.text}    sub={`$${s.tokensBurnUsd} · 8 workers`} />
        </div>

        {/* main 3-col — GRAPH dominates */}
        <div style={{ display: 'grid', gridTemplateColumns: '240px minmax(0, 1fr) 280px', gap: 10, height: 720 }}>

          {/* LEFT — compact agent roster */}
          <Panel
            title="Agents"
            sub={`· ${window.CBS_AGENTS_DENSE.length}`}
            right={
              <div style={{ display: 'flex', gap: 5 }}>
                {Object.entries(s.byStatus).map(([k,v]) => (
                  <span key={k} title={k} style={{ display: 'inline-flex', alignItems: 'center', gap: 2 }}>
                    <Dot color={statusColor[k]} size={4} />
                    <span style={{ ...fontMono, fontSize: 9, color: c.muted }}>{v}</span>
                  </span>
                ))}
              </div>
            }
            padding={0}
          >
            <div style={{ display: 'grid', gridTemplateColumns: '10px 50px 1fr 40px 18px', gap: 6, padding: '5px 10px', borderBottom: `1px solid ${c.border}`, ...fontMono, fontSize: 9, color: c.faint, letterSpacing: 0.4, textTransform: 'uppercase', background: c.surface2 }}>
              <span></span><span>id</span><span>task</span><span style={{ textAlign: 'right' }}>step</span><span style={{ textAlign: 'center' }}>p</span>
            </div>
            <div style={{ overflow: 'auto', flex: 1 }}>
              {window.CBS_AGENTS_DENSE.map(a => <AgentRow key={a.id} a={a} onSelect={setSel} isSel={a.node === sel} />)}
            </div>
          </Panel>

          {/* CENTER — graph dominates */}
          <Panel
            title="Dependency graph"
            sub={`· ${window.CBS_NODES_DENSE.length} nodes · ${window.CBS_EDGES_DENSE.length} edges`}
            right={
              <div style={{ display: 'flex', gap: 5, alignItems: 'center' }}>
                <Pill color={c.warn} strong>4 hot</Pill>
                <Pill color={c.muted}>force</Pill>
                <Pill color={c.muted}>module ▾</Pill>
              </div>
            }
            padding={0}
          >
            <div style={{ flex: 1, minHeight: 0 }}>
              <Graph selected={sel} onSelect={setSel} />
            </div>
          </Panel>

          {/* RIGHT — compact stack: node detail, open conflicts, hot nodes */}
          <div style={{ display: 'grid', gridTemplateRows: 'auto minmax(0, 1fr) minmax(0, 1fr)', gap: 10, minWidth: 0, minHeight: 0 }}>
            <Panel title="Selected node" right={<Pill color={c.accent}>{node.mod}</Pill>} padding={10}>
              <div style={{ ...fontMono, fontSize: 11.5, color: c.text, marginBottom: 8, wordBreak: 'break-all', lineHeight: 1.3 }}>{node.label}</div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 5, marginBottom: 6 }}>
                {[
                  { l: 'agents', v: node.agents, c: c.text },
                  { l: 'queue',  v: node.queue,  c: node.queue > 2 ? c.warn : c.text },
                  { l: 'in',     v: window.CBS_EDGES_DENSE.filter(([,b])=>b===node.id).length, c: c.text },
                  { l: 'out',    v: window.CBS_EDGES_DENSE.filter(([a])=>a===node.id).length, c: c.text },
                ].map(b => (
                  <div key={b.l} style={{ padding: '5px 7px', background: c.surface2, border: `1px solid ${c.borderSoft}`, borderRadius: 4 }}>
                    <div style={{ ...fontMono, fontSize: 8.5, color: c.faint, textTransform: 'uppercase', letterSpacing: 0.3 }}>{b.l}</div>
                    <div style={{ ...fontSans, fontSize: 16, fontWeight: 500, color: b.c, lineHeight: 1.1, letterSpacing: -0.3 }}>{b.v}</div>
                  </div>
                ))}
              </div>
              {onNode.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 3, maxHeight: 96, overflow: 'auto' }}>
                  {onNode.slice(0, 4).map(a => (
                    <div key={a.id} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 6px', background: c.surface2, border: `1px solid ${c.borderSoft}`, borderRadius: 3 }}>
                      <Dot color={statusColor[a.status]} size={5} pulse={a.status==='running'} />
                      <span style={{ ...fontMono, fontSize: 10, color: c.text }}>{a.id}</span>
                      <span style={{ ...fontMono, fontSize: 9.5, color: c.muted, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.task}</span>
                    </div>
                  ))}
                  {onNode.length > 4 && <span style={{ ...fontMono, fontSize: 9.5, color: c.faint, padding: '2px 6px' }}>+{onNode.length - 4} more</span>}
                </div>
              )}
            </Panel>

            <Panel title="Open conflicts" sub={`· ${s.conflictsOpen}`} padding={0}>
              <div style={{ overflow: 'auto', flex: 1 }}>
                {window.CBS_OPEN_CONFLICTS.map((cf,i) => {
                  const col = cf.kind === 'vertex' ? c.warn : cf.kind === 'edge' ? c.accent : c.violet;
                  return (
                    <div key={i} onClick={() => {
                      const target = window.CBS_NODES_DENSE.find(n => n.label === cf.node);
                      if (target) setSel(target.id);
                    }} style={{ padding: '6px 10px', borderBottom: `1px solid ${c.borderSoft}`, display: 'flex', flexDirection: 'column', gap: 2, cursor: 'pointer' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <Pill color={col} strong>{cf.kind}</Pill>
                        <span style={{ ...fontMono, fontSize: 10, color: c.text }}>{cf.pair}</span>
                        <div style={{ flex: 1 }} />
                        <span style={{ ...fontMono, fontSize: 9.5, color: c.faint }}>{cf.age}</span>
                      </div>
                      <span style={{ ...fontMono, fontSize: 9.5, color: c.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{cf.node}</span>
                    </div>
                  );
                })}
              </div>
            </Panel>

            <Panel title="Hot nodes" sub="· contention" padding={0}>
              <div style={{ overflow: 'auto', flex: 1 }}>
                {hotNodes.map((n, i) => {
                  const cont = n.agents + n.queue;
                  const max = hotNodes[0].agents + hotNodes[0].queue;
                  const isSel = sel === n.id;
                  const barCol = cont >= max*0.75 ? c.warn : cont >= max*0.5 ? c.accent : c.ok;
                  return (
                    <div key={n.id} onClick={() => setSel(n.id)} style={{ display: 'grid', gridTemplateColumns: '14px 1fr 20px 20px', gap: 6, padding: '5px 10px', borderBottom: `1px solid ${c.borderSoft}`, alignItems: 'center', background: isSel ? c.surface2 : 'transparent', cursor: 'pointer', position: 'relative' }}>
                      <span style={{ ...fontMono, fontSize: 9, color: c.faint }}>{i+1}</span>
                      <span style={{ ...fontMono, fontSize: 10, color: c.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{n.label.replace('.py','')}</span>
                      <span style={{ ...fontMono, fontSize: 9.5, color: c.muted, textAlign: 'right' }}>{n.agents}a</span>
                      <span style={{ ...fontMono, fontSize: 9.5, color: n.queue >= 4 ? c.warn : c.muted, textAlign: 'right' }}>{n.queue}q</span>
                      <div style={{ position: 'absolute', left: 0, bottom: 0, height: 1.5, width: `${(cont/max)*100}%`, background: barCol, opacity: 0.55 }} />
                    </div>
                  );
                })}
              </div>
            </Panel>
          </div>
        </div>

        {/* time-series strip — 4 aggregate rates */}
        <div style={{ display: 'flex', gap: 10 }}>
          <TSCard ts={window.CBS_TS.throughput} color={c.ok} />
          <TSCard ts={window.CBS_TS.conflicts}  color={c.warn} />
          <TSCard ts={window.CBS_TS.ctExpand}   color={c.violet} />
          <TSCard ts={window.CBS_TS.tokens}     color={c.accent} />
        </div>

        {/* bottom row: events + ledger + resolution mix */}
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.4fr) minmax(0, 1fr) 240px', gap: 10, height: 280 }}>
          <Panel
            title="Hook event stream"
            sub="· live tail"
            right={
              <div style={{ display: 'flex', gap: 5 }}>
                <Pill color={c.ok} strong><Dot color={c.ok} size={5} pulse /> &nbsp;tailing</Pill>
                <Pill color={c.muted}>14 ev/s</Pill>
                <Pill color={c.muted}>filter ▾</Pill>
              </div>
            }
            padding={0}
          >
            <div style={{ display: 'grid', gridTemplateColumns: '80px 88px 64px 1fr', gap: 10, padding: '5px 12px', borderBottom: `1px solid ${c.border}`, ...fontMono, fontSize: 9, color: c.faint, letterSpacing: 0.4, textTransform: 'uppercase', background: c.surface2 }}>
              <span>time</span><span>kind</span><span>agent</span><span>target · msg</span>
            </div>
            <div style={{ flex: 1, overflow: 'auto' }}>
              {window.CBS_EVENTS_DENSE.map((e, i) => <EventRow key={i} e={e} />)}
            </div>
          </Panel>

          <Panel title="Recent resolutions" sub="last 7" padding={0}>
            <div style={{ display: 'grid', gridTemplateColumns: '48px 44px 1fr 70px 38px', gap: 6, padding: '5px 12px', borderBottom: `1px solid ${c.border}`, ...fontMono, fontSize: 9, color: c.faint, letterSpacing: 0.4, textTransform: 'uppercase', background: c.surface2 }}>
              <span>id</span><span>kind</span><span>pair · node</span><span>via</span><span style={{ textAlign: 'right' }}>ms</span>
            </div>
            <div style={{ flex: 1, overflow: 'auto' }}>
              {window.CBS_LEDGER.map(cf => {
                const col = cf.kind === 'vertex' ? c.warn : cf.kind === 'edge' ? c.accent : c.violet;
                const viaCol = cf.via === 'priority-shift' ? c.violet : cf.via === 'reroute' ? c.accent : cf.via === 'bypass-cut' ? c.violet : cf.via === 'wait' ? c.muted : c.warn;
                return (
                  <div key={cf.id} style={{ display: 'grid', gridTemplateColumns: '48px 44px 1fr 70px 38px', gap: 6, padding: '5px 12px', borderBottom: `1px solid ${c.borderSoft}`, alignItems: 'center' }}>
                    <span style={{ ...fontMono, fontSize: 9.5, color: c.faint }}>{cf.id}</span>
                    <Pill color={col}>{cf.kind}</Pill>
                    <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                      <span style={{ ...fontMono, fontSize: 10, color: c.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{cf.pair}</span>
                      <span style={{ ...fontMono, fontSize: 9, color: c.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{cf.node}</span>
                    </div>
                    <Pill color={viaCol}>{cf.via}</Pill>
                    <span style={{ ...fontMono, fontSize: 10, color: c.textDim, textAlign: 'right' }}>{cf.ms}</span>
                  </div>
                );
              })}
            </div>
          </Panel>

          <Panel title="Resolution mix" sub="24h · n=184" padding={10}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 9, marginTop: 2 }}>
              {Object.entries(s.conflictsResolvedKind).map(([k, v]) => {
                const total = Object.values(s.conflictsResolvedKind).reduce((a,b)=>a+b,0);
                const pct = (v/total)*100;
                const col = k === 'priority-shift' ? c.violet : k === 'reroute' ? c.accent : k === 'bypass-cut' ? c.violet : k === 'wait' ? c.muted : c.warn;
                return (
                  <div key={k}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
                      <Dot color={col} size={5} />
                      <span style={{ ...fontMono, fontSize: 9.5, color: c.textDim, flex: 1 }}>{k}</span>
                      <span style={{ ...fontMono, fontSize: 10, color: c.text }}>{v}</span>
                      <span style={{ ...fontMono, fontSize: 9, color: c.faint, width: 30, textAlign: 'right' }}>{pct.toFixed(0)}%</span>
                    </div>
                    <div style={{ height: 3, background: c.surface2, borderRadius: 2, overflow: 'hidden' }}>
                      <div style={{ width: `${pct}%`, height: '100%', background: col, borderRadius: 2 }} />
                    </div>
                  </div>
                );
              })}
            </div>
          </Panel>
        </div>
      </div>

      {/* footer */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '6px 20px', color: c.faint, ...fontMono, fontSize: 10, borderTop: `1px solid ${c.border}`, background: c.surface, marginTop: 12 }}>
        <span>cbs-coord/0.4.2</span>
        <span>·</span>
        <span>haiku-4.5 · 8 workers</span>
        <span>·</span>
        <span>ct {(s.ctExpanded/1000).toFixed(0)}k expanded</span>
        <span style={{ flex: 1 }} />
        <span>replan <span style={{ color: c.textDim }}>318ms</span> ago</span>
        <span>·</span>
        <span>mem 412 MB</span>
        <span>·</span>
        <span>tick 250ms</span>
        <span>·</span>
        <span style={{ color: c.ok, animation: 'blink 2s infinite' }}>● live</span>
      </div>
    </div>
  );
}

window.ConceptA2 = ConceptA2;
