const CONTENT_SCRIPT_FILES = ['lib/Readability.js', 'content/ui.js', 'content/highlighter.js', 'content/content_script.js'];
let userOverlays = []; // 存储用户的 Skill
let disabledTools = []; // 存储用户禁用的工具名
let allTools = [];      // 从后端 /skills 获取的工具目录
let disabledToolsLimit = 50;  // 上限，从 /skills 获取

// --- Skill 渲染与存储逻辑 ---
const overlayListEl = document.getElementById('overlay-list');

async function loadOverlays() {
    const data = await chrome.storage.local.get('agentOverlays');
    userOverlays = data.agentOverlays || [];
    renderOverlays();
}

async function saveOverlays() {
    await chrome.storage.local.set({ agentOverlays: userOverlays });
    renderOverlays();
}

function renderOverlays() {
    overlayListEl.innerHTML = '';
    if (userOverlays.length === 0) {
        overlayListEl.innerHTML = `<div style="text-align:center; font-size:12px; color:var(--text-muted); padding:10px;">暂无自定义 skill，点击上方新增。</div>`;
        return;
    }

    userOverlays.forEach((overlay, index) => {
        const item = document.createElement('div');
        item.className = 'overlay-item';
        item.innerHTML = `
            <div class="overlay-info">
                <div class="overlay-name">${overlay.name}</div>
                <div class="overlay-desc">${overlay.description || '无描述'}</div>
            </div>
            <div class="neu-toggle ${overlay.active ? 'active' : ''}" data-index="${index}"></div>
        `;
        
        // 绑定开关事件
        item.querySelector('.neu-toggle').addEventListener('click', (e) => {
            userOverlays[index].active = !userOverlays[index].active;
            saveOverlays();
        });
        
        overlayListEl.appendChild(item);
    });
}

// --- 3. 抽屉控制与新增逻辑 ---
const drawer = document.getElementById('add-drawer');
document.getElementById('toggle-drawer-btn').addEventListener('click', () => {
    drawer.classList.toggle('open');
});

document.getElementById('save-overlay-btn').addEventListener('click', () => {
    const name = document.getElementById('overlay-name').value.trim();
    const desc = document.getElementById('overlay-desc').value.trim();
    const prompt = document.getElementById('overlay-prompt').value.trim();

    if (!name || !prompt) return alert('标识名和提示词不能为空！');

    userOverlays.unshift({ name, description: desc, prompt, active: true });
    saveOverlays();
    
    // 清空表单并收起抽屉
    document.getElementById('overlay-name').value = '';
    document.getElementById('overlay-desc').value = '';
    document.getElementById('overlay-prompt').value = '';
    drawer.classList.remove('open');
});

// --- 工具开关 (Disabled Tools) 渲染与存储逻辑 ---
const toolsListEl = document.getElementById('tools-list');
const toolsStatusEl = document.getElementById('tools-status');

// 判定"危险"工具：通用核查类，影响面大
const DANGER_TOOLS = ['cross_reference', 'source_verifier'];

async function loadDisabledTools() {
    const data = await chrome.storage.local.get('disabledTools');
    disabledTools = data.disabledTools || [];
}

async function saveDisabledTools() {
    // 去重
    disabledTools = [...new Set(disabledTools)];
    await chrome.storage.local.set({ disabledTools: disabledTools });
    renderTools();
}

