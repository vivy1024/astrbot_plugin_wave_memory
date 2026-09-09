// ═══════════════════════════════════════════════════════════
// Wave Memory 神经云图 v4.0.0-final — NeuroGalaxy Cosmic 3D Engine
// ═══════════════════════════════════════════════════════════

// ─── 统一太空赛博高雅调色盘 (Obsidian & Starlight Palette) ───
const TYPE_COLORS = {
    bot: '#ffffff',             // 超亮恒星白
    person: '#f472b6',          // 温暖粉晶
    topic: '#38bdf8',           // 冰青
    event: '#34d399',           // 极光翡翠
    emotion: '#fbbf24',         // 恒星金
    entity: '#60a5fa',          // 钛蓝
    keyword: '#94a3b8',         // 陨石灰
    fact: '#c084fc',            // 量子紫
    location: '#2dd4bf',        // 青绿
    time: '#e879f9',            // 脉冲紫
    memory: '#818cf8',          // 神经靛蓝
    source: '#fbbf24',          // 金色探针
    belief: '#d8b4fe',          // 星云紫
    concern: '#38bdf8',         // 苍穹蓝
    jargon: '#fb7185',          // 珊瑚红
    mood: '#f59e0b',            // 琥珀金
    timeline: '#2dd4bf',        // 航迹青
    relationship_event: '#f43f5e', // 跃迁红
    few_shot: '#818cf8',        // 知识晶格
    trait: '#a3e635',           // 荧光绿
    book_lore: '#d8b4fe',       // 古籍紫
    community: '#38bdf8',       // 星团蓝
};
const TYPE_LABELS = {
    bot:'Bot', person:'人物', topic:'话题', event:'事件',
    emotion:'情绪', entity:'实体', keyword:'关键词',
    fact:'事实', location:'地点', time:'时间',
    memory:'记忆', source:'查询源', belief:'信念',
    concern:'关切', jargon:'群内表达', mood:'Soul 情绪',
    timeline:'Soul 时间线', relationship_event:'关系事件', few_shot:'Few-shot',
    trait:'风格特征', book_lore:'BookLore', community:'知识簇',
};

let scene = null;
let camera = null;
let webglRenderer = null;
let controls = null;
let composer = null;
let raycaster = null;
let mouse = null;
let galaxyContainer = null;
let graphGroup = null;
let edgeGroup = null;
let edgeLabelGroup = null;
let labelGroup = null;
let starField = null;
let starFieldOuter = null; // 视差双图层
let animationId = null;
let pointerHandlers = null;
let graphUnavailableReason = '';
let initialGalaxyLoadSettled = false;

let currentView = 'galaxy';
let selectedNode = null;
let selectedEdge = null;
let activeFilter = null;
let hoveredNode = null;
let hoveredEdge = null;
let hoveredNeighbors = new Set();
let selectedFact = null;
let selectedFactEntity = null;
let _kgFullNodes = [];
let _kgFullEdges = null;
let _kgLayerCounts = {};
let _kgWarnings = [];
let layoutMode = 'semantic';
let labelDensity = 'core';
let cameraPreset = 'overview';
let actionRingNode = null;
let pickableNodeObjects = [];
let pickableEdgeObjects = [];
let pointerFramePending = false;
let latestPointerEvent = null;
let lastActionRingPoint = { x: null, y: null };
let transientHoverLabelNode = null;

// 粒子流和动画辅助结构
let flowParticles = []; // { curve, mesh, progress, speed, color }
const LAYOUT_MODES = {
    semantic: '语义群岛',
    type: '类型分层',
    layer: '图层分层',
    time: '时间螺旋',
};

const relationState = {
    selected: null,
    hovered: null,
    editing: null,
};

const entityMutationItems = new Map();
const knowledgeMutationState = {
    item: null,
    kind: null,
    contextNodeId: null,
    canUpdate: false,
    canDelete: false,
    busy: false,
};
let mutationNoticeTimer = null;

const graphState = {
    nodes: new Map(),
    edges: new Map(),
    adjacency: new Map(),
    labelIndex: new Map(),
};

// ─── takram & Cosmograph 级光学镜头星尘光晕贴图生成器 ───
let _glowTextureCache = null;
function getStardustGlowTexture() {
    if (_glowTextureCache) return _glowTextureCache;
    const canvas = document.createElement('canvas');
    canvas.width = 256;
    canvas.height = 256;
    const ctx = canvas.getContext('2d');
    const cx = 128, cy = 128;

    // 1. 径向核心光晕 (Radial Core Flare)
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, 128);
    grad.addColorStop(0, 'rgba(255, 255, 255, 1.0)');
    grad.addColorStop(0.12, 'rgba(255, 255, 255, 0.9)');
    grad.addColorStop(0.35, 'rgba(255, 255, 255, 0.35)');
    grad.addColorStop(0.7, 'rgba(255, 255, 255, 0.08)');
    grad.addColorStop(1, 'rgba(255, 255, 255, 0)');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 256, 256);

    // 2. 细微十字光学衍射芒刺 (Subtle Lens Flare Spikes)
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.18)';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(cx, 16); ctx.lineTo(cx, 240);
    ctx.moveTo(16, cy); ctx.lineTo(240, cy);
    ctx.stroke();

    _glowTextureCache = new THREE.CanvasTexture(canvas);
    return _glowTextureCache;
}

const NODE_GEOMETRIES = typeof THREE !== 'undefined' ? {
    sphere: new THREE.SphereGeometry(1, 24, 16),
    icosahedron: new THREE.IcosahedronGeometry(1, 1),
    octahedron: new THREE.OctahedronGeometry(1, 0),
    dodecahedron: new THREE.DodecahedronGeometry(1, 0),
    torus: new THREE.TorusGeometry(0.8, 0.28, 12, 24),
    box: new THREE.BoxGeometry(1.2, 1.2, 1.2),
} : null;
const NODE_GEOMETRY = NODE_GEOMETRIES?.sphere || null;

function geometryForNodeType(type) {
    if (!NODE_GEOMETRIES) return null;
    if (type === 'bot') return NODE_GEOMETRIES.icosahedron;
    if (type === 'person') return NODE_GEOMETRIES.octahedron;
    if (type === 'memory') return NODE_GEOMETRIES.dodecahedron;
    if (type === 'fact' || type === 'relationship_event') return NODE_GEOMETRIES.torus;
    if (type === 'entity' || type === 'source') return NODE_GEOMETRIES.box;
    return NODE_GEOMETRIES.sphere;
}
const DEG2RAD = Math.PI / 180;

