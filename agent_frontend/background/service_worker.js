const BACKEND_BASE_URL = 'http://localhost:8000';
let activeTabId = null;

// =========================================================================
// 设备身份识别：首次安装时生成 UUID，存 chrome.storage.local
// =========================================================================
async function getOrCreateDeviceId() {
    const data = await chrome.storage.local.get('deviceId');
    if (data.deviceId) return data.deviceId;
    const newId = 'dev-' + crypto.randomUUID();
    await chrome.storage.local.set({ deviceId: newId });
    return newId;
}

// 获取带 Device-ID 的通用 Headers
async function apiHeaders(extra = {}) {
    const deviceId = await getOrCreateDeviceId();
    return { 'Content-Type': 'application/json', 'X-Device-ID': deviceId, ...extra };
}

// =========================================================================
// 持久化时间线（存到 chrome.storage.local 防丢失）
// =========================================================================
async function persistEvent(event) {
    const data = await chrome.storage.local.get('agentTimeline');
    const timeline = Array.isArray(data.agentTimeline) ? data.agentTimeline : [];
    timeline.push(event);
    await chrome.storage.local.set({ agentTimeline: timeline });
}

async function clearTimelineStorage() {
    await chrome.storage.local.set({ agentTimeline: [] });
}

// =========================================================================
// 通用事件推送（广播到 sidepanel + content script）
// =========================================================================
async function broadcastEvent(event) {
    const entry = { ...event, _eventId: crypto.randomUUID() };
    try { await persistEvent(entry); } catch (e) { console.warn('persistEvent failed:', e); }
    try { await chrome.runtime.sendMessage({ action: 'AGENT_EVENT', payload: entry }); } catch (e) { }
    if (activeTabId) {
        try { await chrome.tabs.sendMessage(activeTabId, { action: 'AGENT_EVENT', payload: entry }); } catch (e) { }
    }
}

// 核心流程：1. 建会话 -> 2. 发起流式分析
async function startFullSessionAnalysis(articleText, articleTitle, sourceUrl, skillId, overlays, disabledTools) {
    await broadcastEvent({ type: 'status', message: '正在发起分析请求...' });

    try {
        // 直接调用 /analyze，后端自动创建 session，session_id 从首个 SSE 事件获取
        const analyzePayload = { article_text: articleText, article_title: articleTitle, source_url: sourceUrl };
        if (skillId) analyzePayload.skill_id = skillId;
        if (overlays && overlays.length > 0) analyzePayload.overlays = overlays;
        if (disabledTools && disabledTools.length > 0) analyzePayload.disabled_tools = disabledTools;

        const analyzeRes = await fetch(`${BACKEND_BASE_URL}/analyze`, {
            method: 'POST',
            headers: await apiHeaders(),
            body: JSON.stringify(analyzePayload),
        });

        if (!analyzeRes.body) throw new Error('未返回 SSE 数据流');

        const reader = analyzeRes.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const packets = buffer.split('\n\n');
            buffer = packets.pop() || '';

            for (const packet of packets) {
                if (!packet.startsWith('data: ')) continue;
                const jsonStr = packet.substring(6).trim();
                if (!jsonStr) continue;

                try {
                    const event = JSON.parse(jsonStr);
                    // 后端首个事件已携带 session_id，直接透传
                    if (event.session_id) {
                        await chrome.storage.local.set({ activeSessionId: event.session_id });
                    }
                    await broadcastEvent(event);
                } catch (e) { }
            }
        }
    } catch (error) {
        await broadcastEvent({ type: 'error', message: `连接失败: ${error.message}` });
    }
}

