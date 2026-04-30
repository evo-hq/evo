/* evo dashboard */

// ─── State ───────────────────────────────────────────────
const state = {
  stats: {},
  graph: { nodes: {} },
  workspace: {},
  frontierMeta: null,
  selectedNode: null,
  expandedTasks: new Set(),
  chart: null,
  refreshTimer: null,
  // Preserve zoom/pan across re-renders
  chartZoom: null,   // { x: {min,max}, y: {min,max} }
  treeTransform: null, // d3.zoomTransform
  treeUserPanned: false, // true once user manually pans/zooms the tree
  tablePage: 0,
  tablePageSize: 10,
  settingsSection: 'project',
};

// ─── Helpers ─────────────────────────────────────────────
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const STATUS_COLORS = {
  committed: cssVar('--green'),
  discarded: cssVar('--text-6'),
  failed: cssVar('--red'),
  active: cssVar('--blue'),
  pruned: cssVar('--text-5'),
  pending: cssVar('--text-5'),
  root: cssVar('--surface-2'),
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
  hetzner: [
    {key: 'token', label: 'API token', type: 'secret'},
    {key: 'server_type', label: 'Server type', type: 'text'},
    {key: 'image', label: 'Image', type: 'text'},
    {key: 'location', label: 'Location', type: 'text'},
    {key: 'ssh_key_name', label: 'SSH key name', type: 'text'},
    {key: 'ssh_user', label: 'SSH user', type: 'text'},
    {key: 'key', label: 'SSH private key', type: 'secret'},
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
async function fetchAll() {
  try {
    const [stats, graph, runs, workspace] = await Promise.all([
      fetch('/api/stats').then(r => r.json()),
      fetch('/api/graph').then(r => r.json()),
      fetch('/api/runs').then(r => r.json()),
      fetch('/api/workspace').then(r => r.json()),
    ]);
    state.stats = stats;
    state.graph = graph;
    state.runs = runs;
    state.workspace = workspace;
    render();
  } catch (e) {
    console.error('fetch error:', e);
  }
}

async function switchRun(runId) {
  await fetch(`/api/runs/${runId}/activate`, { method: 'POST' });
  state.treeUserPanned = false;
  state.treeTransform = null;
  state.chartZoom = null;
  state.tablePage = 0;
  fetchAll();
}

// ─── Render: Top bar ─────────────────────────────────────
function renderTopbar() {
  const s = state.stats;
  document.getElementById('target-file').textContent = s.target || '';
  const pill = document.getElementById('status-pill');
  const text = document.getElementById('status-text');
  if (s.active > 0) {
    pill.className = 'pill pill-active';
    text.textContent = s.active + ' running';
  } else {
    pill.className = 'pill pill-idle';
    text.textContent = s.total_experiments > 0 ? 'Idle' : 'No experiments';
  }

  // Run switcher: always render as a <select> so the affordance is consistent,
  // even when there's only one run.
  const runs = state.runs || [];
  const switcher = document.getElementById('run-switcher');
  if (runs.length > 0) {
    const options = runs.map(r =>
      `<option value="${r.id}" ${r.active ? 'selected' : ''}>${r.id}</option>`
    ).join('');
    switcher.innerHTML = `<select class="run-select" onchange="switchRun(this.value)">${options}</select>`;
    switcher.classList.remove('hidden');
  } else {
    switcher.classList.add('hidden');
  }
}

function renderOverview() {
  const s = state.stats || {};
  const ws = state.workspace || {};
  const items = [
    {label: 'Epoch', value: s.eval_epoch || 1, tip: 'Current evaluation pass for this run.'},
    {label: 'Metric', value: s.metric || 'max', tip: 'Optimization direction for scores.'},
    {label: 'Host', value: ws.host || '--', tip: 'Configured workspace host label.'},
    {label: 'Backend', value: backendLabel(ws.default_backend), tip: 'Default backend new experiments inherit.'},
    {label: 'Refresh', value: 'on', tip: 'Dashboard auto-refresh is enabled.'},
  ];
  document.getElementById('overview-body').innerHTML = items.map((item) => `
    <div class="overview-chip" title="${esc(item.tip)}">
      <span class="overview-label">${esc(item.label)}</span>
      <span class="overview-value">${esc(String(item.value))}</span>
    </div>
  `).join('');
}

// ─── Render: Hero ────────────────────────────────────────
function renderHero() {
  const s = state.stats;
  document.getElementById('best-score').textContent =
    s.best_score != null ? s.best_score.toFixed(2) : '--';

  if (s.baseline_score != null && s.best_score != null && s.baseline_score !== s.best_score) {
    const improvement = ((s.best_score - s.baseline_score) / s.baseline_score * 100);
    document.getElementById('score-delta').textContent = '+' + Math.round(improvement) + '%';
    document.getElementById('baseline-info').textContent = 'from ' + s.baseline_score.toFixed(2) + ' baseline';
  } else {
    document.getElementById('score-delta').textContent = '';
    document.getElementById('baseline-info').textContent = '';
  }

  document.getElementById('total-exp').textContent = s.total_experiments || 0;
  document.getElementById('exp-breakdown').innerHTML =
    `<span class="kept">${s.committed || 0} kept</span>` +
    `<span class="skip">${s.discarded || 0} skip</span>` +
    `<span class="err">${s.failed || 0} err</span>`;

  const total = s.total_experiments || 0;
  const committed = s.committed || 0;
  document.getElementById('keep-rate').textContent = total > 0 ? pct(committed, total) : '--';
  document.getElementById('keep-detail').textContent = total > 0 ? `${committed} of ${total}` : '';

  document.getElementById('frontier-count').textContent = s.frontier || 0;

  const activeEl = document.getElementById('active-count');
  activeEl.textContent = s.active || 0;
  activeEl.className = 'hero-num' + (s.active > 0 ? ' blue' : '');
}

// ─── Render: Score chart ─────────────────────────────────
function renderChart() {
  const experiments = getExperiments()
    .filter(n => n.score != null)
    .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));

  const metric = state.stats.metric || 'max';
  const isMax = metric === 'max';
  const colorGreen = cssVar('--green');
  const colorBlue = cssVar('--blue');
  const colorRed = cssVar('--red');
  const colorText4 = cssVar('--text-1');
  const colorText5 = cssVar('--text-2');
  const colorBorder = 'rgba(63,63,70,0.95)';
  const colorSurface = cssVar('--surface-2');
  const colorText1 = cssVar('--text-1');
  const colorText2 = cssVar('--text-1');

  // Build running best staircase
  let runningBest = null;
  const staircaseData = [];
  const committedExps = experiments.filter(n => n.status === 'committed');
  committedExps.forEach((n, i) => {
    if (runningBest == null || (isMax ? n.score > runningBest : n.score < runningBest)) {
      runningBest = n.score;
    }
    staircaseData.push({ x: i, y: runningBest });
  });

  // All experiments as scatter
  const allData = experiments.map((n, i) => ({
    x: i,
    y: n.score,
    status: n.status,
    id: n.id,
    hypothesis: n.hypothesis,
  }));

  const ctx = document.getElementById('score-chart');
  // Save zoom state before destroying
  if (state.chart) {
    const {x, y} = state.chart.scales;
    if (x && y) {
      state.chartZoom = { x: { min: x.min, max: x.max }, y: { min: y.min, max: y.max } };
    }
    state.chart.destroy();
  }

  state.chart = new Chart(ctx, {
    type: 'scatter',
    data: {
      datasets: [
        {
          label: staircaseData.length > 1 ? 'Running best' : '',
          data: staircaseData,
          type: 'line',
          borderColor: staircaseData.length > 1 ? colorGreen : 'transparent',
          borderWidth: 2,
          pointRadius: 0,
          stepped: 'before',
          fill: false,
          order: 2,
        },
        {
          label: 'Committed',
          data: allData.filter(d => d.status === 'committed'),
          backgroundColor: colorGreen,
          pointRadius: 5,
          order: 1,
        },
        {
          label: 'Discarded',
          data: allData.filter(d => d.status === 'discarded'),
          backgroundColor: colorText5,
          pointRadius: 3.5,
          order: 1,
        },
        {
          label: 'Failed',
          data: allData.filter(d => d.status === 'failed'),
          backgroundColor: colorRed,
          pointRadius: 3.5,
          order: 1,
        },
        {
          label: 'Active',
          data: allData.filter(d => d.status === 'active'),
          backgroundColor: colorBlue,
          pointRadius: 5,
          pointStyle: 'rectRot',
          order: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        zoom: {
          pan: {
            enabled: true,
            mode: 'xy',
          },
          zoom: {
            wheel: { enabled: true },
            drag: {
              enabled: true,
              backgroundColor: 'rgba(105,168,255,0.12)',
              borderColor: 'rgba(105,168,255,0.35)',
              borderWidth: 1,
              modifierKey: 'shift',
            },
            mode: 'xy',
          },
        },
        legend: {
          display: true,
          position: 'top',
          align: 'end',
          labels: {
            color: colorText4,
            font: { size: 11 },
            boxWidth: 8,
            boxHeight: 8,
            usePointStyle: true,
            pointStyle: 'circle',
            padding: 14,
          },
        },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const d = ctx.raw;
              if (d.id) return `${d.id} | ${d.y} | ${d.status}`;
              return `Score: ${d.y}`;
            },
          },
          backgroundColor: colorSurface,
          borderColor: cssVar('--border-subtle'),
          borderWidth: 1,
          titleColor: colorText1,
          bodyColor: colorText2,
          bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
        },
      },
      scales: {
        x: {
          min: 0,
          suggestedMax: Math.max(allData.length - 1, 1),
          offset: true,
          title: { display: true, text: 'experiment #', color: colorText4, font: { size: 11 } },
          grid: { color: colorBorder },
          ticks: {
            color: colorText4,
            font: { family: "'JetBrains Mono', monospace", size: 11 },
            stepSize: 1,
            callback: (v) => v >= 0 ? Math.round(v) : '',
          },
        },
        y: {
          suggestedMin: 0,
          suggestedMax: 1,
          title: { display: false },
          grid: { color: colorBorder },
          ticks: {
            color: colorText4,
            font: { family: "'JetBrains Mono', monospace", size: 11 },
          },
        },
      },
      onClick: (e, elements) => {
        if (elements.length > 0) {
          const d = elements[0].element.$context.raw;
          if (d && d.id) openDrawer(d.id);
        }
      },
    },
  });

  // Double-click canvas to reset zoom
  ctx.ondblclick = () => {
    if (state.chart) { state.chart.resetZoom(); state.chartZoom = null; }
  };

  // Restore zoom state from before re-render
  if (state.chartZoom) {
    state.chart.zoomScale('x', state.chartZoom.x, 'none');
    state.chart.zoomScale('y', state.chartZoom.y, 'none');
    state.chart.update('none');
  }
}