// ─── 基础工具与自愈映射 ───
function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function escapeJs(value) {
    return String(value ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n');
}

function colorForType(type) {
    return TYPE_COLORS[type] || TYPE_COLORS.keyword;
}

function settleInitialGalaxyLoad(type, message) {
    if (initialGalaxyLoadSettled) return;
    initialGalaxyLoadSettled = true;
    if (typeof notifyExploreParent === 'function') {
        notifyExploreParent(type, message);
    }
}

// 契约对齐 A：标准化节点类型
function normalizeNodeType(rawNode) {
    const rawType = String(rawNode.type || rawNode.tag_type || rawNode.nodeType || rawNode.node_type || 'keyword').toLowerCase();
    if (rawType === 'relationship' || rawType === 'affinity_engine') return 'affinity';
    if (rawType === 'holyman_phrase' || rawType === 'catchphrase') return 'jargon';
    if (rawType === 'belief_emergence' || rawType === 'belief_engine') return 'belief';
    if (rawType === 'soul_concern' || rawType === 'concern_engine') return 'concern';
    return rawType;
}

// 契约对齐 B：标准化关系层
function normalizeEdgeLayer(rawLayer) {
    const layer = String(rawLayer || 'facts').toLowerCase();
    if (layer === 'relationship' || layer === 'affinity_engine') return 'affinity';
    if (layer === 'holyman' || layer === 'holyman_skills' || layer === 'jargon_candidates') return 'jargon';
    if (layer === 'belief_emergence' || layer === 'belief_engine') return 'beliefs';
    if (layer === 'soul_concern' || layer === 'concern_engine') return 'concerns';
    return layer;
}

function showGraphUnavailable(message) {
    graphUnavailableReason = message || '当前浏览器无法初始化 WebGL 3D 画布。';
    setEventStatus('error', graphUnavailableReason);
    renderEventWarnings([{ stage: 'webgl', reason: graphUnavailableReason }]);
    const container = galaxyContainer || document.getElementById('galaxy-container');
    if (!container) return;
    container.innerHTML = `
        <div class="absolute inset-0 flex items-center justify-center p-8 text-center">
            <div class="glass glass-accent rounded-2xl px-6 py-5 max-w-md">
                <div class="text-glow-purple text-sm font-semibold mb-2">3D 神经云图暂不可用</div>
                <div class="text-slate-400 text-xs leading-relaxed">${escapeHtml(graphUnavailableReason)}</div>
                <div class="text-slate-600 text-[10px] mt-3">请使用支持 WebGL 的浏览器/显卡环境，或开启硬件加速后刷新。</div>
            </div>
        </div>`;
}

function isWebGLAvailable() {
    try {
        const canvas = document.createElement('canvas');
        return !!(window.WebGLRenderingContext && (canvas.getContext('webgl') || canvas.getContext('experimental-webgl')));
    } catch (_) {
        return false;
    }
}

function normalizeNodeId(n, fallback) {
    const raw = n?.id ?? n?.tag_id ?? n?.tagId ?? n?.memId ?? n?.name ?? n?.label ?? fallback;
    return String(raw);
}

function normalizeEdgeEndpoint(value) {
    if (value === undefined || value === null) return '';
    const raw = String(value);
    if (graphState.nodes.has(raw)) return raw;
    if (graphState.labelIndex.has(raw)) return graphState.labelIndex.get(raw);
    return raw;
}

function edgeKey(source, target, label='', identity='') {
    const a = String(source);
    const b = String(target);
    return `${a}::${b}::${label}::${identity}`;
}

function hashString(str) {
    let h = 2166136261;
    for (let i = 0; i < String(str).length; i++) {
        h ^= String(str).charCodeAt(i);
        h = Math.imul(h, 16777619);
    }
    return h >>> 0;
}

function getNeighbors(nodeId) {
    return Array.from(graphState.adjacency.get(String(nodeId)) || []);
}

function getEdgesForNode(nodeId) {
    const id = String(nodeId);
    return Array.from(graphState.edges.values()).filter(e => e.source === id || e.target === id);
}

function removeEdgeFromAdjacency(record) {
    if (!record) return;
    graphState.adjacency.get(record.source)?.delete(record.target);
    graphState.adjacency.get(record.target)?.delete(record.source);
}

function replaceEdgeKey(record, newLabel) {
    if (!record) return record;
    graphState.edges.delete(record.key);
    record.label = newLabel;
    record.raw.label = newLabel;
    record.raw.l = newLabel;
    const identity = record.raw.id || record.raw.fact_id || record.raw.relation_id || record.raw.source_memory_id || '';
    record.key = edgeKey(record.source, record.target, newLabel, identity);
    graphState.edges.set(record.key, record);
    if (selectedEdge) selectedEdge = record.key;
    return record;
}

function getNodeRecord(nodeId) {
    return graphState.nodes.get(String(nodeId));
}

function clearGraphState() {
    graphState.nodes.clear();
    graphState.edges.clear();
    graphState.adjacency.clear();
    graphState.labelIndex.clear();
    pickableNodeObjects = [];
    pickableEdgeObjects = [];
    transientHoverLabelNode = null;
    flowParticles.forEach(p => {
        if (p.mesh && scene) scene.remove(p.mesh);
        disposeSceneObject(p.mesh);
    });
    flowParticles = [];
}

function refreshPickableObjects() {
    pickableNodeObjects = Array.from(graphState.nodes.values()).map(n => n.object).filter(Boolean);
    pickableEdgeObjects = Array.from(graphState.edges.values()).map(e => e.object).filter(Boolean);
}

function ensureAdjacency(nodeId) {
    if (!graphState.adjacency.has(nodeId)) graphState.adjacency.set(nodeId, new Set());
}

function addNodeRecord(rawNode, index=0, options={}) {
    const id = normalizeNodeId(rawNode, index);
    if (graphState.nodes.has(id)) return graphState.nodes.get(id);

    const type = normalizeNodeType(rawNode);
    const degree = rawNode.value || rawNode.degree || rawNode.weight || 1;
    const isSeed = rawNode.isSource || rawNode.isSeed || type === 'source';
    // 标签优化：memory 节点用 sender_name + 摘要，去掉裸 QQ 号噪声
    let label = rawNode.name || rawNode.label || id;
    if (type === 'memory' && rawNode.sender_name) {
        const cleanContent = (rawNode.content || rawNode.name || '').replace(/@[^\s(]*\(\d+\)\s*/g, '').trim();
        label = cleanContent ? `${rawNode.sender_name}: ${cleanContent.slice(0, 20)}` : rawNode.sender_name;
    } else if (label.length > 24) {
        label = label.slice(0, 22) + '…';
    }
    const color = isSeed ? TYPE_COLORS.source : colorForType(type);
    const radius = isSeed ? 1.9 : Math.max(0.55, Math.min(1.65, Math.log2((degree || 1) + 1) * 0.22 + 0.62));
    const position = rawNode._position || computeNodePosition(rawNode, index, options);

    const record = {
        id, label, type, degree, color, radius,
        raw: { ...rawNode, type, degree },
        position,
        object: null,
        labelObject: null,
        visible: true,
    };
    graphState.nodes.set(id, record);
    graphState.labelIndex.set(id, id);
    graphState.labelIndex.set(label, id);
    if (rawNode.name) graphState.labelIndex.set(rawNode.name, id);
    if (rawNode.label) graphState.labelIndex.set(rawNode.label, id);
    ensureAdjacency(id);
    return record;
}

function addEdgeRecord(rawEdge) {
    const label = rawEdge.label || rawEdge.relation_type || rawEdge.l || '';
    const source = normalizeEdgeEndpoint(rawEdge.source ?? rawEdge.from ?? rawEdge.s);
    const target = normalizeEdgeEndpoint(rawEdge.target ?? rawEdge.to ?? rawEdge.t);
    if (!source || !target || source === target) return null;
    if (!graphState.nodes.has(source) || !graphState.nodes.has(target)) return null;

    const identity = rawEdge.id || rawEdge.fact_id || rawEdge.relation_id || rawEdge.source_memory_id || '';
    const key = edgeKey(source, target, label, identity);
    const reverseKey = edgeKey(target, source, label, identity);
    if (graphState.edges.has(key)) return graphState.edges.get(key);
    if (graphState.edges.has(reverseKey)) return graphState.edges.get(reverseKey);

    const weight = rawEdge.value || rawEdge.weight || rawEdge.w || rawEdge.count || 1;
    const layer = normalizeEdgeLayer(rawEdge.layer);
    
    const record = {
        key, source, target, label,
        weight,
        isPath: !!rawEdge.isPath,
        layer,
        raw: { ...rawEdge, source, target, label, weight, layer },
        object: null,
        labelObject: null,
        visible: true,
    };
    graphState.edges.set(key, record);
    ensureAdjacency(source);
    ensureAdjacency(target);
    graphState.adjacency.get(source).add(target);
    graphState.adjacency.get(target).add(source);
    return record;
}

function computeNodePosition(node, index, options={}) {
    if (node.x !== undefined && node.y !== undefined && node.z !== undefined) {
        return new THREE.Vector3(Number(node.x), Number(node.y), Number(node.z));
    }
    const total = Math.max(1, options.total || 1);
    const layout = options.layout || 'galaxy';
    const idSeed = hashString(node.name || node.label || node.id || index);

    if (layout === 'path') {
        const step = 12;
        return new THREE.Vector3((index - (total - 1) / 2) * step, Math.sin(index * 0.9) * 2.2, 0);
    }

    if (layout === 'query') {
        if (node.type === 'source' || node.isSource) return new THREE.Vector3(0, 0, 0);
        const angle = (index / Math.max(1, total - 1)) * Math.PI * 2;
        const ring = 13 + (idSeed % 5);
        return new THREE.Vector3(Math.cos(angle) * ring, Math.sin(angle * 1.7) * 4, Math.sin(angle) * ring);
    }

    if (options.anchor) {
        const angle = ((idSeed % 360) * DEG2RAD);
        const lift = (((idSeed >> 8) % 100) / 100 - 0.5) * 12;
        const dist = 8 + ((idSeed >> 16) % 100) / 16;
        return options.anchor.clone().add(new THREE.Vector3(Math.cos(angle) * dist, lift, Math.sin(angle) * dist));
    }

    const golden = Math.PI * (3 - Math.sqrt(5));
    const y = 1 - (index / Math.max(1, total - 1)) * 2;
    const radiusAtY = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = golden * index + ((idSeed % 97) / 97) * 0.4;
    const degree = node.degree || node.value || node.weight || 1;
    const scale = 20 + Math.min(18, Math.log2(degree + 1) * 4) + Math.min(16, total / 16);
    return new THREE.Vector3(Math.cos(theta) * radiusAtY * scale, y * scale * 0.72, Math.sin(theta) * radiusAtY * scale);
}

// ─── Three.js 核心初始化与高级后处理 ───
function initGraph() {
    if (typeof THREE === 'undefined') {
        console.error('[WaveMemory] THREE.js 未加载，无法初始化 3D 神经云图');
        return;
    }
    const container = document.getElementById('galaxy-container');
    if (!container) return;

    disposeGraph();
    galaxyContainer = container;
    galaxyContainer.innerHTML = '';
    graphUnavailableReason = '';

    if (!isWebGLAvailable()) {
        showGraphUnavailable('当前运行环境没有可用的 WebGL context。');
        return;
    }

    scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x020408, 0.0035); // takram 风格深空透镜软雾
    camera = new THREE.PerspectiveCamera(54, window.innerWidth / window.innerHeight, 0.1, 2500);
    camera.position.set(0, 25, 75);

    try {
        webglRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    } catch (err) {
        console.warn('[WaveMemory] WebGLRenderer 初始化失败', err);
        scene = null;
        camera = null;
        webglRenderer = null;
        showGraphUnavailable(err?.message || 'WebGLRenderer 初始化失败。');
        return;
    }
    webglRenderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    webglRenderer.setSize(window.innerWidth, window.innerHeight);
    webglRenderer.setClearColor(0x020408, 1);
    galaxyContainer.appendChild(webglRenderer.domElement);

    controls = new THREE.OrbitControls(camera, webglRenderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05; // 丝滑阻尼
    controls.rotateSpeed = 0.32;
    controls.zoomSpeed = 0.55;
    controls.minDistance = 8;
    controls.maxDistance = 300;
    controls.addEventListener?.('change', updateActionRingPosition);

    raycaster = new THREE.Raycaster();
    mouse = new THREE.Vector2();

    graphGroup = new THREE.Group();
    edgeGroup = new THREE.Group();
    edgeLabelGroup = new THREE.Group();
    labelGroup = new THREE.Group();
    scene.add(edgeGroup);
    scene.add(edgeLabelGroup);
    scene.add(graphGroup);
    scene.add(labelGroup);

    buildNebulaField(); // 视差背景星野
    setupLights();
    setupBloom(); // 赛博朋克后期光晕
    setupPointerEvents();
    window.addEventListener('resize', onWindowResize);
    animate();
}

function setupLights() {
    scene.add(new THREE.AmbientLight(0x8b5cf6, 0.42)); // 软紫色环境光
    const key = new THREE.PointLight(0x8b5cf6, 1.8, 300);
    key.position.set(45, 55, 40);
    scene.add(key);
    const rim = new THREE.PointLight(0x3b82f6, 1.3, 240);
    rim.position.set(-60, -30, -50);
    scene.add(rim);
}

function setupBloom() {
    composer = null;
    try {
        if (THREE.EffectComposer && THREE.RenderPass && THREE.UnrealBloomPass) {
            composer = new THREE.EffectComposer(webglRenderer);
            composer.addPass(new THREE.RenderPass(scene, camera));
            // 优雅的太空发光后处理：通透微光，不糊镜
            const bloom = new THREE.UnrealBloomPass(new THREE.Vector2(window.innerWidth, window.innerHeight), 0.48, 0.52, 0.24);
            composer.addPass(bloom);
        }
    } catch (e) {
        console.warn('[WaveMemory] Bloom 初始化失败，降级为普通 WebGL 渲染', e);
        composer = null;
    }
}

// 借鉴顶级 3D 图谱：三层视差深空星云与星尘粒子流
function buildNebulaField() {
    const palette = [
        new THREE.Color('#7de1ff'), // 冰青
        new THREE.Color('#a78bfa'), // 柔紫
        new THREE.Color('#f472b6'), // 粉红
        new THREE.Color('#ffffff'), // 纯白恒星
        new THREE.Color('#38bdf8')  // 天蓝
    ];
    
    // 1. 近景星尘 (1800 颗微晶粒子)
    const count = 1800;
    const positions = new Float32Array(count * 3);
    const colors = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
        const r = 80 + Math.random() * 180;
        const theta = Math.random() * Math.PI * 2;
        const phi = Math.acos(2 * Math.random() - 1);
        positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
        positions[i * 3 + 1] = r * Math.cos(phi) * 0.65;
        positions[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
        const c = palette[i % palette.length];
        colors[i * 3] = c.r;
        colors[i * 3 + 1] = c.g;
        colors[i * 3 + 2] = c.b;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    const mat = new THREE.PointsMaterial({ size: 0.85, vertexColors: true, transparent: true, opacity: 0.65, depthWrite: false, blending: THREE.AdditiveBlending });
    starField = new THREE.Points(geo, mat);
    scene.add(starField);

    // 2. 远景深空星团 (1600 颗产生视差与空间深度)
    const countOuter = 1600;
    const positionsOuter = new Float32Array(countOuter * 3);
    const colorsOuter = new Float32Array(countOuter * 3);
    for (let i = 0; i < countOuter; i++) {
        const r = 240 + Math.random() * 320;
        const theta = Math.random() * Math.PI * 2;
        const phi = Math.acos(2 * Math.random() - 1);
        positionsOuter[i * 3] = r * Math.sin(phi) * Math.cos(theta);
        positionsOuter[i * 3 + 1] = r * Math.cos(phi) * 0.55;
        positionsOuter[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
        const c = palette[(i + 1) % palette.length];
        colorsOuter[i * 3] = c.r * 0.75;
        colorsOuter[i * 3 + 1] = c.g * 0.75;
        colorsOuter[i * 3 + 2] = c.b * 0.75;
    }
    const geoOuter = new THREE.BufferGeometry();
    geoOuter.setAttribute('position', new THREE.BufferAttribute(positionsOuter, 3));
    geoOuter.setAttribute('color', new THREE.BufferAttribute(colorsOuter, 3));
    const matOuter = new THREE.PointsMaterial({ size: 1.35, vertexColors: true, transparent: true, opacity: 0.45, depthWrite: false, blending: THREE.AdditiveBlending });
    starFieldOuter = new THREE.Points(geoOuter, matOuter);
    scene.add(starFieldOuter);
}

function setupPointerEvents() {
    const tooltip = document.getElementById('node-tooltip');
    pointerHandlers = {
        mousemove(event) {
            latestPointerEvent = event;
            if (tooltip) moveTooltip(event, tooltip);
            if (pointerFramePending) return;
            pointerFramePending = true;
            requestAnimationFrame(processPointerFrame);
        },
        mouseleave() {
            latestPointerEvent = null;
            setHoveredNode(null);
            setHoveredEdge(null);
        },
        click() {
            if (hoveredNode) selectNodeById(hoveredNode);
            else if (hoveredEdge) selectEdgeByKey(hoveredEdge);
            else {
                selectedNode = null;
                selectedEdge = null;
                relationState.selected = null;
                selectedFact = null;
                selectedFactEntity = null;
                hideDetail();
                hideRelationDetail();
                createContextActionRing(null);
                applyVisibility();
            }
        },
        dblclick() {
            if (!hoveredNode) return;
            selectedNode = hoveredNode;
            expandNode();
        },
    };
    Object.entries(pointerHandlers).forEach(([event, handler]) => galaxyContainer.addEventListener(event, handler));
}

// 缺陷自愈 5：引入 DevicePixelRatio 设备像素缩放因子适配坐标检测 (精确 3D 点击)
function updateMouse(event) {
    const rect = galaxyContainer.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
}

function processPointerFrame() {
    pointerFramePending = false;
    if (!latestPointerEvent || !galaxyContainer) return;
    updateMouse(latestPointerEvent);
    const hit = pickNode();
    const edgeHit = hit ? null : pickEdge();
    if (hit !== hoveredNode) setHoveredNode(hit);
    if (edgeHit !== hoveredEdge) setHoveredEdge(edgeHit);
}

function pickNode() {
    if (!raycaster || !camera) return null;
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObjects(pickableNodeObjects, false);
    return hits.length ? hits[0].object.userData.nodeId : null;
}

function pickEdge() {
    if (!raycaster || !camera) return null;
    raycaster.params.Line = raycaster.params.Line || {};
    raycaster.params.Line.threshold = 1.6; // 稍微扩大判定边界，利于鼠标轻松悬停选中
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObjects(pickableEdgeObjects, false);
    return hits.length ? hits[0].object.userData.edgeKey : null;
}

function setHoveredEdge(edgeKeyValue) {
    hoveredEdge = edgeKeyValue;
    relationState.hovered = edgeKeyValue ? graphState.edges.get(edgeKeyValue) : null;
    applyVisibility();
}

function setHoveredNode(nodeId) {
    hoveredNode = nodeId;
    hoveredNeighbors = nodeId ? new Set(getNeighbors(nodeId)) : new Set();
    if (galaxyContainer) galaxyContainer.style.cursor = nodeId ? 'pointer' : 'grab';
    const tooltip = document.getElementById('node-tooltip');
    if (tooltip) {
        if (nodeId) showNodeTooltip(nodeId, tooltip);
        else tooltip.classList.remove('visible');
    }
    ensureHoverLabel(nodeId);
    applyVisibility();
}

window.addEventListener('beforeunload', disposeGraph);

function moveTooltip(event, tooltip) {
    tooltip.style.left = (event.clientX + 16) + 'px';
    tooltip.style.top = (event.clientY - 10) + 'px';
    const rect = tooltip.getBoundingClientRect();
    if (rect.right > window.innerWidth - 10) tooltip.style.left = (event.clientX - rect.width - 16) + 'px';
    if (rect.bottom > window.innerHeight - 10) tooltip.style.top = (event.clientY - rect.height - 10) + 'px';
}

function showNodeTooltip(nodeId, tooltip) {
    const rec = getNodeRecord(nodeId);
    if (!rec) return;
    const neighbors = getNeighbors(nodeId);
    tooltip.innerHTML = `
        <div class="tt-name">${escapeHtml(rec.label)}</div>
        <span class="tt-type" style="background:${rec.color}20; color:${rec.color}; border: 1px solid ${rec.color}40">${escapeHtml(TYPE_LABELS[rec.type] || rec.type || '未知')}</span>
        <div class="tt-meta">
            ${rec.raw.score ? `相似度: ${Number(rec.raw.score).toFixed(3)}<br>` : ''}
            ${rec.raw.community !== undefined ? `社区: #${escapeHtml(rec.raw.community)}<br>` : ''}
            连接数: ${neighbors.length}
        </div>
        ${neighbors.length ? `<div class="tt-neighbors">邻居: ${neighbors.slice(0, 5).map(n => escapeHtml(getNodeRecord(n)?.label || n)).join(', ')}${neighbors.length > 5 ? ` +${neighbors.length - 5}` : ''}</div>` : ''}
    `;
    tooltip.classList.add('visible');
}

// 缺陷自愈 4：解决 EffectComposer 的全屏比例像素拉伸
function onWindowResize() {
    if (!camera || !webglRenderer) return;
    const w = window.innerWidth;
    const h = window.innerHeight;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    webglRenderer.setSize(w, h);
    if (composer) composer.setSize(w, h);
}

// 极致美化 B & C & F：动画线程更新双层星场视差自转，和能量粒子连线流流动
function animate() {
    animationId = requestAnimationFrame(animate);
    const t = performance.now() * 0.001;
    
    // 双图层视差自转
    if (starField) starField.rotation.y = t * 0.006;
    if (starFieldOuter) starFieldOuter.rotation.y = -t * 0.003;
    
    // 默认保持拓扑静止，让用户在阅读和选中时不会被持续自转打断。
    // 背景星尘仍保持极慢视差，保留空间感但不移动数据对象。
    if (graphGroup && graphGroup.rotation.y !== 0) graphGroup.rotation.y = 0;
    if (edgeGroup) edgeGroup.rotation.y = graphGroup?.rotation.y || 0;
    if (edgeLabelGroup) {
        edgeLabelGroup.rotation.y = graphGroup?.rotation.y || 0;
        edgeLabelGroup.children.forEach(sprite => sprite.lookAt(camera.position));
    }
    if (labelGroup) {
        labelGroup.rotation.y = graphGroup.rotation.y;
        labelGroup.children.forEach(sprite => sprite.lookAt(camera.position));
    }

    // 粒子沿 Curve 路径流淌机制 + 脉冲波衰减效果
    flowParticles.forEach(p => {
        p.progress += p.speed;
        if (p.progress > 1) {
            p.progress = 0; // 重复流动
            p.phaseOffset = Math.random() * 0.5; // 每次重置随机错开节奏
        }
        if (p.curve && p.mesh) {
            // 获取样条线当前进度的空间物理位置
            const point = p.curve.getPointAt(p.progress);
            // 补偿图组自转导致的物理世界位移
            p.mesh.position.copy(point).applyMatrix4(graphGroup.matrixWorld);
            
            // 脉冲波衰减：起始大、到达目标时缩小
            const pulseScale = 1.4 - p.progress * 0.8; // 1.4 → 0.6
            const pulseOpacity = 0.9 - p.progress * 0.6; // 0.9 → 0.3
            
            // 节点选定或悬停时，关联连线粒子呼吸闪烁
            const isHoveredLine = hoveredNode && (p.sourceId === hoveredNode || p.targetId === hoveredNode);
            p.mesh.scale.setScalar(isHoveredLine ? 2.0 : pulseScale);
            if (p.mesh.material) p.mesh.material.opacity = isHoveredLine ? 1.0 : pulseOpacity;
        }
    });

    // 节点呼吸与多面体微自转（让晶体、八面体与 Torus 环拥有生命感）
    if (graphGroup) {
        graphState.nodes.forEach(record => {
            if (!record.object || !record.visible) return;
            // 多态晶体原地微自旋
            if (record.type === 'memory' || record.type === 'person' || record.type === 'bot') {
                record.object.rotation.y += 0.008;
                record.object.rotation.x += 0.004;
            } else if (record.type === 'fact' || record.type === 'relationship_event') {
                record.object.rotation.z += 0.012;
            }
            if (record.id === hoveredNode || record.id === selectedNode) return;
            const breathe = 1 + Math.sin(t * 1.5 + record.position.x * 0.1 + record.position.z * 0.07) * 0.05;
            record.object.scale.setScalar(record.radius * breathe);
        });
    }

    if (controls) controls.update();
    if (composer) composer.render();
    else if (webglRenderer) webglRenderer.render(scene, camera);
}

// ─── 核心图谱拓扑力学与粒子星海绘制 ───
function renderGraph(nodes, edges, options={}) {
    if (!graphGroup || !edgeGroup || !labelGroup) return;
    clearGraph3D();
    clearGraphState();
    const renderOptions = { ...options, total: nodes.length || 1 };

    nodes.forEach((n, i) => addNodeRecord(n, i, renderOptions));
    edges.forEach(e => addEdgeRecord(e));
    applySemanticLayout(renderOptions);

    graphState.nodes.forEach((record) => createNodeObject(record));
    graphState.edges.forEach((record) => createEdgeObject(record));
    
    // 生成粒子数据流
    createFlowParticles();
    
    refreshPickableObjects();
    createAllReadableLabels();
    createImportantEdgeLabels();
    updateStats();
    applyVisibility();
    flyToGraph();
    initLegend(); // 渲染完毕后刷新动态图例（只显示实际出现的类型）
}

function createFlowParticles() {
    flowParticles.forEach(p => {
        if (p.mesh && scene) scene.remove(p.mesh);
        disposeSceneObject(p.mesh);
    });
    flowParticles = [];

    // 精选前 80 条高权重连线渲染流动粒子，防止过载导致 Draw Call 骤增
    const sortedEdges = Array.from(graphState.edges.values())
        .filter(e => e.visible)
        .sort((a,b) => b.weight - a.weight)
        .slice(0, 80);

    const particleGeometry = new THREE.SphereGeometry(0.18, 8, 8);

    sortedEdges.forEach(edge => {
        const sNode = getNodeRecord(edge.source);
        const tNode = getNodeRecord(edge.target);
        if (!sNode || !tNode) return;

        // 建立二次贝塞尔曲线 Spline 连线路径，带有 12% 轴向上凸起
        const midPoint = sNode.position.clone().lerp(tNode.position, 0.5);
        midPoint.y += sNode.position.distanceTo(tNode.position) * 0.12;
        const curve = new THREE.QuadraticBezierCurve3(sNode.position, midPoint, tNode.position);

        const colorHex = edge.isPath ? '#fbbf24' : (edge.layer === 'jargon' ? '#fb7185' : sNode.color);
        const mat = new THREE.MeshBasicMaterial({
            color: new THREE.Color(colorHex),
            transparent: true,
            opacity: 0.95,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
        });

        const mesh = new THREE.Mesh(particleGeometry, mat);
        scene.add(mesh);

        flowParticles.push({
            curve,
            mesh,
            progress: Math.random(), // 随机起点避免整齐划一
            speed: 0.004 + Math.random() * 0.005, // 优雅的光速传递
            sourceId: edge.source,
            targetId: edge.target,
        });
    });
}

function disposeSceneObject(child) {
    if (!child) return;
    if (child.geometry && child.geometry !== NODE_GEOMETRY) child.geometry.dispose?.();
    const materials = Array.isArray(child.material) ? child.material : (child.material ? [child.material] : []);
    materials.forEach(mat => {
        if (mat.map) mat.map.dispose?.();
        mat.dispose?.();
    });
}

function clearGraph3D() {
    if (!graphGroup || !edgeGroup || !labelGroup) return;
    [graphGroup, edgeGroup, edgeLabelGroup, labelGroup].filter(Boolean).forEach(group => {
        while (group.children.length) {
            const child = group.children.pop();
            disposeSceneObject(child);
        }
    });
}

function applySemanticLayout(options={}) {
    const records = Array.from(graphState.nodes.values());
    if (!records.length || typeof THREE === 'undefined') return;
    const mode = layoutMode || options.layout || 'semantic';
    const islandKeys = Array.from(new Set(records.map(r => {
        if (mode === 'layer') return r.raw.layer || r.raw.sourceLayer || r.type || 'facts';
        if (mode === 'type') return r.type || 'entity';
        if (mode === 'time') return 'timeline';
        return r.raw.community !== undefined ? `community-${r.raw.community}` : (r.type || r.raw.layer || 'entity');
    })));
    const centers = new Map();
    const islandRadius = Math.max(40, Math.min(120, 30 + records.length * 0.25));
    islandKeys.forEach((key, idx) => {
        const angle = (idx / Math.max(1, islandKeys.length)) * Math.PI * 2;
        centers.set(key, new THREE.Vector3(Math.cos(angle) * islandRadius, ((idx % 3) - 1) * 9, Math.sin(angle) * islandRadius));
    });
    const perIslandIndex = new Map();
    records.forEach((record, idx) => {
        if (record.raw.isSource || record.type === 'source') {
            record.position.set(0, 0, 0);
            return;
        }
        if (options.layout === 'path') {
            record.position.copy(computeNodePosition(record.raw, idx, { ...options, layout: 'path', total: records.length }));
            return;
        }
        if (options.layout === 'query') {
            record.position.copy(computeNodePosition(record.raw, idx, { ...options, layout: 'query', total: records.length }));
            return;
        }
        const key = mode === 'layer' ? (record.raw.layer || record.type || 'facts')
            : mode === 'type' ? (record.type || 'entity')
            : mode === 'time' ? 'timeline'
            : (record.raw.community !== undefined ? `community-${record.raw.community}` : (record.type || record.raw.layer || 'entity'));
        const localIndex = perIslandIndex.get(key) || 0;
        perIslandIndex.set(key, localIndex + 1);
        if (mode === 'time') {
            const ts = Number(record.raw.ts || record.raw.timestamp || record.raw.created_at || 0);
            const angle = idx * 0.55;
            const radius = 8 + idx * 0.32;
            const y = ts ? Math.sin(idx * 0.23) * 18 : ((idx % 9) - 4) * 2.2;
            record.position.set(Math.cos(angle) * radius, y, Math.sin(angle) * radius);
            return;
        }
        const center = centers.get(key) || new THREE.Vector3(0, 0, 0);
        const seed = hashString(record.id + key);
        const angle = ((seed % 360) * DEG2RAD) + localIndex * 0.72;
        const lane = Math.floor(localIndex / 8);
        const radius = 4 + (localIndex % 8) * 2.6 + lane * 5.2 + Math.min(6, (record.degree || 1) * 0.16);
        const lift = (((seed >> 8) % 100) / 100 - 0.5) * 14;
        record.position.copy(center.clone().add(new THREE.Vector3(Math.cos(angle) * radius, lift, Math.sin(angle) * radius)));
    });
    // 基于 3D Spring-Force 轻量引力质点排斥微调
    if (records.length <= 400) relaxNodePositions(4);
}

function relaxNodePositions(iterations=3) {
    const records = Array.from(graphState.nodes.values());
    const nodeCount = records.length;
    const maxIter = nodeCount <= 400 ? 20 : 8;
    const effectiveIter = Math.max(iterations, maxIter);
    const repulsionRadius = 12;
    const repulsionStrength = 0.35;
    const springIdeal = 10;
    const springStrength = 0.04;
    const gravityStrength = 0.01;
    const damping = 0.92;

    for (let iter = 0; iter < effectiveIter; iter++) {
        // 库仑斥力
        for (let i = 0; i < records.length; i++) {
            for (let j = i + 1; j < records.length; j++) {
                const a = records[i];
                const b = records[j];
                const delta = a.position.clone().sub(b.position);
                const dist = Math.max(0.01, delta.length());
                if (dist > repulsionRadius) continue;
                const push = delta.normalize().multiplyScalar((repulsionRadius - dist) * repulsionStrength);
                a.position.add(push);
                b.position.sub(push);
            }
        }
        // 胡克弹簧吸引
        graphState.edges.forEach(edge => {
            const a = getNodeRecord(edge.source);
            const b = getNodeRecord(edge.target);
            if (!a || !b) return;
            const delta = b.position.clone().sub(a.position);
            const dist = delta.length();
            if (dist < springIdeal * 0.5 || dist > 70) return;
            const pull = delta.normalize().multiplyScalar(Math.min(3.0, (dist - springIdeal) * springStrength));
            a.position.add(pull);
            b.position.sub(pull);
        });
        // 重力：向各自 island center 微弱吸引，防止飘散
        records.forEach(record => {
            if (record.raw.isSource || record.type === 'source') return;
            const center = new THREE.Vector3(0, 0, 0);
            const toCenter = center.clone().sub(record.position);
            const dist = toCenter.length();
            if (dist > 5) {
                record.position.add(toCenter.normalize().multiplyScalar(dist * gravityStrength));
            }
        });
        // 阻尼防震荡
        records.forEach(record => {
            record.position.multiplyScalar(damping + (1 - damping) * 0.98);
        });
    }
}

function updateLayoutMode(value) {
    layoutMode = value || 'semantic';
    applySemanticLayout({ layout: currentView === 'path' ? 'path' : currentView === 'query' ? 'query' : 'galaxy', total: graphState.nodes.size || 1 });
    graphState.nodes.forEach(record => {
        if (!record.object) return;
        if (typeof gsap !== 'undefined') gsap.to(record.object.position, { x: record.position.x, y: record.position.y, z: record.position.z, duration: 0.8, ease: 'power3.out' });
        else record.object.position.copy(record.position);
    });
    rebuildEdgeObjects();
    createAllReadableLabels();
    createImportantEdgeLabels();
    applyVisibility();
}

function createNodeObject(record) {
    const isCore = record.raw.isSource || record.type === 'bot' || record.type === 'person' || (record.degree && record.degree >= 6);
    const nodeGeometry = geometryForNodeType(record.type) || NODE_GEOMETRY;

    // 1. 核心高反光微晶核心 (Crystal Core)
    const material = new THREE.MeshStandardMaterial({
        color: new THREE.Color(record.color),
        emissive: new THREE.Color(record.color),
        emissiveIntensity: isCore ? 1.8 : 0.8,
        roughness: 0.1,
        metalness: 0.85,
        transparent: true,
        opacity: isCore ? 1.0 : 0.92,
    });
    const mesh = new THREE.Mesh(nodeGeometry, material);
    mesh.position.copy(record.position);
    mesh.scale.setScalar(record.radius);
    mesh.userData.nodeId = record.id;
    mesh.userData.baseScale = record.radius;
    mesh.userData.nodeType = record.type;

    // 2. Cosmograph 星尘等离子发光外晕 (Stardust Plasma Halo)
    const glowTex = getStardustGlowTexture();
    const haloMat = new THREE.SpriteMaterial({
        map: glowTex,
        color: new THREE.Color(record.color),
        transparent: true,
        opacity: isCore ? 0.85 : 0.45,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
    });
    const haloSprite = new THREE.Sprite(haloMat);
    const haloScale = isCore ? 3.0 : 1.8;
    haloSprite.scale.set(haloScale, haloScale, 1);
    mesh.add(haloSprite);

    // 3. 核心恒星增加双重旋转轨道环 (Orbital Rings)
    if (isCore && (record.raw.isSource || record.type === 'bot' || record.degree >= 12)) {
        const ringGeo = new THREE.RingGeometry(record.radius * 1.5, record.radius * 1.58, 32);
        const ringMat = new THREE.MeshBasicMaterial({
            color: new THREE.Color(record.color),
            side: THREE.DoubleSide,
            transparent: true,
            opacity: 0.75,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
        });
        const ringMesh = new THREE.Mesh(ringGeo, ringMat);
        ringMesh.rotation.x = Math.PI / 3;
        ringMesh.userData.isRing = true;
        mesh.add(ringMesh);
    }

    graphGroup.add(mesh);
    record.object = mesh;
}

function createEdgeObject(record) {
    const a = getNodeRecord(record.source);
    const b = getNodeRecord(record.target);
    if (!a || !b) return;

    // 二次样条线作为连线，避让节点几何重叠，带 8% 微弱起伏
    const midPoint = a.position.clone().lerp(b.position, 0.5);
    midPoint.y += a.position.distanceTo(b.position) * 0.08;
    const curve = new THREE.QuadraticBezierCurve3(a.position, midPoint, b.position);
    const points = curve.getPoints(16);
    const geo = new THREE.BufferGeometry().setFromPoints(points);

    // 顶点渐变色：从 Source 颜色自然流向 Target 颜色
    const colorA = new THREE.Color(record.isPath ? '#fbbf24' : (a.color || '#38bdf8'));
    const colorB = new THREE.Color(record.isPath ? '#f59e0b' : (b.color || '#a855f7'));
    const colors = new Float32Array(points.length * 3);
    for (let i = 0; i < points.length; i++) {
        const ratio = i / (points.length - 1);
        const c = colorA.clone().lerp(colorB, ratio);
        colors[i * 3] = c.r;
        colors[i * 3 + 1] = c.g;
        colors[i * 3 + 2] = c.b;
    }
    geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));

    // Cosmograph 级极细微光光纤丝（平时极克制，不遮挡星系）
    const baseOpacity = record.isPath ? 0.92 : (record.raw.kind === 'fact' ? 0.16 : 0.045);
    const mat = new THREE.LineBasicMaterial({
        vertexColors: true,
        transparent: true,
        opacity: baseOpacity,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
    });
    const line = new THREE.Line(geo, mat);
    line.userData.edgeKey = record.key;
    line.userData.baseOpacity = baseOpacity;
    edgeGroup.add(line);
    record.object = line;
}

function rebuildEdgeObjects() {
    if (!edgeGroup) return;
    while (edgeGroup.children.length) disposeSceneObject(edgeGroup.children.pop());
    graphState.edges.forEach(record => {
        record.object = null;
        createEdgeObject(record);
    });
    createFlowParticles(); // 重新跟随重组连线流动粒子
    refreshPickableObjects();
}

function createImportantLabels() {
    createAllReadableLabels();
}

function createAllReadableLabels() {
    if (!labelGroup) return;
    while (labelGroup.children.length) disposeSceneObject(labelGroup.children.pop());
    graphState.nodes.forEach(record => { record.labelObject = null; });
    const records = Array.from(graphState.nodes.values()).sort((a, b) => (b.degree || 0) - (a.degree || 0));
    
    // 借鉴 Cosmograph：默认只展示前 3~5 个核心恒星，其余按需 Hover 浮现，保证星海纯净深邃
    let selectedRecords = [];
    if (labelDensity === 'all') {
        selectedRecords = records.slice(0, Math.min(30, records.length));
    } else if (labelDensity === 'core') {
        selectedRecords = records.filter(r => r.raw.isSource || r.type === 'bot' || (r.degree && r.degree >= 12)).slice(0, 6);
    } else { // focus 模式
        const focusSet = new Set();
        if (selectedNode) {
            focusSet.add(selectedNode);
            getNeighbors(selectedNode).slice(0, 6).forEach(n => focusSet.add(n));
        }
        if (hoveredNode) focusSet.add(hoveredNode);
        selectedRecords = records.filter(r => r.raw.isSource || r.type === 'bot' || focusSet.has(r.id));
    }

    selectedRecords.forEach(record => {
        const sprite = createTextSprite(record.label, record.color, record.radius);
        sprite.position.copy(record.position).add(new THREE.Vector3(record.radius * 1.8, record.radius * 0.9, 0));
        sprite.userData.nodeId = record.id;
        labelGroup.add(sprite);
        record.labelObject = sprite;
    });
}

function ensureHoverLabel(nodeId) {
    if (!labelGroup) return;
    if (transientHoverLabelNode && transientHoverLabelNode !== nodeId) {
        const prev = getNodeRecord(transientHoverLabelNode);
        if (prev?.labelObject?.userData?.transient) {
            labelGroup.remove(prev.labelObject);
            disposeSceneObject(prev.labelObject);
            prev.labelObject = null;
        }
        transientHoverLabelNode = null;
    }
    if (!nodeId) return;
    const record = getNodeRecord(nodeId);
    if (!record || record.labelObject) return;
    const sprite = createTextSprite(record.label, record.color, record.radius);
    sprite.position.copy(record.position).add(new THREE.Vector3(record.radius * 1.7, record.radius * 0.7, 0));
    sprite.userData.nodeId = record.id;
    sprite.userData.transient = true;
    labelGroup.add(sprite);
    record.labelObject = sprite;
    transientHoverLabelNode = nodeId;
}

function setLabelDensity(value) {
    labelDensity = value || 'focus';
    createAllReadableLabels();
    createImportantEdgeLabels();
    applyVisibility();
}

function createImportantEdgeLabels() {
    if (!edgeLabelGroup) return;
    while (edgeLabelGroup.children.length) disposeSceneObject(edgeLabelGroup.children.pop());
    graphState.edges.forEach(record => { record.labelObject = null; });
    const records = Array.from(graphState.edges.values()).sort((a, b) => (Number(b.weight || 0) - Number(a.weight || 0)));
    const limit = labelDensity === 'all' ? Math.min(90, records.length) : labelDensity === 'focus' ? Math.min(28, records.length) : Math.min(12, records.length);
    records.slice(0, limit).forEach(record => createEdgeLabelObject(record));
}

function createEdgeLabelObject(record) {
    const a = getNodeRecord(record.source);
    const b = getNodeRecord(record.target);
    if (!a || !b || !record.label) return null;
    const color = record.isPath ? '#fbbf24' : (record.raw.kind === 'fact' ? '#c4b5fd' : '#93c5fd');
    const sprite = createTextSprite(record.label, color, 0.7);
    sprite.position.copy(a.position).lerp(b.position, 0.5).add(new THREE.Vector3(0, 1.3, 0));
    sprite.scale.multiplyScalar(0.58);
    sprite.userData.edgeKey = record.key;
    edgeLabelGroup.add(sprite);
    record.labelObject = sprite;
    return sprite;
}

function createTextSprite(text, color, radius) {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    const fontSize = 32;
    const label = String(text || '').slice(0, 24);
    ctx.font = `600 ${fontSize}px "Geist Variable", system-ui, -apple-system, sans-serif`;
    const textWidth = ctx.measureText(label).width;
    const width = Math.ceil(textWidth + 32);
    canvas.width = Math.max(96, width);
    canvas.height = 56;
    ctx.font = `600 ${fontSize}px "Geist Variable", system-ui, -apple-system, sans-serif`;
    // 现代太空 HUD 晶体胶囊：超薄毛玻璃背景 + 柔和光晕边缘
    ctx.fillStyle = 'rgba(10, 14, 23, 0.55)';
    ctx.strokeStyle = color + '66';
    ctx.lineWidth = 2;
    roundRect(ctx, 3, 5, canvas.width - 6, 46, 23);
    ctx.fill();
    ctx.stroke();
    // 柔和微光发光字
    ctx.fillStyle = '#ffffff';
    ctx.shadowColor = color;
    ctx.shadowBlur = 12;
    ctx.fillText(label, 16, 37);
    const texture = new THREE.CanvasTexture(canvas);
    texture.needsUpdate = true;
    const material = new THREE.SpriteMaterial({
        map: texture,
        transparent: true,
        depthWrite: false,
        blending: THREE.NormalBlending,
    });
    const sprite = new THREE.Sprite(material);
    const scale = Math.min(3.2, Math.max(1.6, radius * 1.5));
    sprite.scale.set((canvas.width / canvas.height) * scale, scale, 1);
    return sprite;
}

function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
}

function applyVisibility() {
    graphState.nodes.forEach(record => {
        const filterHidden = activeFilter && record.type !== activeFilter;
        const hoverDim = hoveredNode && record.id !== hoveredNode && !hoveredNeighbors.has(record.id);
        const selectedDim = selectedNode && record.id !== selectedNode && !getNeighbors(selectedNode).includes(record.id) && record.id !== hoveredNode;
        const visible = !filterHidden;
        record.visible = visible;
        if (record.object) {
            record.object.visible = visible;
            record.object.material.opacity = visible ? (hoverDim || selectedDim ? 0.22 : 0.95) : 0;
            const pulse = record.id === hoveredNode || record.id === selectedNode ? 1.45 : 1;
            record.object.scale.setScalar(record.radius * pulse);
        }
        if (record.labelObject) {
            const forceLabel = labelDensity === 'all' || record.id === hoveredNode || record.id === selectedNode || hoveredNeighbors.has(record.id);
            record.labelObject.visible = visible && (forceLabel || !(hoverDim || selectedDim));
        }
    });
    graphState.edges.forEach(record => {
        const a = getNodeRecord(record.source);
        const b = getNodeRecord(record.target);
        const filterHidden = activeFilter && a?.type !== activeFilter && b?.type !== activeFilter;
        const hoverHit = hoveredNode && (record.source === hoveredNode || record.target === hoveredNode);
        const selectedHit = selectedNode && (record.source === selectedNode || record.target === selectedNode);
        const edgeHit = record.key === hoveredEdge || record.key === selectedEdge;
        const visible = !filterHidden && a?.visible !== false && b?.visible !== false;
        record.visible = visible;
        if (record.object) {
            record.object.visible = visible;
            const baseOpacity = record.object.userData.baseOpacity || Math.max(0.12, Math.min(0.42, Number(record.weight || 1) / 4));
            record.object.material.opacity = visible ? ((edgeHit || hoverHit || selectedHit || record.isPath) ? 0.95 : (hoveredNode || selectedNode || hoveredEdge || selectedEdge ? 0.08 : baseOpacity)) : 0;
        }
        if (record.labelObject) {
            record.labelObject.visible = visible && (labelDensity === 'all' || edgeHit || hoverHit || selectedHit || record.isPath);
        }
    });
}

// 缺陷自愈与美化 13 & 14：GSAP 赛博朋克入场飞驰拉镜动画
function flyToGraph() {
    if (!camera || !controls) return;
    const count = Math.max(1, graphState.nodes.size);
    const distance = Math.max(34, Math.min(150, 38 + count * 0.32));
    if (typeof gsap !== 'undefined') {
        // 从极其深邃的太空中（x=20, y=180, z=400）阻尼拉镜，营造极具冲击力的开场感觉
        gsap.fromTo(camera.position, 
            { x: 30, y: 150, z: 420 },
            { x: 0, y: Math.min(60, distance * 0.34), z: distance, duration: 1.6, ease: 'power4.out' }
        );
        gsap.to(controls.target, { x: 0, y: 0, z: 0, duration: 1.4, ease: 'power3.out' });
    } else {
        camera.position.set(0, distance * 0.34, distance);
        controls.target.set(0, 0, 0);
    }
}

function flyToNode(nodeId) {
    const record = getNodeRecord(nodeId);
    if (!record || !camera || !controls || !graphGroup) return;
    
    // 关键自愈修正：将图自转导致的本地相对坐标转换至 3D 物理世界的绝对空间坐标
    const target = record.position.clone();
    graphGroup.localToWorld(target);
    
    const camTarget = target.clone().add(new THREE.Vector3(0, Math.max(4, record.radius * 3.5), Math.max(12, record.radius * 10)));
    if (typeof gsap !== 'undefined') {
        gsap.killTweensOf(controls.target);
        gsap.killTweensOf(camera.position);
        gsap.to(controls.target, { 
            x: target.x, y: target.y, z: target.z, 
            duration: 0.8, ease: 'power2.out',
            onUpdate: () => controls.update()
        });
        gsap.to(camera.position, { 
            x: camTarget.x, y: camTarget.y, z: camTarget.z, 
            duration: 0.8, ease: 'power2.out' 
        });
    } else {
        controls.target.copy(target);
        camera.position.copy(camTarget);
        controls.update();
    }
    createScreenRipple(nodeId);
    if (actionRingNode) setTimeout(updateActionRingPosition, 810);
}

function createScreenRipple(nodeId) {
    const record = getNodeRecord(nodeId);
    if (!record || !record.object || !galaxyContainer) return;
    const p = record.object.position.clone();
    graphGroup.localToWorld(p);
    p.project(camera);
    const rect = galaxyContainer.getBoundingClientRect();
    const x = (p.x * 0.5 + 0.5) * rect.width + rect.left;
    const y = (-p.y * 0.5 + 0.5) * rect.height + rect.top;
    const ripple = document.createElement('div');
    ripple.style.position = 'fixed';
    ripple.style.left = x + 'px';
    ripple.style.top = y + 'px';
    ripple.style.width = '10px';
    ripple.style.height = '10px';
    ripple.style.transform = 'translate(-50%, -50%)';
    ripple.style.borderRadius = '50%';
    ripple.style.border = `2px solid ${record.color}`;
    ripple.style.boxShadow = `0 0 18px ${record.color}`;
    ripple.style.pointerEvents = 'none';
    ripple.style.zIndex = '99';
    document.body.appendChild(ripple);
    if (typeof gsap !== 'undefined') {
        gsap.fromTo(ripple, { width: '10px', height: '10px', opacity: 1 }, { width: '130px', height: '130px', opacity: 0, duration: 1.0, ease: 'power2.out', onComplete: () => ripple.remove() });
    } else {
        setTimeout(() => ripple.remove(), 1000);
    }
}

function appendGraphData(newNodes, newEdges) {
    if (!graphGroup || !edgeGroup || !labelGroup) return;
    const anchor = selectedNode && getNodeRecord(selectedNode) ? getNodeRecord(selectedNode).position : new THREE.Vector3(0, 0, 0);
    const startIndex = graphState.nodes.size;
    newNodes.forEach((n, idx) => addNodeRecord(n, startIndex + idx, { total: startIndex + newNodes.length, anchor }));
    newEdges.forEach(e => addEdgeRecord(e));

    graphState.nodes.forEach(record => {
        if (!record.object) createNodeObject(record);
    });
    graphState.edges.forEach(record => {
        if (!record.object) createEdgeObject(record);
    });
    
    createFlowParticles(); // 更新流动粒子
    refreshPickableObjects();
    while (labelGroup.children.length) {
        const child = labelGroup.children.pop();
        disposeSceneObject(child);
    }
    graphState.nodes.forEach(record => { record.labelObject = null; });
    createAllReadableLabels();
    createImportantEdgeLabels();
    updateStats();
    applyVisibility();
}

// ─── Legend ───
function initLegend() {
    const legend = document.getElementById('legend');
    if (!legend) return;
    // 动态图例：只显示当前图谱实际出现的节点类型
    const activeTypes = new Set();
    graphState.nodes.forEach(record => { if (record.type) activeTypes.add(record.type); });
    const types = activeTypes.size > 0
        ? Array.from(activeTypes).sort((a, b) => (TYPE_LABELS[a] || a).localeCompare(TYPE_LABELS[b] || b))
        : ['entity','topic','person','memory','event','fact','keyword'];
    legend.innerHTML = types.map(t => `
        <button class="legend-pill flex items-center gap-1.5 px-2.5 py-1.5 rounded-full text-[10px] cursor-pointer border border-transparent"
                data-type="${t}" style="background:${TYPE_COLORS[t] || '#94a3b8'}15; color:${TYPE_COLORS[t] || '#94a3b8'}; --pill-glow:${(TYPE_COLORS[t] || '#94a3b8')}40">
            <span class="w-2.5 h-2.5 rounded-full" style="background: radial-gradient(circle at 30% 30%, ${TYPE_COLORS[t] || '#94a3b8'}, ${(TYPE_COLORS[t] || '#94a3b8')}80)"></span>
            ${TYPE_LABELS[t] || t}
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
    applyVisibility();
}

// ─── Load Galaxy 与 社区 Cluster 空聚类保护 ───
async function loadGalaxy() {
    setEventStatus('loading', '加载图谱视图');
    renderEventWarnings([]);
    updateRuntimeConfigStatus();
    showLoading('正在加载 3D 知识星海...');
    try {
        const layers = [];
        document.querySelectorAll('#cfg-layers input[type=checkbox]').forEach(cb => {
            if (cb.checked) layers.push(cb.dataset.layer);
        });
        const layerParam = layers.length ? layers.join(',') : 'facts';
        const minConf = parseFloat(document.getElementById('cfg-min-confidence')?.value || '0.0');
        const memoryLimit = parseInt(document.getElementById('cfg-memory-limit')?.value || '150');
        const similarityK = parseInt(document.getElementById('cfg-similarity-k')?.value || '3');
        const similarityThreshold = parseFloat(document.getElementById('cfg-similarity-threshold')?.value || '0.65');
        const query = new URLSearchParams({
            layers: layerParam,
            min_confidence: String(minConf),
            memory_limit: String(memoryLimit),
            similarity_k: String(similarityK),
            similarity_threshold: String(similarityThreshold),
        });
        const res = await fetch(`/api/kg/full?${query.toString()}`);
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error?.code || `HTTP ${res.status}`);
        _kgFullNodes = data.nodes || [];
        _kgFullEdges = data.edges || [];
        _kgLayerCounts = data.layer_counts || {};
        _kgWarnings = data.warnings || [];
        renderEventWarnings(_kgWarnings.map(item => ({ stage: item.layer || 'graph', reason: item.reason || 'unavailable' })));
        showLoading(`已加载 ${_kgFullNodes.length} 节点 / ${_kgFullEdges.length} 连线，投射 WebGL 星空...`);
        if (!kgConfigLoaded) await loadKgConfig();
        applyKgConfig();
    } catch(e) {
        console.error('Load KG failed:', e);
        const message = e?.message || '知识星海加载失败';
        setEventStatus('error', '知识星海加载失败');
        renderEventWarnings([{ stage: 'kg_full', reason: message }]);
        showLoading('知识星海加载失败');
        settleInitialGalaxyLoad('initial-load-error', message);
        setTimeout(hideLoading, 1400);
        return;
    }
    setEventStatus('ok', '图谱视图已更新');
    settleInitialGalaxyLoad('initial-load-ready', `已加载 ${_kgFullNodes.length} 节点 / ${_kgFullEdges.length} 连线`);
    hideLoading();
}

function applyKgConfig() {
    if (!_kgFullEdges) { loadGalaxy(); return; }
    const maxNodes = parseInt(document.getElementById('cfg-max-nodes')?.value || '300');
    const minWeight = parseFloat(document.getElementById('cfg-min-weight')?.value || '0');
    const days = parseInt(document.getElementById('cfg-days')?.value || '0');
    const cutoff = days > 0 ? (Date.now()/1000 - days * 86400) : 0;
    const selectedLayers = new Set(Array.from(document.querySelectorAll('#cfg-layers input[type=checkbox]:checked')).map(cb => cb.dataset.layer));

    let filtered = [..._kgFullEdges].filter(e => !selectedLayers.size || selectedLayers.has(normalizeEdgeLayer(e.layer)) || selectedLayers.has(e.layer));
    if (minWeight > 0) filtered = filtered.filter(e => Number(e.w ?? e.weight ?? 0) >= minWeight);
    if (cutoff > 0) filtered = filtered.filter(e => !Number(e.ts || 0) || Number(e.ts) >= cutoff);
    if (typeof selectedRelTypes !== 'undefined' && typeof availableRelTypes !== 'undefined' && availableRelTypes.size > 0) {
        filtered = filtered.filter(e => {
            const label = e.l || e.label;
            return !availableRelTypes.has(label) || selectedRelTypes.has(label);
        });
    }
    if (typeof selectedNodeTypes !== 'undefined' && selectedNodeTypes.size > 0) {
        filtered = filtered.filter(e => selectedNodeTypes.has(normalizeNodeType({type:e.st})) || selectedNodeTypes.has(normalizeNodeType({type:e.tt})));
    }

    filtered.sort((a, b) => Number(b.w ?? b.weight ?? 0) - Number(a.w ?? a.weight ?? 0));
    filtered = filtered.slice(0, Math.ceil(maxNodes * 3));

    const explicitById = new Map((_kgFullNodes || []).map(node => [String(node.id), node]));
    const degree = new Map();
    for (const edge of filtered) {
        const source = String(edge.s ?? edge.source);
        const target = String(edge.t ?? edge.target);
        degree.set(source, (degree.get(source) || 0) + 1);
        degree.set(target, (degree.get(target) || 0) + 1);
        if (!explicitById.has(source)) explicitById.set(source, {id:source, name:source, type:edge.st || 'entity', layer:edge.layer});
        if (!explicitById.has(target)) explicitById.set(target, {id:target, name:target, type:edge.tt || 'entity', layer:edge.layer});
    }

    let candidates = Array.from(explicitById.values()).filter(node => {
        const layer = normalizeEdgeLayer(node.layer || 'facts');
        const type = normalizeNodeType(node);
        return (!selectedLayers.size || selectedLayers.has(layer) || degree.has(String(node.id)))
            && (!selectedNodeTypes.size || selectedNodeTypes.has(type));
    });
    candidates.sort((a,b) => (degree.get(String(b.id)) || b.degree || b.weight || 0) - (degree.get(String(a.id)) || a.degree || a.weight || 0));
    candidates = candidates.slice(0, maxNodes);
    const topSet = new Set(candidates.map(node => String(node.id)));
    const nodes = candidates.map(node => ({...node, id:String(node.id), degree:degree.get(String(node.id)) || node.degree || 0}));
    const edges = filtered
        .filter(e => topSet.has(String(e.s ?? e.source)) && topSet.has(String(e.t ?? e.target)))
        .map(e => ({ ...e, source:String(e.s ?? e.source), target:String(e.t ?? e.target), label:e.l || e.label, weight:e.w ?? e.weight, layer:normalizeEdgeLayer(e.layer) }));

    renderGraph(nodes, edges, { layout: 'galaxy' });
    updateRuntimeConfigStatus();
    const status = document.getElementById('cfg-status');
    const layerSummary = Object.entries(_kgLayerCounts || {}).map(([layer, count]) => `${layer}:${count?.nodes ?? 0}/${count?.edges ?? 0}`).join(' · ');
    if (status) status.textContent = `显示 ${nodes.length} 节点 / ${edges.length} 连线（后端总 ${_kgFullNodes.length}/${_kgFullEdges.length}）${layerSummary ? ` · ${layerSummary}` : ''}`;
}

// ─── Query ───
function applyQueryPreset(preset) {
    const stageMap = {
        baseline: { epa: false, pyramid: false, spike: false, geodesic: false },
        spike: { epa: false, pyramid: false, spike: true, geodesic: false },
        pyramid: { epa: true, pyramid: true, spike: true, geodesic: false },
        full: { epa: true, pyramid: true, spike: true, geodesic: true },
        ablation: { epa: true, pyramid: true, spike: true, geodesic: true },
    };
    const stages = stageMap[preset] || stageMap.full;
    const bind = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.checked = value;
    };
    bind('query-stage-epa', stages.epa);
    bind('query-stage-pyramid', stages.pyramid);
    bind('query-stage-spike', stages.spike);
    bind('query-stage-geodesic', stages.geodesic);
    updateRuntimeConfigStatus();
}

function readQueryConfig() {
    const boolInput = (id, fallback=true) => {
        const el = document.getElementById(id);
        return el ? Boolean(el.checked) : fallback;
    };
    const numberInput = (id, fallback, min, max) => {
        const raw = Number(document.getElementById(id)?.value ?? fallback);
        const value = Number.isFinite(raw) ? raw : fallback;
        return Math.max(min, Math.min(max, value));
    };
    const modePreset = document.getElementById('query-mode-preset')?.value || 'full';
    const sourceFilter = String(document.getElementById('query-source-filter')?.value || '').trim();
    return {
        modePreset,
        sourceFilter,
        debug: boolInput('query-debug-toggle', true),
        topK: numberInput('query-top-k', 12, 1, 50),
        stages: {
            epa: boolInput('query-stage-epa', true),
            pyramid: boolInput('query-stage-pyramid', true),
            spike: boolInput('query-stage-spike', true),
            geodesic: boolInput('query-stage-geodesic', true),
        },
        params: {
            pyramid_top_k: numberInput('query-pyramid-top-k', 10, 1, 30),
            spike_max_hops: numberInput('query-spike-max-hops', 4, 1, 8),
            geodesic_alpha: numberInput('query-geodesic-alpha', 0.3, 0, 1),
        },
    };
}

async function doQuery() {
    const q = document.getElementById('search-input').value.trim();
    if (!q) return;
    setEventStatus('loading', '执行高级检索查询');
    renderEventWarnings([]);
    showLoading('正在语义检索...');
    try {
        const queryConfig = readQueryConfig();
        updateRuntimeConfigStatus();
        const res = await fetch('/api/query', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: q,
                top_k: queryConfig.topK,
                mode: queryConfig.modePreset,
                source_filter: queryConfig.sourceFilter,
                debug: queryConfig.debug,
                stages: queryConfig.stages,
                params: queryConfig.params,
            }),
        });
        const data = await res.json();
        if (!res.ok || data?.error) {
            const code = data?.error?.code || 'query_debug_failed';
            const message = data?.error?.message || '高级检索调试失败';
            setEventStatus('error', `${message} (${code})`);
            renderEventWarnings([{ stage: 'query', reason_code: code, reason: message }]);
            showLoading(`${message} · ${code}`);
            setTimeout(hideLoading, 1800);
            return;
        }
        if (data.results && data.results.length) {
            const nodes = [{ id: 'query-source', name: q, type: 'source', degree: data.results.length, isSource: true }];
            const edges = [];
            const debug = data.debug || {};
            const highlights = debug.highlights || {};
            const stageHighlightNodes = [];
            const stageHighlightEdges = [];
            const stageHighlightNodeIds = new Set();
            const addStageTagNode = (stageKey, label, tag, type='keyword') => {
                const tagId = tag && typeof tag === 'object' ? tag.tag_id : tag;
                if (tagId === undefined || tagId === null || tagId === '') return;
                const nodeId = `${stageKey}-${String(tagId)}`;
                if (!stageHighlightNodeIds.has(nodeId)) {
                    stageHighlightNodeIds.add(nodeId);
                    stageHighlightNodes.push({
                        id: nodeId,
                        name: `${label}: ${String(tagId)}`,
                        type,
                        degree: Math.max(1, Math.round((tag?.energy || tag?.weight || tag?.similarity || 0.5) * 10)),
                        source_stage: stageKey,
                        stage_label: label,
                        raw: tag,
                    });
                }
                stageHighlightEdges.push({ source: 'query-source', target: nodeId, label, weight: Math.max(0.25, tag?.energy || tag?.weight || tag?.similarity || 0.35) });
            };
            (highlights.pyramid_tags || []).forEach(tag => addStageTagNode('pyramid', '残差金字塔', tag, 'keyword'));
            (highlights.seed_tags || []).forEach(tag => addStageTagNode('spike-seed', '脉冲种子', tag, 'topic'));
            (highlights.emergent_tags || []).forEach(tag => addStageTagNode('spike-emergent', '脉冲涌现', tag, 'community'));
            data.results.forEach((m, i) => {
                const id = `mem-${m.id || i}`;
                nodes.push({ id, name: `${m.sender_name || '未知'}: ${(m.content || '').slice(0, 18)}`, type: 'memory', degree: Math.max(1, Math.round((m.score || 0.2) * 10)), content: m.content, sender: m.sender_name, ts: m.timestamp, score: m.score });
                edges.push({ source: 'query-source', target: id, label: '联想', weight: Math.max(0.3, m.score || 0.3) });
            });
            (highlights.geodesic_memory_ids || []).forEach(memId => {
                stageHighlightEdges.push({ source: 'query-source', target: `mem-${memId}`, label: '测地线重排', weight: 0.75 });
            });
            nodes.push(...stageHighlightNodes);
            edges.push(...stageHighlightEdges);
            renderGraph(nodes, edges, { layout: 'query' });
            showQueryDetail(q, data);
        } else {
            const debug = data.debug || {};
            const warnings = Array.isArray(debug.warnings) ? debug.warnings : [];
            renderEventWarnings(warnings);
            setEventStatus(warnings.length ? 'degraded' : 'ok', warnings.length ? '查询完成：存在降级阶段' : '查询完成：无相关记忆');
            showLoading(`「${q}」无相关记忆${warnings.length ? '（部分阶段降级）' : ''}`);
            setTimeout(hideLoading, 1500);
            return;
        }
    } catch(e) {
        console.error('Query failed:', e);
        setEventStatus('error', '高级检索查询失败');
        renderEventWarnings([{ stage: 'query', reason: e?.message || 'fetch failed' }]);
    }
    hideLoading();
}

