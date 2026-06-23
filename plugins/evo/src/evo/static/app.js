/* evo dashboard */

// ─── State ───────────────────────────────────────────────
const state = {
  stats: {},
  graph: { nodes: {} },
  workspace: {},
  frontier: null,        // backend-authoritative {strategy, picks, all_ids}
  frontierMeta: null,
  selectedNode: null,
  // Detail-view presentation: 'side' (right-rail peek) or 'center'
  // (centered modal over a backdrop), Notion-style. Persisted across loads.
  detailMode: (() => { try { return localStorage.getItem('evo.detailMode') || 'side'; } catch (_) { return 'side'; } })(),
  // Diff layout: 'split' (side-by-side, GitHub-style) or 'unified'. Persisted.
  diffView: (() => { try { return localStorage.getItem('evo.diffView') || 'split'; } catch (_) { return 'split'; } })(),
  sidebarTab: 'summary',
  expandedTasks: new Set(),
  refreshTimer: null,
  settingsSection: 'execution',
  // Timeline filters / view state
  statusFilters: new Set(['committed', 'evaluated', 'discarded', 'failed', 'active', 'pending', 'pruned']),
  viewMode: 'all',         // 'all' | 'lineage' | 'frontier'
  scopeRoot: 'root',       // node id treated as the local root of the timeline
  collapsed: new Set(),    // node ids whose descendants are collapsed
  // Drawer back/forward navigation. Every user-initiated openDrawer
  // (timeline click, rail click, child row, parent link, etc.) pushes
  // onto this stack and advances the index. drawerBack/drawerForward
  // move the index without pushing. closeSidebar resets both.
  drawerHistory: [],
  drawerHistoryIndex: -1,
  timelineScroll: { top: 0, left: 0 },
  timelineZoom: 1,         // ctrl/meta + wheel zoom on the timeline canvas
  _zoomWheelBound: false,  // flips true once we've attached the wheel handler
  _dragPanBound: false,    // flips true once we've attached drag-pan listeners
  _dblResetBound: false,   // flips true once dblclick-reset is bound
  _scatterResizeBound: false,  // scatter overlay drag-resize handler
  _barHoverTipsBound: false,   // hover-tooltips on compact off-spine bars
  _scatterTipsBound: false,    // hover-tooltips on scatter dots
  // Logs tab state (per-drawer; resets when the drawer or file changes).
  logsSelected: null,    // currently-displayed log file name
  logsOffset: 0,         // byte offset for incremental append polling
  logsAutoRefresh: false, // whether the 2s poll timer is active
};

// ─── Helpers ─────────────────────────────────────────────
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Dot color = result class only. Three values: green (committed/success),
// red (failed/error), grey (everything else — pending, active, evaluated,
// discarded, pruned, root). "Currently running" is signaled by the card
// OUTLINE (purple border + pulse), not the dot, so the dot stays a clean
// pass/fail/neutral indicator.
const STATUS_COLORS = {
  root:      cssVar('--text-5'),
  pending:   cssVar('--text-5'),
  active:    cssVar('--text-5'),
  evaluated: cssVar('--text-5'),
  committed: cssVar('--green'),
  discarded: cssVar('--text-5'),
  failed:    cssVar('--red'),
  pruned:    cssVar('--text-5'),
};

const REMOTE_PROVIDER_FIELDS = {
  modal: [
    {key: 'app_name', label: 'App name', type: 'text'},
    {key: 'gpu', label: 'GPU', type: 'text', help: 'e.g. L4, A100, H100:2'},
    {key: 'region', label: 'Region', type: 'text'},
    {key: 'timeout_seconds', label: 'Timeout seconds', type: 'int', advanced: true},
    {key: 'health_timeout_seconds', label: 'Health timeout', type: 'float', advanced: true},
    {key: 'apt_install', label: 'Extra apt packages', type: 'text', help: 'comma-separated', advanced: true},
    {key: 'pip_install', label: 'Extra pip packages', type: 'text', help: 'comma-separated', advanced: true},
  ],
  e2b: [
    {key: 'template', label: 'Template', type: 'text'},
    {key: 'api_key', label: 'API key', type: 'secret'},
    {key: 'timeout_seconds', label: 'Timeout seconds', type: 'int', advanced: true},
    {key: 'health_timeout_seconds', label: 'Health timeout', type: 'float', advanced: true},
    {key: 'pool_size', label: 'Pool size', type: 'int', advanced: true},
    {key: 'domain', label: 'Domain', type: 'text', advanced: true},
    {key: 'allow_internet_access', label: 'Allow internet access', type: 'bool', advanced: true},
    {key: 'secure', label: 'Secure sandbox', type: 'bool', advanced: true},
  ],
  ssh: [
    {key: 'host', label: 'Host', type: 'text'},
    {key: 'port', label: 'SSH port', type: 'int'},
    {key: 'key', label: 'SSH key path', type: 'secret'},
    {key: 'tunnel_port', label: 'Tunnel port', type: 'int', advanced: true},
    {key: 'keep_warm', label: 'Keep warm', type: 'bool', advanced: true},
    {key: 'health_timeout_seconds', label: 'Health timeout', type: 'float', advanced: true},
  ],
  daytona: [
    {key: 'api_key', label: 'API key', type: 'secret'},
    {key: 'api_url', label: 'API URL', type: 'text'},
    {key: 'target', label: 'Target', type: 'text'},
    {key: 'ssh_host', label: 'SSH host', type: 'text'},
    {key: 'ssh_port', label: 'SSH port', type: 'int'},
    {key: 'timeout_seconds', label: 'Timeout seconds', type: 'int', advanced: true},
    {key: 'health_timeout_seconds', label: 'Health timeout', type: 'float', advanced: true},
    {key: 'ssh_token_ttl_minutes', label: 'SSH token TTL minutes', type: 'int', advanced: true},
    {key: 'sandbox_timeout_seconds', label: 'Sandbox timeout', type: 'int', advanced: true},
  ],
  aws: [
    {key: 'region', label: 'Region', type: 'text'},
    {key: 'image_id', label: 'Image ID', type: 'text'},
    {key: 'key_name', label: 'Key name', type: 'text'},
    {key: 'instance_type', label: 'Instance type', type: 'text'},
    {key: 'ssh_user', label: 'SSH user', type: 'text'},
    {key: 'key', label: 'SSH private key', type: 'secret'},
    {key: 'subnet_id', label: 'Subnet ID', type: 'text', advanced: true},
    {key: 'security_group_ids', label: 'Security groups', type: 'text', help: 'comma-separated', advanced: true},
    {key: 'ssh_port', label: 'SSH port', type: 'int', advanced: true},
    {key: 'timeout_seconds', label: 'Timeout seconds', type: 'int', advanced: true},
    {key: 'health_timeout_seconds', label: 'Health timeout', type: 'float', advanced: true},
    {key: 'keep_warm', label: 'Keep warm', type: 'bool', advanced: true},
  ],
  azure: [
    {key: 'subscription_id', label: 'Subscription ID', type: 'text'},
    {key: 'resource_group', label: 'Resource group', type: 'text'},
    {key: 'location', label: 'Location', type: 'text'},
    {key: 'vm_size', label: 'VM size', type: 'text'},
    {key: 'image', label: 'Image', type: 'text', help: 'Publisher:Offer:Sku:Version'},
    {key: 'ssh_user', label: 'SSH user', type: 'text'},
    {key: 'key', label: 'SSH private key', type: 'secret'},
    {key: 'ssh_public_key', label: 'SSH public key', type: 'secret', advanced: true},
    {key: 'ssh_cidr', label: 'SSH CIDR', type: 'text', advanced: true},
    {key: 'vnet_cidr', label: 'VNet CIDR', type: 'text', advanced: true},
    {key: 'subnet_cidr', label: 'Subnet CIDR', type: 'text', advanced: true},
    {key: 'ssh_port', label: 'SSH port', type: 'int', advanced: true},
    {key: 'timeout_seconds', label: 'Timeout seconds', type: 'int', advanced: true},
    {key: 'health_timeout_seconds', label: 'Health timeout', type: 'float', advanced: true},
    {key: 'keep_warm', label: 'Keep warm', type: 'bool', advanced: true},
  ],
  manual: [
    {key: 'base_url', label: 'Base URL', type: 'text'},
    {key: 'bearer_token', label: 'Bearer token', type: 'secret'},
    {key: 'workspace_root', label: 'Workspace root', type: 'text', advanced: true},
    {key: 'bundle_dir', label: 'Bundle dir', type: 'text', advanced: true},
  ],
};

function statusLabel(s) {
  if (s === 'committed') return 'Kept';
  if (s === 'discarded') return 'Skip';
  if (s === 'failed') return 'Failed';
  if (s === 'active') return 'Active';
  if (s === 'pruned') return 'Pruned';
  return s || '?';
}

function isPrunedKept(node) {
  return !!node && node.status === 'pruned' && node.prune_kind === 'exhausted' && !!node.commit;
}

function isInvalidLineage(node) {
  return !!node && (node.effective_status === 'invalidated' || node.effective_status === 'lineage_blocked');
}

function resultStatus(node) {
  if (isPrunedKept(node)) return 'committed';
  if (isInvalidLineage(node)) return 'pruned';
  return node?.status || 'pending';
}

function statusLabelForNode(node) {
  if (isPrunedKept(node)) return 'Kept / Pruned';
  if (node?.effective_status === 'invalidated') return 'Invalid';
  if (node?.effective_status === 'lineage_blocked') return 'Blocked';
  return statusLabel(node?.status);
}

function statusColorForNode(node) {
  return STATUS_COLORS[resultStatus(node)] || STATUS_COLORS.pending || '#52525b';
}

function isValidResultForDashboard(node) {
  return !!node
    && node.id !== 'root'
    && node.score != null
    && node.gate_result !== false
    && resultStatus(node) === 'committed';
}

function shortId(id) {
  return id.replace('exp_', '');
}

function relTime(iso) {
  if (!iso) return '--';
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return '<1m';
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ' + (m % 60) + 'm';
  return Math.floor(h / 24) + 'd';
}

function pct(a, b) {
  if (!b || b === 0) return '--';
  return Math.round((a / b) * 100) + '%';
}

function formatDuration(startIso, endIso) {
  if (!startIso || !endIso) return '';
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (ms < 0) return '';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return m + 'm ' + rs + 's';
  const h = Math.floor(m / 60);
  return h + 'h ' + (m % 60) + 'm';
}

function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function scoreDelta(node) {
  if (node.score == null) return '';
  const parent = state.graph.nodes[node.parent];
  if (!parent || parent.score == null) return '';
  const d = node.score - parent.score;
  const sign = d >= 0 ? '+' : '';
  return sign + d.toFixed(2);
}

// Color for a score-delta string from scoreDelta(), honoring metric
// direction. For a 'max' metric an increase (+) is the improvement (green);
// for 'min' (less is better) a decrease (-) is. No change / no parent is
// neutral.
function deltaColorFor(delta) {
  if (!delta || delta === '+0.00' || delta === '-0.00') return 'var(--text-4)';
  const isMax = (state.stats?.metric || 'max') === 'max';
  const improved = delta.startsWith('+') ? isMax : !isMax;
  return improved ? 'var(--green)' : 'var(--red)';
}

// Improvement class ('up'/'down'/'neutral') for a delta string — same
// metric-direction logic as deltaColorFor. 'up' (green) means improved.
function deltaClassFor(delta) {
  if (!delta || delta === '+0.00' || delta === '-0.00') return 'neutral';
  const isMax = (state.stats?.metric || 'max') === 'max';
  return (delta.startsWith('+') ? isMax : !isMax) ? 'up' : 'down';
}

function getExperiments() {
  return Object.values(state.graph.nodes)
    .filter(n => n.id !== 'root')
    .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
}

function backendLabel(spec) {
  if (!spec) return '--';
  if (spec.name === 'remote') {
    return spec.provider ? `remote/${spec.provider}` : 'remote';
  }
  return spec.name || '--';
}

// Backend / provider logos.
//
// Vendor brand marks (modal, e2b, daytona, aws, azure) load from
// /static/logos/<name>.svg — drop the official SVG from each vendor's
// brand page into that directory and it surfaces here. The <img>
// onerror handler falls back to the stylized inline mark below so a
// missing file degrades gracefully instead of breaking the layout.
//
// Generic backends (worktree, pool, ssh, manual) stay stylized — no
// vendor brand exists for them.
const BACKEND_FALLBACK_SVG = {
  worktree: `<svg viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <circle cx="2.5" cy="2.5" r="1.3" fill="#22c55e"/>
    <circle cx="2.5" cy="9.5" r="1.3" fill="#22c55e"/>
    <circle cx="9.5" cy="2.5" r="1.3" fill="#22c55e"/>
    <line x1="2.5" y1="3.8" x2="2.5" y2="8.2" stroke="#22c55e" stroke-width="1.1"/>
    <path d="M 2.5 5.5 Q 2.5 7.5 9.5 4" stroke="#22c55e" stroke-width="1.1" fill="none"/>
  </svg>`,
  pool: `<svg viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <rect x="1" y="3" width="2" height="6" rx="0.4" fill="#3b82f6"/>
    <rect x="4" y="3" width="2" height="6" rx="0.4" fill="#3b82f6"/>
    <rect x="7" y="3" width="2" height="6" rx="0.4" fill="#3b82f6" opacity="0.45"/>
    <rect x="10" y="3" width="2" height="6" rx="0.4" fill="#3b82f6" opacity="0.45"/>
  </svg>`,
  ssh: `<svg viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <rect x="1" y="2" width="10" height="8" rx="1.2" fill="none" stroke="#a1a1aa" stroke-width="1"/>
    <path d="M 3.2 5 L 4.8 6 L 3.2 7" stroke="#a1a1aa" stroke-width="1" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
    <line x1="6.2" y1="7.1" x2="9" y2="7.1" stroke="#a1a1aa" stroke-width="1" stroke-linecap="round"/>
  </svg>`,
  manual: `<svg viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <circle cx="6" cy="6" r="4" fill="none" stroke="#a1a1aa" stroke-width="1.3" stroke-dasharray="2 2"/>
  </svg>`,
  // Vendor fallbacks — only used if the official SVG isn't dropped in
  // /static/logos/. Recognizable-by-shape marks in the vendor's color,
  // not trademark-fidelity reproductions.
  e2b: `<svg viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <rect x="1" y="1" width="4" height="4" rx="0.6" fill="#fbbf24"/>
    <rect x="7" y="1" width="4" height="4" rx="0.6" fill="#fbbf24"/>
    <rect x="1" y="7" width="4" height="4" rx="0.6" fill="#fbbf24"/>
    <rect x="7" y="7" width="4" height="4" rx="0.6" fill="#fbbf24"/>
  </svg>`,
  daytona: `<svg viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <circle cx="6" cy="6" r="4.8" fill="#10b981"/>
    <path d="M 4 4 L 8 6 L 4 8 Z" fill="#0a0a0c"/>
  </svg>`,
  aws: `<svg viewBox="0 0 24 12" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <text x="12" y="5" font-family="-apple-system, sans-serif" font-size="5.4" font-weight="800" fill="#ff9900" text-anchor="middle" dominant-baseline="central">AWS</text>
    <path d="M 4 9.2 Q 12 11.4 20 9.2" stroke="#ff9900" stroke-width="1.2" fill="none" stroke-linecap="round"/>
  </svg>`,
  azure: `<svg viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <polygon points="6,1.5 10.5,10.5 1.5,10.5" fill="#0078d4"/>
    <polygon points="6,1.5 7.6,5 4.5,10.5 1.5,10.5" fill="#005ba1"/>
  </svg>`,
};
// Vendor brand → file extension. Most marks ship as SVG; E2B only
// publishes a PNG of their circular mark publicly, so we load that.
// Anything missing from this map falls through to the stylized inline
// SVG fallback.
const VENDOR_LOGO_EXT = {
  modal: 'svg', aws: 'svg', azure: 'svg', daytona: 'svg', e2b: 'png',
};

function backendLogoFor(spec) {
  if (!spec) return '';
  const key = spec.name === 'remote' ? spec.provider : spec.name;
  if (!key) return '';
  const ext = VENDOR_LOGO_EXT[key];
  if (ext) {
    // Inline the fallback in the onerror handler so a missing file
    // gracefully degrades to the stylized mark without a broken-image
    // glyph in between.
    const fallback = BACKEND_FALLBACK_SVG[key] || '';
    const fallbackEnc = fallback ? encodeURIComponent(fallback) : '';
    return `<img src="/static/logos/${key}.${ext}" alt="${esc(key)}"
      onerror="this.onerror=null; this.outerHTML=decodeURIComponent('${fallbackEnc}');">`;
  }
  return BACKEND_FALLBACK_SVG[key] || '';
}

function metaRow(key, value, opts) {
  opts = opts || {};
  const mono = opts.mono ? 'mono' : '';
  const style = opts.color ? ` style="color:${opts.color}"` : '';
  return `<div class="drawer-meta-row"><span class="drawer-meta-key">${esc(key)}</span><span class="drawer-meta-val ${mono}"${style}>${esc(String(value ?? '--'))}</span></div>`;
}

// Per-backend specialized renderer. Each branch picks the fields that
// actually matter for that backend (e.g., worktree path vs remote app
// name + region) instead of dumping the raw config blob. The raw JSON
// is preserved behind a small "show raw config" details fold-out for
// power users.
function renderBackendSection(node, ws) {
  const spec = node.resolved_backend || ws.default_backend;
  if (!spec) return '';
  const name = spec.name || 'unknown';
  const cfg = spec.config || {};
  const logo = backendLogoFor(spec);
  const sourceLabel = spec.source === 'override' ? 'per-experiment override' : 'workspace default';

  let label = name;
  let rows = '';

  if (name === 'worktree') {
    rows = metaRow('Path', node.worktree || '--', { mono: true });
  } else if (name === 'pool') {
    const slots = Array.isArray(cfg.slots) ? cfg.slots : [];
    rows = metaRow('Leased', node.worktree || '--', { mono: true })
         + metaRow('Pool size', `${slots.length} slot${slots.length === 1 ? '' : 's'}`);
  } else if (name === 'remote') {
    const provider = spec.provider || 'unknown';
    label = `remote · ${provider}`;
    const pc = cfg.provider_config || {};
    rows = renderProviderRows(provider, pc);
  }

  // Backend label sits in a normal meta-row, with the vendor logo
  // inlined next to the text (not a separate pill on the right).
  const backendRow = `<div class="drawer-meta-row">
    <span class="drawer-meta-key">Backend</span>
    <span class="drawer-meta-val">
      <span class="drawer-backend-inline-logo">${logo}</span>${esc(label)}
    </span>
  </div>`;

  return `<div class="drawer-section">
    <span class="drawer-section-title">Execution Backend</span>
    ${backendRow}
    ${metaRow('Source', sourceLabel)}
    ${spec.state_key ? metaRow('State key', spec.state_key, { mono: true }) : ''}
    ${rows}
    <details class="drawer-raw-config">
      <summary>raw config</summary>
      <pre class="config-pre">${esc(prettyJson(cfg))}</pre>
    </details>
  </div>`;
}

function renderProviderRows(provider, pc) {
  // Keys that are interesting per provider. Anything that doesn't map
  // gets surfaced via the "raw config" fold-out.
  if (provider === 'modal') {
    return metaRow('App', pc.app_name || '--')
         + metaRow('Region', pc.region || '--')
         + (pc.image ? metaRow('Image', pc.image, { mono: true }) : '')
         + (pc.cpu != null || pc.memory_gb != null
             ? metaRow('Compute', `${pc.cpu ?? '?'} cpu · ${pc.memory_gb ?? '?'} gb`)
             : '')
         + (pc.timeout != null ? metaRow('Timeout', `${pc.timeout}s`) : '')
         + (Array.isArray(pc.secrets) && pc.secrets.length
             ? metaRow('Secrets', pc.secrets.join(', '))
             : '');
  }
  if (provider === 'e2b') {
    return metaRow('Template', pc.template || '--', { mono: true })
         + (pc.timeout != null ? metaRow('Timeout', `${pc.timeout}s`) : '');
  }
  if (provider === 'aws') {
    return metaRow('Instance', pc.instance_type || '--', { mono: true })
         + metaRow('Region', pc.region || '--')
         + (pc.ami ? metaRow('AMI', pc.ami, { mono: true }) : '')
         + (pc.subnet_id ? metaRow('Subnet', pc.subnet_id, { mono: true }) : '');
  }
  if (provider === 'azure') {
    return metaRow('VM size', pc.vm_size || '--', { mono: true })
         + metaRow('Region', pc.region || '--')
         + (pc.resource_group ? metaRow('Resource group', pc.resource_group, { mono: true }) : '');
  }
  if (provider === 'daytona') {
    return metaRow('Region', pc.region || '--')
         + (pc.image ? metaRow('Image', pc.image, { mono: true }) : '');
  }
  if (provider === 'ssh') {
    const host = pc.host || '--';
    const port = pc.port || 22;
    return metaRow('Host', `${host}:${port}`, { mono: true })
         + (pc.user ? metaRow('User', pc.user, { mono: true }) : '')
         + (pc.agent_path ? metaRow('Agent path', pc.agent_path, { mono: true }) : '');
  }
  if (provider === 'manual') {
    return (pc.base_url ? metaRow('Base URL', pc.base_url, { mono: true }) : '')
         + metaRow('Auth', isRedacted(pc.bearer_token) ? 'bearer (redacted)' : (pc.bearer_token ? 'bearer set' : 'none'));
  }
  // Unknown provider — just an empty body; the raw config fold-out
  // will surface everything.
  return '';
}

function prettyJson(value) {
  return JSON.stringify(value || {}, null, 2);
}

function isRedacted(value) {
  return value === '<redacted>';
}

function normalizeFormValue(raw, type) {
  if (type === 'bool') return !!raw;
  if (raw == null) return '';
  const value = String(raw).trim();
  if (value === '') return '';
  if (type === 'int') {
    const parsed = parseInt(value, 10);
    return Number.isNaN(parsed) ? '' : parsed;
  }
  if (type === 'float') {
    const parsed = parseFloat(value);
    return Number.isNaN(parsed) ? '' : parsed;
  }
  return value;
}

function providerFields(provider) {
  return REMOTE_PROVIDER_FIELDS[provider] || [];
}

// ─── API ─────────────────────────────────────────────────
// Track in-flight polls so a slow request can't overwrite a fresher one
// (e.g. /api/graph takes >5s during a big run; the next interval-tick
// would race the response and clobber state.graph mid-render).
let _fetchInflight = false;
async function fetchAll() {
  if (_fetchInflight) return;
  _fetchInflight = true;
  try {
    const [stats, graph, runs, workspace, frontier] = await Promise.all([
      fetch('/api/stats').then(r => r.json()),
      fetch('/api/graph').then(r => r.json()),
      fetch('/api/runs').then(r => r.json()),
      fetch('/api/workspace').then(r => r.json()),
      fetch('/api/frontier').then(r => r.json()).catch(() => null),
    ]);
    state.stats = stats;
    state.graph = graph;
    state.runs = runs;
    state.workspace = workspace;
    state.frontier = frontier;
    render();
  } catch (e) {
    console.error('fetch error:', e);
  } finally {
    _fetchInflight = false;
  }
}