// ─── Render: D3 Tree ─────────────────────────────────────
function renderTree() {
  const container = document.getElementById('tree-container');
  const svg = d3.select('#tree-svg');

  // Save current zoom transform before clearing (only if user panned)
  if (state.treeUserPanned) {
    try { state.treeTransform = d3.zoomTransform(svg.node()); } catch(e) {}
  }

  svg.selectAll('*').remove();

  const nodes = state.graph.nodes;
  if (!nodes.root) return;

  // Build hierarchy
  function buildChildren(nodeId) {
    const node = nodes[nodeId];
    if (!node) return null;
    const children = (node.children || [])
      .map(cid => buildChildren(cid))
      .filter(Boolean);
    return { ...node, children: children.length > 0 ? children : undefined };
  }
  const rootData = buildChildren('root');
  if (!rootData) return;

  const root = d3.hierarchy(rootData);
  const width = container.clientWidth || 400;
  const height = container.clientHeight || 350;

  const treeLayout = d3.tree().nodeSize([40, 60]);
  treeLayout(root);

  // Find tree bounds
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  root.each(d => {
    minX = Math.min(minX, d.x); maxX = Math.max(maxX, d.x);
    minY = Math.min(minY, d.y); maxY = Math.max(maxY, d.y);
  });
  const treeW = maxX - minX || 1;
  const treeH = maxY - minY || 1;
  const treeCX = minX + treeW / 2;
  const treeCY = minY + treeH / 2;

  const g = svg.append('g');

  // Pan + zoom
  const zoom = d3.zoom()
    .scaleExtent([0.3, 4])
    .on('zoom', (e) => {
      g.attr('transform', e.transform);
      // Only save transform if triggered by user interaction (not programmatic)
      if (e.sourceEvent) {
        state.treeTransform = e.transform;
        state.treeUserPanned = true;
      }
    });

  svg.call(zoom)
    .on('dblclick.zoom', null); // disable default dblclick zoom

  // Center tree in container with a comfortable zoom level
  const defaultScale = 1.5;
  const initialTransform = d3.zoomIdentity
    .translate(width / 2 - treeCX * defaultScale, height / 2 - treeCY * defaultScale)
    .scale(defaultScale);

  // Only reuse saved transform if user explicitly panned/zoomed AND container size is stable
  const useTransform = (state.treeUserPanned && state.treeTransform) ? state.treeTransform : initialTransform;
  svg.call(zoom.transform, useTransform);

  // Double-click to reset
  svg.on('dblclick', () => {
    state.treeTransform = null;
    state.treeUserPanned = false;
    svg.transition().duration(300).call(zoom.transform, initialTransform);
  });

  // Links: draw from parent dot edge to child dot edge (not through the dot)
  g.selectAll('.tree-link')
    .data(root.links())
    .join('path')
    .attr('class', 'tree-link')
    .attr('d', d => {
      const sr = d.source.data.id === 'root' ? 4 : (d.source.data.status === 'committed' ? 7 : 5);
      const tr = d.target.data.status === 'committed' ? 7 : 5;
      const sx = d.source.x, sy = d.source.y + sr;
      const tx = d.target.x, ty = d.target.y - tr;
      const my = (sy + ty) / 2;
      return `M${sx},${sy} L${sx},${my} L${tx},${my} L${tx},${ty}`;
    })
    .attr('stroke', d => {
      const child = d.target.data;
      if (child.status === 'committed') return cssVar('--green');
      if (child.status === 'active') return cssVar('--blue');
      return 'rgba(113,113,122,0.95)';
    })
    .attr('opacity', d => d.target.data.status === 'committed' ? 0.95 : 0.85);

  // Nodes
  const nodeG = g.selectAll('.tree-node')
    .data(root.descendants())
    .join('g')
    .attr('class', 'tree-node')
    .attr('transform', d => `translate(${d.x},${d.y})`)
    .on('click', (e, d) => {
      if (d.data.id !== 'root') openDrawer(d.data.id);
    });

  // Solid filled circles -- size and fill by status
  nodeG.append('circle')
    .attr('r', d => {
      if (d.data.id === 'root') return 4;
      if (d.data.status === 'committed') return 7;
      return 5;
    })
    .attr('fill', d => STATUS_COLORS[d.data.status] || cssVar('--text-6'))
    .attr('opacity', d => {
      if (d.data.status === 'committed' || d.data.status === 'active') return 1;
      if (d.data.id === 'root') return 0.5;
      return 0.5;
    });

  // Labels offset to the right: ID on top, score below (only for committed)
  // Skip root label
  const labelG = nodeG.filter(d => d.data.id !== 'root');

  // ID label
  labelG.append('text')
    .attr('x', d => d.data.status === 'committed' ? 12 : 10)
    .attr('dy', d => (d.data.status === 'committed' && d.data.score != null) ? '-0.15em' : '0.35em')
    .attr('fill', d => {
      if (d.data.status === 'committed') return cssVar('--text-2');
      if (d.data.status === 'active') return cssVar('--blue');
      if (d.data.status === 'failed') return cssVar('--red');
      return cssVar('--text-1');
    })
    .attr('font-size', '9px')
    .attr('font-weight', d => d.data.status === 'committed' ? '500' : '400')
    .text(d => shortId(d.data.id));

  // Score label below ID (only for committed nodes with a score)
  labelG.filter(d => d.data.status === 'committed' && d.data.score != null)
    .append('text')
    .attr('x', 12)
    .attr('dy', '1em')
    .attr('fill', cssVar('--text-2'))
    .attr('font-size', '9px')
    .text(d => d.data.score.toFixed(2));
}