function showQueryDetail(q, data) {
    const panel = document.getElementById('detail-panel');
    document.getElementById('detail-title').textContent = `「${q}」语义检索`;
    document.getElementById('detail-meta').innerHTML = `<span class="text-purple-300">${data.results.length} 条相关记忆</span> · ${data.timing?.total_ms || '?'}ms`;
    document.getElementById('detail-neighbor-list').innerHTML = '';
    const memList = document.getElementById('detail-memory-list');
    const stageDebug = data.debug || {};
    const stageCards = [
        ['epa', 'EPA'],
        ['pyramid', '残差金字塔'],
        ['spike', '脉冲传播'],
        ['geodesic', '测地线重排'],
    ].map(([key, label]) => {
        const stage = stageDebug[key] || {};
        const enabled = stage.enabled !== false;
        const available = stage.available === true;
        const badge = !enabled ? '关闭' : available ? '可用' : '降级';
        const reason = stage.reason_code || stage.reason || stage.error || '';
        return `<div class="rounded-lg border border-white/5 bg-white/[.03] p-2">
            <div class="flex items-center justify-between gap-2">
                <span class="text-[10px] font-medium text-slate-200">${label}</span>
                <span class="rounded-full border border-white/10 px-1.5 py-0.5 text-[9px] ${available ? 'text-emerald-300' : enabled ? 'text-amber-300' : 'text-slate-500'}">${badge}</span>
            </div>
            <div class="mt-1 text-[9px] text-slate-500">${escapeHtml(reason || `available=${available}`)}</div>
        </div>`;
    }).join('');
    const warnings = Array.isArray(stageDebug.warnings) ? stageDebug.warnings : [];
    renderEventWarnings(stageDebug.warnings);
    setEventStatus(warnings.length ? 'degraded' : 'ok', warnings.length ? '查询完成：存在降级阶段' : '查询完成：高级检索链路正常');
    const warningHtml = warnings.length ? `<div class="mt-2 text-[10px] text-amber-300">${warnings.map(w => escapeHtml(`${w.stage || 'stage'}: ${w.reason_code || w.reason || ''}`)).join(' · ')}</div>` : '';
    const shortJson = (value, max=180) => escapeHtml(JSON.stringify(value ?? [], null, 0).slice(0, max));
    const stageTabs = `<div class="mt-3 grid grid-cols-1 gap-2 text-[9px] text-slate-400">
        <div class="rounded-lg bg-white/[.025] border border-white/5 p-2"><span class="text-purple-200 font-medium">总览</span> · stages=${escapeHtml(Object.keys(stageDebug.query?.stages || {}).filter(k => stageDebug.query.stages[k]).join('/') || 'vector')}</div>
        <div class="rounded-lg bg-white/[.025] border border-white/5 p-2"><span class="text-purple-200 font-medium">EPA</span> · logic_depth=${escapeHtml(stageDebug.epa?.logic_depth ?? '-')} · entropy=${escapeHtml(stageDebug.epa?.entropy ?? '-')} · dominant_axis=${escapeHtml(stageDebug.epa?.dominant_axis ?? '-')}</div>
        <div class="rounded-lg bg-white/[.025] border border-white/5 p-2"><span class="text-purple-200 font-medium">残差金字塔</span> · coverage=${escapeHtml(stageDebug.pyramid?.coverage ?? '-')} · levels=${shortJson(stageDebug.pyramid?.levels, 140)}</div>
        <div class="rounded-lg bg-white/[.025] border border-white/5 p-2"><span class="text-purple-200 font-medium">脉冲传播</span> · seed=${shortJson(stageDebug.spike?.seed_tags, 120)} · activated=${shortJson(stageDebug.spike?.activated_tags, 120)} · energy_field_top=${shortJson(stageDebug.spike?.energy_field_top, 120)}</div>
        <div class="rounded-lg bg-white/[.025] border border-white/5 p-2"><span class="text-purple-200 font-medium">向量召回</span> · used_vector=${escapeHtml(stageDebug.vector_search?.used_vector || 'raw')} · top_candidates=${shortJson(stageDebug.vector_search?.top_candidates, 140)}</div>
        <div class="rounded-lg bg-white/[.025] border border-white/5 p-2"><span class="text-purple-200 font-medium">测地线</span> · mode=${escapeHtml(stageDebug.geodesic?.mode || stageDebug.geodesic?.reason || '-')} · reranked=${shortJson(stageDebug.geodesic?.reranked, 140)}</div>
        <div class="rounded-lg bg-white/[.025] border border-white/5 p-2"><span class="text-purple-200 font-medium">最终结果</span> · score_breakdown=${shortJson(stageDebug.final?.score_breakdown, 160)}</div>
        <div class="rounded-lg bg-white/[.025] border border-white/5 p-2"><span class="text-amber-200 font-medium">Warning</span> · ${shortJson(warnings, 160)}</div>
    </div>`;
    const stagePanel = `<div class="mb-3 rounded-xl border border-purple-500/20 bg-purple-500/[.04] p-3">
        <div class="mb-2 flex items-center justify-between gap-2">
            <span class="text-xs font-semibold text-purple-200">高级检索阶段</span>
            <span class="text-[10px] text-slate-500">${escapeHtml(String(data.timing?.total_ms || '?'))}ms</span>
        </div>
        <div class="grid grid-cols-2 gap-2">${stageCards}</div>
        ${stageTabs}
        ${warningHtml}
    </div>`;
    memList.innerHTML = stagePanel + data.results.map(m => {
        const time = m.timestamp ? new Date(m.timestamp * 1000).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
        const score = m.score ? `<span class="text-purple-400/70 text-[9px]">${(m.score*100).toFixed(0)}%</span>` : '';
        return `<div class="p-2.5 rounded-lg bg-white/[.03] border border-white/5">
            <div class="flex items-center gap-2 mb-1">
                <span class="text-purple-300 text-[10px] font-medium">${escapeHtml(m.sender_name || '未知')}</span>
                ${score}
                <span class="text-slate-600 text-[9px] ml-auto">${escapeHtml(time)}</span>
            </div>
            <p class="text-slate-300 text-[11px] leading-relaxed">${escapeHtml((m.content||'').slice(0,180))}${(m.content||'').length>180?'...':''}</p>
        </div>`;
    }).join('');
    document.getElementById('btn-expand').style.display = 'none';
    panel.classList.remove('hidden');
    if (typeof gsap !== 'undefined') gsap.fromTo(panel, { autoAlpha: 0, x: 30 }, { autoAlpha: 1, x: 0, duration: 0.4, ease: 'power3.out' });
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
            <div class="person-item" onclick="loadPersonGraph('${escapeJs(p.id)}', '${escapeJs(p.name || '')}')">
                <div class="text-slate-200 text-xs font-medium">${escapeHtml(p.name || p.id)}</div>
                <div class="text-slate-500 text-[10px] mt-0.5">${escapeHtml(p.count || 0)} 条记忆</div>
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
            renderGraph(data.nodes, data.edges || [], { layout: 'galaxy' });
            const selected = data.nodes.find(n => String(n.id) === `p${qqId}` || String(n.qq_id || '') === String(qqId)) || data.nodes.find(n => String(n.id || n.memId || n.tagId) === String(qqId)) || data.nodes.find(n => n.type === 'memory') || data.nodes[0];
            if (selected) {
                selectedNode = normalizeNodeId(selected, selected.id || selected.memId || selected.tagId || qqId);
                await showDetail(selectedNode);
                flyToNode(selectedNode);
            }
        } else {
            showLoading(`${name || qqId} 暂无记忆网络`);
            setTimeout(hideLoading, 1200);
            return;
        }
    } catch(e) { console.error(e); }
    hideLoading();
}