async function switchRun(runId) {
  await fetch(`/api/runs/${runId}/activate`, { method: 'POST' });
  state.scopeRoot = 'root';
  state.collapsed = new Set();
  state.selectedNode = null;
  state.viewMode = 'all';
  closeSidebar();
  // Await so the next interval tick doesn't see partial state mid-swap.
  await fetchAll();
}

// ─── Render: Top bar ─────────────────────────────────────
function renderTopbar() {
  const s = state.stats || {};

  // Project + entrypoint — middot separator instead of an "Entrypoint" label.
  const targetEl = document.getElementById('target-file');
  if (targetEl) {
    targetEl.innerHTML = s.target
      ? `<strong>${esc(s.project_name || 'evo project')}</strong><span class="target-sep">·</span><span>${esc(s.target)}</span>`
      : (s.project_name ? `<strong>${esc(s.project_name)}</strong>` : '');
  }

  // ─── Stat strip: 5 cards × (label / value+delta / sub-line) ─────
  // Best score · Experiments · Keep rate · Frontier · Active.
  const totalExps = (s.total_experiments || 0) - (s.total_experiments ? 1 : 0); // strip the synthetic root
  const committed = s.committed || 0;
  const discarded = s.discarded || 0;
  const failed = s.failed || 0;
  const active = s.active || 0;
  const frontier = (state.frontier && state.frontier.all_ids ? state.frontier.all_ids.length : s.frontier) || 0;

  // BEST SCORE — value + percent delta + "from <baseline> baseline".
  const bestValEl = document.getElementById('stat-best-value');
  const bestDeltaEl = document.getElementById('stat-best-delta');
  const bestSubEl = document.getElementById('stat-best-sub');
  if (bestValEl && bestDeltaEl && bestSubEl) {
    if (s.best_score == null) {
      bestValEl.textContent = '--';
      bestDeltaEl.textContent = '';
      bestDeltaEl.className = 'stat-delta';
      bestSubEl.textContent = 'no committed score yet';
    } else {
      bestValEl.textContent = s.best_score.toFixed(2);
      const hasBaseline = s.baseline_score != null;
      const baseline = hasBaseline ? s.baseline_score : s.best_score;
      const isMax = (s.metric || 'max') === 'max';
      const diff = s.best_score - baseline;
      let cls, pctStr;
      if (Math.abs(diff) < 1e-9) {
        cls = 'neutral';
        pctStr = '0%';
      } else {
        cls = (isMax ? diff > 0 : diff < 0) ? 'up' : 'down';
        if (baseline !== 0) {
          const pctVal = Math.round((diff / Math.abs(baseline)) * 100);
          pctStr = `${pctVal >= 0 ? '+' : ''}${pctVal}%`;
        } else {
          pctStr = `${diff >= 0 ? '+' : ''}${diff.toFixed(2)}`;
        }
      }
      bestDeltaEl.textContent = pctStr;
      bestDeltaEl.className = `stat-delta ${cls}`;
      bestSubEl.textContent = hasBaseline ? `from ${baseline.toFixed(2)} baseline` : 'baseline';
    }
  }

  // EXPERIMENTS — total non-root count + kept/skip/err breakdown.
  const expValEl = document.getElementById('stat-exp-value');
  const expSubEl = document.getElementById('stat-exp-sub');
  if (expValEl) expValEl.textContent = String(totalExps);
  if (expSubEl) {
    if (totalExps === 0) {
      expSubEl.textContent = 'no experiments yet';
    } else {
      const parts = [];
      if (committed > 0) parts.push(`<span class="exp-count kept">${committed} kept</span>`);
      if (discarded > 0) parts.push(`<span class="exp-count skip">${discarded} skip</span>`);
      if (failed > 0) parts.push(`<span class="exp-count err">${failed} err</span>`);
      expSubEl.innerHTML = parts.join(' ');
    }
  }

  // FRONTIER + ACTIVE — simple counts.
  const frontierValEl = document.getElementById('stat-frontier-value');
  if (frontierValEl) frontierValEl.textContent = String(frontier);
  const activeValEl = document.getElementById('stat-active-value');
  if (activeValEl) activeValEl.textContent = String(active);

  // Run switcher: always render as a <select> so the affordance is consistent,
  // even when there's only one run.
  const runs = state.runs || [];
  const switcher = document.getElementById('run-switcher');
  if (runs.length > 0) {
    const options = runs.map(r =>
      `<option value="${esc(r.id)}" ${r.active ? 'selected' : ''}>${esc(r.id)}</option>`
    ).join('');
    switcher.innerHTML = `<select class="run-select" onchange="switchRun(this.value)">${options}</select>`;
    switcher.classList.remove('hidden');
  } else {
    switcher.classList.add('hidden');
  }
}