// 监听扩展内部消息
chrome.runtime.onMessage.addListener((message, sender) => {
    if (sender.tab) activeTabId = sender.tab.id;

    if (message.action === 'OPEN_SIDEPANEL') {
        chrome.sidePanel.open({ windowId: message.windowId });
    }
    else if (message.action === 'RESET_AGENT_TIMELINE') {
        clearTimelineStorage().catch(e => console.warn(e));
        chrome.storage.local.remove('activeSessionId').catch(e => console.warn(e));
        chrome.runtime.sendMessage({ action: 'TIMELINE_RESET' }).catch(e => e);
    }
    else if (message.action === 'START_BACKEND_ANALYSIS') {
        console.log('[BG] START_BACKEND_ANALYSIS received, textLen:', message.articleText?.length);
        // 记录目标 tab ID，后续 annotation 事件必须推送到该 tab 的 content script 才能高亮
        if (message.tabId) activeTabId = message.tabId;
        // 存储文章标题供 panel.js 读取，动态更新侧边栏头部
        if (message.articleTitle) {
            chrome.storage.local.set({ activeArticleTitle: message.articleTitle }).catch(() => { });
        }
        // 在后台打开侧边栏，避免 popup 端调用 chrome.sidePanel.open() 导致 popup 失焦关闭
        if (message.windowId) {
            chrome.sidePanel.open({ windowId: message.windowId }).catch(() => { });
        }
        startFullSessionAnalysis(
            message.articleText, message.articleTitle, message.sourceUrl,
            message.skillId, message.overlays, message.disabledTools
        );
    }
    else if (message.action === 'CALL_API') {
        handleCallApi(message);
    }
    else if (message.action === 'LOAD_PAST_SESSION') {
        chrome.sidePanel.open({ windowId: message.windowId });
        loadPastSessionFromBackend(message.sessionId, message.windowId);
    }
});

// =========================================================================
// 通用 API 调用处理（聊天、重新验证等）
// =========================================================================
async function handleCallApi(message) {
    const { endpoint, method, body, sessionId } = message;

    // 聊天请求 → 走 SSE 流式接口
    const isChatRequest = endpoint && endpoint.includes('/chat') && method === 'POST' && !endpoint.includes('/chat/stream');

    try {
        // 聊天请求使用新的 SSE streaming endpoint
        const url = isChatRequest
            ? `${BACKEND_BASE_URL}/api/v1/sessions/${sessionId}/chat/stream`
            : `${BACKEND_BASE_URL}${endpoint}`;

        if (isChatRequest) {
            // SSE 流式聊天
            const res = await fetch(url, {
                method: 'POST',
                headers: await apiHeaders(),
                body: JSON.stringify(body)
            });

            if (!res.ok || !res.body) {
                const errData = await res.json().catch(() => ({}));
                const errMsg = errData.detail?.error?.message || errData.detail || `HTTP ${res.status}`;
                await broadcastEvent({ type: 'error', message: `请求失败: ${errMsg}` });
                return;
            }

            // 读取 SSE 流
            const reader = res.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const packets = buffer.split('\n\n');
                buffer = packets.pop() || '';

                for (const packet of packets) {
                    if (!packet.startsWith('data: ')) continue;
                    const jsonStr = packet.substring(6).trim();
                    if (!jsonStr) continue;

                    try {
                        const event = JSON.parse(jsonStr);
                        if (event.session_id) event.session_id = sessionId || event.session_id;
                        await broadcastEvent(event);
                    } catch (e) {
                        console.warn('[handleCallApi] SSE parse error:', e);
                    }
                }
            }
            return;
        }

        // 非聊天请求：传统 JSON 请求/响应
        const res = await fetch(url, {
            method: method || 'POST',
            headers: await apiHeaders(),
            body: JSON.stringify(body)
        });

        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            const errMsg = errData.detail?.error?.message || errData.detail || `HTTP ${res.status}`;
            await broadcastEvent({ type: 'error', message: `请求失败: ${errMsg}` });
            return;
        }

        const data = await res.json();

        // 重新验证请求
        if (data.status === 'queued') {
            await broadcastEvent({
                type: 'status',
                message: `🔄 重新验证请求已排队 (claim: ${data.claim_id || 'all'})`,
                session_id: data.session_id || sessionId
            });
        }

    } catch (err) {
        await broadcastEvent({ type: 'error', message: `API 请求失败: ${err.message}` });
    }
}