function focusNode(nodeId) {
    if (getNodeRecord(nodeId)) {
        selectedNode = String(nodeId);
        applyVisibility();
        return flyToNode(nodeId);
    }
    const byLabel = graphState.labelIndex.get(String(nodeId));
    if (byLabel) {
        selectedNode = byLabel;
        applyVisibility();
        return flyToNode(byLabel);
    }
    return null;
}

function focusPerson(qqId, name) {
    return loadPersonGraph(qqId, name);
}

function loadPerson(qqId, name) {
    return loadPersonGraph(qqId, name);
}

window.focusNode = focusNode;
window.focusPerson = focusPerson;
window.loadPerson = loadPerson;
window.loadPersonGraph = loadPersonGraph;

// ─── Path Finding ───
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
            const pathEdges = (data.edges || []).map(e => ({ ...e, isPath: true, weight: e.weight || 2 }));
            renderGraph(data.nodes, pathEdges, { layout: 'path' });
            showPathDetail(from, to, data);
        } else {
            showLoading(`${from} → ${to} 之间无连通路径`);
            setTimeout(hideLoading, 1500);
            return;
        }
    } catch(e) { console.error(e); }
    hideLoading();
}

function showPathDetail(from, to, data) {
    const panel = document.getElementById('detail-panel');
    document.getElementById('detail-title').textContent = `${from} → ${to} 路径`;
    document.getElementById('detail-meta').innerHTML = `<span class="text-purple-300">${data.path.length} 跳</span>`;
    document.getElementById('detail-neighbor-list').innerHTML = '';
    const memList = document.getElementById('detail-memory-list');
    memList.innerHTML = '<p class="text-slate-500 text-[9px] uppercase tracking-wider mb-1.5">关系链</p>' +
        (data.edges || []).map(e => `<div class="px-2 py-1.5 rounded bg-white/[.02] text-[10px] text-slate-400 mb-1"><span class="text-purple-300">${escapeHtml(e.source)}</span> <span class="text-amber-400/70">→${escapeHtml(e.label)}→</span> <span class="text-blue-300">${escapeHtml(e.target)}</span></div>`).join('');
    document.getElementById('btn-expand').style.display = 'none';
    panel.classList.remove('hidden');
    if (typeof gsap !== 'undefined') gsap.fromTo(panel, {autoAlpha:0,x:30}, {autoAlpha:1,x:0,duration:0.4,ease:'power3.out'});
}

