import { useEffect, useState, useRef, useCallback } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import { Search, SlidersHorizontal, ChevronDown, ChevronRight, Eye, EyeOff, X, FileX } from 'lucide-react';
import axios from 'axios';

const GRAPH_LAYOUT_STORAGE_KEY = 'spatial_graph_layout_v2';
const GRAPH_SETTINGS_KEY       = 'spatial_graph_settings_v4';

const LINK_DISTANCE_BASE = 25;  // px — multiplied by settings.nodeDistance
const LINK_STRENGTH      = 0.1;  // soft springs — connected nodes follow hub gently, never dominate

// ── Physics design ───────────────────────────────────────────────────────────
//
//  centerForce (default 0.83)
//    Proportional gravity F = −k·x, −k·y. Pulls ALL nodes into one disc.
//    gravityRef is read every tick → slider instant, no reheat needed.
//    NEVER reduced during or after drag. Any reduction while alpha > ~0.05
//    lets charge repulsion dominate and blasts nodes outward (explosion).
//
//  d3VelocityDecay = 0.55 (raised from 0.4)
//    Higher damping makes ALL physics motion more gentle/smooth, including
//    snap-back after drag. No risk of explosion — only affects motion speed.
//
//  repelForce (default 61)
//    Global charge repulsion for even spacing within the disc.
//
//  LINK_STRENGTH = 0.1
//    Soft springs so connected nodes follow hub at reasonable drag distances.
//    Low enough that spring force never overpowers gravity (no explosion).
// ─────────────────────────────────────────────────────────────────────────────

const DEFAULT_SETTINGS = {
  textOpacity:   1.00,
  nodeSize:      0.50,
  linkThickness: 0.3,
  centerForce:   0.57,
  repelForce:    188,
  nodeDistance:  2.0,
  showLinks:     true,
};

function createGravityForce(gravityRef) {
  let nodes = [];
  const force = (alpha) => {
    const k = gravityRef.current;
    if (!k) return;
    for (const n of nodes) {
      n.vx -= n.x * k * alpha;
      n.vy -= n.y * k * alpha;
    }
  };
  force.initialize = (n) => { nodes = n; };
  return force;
}

function SliderRow({ label, value, min, max, step, onChange, fmt }) {
  return (
    <div className="space-y-1">
      <div className="flex justify-between items-center">
        <span className="text-[11px] text-gray-400">{label}</span>
        <span className="text-[10px] text-gray-300 font-mono tabular-nums w-9 text-right">
          {fmt ? fmt(value) : value}
        </span>
      </div>
      <input
        type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full graph-slider"
      />
    </div>
  );
}