// ─── Render: Scatter (left rail, score over time) ────────
//
// Inline-SVG scatter that lives in the left rail. x = experiment index by
// creation order, y = score (auto-ranged with a 0..1 fallback). Each dot
// is colored by status (green/red/grey + purple for active) and gets a
// gold ring overlay when it's on the best-path spine. A "running best"
// stair line traces the cumulative best committed score. Click a dot to
// open its drawer. Replaces the old histogram strip above the timeline.
function renderScatter() {
  const host = document.getElementById('scatter-plot');
  if (!host) return;

  const exps = Object.values(state.graph.nodes)
    .filter((n) => n.id !== 'root')
    .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));

  const bestEl = document.getElementById('scatter-best');
  if (!exps.length) {
    host.innerHTML = `<div class="scatter-empty">Scores will show up here.</div>`;
    if (bestEl) bestEl.textContent = '';
    return;
  }

  // y-range from scored experiments only; clamp to 0..1 floor for readability.
  let ymin = Infinity, ymax = -Infinity;
  for (const n of exps) {
    if (n.score == null) continue;
    ymin = Math.min(ymin, n.score);
    ymax = Math.max(ymax, n.score);
  }
  if (!isFinite(ymin)) { ymin = 0; ymax = 1; }
  if (ymax === ymin) { ymax = ymin + 1; }
  // Pad the visible range so dots never touch the frame edges.
  const yPad = (ymax - ymin) * 0.1;
  ymin -= yPad;
  ymax += yPad;

  // Use the host's actual rendered size — the scatter lives in a fixed
  // overlay panel so width/height are stable per render.
  const W = Math.max(host.clientWidth || 252, 200);
  const H = Math.max(host.clientHeight || 122, 80);
  const PAD_L = 26, PAD_R = 8, PAD_T = 6, PAD_B = 14;
  const innerW = W - PAD_L - PAD_R;
  const innerH = H - PAD_T - PAD_B;
  // Reserve a horizontal gutter inside the plot area so dots never sit
  // flush against the y-axis or the right edge — important when there
  // are only one or two points (otherwise the first dot pins to the
  // axis line and reads as part of the chrome).
  const xGutter = Math.max(14, Math.min(40, innerW * 0.08));
  const usableW = Math.max(20, innerW - 2 * xGutter);
  const xFor = (i) =>
    exps.length > 1
      ? PAD_L + xGutter + (i / (exps.length - 1)) * usableW
      : PAD_L + xGutter + usableW / 2;
  const yFor = (s) => PAD_T + (1 - (s - ymin) / (ymax - ymin)) * innerH;

  const isMax = (state.stats.metric || 'max') === 'max';
  const bestId = bestExperimentId();
  const spine = lineageSet(bestId);

  // Cumulative best line — step function across committed experiments only.
  let best = null;
  const stairPts = [];
  exps.forEach((n, i) => {
    if (!isValidResultForDashboard(n)) return;
    if (best == null || (isMax ? n.score > best : n.score < best)) best = n.score;
    stairPts.push([xFor(i), yFor(best)]);
  });
  // Build a stepped path: move to first, step horizontally to next x at
  // current y, then vertically to next y, etc.
  let stairD = '';
  for (let i = 0; i < stairPts.length; i++) {
    const [x, y] = stairPts[i];
    if (i === 0) stairD += `M${x.toFixed(1)},${y.toFixed(1)}`;
    else {
      const [, prevY] = stairPts[i - 1];
      stairD += ` L${x.toFixed(1)},${prevY.toFixed(1)} L${x.toFixed(1)},${y.toFixed(1)}`;
    }
  }

  // Y-axis tick labels (5 evenly-spaced values).
  const ticks = [];
  const tickCount = 4;
  for (let i = 0; i <= tickCount; i++) {
    const v = ymin + (ymax - ymin) * (i / tickCount);
    const y = yFor(v);
    ticks.push(`<line class="scatter-grid" x1="${PAD_L}" x2="${W - PAD_R}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>
                <text class="scatter-tick" x="${PAD_L - 4}" y="${y.toFixed(1)}" text-anchor="end" dominant-baseline="middle">${v.toFixed(2)}</text>`);
  }

  // Dots — render committed last so they sit on top visually.
  const dotOrder = (n) => {
    const status = resultStatus(n);
    if (status === 'committed') return 4;
    if (n.status === 'active') return 3;
    if (n.status === 'failed') return 2;
    return 1;
  };
  const items = exps.map((n, i) => ({ n, i }))
    .sort((a, b) => dotOrder(a.n) - dotOrder(b.n));

  // Build the same kind of hover payload the compact bars use:
  //   <id> • <status> • <score>
  //   <hypothesis>
  //   <delta from parent>
  function dotHoverText(n, scoreText) {
    const head = `${shortId(n.id)} · ${statusLabelForNode(n)} · ${scoreText}`;
    const hyp = n.hypothesis || (n.status === 'failed' ? (n.error || 'failed') : '');
    const delta = scoreDelta(n);
    const parts = [head];
    if (hyp) parts.push(hyp);
    if (delta) parts.push(`${delta} from ${shortId(n.parent)}`);
    return parts.join('\n\n');
  }

  const dots = items.map(({ n, i }) => {
    if (n.score == null) return '';  // nothing to plot for active/pending
    const cx = xFor(i);
    const cy = yFor(n.score);
    const status = resultStatus(n);
    const statusCls = `${status}${isPrunedKept(n) ? ' pruned-kept' : ''}`;
    const sel = state.selectedNode === n.id ? 'selected' : '';
    const isBest = n.id === bestId;
    // Best dot reads brighter (white-ish outline + larger radius) so the
    // eye lands on it first. Spine dots get the amber halo. Selected
    // dots get the white selection ring (handled in CSS).
    const r = isBest ? 5 : (status === 'committed' ? 4 : 3);
    const lin = spine.has(n.id) ? `<circle class="scatter-spine" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${r + 2.5}"/>` : '';
    const bestCls = isBest ? ' best' : '';
    const hover = esc(dotHoverText(n, n.score.toFixed(3)));
    return `${lin}<circle class="scatter-dot ${statusCls}${bestCls} ${sel}" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${r}" data-id="${esc(n.id)}" data-hover="${hover}"/>`;
  }).join('');

  // No-score dots (active / pending) — sit on a phantom baseline row at the
  // bottom of the plot so the user still sees them appear.
  const noScore = exps
    .map((n, i) => ({ n, i }))
    .filter(({ n }) => n.score == null)
    .map(({ n, i }) => {
      const cx = xFor(i);
      const cy = H - PAD_B + 6;  // just below the axis line
      const sel = state.selectedNode === n.id ? 'selected' : '';
      const hover = esc(dotHoverText(n, 'no score yet'));
      const statusCls = `${resultStatus(n)}${isPrunedKept(n) ? ' pruned-kept' : ''}`;
      return `<circle class="scatter-dot ${statusCls} ${sel} no-score" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="2.5" data-id="${esc(n.id)}" data-hover="${hover}"/>`;
    })
    .join('');

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="scatter-svg">
    ${ticks.join('')}
    <line class="scatter-axis" x1="${PAD_L}" x2="${W - PAD_R}" y1="${H - PAD_B}" y2="${H - PAD_B}"/>
    ${stairD ? `<path class="scatter-stair" d="${stairD}"/>` : ''}
    ${dots}
    ${noScore}
  </svg>`;

  // Click → open drawer for that node.
  host.querySelectorAll('.scatter-dot').forEach((el) => {
    el.addEventListener('click', () => {
      const id = el.getAttribute('data-id');
      if (id) openDrawer(id);
    });
  });
  // Hover → same tip-popover treatment the compact bars use. Bound once
  // per session on the scatter host via delegation so it survives re-renders.
  bindScatterHoverTips(host);

  // Best-score chip in the rail header.
  if (bestEl) {
    const b = state.stats.best_score;
    bestEl.textContent = b != null ? `best ${b.toFixed(2)}` : '';
  }

  bindScatterResize();
}

// ─── Timeline: tree helpers ──────────────────────────────
const STATUS_ORDER = ['committed', 'active', 'failed', 'discarded', 'pruned'];

function getNode(id) { return state.graph.nodes[id]; }

function childIds(id) {
  const n = getNode(id);
  if (!n) return [];
  return (n.children || []).filter((c) => getNode(c));
}

function isInScope(id) {
  if (state.scopeRoot === 'root') return true;
  if (id === state.scopeRoot) return true;
  let cur = getNode(id);
  while (cur && cur.parent) {
    if (cur.parent === state.scopeRoot) return true;
    cur = getNode(cur.parent);
  }
  return false;
}

function pathFromRoot(id) {
  const path = [];
  let cur = getNode(id);
  while (cur) {
    path.unshift(cur.id);
    cur = cur.parent ? getNode(cur.parent) : null;
  }
  return path;
}

// Best (committed) experiment by metric direction
function bestExperimentId() {
  const metric = state.stats.metric || 'max';
  const isMax = metric === 'max';
  let best = null;
  for (const n of Object.values(state.graph.nodes)) {
    if (!isValidResultForDashboard(n)) continue;
    if (!best || (isMax ? n.score > best.score : n.score < best.score)) best = n;
  }
  return best ? best.id : null;
}

// Frontier semantics come from the backend (mirrors `evo frontier`):
// committed, not pruned, no children with status in {committed, active}.
// We rely on state.frontier.all_ids rather than recomputing here so the
// definition can't drift from core.py.
function isFrontierCandidate(n) {
  if (!n || n.id === 'root') return false;
  const all = state.frontier && state.frontier.all_ids;
  if (!all) return false;
  return all.includes(n.id);
}

function lineageSet(id) {
  if (!id) return new Set();
  return new Set(pathFromRoot(id));
}

function frontierSet() {
  const all = state.frontier && state.frontier.all_ids;
  return new Set(all || []);
}

// Build the set of visible rows with explicit (depth, rowIndex) coords.
//
// Layout strategy (tidy-tree):
//   1. Sort each parent's children "spine-first": the child on the path to
//      the current best comes first, then siblings by created_at.
//   2. Assign rowIndex post-order: each leaf gets a fresh row; every parent
//      INHERITS the rowIndex of its first (spine) child.
//
// Result: the entire path root → ... → best collapses onto rowIndex=0, so
// the best lineage reads as a flat horizontal spine. Childless off-spine
// siblings stack DIRECTLY under the spine row at their depth (so a node
// with no descendants of its own sits visually close to its parent on the
// spine, not far below an unrelated subtree). Off-spine subtrees with
// their own descendants take their own row blocks after all leaves are
// placed.
function buildVisibleRows() {
  const out = [];
  const root = getNode(state.scopeRoot) || getNode('root');
  if (!root) return out;

  const showStatuses = state.statusFilters;
  const frontIds = state.viewMode === 'frontier' ? frontierSet() : null;
  const spine = lineageSet(bestExperimentId());

  function nodePasses(n) {
    if (n.id === 'root') return true;
    if (frontIds && !frontIds.has(n.id)) return false;
    if (!showStatuses.has(n.status)) return false;
    return true;
  }

  // Two flavors of child enumeration:
  //   * sortedChildrenAll — full topology, ignores collapse. Used by the
  //     layout pass so toggling collapse on a subtree never reshuffles
  //     the sibling arrangement.
  //   * sortedChildren — collapse-aware. Used by the emit pass so
  //     collapsed branches don't render their descendants.
  function sortChildren(kids) {
    return [...kids].sort((a, b) => {
      const ain = spine.has(a) ? 0 : 1;
      const bin = spine.has(b) ? 0 : 1;
      if (ain !== bin) return ain - bin;
      const an = getNode(a), bn = getNode(b);
      return (an?.created_at || '').localeCompare(bn?.created_at || '');
    });
  }
  function sortedChildrenAll(id) {
    return sortChildren(childIds(id));
  }
  function sortedChildren(id) {
    if (state.collapsed.has(id)) return [];
    return sortChildren(childIds(id));
  }

  // Pass 1: assign rowIndex to every node in the (possibly scoped) subtree.
  //
  // Each depth owns its own row lane. Different depths live in different
  // columns (barX = leftPad + depth * colWidth), so a node at (depth=3,
  // row=1) and a node at (depth=2, row=1) don't visually overlap — they
  // can safely share the row number. We allocate per depth with a counter
  // map and only enforce one geometric rule: a node's row must be >= its
  // parent's row, so connectors never point upward.
  //
  // Spine (root → ... → best) collapses onto row 0; row 0 is reserved
  // when a spine is in view. Off-spine children of each spine node split:
  //   * Leaves take the next free row at depth+1 immediately, so they
  //     cluster directly under the spine parent's column.
  //   * Subtrees wait until the spine pass finishes, then each subtree
  //     root takes the next free row at its depth (>= 1 if spine in view),
  //     and recurses with the same rule.
  const rowOf = new Map();
  const nextRowAtDepth = new Map();
  const spineInView = spine.has(root.id);
  const baseRow = spineInView ? 1 : 0;  // row 0 is spine when present

  function isLeaf(id) {
    // Topology check — ignores collapse so a collapsed subtree still
    // classifies as a subtree (not a leaf) for layout purposes.
    return sortedChildrenAll(id).length === 0;
  }
  function takeRow(depth, minRow) {
    const next = nextRowAtDepth.has(depth) ? nextRowAtDepth.get(depth) : baseRow;
    const row = Math.max(minRow, next);
    nextRowAtDepth.set(depth, row + 1);
    return row;
  }
  function placeSpine(id, depth) {
    rowOf.set(id, 0);
    const kids = sortedChildrenAll(id);
    // Phase A: leaves at depth+1 cluster directly under the spine.
    for (const k of kids) {
      if (!spine.has(k) && isLeaf(k)) {
        rowOf.set(k, takeRow(depth + 1, 0));
      }
    }
    // Phase B: continue down the spine.
    for (const k of kids) {
      if (spine.has(k)) { placeSpine(k, depth + 1); break; }
    }
    // Phase C: off-spine subtrees take blocks at depth+1, constrained to
    // sit below any leaves already placed there.
    for (const k of kids) {
      if (!spine.has(k) && !isLeaf(k)) placeSubtree(k, depth + 1, 0);
    }
  }
  function placeSubtree(id, depth, minRow) {
    const kids = sortedChildrenAll(id);
    if (kids.length === 0) {
      rowOf.set(id, takeRow(depth, minRow));
      return;
    }
    // Place all descendants FIRST so the subtree root can sit visually
    // below them. Children inherit `minRow` (the floor passed from the
    // grandparent) rather than the parent's row, since the parent's row
    // isn't known yet at this point.
    for (const k of kids) {
      if (isLeaf(k)) {
        rowOf.set(k, takeRow(depth + 1, minRow));
      }
    }
    for (const k of kids) {
      if (!isLeaf(k)) placeSubtree(k, depth + 1, minRow);
    }
    // Now park the subtree root at max(child rows) + 1 so the root visually
    // anchors the BOTTOM of its branch rather than the top.
    let maxKid = -1;
    for (const k of kids) {
      const r = rowOf.get(k);
      if (r !== undefined && r > maxKid) maxKid = r;
    }
    rowOf.set(id, takeRow(depth, Math.max(minRow, maxKid + 1)));
  }
  if (spineInView) {
    placeSpine(root.id, 0);
  } else {
    // Scoped to a non-spine subtree — no spine in view, so layout starts
    // at row 0 with the same per-depth rules.
    placeSubtree(root.id, 0, 0);
  }

  // Pass 2: emit entries in pre-order (depth + rowIndex come from rowOf).
  function emit(id, depth) {
    const node = getNode(id);
    if (!node) return;
    const allKids = childIds(id);
    const hasChildren = allKids.length > 0;
    const collapsed = state.collapsed.has(id);
    if (nodePasses(node)) {
      out.push({
        node,
        depth,
        rowIndex: rowOf.get(id),
        hasChildren,
        collapsed,
        hiddenChildCount: collapsed ? countDescendants(id) : 0,
        bestChildScore: collapsed ? bestDescendantScore(id) : null,
      });
    }
    for (const cid of sortedChildren(id)) emit(cid, depth + 1);
  }
  if (root.id === 'root') {
    for (const cid of sortedChildren(root.id)) emit(cid, 0);
  } else {
    emit(root.id, 0);
  }

  // Full-topology dimensions — used by renderTimeline to size the
  // scrollable inner box. Measuring from the full layout (rather than
  // the emitted subset) keeps scroll / zoom stable when subtrees are
  // collapsed: hidden rows still "exist" in the canvas, just empty.
  let maxRowFull = 0;
  for (const row of rowOf.values()) {
    if (row > maxRowFull) maxRowFull = row;
  }
  let maxDepthFull = 0;
  (function walkDepth(id, d) {
    if (d > maxDepthFull) maxDepthFull = d;
    for (const c of childIds(id)) walkDepth(c, d + 1);
  })(root.id, 0);

  return { rows: out, maxRowFull, maxDepthFull };
}

function countDescendants(id) {
  let n = 0;
  for (const cid of childIds(id)) { n += 1 + countDescendants(cid); }
  return n;
}

function bestDescendantScore(id) {
  const metric = state.stats.metric || 'max';
  const isMax = metric === 'max';
  let best = null;
  function walk(nid) {
    const node = getNode(nid);
    if (!node) return;
    if (isValidResultForDashboard(node)) {
      if (best == null || (isMax ? node.score > best : node.score < best)) best = node.score;
    }
    for (const cid of childIds(nid)) walk(cid);
  }
  for (const cid of childIds(id)) walk(cid);
  return best;
}

// ─── Render: Filter bar ──────────────────────────────────
function renderFilterBar() {
  const s = state.stats || {};
  // Only show status chips with non-zero counts to reduce visual noise.
  // Four primary lifecycle states only. Evaluated / pending / pruned are
  // still rendered in the timeline (they stay in the default statusFilters
  // set) but don't get a togglable chip — keeps the filter bar uncluttered.
  const chipDefs = [
    { id: 'committed', label: 'kept', count: s.committed || 0 },
    { id: 'discarded', label: 'skip', count: s.discarded || 0 },
    { id: 'failed', label: 'err', count: s.failed || 0 },
    { id: 'active', label: 'active', count: s.active || 0 },
  ];
  const chips = chipDefs.filter((c) => c.count > 0 || state.statusFilters.has(c.id));
  const container = document.getElementById('status-filters');
  if (container) {
    container.innerHTML = chips.map((c) => {
      const on = state.statusFilters.has(c.id);
      return `<span class="status-chip ${c.id} ${on ? 'active-filter' : 'muted'}" onclick="toggleStatusFilter('${c.id}')" title="Toggle ${c.label} experiments">
        <span class="status-dot"></span>${c.label} ${c.count}
      </span>`;
    }).join('');
  }

  // View mode dropdown
  const viewSel = document.getElementById('view-mode');
  if (viewSel) viewSel.value = state.viewMode;

  // Collapse/expand toggle: flips based on whether anything is currently collapsed
  const collapseBtn = document.getElementById('collapse-toggle');
  if (collapseBtn) {
    const anyCollapsed = state.collapsed.size > 0;
    collapseBtn.textContent = anyCollapsed ? 'expand all' : 'collapse all';
    collapseBtn.title = anyCollapsed
      ? 'Expand every collapsed branch'
      : 'Collapse every branch to its root';
  }

  renderScopeBreadcrumb();

  // Visible count
  const { rows } = buildVisibleRows();
  const visible = rows.filter((r) => r.node.id !== 'root').length;
  const total = (s.total_experiments || 0);
  const el = document.getElementById('visible-count');
  if (el) el.textContent = visible === total ? `${visible} shown` : `${visible} of ${total} shown`;
}

function renderScopeBreadcrumb() {
  const el = document.getElementById('scope-breadcrumb');
  if (!el) return;
  const path = pathFromRoot(state.scopeRoot);
  const crumbs = path.map((id, i) => {
    const isLast = i === path.length - 1;
    const label = id === 'root' ? 'root' : shortId(id);
    return `<span class="crumb ${isLast ? 'current' : ''}" onclick="scopeTo('${id}')">${label}</span>`;
  });
  const sep = '<span class="crumb-sep">›</span>';
  let html = crumbs.join(sep);
  if (state.scopeRoot !== 'root') {
    html += `<button class="scope-reset" onclick="scopeTo('root')" title="Reset scope to full tree">reset</button>`;
  }
  el.innerHTML = html;
}

function toggleStatusFilter(id) {
  if (state.statusFilters.has(id)) state.statusFilters.delete(id);
  else state.statusFilters.add(id);
  render();
}

function setViewMode(mode) {
  state.viewMode = (mode === 'lineage' || mode === 'frontier') ? mode : 'all';
  render();
}

function scopeTo(id) {
  if (!getNode(id)) return;
  state.scopeRoot = id;
  // Reset collapsed entries that are no longer in scope
  for (const cid of Array.from(state.collapsed)) {
    if (!isInScope(cid) && cid !== id) state.collapsed.delete(cid);
  }
  render();
}

// Single button that flips between collapse-all and expand-all based on
// current state. Keeps the toolbar small while preserving both actions.
function toggleCollapseAll() {
  if (state.collapsed.size > 0) {
    state.collapsed.clear();
  } else {
    for (const n of Object.values(state.graph.nodes)) {
      if (n.id !== 'root' && (n.children || []).length > 0) state.collapsed.add(n.id);
    }
  }
  render();
}

function toggleCollapse(id, ev) {
  if (ev) { ev.stopPropagation(); }
  if (state.collapsed.has(id)) state.collapsed.delete(id);
  else state.collapsed.add(id);
  render();
}

// ─── Render: Unified timeline ────────────────────────────
const TIMELINE = {
  rowHeight: 108,
  // Card height. `rowHeight - barHeight` is the vertical breathing room,
  // half above and half below — the lower gap also doubles as the slot
  // for the hover-revealed action chips (retry / spawn).
  barHeight: 52,
  // Pixels per depth column. Sized so a spine bar at full width still has
  // space for the L-connector segment before the next column starts.
  colWidth: 500,
  // Spine bars carry the full hypothesis text — they're the narrative
  // thread of the search and earn the real estate. Off-spine bars collapse
  // to dot + id + score so 30+ siblings don't drown the user in prose.
  cardWidth: 430,
  cardWidthCompact: 170,
  leftPad: 35,
  topPad: 10,
};

function visibleScoreRange(rows) {
  let min = Infinity, max = -Infinity;
  for (const r of rows) {
    if (r.node.id === 'root') continue;
    if (r.node.score == null) continue;
    min = Math.min(min, r.node.score);
    max = Math.max(max, r.node.score);
  }
  if (!isFinite(min)) return { min: 0, max: 1 };
  return { min, max };
}

function renderTimeline(opts) {
  // opts: { containerId, rowsHostId, axisHostId, scope, allowInteraction }
  const containerId = (opts && opts.containerId) || 'timeline-section';
  const rowsHostId = (opts && opts.rowsHostId) || 'timeline-rows';
  const axisHostId = (opts && opts.axisHostId) || 'timeline-axis';
  const allowInteraction = !opts || opts.allowInteraction !== false;
  const explicitScope = opts && opts.scope;

  const prevScope = state.scopeRoot;
  if (explicitScope) state.scopeRoot = explicitScope;

  const { rows, maxRowFull, maxDepthFull } = buildVisibleRows();

  if (explicitScope) state.scopeRoot = prevScope;

  const rowsHost = document.getElementById(rowsHostId);
  const axisHost = document.getElementById(axisHostId);
  if (!rowsHost) return;

  // Persist scroll on main timeline
  if (containerId === 'timeline-section') {
    state.timelineScroll = {
      top: rowsHost.scrollTop || 0,
      left: rowsHost.scrollLeft || 0,
    };
  }

  // Inner dimensions come from the FULL topology (every node we
  // positioned, including those hidden by collapse). Measuring from the
  // emitted rows alone would shrink the inner box on collapse, clamping
  // scrollTop and re-computing the zoom floor — visually yanking the
  // view. Using the full layout keeps the scrollable area stable.
  const maxRow = maxRowFull;
  const maxDepth = maxDepthFull;

  const scoreRange = visibleScoreRange(rows);
  const totalWidth = TIMELINE.leftPad + (maxDepth + 1) * TIMELINE.colWidth + 40;
  const totalHeight = TIMELINE.topPad + (maxRow + 1) * TIMELINE.rowHeight + 16;

  // Depth labels were visually redundant once the tree structure became clear.
  if (axisHost) {
    axisHost.style.display = 'none';
    axisHost.innerHTML = '';
  }

  // The best-path spine is highlighted in all modes (not just lineage view) —
  // the amber tint marks "this is the current best path." Muting of off-spine
  // bars only happens in lineage mode where the user has asked to focus.
  const spineSet = lineageSet(bestExperimentId());
  const muteOffSpine = state.viewMode === 'lineage';

  // Geometry per entry: y comes from rowIndex (not array order), x from
  // depth. Off-spine bars use a narrower compact width since they no
  // longer render the hypothesis prose.
  function rowGeom(entry) {
    const top = TIMELINE.topPad + entry.rowIndex * TIMELINE.rowHeight;
    const y = top + TIMELINE.rowHeight / 2;
    const barX = TIMELINE.leftPad + entry.depth * TIMELINE.colWidth;
    let barW;
    if (entry.node.id === 'root') barW = 60;
    else if (spineSet.has(entry.node.id)) barW = TIMELINE.cardWidth;
    else barW = TIMELINE.cardWidthCompact;
    return { top, y, barX, barW, barRight: barX + barW };
  }

  const rowById = new Map();
  rows.forEach((r) => rowById.set(r.node.id, r));

  // Connectors (parent right edge → child left edge). In the tidy-tree
  // layout, parent and primary child share a row, so the connector is a
  // simple horizontal segment; off-spine children sit lower and get an
  // L-shape bend.
  const connectorSegments = [];
  for (const r of rows) {
    if (r.node.id === 'root') continue;
    const parent = rowById.get(r.node.parent);
    if (!parent) continue;
    const pg = rowGeom(parent);
    const sg = rowGeom(r);
    const sx = pg.barRight;
    const sy = pg.y;
    const tx = sg.barX;
    const ty = sg.y;
    const isLineage = spineSet.has(r.node.id) && spineSet.has(r.node.parent);
    const muted = muteOffSpine && !isLineage;
    const cls = ['timeline-connector'];
    if (isLineage) cls.push('lineage');
    if (muted) cls.push('muted');
    // Cubic bezier instead of an L-shape. Two reasons:
    //   - L-shapes from multiple parents in the same column share the
    //     midpoint X for their vertical segments, so they pile onto a
    //     single line and the eye loses which connector goes where.
    //   - A smooth S-curve fans out at the source and converges at the
    //     target — even when many parents send connectors into the next
    //     column, the bends sit at distinct Y positions instead of
    //     overlapping. Same-row parent/child degenerates to a flat
    //     horizontal curve, identical to a straight line.
    const dx = tx - sx;
    const c1x = sx + dx * 0.5;
    const c2x = sx + dx * 0.5;
    const d = `M${sx},${sy} C${c1x.toFixed(1)},${sy} ${c2x.toFixed(1)},${ty} ${tx},${ty}`;
    connectorSegments.push(`<path class="${cls.join(' ')}" d="${d}"/>`);
  }

  // Each entry renders independently (no full-width row wrapper) so multiple
  // entries sharing a rowIndex but at different depths never compete for
  // hover/click.
  const rowsHtml = rows.map((r) => {
    const g = rowGeom(r);
    const isSelected = state.selectedNode === r.node.id;
    const inLineage = spineSet.has(r.node.id);
    const muted = muteOffSpine && !inLineage;

    const barCls = ['exp-bar'];
    if (r.node.id === 'root') barCls.push('root-bar');
    else barCls.push(r.node.status || '');
    if (isPrunedKept(r.node)) barCls.push('pruned-kept');
    if (r.node.gate_result === false) barCls.push('gate-failed');
    if (isSelected) barCls.push('selected');
    if (inLineage) barCls.push('lineage');
    if (muted) barCls.push('muted');
    // Off-spine bars are compact (dot + id + score). Hover shows the
    // hypothesis + delta tooltip; click opens the drawer for full detail.
    if (!inLineage && r.node.id !== 'root') barCls.push('compact');

    const clickHandler = allowInteraction && r.node.id !== 'root'
      ? `onclick="onTimelineRowClick(event, '${r.node.id}')"`
      : '';

    let barInner;
    if (r.node.id === 'root') {
      // Root has no status — render a muted ring as a placeholder so the
      // leading dot stays aligned across rows.
      barInner = `<span class="exp-status pruned"></span><span class="exp-id">root</span><span class="exp-hyp">baseline</span>`;
    } else {
      const delta = scoreDelta(r.node);
      const deltaColor = deltaColorFor(delta);
      const hyp = r.node.hypothesis || (r.node.status === 'failed' ? (r.node.error || 'failed') : '(no hypothesis)');
      const scoreText = r.node.score != null ? r.node.score.toFixed(2) : (r.node.status === 'failed' ? 'err' : '—');
      const dotCls = resultStatus(r.node);
      barInner = `<span class="exp-status ${dotCls}"></span>
        <span class="exp-id">${esc(shortId(r.node.id))}</span>
        <span class="exp-hyp">${esc(hyp)}</span>
        <span class="exp-score">${scoreText}</span>
        ${delta ? `<span class="exp-delta" style="color:${deltaColor}">${delta}</span>` : ''}
        ${isPrunedKept(r.node) ? `<span class="exp-pruned-badge" title="Committed result preserved; removed from frontier branching.">pruned</span>` : ''}
        ${r.node.gate_result === false ? `<span class="exp-gate-fail" title="accuracy gate failed">gate ✗</span>` : ''}`;
    }
    // Compact (off-spine) bars get a hover tooltip carrying the full
    // hypothesis + delta so the user can peek without opening the drawer.
    // Spine bars already render that info inline, so no tooltip needed.
    let hoverAttr = '';
    if (r.node.id !== 'root' && !inLineage) {
      const hyp = r.node.hypothesis || (r.node.status === 'failed' ? (r.node.error || 'failed') : '(no hypothesis)');
      const delta = scoreDelta(r.node);
      const hoverPayload = delta ? `${hyp}\n\n${delta} from ${shortId(r.node.parent)}` : hyp;
      hoverAttr = ` data-hover="${esc(hoverPayload)}"`;
    }

    const barTop = g.top + (TIMELINE.rowHeight - TIMELINE.barHeight) / 2;
    const bar = `<div class="${barCls.join(' ')}" style="left:${g.barX}px;top:${barTop}px;width:${g.barW}px;height:${TIMELINE.barHeight}px"${hoverAttr} ${clickHandler}>
      ${barInner}
    </div>`;

    // Branch toggle sits to the right of the card so the node label keeps a
    // clean left edge. Collapsed summaries render after the toggle. When
    // the parent is on the spine, the toggle gets the amber variant so the
    // best-path chain reads continuously through its branch handles.
    // Caret sits centered on the bar's right edge so the connector line
    // visually "passes through" it on the way to children.
    let caret = '';
    if (r.hasChildren && r.node.id !== 'root') {
      const ch = r.collapsed ? '+' : '-';
      const caretTop = g.top + (TIMELINE.rowHeight - 16) / 2;
      const caretCls = inLineage ? 'exp-caret lineage' : 'exp-caret';
      caret = `<span class="${caretCls}" style="left:${g.barRight - 8}px;top:${caretTop}px" onclick="toggleCollapse('${esc(r.node.id)}', event)" title="${r.collapsed ? 'Expand branch' : 'Collapse branch'}">${ch}</span>`;
    }

    // Hover action chips: retry (icon) + spawn (+). Anchored to the bar's
    // bottom edge with internal padding for the visible gap, so the hover
    // area is continuous bar → chips (no flicker as the cursor crosses).
    // Must be the immediate next sibling of .exp-bar so the CSS scopes
    // hover to this one node, not all of them.
    let actionChips = '';
    if (r.node.id !== 'root' && allowInteraction) {
      const actionsTop = barTop + TIMELINE.barHeight;
      // Anchor at the bar's horizontal center; CSS translateX(-50%) on
      // .exp-bar-actions centers the chip group regardless of its width.
      const actionsLeft = g.barX + g.barW / 2;
      const retryIcon = `<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>`;
      actionChips = `<div class="exp-bar-actions" style="left:${actionsLeft}px;top:${actionsTop}px">
        <button class="exp-action exp-action-icon" onclick="retryNode('${esc(r.node.id)}', event)" title="Run a new attempt of this experiment" aria-label="Retry">${retryIcon}</button>
        <button class="exp-action" onclick="spawnFromNode('${esc(r.node.id)}', event)" title="Start a new experiment from this one" aria-label="Spawn">+</button>
      </div>`;
    }

    let summary = '';
    if (r.collapsed && r.hiddenChildCount > 0) {
      const bx = g.barRight + 24;
      const best = r.bestChildScore != null ? `, best ${r.bestChildScore.toFixed(2)}` : '';
      summary = `<div class="exp-collapsed-summary" style="left:${bx}px;top:${g.top + (TIMELINE.rowHeight - 22) / 2}px" onclick="toggleCollapse('${esc(r.node.id)}', event)">+${r.hiddenChildCount} child${r.hiddenChildCount > 1 ? 'ren' : ''}${best}</div>`;
    }

    // actionChips MUST come immediately after bar in DOM so the CSS
    // `.exp-bar:hover + .exp-bar-actions` scopes hover to this node only.
    return bar + actionChips + caret + summary;
  }).join('');

  // Soft amber lane behind the spine row. The spine reliably collapses onto
  // rowIndex 0 in 'all' and 'lineage' modes; 'frontier' mode filters out
  // non-frontier nodes, so row 0 is not the spine — skip there.
  let spineLane = '';
  if (state.viewMode !== 'frontier') {
    const anchor = rowById.get('root') || rowById.get(bestExperimentId());
    if (anchor) {
      const laneTop = TIMELINE.topPad + anchor.rowIndex * TIMELINE.rowHeight + 2;
      const laneH = TIMELINE.rowHeight - 4;
      spineLane = `<div class="timeline-spine-lane" style="top:${laneTop}px;height:${laneH}px"></div>`;
    }
  }

  // Any open hover tip from the previous render is now anchored to a
  // soon-to-be-destroyed DOM element — drop it so we don't leave a
  // dangling popover behind.
  hideTip();
  rowsHost.innerHTML =
    `<div class="timeline-rows-inner" style="width:${totalWidth}px;height:${totalHeight}px">
      ${spineLane}
      <svg class="timeline-connectors" width="${totalWidth}" height="${totalHeight}">${connectorSegments.join('')}</svg>
      ${rowsHtml}
    </div>`;

  // Empty state
  const empty = document.getElementById('timeline-empty');
  if (empty && containerId === 'timeline-section') {
    if (rows.length <= 1) empty.classList.remove('hidden');
    else empty.classList.add('hidden');
  }

  // Restore scroll
  if (containerId === 'timeline-section') {
    rowsHost.scrollTop = state.timelineScroll.top;
    rowsHost.scrollLeft = state.timelineScroll.left;
    rowsHost.onscroll = () => {
      state.timelineScroll = { top: rowsHost.scrollTop, left: rowsHost.scrollLeft };
      if (axisHost) axisHost.scrollLeft = rowsHost.scrollLeft;
    };
    applyTimelineZoom(rowsHost);
    bindTimelineZoomHandler(rowsHost);
    bindTimelineDragPan(rowsHost);
    bindTimelineDoubleClickReset(rowsHost);
    bindBarHoverTips(rowsHost);
  }
}

// Hover tooltip for scatter dots — same tip-popover as the compact bars.
// Bound once on the scatter host via delegation so subsequent renders'
// new SVG elements are handled automatically.
function bindScatterHoverTips(host) {
  if (state._scatterTipsBound) return;
  state._scatterTipsBound = true;
  host.addEventListener('mouseover', (ev) => {
    const dot = ev.target.closest('.scatter-dot');
    if (!dot) return;
    if (ev.relatedTarget && dot.contains(ev.relatedTarget)) return;
    const text = dot.getAttribute('data-hover');
    if (!text) return;
    dot.dataset.tipFor = dot.dataset.tipFor || `scatter-tip-${Math.random().toString(36).slice(2, 8)}`;
    showTip(dot, text);
  });
  host.addEventListener('mouseout', (ev) => {
    const dot = ev.target.closest('.scatter-dot');
    if (!dot) return;
    if (ev.relatedTarget && dot.contains(ev.relatedTarget)) return;
    hideTip();
  });
}

// Hover tooltip for compact (off-spine) bars. The tip-popover infra is
// shared with the settings modal — here we wire it to mouseover/out of
// .exp-bar.compact via event delegation on the rowsHost. Single listener,
// works for all bars including those added on later renders.
function bindBarHoverTips(rowsHost) {
  if (state._barHoverTipsBound) return;
  state._barHoverTipsBound = true;
  rowsHost.addEventListener('mouseover', (ev) => {
    const bar = ev.target.closest('.exp-bar.compact');
    if (!bar) return;
    // mouseover bubbles from child elements; ignore re-entries within the
    // same bar so the tip doesn't flicker.
    if (ev.relatedTarget && bar.contains(ev.relatedTarget)) return;
    const text = bar.dataset.hover;
    if (!text) return;
    bar.dataset.tipFor = bar.dataset.tipFor || `bar-tip-${Math.random().toString(36).slice(2, 8)}`;
    showTip(bar, text);
  });
  rowsHost.addEventListener('mouseout', (ev) => {
    const bar = ev.target.closest('.exp-bar.compact');
    if (!bar) return;
    // mouseout fires when leaving for a child; only hide when the cursor
    // has actually left the whole bar.
    if (ev.relatedTarget && bar.contains(ev.relatedTarget)) return;
    hideTip();
  });
}

// ─── Local pinch/Cmd-wheel zoom + drag-pan on the timeline canvas ──────
//
// Without this, Cmd+wheel and trackpad pinch zoom the entire page (browser
// default). Trapping the gesture here scales only the timeline interior so
// the user can magnify the tree without scaling the chrome around it.
const ZOOM_MAX = 2;

// Scroll the timeline so the currently-selected bar is centered in the
// viewport. Called after selection changes (rail card click, histogram bar
// click, timeline bar click). If the user is zoomed out below 1× (fit-all
// mode), we first snap zoom up to 1 so the centered bar is readable — at
// fit-all there's no point "focusing" because nothing's clipped to begin
// with. Layout under CSS `zoom` settles synchronously in modern engines,
// but we still defer the scroll to the next frame so the new zoom is
// committed before we measure.
function focusSelectedInTimeline({ smooth = true } = {}) {
  if (!state.selectedNode) return;
  if (state.timelineZoom < 1) {
    state.timelineZoom = 1;
    applyTimelineZoom();
  }
  requestAnimationFrame(() => {
    const rowsHost = document.getElementById('timeline-rows');
    if (!rowsHost) return;
    const sel = rowsHost.querySelector('.exp-bar.selected');
    if (!sel) return;
    const hostRect = rowsHost.getBoundingClientRect();
    const selRect = sel.getBoundingClientRect();
    const dx = (selRect.left + selRect.width / 2) - (hostRect.left + hostRect.width / 2);
    const dy = (selRect.top + selRect.height / 2) - (hostRect.top + hostRect.height / 2);
    if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return;  // already centered
    rowsHost.scrollBy({ left: dx, top: dy, behavior: smooth ? 'smooth' : 'auto' });
  });
}

// Lower bound: the zoom level at which the entire tree fits in the viewport.
// We never let the user zoom below this — there's no value in seeing the
// content surrounded by empty canvas. If content already fits at 1×, the
// floor stays at 1 so the unzoomed state remains the natural default.
//
// We read the logical size from the inline width/height on .timeline-rows-inner
// (set by renderTimeline) rather than offsetWidth — offsetWidth's behavior
// under CSS `zoom` varies by browser and is the cause of the previous bug
// where the floor was looser than fit-all for wide trees.
function computeMinZoom(rowsHost) {
  const inner = rowsHost.querySelector('.timeline-rows-inner');
  if (!inner) return 1;
  const baseW = parseFloat(inner.style.width) || inner.offsetWidth;
  const baseH = parseFloat(inner.style.height) || inner.offsetHeight;
  const visibleW = rowsHost.clientWidth;
  const visibleH = rowsHost.clientHeight;
  if (baseW <= 0 || baseH <= 0) return 1;
  const fit = Math.min(visibleW / baseW, visibleH / baseH);
  return Math.min(1, fit);
}

function applyTimelineZoom(rowsHost) {
  const host = rowsHost || document.getElementById('timeline-rows');
  if (!host) return;
  const inner = host.querySelector('.timeline-rows-inner');
  if (!inner) return;
  // First write the current zoom so offsetWidth reflects today's scale,
  // then recompute the floor in case the viewport or content size changed
  // (window resize, render with more rows, etc.) and snap up if needed.
  inner.style.zoom = state.timelineZoom;
  const floor = computeMinZoom(host);
  if (state.timelineZoom < floor) {
    state.timelineZoom = floor;
    inner.style.zoom = state.timelineZoom;
  }
  // Publish 1 / zoom so descendant elements that want to stay screen-
  // constant (e.g., .exp-caret) can counter-scale via transform.
  inner.style.setProperty('--inv-zoom', 1 / state.timelineZoom);
}

function bindTimelineZoomHandler(rowsHost) {
  if (state._zoomWheelBound) return;
  state._zoomWheelBound = true;
  rowsHost.addEventListener('wheel', (ev) => {
    if (!(ev.ctrlKey || ev.metaKey)) return;  // plain wheel keeps panning
    ev.preventDefault();
    const step = ev.deltaY < 0 ? 0.1 : -0.1;
    const floor = computeMinZoom(rowsHost);
    const oldZoom = state.timelineZoom;
    const next = Math.max(floor, Math.min(ZOOM_MAX, oldZoom + step));
    if (Math.abs(next - oldZoom) < 1e-4) return;
    // Cursor-anchored zoom: keep the content point under the pointer
    // pinned to that screen position as the scale changes.
    //
    // Math: cursor sees rendered coord (scroll + c). That same content
    // point sits at rendered coord (scroll + c) * (next / old) after the
    // scale change, so the new scroll must be that minus c again.
    //
    // We must capture scrollLeft/Top BEFORE applying the new zoom. On
    // zoom-out the rendered size shrinks and the browser auto-clamps
    // scrollLeft to the new max during the layout pass — reading it after
    // would feed the already-clamped value into the formula and drag the
    // anchor toward the origin.
    const rect = rowsHost.getBoundingClientRect();
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    const oldScrollLeft = rowsHost.scrollLeft;
    const oldScrollTop = rowsHost.scrollTop;
    const factor = next / oldZoom;
    state.timelineZoom = next;
    applyTimelineZoom(rowsHost);
    rowsHost.scrollLeft = (oldScrollLeft + cx) * factor - cx;
    rowsHost.scrollTop = (oldScrollTop + cy) * factor - cy;
  }, { passive: false });
}

// Click-and-drag panning on the timeline background. The browser's
// overflow:auto already clamps scroll to [0, scrollWidth - clientWidth], so
// we don't need extra edge-clamping — scroll naturally stops at the edges.
// Movement under DRAG_THRESHOLD is treated as a click so bar/caret clicks
// still register normally.
const DRAG_THRESHOLD = 4;
function bindTimelineDragPan(rowsHost) {
  if (state._dragPanBound) return;
  state._dragPanBound = true;

  let isDown = false;
  let didDrag = false;
  let startX = 0, startY = 0;
  let startScrollLeft = 0, startScrollTop = 0;

  rowsHost.addEventListener('mousedown', (ev) => {
    if (ev.button !== 0) return;  // left button only
    // Don't hijack drags that started on an interactive element.
    if (ev.target.closest('.exp-bar, .exp-caret, .exp-collapsed-summary, .timeline-spine-lane')) return;
    isDown = true;
    didDrag = false;
    startX = ev.pageX;
    startY = ev.pageY;
    startScrollLeft = rowsHost.scrollLeft;
    startScrollTop = rowsHost.scrollTop;
  });

  // Listen on window so a fast drag that leaves the canvas still tracks.
  window.addEventListener('mousemove', (ev) => {
    if (!isDown) return;
    const dx = ev.pageX - startX;
    const dy = ev.pageY - startY;
    if (!didDrag && Math.hypot(dx, dy) > DRAG_THRESHOLD) {
      didDrag = true;
      rowsHost.style.cursor = 'grabbing';
      rowsHost.style.userSelect = 'none';
    }
    if (didDrag) {
      ev.preventDefault();
      rowsHost.scrollLeft = startScrollLeft - dx;
      rowsHost.scrollTop = startScrollTop - dy;
    }
  });

  window.addEventListener('mouseup', () => {
    if (!isDown) return;
    isDown = false;
    rowsHost.style.cursor = '';
    rowsHost.style.userSelect = '';
  });
}

// Double-click anywhere on the canvas background snaps zoom back to 1×.
// We anchor the snap at the cursor so the content under the pointer stays
// roughly in place. Double-clicks on bars / carets / collapsed-summary
// chips are ignored — those have their own meaning.
// ─── Scatter strip: vertical resize ─────────────────────────────
// The strip is docked at the bottom of the center column (flex item, no
// absolute positioning). The user can drag the top edge up/down to grow
// or shrink the chart; the timeline above flexes to absorb the change.
// Height persists to localStorage so it stays where the user last set it.
const SCATTER_STRIP_MIN_H = 100;
const SCATTER_STRIP_MAX_H = 600;
const SCATTER_STORAGE_KEY = 'evo:scatter-strip:height';
const SCATTER_STATE_KEY = 'evo:scatter-strip:state';   // collapsed | normal | maximized

function loadScatterHeight() {
  try {
    const raw = localStorage.getItem(SCATTER_STORAGE_KEY);
    if (raw == null) return null;
    const n = parseFloat(raw);
    return isFinite(n) ? n : null;
  } catch { return null; }
}
function saveScatterHeight(h) {
  try { localStorage.setItem(SCATTER_STORAGE_KEY, String(h)); } catch {}
}
// The strip has three sizes: 'collapsed' (a bar at the bottom), 'normal' (the
// docked, resizable chart), and 'maximized' (fills the center column).
function loadScatterState() {
  try {
    const v = localStorage.getItem(SCATTER_STATE_KEY);
    return (v === 'collapsed' || v === 'maximized') ? v : 'normal';
  } catch { return 'normal'; }
}
function saveScatterState(v) {
  try { localStorage.setItem(SCATTER_STATE_KEY, v); } catch {}
}
function applyScatterState(strip, st) {
  strip.classList.toggle('collapsed', st === 'collapsed');
  strip.classList.toggle('maximized', st === 'maximized');
  const cb = document.getElementById('scatter-collapse-btn');
  const eb = document.getElementById('scatter-expand-btn');
  // collapse is a no-op once it's already a bar; expand is a no-op once maximized
  if (cb) cb.classList.toggle('is-disabled', st === 'collapsed');
  if (eb) eb.classList.toggle('is-disabled', st === 'maximized');
}
function applyScatterRect(strip) {
  const saved = loadScatterHeight();
  if (saved != null) {
    const h = Math.max(SCATTER_STRIP_MIN_H, Math.min(SCATTER_STRIP_MAX_H, saved));
    strip.style.height = `${h}px`;
  }
  applyScatterState(strip, loadScatterState());
}

function bindScatterResize() {
  if (state._scatterResizeBound) return;
  const handle = document.getElementById('scatter-resize-top');
  const strip = document.getElementById('scatter-strip');
  if (!handle || !strip) return;
  state._scatterResizeBound = true;
  applyScatterRect(strip);

  // Collapse goes straight to the bottom bar. Expand steps up: a collapsed
  // strip opens to its normal docked size; a normal strip maximizes to fill
  // the column. These two buttons are the only size controls.
  const collapseBtn = document.getElementById('scatter-collapse-btn');
  const expandBtn = document.getElementById('scatter-expand-btn');
  if (collapseBtn) {
    collapseBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      saveScatterState('collapsed');
      applyScatterState(strip, 'collapsed');
    });
  }
  if (expandBtn) {
    expandBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const next = loadScatterState() === 'collapsed' ? 'normal' : 'maximized';
      saveScatterState(next);
      applyScatterState(strip, next);
      renderScatter();
    });
  }

  let dragging = false;
  let startY = 0;
  let startH = 0;
  let renderQueued = false;
  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => { renderQueued = false; renderScatter(); });
  }

  handle.addEventListener('mousedown', (ev) => {
    if (ev.button !== 0) return;
    dragging = true;
    startY = ev.clientY;
    startH = strip.getBoundingClientRect().height;
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'row-resize';
    ev.preventDefault();
  });
  window.addEventListener('mousemove', (ev) => {
    if (!dragging) return;
    // Drag UP (negative dy) grows the strip; drag DOWN shrinks it. The
    // top edge of the strip is what the user grabbed, so as the mouse
    // moves up the strip's top moves up = height increases.
    const dy = ev.clientY - startY;
    const h = Math.max(SCATTER_STRIP_MIN_H, Math.min(SCATTER_STRIP_MAX_H, startH - dy));
    strip.style.height = `${h}px`;
    scheduleRender();
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.userSelect = '';
    document.body.style.cursor = '';
    saveScatterHeight(strip.getBoundingClientRect().height);
  });
}

function bindTimelineDoubleClickReset(rowsHost) {
  if (state._dblResetBound) return;
  state._dblResetBound = true;
  rowsHost.addEventListener('dblclick', (ev) => {
    if (ev.target.closest('.exp-bar, .exp-caret, .exp-collapsed-summary, .timeline-spine-lane')) return;
    const oldZoom = state.timelineZoom;
    if (Math.abs(oldZoom - 1) < 1e-4) return;
    const rect = rowsHost.getBoundingClientRect();
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    const oldScrollLeft = rowsHost.scrollLeft;
    const oldScrollTop = rowsHost.scrollTop;
    const factor = 1 / oldZoom;
    state.timelineZoom = 1;
    applyTimelineZoom(rowsHost);
    rowsHost.scrollLeft = (oldScrollLeft + cx) * factor - cx;
    rowsHost.scrollTop = (oldScrollTop + cy) * factor - cy;
  });
}

function onTimelineRowClick(ev, id) {
  if (id === 'root') {
    if (ev.shiftKey) scopeTo('root');
    return;
  }
  if (ev.shiftKey) {
    scopeTo(id);
    return;
  }
  openDrawer(id);
}

// Drawer children list — a flat sortable view of immediate children
// (replaces the cramped mini-timeline). Sorted by score descending so
// the user can read "did anything beat parent?" top-down. Spine child
// gets a marker. Click a row to navigate the drawer to that child.
function renderChildrenSection(node) {
  const kids = (node.children || [])
    .map((id) => state.graph.nodes[id])
    .filter(Boolean);
  if (!kids.length) {
    return `<div class="drawer-section">
      <span class="drawer-section-title">Children</span>
      <div class="sidebar-empty">No child experiments yet.</div>
    </div>`;
  }
  const isMax = (state.stats.metric || 'max') === 'max';
  const spine = lineageSet(bestExperimentId());
  const sorted = [...kids].sort((a, b) => {
    const sa = a.score == null ? -Infinity : a.score;
    const sb = b.score == null ? -Infinity : b.score;
    return isMax ? sb - sa : sa - sb;
  });
  const rows = sorted.map((c) => {
    const dot = `${resultStatus(c)}${isPrunedKept(c) ? ' pruned-kept' : ''}`;
    const scoreText = c.score != null ? c.score.toFixed(2)
                    : c.status === 'failed' ? 'err'
                    : c.status === 'active' ? 'run' : '—';
    const delta = scoreDelta(c);
    const deltaColor = deltaColorFor(delta);
    const hyp = c.hypothesis || (c.status === 'failed' ? (c.error || 'failed') : '');
    return `<div class="drawer-child ${isPrunedKept(c) ? 'pruned-kept' : ''}" onclick="openDrawer('${esc(c.id)}')" title="${esc(statusLabelForNode(c) + ': ' + (c.hypothesis || c.id))}">
      <span class="drawer-child-dot ${dot}"></span>
      <span class="drawer-child-id">${esc(shortId(c.id))}</span>
      <span class="drawer-child-score">${scoreText}</span>
      ${delta ? `<span class="drawer-child-delta" style="color:${deltaColor}">${delta}</span>` : '<span class="drawer-child-delta"></span>'}
      <span class="drawer-child-hyp">${esc(hyp)}</span>
    </div>`;
  }).join('');
  return `<div class="drawer-section">
    <span class="drawer-section-title">Children (${kids.length})</span>
    <div class="drawer-children">${rows}</div>
  </div>`;
}

// ─── Sidebar (detail panel) ──────────────────────────────
// opts.fromHistory: skip pushing to drawerHistory (the back/forward
// buttons already moved the index themselves).
// ─── Diff rendering (GitHub-style: per-file, split or unified) ───────────

function setDiffView(mode) {
  state.diffView = mode === 'unified' ? 'unified' : 'split';
  try { localStorage.setItem('evo.diffView', state.diffView); } catch (_) { /* ignore */ }
  if (state.selectedNode) openDrawer(state.selectedNode, { fromHistory: true });
}

// File extension → highlight.js language id. Unknown extensions skip
// highlighting (rendered as plain escaped text).
const EXT_LANG = {
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
  ts: 'typescript', tsx: 'typescript',
  py: 'python', rb: 'ruby', go: 'go', rs: 'rust', java: 'java',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp',
  cs: 'csharp', php: 'php', swift: 'swift', kt: 'kotlin', scala: 'scala',
  sh: 'bash', bash: 'bash', zsh: 'bash',
  html: 'xml', htm: 'xml', xml: 'xml', vue: 'xml', svg: 'xml',
  css: 'css', scss: 'scss', less: 'less',
  json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'ini', ini: 'ini',
  md: 'markdown', sql: 'sql', r: 'r', lua: 'lua', pl: 'perl',
  // Loaded from highlight-langs-extra.min.js:
  dockerfile: 'dockerfile', dart: 'dart', ex: 'elixir', exs: 'elixir',
  erl: 'erlang', hrl: 'erlang', hs: 'haskell',
  clj: 'clojure', cljs: 'clojure', cljc: 'clojure', edn: 'clojure',
  jl: 'julia', ps1: 'powershell', psm1: 'powershell',
  groovy: 'groovy', gradle: 'groovy', cmake: 'cmake', proto: 'protobuf',
  nim: 'nim', ml: 'ocaml', mli: 'ocaml', fs: 'fsharp', fsx: 'fsharp', fsi: 'fsharp',
  // Not vendored — fetched on demand via ensureLanguage() the first time a
  // file of this type appears (graceful no-op if the grammar isn't published).
  d: 'd', cr: 'crystal', nix: 'nix', vala: 'vala', tcl: 'tcl',
  f90: 'fortran', f95: 'fortran', f03: 'fortran', for: 'fortran',
  pas: 'delphi', dpr: 'delphi', vhd: 'vhdl', vhdl: 'vhdl', sv: 'verilog', svh: 'verilog',
  scm: 'scheme', ss: 'scheme', lisp: 'lisp', cl: 'lisp', el: 'lisp',
  adb: 'ada', ads: 'ada', tex: 'latex', sty: 'latex',
  awk: 'awk', vim: 'vim', coffee: 'coffeescript', styl: 'stylus',
  pug: 'pug', jade: 'pug', hbs: 'handlebars', twig: 'twig',
  properties: 'properties', pgsql: 'pgsql', asm: 'x86asm',
  nginx: 'nginx', prolog: 'prolog', hx: 'haxe', elm: 'elm', purs: 'purescript',
};

// Some languages key off the filename, not an extension.
const NAME_LANG = {
  dockerfile: 'dockerfile', 'cmakelists.txt': 'cmake',
};

function langForPath(path) {
  if (!path) return null;
  const base = path.split('/').pop();
  const lower = base.toLowerCase();
  if (NAME_LANG[lower]) return NAME_LANG[lower];
  const ext = base.includes('.') ? base.split('.').pop().toLowerCase() : '';
  return EXT_LANG[ext] || null;
}

// Highlight one line of code, returning HTML. Falls back to escaped plain
// text when highlight.js is unavailable or the language is unknown.
// (Per-line highlighting — multi-line tokens like block comments may render
// imperfectly, same trade-off most diff viewers make.)
function highlightCode(code, lang) {
  if (!code) return '';
  if (window.hljs && lang && window.hljs.getLanguage(lang)) {
    try { return window.hljs.highlight(code, { language: lang, ignoreIllegals: true }).value; }
    catch (_) { /* fall through to plain */ }
  }
  return esc(code);
}

// Grammars not in the vendored bundles are fetched on demand from the
// highlight.js CDN (pinned to the core's version). Common languages stay
// offline; only the long tail needs the network, and a failed/blocked fetch
// degrades to plain text. State: 'loading' | 'done' | 'failed'.
const HLJS_VERSION = '11.9.0';
const HLJS_LANG_BASE = `https://cdnjs.cloudflare.com/ajax/libs/highlight.js/${HLJS_VERSION}/languages/`;
const _grammarState = {};