// ─── Expand Node ───
async function expandNode() {
    if (!selectedNode) return;
    const rec = getNodeRecord(selectedNode);
    const entityName = rec?.label || selectedNode;
    if (!entityName) return;
    showLoading('展开邻居...');
    try {
        const res = await fetch(`/api/kg/entity/${encodeURIComponent(entityName)}`);
        const d = await res.json();
        const { nodes, edges } = deriveExpansionFromEntity(entityName, d);
        if (nodes.length || edges.length) {
            appendGraphData(nodes, edges);
        } else {
            showLoading('该节点无可展开关系');
            setTimeout(hideLoading, 1200);
            return;
        }
    } catch(e) { console.error(e); }
    hideLoading();
}

function deriveExpansionFromEntity(entityName, d) {
    const nodes = [];
    const edges = [];
    const seen = new Set();
    function addNeighbor(name, type, label, weight) {
        if (!name || name === entityName || seen.has(name)) return;
        seen.add(name);
        nodes.push({ id: name, name, label: name, type: type || 'entity', degree: 1 });
        edges.push({ source: entityName, target: name, label: label || 'relates', weight: weight || 0.5, layer: 'facts' });
    }
    (d.facts || []).forEach(f => {
        if (f.subject === entityName) addNeighbor(f.object, 'entity', f.predicate, f.confidence);
        else if (f.object === entityName) addNeighbor(f.subject, 'entity', f.predicate, f.confidence);
        else {
            addNeighbor(f.subject, 'entity', f.predicate, f.confidence);
            addNeighbor(f.object, 'entity', f.predicate, f.confidence);
        }
    });
    (d.relations || []).forEach(r => {
        if (r.source === entityName) addNeighbor(r.target, 'topic', r.type, r.weight);
        else if (r.target === entityName) addNeighbor(r.source, 'topic', r.type, r.weight);
        else {
            addNeighbor(r.source, 'topic', r.type, r.weight);
            addNeighbor(r.target, 'topic', r.type, r.weight);
        }
    });
    return { nodes, edges };
}