function renderTools() {
    if (!toolsListEl) return;
    toolsListEl.innerHTML = '';

    if (allTools.length === 0) {
        toolsListEl.innerHTML = `<div style="text-align:center; font-size:12px; color:var(--text-muted); padding:10px;">工具列表为空，无法连接后端。</div>`;
        return;
    }

    const disabledSet = new Set(disabledTools);
    const enabledCount = allTools.length - disabledSet.size;

    if (toolsStatusEl) {
        toolsStatusEl.innerText = `${enabledCount}/${allTools.length} 可用`;
        if (disabledSet.size > 0) {
            toolsStatusEl.style.color = 'var(--accent-red)';
        } else {
            toolsStatusEl.style.color = 'var(--text-muted)';
        }
    }

    allTools.forEach(tool => {
        const isEnabled = !disabledSet.has(tool.name);
        const isDanger = DANGER_TOOLS.includes(tool.name);

        const item = document.createElement('div');
        item.className = 'tool-item' + (isDanger && isEnabled ? ' danger' : '');
        item.innerHTML = `
            <div class="tool-info">
                <div class="tool-name">
                    ${tool.name}
                    ${isDanger && isEnabled ? '<span class="tool-danger-badge">核心</span>' : ''}
                </div>
                <div class="tool-desc">${tool.description || '无描述'}</div>
                ${tool.used_by && tool.used_by.length > 0 
                    ? `<div class="tool-usedby">影响领域: ${tool.used_by.join(', ')}</div>` 
                    : ''}
            </div>
            <div class="neu-toggle ${isEnabled ? 'active' : ''}" data-tool="${tool.name}"></div>
        `;

        // 绑定开关事件
        item.querySelector('.neu-toggle').addEventListener('click', () => {
            if (isEnabled) {
                // 禁用：加入列表
                if (!disabledTools.includes(tool.name)) {
                    disabledTools.push(tool.name);
                }
                // 危险工具二次确认
                if (isDanger) {
                    if (!confirm(`⚠️ "${tool.name}" 是核心核查工具，禁用它会影响所有领域的分析质量。确定要禁用吗？`)) {
                        disabledTools = disabledTools.filter(t => t !== tool.name);
                        return;
                    }
                }
            } else {
                // 启用：从列表移除
                disabledTools = disabledTools.filter(t => t !== tool.name);
            }
            // 数量上限警告
            if (disabledTools.length > disabledToolsLimit) {
                alert(`最多禁用 ${disabledToolsLimit} 个工具。`);
                disabledTools = disabledTools.slice(0, disabledToolsLimit);
            }
            saveDisabledTools();
        });

        toolsListEl.appendChild(item);
    });

    // 空集提醒
    if (enabledCount === 0) {
        const warn = document.createElement('div');
        warn.style.cssText = 'margin-top:8px; padding:8px 12px; border-radius:8px; font-size:11px; color:var(--accent-red); box-shadow:var(--neu-in);';
        warn.innerText = '⚠️ 已禁用全部工具，核查将退化为仅凭常识判断。';
        toolsListEl.appendChild(warn);
    }
}

async function fetchSkillsAndRender() {
    try {
        const res = await fetch('http://localhost:8000/skills');
        if (!res.ok) throw new Error('无法获取 /skills');
        const data = await res.json();
        allTools = data.tools || [];
        if (data.disabled_tools_limits) {
            disabledToolsLimit = data.disabled_tools_limits.max_disabled_tools_per_request || 50;
        }
        await loadDisabledTools();
        // 清理本地存储中已在服务端不存在的工具名
        const validNames = new Set(allTools.map(t => t.name));
        const before = disabledTools.length;
        disabledTools = disabledTools.filter(t => validNames.has(t));
        if (disabledTools.length !== before) {
            await saveDisabledTools();
        }
        renderTools();
    } catch (err) {
        console.warn('获取工具列表失败:', err);
        if (toolsStatusEl) toolsStatusEl.innerText = '获取失败';
        if (toolsListEl) {
            toolsListEl.innerHTML = `<div style="text-align:center; font-size:12px; color:var(--text-muted); padding:10px;">无法连接后端，请确认服务器已启动。</div>`;
        }
    }
}
function isInjectablePage(url) {
    if (!url) return false;
    return url.startsWith('http://') || url.startsWith('https://');
}