function BreathingSphere({ onClick }) {
  const [hovered, setHovered] = useState(false);
  return (
    <div className="absolute inset-0 flex items-center justify-center z-10 pointer-events-none">
      <style>{`
        @keyframes ssb-breathe {
          0%, 100% { transform: scale(1); }
          50% { transform: scale(1.07); }
        }
        @keyframes ssb-ring {
          0%   { transform: scale(0.88); opacity: 0.55; }
          100% { transform: scale(1.85); opacity: 0; }
        }
        .ssb-breathe { animation: ssb-breathe 4.8s ease-in-out infinite; }
        .ssb-r1 { animation: ssb-ring 3.8s ease-out infinite; }
        .ssb-r2 { animation: ssb-ring 3.8s ease-out infinite 1.27s; }
        .ssb-r3 { animation: ssb-ring 3.8s ease-out infinite 2.54s; }
      `}</style>

      <button
        onClick={onClick}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        className="pointer-events-auto flex flex-col items-center gap-4 outline-none select-none group"
        style={{ background: 'none', border: 'none', marginBottom: 60 }}
      >
        {/* Sphere + rings container */}
        <div className="relative flex items-center justify-center" style={{ width: 148, height: 148 }}>

          {/* Expanding pulse rings */}
          {['ssb-r1','ssb-r2','ssb-r3'].map(cls => (
            <div key={cls} className={`absolute rounded-full ${cls}`}
                 style={{ inset: 14, border: '1px solid rgba(139,92,246,0.45)' }} />
          ))}

          {/* Ambient glow bloom */}
          <div className="absolute rounded-full transition-opacity duration-700"
               style={{
                 inset: 0,
                 background: 'radial-gradient(circle, rgba(139,92,246,0.4) 0%, transparent 70%)',
                 filter: 'blur(18px)',
                 opacity: hovered ? 1 : 0.45,
               }} />

          {/* Hover scale wrapper (separate layer so it doesn't fight the breathe animation) */}
          <div style={{
            transform: hovered ? 'scale(1.12)' : 'scale(1)',
            transition: 'transform 0.55s cubic-bezier(0.34,1.56,0.64,1)',
          }}>
            {/* Core sphere — breathing */}
            <div className="ssb-breathe rounded-full" style={{
              width: 90, height: 90,
              background: 'radial-gradient(circle at 34% 30%, #ffffff 0%, #ddd6fe 12%, #8b5cf6 38%, #4c1d95 68%, #1a0b38 100%)',
              boxShadow: hovered
                ? '0 0 44px rgba(139,92,246,0.75), 0 0 88px rgba(139,92,246,0.3), inset 0 1.5px 0 rgba(255,255,255,0.4)'
                : '0 0 26px rgba(139,92,246,0.45), 0 0 52px rgba(139,92,246,0.15), inset 0 1.5px 0 rgba(255,255,255,0.25)',
              transition: 'box-shadow 0.55s ease',
            }} />
          </div>
        </div>

        {/* Label */}
        <div className="text-center space-y-1">
          <p style={{ fontFamily: '"Space Grotesk", Inter, system-ui, sans-serif' }}
             className="text-[13px] font-semibold tracking-wide text-gray-400 group-hover:text-white transition-colors duration-300">
            Upload your knowledge
          </p>
          <p className="text-[11px] text-gray-600 group-hover:text-gray-500 transition-colors duration-300">
            Click to get started
          </p>
        </div>
      </button>
    </div>
  );
}