// ─── Render: Table ───────────────────────────────────────
function renderTable() {
  const s = state.stats;
  const filters = document.getElementById('table-filters');
  filters.innerHTML =
    `<span class="filter-pill kept">kept ${s.committed || 0}</span>` +
    `<span class="filter-pill skip">skip ${s.discarded || 0}</span>` +
    `<span class="filter-pill err">err ${s.failed || 0}</span>` +
    `<span class="filter-pill active-f">active ${s.active || 0}</span>`;

  const tbody = document.getElementById('table-body');
  const experiments = getExperiments();
  const totalPages = Math.max(1, Math.ceil(experiments.length / state.tablePageSize));
  if (state.tablePage >= totalPages) state.tablePage = totalPages - 1;
  const start = state.tablePage * state.tablePageSize;
  const pageExps = experiments.slice(start, start + state.tablePageSize);

  tbody.innerHTML = pageExps.map(n => {
    const delta = scoreDelta(n);
    const deltaClass = delta.startsWith('+') && delta !== '+0.00' ? 'color:var(--green)' :
                       delta.startsWith('-') ? 'color:var(--red)' : 'color:var(--text-4)';
    const scoreHtml = n.score != null
      ? `<span class="score-val">${n.score.toFixed(2)}</span>${delta ? `<span class="score-delta" style="${deltaClass}">${delta}</span>` : ''}`
      : n.status === 'failed' ? '<span style="color:var(--red)">err</span>' : '<span style="color:var(--text-5)">&mdash;</span>';

    const tasks = n.benchmark_result?.tasks;
    let taskStr = '--';
    let taskStyle = '';
    if (tasks) {
      const total = Object.keys(tasks).length;
      const passed = Object.values(tasks).filter(v => v >= 0.5).length;
      taskStr = `${passed}/${total}`;
      if (passed < total) taskStyle = 'color:var(--red)';
      else taskStyle = 'color:var(--text-1)';
    }

    const statusColor = STATUS_COLORS[n.status] || '#52525b';
    const rowClass = n.status === 'active' ? 'active-row' : '';
    const rowStatusClass = 'row-' + n.status;
    const parentId = n.parent === 'root' ? 'root' : shortId(n.parent);

    return `<div class="table-row ${rowClass} ${rowStatusClass}" onclick="openDrawer('${n.id}')">
      <span class="col-id">${shortId(n.id)}</span>
      <span class="col-score">${scoreHtml}</span>
      <span class="col-status"><span class="status-dot" style="background:${statusColor}"></span>${statusLabel(n.status)}</span>
      <span class="col-parent">${parentId}</span>
      <span class="col-hyp">${n.hypothesis || ''}</span>
      <span class="col-tasks" style="${taskStyle}">${taskStr}</span>
      <span class="col-time">${relTime(n.created_at)}</span>
    </div>`;
  }).join('');

  // Pagination controls
  const pager = document.getElementById('table-pager');
  if (totalPages > 1) {
    pager.innerHTML = `
      <button onclick="tablePrev()" ${state.tablePage === 0 ? 'disabled' : ''}>Prev</button>
      <span>${state.tablePage + 1} / ${totalPages}</span>
      <button onclick="tableNext()" ${state.tablePage >= totalPages - 1 ? 'disabled' : ''}>Next</button>
    `;
    pager.classList.remove('hidden');
  } else {
    pager.innerHTML = '';
    pager.classList.add('hidden');
  }
}

