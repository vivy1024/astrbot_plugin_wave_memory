// ═══════════════════════════════════════════════════════════
// Wave Memory 神经云图 v4 — Premium Dark Neon UI
// ═══════════════════════════════════════════════════════════

const TYPE_COLORS = {
    person:'#f472b6', topic:'#60a5fa', event:'#34d399',
    emotion:'#fbbf24', entity:'#fb923c', keyword:'#94a3b8',
    fact:'#a78bfa', location:'#2dd4bf', time:'#e879f9',
    memory:'#6366f1', source:'#ffd700',
};
const TYPE_LABELS = {
    person:'人物', topic:'话题', event:'事件',
    emotion:'情绪', entity:'实体', keyword:'关键词',
    fact:'事实', location:'地点', time:'时间',
    memory:'记忆', source:'查询源',
};

let renderer = null;
let graph = null;
let currentView = 'galaxy';
let selectedNode = null;
let activeFilter = null;
let hoveredNode = null;
let hoveredNeighbors = new Set();
let selectedFact = null;

// ─── Custom Label Renderer ───
function drawLabel(context, data, settings) {
    if (!data.label) return;
    const size = settings.labelSize || 12;
    const font = settings.labelFont || 'system-ui, -apple-system, sans-serif';
    const weight = data.highlighted ? '600' : '400';
    context.font = `${weight} ${size}px ${font}`;
    context.fillStyle = data.highlighted ? '#ffffff' : 'rgba(226, 232, 240, 0.85)';
    context.shadowColor = 'rgba(0,0,0,0.8)';
    context.shadowBlur = 4;
    context.fillText(data.label, data.x + data.size + 4, data.y + size / 3);
    context.shadowBlur = 0;
}

// ─── Custom Hover Renderer (glow ring + backdrop label) ───
function drawHover(context, data, settings) {
    const size = data.size;
    const color = data.color || '#8b5cf6';

    // Outer glow ring
    context.beginPath();
    context.arc(data.x, data.y, size + 6, 0, Math.PI * 2);
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.shadowColor = color;
    context.shadowBlur = 16;
    context.stroke();
    context.shadowBlur = 0;

    // Second glow ring (softer)
    context.beginPath();
    context.arc(data.x, data.y, size + 10, 0, Math.PI * 2);
    context.strokeStyle = color + '40';
    context.lineWidth = 1;
    context.stroke();

    // Label with dark backdrop
    if (data.label) {
        const fontSize = 13;
        const font = `600 ${fontSize}px system-ui, -apple-system, sans-serif`;
        context.font = font;
        const textWidth = context.measureText(data.label).width;
        const labelX = data.x + size + 8;
        const labelY = data.y;
        const padding = 6;
        const boxX = labelX - padding;
        const boxY = labelY - fontSize / 2 - padding;
        const boxW = textWidth + padding * 2;
        const boxH = fontSize + padding * 2;
        const radius = 6;

        // Rounded rect backdrop
        context.beginPath();
        context.roundRect(boxX, boxY, boxW, boxH, radius);
        context.fillStyle = 'rgba(6, 8, 13, 0.95)';
        context.fill();
        context.strokeStyle = color + '60';
        context.lineWidth = 1;
        context.stroke();

        // Label text
        context.fillStyle = '#f8fafc';
        context.shadowColor = color;
        context.shadowBlur = 6;
        context.fillText(data.label, labelX, labelY + fontSize / 3);
        context.shadowBlur = 0;
    }
}

// ─── Legend ───
function initLegend() {
    const legend = document.getElementById('legend');
    const types = ['person','topic','event','emotion','entity','keyword','fact','location'];
    legend.innerHTML = types.map(t => `
        <button class="legend-pill flex items-center gap-1.5 px-2.5 py-1.5 rounded-full text-[10px] cursor-pointer border border-transparent"
                data-type="${t}" style="background:${TYPE_COLORS[t]}15; color:${TYPE_COLORS[t]}; --pill-glow:${TYPE_COLORS[t]}40">
            <span class="w-2.5 h-2.5 rounded-full" style="background: radial-gradient(circle at 30% 30%, ${TYPE_COLORS[t]}, ${TYPE_COLORS[t]}80)"></span>
            ${TYPE_LABELS[t]}
        </button>
    `).join('');
    legend.querySelectorAll('.legend-pill').forEach(btn => {
        btn.addEventListener('click', () => toggleFilter(btn.dataset.type));
    });
}

function toggleFilter(type) {
    activeFilter = activeFilter === type ? null : type;
    document.querySelectorAll('.legend-pill').forEach(btn => {
        const isActive = btn.dataset.type === activeFilter;
        btn.classList.toggle('active', isActive);
        btn.style.borderColor = isActive ? TYPE_COLORS[type] + '60' : 'transparent';
    });
    renderer.refresh();
}