function ensureLanguage(lang, onReady) {
  if (!window.hljs || !lang || window.hljs.getLanguage(lang)) return;
  if (_grammarState[lang]) return; // loading / done / failed — don't refetch
  _grammarState[lang] = 'loading';
  const s = document.createElement('script');
  s.src = HLJS_LANG_BASE + lang + '.min.js';
  s.async = true;
  s.onload = () => {
    _grammarState[lang] = window.hljs.getLanguage(lang) ? 'done' : 'failed';
    if (_grammarState[lang] === 'done' && onReady) onReady();
  };
  s.onerror = () => { _grammarState[lang] = 'failed'; };
  document.head.appendChild(s);
}

// Coalesce re-renders when several grammars finish loading at once.
let _diffRerenderTimer = null;
function scheduleDiffRerender() {
  if (state.sidebarTab !== 'diff' || !state.selectedNode) return;
  clearTimeout(_diffRerenderTimer);
  _diffRerenderTimer = setTimeout(() => {
    if (state.sidebarTab === 'diff' && state.selectedNode) {
      openDrawer(state.selectedNode, { fromHistory: true });
    }
  }, 80);
}

// Parse a unified git diff into per-file structures.
function parseUnifiedDiff(text) {
  const files = [];
  let file = null, hunk = null;
  for (const line of text.split('\n')) {
    if (line.startsWith('diff --git')) {
      const m = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
      file = { path: m ? m[2] : '', oldPath: m ? m[1] : '', newPath: m ? m[2] : '', hunks: [], adds: 0, dels: 0 };
      files.push(file);
      hunk = null;
      continue;
    }
    if (!file) continue;
    if (line.startsWith('--- ')) { file.oldPath = line.slice(4).replace(/^a\//, ''); continue; }
    if (line.startsWith('+++ ')) {
      const p = line.slice(4).replace(/^b\//, '');
      file.newPath = p;
      if (p && p !== '/dev/null') file.path = p;
      continue;
    }
    if (line.startsWith('@@')) {
      const m = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      hunk = { header: line, oldNo: m ? +m[1] : 0, newNo: m ? +m[2] : 0, lines: [] };
      file.hunks.push(hunk);
      continue;
    }
    if (hunk) {
      if (line.startsWith('+')) file.adds++;
      else if (line.startsWith('-')) file.dels++;
      hunk.lines.push(line);
    }
  }
  return files;
}

// Pair deletions with additions into side-by-side rows, tracking line numbers.
function hunkSplitRows(hunk) {
  const rows = [];
  let oldNo = hunk.oldNo, newNo = hunk.newNo;
  const ls = hunk.lines;
  let i = 0;
  while (i < ls.length) {
    const tag = ls[i][0];
    if (tag === '\\') { i++; continue; } // "\ No newline at end of file"
    if (tag === '+' || tag === '-') {
      const dels = [], adds = [];
      while (i < ls.length && ls[i][0] === '-') { dels.push(ls[i].slice(1)); i++; }
      while (i < ls.length && ls[i][0] === '+') { adds.push(ls[i].slice(1)); i++; }
      const n = Math.max(dels.length, adds.length);
      for (let k = 0; k < n; k++) {
        const hasL = k < dels.length, hasR = k < adds.length;
        rows.push({
          leftNo: hasL ? oldNo++ : null, left: hasL ? dels[k] : '', leftType: hasL ? 'del' : 'empty',
          rightNo: hasR ? newNo++ : null, right: hasR ? adds[k] : '', rightType: hasR ? 'add' : 'empty',
        });
      }
    } else {
      const txt = ls[i].slice(1);
      rows.push({ leftNo: oldNo++, left: txt, leftType: 'ctx', rightNo: newNo++, right: txt, rightType: 'ctx' });
      i++;
    }
  }
  return rows;
}

function hunkUnifiedRows(hunk) {
  const rows = [];
  let oldNo = hunk.oldNo, newNo = hunk.newNo;
  for (const l of hunk.lines) {
    const tag = l[0], code = l.slice(1);
    if (tag === '\\') continue;
    if (tag === '+') rows.push({ type: 'add', sign: '+', oldNo: '', newNo: newNo++, code });
    else if (tag === '-') rows.push({ type: 'del', sign: '-', oldNo: oldNo++, newNo: '', code });
    else rows.push({ type: 'ctx', sign: ' ', oldNo: oldNo++, newNo: newNo++, code });
  }
  return rows;
}

function renderDiffFiles(text, mode) {
  const files = parseUnifiedDiff(text);
  // Not a recognizable git diff — fall back to the old single-block view.
  if (!files.length) {
    const raw = text.split('\n').map((line) => {
      if (line.startsWith('@@')) return `<span class="diff-hunk">${esc(line)}</span>`;
      if (line.startsWith('+')) return `<span class="diff-add">${esc(line)}</span>`;
      if (line.startsWith('-')) return `<span class="diff-del">${esc(line)}</span>`;
      return `<span class="diff-ctx">${esc(line)}</span>`;
    }).join('');
    return `<div class="diff-block">${raw}</div>`;
  }
  return files.map((f) => {
    const lang = langForPath(f.path);
    // Lazy-load the grammar if it's mapped but not yet registered; re-render
    // the diff once it arrives so highlighting applies.
    if (lang && window.hljs && !window.hljs.getLanguage(lang)) {
      ensureLanguage(lang, scheduleDiffRerender);
    }
    const stat = `<span class="diff-stat"><span class="diff-stat-add">+${f.adds}</span> <span class="diff-stat-del">−${f.dels}</span></span>`;
    const body = f.hunks.map((h) => {
      if (mode === 'split') {
        const rows = hunkSplitRows(h).map((r) => `<tr>
            <td class="diff-gutter">${r.leftNo ?? ''}</td>
            <td class="diff-cell ${r.leftType}">${highlightCode(r.left, lang)}</td>
            <td class="diff-gutter diff-gutter-r">${r.rightNo ?? ''}</td>
            <td class="diff-cell ${r.rightType}">${highlightCode(r.right, lang)}</td>
          </tr>`).join('');
        return `<div class="diff-hunk-head">${esc(h.header)}</div><table class="diff-table split"><tbody>${rows}</tbody></table>`;
      }
      const rows = hunkUnifiedRows(h).map((r) => `<tr>
          <td class="diff-gutter">${r.oldNo}</td>
          <td class="diff-gutter">${r.newNo}</td>
          <td class="diff-cell ${r.type}"><span class="diff-sign">${r.sign}</span>${highlightCode(r.code, lang)}</td>
        </tr>`).join('');
      return `<div class="diff-hunk-head">${esc(h.header)}</div><table class="diff-table unified"><tbody>${rows}</tbody></table>`;
    }).join('');
    return `<div class="diff-file">
      <div class="diff-file-head"><span class="diff-file-name">${esc(f.path || f.newPath || f.oldPath)}</span>${stat}</div>
      <div class="diff-file-body">${body}</div>
    </div>`;
  }).join('');
}

async function openDrawer(expId, opts) {
  opts = opts || {};
  const previousNode = state.selectedNode;
  if (previousNode !== expId) state.sidebarTab = 'summary';
  // History push — only on a genuine user navigation to a different node.
  if (!opts.fromHistory && previousNode !== expId) {
    // Truncate any forward entries (browser semantics).
    state.drawerHistory.length = state.drawerHistoryIndex + 1;
    state.drawerHistory.push(expId);
    state.drawerHistoryIndex = state.drawerHistory.length - 1;
  }
  state.selectedNode = expId;
  if (previousNode !== expId) state.expandedTasks.clear();
  const sidebar = document.getElementById('sidebar');
  const content = document.getElementById('sidebar-content');
  if (!sidebar || !content) return;
  sidebar.classList.remove('hidden');
  // Restore a previously dragged side-panel width (ignored in center mode,
  // where .peek-center fixes the width).
  try {
    const savedW = localStorage.getItem('evo.sidebarWidth');
    if (savedW) sidebar.style.setProperty('--evo-sidebar-w', savedW);
  } catch (_) { /* ignore */ }
  applyDetailMode();
  // Reflect selection in scatter + timeline only on a real selection change.
  // Tab switches inside the drawer re-enter openDrawer with the same id —
  // re-rendering the canvas then would reset scroll/zoom for no reason.
  if (previousNode !== expId) {
    renderScatter();
    renderTimeline();
    focusSelectedInTimeline();
  }

  const node = state.graph.nodes[expId];
  if (!node) return;

  // Guard against a faster click clobbering us: if the user selects
  // another node while our /diff or /traces fetches are in flight, the
  // late response must NOT overwrite the panel that's now showing B.
  const requestedId = expId;
  const isStale = () => state.selectedNode !== requestedId;
  const ws = state.workspace || {};

  // Pre-fetch annotations once per render so Summary (global) and Tasks
  // (per-task) can both render synchronously below. Small payload; not
  // worth caching across renders since they mutate on every commit.
  const annotations = await getAnnotationsForExperiment(expId);
  if (isStale()) return;

  const delta = scoreDelta(node);
  const deltaColor = deltaColorFor(delta);
  const statusColor = statusColorForNode(node);
  const hasChildren = (node.children || []).length > 0;
  const activeTab = ['summary', 'diff', 'tasks', 'logs'].includes(state.sidebarTab)
    ? state.sidebarTab
    : 'summary';
  // Drives the diff-tab fill layout in CSS (.sidebar[data-tab="diff"]).
  sidebar.dataset.tab = activeTab;
  const isPrunable = node.status === 'committed' || node.status === 'evaluated';
  const canBack = state.drawerHistoryIndex > 0;
  const canForward = state.drawerHistoryIndex < state.drawerHistory.length - 1;
  const navSvg = (dir) => `<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <polyline points="${dir === 'back' ? '7.5 2.5, 4 6, 7.5 9.5' : '4.5 2.5, 8 6, 4.5 9.5'}"/>
  </svg>`;
  let html = `
    <div class="drawer-header">
      <button class="drawer-nav" onclick="drawerBack()" ${canBack ? '' : 'disabled'} title="Back (previous node)">${navSvg('back')}</button>
      <button class="drawer-nav" onclick="drawerForward()" ${canForward ? '' : 'disabled'} title="Forward (next node)">${navSvg('forward')}</button>
      <span class="drawer-id">${esc(node.id)}</span>
      <span class="pill" style="background:${statusColor}15; color:${statusColor}">
        <span class="dot" style="background:${statusColor}"></span>
        ${esc(statusLabelForNode(node))}
      </span>
      ${node.id !== 'root' ? `<button class="sidebar-spawn-btn" onclick="spawnFromNode('${esc(node.id)}')" title="Queue an evo direct directive asking the orchestrator to branch a new experiment from this node.">+ spawn</button>` : ''}
      ${isPrunable ? `<button class="sidebar-prune-btn" onclick="pruneNode('${esc(node.id)}')" title="Remove this leaf from the frontier so the planner stops branching from it. Preserves the commit.">prune</button>` : ''}
      <div class="spacer"></div>
      <button class="drawer-peek-toggle" onclick="toggleDetailMode(event)" title="Switch between side panel and centered view">${peekToggleSvg(state.detailMode)}</button>
      <span class="drawer-close" onclick="closeSidebar()" title="Close (Esc)">&times;</span>
    </div>
    <div class="drawer-tabs" role="tablist" aria-label="Experiment details">
      ${drawerTabButton('summary', 'Summary', activeTab)}
      ${drawerTabButton('diff', 'Diff', activeTab)}
      ${drawerTabButton('tasks', 'Tasks', activeTab)}
      ${drawerTabButton('logs', 'Logs', activeTab)}
    </div>`;

  if (activeTab === 'summary') {
    // Status pill lives in the drawer HEADER (always-visible). No need
    // to repeat it here. The meta line just carries the parent link.
    const deltaClass = deltaClassFor(delta);
    const parentLine = node.parent && node.parent !== 'root'
      ? `<div class="drawer-score-meta">from <a class="drawer-parent-link" onclick="openDrawer('${esc(node.parent)}')">${esc(node.parent)}</a></div>`
      : (node.parent === 'root' ? `<div class="drawer-score-meta">from baseline</div>` : '');
    let detail = '';
    if (node.gate_result === false) {
      const gf = (node.gate_failures || []).join(', ');
      detail = `<div class="drawer-summary-detail gate-failed">Accuracy gate FAILED${gf ? ' (' + esc(gf) + ')' : ''}${node.discard_reason ? ': ' + esc(node.discard_reason) : ''}</div>`;
    } else if (node.status === 'failed' && node.error) {
      detail = `<div class="drawer-summary-detail error">${esc(node.error)}</div>`;
    } else if (node.effective_status === 'lineage_blocked') {
      const blocker = node.lineage_blocked_by || 'an ancestor';
      const reason = node.lineage_blocked_reason ? `: ${esc(node.lineage_blocked_reason)}` : '';
      detail = `<div class="drawer-summary-detail error">Lineage blocked by ${esc(blocker)}${reason}</div>`;
    } else if (node.status === 'pruned' && node.pruned_reason) {
      const prunedCls = isPrunedKept(node) ? ' pruned-kept' : '';
      const prefix = isPrunedKept(node)
        ? 'Kept result, pruned from frontier: '
        : (node.prune_kind === 'invalid' ? 'Invalidated result: ' : '');
      detail = `<div class="drawer-summary-detail${prunedCls}">${prefix}${esc(node.pruned_reason)}</div>`;
    } else if (node.status === 'discarded' && node.discard_reason) {
      detail = `<div class="drawer-summary-detail">${esc(node.discard_reason)}</div>`;
    } else if (isFrontierCandidate(node)) {
      detail = `<div class="drawer-summary-detail accent">Frontier candidate — evo may branch from this node next.</div>`;
    } else if (node.status === 'active' && node.active_attempt) {
      const a = node.active_attempt;
      const phaseLabel = a.phase || 'running';
      detail = `<div class="drawer-summary-detail accent">Attempt ${a.n} — ${esc(phaseLabel)}${a.updated_at ? ' · ' + esc(relTime(a.updated_at)) + ' ago' : ''}</div>`;
    }
    html += `<div class="drawer-section drawer-summary">
      <div class="drawer-score-row">
        <span class="drawer-score">${node.score != null ? node.score.toFixed(2) : '--'}</span>
        ${delta ? `<span class="drawer-score-delta ${deltaClass}">${esc(delta)}</span>` : ''}
      </div>
      ${parentLine}
      ${detail}
    </div>`;

    // Hypothesis is the narrative — what this experiment tries. Sits
    // right after the score so the reader can answer "what did we try,
    // did it work?" without scrolling past plumbing.
    if (node.hypothesis) {
      html += `<div class="drawer-section">
        <span class="drawer-section-title">Hypothesis</span>
        <div class="drawer-hyp">${esc(node.hypothesis)}</div>
      </div>`;
    }

    // Diagnoses: global annotations written by benchmark-reviewer,
    // verifier, ideator, or the orchestrator itself. Per-task ones live
    // on the Tasks tab; here we surface global + a footnote count.
    const perTaskCount = Object.keys(annotations.byTask).length;
    if (annotations.global.length > 0 || perTaskCount > 0) {
      let diagItems = '';
      for (const a of annotations.global) {
        const split = splitAnnotation(a.analysis);
        const ts = relTime(a.timestamp);
        diagItems += `<div class="drawer-diagnosis">
          ${split.category ? `<span class="task-annotation-cat">${esc(split.category)}</span>` : ''}
          ${esc(split.body)}
          <div class="drawer-diagnosis-meta">${ts ? `${ts} ago` : ''}</div>
        </div>`;
      }
      html += `<div class="drawer-section">
        <span class="drawer-section-title">Diagnoses</span>
        <div class="drawer-diagnoses">${diagItems || '<div class="sidebar-empty">No global diagnoses recorded.</div>'}</div>
        ${perTaskCount > 0 ? `<div class="drawer-diagnoses-footnote">${perTaskCount} per-task diagnosis${perTaskCount === 1 ? '' : 'es'} on the Tasks tab</div>` : ''}
      </div>`;
    }

    html += renderChildrenSection(node);

    // Plumbing — surfaces below the narrative. The Execution Backend
    // section is per-backend specialized (worktree path / pool slot /
    // remote provider rows + logo); raw config behind a fold-out.
    const check = node.checks?.latest;
    const checkOk = check && check.status === 'passed';
    html += `<div class="drawer-section">
      <span class="drawer-section-title">Experiment</span>
      <div class="drawer-meta-row"><span class="drawer-meta-key">Parent</span><span class="drawer-meta-val mono" style="color:var(--indigo);cursor:pointer" onclick="openDrawer('${esc(node.parent)}')">${esc(node.parent)}</span></div>
      <div class="drawer-meta-row"><span class="drawer-meta-key">Branch</span><span class="drawer-meta-val mono">${esc(node.branch || '--')}</span></div>
      <div class="drawer-meta-row"><span class="drawer-meta-key">Epoch</span><span class="drawer-meta-val">${esc(node.eval_epoch || '--')}</span></div>
      <div class="drawer-meta-row"><span class="drawer-meta-key">Backend</span><span class="drawer-meta-val mono">${esc(backendLabel(node.resolved_backend || ws.default_backend))}</span></div>
      <div class="drawer-meta-row"><span class="drawer-meta-key">Created</span><span class="drawer-meta-val">${esc(relTime(node.created_at))} ago</span></div>
      ${node.active_attempt ? `<div class="drawer-meta-row"><span class="drawer-meta-key">Attempt</span><span class="drawer-meta-val mono">${esc(node.active_attempt.phase || 'running')} (#${node.active_attempt.n})</span></div>` : ''}
      ${node.children?.length ? `<div class="drawer-meta-row"><span class="drawer-meta-key">Children</span><span class="drawer-meta-val mono" style="color:var(--indigo)">${node.children.length}</span></div>` : ''}
    </div>
    ${renderBackendSection(node, ws)}
    ${check ? `<div class="drawer-section">
      <span class="drawer-section-title">Latest Run Check</span>
      <div class="drawer-meta-row"><span class="drawer-meta-key">Status</span><span class="drawer-meta-val" style="color:${checkOk ? 'var(--green)' : 'var(--red)'}">${esc(check.status || 'unknown')}</span></div>
      <div class="drawer-meta-row"><span class="drawer-meta-key">Score</span><span class="drawer-meta-val mono">${check.score != null ? Number(check.score).toFixed(2) : '--'}</span></div>
      <div class="drawer-meta-row"><span class="drawer-meta-key">Traces</span><span class="drawer-meta-val mono">${check.trace_count || 0}</span></div>
      <div class="drawer-meta-row"><span class="drawer-meta-key">Artifacts</span><span class="drawer-meta-val mono">${esc(check.artifact_path || '--')}</span></div>
      <div class="drawer-meta-row" id="drawer-trackio-row" style="display:none">
        <span class="drawer-meta-key">Metrics</span>
        <span class="drawer-meta-val" id="drawer-trackio-val"></span>
      </div>
      <div id="drawer-trackio-spark"></div>
      ${check.error ? `<div class="failure-box"><span class="failure-box-title">Failure: ${esc(check.error)}</span></div>` : ''}
    </div>` : ''}`;
  } else if (activeTab === 'diff') {
    try {
    const diff = await fetch(`/api/node/${expId}/log/diff.patch`).then(r => r.text());
    if (isStale()) return;
    if (diff.trim()) {
      const mode = state.diffView === 'unified' ? 'unified' : 'split';
      html += `<div class="drawer-section">
        <div class="diff-toolbar">
          <span class="drawer-section-title" style="margin-bottom:0">Code Changes</span>
          <div class="diff-view-toggle" role="tablist">
            <button class="diff-view-btn ${mode === 'split' ? 'active' : ''}" onclick="setDiffView('split')">Split</button>
            <button class="diff-view-btn ${mode === 'unified' ? 'active' : ''}" onclick="setDiffView('unified')">Unified</button>
          </div>
        </div>
        <div class="diff-files">${renderDiffFiles(diff, mode)}</div>
      </div>`;
    } else {
      html += `<div class="drawer-section"><div class="sidebar-empty">No diff recorded for this experiment.</div></div>`;
    }
    } catch (e) {
      html += `<div class="drawer-section"><div class="sidebar-empty">Failed to load diff.</div></div>`;
    }
  } else if (activeTab === 'tasks') {
    let traces = {};
    try {
    traces = await fetch(`/api/node/${expId}/traces`).then(r => r.json());
    if (isStale()) return;
    } catch (e) { /* no traces */ }

    const tasks = node.benchmark_result?.tasks;
    const isActive = node.status === 'active';

    // Build unified task map: completed results take priority, live traces fill in during active runs
    const taskMap = {};
    if (tasks) {
    for (const [tid, score] of Object.entries(tasks)) {
      taskMap[tid] = score;
    }
    } else if (isActive && Object.keys(traces).length > 0) {
    // Active experiment with no result yet -- build task map from live traces
    for (const [filename, trace] of Object.entries(traces)) {
      taskMap[trace.task_id] = trace.score;
    }
    }

    if (Object.keys(taskMap).length > 0) {
    const total = Object.keys(taskMap).length;
    const passed = Object.values(taskMap).filter(v => v >= 0.5).length;
    let tasksHtml = '';

    const sortedTasks = Object.entries(taskMap).sort((a, b) => a[1] - b[1]);
    for (const [tid, score] of sortedTasks) {
      const taskPassed = score >= 0.5;
      const color = taskPassed ? 'var(--green)' : 'var(--red)';
      const traceKey = `task_${tid}.json`;
      const trace = traces[traceKey];
      const summary = trace?.summary || '';
      const duration = formatDuration(trace?.started_at, trace?.ended_at);

      tasksHtml += `<div class="task-row" onclick="toggleTask(this, '${esc(expId)}', '${esc(tid)}')">
        <div class="task-head">
          <span class="task-dot" style="background:${color}"></span>
          <span class="task-id">${esc(tid)}</span>
          ${duration ? `<span class="task-duration">${esc(duration)}</span>` : ''}
          <span class="task-score" style="color:${color}">${score.toFixed(1)}</span>
        </div>
        ${summary ? `<div class="task-summary">${esc(summary)}</div>` : ''}
      </div>`;

      // Per-task annotation (from benchmark-reviewer's review-experiment
      // mode, or any agent that wrote `evo annotate <exp> --task K`).
      // Sits right under the task row so the diagnosis is co-located
      // with the failure it explains.
      const ann = annotations.byTask[String(tid)];
      if (ann) {
        const split = splitAnnotation(ann.analysis);
        tasksHtml += `<div class="task-annotation">
          ${split.category ? `<span class="task-annotation-cat">${esc(split.category)}</span>` : ''}
          ${esc(split.body)}
        </div>`;
      }

      // Trace detail (hidden by default, toggled by click)
      if (trace) {
        let traceHtml = '<div class="trace-detail hidden" data-task="' + esc(tid) + '">';
        if (trace.started_at || trace.ended_at) {
          const start = formatTime(trace.started_at);
          const end = formatTime(trace.ended_at);
          const dur = formatDuration(trace.started_at, trace.ended_at);
          traceHtml += `<div class="trace-timestamps">`;
          if (start) traceHtml += `<span>Started: ${start}</span>`;
          if (end) traceHtml += `<span>Ended: ${end}</span>`;
          if (dur) traceHtml += `<span>Duration: ${dur}</span>`;
          traceHtml += `</div>`;
        }
        // Target + model output -- the most useful per-task surface for
        // LLM benchmarks. Renders for any trace that has these fields,
        // regardless of whether the benchmark also emitted `events`.
        if (trace.target !== undefined && trace.target !== null) {
          traceHtml += `<div class="trace-msg">
            <div class="trace-role tool">target</div>
            <div class="trace-content mono">${esc(String(trace.target))}</div>
          </div>`;
        }
        if (trace.model_output) {
          traceHtml += `<div class="trace-msg">
            <div class="trace-role agent">model output</div>
            <div class="trace-content mono">${esc(String(trace.model_output))}</div>
          </div>`;
        }
        if (trace.failure_reason) {
          traceHtml += `<div class="failure-box">
            <span class="failure-box-title">Failure: ${esc(trace.failure_reason)}</span>
            ${trace.summary ? `<div class="failure-box-text">${esc(trace.summary)}</div>` : ''}
          </div>`;
        }
        if (trace.events?.length) {
          for (const ev of trace.events) {
            const role = ev.role || ev.name || 'event';
            const roleClass = role === 'user' ? 'user' : role === 'assistant' ? 'agent' : 'tool';
            const content = ev.content || JSON.stringify(ev.attributes || ev, null, 2);
            traceHtml += `<div class="trace-msg">
              <div class="trace-role ${roleClass}">${esc(role)}</div>
              <div class="trace-content">${esc(String(content).substring(0, 500))}</div>
            </div>`;
          }
        }
        traceHtml += '</div>';
        tasksHtml += traceHtml;
      }
    }

    const label = isActive && !tasks ? `${passed}/${total} (running...)` : `${passed}/${total}`;
    html += `<div class="drawer-section">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
        <span class="drawer-section-title" style="margin-bottom:0">Benchmark Tasks</span>
        <span class="mono" style="font-size:11px;color:var(--text-1);font-weight:500">${esc(label)}</span>
      </div>
      ${tasksHtml}
    </div>`;
    } else if (isActive) {
    html += `<div class="drawer-section">
      <span class="drawer-section-title">Benchmark Tasks</span>
      <div class="sidebar-empty">Running... waiting for first task to complete.</div>
    </div>`;
    } else {
      html += `<div class="drawer-section"><div class="sidebar-empty">No benchmark task results recorded.</div></div>`;
    }
  } else if (activeTab === 'logs') {
    let payload = { attempt: null, files: [] };
    try {
      payload = await fetch(`/api/node/${expId}/logs`).then(r => r.json());
      if (isStale()) return;
    } catch (e) { /* empty list */ }
    if (!payload.files.length) {
      html += `<div class="drawer-section"><div class="sidebar-empty">No log files in latest attempt. Training scripts write to logs/*.log under the experiment worktree.</div></div>`;
    } else {
      // Default to the largest file (= most active). User can switch via tabs.
      const sorted = [...payload.files].sort((a, b) => b.size - a.size);
      const initial = state.logsSelected && payload.files.find(f => f.name === state.logsSelected)
        ? state.logsSelected
        : sorted[0].name;
      const fileTabs = payload.files.map(f =>
        `<button class="logs-file-tab ${f.name === initial ? 'active' : ''}" onclick="onLogsFileSelect('${esc(f.name)}')">${esc(f.name)} <span style="opacity:0.65">${formatBytes(f.size)}</span></button>`
      ).join('');
      html += `<div class="drawer-section">
        <div class="logs-file-tabs">${fileTabs}</div>
        <div class="logs-controls">
          <label>
            <input type="checkbox" id="logs-autorefresh" onchange="onLogsAutorefreshChange()" ${state.logsAutoRefresh ? 'checked' : ''}>
            live tail
          </label>
          <button onclick="refreshLogsTail(true)">refresh</button>
        </div>
        <pre id="logs-tail" class="logs-pane">Loading...</pre>
      </div>`;
    }
  }

  if (isStale()) return;
  content.innerHTML = html;

  // Post-render side effects: kick off async fetches that populate placeholders
  // already in the DOM. Done after innerHTML so the main panel paints first.
  if (activeTab === 'summary') {
    loadTrackioPreview(expId);
  } else if (activeTab === 'logs') {
    // Inline file tabs (.logs-file-tab.active) replace the old dropdown.
    // Read the initially-active one off the DOM so a re-render of the same
    // file keeps showing its content (instead of resetting to the first).
    const activeBtn = document.querySelector('.logs-file-tab.active');
    if (activeBtn) {
      // Filename is the part before the size span -- robust enough since
      // log names don't contain spaces in practice.
      state.logsSelected = (activeBtn.textContent || '').trim().split(/\s+/)[0];
      state.logsOffset = 0;
      refreshLogsTail(true);
      if (state.logsAutoRefresh) startLogsAutorefresh(expId);
    }
  }
}

function formatBytes(n) {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}

// Strip ANSI escape codes from log output for readable rendering.
// Handles both real ESC+CSI (logs from a TTY) and the bracket-only
// leftover (some emitters drop ESC when writing through pipes).
function stripAnsi(text) {
  if (!text) return '';
  return text
    .replace(/\x1b\[[0-9;]*[A-Za-z]/g, '')
    .replace(/\[[0-9;]+[A-Za-z]/g, '');
}

// Fetch all annotations and return {global: [...], byTask: {tid: ann}}
// scoped to one experiment. Per-task annotations keyed by string task_id
// so they join cleanly against the trace task_id field.
// Recognizes the benchmark-reviewer convention: leading "category: " in
// the analysis text -> renders as a colored tag.
async function getAnnotationsForExperiment(expId) {
  let all = [];
  try {
    const r = await fetch('/api/annotations');
    if (r.ok) {
      const data = await r.json();
      all = data.annotations || [];
    }
  } catch (e) { /* empty */ }
  const mine = all.filter(a => a.experiment_id === expId);
  const global = mine.filter(a => a.task_id === null || a.task_id === undefined);
  const byTask = {};
  for (const a of mine) {
    if (a.task_id !== null && a.task_id !== undefined) {
      byTask[String(a.task_id)] = a;
    }
  }
  return { global, byTask };
}

// Split a benchmark-reviewer annotation into {category, body}. If no
// leading "<category>: ..." prefix is detected, returns category=null.
function splitAnnotation(text) {
  if (!text) return { category: null, body: '' };
  const m = text.match(/^([a-z][a-z-]{2,30}):\s+(.+)$/s);
  if (!m) return { category: null, body: text };
  return { category: m[1], body: m[2] };
}

async function loadTrackioPreview(expId) {
  try {
    const r = await fetch(`/api/node/${expId}/trackio`);
    if (!r.ok) return;
    const data = await r.json();
    if (!data.url) return;
    const row = document.getElementById('drawer-trackio-row');
    const val = document.getElementById('drawer-trackio-val');
    const spark = document.getElementById('drawer-trackio-spark');
    if (!row || !val) return;
    const label = data.run_name ? `${data.project}: ${data.run_name}` : (data.project || 'trackio');
    val.innerHTML = `<a href="${esc(data.url)}" target="_blank" rel="noopener" style="color:var(--indigo);text-decoration:none">${esc(label)} ↗</a>`;
    row.style.display = '';
    if (spark && data.scalars) {
      spark.innerHTML = renderSparklines(data.scalars);
    }
  } catch (e) { /* silent */ }
}

function renderSparklines(scalars) {
  // Render at most 3 series. Order: prefer loss, lr, then anything else.
  const preferred = ['loss', 'learning_rate', 'lr', 'reward', 'kl', 'grad_norm'];
  const keys = Object.keys(scalars).sort((a, b) => {
    const ai = preferred.indexOf(a); const bi = preferred.indexOf(b);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  }).slice(0, 3);
  return keys.map(k => {
    const series = scalars[k];
    if (!series || series.length < 2) return '';
    const vals = series.map(p => p.value);
    const min = Math.min(...vals); const max = Math.max(...vals);
    const range = max - min || 1;
    const W = 140, H = 24;
    const pts = series.map((p, i) => {
      const x = (i / (series.length - 1)) * W;
      const y = H - ((p.value - min) / range) * H;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const last = vals[vals.length - 1];
    const lastStr = Math.abs(last) >= 100 ? last.toFixed(1) : Math.abs(last) >= 1 ? last.toFixed(3) : last.toExponential(2);
    return `<div style="display:flex;align-items:center;gap:8px;margin-top:4px;font-size:11px">
      <span class="mono" style="color:var(--text-1);min-width:80px">${esc(k)}</span>
      <svg width="${W}" height="${H}" style="flex-shrink:0"><polyline points="${pts}" fill="none" stroke="var(--indigo)" stroke-width="1.2"/></svg>
      <span class="mono" style="color:var(--text-0)">${lastStr}</span>
    </div>`;
  }).join('');
}

let _logsTimer = null;
function startLogsAutorefresh(expId) {
  stopLogsAutorefresh();
  _logsTimer = setInterval(() => refreshLogsTail(false), 2000);
}
function stopLogsAutorefresh() {
  if (_logsTimer) { clearInterval(_logsTimer); _logsTimer = null; }
}
function onLogsFileChange() {
  const picker = document.getElementById('logs-file-picker');
  if (!picker) return;
  state.logsSelected = picker.value;
  state.logsOffset = 0;
  refreshLogsTail(true);
}
// New inline-tab handler -- toggles .active class on the clicked button,
// updates the selected file, and reloads the pane.
function onLogsFileSelect(name) {
  if (!name) return;
  state.logsSelected = name;
  state.logsOffset = 0;
  // Update the visual active state on the file tabs.
  document.querySelectorAll('.logs-file-tab').forEach(btn => {
    const isActive = btn.textContent.trim().startsWith(name);
    btn.classList.toggle('active', isActive);
  });
  refreshLogsTail(true);
}
function onLogsAutorefreshChange() {
  const cb = document.getElementById('logs-autorefresh');
  state.logsAutoRefresh = !!cb?.checked;
  if (state.logsAutoRefresh) {
    const expId = state.selectedNode;
    if (expId) startLogsAutorefresh(expId);
  } else {
    stopLogsAutorefresh();
  }
}
async function refreshLogsTail(reset) {
  const pre = document.getElementById('logs-tail');
  if (!pre || !state.selectedNode || !state.logsSelected) return;
  const params = new URLSearchParams();
  if (reset) {
    params.set('tail', '500');
  } else {
    params.set('offset', String(state.logsOffset || 0));
  }
  try {
    const r = await fetch(`/api/node/${state.selectedNode}/log/${encodeURIComponent(state.logsSelected)}?${params}`);
    if (!r.ok) return;
    const text = await r.text();
    const size = parseInt(r.headers.get('X-Log-Size') || '0', 10);
    const clean = stripAnsi(text);
    if (reset) {
      pre.textContent = clean;
    } else if (clean) {
      pre.textContent += clean;
    }
    state.logsOffset = size;
    // Stick to bottom on append
    pre.scrollTop = pre.scrollHeight;
  } catch (e) { /* network blip; next tick will retry */ }
}

function drawerTabButton(tab, label, activeTab) {
  return `<button class="drawer-tab ${activeTab === tab ? 'active' : ''}" role="tab" aria-selected="${activeTab === tab ? 'true' : 'false'}" onclick="setSidebarTab('${tab}')">${label}</button>`;
}

function setSidebarTab(tab) {
  if (!['summary', 'diff', 'tasks', 'logs'].includes(tab)) return;
  state.sidebarTab = tab;
  // Stop log-tail polling when navigating away from the Logs tab.
  if (tab !== 'logs') stopLogsAutorefresh();
  if (state.selectedNode) openDrawer(state.selectedNode);
}

function toggleTask(el, expId, taskId) {
  const detail = el.nextElementSibling;
  if (detail && detail.classList.contains('trace-detail')) {
    detail.classList.toggle('hidden');
  }
}

// Icon for the side/center peek toggle. Shows the icon for the mode you'd
// switch TO, mirroring Notion's "switch peek mode" affordance.
function peekToggleSvg(mode) {
  if (mode === 'center') {
    // Currently centered → offer "dock to side": a panel hugging the right edge.
    return `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4">
      <rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><line x1="10" y1="2.5" x2="10" y2="13.5"/></svg>`;
  }
  // Currently side → offer "expand to center": a centered framed box.
  return `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4">
    <rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><rect x="4.5" y="5" width="7" height="6" rx="1"/></svg>`;
}

function applyDetailMode() {
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (!sidebar) return;
  const center = state.detailMode === 'center';
  sidebar.classList.toggle('peek-center', center);
  const open = !sidebar.classList.contains('hidden');
  if (backdrop) backdrop.classList.toggle('hidden', !(center && open));
}

// Drag the left edge of the side panel to resize it. No-op in center mode.
function startSidebarResize(e) {
  e.preventDefault();
  const sidebar = document.getElementById('sidebar');
  if (!sidebar || state.detailMode === 'center') return;
  const handle = e.currentTarget;
  handle.classList.add('dragging');
  document.body.style.userSelect = 'none';
  const minW = 380;
  const maxW = Math.min(1100, Math.round(window.innerWidth * 0.8));
  const onMove = (ev) => {
    const w = Math.max(minW, Math.min(maxW, window.innerWidth - ev.clientX));
    sidebar.style.setProperty('--evo-sidebar-w', w + 'px');
  };
  const onUp = () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    handle.classList.remove('dragging');
    document.body.style.userSelect = '';
    const w = sidebar.style.getPropertyValue('--evo-sidebar-w');
    if (w) { try { localStorage.setItem('evo.sidebarWidth', w.trim()); } catch (_) { /* ignore */ } }
  };
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

function toggleDetailMode(e) {
  if (e) e.stopPropagation();
  state.detailMode = state.detailMode === 'center' ? 'side' : 'center';
  try { localStorage.setItem('evo.detailMode', state.detailMode); } catch (_) { /* ignore */ }
  applyDetailMode();
  // Swap just the toggle icon in place — no need to re-render the panel.
  const btn = document.querySelector('.drawer-peek-toggle');
  if (btn) btn.innerHTML = peekToggleSvg(state.detailMode);
}

function closeSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (sidebar) sidebar.classList.add('hidden');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (backdrop) backdrop.classList.add('hidden');
  state.selectedNode = null;
  stopLogsAutorefresh();
  // Reset history — the next open is a fresh navigation chain.
  state.drawerHistory.length = 0;
  state.drawerHistoryIndex = -1;
  if (state.graph && state.graph.nodes && Object.keys(state.graph.nodes).length) {
    renderTimeline();
    renderScatter();
  }
}

// Back-compat alias (some places still call closeDrawer)
function closeDrawer() { closeSidebar(); }

// Browser-style back/forward navigation between nodes opened in the drawer.
function drawerBack() {
  if (state.drawerHistoryIndex <= 0) return;
  state.drawerHistoryIndex--;
  openDrawer(state.drawerHistory[state.drawerHistoryIndex], { fromHistory: true });
}
function drawerForward() {
  if (state.drawerHistoryIndex >= state.drawerHistory.length - 1) return;
  state.drawerHistoryIndex++;
  openDrawer(state.drawerHistory[state.drawerHistoryIndex], { fromHistory: true });
}

// Manual frontier intervention. Mirrors `evo prune <id>` (cli.py:2859):
// flips a committed/evaluated leaf to pruned with a reason. The planner
// stops considering this node on the next iteration. Commit is preserved.
// Prune flow uses an in-app modal so the OS prompt() doesn't yank focus
// out of the app. The backend rejects empty reasons (dashboard.py:725),
// so the modal enforces a non-empty value before enabling submit.
function pruneNode(expId) {
  const node = state.graph.nodes[expId];
  if (!node) return;
  state._pruneTarget = expId;
  const overlay = document.getElementById('prune-overlay');
  const targetEl = document.getElementById('prune-target-id');
  const reasonEl = document.getElementById('prune-reason');
  const errEl = document.getElementById('prune-error');
  const submitBtn = document.getElementById('prune-submit');
  if (!overlay || !targetEl || !reasonEl) return;
  targetEl.textContent = expId;
  reasonEl.value = '';
  state._pruneConfirmYes = false;
  if (submitBtn) submitBtn.textContent = 'Prune';
  if (errEl) { errEl.textContent = ''; errEl.classList.add('hidden'); }
  overlay.classList.remove('hidden');
  setTimeout(() => reasonEl.focus(), 0);
}

function closePruneModal(ev) {
  // Only close on overlay click, not modal-content click.
  if (ev && ev.currentTarget !== ev.target) return;
  const overlay = document.getElementById('prune-overlay');
  if (overlay) overlay.classList.add('hidden');
  state._pruneTarget = null;
  state._pruneConfirmYes = false;
}

async function submitPrune() {
  const expId = state._pruneTarget;
  if (!expId) return;
  const reasonEl = document.getElementById('prune-reason');
  const errEl = document.getElementById('prune-error');
  const submitBtn = document.getElementById('prune-submit');
  const reason = (reasonEl?.value || '').trim();
  if (!reason) {
    if (errEl) {
      errEl.textContent = 'A reason is required.';
      errEl.classList.remove('hidden');
    }
    reasonEl?.focus();
    return;
  }
  if (submitBtn) submitBtn.disabled = true;
  try {
    const res = await fetch(`/api/node/${encodeURIComponent(expId)}/prune`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason, kind: 'exhausted', yes: !!state._pruneConfirmYes }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => ({}));
      if (payload.requires_yes) {
        state._pruneConfirmYes = true;
        if (submitBtn) submitBtn.textContent = 'Prune anyway';
        if (errEl) {
          errEl.textContent = `${expId} is on the best valid path. Click Prune anyway to confirm.`;
          errEl.classList.remove('hidden');
        }
        return;
      }
      if (errEl) {
        errEl.textContent = `Could not prune ${expId}: ${payload.error || res.statusText}`;
        errEl.classList.remove('hidden');
      }
      return;
    }
    closePruneModal();
    // Refresh from server so frontier/timeline/sidebar all reflect the
    // new status (the planner's next pick reads the same graph file).
    await fetchAll();
    if (state.selectedNode === expId) openDrawer(expId);
  } catch (e) {
    if (errEl) {
      errEl.textContent = `Network error: ${e.message || e}`;
      errEl.classList.remove('hidden');
    }
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

// Retry-from-node: same modal as spawn, but the wrapper text on the
// backend tells the agent this is a re-run, not a branch. Reuses the
// spawn modal UI with a different mode flag.
function retryNode(expId, ev) {
  if (ev) ev.stopPropagation();
  openSpawnModal(expId, 'retry');
}

// Spawn-from-node: posts to /api/direct with from_exp_id so the orchestrator
// branches a new experiment from the clicked node with the user's guidance.
// Uses the same modal pattern as prune to avoid OS-level prompts.
function spawnFromNode(expId, ev) {
  if (ev) ev.stopPropagation();
  openSpawnModal(expId, 'spawn');
}

function openSpawnModal(expId, mode) {
  const node = state.graph.nodes[expId];
  if (!node) return;
  state._spawnTarget = expId;
  state._spawnMode = mode || 'spawn';
  const overlay = document.getElementById('spawn-overlay');
  const titleEl = document.getElementById('spawn-title');
  const targetEl = document.getElementById('spawn-target-id');
  const textEl = document.getElementById('spawn-text');
  const errEl = document.getElementById('spawn-error');
  const statusEl = document.getElementById('spawn-status');
  const submitEl = document.getElementById('spawn-submit');
  if (!overlay || !targetEl || !textEl) return;
  const isRetry = state._spawnMode === 'retry';
  if (titleEl) titleEl.textContent = isRetry ? 'Retry' : 'New experiment from';
  targetEl.textContent = expId;
  textEl.value = '';
  textEl.placeholder = isRetry ? 'what to change...' : 'what to try next...';
  if (submitEl) submitEl.textContent = isRetry ? 'Retry' : 'Spawn';
  if (errEl) { errEl.textContent = ''; errEl.classList.add('hidden'); }
  if (statusEl) { statusEl.textContent = ''; statusEl.classList.add('hidden'); }
  overlay.classList.remove('hidden');
  setTimeout(() => textEl.focus(), 0);
}

function closeSpawnModal(ev) {
  if (ev && ev.currentTarget !== ev.target) return;
  const overlay = document.getElementById('spawn-overlay');
  if (overlay) overlay.classList.add('hidden');
  state._spawnTarget = null;
}

async function submitSpawn() {
  const expId = state._spawnTarget;
  if (!expId) return;
  const textEl = document.getElementById('spawn-text');
  const errEl = document.getElementById('spawn-error');
  const statusEl = document.getElementById('spawn-status');
  const submitBtn = document.getElementById('spawn-submit');
  const text = (textEl?.value || '').trim();
  if (!text) {
    if (errEl) {
      errEl.textContent = 'Guidance is required.';
      errEl.classList.remove('hidden');
    }
    textEl?.focus();
    return;
  }
  if (submitBtn) submitBtn.disabled = true;
  try {
    const res = await fetch('/api/direct', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from_exp_id: expId, text, mode: state._spawnMode || 'spawn' }),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (errEl) {
        errEl.textContent = `Could not send: ${payload.error || res.statusText}`;
        errEl.classList.remove('hidden');
      }
      return;
    }
    // fanout=0 means it's queued but no agent is running — it'll get picked
    // up the next time one starts. Surface that without jargon.
    const fanout = payload.fanout ?? 0;
    if (statusEl) {
      statusEl.textContent = fanout > 0
        ? 'Sent to the agent.'
        : 'Queued — no agent is running right now. It will pick this up when it starts.';
      statusEl.classList.remove('hidden');
    }
    if (textEl) textEl.value = '';
    setTimeout(() => closeSpawnModal(), 1200);
  } catch (e) {
    if (errEl) {
      errEl.textContent = `Network error: ${e.message || e}`;
      errEl.classList.remove('hidden');
    }
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

function esc(s) {
  // textContent assignment encodes <, >, &; quotes pass through. We also
  // encode " and ' so the same helper is safe inside attribute contexts
  // (title="...", onclick="...('${id}')", value="...").
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ─── Scratchpad modal ────────────────────────────────────
async function openScratchpad() {
  const body = document.getElementById('scratchpad-body');
  body.innerHTML = '<pre>Loading...</pre>';
  document.getElementById('scratchpad-overlay').classList.remove('hidden');
  try {
    const text = await fetch('/api/scratchpad').then(r => r.text());
    body.innerHTML = `<pre>${esc(text)}</pre>`;
  } catch (e) {
    body.innerHTML = '<pre>Failed to load scratchpad</pre>';
  }
}

function closeScratchpad() {
  document.getElementById('scratchpad-overlay').classList.add('hidden');
}

// ─── Settings modal ─────────────────────────────────────
// Rich select: replaces a nested left-rail with an in-context dropdown
// that shows label + description per option. Use this when picking from
// a list of named options inside a tight container (e.g. settings panels).
//
//   options: [{ value, label, description?, group?, badge?, icon? }]
//     icon: optional HTML string rendered as a leading slot in both the
//     trigger (when this option is current) and the popover row.
//   current: the option the trigger displays and the popover highlights
//            (the user's pending pick — moves as they click rows).
//   applied: the option to tag "active" in the popover (the saved server
//            value — only moves once the user actually saves). Falls back
//            to `current` if not provided.
//   onChange: (newValue) => void; only called when value actually changes
function renderRichSelect(host, { options, current, applied, onChange, ariaLabel }) {
  host.classList.add('rich-select');
  const currentOpt = options.find(o => o.value === current) || options[0];
  const appliedVal = applied !== undefined ? applied : current;

  const popParts = [];
  let lastGroup = null;
  for (const opt of options) {
    if (opt.group && opt.group !== lastGroup) {
      popParts.push(`<div class="rich-select-group-label">${esc(opt.group)}</div>`);
      lastGroup = opt.group;
    }
    const isCurrent = opt.value === current;
    const isApplied = opt.value === appliedVal;
    const badge = isApplied ? 'active' : (opt.badge || '');
    popParts.push(`
      <div class="rich-select-option${isCurrent ? ' current' : ''}" data-value="${esc(opt.value)}" role="option" aria-selected="${isCurrent}">
        ${opt.icon ? `<span class="rich-select-option-icon">${opt.icon}</span>` : ''}
        <div class="rich-select-option-text">
          <div class="rich-select-option-label">${esc(opt.label)}</div>
          ${opt.description ? `<div class="rich-select-option-desc">${esc(opt.description)}</div>` : ''}
        </div>
        ${badge ? `<span class="rich-select-option-badge">${esc(badge)}</span>` : ''}
      </div>
    `);
  }

  host.innerHTML = `
    <button type="button" class="rich-select-trigger" aria-haspopup="listbox" aria-label="${esc(ariaLabel || 'Select')}">
      ${currentOpt.icon ? `<span class="rich-select-current-icon">${currentOpt.icon}</span>` : ''}
      <span class="rich-select-current">
        <span class="rich-select-current-label">${esc(currentOpt.label)}</span>
        ${currentOpt.description ? `<span class="rich-select-current-sub">${esc(currentOpt.description)}</span>` : ''}
      </span>
      <svg class="rich-select-caret" width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="2 4 5 7 8 4"/>
      </svg>
    </button>
    <div class="rich-select-pop hidden" role="listbox">${popParts.join('')}</div>
  `;

  const trigger = host.querySelector('.rich-select-trigger');
  const pop = host.querySelector('.rich-select-pop');
  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    closeAllRichSelectsExcept(host);
    if (pop.classList.contains('hidden')) {
      pop.classList.remove('hidden');
      host.classList.add('open');
    } else {
      pop.classList.add('hidden');
      host.classList.remove('open');
    }
  });
  pop.addEventListener('click', (e) => {
    const opt = e.target.closest('.rich-select-option');
    if (!opt) return;
    const value = opt.dataset.value;
    pop.classList.add('hidden');
    host.classList.remove('open');
    if (value !== current) onChange(value);
  });
}

function closeAllRichSelectsExcept(keep) {
  document.querySelectorAll('.rich-select.open').forEach((rs) => {
    if (rs === keep) return;
    rs.querySelector('.rich-select-pop')?.classList.add('hidden');
    rs.classList.remove('open');
  });
}

// One document-level click handler closes any open rich-select. Uses the
// capture phase because parent modals (.modal) stop propagation on click,
// which would otherwise hide this listener from clicks inside the modal.
document.addEventListener('click', (e) => {
  if (e.target.closest('.rich-select.open')) return;
  closeAllRichSelectsExcept(null);
}, true);

// Workspace popover: read-only view of init-time facts (metric, host,
// commit strategy, keyfile presence, benchmark, gate). Opens from the
// project/target text in the topbar. The settings modal handles editable
// settings only; this is for the things you don't change after init.
function toggleWorkspacePopover(ev) {
  if (ev) ev.stopPropagation();
  const popover = document.getElementById('workspace-popover');
  const anchor = document.getElementById('target-file');
  if (!popover || !anchor) return;
  if (!popover.classList.contains('hidden')) {
    popover.classList.add('hidden');
    return;
  }
  popover.innerHTML = renderWorkspacePopover(state.workspace || {}, state.stats || {});
  popover.classList.remove('hidden');
  const r = anchor.getBoundingClientRect();
  const margin = 8;
  // Position below the target-file; clamp to viewport.
  popover.style.visibility = 'hidden';
  popover.style.left = '0px';
  popover.style.top = '0px';
  const pw = popover.offsetWidth;
  let left = r.left;
  if (left + pw > window.innerWidth - margin) left = Math.max(margin, window.innerWidth - pw - margin);
  popover.style.left = `${left}px`;
  popover.style.top = `${r.bottom + 6}px`;
  popover.style.visibility = '';
}

function renderWorkspacePopover(ws, stats) {
  const row = (label, value, mono) => `
    <div class="ws-row">
      <span class="ws-label">${esc(label)}</span>
      <span class="ws-value${mono ? ' mono' : ''}">${esc(value || '--')}</span>
    </div>
  `;
  const keyfile = ws.keyfile_present ? 'present' : 'missing';
  return `
    <div class="ws-pop-head">
      <span class="ws-pop-title">${esc(ws.project_name || stats.project_name || 'workspace')}</span>
      <span class="ws-pop-sub">read-only · set at init</span>
    </div>
    <div class="ws-pop-body">
      ${row('entrypoint', ws.target, true)}
      ${row('metric', ws.metric)}
      ${row('host', ws.host)}
      ${row('commit strategy', ws.commit_strategy)}
      ${row('keyfile', keyfile)}
    </div>
    ${ws.benchmark ? `
      <div class="ws-pop-section">
        <div class="ws-pop-section-label">Benchmark</div>
        <pre class="ws-pre">${esc(ws.benchmark)}</pre>
      </div>
    ` : ''}
    ${ws.gate ? `
      <div class="ws-pop-section">
        <div class="ws-pop-section-label">Gate</div>
        <pre class="ws-pre">${esc(ws.gate)}</pre>
      </div>
    ` : ''}
  `;
}

// Outside-click dismiss for the workspace popover. Hooked into the same
// document-level click that already dismisses the tip-popover.
document.addEventListener('click', (e) => {
  const popover = document.getElementById('workspace-popover');
  if (!popover || popover.classList.contains('hidden')) return;
  if (e.target.closest('#workspace-popover') || e.target.closest('#target-file')) return;
  popover.classList.add('hidden');
});

async function openSettings() {
  const body = document.getElementById('settings-body');
  body.innerHTML = '<pre>Loading...</pre>';
  document.getElementById('settings-overlay').classList.remove('hidden');
  try {
    const [ws, frontierMeta] = await Promise.all([
      fetch('/api/workspace').then(r => r.json()),
      fetch('/api/frontier-strategy').then(r => r.json()),
    ]);
    state.workspace = ws;
    state.frontierMeta = frontierMeta;
    renderSettings(body, ws, frontierMeta);
  } catch (e) {
    body.innerHTML = '<pre>Failed to load settings</pre>';
  }
}

function closeSettings() {
  document.getElementById('settings-overlay').classList.add('hidden');
  hideTip();
}

function renderBackendRuntimeCard(spec) {
  const runtime = spec.runtime || {};
  const title = backendLabel(spec);
  const badges = [
    spec.is_default ? '<span class="config-badge">default</span>' : '',
    spec.active_node_ids?.length ? `<span class="config-badge active">active ${spec.active_node_ids.length}</span>` : '',
    runtime.state_key ? `<span class="config-badge mono">${runtime.state_key}</span>` : '',
  ].filter(Boolean).join('');

  let runtimeHtml = '<div class="config-runtime-empty">no runtime state</div>';
  if (runtime.kind === 'pool') {
    runtimeHtml = `
      <div class="config-kv-grid">
        <div class="config-kv"><span>slots</span><strong>${runtime.slot_count || 0}</strong></div>
        <div class="config-kv"><span>leased</span><strong>${runtime.leased_count || 0}</strong></div>
        <div class="config-kv"><span>free</span><strong>${runtime.free_count || 0}</strong></div>
      </div>
      ${(runtime.slots || []).length ? `
        <div class="config-list">
          ${runtime.slots.map(slot => `
            <div class="config-list-row">
              <span class="mono">slot ${slot.id}</span>
              <span class="config-list-main mono">${esc(slot.path || '')}</span>
              <span>${slot.leased_by?.exp_id || 'free'}</span>
            </div>
          `).join('')}
        </div>` : ''}
    `;
  } else if (runtime.kind === 'remote') {
    runtimeHtml = `
      <div class="config-kv-grid">
        <div class="config-kv"><span>provider</span><strong>${esc(runtime.provider || spec.provider || '--')}</strong></div>
        <div class="config-kv"><span>sandboxes</span><strong>${runtime.sandbox_count || 0}</strong></div>
        <div class="config-kv"><span>leased</span><strong>${runtime.leased_count || 0}</strong></div>
        <div class="config-kv"><span>free</span><strong>${runtime.free_count || 0}</strong></div>
        <div class="config-kv"><span>pool size</span><strong>${runtime.pool_size != null ? runtime.pool_size : 'unbounded'}</strong></div>
      </div>
      ${(runtime.sandboxes || []).length ? `
        <div class="config-list">
          ${runtime.sandboxes.map(sb => `
            <div class="config-list-row">
              <span class="mono">sb ${sb.id}</span>
              <span class="config-list-main mono">${esc(sb.native_id || sb.base_url || '')}</span>
              <span>${sb.leased_by?.exp_id || 'free'}</span>
            </div>
          `).join('')}
        </div>` : ''}
    `;
  }

  return `
    <div class="config-card">
      <div class="config-card-head">
        <div>
          <div class="config-card-title">${esc(title)}</div>
          <div class="config-card-sub">${spec.node_ids?.length || 0} experiments reference this backend</div>
        </div>
        <div class="config-badges">${badges}</div>
      </div>
      <div class="config-card-section">
        <div class="config-section-label">Configuration</div>
        <pre class="config-pre">${esc(prettyJson(spec.config || {}))}</pre>
      </div>
      <div class="config-card-section">
        <div class="config-section-label">Live runtime</div>
        ${runtimeHtml}
      </div>
    </div>
  `;
}

function renderProviderStatus(provider, readiness) {
  const info = readiness?.[provider];
  if (!info) return '';
  return `
    <div class="provider-status">
      <div class="config-section-label">Provider status</div>
      <div class="provider-status-grid">
        ${Object.entries(info).map(([key, value]) => `
          <div class="provider-status-item"><span>${esc(key.replaceAll('_', ' '))}</span><strong>${esc(String(value))}</strong></div>
        `).join('')}
      </div>
    </div>
  `;
}

function renderOverrideSummary(spec) {
  const label = backendLabel(spec);
  const nodeList = (spec.node_ids || []).join(', ');
  return `
    <div class="override-row">
      <div>
        <div class="override-title">${esc(label)}</div>
        <div class="override-sub">${spec.node_ids?.length || 0} experiments</div>
      </div>
      <button class="btn-link override-toggle" type="button" data-state-key="${spec.runtime?.state_key || ''}">details</button>
    </div>
    <div class="override-detail hidden" data-state-detail="${spec.runtime?.state_key || ''}">
      <pre class="config-pre">${esc(prettyJson(spec.config || {}))}</pre>
      ${spec.runtime ? `<pre class="config-pre">${esc(prettyJson(spec.runtime))}</pre>` : ''}
    </div>
  `;
}

function renderSettings(body, ws, frontierMeta) {
  // Reset the footer's submit handler before each (re-)render so a stale
  // closure from a previously-selected section can't fire.
  state._settingsSubmit = null;
  const saveBtn = document.getElementById('settings-footer-save');
  if (saveBtn) saveBtn.disabled = true;
  setSettingsStatus('');

  body.innerHTML = `
    <div class="settings-shell">
      <aside class="settings-nav" aria-label="Settings sections">
        <button class="settings-nav-item ${state.settingsSection === 'execution' ? 'selected' : ''}" data-section="execution" type="button">
          <span class="settings-nav-title">Backend</span>
          <span class="settings-nav-sub">worktree · pool · remote</span>
        </button>
        <button class="settings-nav-item ${state.settingsSection === 'runtime' ? 'selected' : ''}" data-section="runtime" type="button">
          <span class="settings-nav-title">Environment</span>
          <span class="settings-nav-sub">commands · env vars</span>
        </button>
        <button class="settings-nav-item ${state.settingsSection === 'frontier' ? 'selected' : ''}" data-section="frontier" type="button">
          <span class="settings-nav-title">Frontier</span>
          <span class="settings-nav-sub">branch strategy</span>
        </button>
      </aside>
      <div id="settings-panel" class="settings-panel"></div>
    </div>
  `;
  body.querySelectorAll('[data-section]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.settingsSection = btn.dataset.section;
      renderSettings(body, state.workspace, state.frontierMeta);
    });
  });
  const panel = body.querySelector('#settings-panel');
  if (state.settingsSection === 'execution') {
    renderExecutionSettings(panel, ws);
  } else if (state.settingsSection === 'runtime') {
    renderRuntimeSettings(panel, ws);
  } else {
    renderFrontierSettings(panel, frontierMeta);
  }
}

// Footer status / submit plumbing. Each section's renderer assigns
// state._settingsSubmit to its save closure (or leaves it null for
// read-only sections), and writes user-facing status via setSettingsStatus.
function setSettingsStatus(text, tone) {
  const el = document.getElementById('settings-footer-status');
  if (!el) return;
  el.textContent = text || '';
  el.dataset.tone = tone || '';
}
function registerSettingsSubmit(fn) {
  state._settingsSubmit = fn || null;
  const btn = document.getElementById('settings-footer-save');
  if (btn) btn.disabled = !fn;
}
async function submitActiveSettings() {
  const fn = state._settingsSubmit;
  if (!fn) return;
  const btn = document.getElementById('settings-footer-save');
  if (btn) btn.disabled = true;
  try {
    await fn();
  } finally {
    // Re-enable; per-section renderers may re-register a fresh submit
    // after a successful save (e.g. after re-fetching workspace state).
    if (btn) btn.disabled = !state._settingsSubmit;
  }
}

function renderRuntimeSettings(panel, ws) {
  const runtimeRecipe = ws.runtime || {};
  const runtimeEnv = ws.runtime_env || {};
  const sources = runtimeEnv.dotenv || [];
  const configuredKeyPreviews = runtimeEnv.configured_key_previews || {};
  const runtimeVariablePreviews = runtimeEnv.runtime_variable_previews || {};
  const experiments = getExperiments();
  const checked = experiments.filter(n => n.checks?.latest);
  const latestChecks = checked
    .sort((a, b) => ((b.checks.latest?.finished_at || '').localeCompare(a.checks.latest?.finished_at || '')))
    .slice(0, 8);
  const draft = {
    prepare: runtimeRecipe.prepare || '',
    beforeRun: runtimeRecipe.before_run || '',
    prefix: runtimeRecipe.prefix || '',
    inheritShell: !!runtimeEnv.inherit_shell,
    sources: sources.map(source => ({
      path: source.path || '',
      mode: source.mode || 'all',
      keys: (source.keys || []).join(', '),
    })),
  };

  // No hero, no read-only summary. The form below reveals current values
  // directly; the left rail already labels the section. Recent checks moved
  // to the bottom so editable fields sit above diagnostic state.
  panel.innerHTML = `
    <div id="runtime-env-form"></div>
    <div class="settings-block">
      <div class="settings-block-label">Recent run checks</div>
      ${latestChecks.length ? latestChecks.map(renderCheckSummaryRow).join('') : '<div class="config-runtime-empty">No check runs yet. Use `evo run <exp_id> --check` to validate wiring without committing.</div>'}
    </div>
  `;

  const formHost = panel.querySelector('#runtime-env-form');

  function renderRuntimeForm() {
    formHost.innerHTML = `
      <div class="settings-block">
        <div class="settings-block-label">Recipe</div>
        <label class="settings-field">
          <span>Prepare command</span>
          <input id="runtime-prepare" class="settings-input mono" value="${esc(draft.prepare)}" placeholder="uv sync">
        </label>
        <label class="settings-field">
          <span>Before each run</span>
          <input id="runtime-before-run" class="settings-input mono" value="${esc(draft.beforeRun)}" placeholder="make reset-test-state">
        </label>
        <label class="settings-field">
          <span>Command prefix</span>
          <input id="runtime-prefix" class="settings-input mono" value="${esc(draft.prefix)}" placeholder="uv run">
        </label>
        <div class="settings-help">Prepare and before-run execute in the experiment workspace. Prefix wraps benchmark and gate commands.</div>
      </div>
      <div class="settings-block">
        <div class="settings-block-label">Variables</div>
        <label class="settings-field checkbox runtime-toggle-row">
          <span>
            <strong>Inherit shell process env</strong>
            <small>Pass variables visible to the evo process, such as PATH and provider/API keys already exported in the shell.</small>
          </span>
          <input id="runtime-inherit-shell" type="checkbox" ${draft.inheritShell ? 'checked' : ''}>
        </label>
        <div class="runtime-action-row">
          <button id="runtime-add-variable" class="btn-primary" type="button">Add variable</button>
          <button id="runtime-import-env" class="btn-ghost compact" type="button">Import .env</button>
        </div>
      </div>
      <div class="settings-block">
        <div class="settings-block-label">Dashboard variables</div>
        ${Object.keys(runtimeVariablePreviews).length ? renderRuntimeVariableGrid(runtimeVariablePreviews) : '<div class="config-runtime-empty">No dashboard variables yet. Add variables one-by-one or import a .env file.</div>'}
      </div>
      ${sources.length ? `<div class="settings-block">
        <div class="settings-block-label">File sources</div>
        ${sources.map((source, index) => renderRuntimeSourceEditor(source, index, source)).join('')}
      </div>` : ''}
      ${Object.keys(configuredKeyPreviews).length ? `<div class="settings-block">
        <div class="settings-block-label">Keys from file sources</div>
        ${renderRuntimeKeyGrid(configuredKeyPreviews)}
      </div>` : ''}
    `;

    const inheritInput = formHost.querySelector('#runtime-inherit-shell');
    inheritInput.addEventListener('change', () => {
      draft.inheritShell = inheritInput.checked;
    });
    formHost.querySelectorAll('[data-runtime-remove]').forEach((btn) => {
      btn.addEventListener('click', () => {
        collectRuntimeDraft();
        draft.sources.splice(Number(btn.dataset.runtimeRemove), 1);
        renderRuntimeForm();
      });
    });
    formHost.querySelectorAll('[data-runtime-mode]').forEach((select) => {
      select.addEventListener('change', () => {
        collectRuntimeDraft();
        renderRuntimeForm();
      });
    });
    registerSettingsSubmit(async () => {
      collectRuntimeDraft();
      await saveRuntimeSettings(draft);
    });
    formHost.querySelector('#runtime-add-variable').addEventListener('click', () => {
      openRuntimeVariableModal();
    });
    formHost.querySelector('#runtime-import-env').addEventListener('click', () => {
      openRuntimeImportModal();
    });
  }

  function collectRuntimeDraft() {
    draft.prepare = formHost.querySelector('#runtime-prepare')?.value.trim() || '';
    draft.beforeRun = formHost.querySelector('#runtime-before-run')?.value.trim() || '';
    draft.prefix = formHost.querySelector('#runtime-prefix')?.value.trim() || '';
    const inheritInput = formHost.querySelector('#runtime-inherit-shell');
    if (inheritInput) draft.inheritShell = inheritInput.checked;
    draft.sources = Array.from(formHost.querySelectorAll('[data-runtime-source]')).map((row) => ({
      path: row.querySelector('[data-runtime-path]').value.trim(),
      mode: row.querySelector('[data-runtime-mode]').value,
      keys: row.querySelector('[data-runtime-keys]')?.value.trim() || '',
    })).filter(source => source.path);
  }

  renderRuntimeForm();
}

function renderRuntimeVariableGrid(previews) {
  return `<div class="runtime-key-list">${Object.entries(previews).map(([key, value]) => `
    <div class="runtime-key-row">
      <span class="mono">${esc(key)}</span>
      <div class="runtime-key-actions">
        <code>${esc(value)}</code>
        <button class="btn-link" type="button" onclick="deleteRuntimeVariable('${esc(key)}')">remove</button>
      </div>
    </div>
  `).join('')}</div>`;
}

function renderRuntimeSourceEditor(source, index, summary) {
  const resolvedKeys = summary?.resolved_keys || [];
  const status = summary ? (summary.exists ? 'present' : 'missing') : 'new';
  const count = summary ? `${resolvedKeys.length} keys` : 'not saved';
  const allowOpen = source.mode === 'allow';
  return `
    <div class="runtime-source-editor" data-runtime-source>
      <div class="runtime-source-editor-main">
        <input data-runtime-path class="settings-input mono" value="${esc(source.path || '')}" placeholder=".env">
        <select data-runtime-mode class="settings-select">
          <option value="all" ${source.mode === 'all' ? 'selected' : ''}>all keys</option>
          <option value="allow" ${source.mode === 'allow' ? 'selected' : ''}>only listed keys</option>
        </select>
        <button class="btn-link" type="button" data-runtime-remove="${index}">remove</button>
      </div>
      ${allowOpen ? `<input data-runtime-keys class="settings-input mono" value="${esc(source.keys || '')}" placeholder="KEY1,KEY2">` : '<input data-runtime-keys type="hidden" value="">'}
      <div class="runtime-source-foot">
        <span>${esc(count)}</span>
        <span class="runtime-source-status ${status === 'present' ? 'ok' : (status === 'missing' ? 'bad' : '')}">${status}</span>
      </div>
    </div>
  `;
}

function renderRuntimeKeyGrid(previews) {
  return `<div class="runtime-key-list">${Object.entries(previews).map(([key, value]) => `
    <div class="runtime-key-row">
      <span class="mono">${esc(key)}</span>
      <code>${esc(value)}</code>
    </div>
  `).join('')}</div>`;
}

async function saveRuntimeSettings(draft) {
  setSettingsStatus('saving...', 'pending');
  const runtimePayload = {
    prepare: draft.prepare,
    before_run: draft.beforeRun,
    prefix: draft.prefix,
  };
  const envPayload = {
    inherit_shell: draft.inheritShell,
    dotenv: draft.sources.map(source => ({
      path: source.path,
      mode: source.mode,
      keys: source.mode === 'allow' ? source.keys : [],
    })),
  };
  try {
    const runtimeRes = await fetch('/api/workspace/runtime', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(runtimePayload),
    });
    const runtimeData = await runtimeRes.json();
    if (!runtimeRes.ok) {
      setSettingsStatus(`error: ${runtimeData.error || runtimeRes.status}`, 'error');
      return;
    }
    const res = await fetch('/api/workspace/runtime-env', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(envPayload),
    });
    const data = await res.json();
    if (!res.ok) {
      setSettingsStatus(`error: ${data.error || res.status}`, 'error');
      return;
    }
    state.workspace = data;
    setSettingsStatus('saved', 'ok');
    renderSettings(document.getElementById('settings-body'), state.workspace, state.frontierMeta);
    fetchAll();
  } catch (e) {
    setSettingsStatus('request failed', 'error');
  }
}