document.getElementById('start-btn').addEventListener('click', async () => {
    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab || !tab.id) throw new Error('未找到当前活动标签页');
        if (!isInjectablePage(tab.url)) {
            alert('系统警告: 当前页面不支持分析，协议必须为 HTTP/HTTPS。');
            window.close();
            return;
        }

        // 提取已激活的 overlay
        const activeOverlays = userOverlays
            .filter(o => o.active)
            .map(o => ({ name: o.name, prompt: o.prompt, description: o.description }));

        chrome.runtime.sendMessage({ action: 'RESET_AGENT_TIMELINE' }).catch(() => {});

        // 确保 content script 已注入（Readability 等依赖）
        try { await chrome.tabs.sendMessage(tab.id, { action: 'PING' }); } 
        catch (e) {
            await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: CONTENT_SCRIPT_FILES });
        }

        // 🚀 直接在 popup 端提取文本，避免 content-script → background 链路在 Edge 上丢消息
        const results = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: () => {
                try {
                    const documentClone = document.cloneNode(true);
                    const reader = new Readability(documentClone);
                    const article = reader.parse();
                    if (article && article.textContent) {
                        return { text: article.textContent.replace(/\n\s*\n/g, '\n').trim().substring(0, 8000), title: article.title || document.title };
                    }
                } catch (e) {}
                return { text: document.body.innerText.substring(0, 5000), title: document.title };
            }
        });

        const { text, title } = results[0].result || {};
        if (!text || text.trim() === "") {
            alert("未检测到有效文章正文！");
            window.close();
            return;
        }

        // 直接发给 background，必须 await 确保送达后再关 popup
        // windowId 传给 background 用于在后台打开侧边栏，避免 popup 因侧边栏打开而提前关闭
        // tabId 必须传递，否则 background 无法将高亮事件推送到 content script
        await chrome.runtime.sendMessage({ 
            action: 'START_BACKEND_ANALYSIS', 
            articleText: text,
            articleTitle: title || "未命名网页",
            sourceUrl: tab.url || "",
            tabId: tab.id,
            overlays: activeOverlays,
            disabledTools: disabledTools,
            windowId: tab.windowId
        });

        document.getElementById('start-btn').innerText = "已启动";
        setTimeout(() => window.close(), 300);
    } catch (error) {
        alert(`启动失败：${error.message}`);
    }
});

// 初始化加载
loadOverlays();
fetchSkillsAndRender();

const mainView = document.getElementById('main-view');
const historyView = document.getElementById('history-view');
const toggleHistoryBtn = document.getElementById('toggle-history-btn');
const historyList = document.getElementById('history-list');

let isHistoryOpen = false;

toggleHistoryBtn.addEventListener('click', async () => {
    isHistoryOpen = !isHistoryOpen;
    if (isHistoryOpen) {
        mainView.classList.add('hidden');
        historyView.classList.add('active');
        toggleHistoryBtn.innerHTML = `  返回`;
        await fetchHistoryList();
    } else {
        mainView.classList.remove('hidden');
        historyView.classList.remove('active');
        toggleHistoryBtn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg> 历史`;
    }
});

async function fetchHistoryList() {
    historyList.innerHTML = `<div style="text-align:center; padding:20px; color:var(--text-muted); font-size:12px;">正在加载服务器记录...</div>`;
    try {
        // 获取设备 ID
        const storage = await chrome.storage.local.get('deviceId');
        const deviceId = storage.deviceId || 'dev-' + crypto.randomUUID();
        if (!storage.deviceId) await chrome.storage.local.set({ deviceId });

        const res = await fetch(`http://localhost:8000/api/v1/sessions?limit=10&device_id=${encodeURIComponent(deviceId)}`, {
            headers: { 'X-Device-ID': deviceId }
        });
        if (!res.ok) throw new Error('无法连接后端');
        const data = await res.json();
        
        if (!data.items || data.items.length === 0) {
            historyList.innerHTML = `<div style="text-align:center; padding:20px; color:var(--text-muted); font-size:12px;">暂无历史记录</div>`;
            return;
        }

        historyList.innerHTML = '';
        data.items.forEach(session => {
            const card = document.createElement('div');
            card.className = 'history-item';
            
            // 格式化时间
            const dateStr = session.created_at ? new Date(session.created_at).toLocaleString() : '未知时间';
            
            card.innerHTML = `
                <div class="history-title">${session.article_title || '未命名网页分析'}</div>
                <div class="history-meta">
                    <span>领域: ${session.domain || 'general'}</span>
                    <span>错误数: <b style="color:var(--accent-red)">${session.total_annotations || 0}</b></span>
                </div>
                <div style="font-size:10px; color:var(--text-muted); margin-top:6px;">${dateStr} (ID: ${session.session_id.substring(0,6)})</div>
            `;
            
            // 点击历史卡片，触发加载详情
            card.addEventListener('click', async () => {
                const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
                // 通知 Background 恢复特定会话
                chrome.runtime.sendMessage({ 
                    action: 'LOAD_PAST_SESSION', 
                    sessionId: session.session_id,
                    windowId: tab.windowId
                });
                window.close(); // 关掉弹窗
            });
            historyList.appendChild(card);
        });

    } catch (error) {
        historyList.innerHTML = `<div style="text-align:center; padding:20px; color:var(--accent-red); font-size:12px;">❌ 无法加载历史: 后端未启动</div>`;
    }
}