// ─── Graph Init ───
function initGraph() {
    graph = new graphology.Graph();
    const container = document.getElementById('sigma-container');

    renderer = new Sigma(graph, container, {
        allowInvalidContainer: true,
        renderLabels: true,
        renderEdgeLabels: true,
        labelFont: 'system-ui, -apple-system, sans-serif',
        labelSize: 11,
        edgeLabelFont: 'system-ui, sans-serif',
        edgeLabelSize: 9,
        edgeLabelColor: { color: '#64748b' },
        labelColor: { color: '#cbd5e1' },
        labelRenderedSizeThreshold: 12,
        edgeLabelRenderedSizeThreshold: 1.5,
        defaultNodeColor: '#64748b',
        defaultEdgeColor: 'rgba(100, 116, 139, 0.08)',
        defaultDrawNodeLabel: drawLabel,
        defaultDrawNodeHover: drawHover,
        hoverRenderer: drawHover,
        edgeReducer(edge, data) {
            const res = { ...data };
            if (hoveredNode) {
                const src = graph.source(edge);
                const tgt = graph.target(edge);
                if (src === hoveredNode || tgt === hoveredNode) {
                    const neighborNode = src === hoveredNode ? tgt : src;
                    const nColor = graph.getNodeAttribute(neighborNode, 'color') || '#8b5cf6';
                    res.color = nColor + '60';
                    res.size = Math.max(1.5, (res.size || 1) * 2);
                } else {
                    res.color = 'rgba(100, 116, 139, 0.02)';
                    res.hidden = false;
                }
            }
            if (activeFilter) {
                const src = graph.source(edge);
                const tgt = graph.target(edge);
                const srcType = graph.getNodeAttribute(src, 'nodeType');
                const tgtType = graph.getNodeAttribute(tgt, 'nodeType');
                if (srcType !== activeFilter && tgtType !== activeFilter) {
                    res.hidden = true;  // 两端都不匹配则隐藏，避免连到隐藏节点的悬空边
                }
            }
            return res;
        },
        nodeReducer(node, data) {
            const res = { ...data };
            if (hoveredNode) {
                if (node === hoveredNode) {
                    res.highlighted = true;
                    res.zIndex = 2;
                } else if (hoveredNeighbors.has(node)) {
                    res.zIndex = 1;
                    // Keep label for neighbors
                } else {
                    res.color = '#1e293b';
                    res.label = '';
                    res.zIndex = 0;
                }
            }
            if (activeFilter) {
                const nodeType = graph.getNodeAttribute(node, 'nodeType');
                if (nodeType !== activeFilter) {
                    res.hidden = true;   // 真正隐藏，避免不匹配节点占位堆叠
                }
            }
            return res;
        },
    });

    // Tooltip logic
    const tooltip = document.getElementById('node-tooltip');
    let tooltipTimeout = null;

    renderer.on('enterNode', ({ node }) => {
        hoveredNode = node;
        hoveredNeighbors = new Set(graph.neighbors(node));
        renderer.refresh();
        container.style.cursor = 'pointer';

        // Show tooltip
        clearTimeout(tooltipTimeout);
        tooltipTimeout = setTimeout(() => {
            const attrs = graph.getNodeAttributes(node);
            const nodeData = attrs._data || {};
            const color = TYPE_COLORS[nodeData.type] || '#94a3b8';
            const neighbors = graph.neighbors(node);
            const degree = neighbors.length;

            tooltip.innerHTML = `
                <div class="tt-name">${attrs.label || node}</div>
                <span class="tt-type" style="background:${color}20; color:${color}; border: 1px solid ${color}40">${TYPE_LABELS[nodeData.type] || nodeData.type || '未知'}</span>
                <div class="tt-meta">
                    ${nodeData.score ? `相似度: ${nodeData.score.toFixed(3)}<br>` : ''}
                    ${nodeData.community !== undefined ? `社区: #${nodeData.community}<br>` : ''}
                    连接数: ${degree}
                </div>
                ${degree > 0 ? `<div class="tt-neighbors">邻居: ${neighbors.slice(0, 5).map(n => graph.getNodeAttribute(n, 'label') || n).join(', ')}${degree > 5 ? ` +${degree-5}` : ''}</div>` : ''}
            `;
            tooltip.classList.add('visible');
        }, 200);
    });

    renderer.on('leaveNode', () => {
        hoveredNode = null;
        hoveredNeighbors.clear();
        renderer.refresh();
        container.style.cursor = 'grab';
        clearTimeout(tooltipTimeout);
        tooltip.classList.remove('visible');
    });

    renderer.on('clickNode', ({ node }) => {
        selectedNode = node;
        selectedFact = null; // 重置事实选中状态
        showDetail(node);
        tooltip.classList.remove('visible');

        // ─── 引力平移：相机平滑缩放移动到目标节点 (WebGL 硬件自适应) ───
        const nodeDisplay = renderer.getNodeDisplayData(node);
        if (nodeDisplay) {
            renderer.getCamera().animate(
                { x: nodeDisplay.x, y: nodeDisplay.y, ratio: 0.55 },
                { duration: 800, ease: 'sine.inOut' }
            );

            // 在屏幕上创造一个精美的“脑电波扩散 ripple”动画
            const containerRect = container.getBoundingClientRect();
            // 换算成屏幕坐标
            const screenPos = renderer.nodeToViewport(node);
            if (screenPos) {
                const ripple = document.createElement('div');
                ripple.style.position = 'fixed';
                ripple.style.left = (screenPos.x + containerRect.left) + 'px';
                ripple.style.top = (screenPos.y + containerRect.top) + 'px';
                ripple.style.width = '10px';
                ripple.style.height = '10px';
                ripple.style.transform = 'translate(-50%, -50%)';
                ripple.style.borderRadius = '50%';
                ripple.style.border = '2px solid rgba(139, 92, 246, 0.7)';
                ripple.style.boxShadow = '0 0 16px rgba(139, 92, 246, 0.5)';
                ripple.style.pointerEvents = 'none';
                ripple.style.zIndex = '99';
                document.body.appendChild(ripple);

                if (typeof gsap !== 'undefined') {
                    gsap.fromTo(ripple, 
                        { width: '10px', height: '10px', opacity: 1 },
                        { width: '120px', height: '120px', opacity: 0, duration: 1.0, ease: 'power2.out', onComplete: () => ripple.remove() }
                    );
                } else {
                    setTimeout(() => ripple.remove(), 1000);
                }
            }
        }
    });

    // 双击节点展开 KG 邻居（焦点探索模式 v1.1.0 #1.3）
    renderer.on('doubleClickNode', ({ node }) => {
        selectedNode = node;
        expandNode();
    });

    // Track mouse for tooltip position
    container.addEventListener('mousemove', (e) => {
        tooltip.style.left = (e.clientX + 16) + 'px';
        tooltip.style.top = (e.clientY - 10) + 'px';
        // Prevent overflow
        const rect = tooltip.getBoundingClientRect();
        if (rect.right > window.innerWidth - 10) {
            tooltip.style.left = (e.clientX - rect.width - 16) + 'px';
        }
        if (rect.bottom > window.innerHeight - 10) {
            tooltip.style.top = (e.clientY - rect.height - 10) + 'px';
        }
    });

    renderer.on('clickStage', () => {
        selectedNode = null;
        hideDetail();
    });

    // ─── 节点拖拽（高级交互）───
    let draggedNode = null;
    let isDragging = false;

    renderer.on('downNode', ({ node, event }) => {
        isDragging = true;
        draggedNode = node;
        graph.setNodeAttribute(node, 'highlighted', true);
        renderer.getCamera().disable();
    });

    renderer.getMouseCaptor().on('mousemovebody', (e) => {
        if (!isDragging || !draggedNode) return;
        const pos = renderer.viewportToGraph(e);
        graph.setNodeAttribute(draggedNode, 'x', pos.x);
        graph.setNodeAttribute(draggedNode, 'y', pos.y);
    });

    renderer.getMouseCaptor().on('mouseup', () => {
        if (draggedNode) {
            graph.removeNodeAttribute(draggedNode, 'highlighted');
        }
        isDragging = false;
        draggedNode = null;
        renderer.getCamera().enable();
    });
}

// ─── Load Galaxy (全量前端模式：一次加载 → 内存筛选 → 零延迟) ───
let _kgFullEdges = null; // 全量边数据(内存缓存)

async function loadGalaxy() {
    showLoading('正在加载知识图谱...');
    try {
        // 读取勾选的图层
        const layers = [];
        document.querySelectorAll('#cfg-layers input[type=checkbox]').forEach(cb => {
            if (cb.checked) layers.push(cb.dataset.layer);
        });
        const layerParam = layers.length ? layers.join(',') : 'facts';
        const res = await fetch(`/api/kg/full?layers=${layerParam}`);
        const data = await res.json();
        _kgFullEdges = data.edges || [];
        showLoading(`已加载 ${_kgFullEdges.length} 条关系（图层: ${data.layers?.join(', ')}），渲染中...`);
        // 自动加载配置面板 pills（修复首次加载为空）
        if (!kgConfigLoaded) loadKgConfig();
        applyKgConfig();
    } catch(e) {
        console.error('Load KG failed:', e);
    }
    hideLoading();
}

function applyKgConfig() {
    if (!_kgFullEdges) { loadGalaxy(); return; }
    const maxNodes = parseInt(document.getElementById('cfg-max-nodes')?.value || '150');
    const minWeight = parseFloat(document.getElementById('cfg-min-weight')?.value || '0');
    const days = parseInt(document.getElementById('cfg-days')?.value || '0');
    const cutoff = days > 0 ? (Date.now()/1000 - days * 86400) : 0;

    // 前端过滤（减法逻辑：selectedRelTypes/selectedNodeTypes 里有的才显示，取消的隐藏）
    // 关系类型/节点类型/时间筛选只对 facts 图层生效，其他图层只要 layer 被勾选就通过
    let filtered = _kgFullEdges;
    if (minWeight > 0) filtered = filtered.filter(e => e.layer !== 'facts' || e.w >= minWeight);
    if (cutoff > 0) filtered = filtered.filter(e => e.layer !== 'facts' || e.ts >= cutoff);
    if (selectedRelTypes.size > 0) {
        filtered = filtered.filter(e => e.layer !== 'facts' || selectedRelTypes.has(e.l));
    }
    if (selectedNodeTypes.size > 0) {
        filtered = filtered.filter(e => {
            if (e.layer !== 'facts') return true;
            return selectedNodeTypes.has(e.st) || selectedNodeTypes.has(e.tt);
        });
    }

    // 按权重排序取 top(以边为中心)
    filtered.sort((a, b) => b.w - a.w);
    const maxEdges = maxNodes * 2;
    filtered = filtered.slice(0, maxEdges);

    // 从边端点构建节点集
    const nodeDeg = {};
    const nodeType = {};
    for (const e of filtered) {
        nodeDeg[e.s] = (nodeDeg[e.s]||0) + 1;
        nodeDeg[e.t] = (nodeDeg[e.t]||0) + 1;
        nodeType[e.s] = nodeType[e.s] || e.st;
        nodeType[e.t] = nodeType[e.t] || e.tt;
    }

    // 限制节点数
    let sortedNodes = Object.entries(nodeDeg).sort((a,b) => b[1]-a[1]);
    if (sortedNodes.length > maxNodes) sortedNodes = sortedNodes.slice(0, maxNodes);
    const topSet = new Set(sortedNodes.map(x => x[0]));

    // 构建 graph 数据
    const nameToId = {};
    const nodes = sortedNodes.map(([name, deg], i) => {
        nameToId[name] = i + 1;
        return { id: i+1, name, type: nodeType[name] || 'entity', degree: deg };
    });
    const edges = [];
    for (const e of filtered) {
        const sId = nameToId[e.s], tId = nameToId[e.t];
        if (sId && tId) edges.push({ source: sId, target: tId, label: e.l, weight: e.w });
    }

    renderGraph(nodes, edges);
    // 状态反馈
    const status = document.getElementById('cfg-status');
    if (status) status.textContent = `显示 ${nodes.length} 实体 / ${edges.length} 关系（总 ${_kgFullEdges.length} 条）`;
    const badge = document.getElementById('stats-badge');
    if (badge) badge.innerHTML = `<span class="text-purple-300 font-semibold">${nodes.length}</span> 实体 · <span class="text-blue-300 font-semibold">${edges.length}</span> 关系 · <span class="text-slate-500">总 ${_kgFullEdges.length}</span>`;
}

function renderGraph(nodes, edges) {
    graph.clear();
    nodes.forEach((n, i) => {
        const nid = String(n.id || n.tag_id || i);
        const type = n.type || n.tag_type || 'keyword';
        const isSeed = n.isSource || n.isSeed;
        const color = isSeed ? TYPE_COLORS.source : (TYPE_COLORS[type] || TYPE_COLORS.keyword);
        const degree = n.value || n.degree || n.weight || 1;
        const size = isSeed ? 14 : Math.max(3, Math.min(13, Math.log2(degree + 1) * 2.2 + 3));
        graph.addNode(nid, {
            label: n.name || n.label || nid,
            x: n.x || (Math.random() * 100),
            y: n.y || (Math.random() * 100),
            size,
            color,
            nodeType: type,
            _data: { ...n, type, degree },
        });
    });
    edges.forEach(e => {
        const src = String(e.source || e.from);
        const tgt = String(e.target || e.to);
        if (!graph.hasNode(src) || !graph.hasNode(tgt)) return;
        if (graph.hasEdge(src, tgt) || graph.hasEdge(tgt, src)) return;
        const weight = e.value || e.weight || e.count || 1;
        const label = e.label || e.relation_type || '';
        const srcColor = graph.getNodeAttribute(src, 'color');
        graph.addEdge(src, tgt, {
            size: Math.max(0.4, Math.min(2.5, weight * 1.5)),
            color: srcColor + '25',
            label: label,
            _label: label,
        });
    });

    // Layout
    if (graph.order > 0 && typeof graphologyLibrary !== 'undefined') {
        try {
            graphologyLibrary.layoutForceAtlas2.assign(graph, {
                iterations: 120,
                settings: {
                    gravity: 0.35,
                    scalingRatio: 14,
                    barnesHutOptimize: graph.order > 200,
                    strongGravityMode: true,
                    slowDown: 6,
                }
            });
        } catch(e) { console.warn('Layout failed:', e); }
    }

    updateStats();
    setTimeout(() => {
        const camera = renderer.getCamera();
        camera.animatedReset({ duration: 400 });
    }, 150);
}

// ─── Query (语义向量检索 — 搜任意词返回相关记忆) ───
async function doQuery() {
    const q = document.getElementById('search-input').value.trim();
    if (!q) return;
    showLoading('正在语义检索...');
    try {
        const res = await fetch('/api/query', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: q, top_k: 12, enable_spike: true, enable_pyramid: false, enable_epa: false, enable_geodesic: false }),
        });
        const data = await res.json();
        if (data.results && data.results.length) {
            // 把语义检索结果渲染为记忆列表（比图更直观）
            const panel = document.getElementById('detail-panel');
            document.getElementById('detail-title').textContent = `「${q}」语义检索`;
            document.getElementById('detail-meta').innerHTML = `<span class="text-purple-300">${data.results.length} 条相关记忆</span> · ${data.timing?.total_ms || '?'}ms`;
            document.getElementById('detail-neighbor-list').innerHTML = '';
            const memList = document.getElementById('detail-memory-list');
            memList.innerHTML = data.results.map(m => {
                const time = m.timestamp ? new Date(m.timestamp * 1000).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
                const score = m.score ? `<span class="text-purple-400/70 text-[9px]">${(m.score*100).toFixed(0)}%</span>` : '';
                return `<div class="p-2.5 rounded-lg bg-white/[.03] border border-white/5">
                    <div class="flex items-center gap-2 mb-1">
                        <span class="text-purple-300 text-[10px] font-medium">${m.sender_name || '未知'}</span>
                        ${score}
                        <span class="text-slate-600 text-[9px] ml-auto">${time}</span>
                    </div>
                    <p class="text-slate-300 text-[11px] leading-relaxed">${(m.content||'').slice(0,180)}${(m.content||'').length>180?'...':''}</p>
                </div>`;
            }).join('');
            document.getElementById('btn-expand').style.display = 'none';
            panel.classList.remove('hidden');
            if (typeof gsap !== 'undefined') {
                gsap.fromTo(panel, { autoAlpha: 0, x: 30 }, { autoAlpha: 1, x: 0, duration: 0.4, ease: 'power3.out' });
            }
        } else {
            showLoading(`「${q}」无相关记忆`);
            setTimeout(hideLoading, 1500);
            return;
        }
    } catch(e) { console.error('Query failed:', e); }
    hideLoading();
}

// ─── Person View ───
async function loadPersonList() {
    const list = document.getElementById('person-list');
    list.innerHTML = '<p class="text-slate-600 text-xs">加载中...</p>';
    try {
        const res = await fetch('/api/explore/persons?limit=50');
        const persons = await res.json();
        if (!Array.isArray(persons) || persons.length === 0) {
            list.innerHTML = '<p class="text-slate-600 text-xs">暂无人物</p>';
            return;
        }
        list.innerHTML = persons.map(p => `
            <div class="person-item" onclick="loadPersonGraph('${String(p.id).replace(/'/g, "\\'")}', '${(p.name||'').replace(/'/g, "\\'")}')">
                <div class="text-slate-200 text-xs font-medium">${p.name || p.id}</div>
                <div class="text-slate-500 text-[10px] mt-0.5">${p.count || 0} 条记忆</div>
            </div>
        `).join('');
        if (typeof gsap !== 'undefined') {
            gsap.fromTo('#person-list .person-item', { x: -16, autoAlpha: 0 }, { x: 0, autoAlpha: 1, duration: 0.3, stagger: 0.03, ease: 'power2.out' });
        }
    } catch(e) {
        list.innerHTML = '<p class="text-red-400 text-xs">加载失败</p>';
    }
}

async function loadPersonGraph(qqId, name) {
    showLoading(`加载 ${name || qqId} 的关系网...`);
    try {
        const res = await fetch(`/api/explore/person/${encodeURIComponent(qqId)}?max_memories=80`);
        const data = await res.json();
        if (data.nodes && data.nodes.length) {
            renderGraph(data.nodes, data.edges || []);
        } else {
            showLoading(`${name || qqId} 暂无记忆网络`);
            setTimeout(hideLoading, 1200);
            return;
        }
    } catch(e) { console.error(e); }
    hideLoading();
}

// ─── Path Finding ───
async function _resolveTagId(name) {
    // 先在已加载图中按 label 精确匹配
    let hit = null;
    graph.forEachNode((nid, attrs) => {
        if (!hit && (attrs.label || '') === name) hit = nid;
    });
    if (hit) return parseInt(hit, 10);
    // 否则查 tag 表
    const res = await fetch(`/api/tags/?search=${encodeURIComponent(name)}&limit=1`);
    const data = await res.json();
    const items = data.items || [];
    return items.length ? items[0].id : null;
}

async function doPathFind() {
    const from = document.getElementById('path-from').value.trim();
    const to = document.getElementById('path-to').value.trim();
    if (!from || !to) return;
    showLoading('寻路中...');
    try {
        const res = await fetch('/api/kg/path', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ from, to, max_depth: 6 }),
        });
        const data = await res.json();
        if (data.nodes && data.nodes.length) {
            renderGraph(data.nodes, data.edges || []);
            // 在详情面板展示路径语义链
            const panel = document.getElementById('detail-panel');
            document.getElementById('detail-title').textContent = `${from} → ${to} 路径`;
            document.getElementById('detail-meta').innerHTML = `<span class="text-purple-300">${data.path.length} 跳</span>`;
            document.getElementById('detail-neighbor-list').innerHTML = '';
            const memList = document.getElementById('detail-memory-list');
            memList.innerHTML = '<p class="text-slate-500 text-[9px] uppercase tracking-wider mb-1.5">关系链</p>' +
                data.edges.map(e => `<div class="px-2 py-1.5 rounded bg-white/[.02] text-[10px] text-slate-400 mb-1"><span class="text-purple-300">${e.source}</span> <span class="text-amber-400/70">→${e.label}→</span> <span class="text-blue-300">${e.target}</span></div>`).join('');
            document.getElementById('btn-expand').style.display = 'none';
            panel.classList.remove('hidden');
            if (typeof gsap !== 'undefined') gsap.fromTo(panel, {autoAlpha:0,x:30}, {autoAlpha:1,x:0,duration:0.4,ease:'power3.out'});
        } else {
            showLoading(`${from} → ${to} 之间无连通路径`);
            setTimeout(hideLoading, 1500);
            return;
        }
    } catch(e) { console.error(e); }
    hideLoading();
}

// ─── Expand Node (展开 KG 邻居) ───
async function expandNode() {
    if (!selectedNode) return;
    const attrs = graph.getNodeAttributes(selectedNode);
    const entityName = attrs.label || selectedNode;
    if (!entityName) return;
    showLoading('展开邻居...');
    try {
        const res = await fetch(`/api/kg/entity/${encodeURIComponent(entityName)}`);
        const data = await res.json();
        if (data.neighbors && data.neighbors.length) {
            const nodes = [];
            const edges = [];
            for (const n of data.neighbors) {
                nodes.push({ id: n.name, label: n.name, type: n.type || 'entity' });
                edges.push({ s: entityName, t: n.name, l: n.predicate || 'relates_to', w: n.confidence || 0.5, layer: 'facts' });
            }
            appendGraphData(nodes, edges);
        } else {
            showLoading('该节点无 KG 邻居');
            setTimeout(hideLoading, 1200);
            return;
        }
    } catch(e) { console.error(e); }
    hideLoading();
}

// ─── Timeline View ───
async function loadTimeline() {
    if (!selectedNode) return;
    const attrs = graph.getNodeAttributes(selectedNode);
    const entityName = attrs.label || '';
    if (!entityName) return;
    const memList = document.getElementById('detail-memory-list');
    memList.innerHTML = '<p class="text-slate-600 text-[10px]">加载时间线...</p>';
    try {
        const r = await fetch(`/api/kg/entity/${encodeURIComponent(entityName)}/timeline?limit=25`);
        const d = await r.json();
        if (!d.events || !d.events.length) {
            memList.innerHTML = '<p class="text-slate-600 text-[10px]">暂无时间线数据</p>';
            return;
        }
        document.getElementById('detail-title').textContent = `📅 ${entityName} 时间线`;
        document.getElementById('detail-meta').innerHTML = `<span class="text-blue-300">${d.events.length} 个事件</span>`;
        // 渲染纵向时间轴
        let html = '<div class="relative pl-4 border-l-2 border-purple-500/20 space-y-3">';
        for (const ev of d.events) {
            const time = ev.ts ? new Date(ev.ts * 1000).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '?';
            const dotColor = ev.type === 'fact' ? '#a78bfa' : '#60a5fa';
            if (ev.type === 'fact') {
                html += `<div class="relative">
                    <div class="absolute -left-[21px] top-1.5 w-2.5 h-2.5 rounded-full" style="background:${dotColor}"></div>
                    <div class="text-[9px] text-slate-600 mb-0.5">${time}</div>
                    <div class="px-2 py-1.5 rounded bg-purple-500/[.06] text-[10px] text-slate-300">
                        <span class="text-purple-300">${ev.subject||''}</span> <span class="text-slate-600">→${ev.predicate||''}→</span> <span class="text-blue-300">${(ev.object||'').slice(0,50)}</span>
                    </div>
                </div>`;
            } else {
                html += `<div class="relative">
                    <div class="absolute -left-[21px] top-1.5 w-2.5 h-2.5 rounded-full" style="background:${dotColor}"></div>
                    <div class="text-[9px] text-slate-600 mb-0.5">${time}</div>
                    <div class="px-2 py-1.5 rounded bg-blue-500/[.06] text-[10px]">
                        <span class="text-blue-300 font-medium">${ev.sender||''}</span>
                        <span class="text-slate-400 ml-1">${(ev.content||'').slice(0,80)}</span>
                    </div>
                </div>`;
            }
        }
        html += '</div>';
        memList.innerHTML = html;
    } catch(e) {
        memList.innerHTML = '<p class="text-red-400/60 text-[10px]">时间线加载失败</p>';
    }
}

// ─── 事实选择、斩断与弹窗编辑 (WaveMemory 2.0 终极进化交互) ───

function selectFact(el) {
    // 清空其他事实卡片的高亮
    document.querySelectorAll('.fact-item').forEach(item => {
        item.style.borderColor = 'transparent';
        item.style.boxShadow = 'none';
        item.style.background = 'rgba(255, 255, 255, 0.02)';
    });
    
    // 高亮当前选中的事实
    el.style.borderColor = 'rgba(139, 92, 246, 0.7)';
    el.style.boxShadow = '0 0 10px rgba(139, 92, 246, 0.35)';
    el.style.background = 'rgba(139, 92, 246, 0.06)';
    
    selectedFact = {
        id: el.dataset.id,
        subject: el.dataset.sub,
        predicate: el.dataset.pred,
        object: el.dataset.obj,
        confidence: el.dataset.conf
    };
    console.log("[WaveMemory] 选中事实:", selectedFact);
}

async function severFactRelation() {
    if (!selectedFact) {
        alert('请先在上方的事实列表中，点击选择要斩断的那条事实。');
        return;
    }
    if (!confirm(`确认要斩断并彻底物理删除这一事实关联吗？\n【${selectedFact.subject} → ${selectedFact.predicate} → ${selectedFact.object}】\n此操作不可逆！`)) {
        return;
    }
    
    const btn = document.getElementById('btn-sever-fact');
    const oldText = btn.textContent;
    btn.textContent = '斩断中...';
    btn.disabled = true;
    
    try {
        const r = await fetch(`/api/kg/facts/${selectedFact.id}`, { method: 'DELETE' });
        const d = await r.json();
        if (d.ok) {
            // 清理缓存
            _kgFullEdges = null;
            selectedFact = null;
            alert('✓ 事实已成功物理斩断！该认知已从灵魂中抹去。');
            // 刷新详情和图谱
            await showDetail(selectedNode);
            initGraph();
        } else {
            alert('✗ 斩断失败: ' + (d.error || '未知错误'));
        }
    } catch(e) {
        alert('✗ 网络错误，斩断失败');
    } finally {
        btn.textContent = oldText;
        btn.disabled = false;
    }
}

function editEntity() {
    const dialog = document.getElementById('fact-edit-dialog');
    const inputSub = document.getElementById('edit-fact-subject');
    const inputPred = document.getElementById('edit-fact-predicate');
    const inputObj = document.getElementById('edit-fact-object');
    const inputConf = document.getElementById('edit-fact-confidence');
    
    if (selectedFact) {
        // 如果选中了具体事实，进入修正模式
        inputSub.value = selectedFact.subject || '';
        inputPred.value = selectedFact.predicate || '';
        inputObj.value = selectedFact.object || '';
        inputConf.value = selectedFact.confidence || 0.8;
    } else {
        // 如果没选中具体事实，预填当前节点名称，进入快速创建模式
        const attrs = graph.getNodeAttributes(selectedNode);
        inputSub.value = attrs.label || selectedNode;
        inputPred.value = '';
        inputObj.value = '';
        inputConf.value = 0.8;
    }
    
    dialog.classList.remove('hidden');
    if (typeof gsap !== 'undefined') {
        gsap.fromTo(dialog.querySelector('.glass'), { scale: 0.9, opacity: 0 }, { scale: 1, opacity: 1, duration: 0.35, ease: 'back.out(1.5)' });
    }
}

function closeFactEdit() {
    const dialog = document.getElementById('fact-edit-dialog');
    if (typeof gsap !== 'undefined') {
        gsap.to(dialog.querySelector('.glass'), { scale: 0.9, opacity: 0, duration: 0.2, ease: 'power2.in', onComplete: () => dialog.classList.add('hidden') });
    } else {
        dialog.classList.add('hidden');
    }
}

async function saveFactEdit() {
    const subj = document.getElementById('edit-fact-subject').value.trim();
    const pred = document.getElementById('edit-fact-predicate').value.trim();
    const obj = document.getElementById('edit-fact-object').value.trim();
    const conf = parseFloat(document.getElementById('edit-fact-confidence').value) || 0.8;
    
    if (!subj || !pred || !obj) {
        alert('请填写完整的三元组内容');
        return;
    }
    
    try {
        let r, d;
        if (selectedFact) {
            // 修正模式：PUT /api/kg/facts/<id>
            r = await fetch(`/api/kg/facts/${selectedFact.id}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ subject: subj, predicate: pred, object: obj, confidence: conf })
            });
        } else {
            // 创建模式：POST /api/kg/add-fact
            r = await fetch('/api/kg/add-fact', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ subject: subj, predicate: pred, object: obj, confidence: conf })
            });
        }
        
        d = await r.json();
        if (d.ok) {
            _kgFullEdges = null; // 清缓存
            closeFactEdit();
            selectedFact = null;
            // 重新刷新
            await showDetail(selectedNode);
            initGraph();
        } else {
            alert('保存失败: ' + (d.error || '未知错误'));
        }
    } catch(e) {
        alert('网络错误，保存失败');
    }
}