export function KnowledgeGraph({
  onPreview,
  onOpenBrainDumpNote,
  showNodeLabels = true,
  onShowNodeLabelsChange,
  agentName = '',
  onAgentNameChange,
  refreshKey = 0,
  onDataChanged,
  onOpenFiles,
}) {
  const [graphData,      setGraphData]      = useState({ nodes: [], links: [] });
  const [graphLoaded,    setGraphLoaded]    = useState(false);
  const [dimensions,     setDimensions]     = useState({ width: window.innerWidth, height: window.innerHeight });
  const [searchTerm,     setSearchTerm]     = useState('');
  const [matchedNodeIds, setMatchedNodeIds] = useState([]);
  const [matchIndex,     setMatchIndex]     = useState(0);
  const [focusedNodeId,  setFocusedNodeId]  = useState(null);
  const [hoverNode,      setHoverNode]      = useState(null);
  const [contextMenu,    setContextMenu]    = useState(null);
  const [deleteTarget,   setDeleteTarget]   = useState(null);
  const [isDeleting,     setIsDeleting]     = useState(false);
  const [showSettings,   setShowSettings]   = useState(false);
  const [displayOpen,    setDisplayOpen]    = useState(true);
  const [forcesOpen,     setForcesOpen]     = useState(true);

  const [settings, setSettings] = useState(() => {
    try {
      const s = localStorage.getItem(GRAPH_SETTINGS_KEY);
      return s ? { ...DEFAULT_SETTINGS, ...JSON.parse(s) } : DEFAULT_SETTINGS;
    } catch { return DEFAULT_SETTINGS; }
  });

  const fgRef              = useRef();
  const containerRef       = useRef();
  const cameraStateRef     = useRef({ x: 0, y: 0, k: 1 });
  const cameraSaveTimerRef = useRef(null);
  const gravityRef         = useRef(settings.centerForce);
  const settingsRef        = useRef(settings);
  const cameraRestoredRef  = useRef(false);
  // Read node deep-link synchronously — supports both /node/filename (path) and ?node=filename (query)
  const deepLinkNodeRef    = useRef(
    (() => {
      const path = window.location.pathname;
      if (path.startsWith('/node/')) return decodeURIComponent(path.slice(6));
      const p = new URLSearchParams(window.location.search);
      const v = p.get('node');
      return v ? decodeURIComponent(v) : null;
    })()
  );

  // ── Settings sync ─────────────────────────────────────────────────────────
  useEffect(() => {
    settingsRef.current = settings;
    gravityRef.current  = settings.centerForce;
    try { localStorage.setItem(GRAPH_SETTINGS_KEY, JSON.stringify(settings)); } catch { /* Ignore storage persistence failures. */ }
  }, [settings]);

  const updateSetting = useCallback((k, v) => setSettings(p => ({ ...p, [k]: v })), []);
  const resetSettings  = useCallback(() => setSettings(DEFAULT_SETTINGS), []);

  // ── Layout persistence ────────────────────────────────────────────────────
  const getSavedLayout = useCallback(() => {
    try { const r = localStorage.getItem(GRAPH_LAYOUT_STORAGE_KEY); return r ? JSON.parse(r) : null; }
    catch { return null; }
  }, []);

  const saveGraphState = useCallback(() => {
    if (!fgRef.current || !graphData?.nodes?.length) return;
    const pos = {};
    graphData.nodes.forEach(n => {
      if (Number.isFinite(n?.x) && Number.isFinite(n?.y)) pos[String(n.id)] = { x: n.x, y: n.y };
    });
    try {
      const c = fgRef.current.centerAt(), z = fgRef.current.zoom();
      if (c && Number.isFinite(c.x) && Number.isFinite(c.y) && Number.isFinite(z))
        cameraStateRef.current = { x: c.x, y: c.y, k: z };
    } catch { /* Ignore camera read failures from graph instance. */ }
    try { localStorage.setItem(GRAPH_LAYOUT_STORAGE_KEY, JSON.stringify({ nodePositions: pos, camera: cameraStateRef.current })); }
    catch { /* Ignore storage persistence failures. */ }
  }, [graphData]);

  // ── Data ──────────────────────────────────────────────────────────────────
  const fetchGraph = useCallback(async () => {
    try {
      const res   = await axios.get('/api/graph');
      const inc   = res.data || { nodes: [], links: [] };
      const saved = getSavedLayout();
      const sp    = saved?.nodePositions || {};
      const nodes = (inc.nodes || []).map(n => {
        const p = sp[String(n.id)];
        return (p && Number.isFinite(p.x) && Number.isFinite(p.y))
          ? { ...n, x: p.x, y: p.y, vx: 0, vy: 0 }
          : n;
      });
      setGraphData({ ...inc, nodes });

      // Deep-link: check once right after data arrives
      const requested = deepLinkNodeRef.current;
      if (requested && !deepLinkHandledRef.current) {
        deepLinkHandledRef.current = true;
        deepLinkNodeRef.current = null;
        const target = nodes.find(n =>
          (n.name || '').toLowerCase() === requested.toLowerCase() ||
          (n.source_file || '').toLowerCase() === requested.toLowerCase()
        );
        if (!target) {
          setDeletedNodeName(requested);
        } else {
          pendingZoomNodeRef.current = target;
        }
      }
    } catch (e) { console.error('fetch graph failed', e); }
    finally { setGraphLoaded(true); }
  }, [getSavedLayout]);

  useEffect(() => {
    fetchGraph();
    const onResize = () => setDimensions({ width: window.innerWidth, height: window.innerHeight });
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [fetchGraph]);

  useEffect(() => { if (refreshKey > 0) fetchGraph(); }, [refreshKey, fetchGraph]);

  // ── Force setup ───────────────────────────────────────────────────────────
  useEffect(() => {
    if (!fgRef.current) return;
    fgRef.current.d3Force('center', null);
    gravityRef.current = settingsRef.current.centerForce;
    fgRef.current.d3Force('gravity', createGravityForce(gravityRef));
    const charge = fgRef.current.d3Force('charge');
    if (charge) charge.strength(-settingsRef.current.repelForce).distanceMin(5);
    const link = fgRef.current.d3Force('link');
    if (link) link.strength(LINK_STRENGTH).distance(LINK_DISTANCE_BASE * settingsRef.current.nodeDistance);
    // Only run physics if some nodes lack saved positions (they need placement).
    // When all nodes have x/y from localStorage, skip reheat so they stay put.
    const sp = getSavedLayout()?.nodePositions || {};
    const hasUnsavedNodes = graphData.nodes?.some(
      n => !sp[String(n.id)] || !Number.isFinite(sp[String(n.id)]?.x)
    );
    if (hasUnsavedNodes) fgRef.current.d3ReheatSimulation();
  }, [graphData, getSavedLayout]);

  // ── Real-time slider updates ───────────────────────────────────────────────
  useEffect(() => {
    if (!fgRef.current) return;
    gravityRef.current = settings.centerForce;
    const charge = fgRef.current.d3Force('charge');
    if (charge) charge.strength(-settings.repelForce).distanceMin(5);
    const link = fgRef.current.d3Force('link');
    if (link) link.distance(LINK_DISTANCE_BASE * settings.nodeDistance);
    fgRef.current.d3ReheatSimulation();
  }, [settings.centerForce, settings.repelForce, settings.nodeDistance]);

  // ── Camera restore ────────────────────────────────────────────────────────
  useEffect(() => {
    // Only restore camera once — on the initial data load, not on every refresh.
    if (cameraRestoredRef.current || !fgRef.current) return;
    const saved = getSavedLayout();
    const cam   = saved?.camera;
    if (!cam || !Number.isFinite(cam.x) || !Number.isFinite(cam.y) || !Number.isFinite(cam.k)) return;
    cameraStateRef.current = cam;
    // Small timeout gives the canvas a frame to mount before we command the camera.
    const t = setTimeout(() => {
      if (!fgRef.current) return;
      fgRef.current.centerAt(cam.x, cam.y, 0);
      fgRef.current.zoom(cam.k, 0);
      cameraRestoredRef.current = true;
    }, 80);
    return () => clearTimeout(t);
  }, [graphData, getSavedLayout]);

  // ── Node canvas rendering ─────────────────────────────────────────────────
  const nodeCanvasObject = useCallback((node, ctx, globalScale) => {
    if (node.x === undefined || node.y === undefined) return;

    const isHovered = hoverNode?.id === node.id;
    const isDoc     = node.group === 'document';
    const isFocused = focusedNodeId === node.id;

    node.__hoverEase = node.__hoverEase ?? 0;
    node.__focusEase = node.__focusEase ?? 0;
    node.__hoverEase += ((isHovered ? 1 : 0) - node.__hoverEase) * 0.16;
    node.__focusEase += ((isFocused ? 1 : 0) - node.__focusEase) * 0.14;

    const baseSize = (node.val || 5) * settings.nodeSize;
    const size = baseSize * (1 + node.__hoverEase * 0.16 + node.__focusEase * 0.1);

    ctx.save();
    ctx.shadowBlur  = 5 + node.__hoverEase * 10 + node.__focusEase * 12;
    ctx.shadowColor = isFocused
      ? 'rgba(56,189,248,0.5)'
      : isDoc ? 'rgba(139,92,246,0.3)' : 'rgba(100,116,139,0.2)';

    const g = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, size);
    if (isDoc) {
      g.addColorStop(0, '#f5f3ff'); g.addColorStop(0.4, '#8b5cf6'); g.addColorStop(1, '#4c1d95');
    } else {
      const palette = ['#64748b','#818cf8','#a3e635','#fbbf24','#fda4af','#22d3ee'];
      const color = palette[(node.color_seed || 0) % palette.length] || '#94a3b8';
      g.addColorStop(0, '#ffffff'); g.addColorStop(0.3, color); g.addColorStop(1, '#1e293b');
    }
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(node.x, node.y, size, 0, 2 * Math.PI, false);
    ctx.fill();

    if (isDoc) {
      ctx.strokeStyle = 'rgba(255,255,255,0.6)';
      ctx.lineWidth = 1.2 / globalScale;
      ctx.stroke();
    }
    if (isFocused) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, size + 3.5 / globalScale, 0, 2 * Math.PI, false);
      ctx.strokeStyle = 'rgba(56,189,248,0.85)';
      ctx.lineWidth = 2.2 / globalScale;
      ctx.stroke();
    }

    if (showNodeLabels || isHovered || isFocused) {
      const label = String(node.name || '');
      const isTopic = node.group === 'topic';
      const docPx = 2.7 + node.__hoverEase * 0.28 + node.__focusEase * 0.18;
      const fontSize = Math.max(2.35, isTopic ? docPx - 0.35 : docPx);
      ctx.font = `${isHovered ? '600' : '500'} ${fontSize}px "Space Grotesk","Inter",system-ui,sans-serif`;
      ctx.textAlign = 'center';
      const alpha = (isHovered || isFocused) ? 0.82 : settings.textOpacity * 0.72;
      if (alpha > 0.01) {
        ctx.fillStyle = `rgba(160,170,186,${alpha})`;
        ctx.fillText(label, node.x, node.y + size + 2.8);
      }
    }
    ctx.restore();
  }, [hoverNode, showNodeLabels, focusedNodeId, settings.nodeSize, settings.textOpacity]);

  // ── Search ────────────────────────────────────────────────────────────────
  const moveToNode = useCallback((node) => {
    if (!node || !fgRef.current || node.x === undefined) return;
    setFocusedNodeId(node.id);
    fgRef.current.centerAt(node.x, node.y, 1200);
    fgRef.current.zoom(8.2, 1200);
  }, []);

  // ── URL deep-link: ?node=filename ─────────────────────────────────────────
  const [deletedNodeName, setDeletedNodeName] = useState(null);
  const deepLinkHandledRef = useRef(false);
  const pendingZoomNodeRef = useRef(null);

  // After graph renders, fire the pending zoom if one was set by fetchGraph
  useEffect(() => {
    if (!graphLoaded || !pendingZoomNodeRef.current) return;
    const node = pendingZoomNodeRef.current;
    pendingZoomNodeRef.current = null;
    const t = setTimeout(() => moveToNode(node), 400);
    return () => clearTimeout(t);
  }, [graphLoaded, moveToNode]);

  const handleNodeSearch = useCallback(() => {
    if (!searchTerm.trim() || !graphData.nodes?.length) return;
    const needle = searchTerm.trim().toLowerCase();
    const matches = graphData.nodes
      .filter(n => (n?.name || '').toLowerCase().includes(needle))
      .sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    setMatchedNodeIds(matches.map(n => n.id));
    setMatchIndex(0);
    if (matches.length) moveToNode(matches[0]);
  }, [searchTerm, graphData, moveToNode]);

  const handleNextMatch = useCallback(() => {
    if (!matchedNodeIds.length) return;
    const next = (matchIndex + 1) % matchedNodeIds.length;
    setMatchIndex(next);
    moveToNode(graphData.nodes.find(n => n.id === matchedNodeIds[next]));
  }, [matchedNodeIds, matchIndex, graphData, moveToNode]);

  const handleClearFocus = useCallback(() => {
    setFocusedNodeId(null); setMatchedNodeIds([]); setMatchIndex(0);
  }, []);

  // ── Right-click / delete ──────────────────────────────────────────────────
  const handleNodeRightClick = useCallback((node, event) => {
    if (!node || !event || !containerRef.current) return;
    event.preventDefault();
    const rect = containerRef.current.getBoundingClientRect();
    const x = Math.min(rect.width - 196, Math.max(8, event.clientX - rect.left));
    const y = Math.min(rect.height - 100, Math.max(8, event.clientY - rect.top));
    setContextMenu({ x, y, nodeGroup: node.group, fileName: String(node.source_file || node.name || ''), label: String(node.name || '') });
  }, []);

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget) return;
    setIsDeleting(true);
    try {
      await axios.delete(`/api/files/${encodeURIComponent(deleteTarget)}`);
      setDeleteTarget(null); setFocusedNodeId(null);
      await fetchGraph();
      if (onDataChanged) onDataChanged();
    } catch (e) { console.error('delete failed', e); }
    finally { setIsDeleting(false); }
  }, [deleteTarget, fetchGraph, onDataChanged]);

  useEffect(() => {
    const close = () => setContextMenu(null);
    window.addEventListener('click', close);
    window.addEventListener('blur', close);
    return () => {
      window.removeEventListener('click', close);
      window.removeEventListener('blur', close);
      if (cameraSaveTimerRef.current) clearTimeout(cameraSaveTimerRef.current);
    };
  }, []);

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div ref={containerRef} className="relative w-full h-full bg-transparent overflow-hidden">

      {/* Search */}
      <div className="absolute top-6 left-1/2 -translate-x-1/2 z-20 w-[360px] pointer-events-auto">
        <div className="flex items-center gap-2 px-3 py-2 rounded-xl bg-black/45 border border-white/10 backdrop-blur-md">
          <Search size={14} className="text-gray-400 shrink-0" />
          <input type="text" value={searchTerm}
            onChange={e => setSearchTerm(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleNodeSearch()}
            placeholder="Find node by name…"
            className="w-full bg-transparent text-sm text-gray-200 placeholder:text-gray-500 focus:outline-none"
          />
          <button onClick={handleNodeSearch} className="text-[11px] px-2 py-1 rounded-md bg-white/10 hover:bg-white/20 text-gray-200 transition-colors shrink-0">Go</button>
          <button onClick={handleNextMatch} disabled={matchedNodeIds.length <= 1} className="text-[11px] px-2 py-1 rounded-md bg-white/10 hover:bg-white/20 disabled:opacity-40 text-gray-200 transition-colors shrink-0">Next</button>
          <button onClick={handleClearFocus} disabled={!focusedNodeId && !matchedNodeIds.length} className="text-[11px] px-2 py-1 rounded-md bg-white/10 hover:bg-white/20 disabled:opacity-40 text-gray-200 transition-colors shrink-0">Stop</button>
          {matchedNodeIds.length > 0 && <span className="text-[10px] text-gray-400 min-w-[34px] text-right shrink-0">{matchIndex + 1}/{matchedNodeIds.length}</span>}
        </div>
      </div>

      {/* ── Settings panel (top-right) ─────────────────────────────────────── */}
      <div className="absolute top-6 right-6 z-20 pointer-events-auto flex flex-col items-end gap-2">
        <button onClick={() => setShowSettings(v => !v)}
          className={`flex items-center gap-1.5 px-3 py-2 rounded-xl border backdrop-blur-md transition-all duration-200 text-[12px] font-medium
            ${showSettings ? 'bg-white/12 border-white/20 text-gray-200' : 'bg-black/45 border-white/10 text-gray-400 hover:text-gray-200 hover:bg-black/60'}`}>
          <SlidersHorizontal size={13} /><span>Settings</span>
        </button>

        {showSettings && (
          <div className="w-[256px] rounded-2xl border border-white/10 bg-[#13131a]/95 backdrop-blur-xl shadow-2xl overflow-hidden animate-spring-pop max-h-[80vh] overflow-y-auto">

            {/* ── Identity ───────────────────────────────────────────────── */}
            <div className="px-4 pt-4 pb-3 border-b border-white/5 space-y-3">
              <div>
                <span className="text-[10px] font-semibold uppercase tracking-widest text-gray-500 block mb-1.5">Agent name</span>
                <input
                  type="text"
                  value={agentName}
                  onChange={e => onAgentNameChange?.(e.target.value)}
                  placeholder="Name your intelligence…"
                  className="w-full bg-white/5 border border-white/8 rounded-lg px-2.5 py-1.5 text-[12px] text-gray-200 placeholder:text-gray-600 focus:outline-none focus:border-white/20 transition-colors"
                />
              </div>
              <div
                className="flex items-center justify-between cursor-pointer py-0.5 group"
                onClick={() => onShowNodeLabelsChange?.(!showNodeLabels)}
              >
                <div className="flex items-center gap-2">
                  <div className={`p-1.5 rounded-lg transition-colors ${showNodeLabels ? 'bg-[#0a84ff]/20 text-[#0a84ff]' : 'bg-white/5 text-gray-500'}`}>
                    {showNodeLabels ? <Eye size={12} /> : <EyeOff size={12} />}
                  </div>
                  <span className="text-[12px] text-gray-300 group-hover:text-white transition-colors">Show node labels</span>
                </div>
                <div className={`w-8 h-4 rounded-full relative transition-colors duration-200 shrink-0 ${showNodeLabels ? 'bg-[#0a84ff]' : 'bg-white/15'}`}>
                  <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all duration-200 ${showNodeLabels ? 'left-[18px]' : 'left-0.5'}`} />
                </div>
              </div>
            </div>

            {/* ── Display ────────────────────────────────────────────────── */}
            <button onClick={() => setDisplayOpen(v => !v)}
              className="w-full flex items-center justify-between px-4 py-2.5 text-[11px] font-semibold uppercase tracking-widest text-gray-400 hover:text-gray-300 transition-colors border-b border-white/5">
              <span>Display</span>{displayOpen ? <ChevronDown size={12}/> : <ChevronRight size={12}/>}
            </button>
            {displayOpen && (
              <div className="px-4 py-3 space-y-4 border-b border-white/5">
                <SliderRow label="Label opacity" value={settings.textOpacity}  min={0}   max={1}   step={0.01} onChange={v => updateSetting('textOpacity', v)}  fmt={v => v.toFixed(2)} />
                <SliderRow label="Node size"      value={settings.nodeSize}      min={0.2} max={2.5} step={0.05} onChange={v => updateSetting('nodeSize', v)}      fmt={v => v.toFixed(2)} />
                <SliderRow label="Link thickness" value={settings.linkThickness} min={0.1} max={3.0} step={0.1}  onChange={v => updateSetting('linkThickness', v)} fmt={v => v.toFixed(1)} />
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-gray-400">Show links</span>
                  <button
                    onClick={() => updateSetting('showLinks', !settings.showLinks)}
                    style={{
                      width: 36, height: 20, borderRadius: 10, border: 'none', cursor: 'pointer',
                      background: settings.showLinks ? 'var(--color-accent-main)' : 'rgba(255,255,255,0.12)',
                      position: 'relative', transition: 'background 0.2s',
                    }}
                  >
                    <span style={{
                      position: 'absolute', top: 3, left: settings.showLinks ? 18 : 3,
                      width: 14, height: 14, borderRadius: '50%', background: '#fff',
                      transition: 'left 0.2s',
                    }} />
                  </button>
                </div>
              </div>
            )}

            {/* ── Forces ─────────────────────────────────────────────────── */}
            <button onClick={() => setForcesOpen(v => !v)}
              className="w-full flex items-center justify-between px-4 py-2.5 text-[11px] font-semibold uppercase tracking-widest text-gray-400 hover:text-gray-300 transition-colors border-b border-white/5">
              <span>Forces</span>{forcesOpen ? <ChevronDown size={12}/> : <ChevronRight size={12}/>}
            </button>
            {forcesOpen && (
              <div className="px-4 py-3 space-y-4">
                <SliderRow label="Center force"   value={settings.centerForce}  min={0.01} max={1.0} step={0.01} onChange={v => updateSetting('centerForce', v)}  fmt={v => v.toFixed(2)} />
                <SliderRow label="Repel force"    value={settings.repelForce}   min={1}    max={200} step={1}    onChange={v => updateSetting('repelForce', v)}   fmt={v => String(v)} />
                <SliderRow label="Node distance"  value={settings.nodeDistance} min={0.5}  max={8.0} step={0.1}  onChange={v => updateSetting('nodeDistance', v)} fmt={v => v.toFixed(1)} />
              </div>
            )}

            {/* ── Reset ──────────────────────────────────────────────────── */}
            <div className="px-4 pb-3 pt-2">
              <button onClick={resetSettings} className="w-full py-1.5 rounded-lg text-[11px] text-gray-500 hover:text-gray-300 hover:bg-white/8 transition-all border border-white/5">
                Reset to defaults
              </button>
            </div>
          </div>
        )}
      </div>

      <ForceGraph2D
        ref={fgRef}
        width={dimensions.width}
        height={dimensions.height}
        graphData={graphData}
        nodeLabel=""
        onNodeClick={node => {
          if (node?.is_brain_dump_note && node?.note_id && onOpenBrainDumpNote) {
            onOpenBrainDumpNote(node.note_id);
            return;
          }
          onPreview(node.id);
        }}
        onNodeRightClick={handleNodeRightClick}
        onBackgroundRightClick={e => { e?.preventDefault?.(); setContextMenu(null); }}
        onBackgroundClick={() => setContextMenu(null)}
        onNodeDragEnd={node => {
          // Unpin — library does NOT clear fx/fy automatically.
          // Gravity stays constant always — any reduction while alpha > ~0.05
          // lets charge repulsion dominate → explosion. Motion smoothness comes
          // from d3VelocityDecay=0.55 (higher damping) instead.
          node.fx = null;
          node.fy = null;
          saveGraphState();
        }}
        onEngineStop={() => {
          saveGraphState();
        }}
        onZoom={() => {
          if (cameraSaveTimerRef.current) clearTimeout(cameraSaveTimerRef.current);
          cameraSaveTimerRef.current = setTimeout(saveGraphState, 220);
        }}
        onNodeHover={node => setHoverNode(node || null)}
        linkColor={link => {
          if (!settings.showLinks) return 'rgba(0,0,0,0)';
          const w = link.weight || 0.5;
          if (link.type === 'entity') return `rgba(66,120,196,${0.2 + w * 0.4})`;
          if (link.type === 'semantic') return `rgba(167,139,250,${0.08 + w * 0.25})`;
          return `rgba(255,255,255,${0.04 + w * 0.15})`;
        }}
        linkWidth={link => settings.showLinks ? (link.weight || 0.5) * 1.0 * settings.linkThickness : 0}
        backgroundColor="rgba(0,0,0,0)"
        nodeCanvasObject={nodeCanvasObject}
        d3VelocityDecay={0.55}
        warmupTicks={200}
        enablePointerInteraction={true}
      />

      {/* Empty-state breathing sphere — rendered after canvas so it sits on top */}
      {graphLoaded && graphData.nodes.length === 0 && (
        <BreathingSphere onClick={onOpenFiles} />
      )}

      {/* Context menu */}
      {contextMenu && (
        <div className="absolute z-40 rounded-xl border border-white/15 bg-[#15171d]/95 backdrop-blur-md shadow-2xl px-2 py-2 animate-spring-pop"
          style={{ left: contextMenu.x, top: contextMenu.y, width: 188 }}
          onClick={e => e.stopPropagation()}>
          <p className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-gray-500">Node Actions</p>
          {contextMenu.nodeGroup === 'document'
            ? <button onClick={() => { setDeleteTarget(contextMenu.fileName); setContextMenu(null); }}
                className="w-full text-left px-2 py-2 rounded-lg text-[12px] text-red-300 hover:text-red-200 hover:bg-red-500/15 transition-all">
                Delete this file node
              </button>
            : <p className="px-2 py-2 text-[11px] text-gray-400">Only file nodes can be deleted.</p>
          }
          <p className="px-2 pt-1 text-[10px] text-gray-500 truncate">{contextMenu.label}</p>
        </div>
      )}

      {/* Delete modal */}
      {deleteTarget && (
        <div className="absolute inset-0 bg-black/55 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="w-full max-w-md rounded-2xl border border-white/10 bg-[#141419] shadow-2xl p-5 animate-spring-pop">
            <p className="text-sm text-white font-semibold">Delete file permanently?</p>
            <p className="mt-2 text-xs text-gray-400">File:</p>
            <p className="text-xs text-gray-300 break-all">{deleteTarget}</p>
            <p className="mt-2 text-[11px] text-gray-500">This removes it from the knowledge base, map, and local storage.</p>
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={() => setDeleteTarget(null)} disabled={isDeleting}
                className="px-3 py-1.5 text-[11px] rounded-lg bg-white/10 hover:bg-white/20 text-gray-200 transition-all disabled:opacity-50">Cancel</button>
              <button onClick={confirmDelete} disabled={isDeleting}
                className="px-3 py-1.5 text-[11px] rounded-lg bg-red-500/80 hover:bg-red-500 text-white transition-all disabled:opacity-50">
                {isDeleting ? 'Deleting…' : 'Confirm Delete'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Deleted node deep-link error */}
      {deletedNodeName && (
        <div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/60 backdrop-blur-sm pointer-events-auto">
          <div className="relative flex flex-col items-center gap-5 px-10 py-9 rounded-2xl border border-white/10 shadow-2xl"
            style={{ background: '#161616', maxWidth: 380, width: '90vw' }}>
            <button
              onClick={() => {
                setDeletedNodeName(null);
                window.history.replaceState(null, '', '/');
              }}
              className="absolute top-3.5 right-3.5 p-1.5 rounded-lg hover:bg-white/10 text-gray-500 hover:text-white transition-all"
              aria-label="Close"
            >
              <X size={16} />
            </button>
            <div className="flex items-center justify-center w-12 h-12 rounded-xl bg-red-500/10 text-red-400">
              <FileX size={24} />
            </div>
            <div className="flex flex-col items-center gap-1.5 text-center">
              <p className="text-white font-semibold text-base">File no longer exists</p>
              <p className="text-gray-400 text-sm leading-snug">
                <span className="text-gray-200 font-medium break-all">{deletedNodeName}</span>
                {' '}has been removed from your knowledge base.
              </p>
            </div>
            <button
              onClick={() => {
                setDeletedNodeName(null);
                window.history.replaceState(null, '', '/');
              }}
              className="mt-1 px-6 py-2 rounded-xl bg-white/8 hover:bg-white/15 text-gray-200 text-sm font-medium transition-all border border-white/10"
            >
              Back to Knowledge Base
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
