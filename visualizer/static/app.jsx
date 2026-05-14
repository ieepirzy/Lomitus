// CBS Coordinator — live visualizer
// Adapted from the Claude Design prototype; data comes from /api/* instead of static globals.

// ─── palette ───────────────────────────────────────────────
const c = {
  bg:         'oklch(0.135 0.011 250)',
  surface:    'oklch(0.185 0.013 250)',
  surface2:   'oklch(0.225 0.014 250)',
  surface3:   'oklch(0.27  0.016 250)',
  border:     'oklch(0.32  0.018 250)',
  borderSoft: 'oklch(0.255 0.014 250)',
  text:       'oklch(0.97  0.005 250)',
  textDim:    'oklch(0.82  0.012 250)',
  muted:      'oklch(0.65  0.014 250)',
  faint:      'oklch(0.48  0.014 250)',
  accent:     'oklch(0.72 0.16 240)',
  ok:         'oklch(0.76 0.16 158)',
  warn:       'oklch(0.80 0.16 75)',
  violet:     'oklch(0.70 0.16 295)',
  grid:       'oklch(0.19 0.011 250)',
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

// ─── layout: pack nodes into auto-placed cluster circles ───
function computeLayout(apiNodes, W, H) {
  if (!apiNodes.length) return { nodes: [], clusters: [] };

  const groups = {};
  for (const n of apiNodes) {
    (groups[n.mod] = groups[n.mod] || []).push(n);
  }
  const mods = Object.keys(groups);
  const N = mods.length;

  const cx = W / 2, cy = H / 2;
  const arrangeR = Math.min(W, H) * 0.36;

  const clusterCenters = {};
  mods.forEach((mod, i) => {
    if (N === 1) {
      clusterCenters[mod] = { cx, cy };
    } else {
      const angle = (i / N) * 2 * Math.PI - Math.PI / 2;
      clusterCenters[mod] = {
        cx: cx + Math.cos(angle) * arrangeR,
        cy: cy + Math.sin(angle) * arrangeR,
      };
    }
  });

  const maxCount = Math.max(...mods.map(m => groups[m].length));
  const packR = (count) => Math.max(55, Math.min(140, 35 + (count / maxCount) * 100));

  const nodes = [];
  const clusters = [];

  mods.forEach((mod) => {
    const items = groups[mod];
    const { cx: ccx, cy: ccy } = clusterCenters[mod];
    const r = packR(items.length);
    clusters.push({ name: mod, cx: ccx, cy: ccy });

    items.forEach((n, j) => {
      const t = (j + 0.5) / items.length;
      const pr = r * Math.sqrt(t) * 0.92;
      const angle = j * 2.39996323; // golden angle
      nodes.push({
        ...n,
        x: ccx + Math.cos(angle) * pr,
        y: ccy + Math.sin(angle) * pr,
      });
    });
  });

  return { nodes, clusters };
}

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
  return <span style={{ ...fontMono, fontSize: 9.5, letterSpacing: 0.2, color, background: bg, border: `1px solid color-mix(in oklch, ${color} ${strong ? 40 : 25}%, transparent)`, padding: '1px 6px', borderRadius: 3, whiteSpace: 'nowrap', display: 'inline-flex', alignItems: 'center', gap: 4 }}>{children}</span>;
}

function EmptyState({ label }) {
  return (
    <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', ...fontMono, fontSize: 10, color: c.faint }}>
      {label}
    </div>
  );
}

// ─── graph · pan + zoom SVG ────────────────────────────────
const btn = {
  width: 22, height: 22, background: 'transparent', border: 'none',
  color: c.textDim, cursor: 'pointer', fontSize: 14, lineHeight: 1, borderRadius: 4,
  display: 'grid', placeItems: 'center',
};