// 👇 新增：从后端恢复某次历史会话的完整记录
async function loadPastSessionFromBackend(sessionId, windowId) {
    await broadcastEvent({ type: 'status', message: `📡 正在从云端调取历史档案: ${sessionId}...` });

    // 标记当前活跃会话
    await chrome.storage.local.set({ activeSessionId: sessionId });

    // ---- 0. 先获取会话详情，拿到 source_url，导航到原始网页 ----
    let sourceUrl = null;
    let articleTitle = null;
    try {
        const detailRes = await fetch(`${BACKEND_BASE_URL}/api/v1/sessions/${sessionId}`,
            { headers: await apiHeaders() });
        if (detailRes.ok) {
            const detail = await detailRes.json();
            sourceUrl = detail.session?.source_url || null;
            articleTitle = detail.session?.article_title || null;
            // 将文章标题写入storage，确保侧边栏恢复时显示正确的标题
            if (articleTitle) {
                await chrome.storage.local.set({ activeArticleTitle: articleTitle });
            }
        }
    } catch (e) {
        console.warn('[loadPastSession] 获取会话详情失败:', e);
    }

    // 如果有关联的原始网页，导航过去
    if (sourceUrl) {
        try {
            // 先查找是否已有该 URL 的标签页
            const existingTabs = await chrome.tabs.query({ url: sourceUrl });
            let targetTab;
            if (existingTabs && existingTabs.length > 0) {
                targetTab = existingTabs[0];
                await chrome.tabs.update(targetTab.id, { active: true });
            } else if (windowId) {
                // 在同一窗口打开新标签页
                targetTab = await chrome.tabs.create({ url: sourceUrl, windowId: windowId, active: true });
            } else {
                targetTab = await chrome.tabs.create({ url: sourceUrl, active: true });
            }

            // 等待页面加载完成
            await new Promise((resolve) => {
                const checkComplete = (tabId, changeInfo) => {
                    if (tabId === targetTab.id && changeInfo.status === 'complete') {
                        chrome.tabs.onUpdated.removeListener(checkComplete);
                        resolve();
                    }
                };
                chrome.tabs.onUpdated.addListener(checkComplete);
                // 超时兜底：15 秒后无论如何继续
                setTimeout(() => {
                    chrome.tabs.onUpdated.removeListener(checkComplete);
                    resolve();
                }, 15000);
            });

            // 注入 content script 以便后续 annotation 事件能高亮
            activeTabId = targetTab.id;
            try {
                await chrome.scripting.executeScript({
                    target: { tabId: targetTab.id },
                    files: ['lib/Readability.js', 'content/ui.js', 'content/highlighter.js', 'content/content_script.js']
                });
                console.log('[loadPastSession] content scripts injected into tab', targetTab.id);
            } catch (e) {
                console.warn('[loadPastSession] content script 注入失败（可能为非 HTTP 页面）:', e);
            }

            // 等一下确保 content script 就绪
            await new Promise(r => setTimeout(r, 500));
        } catch (e) {
            console.warn('[loadPastSession] 导航到原始网页失败:', e);
        }
    }

    // ---- 1. 获取该会话的事件流 (推理过程) ----
    try {
        const eventRes = await fetch(`${BACKEND_BASE_URL}/api/v1/sessions/${sessionId}/events`,
            { headers: await apiHeaders() });
        let events = [];
        if (eventRes.ok) {
            const evData = await eventRes.json();
            events = (evData.items || []).map(i => {
                let payload = {};
                try { payload = typeof i.payload === 'string' ? JSON.parse(i.payload) : i.payload; } catch (e) { }
                return { ...payload, session_id: sessionId, _eventId: 'hist-' + crypto.randomUUID() };
            });
        }

        // 2. 获取该会话的历史对话 (聊天记录)
        const chatRes = await fetch(`${BACKEND_BASE_URL}/api/v1/sessions/${sessionId}/chat`,
            { headers: await apiHeaders() });
        let chats = [];
        if (chatRes.ok) {
            const chatData = await chatRes.json();
            chats = (chatData.items || []).map(c => ({
                type: 'chat',
                role: c.role,
                content: c.content,
                message_type: c.message_type,
                related_claim_id: c.related_claim_id,
                session_id: sessionId,
                _eventId: 'chat-' + crypto.randomUUID()
            }));
        }

        // 3. 重置本地时间线
        await clearTimelineStorage();
        await chrome.runtime.sendMessage({ action: 'TIMELINE_RESET' }).catch(e => e);

        // 4. 合并事件和聊天，逐条渲染（用 broadcastEvent 确保也推送到 content script 做高亮）
        const allHistory = [...events, ...chats];
        for (const item of allHistory) {
            item.session_id = sessionId;
            await broadcastEvent(item);
        }

        await broadcastEvent({ type: 'status', message: `历史数据装载完毕` });

    } catch (error) {
        await broadcastEvent({ type: 'error', message: `历史恢复失败: ${error.message}` });
    }
}