// ─── Scoped Fact / Tag relation mutations ───
function mutationCapabilityAvailable(raw, action) {
    if (!raw || !raw.object_ref || raw.revision === undefined || raw.revision === null) return false;
    if (raw.editable === true) return true;
    const capabilities = raw.capabilities;
    const names = action === 'delete' ? ['delete', 'remove'] : ['update', 'edit'];
    const available = value => value === true
        || value?.available === true
        || value?.enabled === true
        || value?.status === 'available';
    if (Array.isArray(capabilities)) {
        return capabilities.some(value => names.includes(String(value).toLowerCase()));
    }
    if (!capabilities || typeof capabilities !== 'object') return false;
    if (names.some(name => available(capabilities[name]))) return true;
    return ['actions', 'commands', 'mutations'].some(group => {
        const nested = capabilities[group];
        return nested && typeof nested === 'object' && names.some(name => available(nested[name]));
    });
}

function knowledgeMutationKind(raw) {
    if (!raw || !raw.object_ref || raw.revision === undefined || raw.revision === null) return null;
    const objectRef = raw.object_ref && typeof raw.object_ref === 'object' ? raw.object_ref : {};
    const identity = [raw.kind, raw.object_type, objectRef.kind, objectRef.type, objectRef.resource_type]
        .filter(Boolean)
        .join(':')
        .toLowerCase()
        .replace(/-/g, '_');
    if (identity.includes('tag_relation')) return 'tag_relation';
    if (identity.includes('fact')) return 'fact';
    return null;
}

function mutationItemFromEdge(record) {
    if (!record) return null;
    const raw = { ...record.raw };
    const kind = knowledgeMutationKind(raw);
    if (!kind) return null;
    const source = getNodeRecord(record.source)?.label || record.source;
    const target = getNodeRecord(record.target)?.label || record.target;
    if (kind === 'fact') {
        return {
            ...raw,
            kind: 'fact',
            subject: raw.subject ?? source,
            predicate: raw.predicate ?? record.label,
            object: raw.object ?? target,
            confidence: raw.confidence ?? record.weight,
        };
    }
    return {
        ...raw,
        kind: 'tag_relation',
        source: raw.source ?? source,
        target: raw.target ?? target,
        relation_type: raw.relation_type ?? raw.type ?? record.label,
        weight: raw.weight ?? record.weight,
        confidence: raw.confidence ?? record.weight,
    };
}

function registerEntityMutationItem(kind, item, index) {
    const key = `${kind}:${index}:${String(item?.revision ?? '')}:${String(item?.object_ref?.id ?? item?.id ?? '')}`;
    entityMutationItems.set(key, { ...item, kind });
    return key;
}

function showMutationNotice(message, type='success') {
    const notice = document.getElementById('mutation-notice');
    if (!notice) return;
    if (mutationNoticeTimer) clearTimeout(mutationNoticeTimer);
    notice.textContent = message;
    notice.className = `fixed right-4 top-4 z-[90] max-w-sm rounded-xl border px-4 py-3 text-xs shadow-2xl backdrop-blur-xl ${type === 'error' ? 'border-red-400/30 bg-red-950/90 text-red-100' : type === 'warning' ? 'border-amber-400/30 bg-amber-950/90 text-amber-100' : 'border-emerald-400/30 bg-emerald-950/90 text-emerald-100'}`;
    notice.classList.remove('hidden');
    mutationNoticeTimer = setTimeout(() => notice.classList.add('hidden'), 4500);
}

function setKnowledgeEditorStatus(message='', type='error') {
    const status = document.getElementById('knowledge-editor-status');
    if (!status) return;
    status.textContent = message;
    status.className = `rounded-lg border px-3 py-2 text-[10px] ${type === 'loading' ? 'border-blue-400/20 bg-blue-500/10 text-blue-100' : type === 'warning' ? 'border-amber-400/20 bg-amber-500/10 text-amber-100' : 'border-red-400/20 bg-red-500/10 text-red-100'}`;
    status.classList.toggle('hidden', !message);
}

function setKnowledgeMutationBusy(busy) {
    knowledgeMutationState.busy = busy;
    const canUpdate = knowledgeMutationState.canUpdate;
    const editor = document.getElementById('knowledge-editor-dialog');
    editor?.querySelectorAll('button').forEach(button => { button.disabled = busy; });
    editor?.querySelectorAll('input:not([readonly]), textarea').forEach(field => { field.disabled = busy || !canUpdate; });
    const deleteConfirm = document.getElementById('knowledge-delete-confirm');
    deleteConfirm?.querySelectorAll('button').forEach(button => { button.disabled = busy; });
    const save = document.getElementById('knowledge-editor-save');
    if (save) {
        save.classList.toggle('hidden', !canUpdate);
        save.disabled = busy || !canUpdate;
        save.textContent = busy ? '提交中…' : '保存并刷新';
    }
    const deleteButton = document.getElementById('knowledge-editor-delete');
    if (deleteButton) deleteButton.classList.toggle('hidden', !knowledgeMutationState.canDelete);
}

function openKnowledgeEditor(item, contextNodeId=null) {
    const kind = knowledgeMutationKind(item);
    const canUpdate = mutationCapabilityAvailable(item, 'update');
    const canDelete = mutationCapabilityAvailable(item, 'delete');
    if (!kind || (!canUpdate && !canDelete)) return;
    knowledgeMutationState.item = { ...item, kind };
    knowledgeMutationState.kind = kind;
    knowledgeMutationState.contextNodeId = contextNodeId;
    knowledgeMutationState.canUpdate = canUpdate;
    knowledgeMutationState.canDelete = canDelete;
    knowledgeMutationState.busy = false;
    relationState.editing = knowledgeMutationState.item;

    document.getElementById('knowledge-editor-title').textContent = kind === 'fact' ? '编辑 Fact' : '编辑 Tag relation';
    document.getElementById('knowledge-editor-description').textContent = `revision ${String(item.revision)} · 使用服务端 ObjectRef 乐观并发更新`;
    document.getElementById('fact-editor-fields').classList.toggle('hidden', kind !== 'fact');
    document.getElementById('relation-editor-fields').classList.toggle('hidden', kind !== 'tag_relation');
    if (kind === 'fact') {
        document.getElementById('fact-editor-subject').value = item.subject ?? '';
        document.getElementById('fact-editor-predicate').value = item.predicate ?? '';
        document.getElementById('fact-editor-object').value = item.object ?? '';
        document.getElementById('fact-editor-confidence').value = item.confidence ?? '';
    } else {
        document.getElementById('relation-editor-source').value = item.source ?? '';
        document.getElementById('relation-editor-target').value = item.target ?? '';
        document.getElementById('relation-editor-type').value = item.relation_type ?? item.type ?? '';
        document.getElementById('relation-editor-weight').value = item.weight ?? '';
        document.getElementById('relation-editor-confidence').value = item.confidence ?? '';
    }
    setKnowledgeEditorStatus('');
    setKnowledgeMutationBusy(false);
    const dialog = document.getElementById('knowledge-editor-dialog');
    dialog.classList.remove('hidden');
    dialog.classList.add('flex');
}