function Graph({ nodes, edges, clusters, selected, onSelect }) {
  const W = 1100, H = 680;
  const byId = React.useMemo(() => Object.fromEntries(nodes.map(n => [n.id, n])), [nodes]);

  const [zoom, setZoom] = React.useState(1);
  const [pan, setPan] = React.useState({ x: 0, y: 0 });
  const [dragging, setDragging] = React.useState(false);
  const dragRef = React.useRef(null);
  const svgRef = React.useRef(null);

  React.useEffect(() => {
    const el = svgRef.current; if (!el) return;
    const onWheel = (e) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const sX = ((e.clientX - rect.left) / rect.width) * W;
      const sY = ((e.clientY - rect.top) / rect.height) * H;
      setZoom(z0 => {
        const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
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
    if (e.target.closest('[data-node-hit]')) return;
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

  const labelSet = new Set([selected, ...nodes.filter(n => n.hot).map(n => n.id)]);
  const hotCount = nodes.filter(n => n.hot).length;

  if (!nodes.length) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', background: `radial-gradient(ellipse at 60% 40%, ${c.surface2}, ${c.surface})`, borderRadius: 8, ...fontMono, fontSize: 11, color: c.faint }}>
        no graph data · run the coordinator to index files
      </div>
    );
  }

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
            <stop offset="0%" stopColor={c.warn} stopOpacity="0.5" />
            <stop offset="100%" stopColor={c.warn} stopOpacity="0" />
          </radialGradient>
          <radialGradient id="gSelHalo">
            <stop offset="0%" stopColor={c.accent} stopOpacity="0.45" />
            <stop offset="100%" stopColor={c.accent} stopOpacity="0" />
          </radialGradient>
        </defs>

        <rect width={W} height={H} fill="url(#gGrid)" opacity="0.7" />

        <g transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}>
          {clusters.map((cl) => (
            <text key={cl.name} x={cl.cx} y={cl.cy} textAnchor="middle"
              style={{ ...fontMono, fontSize: 48, fill: c.text, opacity: 0.05, letterSpacing: 4, textTransform: 'uppercase', pointerEvents: 'none', fontWeight: 600 }}>
              {cl.name}
            </text>
          ))}

          {edges.map(([a, b], i) => {
            const A = byId[a], B = byId[b]; if (!A || !B) return null;
            const isHot = A.hot && B.hot;
            const isSel = a === selected || b === selected;
            const stroke = isHot ? c.warn : isSel ? c.accent : c.borderSoft;
            const sw = (isHot ? 1.4 : isSel ? 1.2 : 0.6) / zoom;
            const op = isHot ? 0.85 : isSel ? 0.8 : 0.4;
            return (
              <g key={i}>
                <path id={`gE${i}`} d={`M${A.x},${A.y} L${B.x},${B.y}`} stroke={stroke} strokeWidth={sw} opacity={op} markerEnd={isHot ? 'url(#gArr)' : undefined} fill="none" />
                {(isHot || (isSel && i % 4 === 0)) && (
                  <circle r={2.4 / zoom} fill={isHot ? c.warn : c.accent}>
                    <animateMotion dur={`${4 + (i % 4)}s`} repeatCount="indefinite">
                      <mpath xlinkHref={`#gE${i}`} />
                    </animateMotion>
                  </circle>
                )}
              </g>
            );
          })}

          {nodes.map((n) => {
            const baseR = 4.5 + Math.min(n.structural_count || 0, 8) * 1.2 + (n.hot ? 2.5 : 0);
            const r = baseR;
            const isSel = selected === n.id;
            const fill = `color-mix(in oklch, ${c.text} 18%, ${c.surface2})`;
            const stroke = isSel ? c.accent : n.hot ? c.warn : c.muted;
            const sw = (isSel ? 2 : n.hot ? 1.4 : 1) / zoom;
            const showLabel = labelSet.has(n.id) || zoom >= 1.6;
            return (
              <g key={n.id} data-node-hit onClick={() => onSelect && onSelect(n.id)} style={{ cursor: 'pointer' }}>
                <title>{n.label} · {n.structural_count} symbols · {n.agents} agent{n.agents !== 1 ? 's' : ''}</title>
                {n.hot && (
                  <circle cx={n.x} cy={n.y} r={(r + 12) / 1} fill="url(#gHotHalo)">
                    <animate attributeName="r" values={`${r + 7};${r + 14};${r + 7}`} dur="2.4s" repeatCount="indefinite" />
                  </circle>
                )}
                {isSel && <circle cx={n.x} cy={n.y} r={r + 8} fill="url(#gSelHalo)" />}
                <circle cx={n.x} cy={n.y} r={r} fill={fill} stroke={stroke} strokeWidth={sw} />
                {n.agents >= 1 && (
                  <text x={n.x} y={n.y + 3} textAnchor="middle"
                    style={{ ...fontMono, fontSize: 9 / Math.max(1, zoom * 0.7), fill: c.text, pointerEvents: 'none', fontWeight: 600 }}>
                    {n.agents}
                  </text>
                )}
                {showLabel && (
                  <text x={n.x} y={n.y + r + 8} textAnchor="middle"
                    style={{ ...fontMono, fontSize: 8.5 / Math.max(1, zoom * 0.6), fill: isSel ? c.text : c.textDim, pointerEvents: 'none' }}>
                    {n.label.split('/').pop()}
                  </text>
                )}
              </g>
            );
          })}
        </g>
      </svg>

      <div style={{ position: 'absolute', top: 10, right: 10, display: 'flex', flexDirection: 'column', gap: 4, background: c.surface, border: `1px solid ${c.border}`, borderRadius: 6, padding: 3 }}>
        <button onClick={() => setZoom(z => Math.min(4.5, z * 1.2))} title="Zoom in" style={btn}>+</button>
        <button onClick={() => setZoom(z => Math.max(0.6, z / 1.2))} title="Zoom out" style={btn}>−</button>
        <button onClick={reset} title="Reset view" style={{ ...btn, ...fontMono, fontSize: 9 }}>⟲</button>
      </div>

      <div style={{ position: 'absolute', bottom: 10, right: 10, ...fontMono, fontSize: 9.5, color: c.muted, background: `color-mix(in oklch, ${c.surface} 80%, transparent)`, border: `1px solid ${c.borderSoft}`, borderRadius: 4, padding: '3px 7px' }}>
        {(zoom * 100).toFixed(0)}% · drag to pan · wheel to zoom
      </div>

      <div style={{ position: 'absolute', bottom: 10, left: 10, display: 'flex', gap: 12, background: `color-mix(in oklch, ${c.surface} 80%, transparent)`, border: `1px solid ${c.borderSoft}`, borderRadius: 4, padding: '4px 10px', ...fontMono, fontSize: 9.5, color: c.muted }}>
        {hotCount > 0 && <span><Dot color={c.warn} size={6} /> &nbsp;locked</span>}
        <span><Dot color={c.accent} size={6} /> &nbsp;selected</span>
        <span><Dot color={c.muted} size={6} /> &nbsp;file</span>
        <span style={{ color: c.faint }}>size = symbol count</span>
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

// ─── agent / lock row ──────────────────────────────────────
function LockRow({ lock, onSelect, isSel }) {
  const parts = lock.node_id.split('::');
  const symbol = parts.length > 1 ? parts.slice(1).join('::') : '—';
  const fileName = lock.file_path.split('/').pop();

  return (
    <div
      onClick={() => onSelect && onSelect(lock.file_path)}
      style={{
        display: 'grid',
        gridTemplateColumns: '10px 1fr 1fr',
        alignItems: 'center', gap: 6,
        padding: '4px 10px',
        borderBottom: `1px solid ${c.borderSoft}`,
        background: isSel ? c.surface2 : 'transparent',
        cursor: 'pointer',
      }}
    >
      <Dot color={c.ok} size={6} pulse />
      <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <span style={{ ...fontMono, fontSize: 10.5, color: c.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{lock.agent_id}</span>
        <span style={{ ...fontMono, fontSize: 9, color: c.faint, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{fileName}</span>
      </div>
      <span style={{ ...fontMono, fontSize: 9.5, color: c.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'right' }}>{symbol}</span>
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
      <span style={{ ...fontMono, fontSize: 10, color: c.faint }}>{e.t.slice(0, 12)}</span>
      <span style={{ ...fontMono, fontSize: 10, color: col, letterSpacing: 0.3, fontWeight: 500 }}>{e.kind}</span>
      <span style={{ ...fontMono, fontSize: 10, color: c.textDim }}>{e.agent}</span>
      <span style={{ ...fontMono, fontSize: 10, color: c.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        <span style={{ color: c.text }}>{e.target}</span> · {e.msg}
      </span>
    </div>
  );
}

// ─── main ──────────────────────────────────────────────────
function App() {
  const [rawNodes, setRawNodes] = React.useState([]);
  const [edges, setEdges] = React.useState([]);
  const [locks, setLocks] = React.useState([]);
  const [stats, setStats] = React.useState(null);
  const [events, setEvents] = React.useState([]);
  const [sel, setSel] = React.useState(null);
  const [connected, setConnected] = React.useState(true);

  const W = 1100, H = 680;

  const { nodes, clusters } = React.useMemo(
    () => computeLayout(rawNodes, W, H),
    [rawNodes]
  );
  const byId = React.useMemo(() => Object.fromEntries(nodes.map(n => [n.id, n])), [nodes]);

  const hotNodes = React.useMemo(
    () => [...nodes].sort((a, b) => b.agents - a.agents).filter(n => n.agents > 0).slice(0, 6),
    [nodes]
  );

  const fetchAll = React.useCallback(async () => {
    try {
      const [graphRes, locksRes, statsRes, eventsRes] = await Promise.all([
        fetch('/api/graph').then(r => r.json()),
        fetch('/api/locks').then(r => r.json()),
        fetch('/api/stats').then(r => r.json()),
        fetch('/api/events').then(r => r.json()),
      ]);
      setRawNodes(graphRes.nodes || []);
      setEdges(graphRes.edges || []);
      setLocks(locksRes || []);
      setStats(statsRes);
      setEvents(eventsRes || []);
      setConnected(true);
      // Select first node automatically on first load
      setSel(prev => prev ?? (graphRes.nodes?.[0]?.id ?? null));
    } catch {
      setConnected(false);
    }
  }, []);

  React.useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 2000);
    return () => clearInterval(id);
  }, [fetchAll]);

  const selNode = byId[sel];
  const selLocks = locks.filter(l => l.file_path === sel);
  const inEdges = edges.filter(([, b]) => b === sel).length;
  const outEdges = edges.filter(([a]) => a === sel).length;

  return (
    <div style={{ minHeight: '100vh', background: c.bg, color: c.text, ...fontSans, display: 'flex', flexDirection: 'column', fontSize: 12 }}>
      <style>{`
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
        @keyframes blink  { 0%,100%{opacity:1} 50%{opacity:0.4} }
      `}</style>

      {/* top bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '10px 20px', borderBottom: `1px solid ${c.border}`, background: c.surface, height: 44 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
          <svg width="20" height="20" viewBox="0 0 22 22">
            <circle cx="11" cy="11" r="9.5" fill="none" stroke={c.accent} strokeWidth="1.5" />
            <circle cx="11" cy="11" r="3" fill={c.accent} />
            <circle cx="11" cy="3.5" r="1.5" fill={c.accent} />
            <circle cx="18.5" cy="11" r="1.5" fill={c.accent} />
            <circle cx="11" cy="18.5" r="1.5" fill={c.accent} />
            <circle cx="3.5" cy="11" r="1.5" fill={c.accent} />
          </svg>
          <div style={{ ...fontMono, fontSize: 12, letterSpacing: 0.7, color: c.text }}>CBS<span style={{ color: c.muted }}> · coordinator</span></div>
        </div>
        <div style={{ width: 1, height: 16, background: c.border }} />
        {connected
          ? <Pill color={c.ok} strong><Dot color={c.ok} size={5} pulse /> &nbsp;LIVE</Pill>
          : <Pill color={c.warn} strong>DISCONNECTED</Pill>
        }
        <Pill color={c.muted}>poll 2s</Pill>
        <div style={{ flex: 1 }} />
        <Pill color={c.accent} strong>dep graph · file level</Pill>
      </div>

      <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 12 }}>

        {/* stat strip */}
        <div style={{ display: 'flex', gap: 10 }}>
          <Stat label="files indexed"    value={stats?.total_files ?? '—'}            color={c.text}   sub={`${stats?.total_edges ?? 0} import edges`} />
          <Stat label="symbols"          value={stats?.total_structural_nodes ?? '—'} color={c.text}   sub="functions · classes" />
          <Stat label="active locks"     value={stats?.active_locks ?? '—'}           color={stats?.active_locks > 0 ? c.warn : c.text} sub={`${stats?.locked_files ?? 0} file${stats?.locked_files !== 1 ? 's' : ''} locked`} />
          <Stat label="agents"           value={stats?.agents?.length ?? '—'}         color={c.text}   sub={stats?.agents?.join(' · ') || 'none active'} />
          <Stat label="conflicts open"   value="—"                                    color={c.faint}  sub="not yet tracked" />
          <Stat label="ct-tree"          value="—"                                    color={c.faint}  sub="not yet tracked" />
        </div>

        {/* main 3-col */}
        <div style={{ display: 'grid', gridTemplateColumns: '240px minmax(0, 1fr) 280px', gap: 10, height: 720 }}>

          {/* LEFT — active locks */}
          <Panel
            title="Active locks"
            sub={`· ${locks.length}`}
            right={
              locks.length > 0
                ? <Pill color={c.warn} strong>{locks.length} held</Pill>
                : <Pill color={c.faint}>none</Pill>
            }
            padding={0}
          >
            {locks.length === 0
              ? <EmptyState label="no locks held" />
              : (
                <>
                  <div style={{ display: 'grid', gridTemplateColumns: '10px 1fr 1fr', gap: 6, padding: '5px 10px', borderBottom: `1px solid ${c.border}`, ...fontMono, fontSize: 9, color: c.faint, letterSpacing: 0.4, textTransform: 'uppercase', background: c.surface2 }}>
                    <span></span><span>agent · file</span><span style={{ textAlign: 'right' }}>symbol</span>
                  </div>
                  <div style={{ overflow: 'auto', flex: 1 }}>
                    {locks.map((lock, i) => (
                      <LockRow key={i} lock={lock} onSelect={setSel} isSel={lock.file_path === sel} />
                    ))}
                  </div>
                </>
              )
            }
          </Panel>

          {/* CENTER — graph */}
          <Panel
            title="Dependency graph"
            sub={`· ${nodes.length} files · ${edges.length} edges`}
            right={
              <div style={{ display: 'flex', gap: 5, alignItems: 'center' }}>
                {hotNodes.length > 0 && <Pill color={c.warn} strong>{hotNodes.length} locked</Pill>}
                <Pill color={c.muted}>file level</Pill>
              </div>
            }
            padding={0}
          >
            <div style={{ flex: 1, minHeight: 0 }}>
              <Graph nodes={nodes} edges={edges} clusters={clusters} selected={sel} onSelect={setSel} />
            </div>
          </Panel>

          {/* RIGHT — selected file + hot files */}
          <div style={{ display: 'grid', gridTemplateRows: 'auto minmax(0, 1fr) minmax(0, 1fr)', gap: 10, minWidth: 0, minHeight: 0 }}>

            <Panel title="Selected file" right={selNode ? <Pill color={c.accent}>{selNode.mod}</Pill> : null} padding={10}>
              {!selNode
                ? <EmptyState label="click a node to inspect" />
                : (
                  <>
                    <div style={{ ...fontMono, fontSize: 11.5, color: c.text, marginBottom: 8, wordBreak: 'break-all', lineHeight: 1.3 }}>{selNode.label}</div>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 5, marginBottom: 6 }}>
                      {[
                        { l: 'symbols', v: selNode.structural_count, c: c.text },
                        { l: 'agents',  v: selNode.agents,           c: selNode.agents > 0 ? c.warn : c.text },
                        { l: 'in',      v: inEdges,                  c: c.text },
                        { l: 'out',     v: outEdges,                 c: c.text },
                      ].map(b => (
                        <div key={b.l} style={{ padding: '5px 7px', background: c.surface2, border: `1px solid ${c.borderSoft}`, borderRadius: 4 }}>
                          <div style={{ ...fontMono, fontSize: 8.5, color: c.faint, textTransform: 'uppercase', letterSpacing: 0.3 }}>{b.l}</div>
                          <div style={{ ...fontSans, fontSize: 16, fontWeight: 500, color: b.c, lineHeight: 1.1, letterSpacing: -0.3 }}>{b.v}</div>
                        </div>
                      ))}
                    </div>
                    {selLocks.length > 0 && (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 3, maxHeight: 96, overflow: 'auto' }}>
                        {selLocks.map((lock, i) => {
                          const sym = lock.node_id.split('::').slice(1).join('::') || '—';
                          return (
                            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 6px', background: c.surface2, border: `1px solid ${c.borderSoft}`, borderRadius: 3 }}>
                              <Dot color={c.ok} size={5} pulse />
                              <span style={{ ...fontMono, fontSize: 10, color: c.text }}>{lock.agent_id}</span>
                              <span style={{ ...fontMono, fontSize: 9.5, color: c.muted, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sym}</span>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </>
                )
              }
            </Panel>

            <Panel title="Open conflicts" sub="· not yet tracked" padding={10}>
              <EmptyState label="conflict tracking coming soon" />
            </Panel>

            <Panel title="Hot files" sub="· by lock count" padding={0}>
              {hotNodes.length === 0
                ? <EmptyState label="no files locked" />
                : (
                  <div style={{ overflow: 'auto', flex: 1 }}>
                    {hotNodes.map((n, i) => {
                      const isSel = sel === n.id;
                      return (
                        <div key={n.id} onClick={() => setSel(n.id)} style={{ display: 'grid', gridTemplateColumns: '14px 1fr 22px', gap: 6, padding: '5px 10px', borderBottom: `1px solid ${c.borderSoft}`, alignItems: 'center', background: isSel ? c.surface2 : 'transparent', cursor: 'pointer', position: 'relative' }}>
                          <span style={{ ...fontMono, fontSize: 9, color: c.faint }}>{i + 1}</span>
                          <span style={{ ...fontMono, fontSize: 10, color: c.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{n.label.replace('.py', '')}</span>
                          <span style={{ ...fontMono, fontSize: 9.5, color: c.warn, textAlign: 'right' }}>{n.agents}a</span>
                          <div style={{ position: 'absolute', left: 0, bottom: 0, height: 1.5, width: `${(n.agents / hotNodes[0].agents) * 100}%`, background: c.warn, opacity: 0.55 }} />
                        </div>
                      );
                    })}
                  </div>
                )
              }
            </Panel>
          </div>
        </div>

        {/* bottom row: event stream */}
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 320px', gap: 10, height: 220 }}>
          <Panel
            title="Hook event stream"
            sub="· live tail"
            right={
              events.length > 0
                ? <Pill color={c.ok} strong><Dot color={c.ok} size={5} pulse /> &nbsp;tailing</Pill>
                : <Pill color={c.faint}>no events yet</Pill>
            }
            padding={0}
          >
            {events.length === 0
              ? <EmptyState label="event logging not yet wired" />
              : (
                <>
                  <div style={{ display: 'grid', gridTemplateColumns: '80px 88px 64px 1fr', gap: 10, padding: '5px 12px', borderBottom: `1px solid ${c.border}`, ...fontMono, fontSize: 9, color: c.faint, letterSpacing: 0.4, textTransform: 'uppercase', background: c.surface2 }}>
                    <span>time</span><span>kind</span><span>agent</span><span>target · msg</span>
                  </div>
                  <div style={{ flex: 1, overflow: 'auto' }}>
                    {events.map((e, i) => <EventRow key={i} e={e} />)}
                  </div>
                </>
              )
            }
          </Panel>

          <Panel title="Subgraph detail" sub="· selected file imports" padding={10}>
            {!selNode
              ? <EmptyState label="select a file" />
              : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, overflow: 'auto', flex: 1 }}>
                  {edges.filter(([a]) => a === sel).map(([, b], i) => (
                    <div key={i} onClick={() => setSel(b)} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 6px', background: c.surface2, border: `1px solid ${c.borderSoft}`, borderRadius: 3, cursor: 'pointer' }}>
                      <span style={{ ...fontMono, fontSize: 9, color: c.faint }}>→</span>
                      <span style={{ ...fontMono, fontSize: 10, color: byId[b]?.hot ? c.warn : c.textDim, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{b}</span>
                      {byId[b]?.hot && <Dot color={c.warn} size={5} pulse />}
                    </div>
                  ))}
                  {edges.filter(([, b]) => b === sel).map(([a], i) => (
                    <div key={`in-${i}`} onClick={() => setSel(a)} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 6px', background: c.surface2, border: `1px solid ${c.borderSoft}`, borderRadius: 3, cursor: 'pointer', opacity: 0.7 }}>
                      <span style={{ ...fontMono, fontSize: 9, color: c.faint }}>←</span>
                      <span style={{ ...fontMono, fontSize: 10, color: c.faint, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a}</span>
                    </div>
                  ))}
                  {inEdges === 0 && outEdges === 0 && <EmptyState label="no import edges" />}
                </div>
              )
            }
          </Panel>
        </div>
      </div>

      {/* footer */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '6px 20px', color: c.faint, ...fontMono, fontSize: 10, borderTop: `1px solid ${c.border}`, background: c.surface, marginTop: 12 }}>
        <span>cbs-coord/0.1.0</span>
        <span>·</span>
        <span>file-level dep graph</span>
        <span style={{ flex: 1 }} />
        <span style={{ color: connected ? c.ok : c.warn, animation: connected ? 'blink 2s infinite' : 'none' }}>
          {connected ? '● live' : '○ disconnected'}
        </span>
      </div>
    </div>
  );
}

window.App = App;