function appendGraphData(newNodes, newEdges) {
    const existingIds = new Set();
    graph.forEachNode(n => existingIds.add(n));

    newNodes.forEach(n => {
        const nid = String(n.id || n.tag_id);
        if (existingIds.has(nid)) return;
        existingIds.add(nid);
        const type = n.type || n.tag_type || 'keyword';
        const color = n.isSource ? TYPE_COLORS.source : (TYPE_COLORS[type] || TYPE_COLORS.keyword);
        const degree = n.degree || 1;
        const size = Math.max(4, Math.min(16, Math.sqrt(degree) * 2.5 + 3));
        const selPos = selectedNode ? { x: graph.getNodeAttribute(selectedNode, 'x'), y: graph.getNodeAttribute(selectedNode, 'y') } : { x: 50, y: 50 };
        graph.addNode(nid, {
            label: n.name || nid,
            x: selPos.x + (Math.random() - 0.5) * 30,
            y: selPos.y + (Math.random() - 0.5) * 30,
            size, color,
            nodeType: type,
            _data: { ...n, type, degree },
        });
    });
    newEdges.forEach(e => {
        const src = String(e.source || e.from);
        const tgt = String(e.target || e.to);
        if (!graph.hasNode(src) || !graph.hasNode(tgt)) return;
        if (graph.hasEdge(src, tgt) || graph.hasEdge(tgt, src)) return;
        const srcColor = graph.getNodeAttribute(src, 'color');
        graph.addEdge(src, tgt, {
            size: Math.max(0.4, Math.sqrt(e.weight || 1) * 0.5),
            color: srcColor + '20',
        });
    });
    updateStats();
}