function parseDotenvText(text) {
  const variables = [];
  for (const rawLine of String(text || '').split(/\r?\n/)) {
    let line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    if (line.startsWith('export ')) line = line.slice(7).trim();
    const idx = line.indexOf('=');
    if (idx <= 0) continue;
    const key = line.slice(0, idx).trim();
    let value = line.slice(idx + 1).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    } else {
      value = value.replace(/\s+#.*$/, '').trim();
    }
    variables.push({key, value});
  }
  return variables;
}

function openRuntimeVariableModal() {
  openRuntimeEnvModal({
    title: 'Add variable',
    body: `
      <label class="settings-field">
        <span>Key</span>
        <input id="runtime-var-key" class="settings-input mono" placeholder="OPENAI_API_KEY">
      </label>
      <label class="settings-field">
        <span>Value</span>
        <textarea id="runtime-var-value" class="settings-input mono" rows="4" placeholder="secret value"></textarea>
      </label>
    `,
    primary: 'Save variable',
    onSave: async (statusEl) => {
      const key = document.getElementById('runtime-var-key').value.trim();
      const value = document.getElementById('runtime-var-value').value;
      if (!key) {
        statusEl.textContent = 'key is required';
        return false;
      }
      return saveRuntimeVariables([{key, value}], statusEl);
    },
  });
}