function tablePrev() { state.tablePage = Math.max(0, state.tablePage - 1); renderTable(); }
function tableNext() { state.tablePage++; renderTable(); }

// ─── Drawer ──────────────────────────────────────────────
async function openDrawer(expId) {
  state.selectedNode = expId;
  state.expandedTasks.clear();
  const overlay = document.getElementById('drawer-overlay');
  const content = document.getElementById('drawer-content');
  overlay.classList.remove('hidden');

  const node = state.graph.nodes[expId];
  if (!node) return;
  const ws = state.workspace || {};

  const parent = state.graph.nodes[node.parent];
  const delta = scoreDelta(node);
  const deltaColor = delta.startsWith('+') && delta !== '+0.00' ? 'var(--green)' :
                     delta.startsWith('-') ? 'var(--red)' : 'var(--text-4)';
  const statusColor = STATUS_COLORS[node.status] || '#52525b';

  let html = `
    <div class="drawer-header">
      <span class="drawer-back" onclick="closeDrawer()">&larr;</span>
      <span class="drawer-id">${node.id}</span>
      <span class="pill" style="background:${statusColor}15; color:${statusColor}">
        <span class="dot" style="background:${statusColor}"></span>
        ${statusLabel(node.status)}
      </span>
      <div class="spacer"></div>
      <span class="drawer-close" onclick="closeDrawer()">&times;</span>
    </div>`;

  // Score
  html += `<div class="drawer-section" style="padding:20px">
    <div style="display:flex;align-items:baseline">
      <span class="drawer-score">${node.score != null ? node.score.toFixed(2) : '--'}</span>
      ${delta ? `<span class="drawer-score-delta" style="color:${deltaColor}">${delta} from ${shortId(node.parent)}</span>` : ''}
    </div>
    ${node.status === 'committed' ? '<span style="font-size:12px;color:var(--text-4);margin-top:4px;display:block">Score improved. Gate passed. Changes committed.</span>' : ''}
    ${node.status === 'discarded' ? '<span style="font-size:12px;color:var(--text-4);margin-top:4px;display:block">Score did not improve vs parent. Discarded.</span>' : ''}
    ${node.status === 'failed' ? `<span style="font-size:12px;color:var(--red);margin-top:4px;display:block">Failed: ${esc(node.error || 'benchmark or gate error')}</span>` : ''}
  </div>`;

  // Metadata
  html += `<div class="drawer-section">
    <div class="drawer-meta-row"><span class="drawer-meta-key">Parent</span><span class="drawer-meta-val mono" style="color:var(--indigo)">${node.parent}</span></div>
    <div class="drawer-meta-row"><span class="drawer-meta-key">Branch</span><span class="drawer-meta-val mono">${node.branch || '--'}</span></div>
    <div class="drawer-meta-row"><span class="drawer-meta-key">Epoch</span><span class="drawer-meta-val">${node.eval_epoch || '--'}</span></div>
    <div class="drawer-meta-row"><span class="drawer-meta-key">Backend</span><span class="drawer-meta-val mono">${backendLabel(node.resolved_backend || ws.default_backend)}</span></div>
    <div class="drawer-meta-row"><span class="drawer-meta-key">Created</span><span class="drawer-meta-val">${relTime(node.created_at)} ago</span></div>
    ${node.children?.length ? `<div class="drawer-meta-row"><span class="drawer-meta-key">Children</span><span class="drawer-meta-val mono" style="color:var(--indigo)">${node.children.join(', ')}</span></div>` : ''}
  </div>`;

  const backendSpec = node.resolved_backend || ws.default_backend;
  if (backendSpec) {
    const backendConfig = backendSpec.config || {};
    const backendJson = prettyJson(backendConfig);
    html += `<div class="drawer-section">
      <span class="drawer-section-title">Execution Backend</span>
      <div class="drawer-meta-row"><span class="drawer-meta-key">Source</span><span class="drawer-meta-val">${backendSpec.source === 'override' ? 'per-experiment override' : 'workspace default'}</span></div>
      ${backendSpec.state_key ? `<div class="drawer-meta-row"><span class="drawer-meta-key">State key</span><span class="drawer-meta-val mono">${backendSpec.state_key}</span></div>` : ''}
      <div class="drawer-meta-row"><span class="drawer-meta-key">Worktree</span><span class="drawer-meta-val mono">${esc(node.worktree || '--')}</span></div>
      <pre class="config-pre">${esc(backendJson)}</pre>
    </div>`;
  }

  // Hypothesis
  if (node.hypothesis) {
    html += `<div class="drawer-section">
      <span class="drawer-section-title">Hypothesis</span>
      <div class="drawer-hyp">${node.hypothesis}</div>
    </div>`;
  }

  // Diff
  try {
    const diff = await fetch(`/api/node/${expId}/log/diff.patch`).then(r => r.text());
    if (diff.trim()) {
      const diffHtml = diff.split('\n').map(line => {
        if (line.startsWith('@@')) return `<span class="diff-hunk">${esc(line)}</span>`;
        if (line.startsWith('+')) return `<span class="diff-add">${esc(line)}</span>`;
        if (line.startsWith('-')) return `<span class="diff-del">${esc(line)}</span>`;
        return `<span class="diff-ctx">${esc(line)}</span>`;
      }).join('');
      html += `<div class="drawer-section">
        <span class="drawer-section-title">Code Changes</span>
        <div class="diff-block">${diffHtml}</div>
      </div>`;
    }
  } catch (e) { /* no diff */ }

  // Tasks -- from completed benchmark result, or live traces for active experiments
  let traces = {};
  try {
    traces = await fetch(`/api/node/${expId}/traces`).then(r => r.json());
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

      tasksHtml += `<div class="task-row" onclick="toggleTask(this, '${expId}', '${tid}')">
        <span class="task-dot" style="background:${color}"></span>
        <span class="task-id">task ${tid}</span>
        <span class="task-summary">${summary}</span>
        ${duration ? `<span class="task-duration">${duration}</span>` : ''}
        <span class="task-score" style="color:${color}">${score.toFixed(1)}</span>
      </div>`;

      // Trace detail (hidden by default, toggled by click)
      if (trace) {
        let traceHtml = '<div class="trace-detail hidden" data-task="' + tid + '">';
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
        if (trace.failure_reason) {
          traceHtml += `<div class="failure-box">
            <span class="failure-box-title">Failure: ${trace.failure_reason}</span>
            ${trace.summary ? `<div class="failure-box-text">${esc(trace.summary)}</div>` : ''}
          </div>`;
        }
        if (trace.events?.length) {
          for (const ev of trace.events) {
            const role = ev.role || ev.name || 'event';
            const roleClass = role === 'user' ? 'user' : role === 'assistant' ? 'agent' : 'tool';
            const content = ev.content || JSON.stringify(ev.attributes || ev, null, 2);
            traceHtml += `<div class="trace-msg">
              <div class="trace-role ${roleClass}">${role}</div>
              <div class="trace-content">${esc(content).substring(0, 500)}</div>
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
        <span class="mono" style="font-size:11px;color:var(--text-1);font-weight:500">${label}</span>
      </div>
      ${tasksHtml}
    </div>`;
  } else if (isActive) {
    html += `<div class="drawer-section">
      <span class="drawer-section-title">Benchmark Tasks</span>
      <div style="font-size:12px;color:var(--text-4)">Running... waiting for first task to complete.</div>
    </div>`;
  }

  content.innerHTML = html;
}

function toggleTask(el, expId, taskId) {
  const detail = el.nextElementSibling;
  if (detail && detail.classList.contains('trace-detail')) {
    detail.classList.toggle('hidden');
  }
}

function closeDrawer() {
  document.getElementById('drawer-overlay').classList.add('hidden');
  state.selectedNode = null;
}

function esc(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
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
  body.innerHTML = `
    <div class="settings-shell">
      <aside class="settings-nav" aria-label="Settings sections">
        <button class="settings-nav-item ${state.settingsSection === 'project' ? 'selected' : ''}" data-section="project" type="button">
          <span class="settings-nav-title">Project</span>
          <span class="settings-nav-sub">workspace facts</span>
        </button>
        <button class="settings-nav-item ${state.settingsSection === 'execution' ? 'selected' : ''}" data-section="execution" type="button">
          <span class="settings-nav-title">Execution</span>
          <span class="settings-nav-sub">provider + runtime</span>
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
  if (state.settingsSection === 'project') {
    renderProjectSettings(panel, ws);
  } else if (state.settingsSection === 'execution') {
    renderExecutionSettings(panel, ws);
  } else {
    renderFrontierSettings(panel, frontierMeta);
  }
}

function renderProjectSettings(panel, ws) {
  panel.innerHTML = `
    <div class="settings-hero">
      <div>
        <div class="settings-section-title">Project</div>
        <div class="settings-section-sub">what this workspace optimizes</div>
      </div>
      <div class="settings-hero-badge mono">${esc(backendLabel(ws.default_backend))}</div>
    </div>
    <div class="settings-rows">
      <div class="settings-row"><span>target</span><strong class="mono">${esc(ws.target || '--')}</strong></div>
      <div class="settings-row"><span>metric</span><strong>${esc(ws.metric || '--')}</strong></div>
      <div class="settings-row"><span>host</span><strong>${esc(ws.host || '--')}</strong></div>
      <div class="settings-row"><span>commit strategy</span><strong>${esc(ws.commit_strategy || '--')}</strong></div>
      <div class="settings-row"><span>keyfile</span><strong>${ws.keyfile_present ? 'present' : 'missing'}</strong></div>
    </div>
    <div class="settings-block">
      <div class="settings-block-label">Benchmark</div>
      <pre class="settings-pre">${esc(ws.benchmark || '--')}</pre>
    </div>
    ${ws.gate ? `<div class="settings-block">
      <div class="settings-block-label">Gate</div>
      <pre class="settings-pre">${esc(ws.gate)}</pre>
    </div>` : ''}
    <div class="settings-block">
      <div class="settings-block-label">Default execution</div>
      <div class="settings-inline mono">${esc(backendLabel(ws.default_backend))}</div>
    </div>
  `;
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

  panel.innerHTML = `
    <div class="settings-hero">
      <div>
        <div class="settings-section-title">Execution</div>
        <div class="settings-section-sub">where new experiments run</div>
      </div>
      <div class="settings-hero-badge mono">${esc(defaultSpec.name || 'worktree')}</div>
    </div>
    <div id="execution-form"></div>
  `;

  const formHost = panel.querySelector('#execution-form');

  function renderForm() {
    const resolvedProvider = draft.providerChoice === '__custom__' ? draft.providerName : draft.providerChoice;
    const fields = providerFields(resolvedProvider);
    const basicFields = fields.filter(field => !field.advanced);
    const advancedFields = fields.filter(field => field.advanced);
    const defaultRuntime = (ws.backend_configs || []).find(spec => spec.is_default)?.runtime;
    formHost.innerHTML = `
      <div class="settings-block">
        <div class="settings-block-label">Default execution</div>
        <div class="segmented-choice">
          ${['worktree', 'pool', 'remote'].map(kind => `
            <button class="segmented-choice-item ${draft.backend === kind ? 'selected' : ''}" type="button" data-backend-choice="${kind}">
              <span>${kind}</span>
            </button>
          `).join('')}
        </div>
        <div class="settings-help">Choose the backend new experiments inherit.</div>
      </div>
      <div id="settings-backend-detail"></div>
      <div class="settings-actions">
        <span id="settings-exec-status" class="strategy-status"></span>
        <span class="spacer"></span>
        <button id="settings-exec-save" class="btn-primary" type="button">Save execution settings</button>
      </div>
    `;
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
          <label class="settings-field">
            <span>Pool size</span>
            <input id="settings-pool-size" class="settings-input" type="number" step="1" min="1" value="${esc(String(draft.poolSize ?? ''))}" placeholder="unbounded">
            <small>Leave blank for unbounded concurrent sandboxes.</small>
          </label>
        </div>
          <div class="settings-block">
            <div class="settings-block-label">Remote provider</div>
            <select id="settings-provider-choice" class="settings-select">
            ${['modal', 'e2b', 'ssh', 'daytona', 'aws', 'hetzner', 'manual'].map(provider => `<option value="${provider}" ${draft.providerChoice === provider ? 'selected' : ''}>${provider}</option>`).join('')}
            <option value="__custom__" ${draft.providerChoice === '__custom__' ? 'selected' : ''}>custom</option>
          </select>
          <div class="settings-help">Pick the provider to use for new remote experiments.</div>
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
        <details class="settings-advanced" open>
          <summary>Current runtime</summary>
          <div class="settings-advanced-body">
            ${defaultRuntime ? renderBackendRuntimeCard({...ws.default_backend, is_default: true, node_ids: [], active_node_ids: [], runtime: defaultRuntime}) : '<div class="config-runtime-empty">no runtime state yet for the current default backend</div>'}
          </div>
        </details>
      `;
    } else {
      detail.innerHTML = `<div class="config-runtime-empty">Worktree mode creates a fresh local git worktree per experiment.</div>`;
    }
    formHost.querySelectorAll('[data-backend-choice]').forEach((btn) => {
      btn.addEventListener('click', () => {
        collectDraft();
        draft.backend = btn.dataset.backendChoice;
        renderForm();
      });
    });
    const providerChoiceSelect = formHost.querySelector('#settings-provider-choice');
    if (providerChoiceSelect) {
      providerChoiceSelect.addEventListener('change', () => {
        collectDraft();
        draft.providerChoice = providerChoiceSelect.value;
        if (draft.providerChoice !== '__custom__') draft.providerName = draft.providerChoice;
        renderForm();
      });
    }
    const saveBtn = formHost.querySelector('#settings-exec-save');
    saveBtn.addEventListener('click', async () => {
      collectDraft();
      await saveExecutionSettings(draft, formHost.querySelector('#settings-exec-status'), panel);
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
      <label class="settings-field checkbox settings-row">
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
  return `
    <label class="settings-field">
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

async function saveExecutionSettings(draft, statusEl, panel) {
  statusEl.textContent = 'saving...';
  let payload = {backend: draft.backend};
  try {
    if (draft.backend === 'pool') {
      payload.workspaces = draft.workspaces;
    } else if (draft.backend === 'remote') {
      const resolvedProvider = draft.providerChoice === '__custom__' ? draft.providerName : draft.providerChoice;
      if (!resolvedProvider) {
        statusEl.textContent = 'provider is required';
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
    statusEl.textContent = 'invalid JSON in additional provider config';
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
      statusEl.textContent = `error: ${data.error || res.status}`;
      return;
    }
    state.workspace = data;
    statusEl.textContent = 'saved';
    renderSettings(document.getElementById('settings-body'), state.workspace, state.frontierMeta);
    fetchAll();
  } catch (e) {
    statusEl.textContent = 'request failed';
  }
}

function renderFrontierSettings(panel, frontierMeta) {
  panel.innerHTML = `
    <div class="settings-hero">
      <div>
        <div class="settings-section-title">Frontier</div>
        <div class="settings-section-sub">how evo picks the next branch to explore</div>
      </div>
      <div class="settings-hero-badge mono">${esc(frontierMeta.current.kind || 'default')}</div>
    </div>
    <div class="settings-block">
      <div id="settings-frontier-form"></div>
    </div>
  `;
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

  const html = [];
  html.push('<div class="strategy-form">');
  html.push('<div class="strategy-split">');
  html.push('<div class="strategy-list">');
  for (const k of kinds) {
    const spec = registry[k];
    html.push(`
      <div class="strategy-item" data-kind="${esc(k)}">
        <span class="strategy-item-label">${esc(spec.label)}</span>
      </div>
    `);
  }
  html.push('</div>');
  html.push('<div id="strategy-detail" class="strategy-detail"></div>');
  html.push('</div>');  // end split
  // Action row is a sibling of the split -- pinned at the bottom of the
  // fixed-height form while split panes scroll independently.
  html.push('<div class="strategy-actions">');
  html.push('<button id="strategy-reset" class="btn-link" type="button">Reset to default</button>');
  html.push('<span id="strategy-status" class="strategy-status"></span>');
  html.push('<span class="spacer"></span>');
  html.push('<button id="strategy-apply" class="btn-primary" disabled>Apply</button>');
  html.push('</div>');
  html.push('</div>');  // end form
  body.innerHTML = html.join('');

  const listDiv = body.querySelector('.strategy-list');
  const detailDiv = document.getElementById('strategy-detail');

  function selectKind(kind) {
    selectedKind = kind;
    listDiv.querySelectorAll('.strategy-item').forEach(el => {
      el.classList.toggle('selected', el.dataset.kind === kind);
    });
    renderDetail();
    hideTip();
  }

  function renderDetail() {
    const spec = registry[selectedKind];
    const curParams = selectedKind === current.kind ? (current.params || {}) : {};
    const lines = [];
    const tipKind = spec.detail ? ` <span class="info-icon" data-tip-for="kind:${esc(selectedKind)}">?</span>` : '';
    lines.push(`<div class="strategy-detail-head">${esc(spec.label)}${tipKind}</div>`);
    lines.push(`<div class="strategy-detail-desc">${esc(spec.description)}</div>`);

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
    const btn = document.getElementById('strategy-apply');
    if (!btn) return;
    const dirty = isDirty();
    btn.disabled = !dirty;
    btn.classList.toggle('dirty', dirty);
  }

  function wireDetailInputs() {
    // Inputs re-render on each selectKind, so re-wire the dirty-state
    // listener each time. The Apply/Reset buttons live outside and are
    // wired once below.
    detailDiv.querySelectorAll('input[data-name]').forEach(inp => {
      inp.addEventListener('input', updateDirtyState);
    });
  }

  // Apply/Reset buttons live outside the split, wired once.
  const applyBtn = document.getElementById('strategy-apply');
  applyBtn.addEventListener('click', async () => {
    const statusEl = document.getElementById('strategy-status');
    await postStrategy(readFormState(), statusEl, 'applied');
    updateDirtyState();
  });
  const resetBtn = document.getElementById('strategy-reset');
  resetBtn.addEventListener('click', async () => {
    const statusEl = document.getElementById('strategy-status');
    const fallback = defaultStrategy || {kind: 'argmax', params: {}};
    const saved = await postStrategy(fallback, statusEl, 'reset to default');
    if (saved) selectKind(saved.kind);
  });

  // Wire item clicks.
  listDiv.addEventListener('click', (e) => {
    const item = e.target.closest('.strategy-item');
    if (!item) return;
    selectKind(item.dataset.kind);
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

  async function postStrategy(payload, statusEl, okMessage) {
    statusEl.textContent = 'saving...';
    try {
      const res = await fetch('/api/frontier-strategy', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        statusEl.textContent = `error: ${data.error || res.status}`;
        return null;
      }
      current = data;
      if (state.frontierMeta) state.frontierMeta.current = data;
      if (state.workspace) state.workspace.frontier_strategy = data;
      statusEl.textContent = okMessage;
      fetchAll();
      setTimeout(() => { statusEl.textContent = ''; }, 2000);
      return data;
    } catch (e) {
      statusEl.textContent = 'request failed';
      return null;
    }
  }

  // Apply / Reset buttons are wired inside renderDetail -> wireDetailHandlers
  // since they're re-created each render.
}

// ─── Main render ─────────────────────────────────────────
function render() {
  renderTopbar();
  renderHero();
  renderOverview();
  renderChart();
  renderTree();
  renderTable();
}

// ─── Init ────────────────────────────────────────────────
fetchAll();
state.refreshTimer = setInterval(fetchAll, 5000);

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    hideTip();
    closeDrawer();
    closeSettings();
    closeScratchpad();
  }
  if (e.key === 's' && !e.ctrlKey && !e.metaKey && !state.selectedNode) {
    openScratchpad();
  }
});