// ─── Detail Panel (展示关联记忆) ───
async function showDetail(nodeId) {
    const attrs = graph.getNodeAttributes(nodeId);
    const data = attrs._data || {};
    const panel = document.getElementById('detail-panel');
    const color = TYPE_COLORS[data.type] || '#94a3b8';

    document.getElementById('detail-title').textContent = attrs.label || nodeId;
    let meta = '';
    if (data.type) meta += `<span style="color:${color}">${TYPE_LABELS[data.type] || data.type}</span>`;
    if (data.degree) meta += ` · 度数 ${data.degree}`;
    if (data.community !== undefined) meta += ` · 社区 #${data.community}`;
    document.getElementById('detail-meta').innerHTML = meta;
    document.getElementById('btn-expand').style.display = data.isSource ? 'none' : '';

    // Neighbor list
    const neighbors = graph.neighbors(nodeId);
    const neighborList = document.getElementById('detail-neighbor-list');
    neighborList.innerHTML = neighbors.slice(0, 12).map(n => {
        const nAttrs = graph.getNodeAttributes(n);
        const nData = nAttrs._data || {};
        const nColor = TYPE_COLORS[nData.type] || '#94a3b8';
        return `<span class="inline-block px-2 py-0.5 rounded text-[10px] cursor-pointer hover:opacity-80 transition" style="background:${nColor}15; color:${nColor}; border: 1px solid ${nColor}25" onclick="selectedNode='${n}'; showDetail('${n}')">${nAttrs.label || n}</span>`;
    }).join('') + (neighbors.length > 12 ? `<span class="text-slate-600 text-[10px] ml-1">+${neighbors.length - 12}</span>` : '');

    // 加载关联记忆 + facts + 人物画像（M2: Entity Card）
    const memList = document.getElementById('detail-memory-list');
    const entityName = attrs.label || '';
    if (entityName) {
        memList.innerHTML = '<p class="text-slate-600 text-[10px]">加载知识...</p>';
        try {
            const r = await fetch(`/api/kg/entity/${encodeURIComponent(entityName)}?limit=12`);
            const d = await r.json();
            let html = '';

            // Person Card（如果是人物）
            if (d.person) {
                const p = d.person;
                const affColor = p.affection > 50 ? '#34d399' : p.affection > 0 ? '#fbbf24' : '#f87171';
                html += `<div class="mb-3 p-3 rounded-xl border border-purple-500/20 bg-purple-500/[.04]">
                    <div class="flex items-center gap-2 mb-2">
                        <div class="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold" style="background:${affColor}20; color:${affColor}; border:2px solid ${affColor}">${(p.name||'?')[0]}</div>
                        <div>
                            <div class="text-white text-xs font-semibold">${p.name}</div>
                            <div class="text-slate-500 text-[9px]">QQ ${p.qq_id} · ${p.msg_count} 条消息</div>
                        </div>
                        <div class="ml-auto text-right">
                            <div class="text-[10px] font-mono" style="color:${affColor}">好感 ${p.affection}</div>
                        </div>
                    </div>
                    ${p.aliases.length ? `<div class="text-[9px] text-slate-500 mb-1.5">别名: ${p.aliases.join(' / ')}</div>` : ''}
                    ${p.personality_tags.length ? `<div class="flex flex-wrap gap-1">${p.personality_tags.slice(0,8).map(t => `<span class="px-1.5 py-0.5 rounded text-[9px] bg-purple-500/10 text-purple-300 border border-purple-500/20">${t}</span>`).join('')}</div>` : ''}
                </div>`;
            }

            // Facts 三元组
            if (d.facts && d.facts.length) {
                html += '<p class="text-slate-500 text-[9px] uppercase tracking-wider mb-1.5">事实 (点击卡片选中后可进行斩断或修正)</p>';
                html += d.facts.slice(0, 6).map((f, idx) => {
                    const isSelected = selectedFact && selectedFact.id === f.id;
                    return `<div class="fact-item px-2 py-1.5 rounded bg-white/[.02] text-[10px] text-slate-400 mb-1 border border-transparent hover:border-purple-500/30 cursor-pointer transition" 
                        data-id="${f.id}" data-sub="${f.subject}" data-pred="${f.predicate}" data-obj="${f.object}" data-conf="${f.confidence}"
                        onclick="selectFact(this)">
                        <span class="text-purple-300">${f.subject}</span> 
                        <span class="text-slate-600">→${f.predicate}→</span> 
                        <span class="text-blue-300">${f.object}</span>
                        ${f.confidence ? `<span class="text-[9px] text-slate-600 ml-1 font-mono">(${Math.round(f.confidence*100)}%)</span>` : ''}
                    </div>`;
                }).join('');
            }
            // Relations
            if (d.relations && d.relations.length) {
                html += '<p class="text-slate-500 text-[9px] uppercase tracking-wider mb-1.5 mt-2">关系</p>';
                html += d.relations.slice(0, 6).map(r =>
                    `<div class="px-2 py-1.5 rounded bg-white/[.02] text-[10px] text-slate-400 mb-1"><span class="text-purple-300">${r.source}</span> <span class="text-amber-400/70">${r.type}</span> <span class="text-blue-300">${r.target}</span></div>`
                ).join('');
            }
            // Memories
            if (d.memories && d.memories.length) {
                html += '<p class="text-slate-500 text-[9px] uppercase tracking-wider mb-1.5 mt-2">记忆</p>';
                html += d.memories.slice(0, 8).map(m => {
                    const time = m.ts ? new Date(m.ts * 1000).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
                    return `<div class="p-2.5 rounded-lg bg-white/[.03] border border-white/5 mb-1.5">
                        <div class="flex items-center gap-2 mb-1"><span class="text-purple-300 text-[10px] font-medium">${m.sender||'未知'}</span><span class="text-slate-600 text-[9px] ml-auto">${time}</span></div>
                        <p class="text-slate-300 text-[11px] leading-relaxed">${(m.content||'').slice(0,120)}${(m.content||'').length>120?'...':''}</p>
                    </div>`;
                }).join('');
            }
            if (!html) html = '<p class="text-slate-600 text-[10px]">暂无关联知识</p>';
            memList.innerHTML = html;
        } catch(e) {
            memList.innerHTML = '<p class="text-red-400/60 text-[10px]">加载失败</p>';
        }
    } else {
        memList.innerHTML = '<p class="text-slate-600 text-[10px]">-</p>';
    }

    panel.classList.remove('hidden');
    if (typeof gsap !== 'undefined') {
        gsap.fromTo(panel, { autoAlpha: 0, x: 30 }, { autoAlpha: 1, x: 0, duration: 0.4, ease: 'power3.out' });
    }
}