function openEntityKnowledgeEditor(key) {
    const item = entityMutationItems.get(key);
    if (item) openKnowledgeEditor(item, selectedFactEntity || selectedNode);
}

function openSelectedRelationEditor() {
    const item = mutationItemFromEdge(relationState.selected);
    if (item) openKnowledgeEditor(item, null);
}

function confirmSelectedRelationDelete() {
    const item = mutationItemFromEdge(relationState.selected);
    if (!item || !mutationCapabilityAvailable(item, 'delete')) return;
    openKnowledgeEditor(item, null);
    openKnowledgeDeleteConfirm();
}

function closeKnowledgeEditor(force=false) {
    if (knowledgeMutationState.busy && !force) return;
    closeKnowledgeDeleteConfirm(true);
    const dialog = document.getElementById('knowledge-editor-dialog');
    dialog?.classList.add('hidden');
    dialog?.classList.remove('flex');
    relationState.editing = null;
    knowledgeMutationState.item = null;
    knowledgeMutationState.kind = null;
    knowledgeMutationState.contextNodeId = null;
    knowledgeMutationState.canUpdate = false;
    knowledgeMutationState.canDelete = false;
    knowledgeMutationState.busy = false;
}

function openKnowledgeDeleteConfirm() {
    if (!knowledgeMutationState.item || !knowledgeMutationState.canDelete || knowledgeMutationState.busy) return;
    const kindLabel = knowledgeMutationState.kind === 'fact' ? 'Fact' : 'Tag relation';
    document.getElementById('knowledge-delete-description').textContent = `将删除当前 ${kindLabel}（revision ${String(knowledgeMutationState.item.revision)}）。成功后会重新加载服务端权威图谱，此操作不能在页面内撤销。`;
    const dialog = document.getElementById('knowledge-delete-confirm');
    dialog.classList.remove('hidden');
    dialog.classList.add('flex');
}

function closeKnowledgeDeleteConfirm(force=false) {
    if (knowledgeMutationState.busy && !force) return;
    const dialog = document.getElementById('knowledge-delete-confirm');
    dialog?.classList.add('hidden');
    dialog?.classList.remove('flex');
}

function numericEditorValue(id) {
    const raw = document.getElementById(id)?.value;
    if (raw === undefined || raw === null || raw === '') return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
}

function currentKnowledgePatch() {
    if (knowledgeMutationState.kind === 'fact') {
        return {
            subject: document.getElementById('fact-editor-subject').value.trim(),
            predicate: document.getElementById('fact-editor-predicate').value.trim(),
            object: document.getElementById('fact-editor-object').value.trim(),
            confidence: numericEditorValue('fact-editor-confidence'),
        };
    }
    return {
        relation_type: document.getElementById('relation-editor-type').value.trim(),
        weight: numericEditorValue('relation-editor-weight'),
        confidence: numericEditorValue('relation-editor-confidence'),
    };
}

async function reloadAuthoritativeKnowledge(contextNodeId) {
    hideRelationDetail();
    selectedEdge = null;
    relationState.selected = null;
    await loadGalaxy();
    if (contextNodeId && getNodeRecord(contextNodeId)) {
        selectedNode = contextNodeId;
        await showDetail(contextNodeId);
    }
}

function mutationErrorMessage(data, response) {
    return data?.error?.message || data?.error?.code || data?.message || `HTTP ${response.status}`;
}

async function knowledgeCommandIdempotencyKey(action, item, patch) {
    const payload = JSON.stringify({ action, object_ref: item.object_ref, revision: item.revision, patch });
    if (window.crypto?.subtle && window.TextEncoder) {
        const digest = await window.crypto.subtle.digest('SHA-256', new TextEncoder().encode(payload));
        const fingerprint = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
        return `explore:${action}:${fingerprint}`;
    }
    let hash = 2166136261;
    for (let index = 0; index < payload.length; index += 1) {
        hash = Math.imul(hash ^ payload.charCodeAt(index), 16777619);
    }
    return `explore:${action}:${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

async function runKnowledgeCommand(action, patch) {
    const item = knowledgeMutationState.item;
    const kind = knowledgeMutationState.kind;
    if (!item || !kind || knowledgeMutationState.busy) return;
    const contextNodeId = knowledgeMutationState.contextNodeId;
    const resource = kind === 'fact' ? 'facts' : 'tag-relations';
    const url = `/api/kg/commands/${resource}/${action}`;
    setKnowledgeMutationBusy(true);
    setKnowledgeEditorStatus(action === 'delete' ? '正在删除并刷新权威图谱…' : '正在保存并刷新权威图谱…', 'loading');
    try {
        const idempotencyKey = await knowledgeCommandIdempotencyKey(action, item, patch);
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                object_ref: item.object_ref,
                revision: item.revision,
                patch,
                idempotency_key: idempotencyKey,
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 409) {
            setKnowledgeEditorStatus('检测到 revision 冲突，正在刷新服务端最新内容。', 'warning');
            await reloadAuthoritativeKnowledge(contextNodeId);
            closeKnowledgeEditor(true);
            showMutationNotice('数据已被其他操作更新，已刷新最新内容；请重新打开编辑器。', 'warning');
            return;
        }
        if (!response.ok || data?.error) throw new Error(mutationErrorMessage(data, response));
        await reloadAuthoritativeKnowledge(contextNodeId);
        closeKnowledgeEditor(true);
        showMutationNotice(action === 'delete' ? '删除成功，已加载服务端权威图谱。' : '保存成功，已加载服务端权威图谱。');
    } catch (error) {
        closeKnowledgeDeleteConfirm(true);
        setKnowledgeMutationBusy(false);
        setKnowledgeEditorStatus(error?.message || '知识命令执行失败');
    }
}

async function submitKnowledgeMutation(event) {
    event.preventDefault();
    if (!knowledgeMutationState.canUpdate) return;
    await runKnowledgeCommand('update', currentKnowledgePatch());
}

async function submitKnowledgeDelete() {
    if (!knowledgeMutationState.canDelete) return;
    await runKnowledgeCommand('delete', {});
}

// ─── Relation HUD ───
function selectEdgeByKey(edgeKeyValue) {
    const record = graphState.edges.get(edgeKeyValue);
    if (!record) return;
    selectedEdge = edgeKeyValue;
    relationState.selected = record;
    selectedNode = null;
    selectedFact = null;
    hideDetail();
    createContextActionRing(null);
    showRelationDetail(record);
    createImportantEdgeLabels();
    applyVisibility();
}

function showRelationDetail(record) {
    const panel = document.getElementById('relation-panel');
    if (!panel || !record) return;
    const source = getNodeRecord(record.source)?.label || record.source;
    const target = getNodeRecord(record.target)?.label || record.target;
    const kind = record.raw.kind === 'tag_relation' ? 'Tag 关系' : record.raw.kind === 'fact' ? '事实边' : (record.layer || '关系');
    const confidence = record.raw.confidence ?? record.weight;
    const time = record.raw.ts ? new Date(record.raw.ts * 1000).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '未知时间';
    document.getElementById('relation-title').textContent = `${source} → ${target}`;
    document.getElementById('relation-meta').innerHTML = `<span class="text-purple-300">${escapeHtml(kind)}</span> · ${escapeHtml(record.label || 'relates')} · 权重 ${Number(record.weight || 0).toFixed(2)}`;
    document.getElementById('relation-body').innerHTML = `
        <div class="rounded-xl bg-white/[.03] border border-white/5 p-3 space-y-2 text-[11px]">
            <div><span class="text-slate-500">起点</span><div class="text-purple-200 mt-0.5">${escapeHtml(source)}</div></div>
            <div><span class="text-slate-500">关系</span><div class="text-amber-200 mt-0.5">${escapeHtml(record.label || 'relates')}</div></div>
            <div><span class="text-slate-500">终点</span><div class="text-blue-200 mt-0.5">${escapeHtml(target)}</div></div>
            <div class="grid grid-cols-2 gap-2 pt-1 text-slate-400">
                <div>置信度 <span class="font-mono text-slate-200">${confidence !== undefined && confidence !== null ? Number(confidence).toFixed(2) : '-'}</span></div>
                <div>时间 <span class="text-slate-300">${escapeHtml(time)}</span></div>
                <div>ID <span class="font-mono text-slate-300">${escapeHtml(record.raw.id || record.key)}</span></div>
                <div>图层 <span class="text-slate-300 text-purple-300 font-semibold uppercase">${escapeHtml(record.raw.layer || record.layer || '-')}</span></div>
            </div>
        </div>`;
    const mutationItem = mutationItemFromEdge(record);
    const canUpdate = mutationItem ? mutationCapabilityAvailable(mutationItem, 'update') : false;
    const canDelete = mutationItem ? mutationCapabilityAvailable(mutationItem, 'delete') : false;
    const editBtn = document.getElementById('btn-edit-relation');
    const deleteBtn = document.getElementById('btn-delete-relation');
    const readonlyBadge = document.getElementById('relation-readonly-badge');
    if (editBtn) editBtn.classList.toggle('hidden', !canUpdate);
    if (deleteBtn) deleteBtn.classList.toggle('hidden', !canDelete);
    if (readonlyBadge) readonlyBadge.classList.toggle('hidden', canUpdate || canDelete);
    panel.classList.remove('hidden');
    if (typeof gsap !== 'undefined') gsap.fromTo(panel, { autoAlpha: 0, x: 30 }, { autoAlpha: 1, x: 0, duration: 0.35, ease: 'power3.out' });
}

function hideRelationDetail() {
    const panel = document.getElementById('relation-panel');
    if (!panel || panel.classList.contains('hidden')) return;
    if (typeof gsap !== 'undefined') gsap.to(panel, { autoAlpha: 0, x: 30, duration: 0.2, onComplete: () => panel.classList.add('hidden') });
    else panel.classList.add('hidden');
}


function createContextActionRing(nodeId) {
    const ring = document.getElementById('node-action-ring');
    actionRingNode = nodeId;
    lastActionRingPoint = { x: null, y: null };
    if (!ring) return;
    if (!nodeId) { ring.classList.add('hidden'); return; }
    updateActionRingPosition();
}

function updateActionRingPosition() {
    const ring = document.getElementById('node-action-ring');
    if (!ring || !actionRingNode) return;
    const record = getNodeRecord(actionRingNode);
    if (!record || !record.object || !camera || !galaxyContainer) { ring.classList.add('hidden'); return; }
    const p = record.object.position.clone();
    graphGroup.localToWorld(p);
    p.project(camera);
    const rect = galaxyContainer.getBoundingClientRect();
    const x = Math.round((p.x * 0.5 + 0.5) * rect.width + rect.left);
    const y = Math.round((-p.y * 0.5 + 0.5) * rect.height + rect.top);
    if (x !== lastActionRingPoint.x) ring.style.left = x + 'px';
    if (y !== lastActionRingPoint.y) ring.style.top = y + 'px';
    lastActionRingPoint = { x, y };
    ring.classList.remove('hidden');
}

function applyCameraPreset(preset) {
    cameraPreset = preset || 'overview';
    if (cameraPreset === 'selected' && selectedNode) return flyToNode(selectedNode);
    if (cameraPreset === 'path') {
        if (!camera || !controls) return;
        const target = new THREE.Vector3(0, 0, 0);
        if (typeof gsap !== 'undefined') {
            gsap.to(camera.position, { x: 0, y: 22, z: 88, duration: 0.7, ease: 'power2.out' });
            gsap.to(controls.target, { x: target.x, y: target.y, z: target.z, duration: 0.7, ease: 'power2.out' });
        } else {
            camera.position.set(0, 22, 88); controls.target.copy(target);
        }
        return;
    }
    flyToGraph();
}

// ─── Timeline View ───
async function loadTimeline() {
    if (!selectedNode) return;
    const rec = getNodeRecord(selectedNode);
    const entityName = rec?.label || '';
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
        let html = '<div class="relative pl-4 border-l-2 border-purple-500/20 space-y-3">';
        for (const ev of d.events) {
            const time = ev.ts ? new Date(ev.ts * 1000).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '?';
            const dotColor = ev.type === 'fact' ? '#a78bfa' : '#60a5fa';
            if (ev.type === 'fact') {
                html += `<div class="relative"><div class="absolute -left-[21px] top-1.5 w-2.5 h-2.5 rounded-full" style="background:${dotColor}"></div><div class="text-[9px] text-slate-600 mb-0.5">${escapeHtml(time)}</div><div class="px-2 py-1.5 rounded bg-purple-500/[.06] text-[10px] text-slate-300"><span class="text-purple-300">${escapeHtml(ev.subject||'')}</span> <span class="text-slate-600">→${escapeHtml(ev.predicate||'')}→</span> <span class="text-blue-300">${escapeHtml((ev.object||'').slice(0,50))}</span></div></div>`;
            } else {
                html += `<div class="relative"><div class="absolute -left-[21px] top-1.5 w-2.5 h-2.5 rounded-full" style="background:${dotColor}"></div><div class="text-[9px] text-slate-600 mb-0.5">${escapeHtml(time)}</div><div class="px-2 py-1.5 rounded bg-blue-500/[.06] text-[10px]"><span class="text-blue-300 font-medium">${escapeHtml(ev.sender||'')}</span><span class="text-slate-400 ml-1">${escapeHtml((ev.content||'').slice(0,80))}</span></div></div>`;
            }
        }
        html += '</div>';
        memList.innerHTML = html;
    } catch(e) {
        memList.innerHTML = '<p class="text-red-400/60 text-[10px]">时间线加载失败</p>';
    }
}

// ─── Detail Panel ───
async function selectNodeById(nodeId) {
    selectedNode = nodeId;
    selectedEdge = null;
    relationState.selected = null;
    selectedFact = null;
    selectedFactEntity = null;
    hideRelationDetail();
    createContextActionRing(nodeId);
    await showDetail(nodeId);
    flyToNode(nodeId);
    createAllReadableLabels();
    createImportantEdgeLabels();
    applyVisibility();
}

function buildProjectionMetadataHtml(rec) {
    const hidden = new Set(['id','name','label','type','degree','layer','read_only','x','y','z','vector']);
    const entries = Object.entries(rec.raw || {}).filter(([key, value]) => !hidden.has(key) && value !== null && value !== undefined && value !== '' && !(Array.isArray(value) && !value.length));
    if (!entries.length) return '<p class="text-slate-600 text-[10px]">该节点没有额外投影元数据。</p>';
    return `<div class="space-y-1.5">${entries.slice(0, 18).map(([key, value]) => {
        const rendered = typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
        return `<div class="rounded-lg border border-white/5 bg-white/[.025] px-2.5 py-2"><div class="text-[9px] uppercase tracking-wide text-slate-600">${escapeHtml(key)}</div><div class="mt-0.5 whitespace-pre-wrap break-words text-[10px] leading-relaxed text-slate-300">${escapeHtml(rendered.slice(0, 1200))}</div></div>`;
    }).join('')}</div>`;
}

async function showDetail(nodeId) {
    const rec = getNodeRecord(nodeId);
    if (!rec) return;
    const panel = document.getElementById('detail-panel');
    document.getElementById('detail-title').textContent = rec.label || nodeId;
    let meta = `<span style="color:${rec.color}">${TYPE_LABELS[rec.type] || rec.type}</span>`;
    if (rec.degree) meta += ` · 度数 ${rec.degree}`;
    if (rec.raw.community !== undefined) meta += ` · 社区 #${rec.raw.community}`;
    document.getElementById('detail-meta').innerHTML = meta;
    document.getElementById('btn-expand').style.display = rec.raw.isSource ? 'none' : '';

    const neighbors = getNeighbors(nodeId);
    const neighborList = document.getElementById('detail-neighbor-list');
    neighborList.innerHTML = neighbors.slice(0, 12).map(n => {
        const nr = getNodeRecord(n);
        const c = nr?.color || '#94a3b8';
        return `<span class="inline-block px-2 py-0.5 rounded text-[10px] cursor-pointer hover:opacity-80 transition" style="background:${c}15; color:${c}; border: 1px solid ${c}25" onclick="selectNodeById('${escapeJs(n)}')">${escapeHtml(nr?.label || n)}</span>`;
    }).join('') + (neighbors.length > 12 ? `<span class="text-slate-600 text-[10px] ml-1">+${neighbors.length - 12}</span>` : '');

    const memList = document.getElementById('detail-memory-list');
    const entityName = rec.label || '';
    const entityTypes = new Set(['entity','person','topic','keyword','event','location']);
    if (entityName && entityTypes.has(rec.type)) {
        selectedFactEntity = nodeId;
        memList.innerHTML = '<p class="text-slate-600 text-[10px]">加载同域关联知识...</p>';
        try {
            const r = await fetch(`/api/kg/entity/${encodeURIComponent(entityName)}?limit=12`);
            const d = await r.json();
            memList.innerHTML = r.ok ? buildEntityKnowledgeHtml(d) : buildProjectionMetadataHtml(rec);
        } catch(e) {
            memList.innerHTML = buildProjectionMetadataHtml(rec);
        }
    } else {
        memList.innerHTML = buildProjectionMetadataHtml(rec);
    }

    panel.classList.remove('hidden');
    if (typeof gsap !== 'undefined') gsap.fromTo(panel, { autoAlpha: 0, x: 30 }, { autoAlpha: 1, x: 0, duration: 0.4, ease: 'power3.out' });
}