function openRuntimeImportModal() {
  openRuntimeEnvModal({
    title: 'Import .env',
    body: `
      <div class="runtime-import-tabs">
        <label class="settings-field">
          <span>Paste .env contents</span>
          <textarea id="runtime-env-paste" class="settings-input mono" rows="10" placeholder="KEY=value&#10;OTHER=value"></textarea>
        </label>
        <label class="settings-field">
          <span>Or upload file</span>
          <input id="runtime-env-file" class="settings-input" type="file" accept=".env,text/plain">
        </label>
      </div>
      <div id="runtime-import-preview" class="settings-help">Paste or upload to preview keys before import.</div>
    `,
    primary: 'Import variables',
    onReady: () => {
      const paste = document.getElementById('runtime-env-paste');
      const file = document.getElementById('runtime-env-file');
      const preview = document.getElementById('runtime-import-preview');
      const updatePreview = () => {
        const vars = parseDotenvText(paste.value);
        preview.textContent = vars.length
          ? `Found ${vars.length} key${vars.length === 1 ? '' : 's'}: ${vars.map(v => v.key).join(', ')}`
          : 'No valid KEY=value entries found yet.';
      };
      paste.addEventListener('input', updatePreview);
      file.addEventListener('change', async () => {
        const picked = file.files && file.files[0];
        if (!picked) return;
        paste.value = await picked.text();
        updatePreview();
      });
    },
    onSave: async (statusEl) => {
      const variables = parseDotenvText(document.getElementById('runtime-env-paste').value);
      if (!variables.length) {
        statusEl.textContent = 'no variables found';
        return false;
      }
      return saveRuntimeVariables(variables, statusEl);
    },
  });
}