function hideDetail() {
    const panel = document.getElementById('detail-panel');
    if (panel.classList.contains('hidden')) return;
    if (typeof gsap !== 'undefined') {
        gsap.to(panel, { autoAlpha: 0, x: 30, duration: 0.25, ease: 'power2.in', onComplete: () => panel.classList.add('hidden') });
    } else {
        panel.classList.add('hidden');
    }
}

// ─── Utils ───
function updateStats() {
    const badge = document.getElementById('stats-badge');
    const prev = badge._counts || { n: 0, e: 0 };
    const target = { n: graph.order, e: graph.size };
    if (typeof gsap !== 'undefined') {
        gsap.to(prev, {
            n: target.n, e: target.e, duration: 0.8, ease: 'power2.out',
            onUpdate: () => { badge.innerHTML = `<span class="text-purple-300 font-semibold">${Math.round(prev.n)}</span> 节点 · <span class="text-blue-300 font-semibold">${Math.round(prev.e)}</span> 连线`; },
        });
        badge._counts = prev;
        gsap.fromTo(badge, { scale: 0.94 }, { scale: 1, duration: 0.4, ease: 'back.out(2)' });
    } else {
        badge.textContent = `${target.n} 节点 · ${target.e} 连线`;
    }
}
function showLoading(text) {
    const el = document.getElementById('loading');
    document.getElementById('loading-text').textContent = text;
    el.classList.remove('hidden'); el.classList.add('flex');
    if (typeof gsap !== 'undefined') gsap.fromTo(el, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.25 });
}
function hideLoading() {
    const el = document.getElementById('loading');
    if (typeof gsap !== 'undefined') {
        gsap.to(el, { autoAlpha: 0, duration: 0.3, onComplete: () => { el.classList.add('hidden'); el.classList.remove('flex'); } });
    } else {
        el.classList.add('hidden'); el.classList.remove('flex');
    }
}

