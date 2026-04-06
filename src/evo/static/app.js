/* evo dashboard */

// ─── State ───────────────────────────────────────────────
const state = {
  stats: {},
  graph: { nodes: {} },
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
};

// ─── Helpers ─────────────────────────────────────────────
const STATUS_COLORS = {
  committed: '#22c55e',
  discarded: '#3f3f46',
  failed: '#ef4444',
  active: '#3b82f6',
  pruned: '#78716c',
  pending: '#52525b',
  root: '#27272a',
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

// ─── API ─────────────────────────────────────────────────
async function fetchAll() {
  try {
    const [stats, graph, runs] = await Promise.all([
      fetch('/api/stats').then(r => r.json()),
      fetch('/api/graph').then(r => r.json()),
      fetch('/api/runs').then(r => r.json()),
    ]);
    state.stats = stats;
    state.graph = graph;
    state.runs = runs;
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
  document.getElementById('meta-info').textContent =
    `epoch ${s.eval_epoch || 1} \u00b7 ${s.metric || 'max'} \u00b7 auto-refresh`;

  // Run switcher
  const runs = state.runs || [];
  const switcher = document.getElementById('run-switcher');
  if (runs.length > 1) {
    const active = runs.find(r => r.active);
    const options = runs.map(r =>
      `<option value="${r.id}" ${r.active ? 'selected' : ''}>${r.id}</option>`
    ).join('');
    switcher.innerHTML = `<select class="run-select" onchange="switchRun(this.value)">${options}</select>`;
    switcher.classList.remove('hidden');
  } else if (runs.length === 1) {
    switcher.innerHTML = `<span class="run-label">${runs[0].id}</span>`;
    switcher.classList.remove('hidden');
  } else {
    switcher.classList.add('hidden');
  }
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
          borderColor: staircaseData.length > 1 ? '#22c55e' : 'transparent',
          borderWidth: 2,
          pointRadius: 0,
          stepped: 'before',
          fill: false,
          order: 2,
        },
        {
          label: 'Committed',
          data: allData.filter(d => d.status === 'committed'),
          backgroundColor: '#22c55e',
          pointRadius: 5,
          order: 1,
        },
        {
          label: 'Discarded',
          data: allData.filter(d => d.status === 'discarded'),
          backgroundColor: '#52525b',
          pointRadius: 3.5,
          order: 1,
        },
        {
          label: 'Failed',
          data: allData.filter(d => d.status === 'failed'),
          backgroundColor: '#ef4444',
          pointRadius: 3.5,
          order: 1,
        },
        {
          label: 'Active',
          data: allData.filter(d => d.status === 'active'),
          backgroundColor: '#3b82f6',
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
              backgroundColor: 'rgba(59,130,246,0.08)',
              borderColor: 'rgba(59,130,246,0.3)',
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
            color: '#71717a',
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
          backgroundColor: '#141416',
          borderColor: '#27272a',
          borderWidth: 1,
          titleColor: '#fafafa',
          bodyColor: '#a1a1aa',
          bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
        },
      },
      scales: {
        x: {
          min: 0,
          suggestedMax: Math.max(allData.length - 1, 1),
          offset: true,
          title: { display: true, text: 'experiment #', color: '#52525b', font: { size: 11 } },
          grid: { color: '#1e1e22' },
          ticks: {
            color: '#71717a',
            font: { family: "'JetBrains Mono', monospace", size: 11 },
            stepSize: 1,
            callback: (v) => v >= 0 ? Math.round(v) : '',
          },
        },
        y: {
          suggestedMin: 0,
          suggestedMax: 1,
          title: { display: false },
          grid: { color: '#1e1e22' },
          ticks: {
            color: '#71717a',
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
      if (child.status === 'committed') return '#22c55e';
      if (child.status === 'active') return '#3b82f6';
      return '#3f3f46';
    })
    .attr('opacity', d => d.target.data.status === 'committed' ? 0.6 : 0.25);

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
    .attr('fill', d => STATUS_COLORS[d.data.status] || '#3f3f46')
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
      if (d.data.status === 'committed') return '#a1a1aa';
      if (d.data.status === 'active') return '#3b82f6';
      if (d.data.status === 'failed') return '#ef4444';
      return '#52525b';  // discarded/pruned greyed out
    })
    .attr('font-size', '9px')
    .attr('font-weight', d => d.data.status === 'committed' ? '500' : '400')
    .text(d => shortId(d.data.id));

  // Score label below ID (only for committed nodes with a score)
  labelG.filter(d => d.data.status === 'committed' && d.data.score != null)
    .append('text')
    .attr('x', 12)
    .attr('dy', '1em')
    .attr('fill', '#52525b')
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
    <div class="drawer-meta-row"><span class="drawer-meta-key">Created</span><span class="drawer-meta-val">${relTime(node.created_at)} ago</span></div>
    ${node.children?.length ? `<div class="drawer-meta-row"><span class="drawer-meta-key">Children</span><span class="drawer-meta-val mono" style="color:var(--indigo)">${node.children.join(', ')}</span></div>` : ''}
  </div>`;

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

// ─── Main render ─────────────────────────────────────────
function render() {
  renderTopbar();
  renderHero();
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
    closeDrawer();
    closeScratchpad();
  }
  if (e.key === 's' && !e.ctrlKey && !e.metaKey && !state.selectedNode) {
    openScratchpad();
  }
});