function openRuntimeEnvModal({title, body, primary, onSave, onReady}) {
  let overlay = document.getElementById('runtime-env-modal-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'runtime-env-modal-overlay';
    overlay.className = 'mini-modal-overlay';
    document.body.appendChild(overlay);
  }
  overlay.innerHTML = `
    <div class="mini-modal" onclick="event.stopPropagation()">
      <div class="mini-modal-header">
        <span class="modal-title">${esc(title)}</span>
        <span class="close-btn" id="runtime-mini-close">&times;</span>
      </div>
      <div class="mini-modal-body">${body}</div>
      <div class="settings-actions">
        <span id="runtime-mini-status" class="strategy-status"></span>
        <span class="spacer"></span>
        <button id="runtime-mini-cancel" class="btn-ghost compact" type="button">Cancel</button>
        <button id="runtime-mini-save" class="btn-primary" type="button">${esc(primary)}</button>
      </div>
    </div>
  `;
  const close = () => overlay.classList.add('hidden');
  overlay.classList.remove('hidden');
  overlay.onclick = close;
  overlay.querySelector('#runtime-mini-close').onclick = close;
  overlay.querySelector('#runtime-mini-cancel').onclick = close;
  overlay.querySelector('#runtime-mini-save').onclick = async () => {
    const ok = await onSave(overlay.querySelector('#runtime-mini-status'));
    if (ok) close();
  };
  if (onReady) onReady();
}

async function saveRuntimeVariables(variables, statusEl) {
  statusEl.textContent = 'saving...';
  try {
    const res = await fetch('/api/workspace/runtime-variables', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({variables}),
    });
    const data = await res.json();
    if (!res.ok) {
      statusEl.textContent = `error: ${data.error || res.status}`;
      return false;
    }
    state.workspace = data;
    renderSettings(document.getElementById('settings-body'), state.workspace, state.frontierMeta);
    fetchAll();
    return true;
  } catch (e) {
    statusEl.textContent = 'request failed';
    return false;
  }
}

async function deleteRuntimeVariable(key) {
  await fetch('/api/workspace/runtime-variables', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({delete_keys: [key]}),
  });
  const ws = await fetch('/api/workspace').then(r => r.json());
  state.workspace = ws;
  renderSettings(document.getElementById('settings-body'), state.workspace, state.frontierMeta);
  fetchAll();
}

function renderCheckSummaryRow(node) {
  const latest = node.checks.latest;
  const ok = latest.status === 'passed';
  return `
    <div class="runtime-check-row" onclick="openDrawer('${node.id}')">
      <span class="status-dot" style="background:${ok ? 'var(--green)' : 'var(--red)'}"></span>
      <span class="mono">${shortId(node.id)}</span>
      <span>${esc(latest.status || 'unknown')}</span>
      <span class="spacer"></span>
      <span class="mono">${latest.score != null ? Number(latest.score).toFixed(2) : '--'}</span>
      <span>${latest.trace_count || 0} traces</span>
    </div>
  `;
}

// Maps every rich-select option in the Backend picker to (a) its underlying
// backend spec for logo lookup + saving and (b) display copy.
const BACKEND_DROPDOWN_OPTIONS = [
  { value: 'worktree', label: 'Worktree', description: 'fresh local git worktree per experiment', group: 'LOCAL' },
  { value: 'pool',     label: 'Pool',     description: 'reuse a fixed set of local workspaces',   group: 'LOCAL' },
  { value: 'modal',    label: 'Modal',    description: 'Modal serverless cloud',                   group: 'REMOTE' },
  { value: 'e2b',      label: 'E2B',      description: 'E2B cloud sandboxes',                      group: 'REMOTE' },
  { value: 'ssh',      label: 'SSH',      description: 'your own SSH host',                        group: 'REMOTE' },
  { value: 'daytona',  label: 'Daytona',  description: 'Daytona cloud workspaces',                 group: 'REMOTE' },
  { value: 'aws',      label: 'AWS',      description: 'AWS EC2 sandboxes',                        group: 'REMOTE' },
  { value: 'azure',    label: 'Azure',    description: 'Azure VM sandboxes',                       group: 'REMOTE' },
  { value: 'manual',   label: 'Manual',   description: 'orchestrate sandboxes manually',           group: 'REMOTE' },
  { value: 'custom',   label: 'Custom',   description: 'your own SandboxProvider class',           group: 'REMOTE' },
];
const KNOWN_REMOTE_PROVIDERS = new Set(['modal','e2b','ssh','daytona','aws','azure','manual']);

function backendDropdownSpec(value) {
  if (value === 'worktree' || value === 'pool') return { name: value };
  if (value === 'custom') return { name: 'remote' };
  return { name: 'remote', provider: value };
}
function dropdownValueFromDraft(draft) {
  if (draft.backend === 'worktree' || draft.backend === 'pool') return draft.backend;
  return draft.providerChoice === '__custom__' ? 'custom' : draft.providerChoice;
}
function dropdownValueFromSpec(spec) {
  if (!spec) return 'worktree';
  if (spec.name === 'worktree' || spec.name === 'pool') return spec.name;
  const provider = spec.provider || (spec.config && spec.config.provider) || 'modal';
  return KNOWN_REMOTE_PROVIDERS.has(provider) ? provider : 'custom';
}
function applyBackendChoice(draft, value) {
  if (value === 'worktree' || value === 'pool') {
    draft.backend = value;
    return;
  }
  if (value === 'custom') {
    draft.backend = 'remote';
    draft.providerChoice = '__custom__';
    return;
  }
  draft.backend = 'remote';
  draft.providerChoice = value;
  draft.providerName = value;
}