// ─── View Switching ───
function switchView(view) {
    currentView = view;
    document.querySelectorAll('.tab-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === view);
    });
    document.getElementById('search-box').style.display = view === 'query' ? 'flex' : 'none';
    document.getElementById('path-input').style.display = view === 'path' ? 'flex' : 'none';
    document.getElementById('person-panel').classList.toggle('hidden', view !== 'person');
    // GSAP 面板滑入
    if (typeof gsap !== 'undefined') {
        if (view === 'query') gsap.fromTo('#search-box', { y: -16, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.4, ease: 'power3.out' });
        if (view === 'path') gsap.fromTo('#path-input', { y: -16, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.4, ease: 'power3.out' });
        if (view === 'person') gsap.fromTo('#person-panel', { x: -40, autoAlpha: 0 }, { x: 0, autoAlpha: 1, duration: 0.45, ease: 'power3.out' });
    }
    hideDetail();
    selectedNode = null;
    activeFilter = null;
    document.querySelectorAll('.legend-pill').forEach(b => {
        b.classList.remove('active');
        b.style.borderColor = 'transparent';
    });

    if (view === 'galaxy') loadGalaxy();
    else if (view === 'person') { loadPersonList(); graph.clear(); renderer.refresh(); }
    else if (view === 'query') { graph.clear(); renderer.refresh(); }
    else if (view === 'path') { graph.clear(); renderer.refresh(); }
}

// ─── Init ───
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchView(btn.dataset.view));
});
document.getElementById('search-input')?.addEventListener('keydown', e => { if (e.key === 'Enter') doQuery(); });