function buildEntityKnowledgeHtml(d) {
    entityMutationItems.clear();
    let html = '';
    if (d.person) {
        const p = d.person;
        const affColor = p.affection > 50 ? '#34d399' : p.affection > 0 ? '#fbbf24' : '#f87171';
        html += `<div class="mb-3 p-3 rounded-xl border border-purple-500/20 bg-purple-500/[.04]"><div class="flex items-center gap-2 mb-2"><div class="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold" style="background:${p.affection_color || affColor}20; color:${p.affection_color || affColor}; border:2px solid ${p.affection_color || affColor}">${escapeHtml((p.name||'?')[0])}</div><div><div class="text-white text-xs font-semibold">${escapeHtml(p.name)}</div><div class="text-slate-500 text-[9px]">QQ ${escapeHtml(p.qq_id)} · ${escapeHtml(p.msg_count)} 条消息</div></div><div class="ml-auto text-right"><div class="text-[10px] font-mono" style="color:${p.affection_color || affColor}">好感 ${escapeHtml(p.affection)}</div></div></div>${p.aliases?.length ? `<div class="text-[9px] text-slate-500 mb-1.5">别名: ${p.aliases.map(escapeHtml).join(' / ')}</div>` : ''}${p.personality_tags?.length ? `<div class="flex flex-wrap gap-1">${p.personality_tags.slice(0,8).map(t => `<span class="px-1.5 py-0.5 rounded text-[9px] bg-purple-500/10 text-purple-300 border border-purple-500/20">${escapeHtml(t)}</span>`).join('')}</div>` : ''}</div>`;
    }
    if (d.facts && d.facts.length) {
        html += '<p class="text-slate-500 text-[9px] uppercase tracking-wider mb-1.5">正式 Scoped Facts</p>';
        html += d.facts.slice(0, 6).map((fact, index) => {
            const item = { ...fact, kind: 'fact' };
            const canUpdate = mutationCapabilityAvailable(item, 'update');
            const canDelete = mutationCapabilityAvailable(item, 'delete');
            const key = canUpdate || canDelete ? registerEntityMutationItem('fact', item, index) : '';
            const action = key ? `<button type="button" onclick="openEntityKnowledgeEditor('${escapeJs(key)}')" class="ml-auto shrink-0 rounded border border-purple-400/20 bg-purple-500/10 px-2 py-1 text-[9px] text-purple-200 hover:bg-purple-500/20">${canUpdate ? '编辑' : '管理'}</button>` : '<span class="ml-auto text-[9px] text-slate-600">只读</span>';
            return `<div class="flex items-start gap-2 px-2 py-1.5 rounded bg-white/[.02] text-[10px] text-slate-400 mb-1 border border-white/5"><div class="min-w-0 flex-1"><span class="text-purple-300">${escapeHtml(fact.subject)}</span> <span class="text-slate-600">→${escapeHtml(fact.predicate)}→</span> <span class="text-blue-300">${escapeHtml(fact.object)}</span>${fact.confidence !== undefined && fact.confidence !== null ? `<span class="text-[9px] text-slate-600 ml-1 font-mono">(${Math.round(Number(fact.confidence)*100)}%)</span>` : ''}</div>${action}</div>`;
        }).join('');
    }
    if (d.relations && d.relations.length) {
        html += '<p class="text-slate-500 text-[9px] uppercase tracking-wider mb-1.5 mt-2">Tag relations</p>';
        html += d.relations.slice(0, 6).map((relation, index) => {
            const item = { ...relation, relation_type: relation.relation_type ?? relation.type };
            const canUpdate = mutationCapabilityAvailable(item, 'update');
            const canDelete = mutationCapabilityAvailable(item, 'delete');
            const key = canUpdate || canDelete ? registerEntityMutationItem('tag_relation', item, index) : '';
            const action = key ? `<button type="button" onclick="openEntityKnowledgeEditor('${escapeJs(key)}')" class="ml-auto shrink-0 rounded border border-purple-400/20 bg-purple-500/10 px-2 py-1 text-[9px] text-purple-200 hover:bg-purple-500/20">${canUpdate ? '编辑' : '管理'}</button>` : '<span class="ml-auto text-[9px] text-slate-600">只读</span>';
            return `<div class="flex items-start gap-2 px-2 py-1.5 rounded bg-white/[.02] text-[10px] text-slate-400 mb-1 border border-white/5"><div class="min-w-0 flex-1"><span class="text-purple-300">${escapeHtml(relation.source)}</span> <span class="text-amber-400/70">${escapeHtml(relation.relation_type ?? relation.type)}</span> <span class="text-blue-300">${escapeHtml(relation.target)}</span></div>${action}</div>`;
        }).join('');
    }
    if (d.memories && d.memories.length) {
        html += '<p class="text-slate-500 text-[9px] uppercase tracking-wider mb-1.5 mt-2">记忆</p>';
        html += d.memories.slice(0, 8).map(m => {
            const time = m.ts ? new Date(m.ts * 1000).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
            return `<div class="p-2.5 rounded-lg bg-white/[.03] border border-white/5 mb-1.5"><div class="flex items-center gap-2 mb-1"><span class="text-purple-300 text-[10px] font-medium">${escapeHtml(m.sender||'未知')}</span><span class="text-slate-600 text-[9px] ml-auto">${escapeHtml(time)}</span></div><p class="text-slate-300 text-[11px] leading-relaxed">${escapeHtml((m.content||'').slice(0,120))}${(m.content||'').length>120?'...':''}</p></div>`;
        }).join('');
    }
    return html || '<p class="text-slate-600 text-[10px]">暂无关联知识</p>';
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
    if (!badge) return;
    badge.innerHTML = `<span class="text-purple-300 font-semibold">${graphState.nodes.size}</span> 节点 · <span class="text-blue-300 font-semibold">${graphState.edges.size}</span> 连线`;
}

function updateRuntimeConfigStatus() {
    const viewEl = document.getElementById('runtime-status-view');
    const queryEl = document.getElementById('runtime-status-query');
    if (viewEl) viewEl.textContent = currentView || 'galaxy';
    if (queryEl) {
        const queryConfig = typeof readQueryConfig === 'function' ? readQueryConfig() : { stages: {} };
        const labels = [];
        if (queryConfig.stages?.epa) labels.push('EPA');
        if (queryConfig.stages?.pyramid) labels.push('Pyramid');
        if (queryConfig.stages?.spike) labels.push('Spike');
        if (queryConfig.stages?.geodesic) labels.push('Geo');
        queryEl.textContent = labels.length ? labels.join('/') : 'Vector Only';
    }
}

function setEventStatus(status, action='') {
    const statusEl = document.getElementById('event-status-current');
    const actionEl = document.getElementById('event-status-last-action');
    if (statusEl) {
        statusEl.textContent = status || 'idle';
        statusEl.className = `rounded-full border px-2 py-0.5 text-[9px] ${status === 'error' ? 'border-red-400/30 text-red-300' : status === 'degraded' ? 'border-amber-400/30 text-amber-300' : 'border-blue-400/20 text-blue-200/70'}`;
    }
    if (actionEl && action) actionEl.textContent = action;
}

function renderEventWarnings(warnings=[]) {
    const list = document.getElementById('event-status-warning-list');
    if (!list) return;
    if (!Array.isArray(warnings) || !warnings.length) {
        list.textContent = '';
        return;
    }
    list.innerHTML = warnings.slice(0, 4).map(w => `<div>⚠ ${escapeHtml(`${w.stage || 'stage'}: ${w.reason || ''}`)}</div>`).join('');
}

function showLoading(text) {
    const el = document.getElementById('loading');
    const textEl = document.getElementById('loading-text');
    if (textEl) textEl.textContent = text;
    if (!el) return;
    el.classList.remove('hidden'); el.classList.add('flex');
    if (typeof gsap !== 'undefined') gsap.fromTo(el, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.25 });
}

function hideLoading() {
    const el = document.getElementById('loading');
    if (!el) return;
    if (typeof gsap !== 'undefined') gsap.to(el, { autoAlpha: 0, duration: 0.3, onComplete: () => { el.classList.add('hidden'); el.classList.remove('flex'); } });
    else { el.classList.add('hidden'); el.classList.remove('flex'); }
}

// ─── View Switching ───
function switchView(view) {
    currentView = view;
    updateRuntimeConfigStatus();
    setEventStatus('idle', `切换视图：${view || 'galaxy'}`);
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
    document.getElementById('search-box').style.display = view === 'query' ? 'flex' : 'none';
    document.getElementById('path-input').style.display = view === 'path' ? 'flex' : 'none';
    document.getElementById('person-panel').classList.toggle('hidden', view !== 'person');
    if (typeof gsap !== 'undefined') {
        if (view === 'query') gsap.fromTo('#search-box', { y: -16, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.4, ease: 'power3.out' });
        if (view === 'path') gsap.fromTo('#path-input', { y: -16, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.4, ease: 'power3.out' });
        if (view === 'person') gsap.fromTo('#person-panel', { x: -40, autoAlpha: 0 }, { x: 0, autoAlpha: 1, duration: 0.45, ease: 'power3.out' });
    }
    hideDetail();
    hideRelationDetail();
    createContextActionRing(null);
    selectedNode = null;
    selectedEdge = null;
    relationState.selected = null;
    selectedFact = null;
    selectedFactEntity = null;
    activeFilter = null;
    document.querySelectorAll('.legend-pill').forEach(b => { b.classList.remove('active'); b.style.borderColor = 'transparent'; });
    if (view === 'galaxy') loadGalaxy();
    else if (view === 'person') { loadPersonList(); renderGraph([], [], { layout: 'galaxy' }); }
    else if (view === 'query') { renderGraph([], [], { layout: 'query' }); }
    else if (view === 'path') { renderGraph([], [], { layout: 'path' }); }
}

// 缺陷自愈 3：严苛销毁 Three.js / WebGL 显存及上下文 (杜绝 Context Lost 闪退崩溃)
function disposeGraph() {
    if (animationId) {
        cancelAnimationFrame(animationId);
        animationId = null;
    }
    window.removeEventListener('resize', onWindowResize);
    if (galaxyContainer) {
        if (pointerHandlers) {
            Object.entries(pointerHandlers).forEach(([event, handler]) => galaxyContainer.removeEventListener(event, handler));
        }
        pointerHandlers = null;
        galaxyContainer.replaceChildren();
        galaxyContainer.style.cursor = 'grab';
    }
    
    // 销毁多层星野背景
    if (starField) {
        disposeSceneObject(starField);
        starField = null;
    }
    if (starFieldOuter) {
        disposeSceneObject(starFieldOuter);
        starFieldOuter = null;
    }
    
    // 清理全量连线、文字、模型和粒子
    if (graphGroup || edgeGroup || labelGroup) clearGraph3D();
    
    if (controls) {
        controls.dispose?.();
        controls = null;
    }
    if (composer) {
        composer.passes?.forEach?.(pass => {
            if (pass.dispose) pass.dispose();
        });
        composer = null;
    }
    if (webglRenderer) {
        webglRenderer.dispose?.();
        webglRenderer.forceContextLoss?.();
        webglRenderer = null;
    }
    
    scene = null;
    camera = null;
    raycaster = null;
    mouse = null;
    galaxyContainer = null;
    graphGroup = null;
    edgeGroup = null;
    edgeLabelGroup = null;
    labelGroup = null;
    selectedEdge = null;
    hoveredNode = null;
    hoveredEdge = null;
    relationState.selected = null;
    relationState.hovered = null;
    actionRingNode = null;
    transientHoverLabelNode = null;
    hoveredNeighbors.clear();
    clearGraphState();
}

// ─── Init ───
document.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => switchView(btn.dataset.view)));
document.getElementById('search-input')?.addEventListener('keydown', e => { if (e.key === 'Enter') doQuery(); });
document.getElementById('path-from')?.addEventListener('keydown', e => { if (e.key === 'Enter') doPathFind(); });
document.getElementById('path-to')?.addEventListener('keydown', e => { if (e.key === 'Enter') doPathFind(); });