function renderExecutionSettings(panel, ws) {
  const defaultSpec = ws.default_backend || {name: 'worktree', config: {}};
  const config = defaultSpec.config || {};
  const remoteConfig = defaultSpec.name === 'remote' ? (config.provider_config || {}) : {};
  const providerName = defaultSpec.name === 'remote' ? (defaultSpec.provider || config.provider || 'modal') : 'modal';
  const builtIns = Object.keys(REMOTE_PROVIDER_FIELDS);
  const initialProvider = builtIns.includes(providerName) ? providerName : '__custom__';
  const extraEntries = {};
  if (defaultSpec.name === 'remote') {
    const known = new Set(providerFields(providerName).map(field => field.key));
    Object.entries(remoteConfig).forEach(([key, value]) => {
      if (key === 'pool_size') return;
      if (!known.has(key)) extraEntries[key] = value;
    });
  }
  const draft = {
    backend: defaultSpec.name || 'remote',
    workspaces: defaultSpec.name === 'pool' ? (config.slots || []).join(',') : '',
    poolSize: defaultSpec.name === 'remote' ? ((config.provider_config || {}).pool_size ?? '') : '',
    providerChoice: initialProvider,
    providerName: providerName,
    providerConfig: {...remoteConfig},
    extraConfigText: Object.keys(extraEntries).length ? prettyJson(extraEntries) : '',
  };

  // No hero: the left rail already names the section, and the segmented
  // chooser below shows the current selection unambiguously.
  panel.innerHTML = `<div id="execution-form"></div>`;

  const formHost = panel.querySelector('#execution-form');

  function renderForm() {
    const resolvedProvider = draft.providerChoice === '__custom__' ? draft.providerName : draft.providerChoice;
    const fields = providerFields(resolvedProvider);
    const basicFields = fields.filter(field => !field.advanced);
    const advancedFields = fields.filter(field => field.advanced);
    const defaultRuntime = (ws.backend_configs || []).find(spec => spec.is_default)?.runtime;
    formHost.innerHTML = `
      <div id="backend-picker"></div>
      <div id="settings-backend-detail"></div>
    `;

    // Single rich-select replaces both the worktree/pool/remote chooser
    // and the inner remote-provider select. Each option carries its logo.
    const pickerHost = formHost.querySelector('#backend-picker');
    const options = BACKEND_DROPDOWN_OPTIONS.map(opt => ({
      ...opt,
      icon: backendLogoFor(backendDropdownSpec(opt.value)),
    }));
    renderRichSelect(pickerHost, {
      options,
      current: dropdownValueFromDraft(draft),
      applied: dropdownValueFromSpec(ws.default_backend),
      ariaLabel: 'Backend',
      onChange: (value) => {
        collectDraft();
        applyBackendChoice(draft, value);
        renderForm();
      },
    });

    const detail = formHost.querySelector('#settings-backend-detail');
    if (draft.backend === 'pool') {
      detail.innerHTML = `
        <div class="settings-block">
          <div class="settings-block-label">Pool workspaces</div>
          <textarea id="settings-pool-workspaces" class="settings-input" rows="3" placeholder="/abs/ws-1,/abs/ws-2">${esc(draft.workspaces || '')}</textarea>
          <div class="settings-help">Comma-separated absolute paths.</div>
        </div>
      `;
    } else if (draft.backend === 'remote') {
      detail.innerHTML = `
        <div class="settings-block">
          <div class="settings-block-label">Remote capacity</div>
          <label class="settings-field inline">
            <span>Pool size</span>
            <input id="settings-pool-size" class="settings-input" type="number" step="1" min="1" value="${esc(String(draft.poolSize ?? ''))}" placeholder="unbounded">
          </label>
          <small>Leave blank for unbounded concurrent sandboxes.</small>
        </div>
        ${draft.providerChoice === '__custom__' ? `
          <div class="settings-block">
            <div class="settings-block-label">Custom provider path</div>
            <input id="settings-provider-name" class="settings-input" type="text" value="${esc(draft.providerName || '')}" placeholder="my_pkg.providers:Provider">
          </div>
        ` : ''}
        ${basicFields.length ? `
          <div class="settings-block">
            <div class="settings-block-label">Connection</div>
            <div class="settings-provider-fields">
              ${basicFields.map(field => renderProviderField(field, draft.providerConfig[field.key])).join('')}
            </div>
          </div>
        ` : ''}
        ${renderProviderStatus(resolvedProvider, ws.provider_readiness)}
        <details class="settings-advanced">
          <summary>Advanced provider fields</summary>
          <div class="settings-advanced-body">
            ${advancedFields.length ? `
              <div class="settings-provider-fields">
                ${advancedFields.map(field => renderProviderField(field, draft.providerConfig[field.key])).join('')}
              </div>
            ` : ''}
            <label class="settings-field">
              <span>Advanced JSON overrides</span>
              <textarea id="settings-extra-config" rows="6" placeholder="{ }">${esc(draft.extraConfigText || '')}</textarea>
              <small>Only for values not covered above.</small>
            </label>
          </div>
        </details>
        <details class="settings-advanced">
          <summary>Current runtime</summary>
          <div class="settings-advanced-body">
            ${defaultRuntime ? renderBackendRuntimeCard({...ws.default_backend, is_default: true, node_ids: [], active_node_ids: [], runtime: defaultRuntime}) : '<div class="config-runtime-empty">no runtime state yet for the current default backend</div>'}
          </div>
        </details>
      `;
    } else {
      detail.innerHTML = `<div class="config-runtime-empty">Worktree mode creates a fresh local git worktree per experiment.</div>`;
    }

    // Hook the footer Save into this panel's draft.
    registerSettingsSubmit(async () => {
      collectDraft();
      await saveExecutionSettings(draft, { tone: 'ok' }, panel);
    });
  }

  function collectDraft() {
    const poolInput = formHost.querySelector('#settings-pool-workspaces');
    if (poolInput) draft.workspaces = poolInput.value;
    const poolSizeInput = formHost.querySelector('#settings-pool-size');
    if (poolSizeInput) draft.poolSize = poolSizeInput.value;
    const providerNameInput = formHost.querySelector('#settings-provider-name');
    if (providerNameInput) draft.providerName = providerNameInput.value.trim();
    const extraInput = formHost.querySelector('#settings-extra-config');
    if (extraInput) draft.extraConfigText = extraInput.value;
    if (draft.backend === 'remote') {
      const resolved = draft.providerChoice === '__custom__' ? draft.providerName : draft.providerChoice;
      const nextConfig = {};
      providerFields(resolved).forEach((field) => {
        if (field.type === 'bool') {
          const input = formHost.querySelector(`[data-provider-key="${field.key}"]`);
          if (input) nextConfig[field.key] = input.checked;
          return;
        }
        const input = formHost.querySelector(`[data-provider-key="${field.key}"]`);
        if (!input) return;
        const normalized = normalizeFormValue(input.value, field.type);
        if (normalized !== '') nextConfig[field.key] = normalized;
      });
      draft.providerConfig = nextConfig;
    }
  }

  renderForm();

  panel.querySelectorAll('.override-toggle').forEach((btn) => {
    btn.addEventListener('click', () => {
      const detail = panel.querySelector(`[data-state-detail="${btn.dataset.stateKey}"]`);
      if (!detail) return;
      detail.classList.toggle('hidden');
      btn.textContent = detail.classList.contains('hidden') ? 'details' : 'hide';
    });
  });
}

function renderProviderField(field, value) {
  if (field.type === 'bool') {
    return `
      <label class="settings-field checkbox">
        <span>${esc(field.label)}</span>
        <input data-provider-key="${field.key}" type="checkbox" ${value ? 'checked' : ''}>
      </label>
    `;
  }
  const isSecret = field.type === 'secret';
  const displayValue = isSecret || isRedacted(value) ? '' : (value ?? '');
  const placeholder = isSecret && value ? 'leave blank to keep current value' : '';
  const inputType = isSecret ? 'password' : (field.type === 'int' || field.type === 'float' ? 'number' : 'text');
  const step = field.type === 'float' ? 'any' : (field.type === 'int' ? '1' : null);
  // Numbers fit comfortably to the right of the label; secrets and free
  // text often run long, so they stay full-width with the label above.
  const inline = field.type === 'int' || field.type === 'float';
  return `
    <label class="settings-field${inline ? ' inline' : ''}">
      <span>${esc(field.label)}</span>
      <input
        class="settings-input"
        data-provider-key="${field.key}"
        type="${inputType}"
        ${step ? `step="${step}"` : ''}
        value="${esc(String(displayValue))}"
        placeholder="${esc(placeholder)}"
      >
      ${field.help ? `<div class="settings-help">${esc(field.help)}</div>` : ''}
      ${isSecret && value ? `<div class="settings-help">stored; enter a new value to replace it</div>` : ''}
    </label>
  `;
}

async function saveExecutionSettings(draft, _opts, panel) {
  setSettingsStatus('saving...', 'pending');
  let payload = {backend: draft.backend};
  try {
    if (draft.backend === 'pool') {
      payload.workspaces = draft.workspaces;
    } else if (draft.backend === 'remote') {
      const resolvedProvider = draft.providerChoice === '__custom__' ? draft.providerName : draft.providerChoice;
      if (!resolvedProvider) {
        setSettingsStatus('provider is required', 'error');
        return;
      }
      let extra = {};
      if ((draft.extraConfigText || '').trim()) {
        extra = JSON.parse(draft.extraConfigText);
      }
      payload.provider = resolvedProvider;
      payload.provider_config = {...extra, ...draft.providerConfig};
      if (draft.poolSize !== '' && draft.poolSize != null) {
        payload.provider_config.pool_size = normalizeFormValue(draft.poolSize, 'int');
      } else {
        delete payload.provider_config.pool_size;
      }
    }
  } catch (e) {
    setSettingsStatus('invalid JSON in additional provider config', 'error');
    return;
  }

  try {
    const res = await fetch('/api/workspace/execution', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      setSettingsStatus(`error: ${data.error || res.status}`, 'error');
      return;
    }
    state.workspace = data;
    setSettingsStatus('saved', 'ok');
    renderSettings(document.getElementById('settings-body'), state.workspace, state.frontierMeta);
    fetchAll();
  } catch (e) {
    setSettingsStatus('request failed', 'error');
  }
}

function renderFrontierSettings(panel, frontierMeta) {
  // No hero. The strategy list to the left of the detail pane shows the
  // active strategy via its selected state; the rail labels the section.
  panel.innerHTML = `<div class="settings-block"><div id="settings-frontier-form"></div></div>`;
  renderStrategyForm(
    panel.querySelector('#settings-frontier-form'),
    frontierMeta.registry,
    frontierMeta.current,
    frontierMeta.default,
  );
}

// ─── Shared info-icon popover ────────────────────────────
function showTip(anchor, text) {
  const tip = document.getElementById('tip-popover');
  if (!tip) return;
  if (tip.dataset.anchor === anchor.dataset.tipFor && !tip.classList.contains('hidden')) {
    hideTip();
    return;
  }
  tip.dataset.anchor = anchor.dataset.tipFor || '';
  // Render paragraph breaks on blank lines, escape everything else.
  const paragraphs = String(text || '').split(/\n\n+/).map(p => `<p>${esc(p).replace(/\n/g, '<br>')}</p>`);
  tip.innerHTML = paragraphs.join('');
  tip.classList.remove('hidden');
  // Position: below-right of the anchor, clamped inside viewport.
  const r = anchor.getBoundingClientRect();
  const margin = 8;
  tip.style.visibility = 'hidden';
  tip.style.left = '0px';
  tip.style.top = '0px';
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let left = r.right + margin;
  let top = r.top;
  if (left + tw > window.innerWidth - margin) left = Math.max(margin, r.left - tw - margin);
  if (top + th > window.innerHeight - margin) top = Math.max(margin, window.innerHeight - th - margin);
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
  tip.style.visibility = '';
}

function hideTip() {
  const tip = document.getElementById('tip-popover');
  if (tip) {
    tip.classList.add('hidden');
    tip.dataset.anchor = '';
  }
}

document.addEventListener('click', (e) => {
  // Clicks on ? icons are handled by their own listeners (toggle behavior).
  // Anything else, including inside the popover itself, dismisses it.
  if (e.target.closest('.info-icon')) return;
  hideTip();
});

// Belt-and-suspenders: also dismiss on direct popover click, in case some
// intermediate handler stops propagation before it reaches `document`.
document.addEventListener('DOMContentLoaded', () => {
  const tip = document.getElementById('tip-popover');
  if (tip) tip.addEventListener('click', () => hideTip());
});
// If DOMContentLoaded already fired (script loaded late), wire immediately.
(() => {
  const tip = document.getElementById('tip-popover');
  if (tip) tip.addEventListener('click', () => hideTip());
})();

function renderStrategyForm(body, registry, current, defaultStrategy) {
  const kinds = Object.keys(registry);
  let selectedKind = current.kind;

  body.innerHTML = `
    <div class="strategy-form">
      <div id="strategy-picker" class="strategy-picker"></div>
      <div id="strategy-detail" class="strategy-detail"></div>
      <div class="strategy-actions">
        <button id="strategy-reset" class="btn-link" type="button">Reset to default</button>
      </div>
    </div>
  `;

  const pickerHost = body.querySelector('#strategy-picker');
  const detailDiv = body.querySelector('#strategy-detail');

  function renderPicker() {
    // Use the first sentence of each strategy's description as the
    // option's one-liner — the full description still appears in the
    // detail pane below once the option is picked.
    const firstLine = (text) => {
      const s = (text || '').trim();
      const cut = s.search(/[.!?]\s/);
      return cut > 0 ? s.slice(0, cut + 1) : s.split('\n')[0].slice(0, 140);
    };
    const options = kinds.map(k => ({
      value: k,
      label: registry[k].label,
      description: firstLine(registry[k].description),
    }));
    renderRichSelect(pickerHost, {
      options,
      current: selectedKind,
      applied: current.kind,    // server-saved kind; only moves after Save
      ariaLabel: 'Frontier strategy',
      onChange: (kind) => selectKind(kind),
    });
  }

  function selectKind(kind) {
    selectedKind = kind;
    renderPicker();
    renderDetail();
    hideTip();
  }

  function renderDetail() {
    const spec = registry[selectedKind];
    const curParams = selectedKind === current.kind ? (current.params || {}) : {};
    const lines = [];
    // No detail-head: the rich-select trigger above already shows the
    // strategy name. The (?) for the more-detailed explanation anchors
    // inline next to the description.
    const tipKind = spec.detail ? ` <span class="info-icon" data-tip-for="kind:${esc(selectedKind)}">?</span>` : '';
    lines.push(`<div class="strategy-detail-desc">${esc(spec.description)}${tipKind}</div>`);

    if (spec.params && spec.params.length) {
      lines.push('<div class="strategy-detail-params">');
      for (const p of spec.params) {
        const v = curParams[p.name] !== undefined ? curParams[p.name] : p.default;
        const step = p.type === 'int' ? 1 : 'any';
        const tip = p.detail ? ` <span class="info-icon" data-tip-for="param:${esc(p.name)}">?</span>` : '';
        lines.push(`
          <label class="strategy-row" data-param="${esc(p.name)}">
            <span>${esc(p.label)}${tip} <small>(${esc(p.type)}, ${p.min}…${p.max})</small></span>
            <input type="number" step="${step}" min="${p.min}" max="${p.max}"
                   value="${v}" data-name="${esc(p.name)}" data-type="${esc(p.type)}">
          </label>
        `);
      }
      lines.push('</div>');
    } else {
      lines.push('<div class="strategy-empty">No params for this strategy</div>');
    }
    detailDiv.innerHTML = lines.join('');
    wireDetailInputs();
    updateDirtyState();
  }

  function readFormState() {
    const params = {};
    detailDiv.querySelectorAll('input[data-name]').forEach(inp => {
      const n = inp.dataset.name;
      const t = inp.dataset.type;
      const v = t === 'int' ? parseInt(inp.value, 10) : parseFloat(inp.value);
      params[n] = v;
    });
    return {kind: selectedKind, params};
  }

  function isDirty() {
    const form = readFormState();
    if (form.kind !== current.kind) return true;
    const cur = current.params || {};
    const keys = new Set([...Object.keys(form.params), ...Object.keys(cur)]);
    for (const k of keys) {
      if (form.params[k] !== cur[k]) return true;
    }
    return false;
  }

  function updateDirtyState() {
    // Dirty state drives the modal footer's Save button now that Apply
    // has been folded into the shared footer.
    const btn = document.getElementById('settings-footer-save');
    if (!btn) return;
    btn.disabled = !isDirty();
  }

  function wireDetailInputs() {
    // Inputs re-render on each selectKind, so re-wire the dirty-state
    // listener each time. The Apply/Reset buttons live outside and are
    // wired once below.
    detailDiv.querySelectorAll('input[data-name]').forEach(inp => {
      inp.addEventListener('input', updateDirtyState);
    });
  }

  // Footer Save submits via the shared dispatcher; Reset stays local.
  registerSettingsSubmit(async () => {
    await postStrategy(readFormState(), 'applied');
    updateDirtyState();
  });
  const resetBtn = document.getElementById('strategy-reset');
  resetBtn.addEventListener('click', async () => {
    const fallback = defaultStrategy || {kind: 'argmax', params: {}};
    const saved = await postStrategy(fallback, 'reset to default');
    if (saved) selectKind(saved.kind);
  });

  selectKind(current.kind);

  // Info-icon popover: click "?" to open; any other click inside the modal
  // (or outside, handled by the document listener) dismisses the tip. The
  // modal itself has onclick="event.stopPropagation()" in the HTML, so the
  // document handler doesn't see clicks inside the modal -- we dismiss here.
  body.addEventListener('click', (e) => {
    const icon = e.target.closest('.info-icon');
    if (!icon) {
      hideTip();
      return;
    }
    e.preventDefault();   // param `?` sits inside a <label>; block focus-associated-input default
    e.stopPropagation();
    const key = icon.dataset.tipFor;
    let text = '';
    if (key && key.startsWith('kind:')) {
      const k = key.slice(5);
      const s = registry[k];
      text = (s && s.detail) || (s && s.description) || '';
    } else if (key && key.startsWith('param:')) {
      const pname = key.slice(6);
      const p = (registry[selectedKind].params || []).find(x => x.name === pname);
      text = (p && p.detail) || (p && p.label) || '';
    }
    showTip(icon, text);
  });

  async function postStrategy(payload, okMessage) {
    setSettingsStatus('saving...', 'pending');
    try {
      const res = await fetch('/api/frontier-strategy', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        setSettingsStatus(`error: ${data.error || res.status}`, 'error');
        return null;
      }
      current = data;
      if (state.frontierMeta) state.frontierMeta.current = data;
      if (state.workspace) state.workspace.frontier_strategy = data;
      setSettingsStatus(okMessage, 'ok');
      fetchAll();
      setTimeout(() => setSettingsStatus(''), 2000);
      return data;
    } catch (e) {
      setSettingsStatus('request failed', 'error');
      return null;
    }
  }

}

// ─── Main render ─────────────────────────────────────────
// Resize: the scatter sizes itself from the host's clientWidth, so it
// needs a re-render when the window changes. Debounced to coalesce drags.
let _resizeTimer = null;
window.addEventListener('resize', () => {
  if (_resizeTimer) clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(() => { renderScatter(); }, 100);
});

// Signature of every graph + view input that affects timeline/scatter
// geometry or paint. The poll loop re-renders on a 5s cadence; without
// this gate, identical fetches still tear down the canvas and reset
// scroll/zoom because applyTimelineZoom and the scroll-restore writes
// fire on every render. Skipping when nothing changed is the single
// biggest reduction in "jarring view jumps while browsing".
function viewSignature() {
  const g = state.graph;
  const parts = [];
  if (g && g.nodes) {
    const ids = Object.keys(g.nodes).sort();
    for (const id of ids) {
      const n = g.nodes[id];
      parts.push(
        id,
        n.status || '',
        n.score == null ? '' : String(n.score),
        n.parent || '',
        n.commit || '',
        n.pruned_reason || '',
        n.eval_epoch == null ? '' : String(n.eval_epoch),
        n.current_attempt == null ? '' : String(n.current_attempt),
      );
    }
  }
  parts.push(
    '|',
    state.scopeRoot || '',
    state.viewMode || '',
    state.selectedNode || '',
    [...(state.collapsed || [])].sort().join(','),
    (state.activeRunId || ''),
  );
  return parts.join('\x1f');
}

function render() {
  renderTopbar();
  const sig = viewSignature();
  if (state._lastViewSig === sig) return;
  state._lastViewSig = sig;
  renderScatter();
  renderTimeline();
}

// ─── Init ────────────────────────────────────────────────
// Self-rearming poll: only schedule the next fetch after the current one
// finishes. This makes the dashboard graceful under load — slow fetches
// just lower the effective refresh rate instead of stacking.
const POLL_INTERVAL_MS = 5000;
async function pollLoop() {
  await fetchAll();
  state.refreshTimer = setTimeout(pollLoop, POLL_INTERVAL_MS);
}
pollLoop();

// Keyboard shortcuts. We ignore key events whose target is an editable
// element so typing "s" in the settings forms doesn't open the scratchpad.
function isEditableTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    // Close the topmost visible overlay only, so Esc inside a nested
    // sub-modal (e.g., runtime Add variable inside Settings) dismisses
    // the sub-modal first instead of collapsing everything at once.
    // Order is innermost / highest-z first.
    const tip = document.getElementById('tip-popover');
    if (tip && !tip.classList.contains('hidden')) { hideTip(); return; }

    const openSelect = document.querySelector('.rich-select.open');
    if (openSelect) {
      openSelect.querySelector('.rich-select-pop')?.classList.add('hidden');
      openSelect.classList.remove('open');
      return;
    }

    const wsPop = document.getElementById('workspace-popover');
    if (wsPop && !wsPop.classList.contains('hidden')) { wsPop.classList.add('hidden'); return; }

    const miniOverlay = document.getElementById('runtime-env-modal-overlay');
    if (miniOverlay && !miniOverlay.classList.contains('hidden')) {
      miniOverlay.classList.add('hidden');
      return;
    }

    const spawnOverlay = document.getElementById('spawn-overlay');
    if (spawnOverlay && !spawnOverlay.classList.contains('hidden')) { closeSpawnModal(); return; }

    const pruneOverlay = document.getElementById('prune-overlay');
    if (pruneOverlay && !pruneOverlay.classList.contains('hidden')) { closePruneModal(); return; }

    const scratchOverlay = document.getElementById('scratchpad-overlay');
    if (scratchOverlay && !scratchOverlay.classList.contains('hidden')) { closeScratchpad(); return; }

    const settingsOverlay = document.getElementById('settings-overlay');
    if (settingsOverlay && !settingsOverlay.classList.contains('hidden')) { closeSettings(); return; }

    closeDrawer();
  }
  if (e.key === 's' && !e.ctrlKey && !e.metaKey && !state.selectedNode && !isEditableTarget(e.target)) {
    openScratchpad();
  }
});
