// =============================================================================
// panel.js — 并行泳道式侧边栏
// =============================================================================

const swimlanesWrap = document.getElementById('swimlanes-wrap');
const globalLog = document.getElementById('global-log');
const globalFooter = document.getElementById('global-footer');
const mainDot = document.getElementById('main-dot');
const mainTitle = document.getElementById('main-title');
const statusText = document.getElementById('status-text');
const chatInput = document.getElementById('chat-input');
const chatSendBtn = document.getElementById('chat-send-btn');
const sessionIdDisplay = document.getElementById('session-id-display');

// ---- 全局状态 ----
let isFirstLog = true;
let currentSessionId = null;
let articleTitle = '';
const renderedEventIds = new Set();

// 泳道状态：claimId -> { el, headEl, timelineEl, actionsEl, status, claimData }
const swimlanes = new Map();

// 流式聊天状态
let streamingMessageEl = null;
let streamingContent = '';
let streamingMessageId = null;

// =============================================================================
// 工具函数
// =============================================================================

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

function renderMarkdown(text) {
    if (!text) return '';
    let html = escapeHtml(text);
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/`(.+?)`/g, '<code style="background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:11px;">$1</code>');
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" style="color:var(--accent-indigo);">$1</a>');
    html = html.replace(/\n/g, '<br>');
    return html;
}

function el(tag, cls, html, attrs) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    if (attrs) Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
    return e;
}

// =============================================================================
// 全局日志（扫描 / 路由 / heartbeat 等）
// =============================================================================

function addGlobalLog(stage, message, extraHtml) {
    const dotCls = ['scan', 'route', 'context', 'heartbeat', 'error', 'plan', 'complete'].includes(stage) ? stage : 'heartbeat';
    const div = el('div', 'glog-item');
    div.innerHTML = '<span class="glog-dot ' + dotCls + '"></span><span>' + escapeHtml(message) + '</span>' + (extraHtml || '');
    globalLog.appendChild(div);
}

// =============================================================================
// 全局底部区（summary / done / chat）
// =============================================================================

function addGlobalFooter(html) {
    const div = el('div');
    div.innerHTML = html;
    globalFooter.appendChild(div);
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}

// =============================================================================
// 泳道管理
// =============================================================================

function createSwimlanes(claims) {
    swimlanesWrap.innerHTML = '';
    swimlanes.clear();

    claims.forEach(function(claim, idx) {
        var lane = el('div', 'swimlane pending');
        lane.setAttribute('data-claim-id', claim.id);

        var head = el('div', 'sw-head');
        var scorePct = ((claim.suspicion_score || 0) * 100).toFixed(0);
        var shortText = claim.text && claim.text.length > 60 ? claim.text.substring(0, 60) + '...' : (claim.text || '');
        head.innerHTML =
            '<div class="sw-head-top">' +
                '<span class="sw-claim-id">' + escapeHtml(claim.id) + '</span>' +
                '<span class="sw-score">可疑 ' + scorePct + '%</span>' +
            '</div>' +
            '<div class="sw-text" title="' + escapeHtml(claim.text || '') + '">' + escapeHtml(shortText) + '</div>' +
            '<div class="sw-status-badge pending">⏳ 等待中</div>';

        var timeline = el('div', 'sw-timeline');
        var actions = el('div', 'sw-actions');
        actions.style.display = 'none';

        lane.appendChild(head);
        lane.appendChild(timeline);
        lane.appendChild(actions);
        swimlanesWrap.appendChild(lane);

        swimlanes.set(claim.id, {
            el: lane,
            headEl: head,
            timelineEl: timeline,
            actionsEl: actions,
            status: 'pending',
            claimData: claim,
        });
    });

    swimlanesWrap.scrollLeft = 0;
}

function setSwimlaneStatus(claimId, status) {
    var sw = swimlanes.get(claimId);
    if (!sw) return;
    sw.el.classList.remove('pending', 'running', 'pass', 'fail', 'error');
    sw.el.classList.add(status);
    sw.status = status;
    var badge = sw.headEl.querySelector('.sw-status-badge');
    if (!badge) return;
    badge.className = 'sw-status-badge ' + status;
    switch (status) {
        case 'pending':  badge.innerHTML = '⏳ 等待中'; break;
        case 'running':  badge.innerHTML = '🔄 验证中'; break;
        case 'pass':     badge.innerHTML = '✅ 通过'; break;
        case 'fail':     badge.innerHTML = '❌ 发现异常'; break;
        case 'error':    badge.innerHTML = '⚠️ 出错'; break;
    }
}

function addToSwimlane(claimId, dotCls, bodyCls, html) {
    var sw = swimlanes.get(claimId);
    if (!sw) return;
    var node = el('div', 'sw-node');
    node.innerHTML =
        '<span class="sw-node-dot ' + dotCls + '">●</span>' +
        '<div class="sw-node-body ' + bodyCls + '">' + html + '</div>';
    sw.timelineEl.appendChild(node);
    sw.timelineEl.scrollTop = sw.timelineEl.scrollHeight;
}

function showSwimlaneActions(claimId) {
    var sw = swimlanes.get(claimId);
    if (!sw) return;
    sw.actionsEl.style.display = 'flex';
    sw.actionsEl.innerHTML = '';
    var btn = document.createElement('button');
    btn.className = 'sw-action-btn';
    btn.textContent = '追问';
    btn.addEventListener('click', function() {
        if (chatInput) {
            chatInput.value = '请详细解释一下这条声明 (ID: ' + claimId + ') 为什么被判错？';
        }
        sendChat();
    });
    sw.actionsEl.appendChild(btn);
}

// =============================================================================
// 核心：事件路由
// =============================================================================

function renderEvent(event, isRestore) {
    if (event._eventId && renderedEventIds.has(event._eventId)) return;
    if (event._eventId) renderedEventIds.add(event._eventId);

    if (isFirstLog) {
        globalLog.innerHTML = '';
        globalFooter.innerHTML = '';
        isFirstLog = false;
        // 从 storage 读取文章标题，设置后不再改动
        chrome.storage.local.get('activeArticleTitle', function(data) {
            if (data.activeArticleTitle && mainTitle && mainTitle.childNodes[0]) {
                articleTitle = data.activeArticleTitle;
                mainTitle.childNodes[0].nodeValue = articleTitle + ' ';
            }
        });
        if (statusText) statusText.innerText = 'Processing';
        if (mainDot) {
            mainDot.style.backgroundColor = 'var(--accent-indigo)';
            mainDot.style.boxShadow = '0 0 10px rgba(79, 70, 229, 0.4)';
            mainDot.classList.add('pulsing');
        }
        if (chatInput) chatInput.disabled = false;
        if (chatSendBtn) chatSendBtn.disabled = false;
    }

    if (event.session_id && event.session_id !== currentSessionId) {
        currentSessionId = event.session_id;
        if (sessionIdDisplay) sessionIdDisplay.innerText = 'ID: ' + currentSessionId.substring(0, 8);
        // 新会话 → 刷新 RAG 文档列表
        setTimeout(refreshRagDocList, 800);
    }

    var type = event.type;
    var stage = (type === 'status') ? event.stage : null;
    var claimId = event.claim_id || null;

    // =====================================================================
    // 1. plan 事件 → 创建泳道
    // =====================================================================
    if (type === 'plan') {
        var claims = event.claims || [];
        if (claims.length > 0) {
            createSwimlanes(claims);
            addGlobalLog('plan', '扫描完成，共 ' + claims.length + ' 条声明待验证');
        } else {
            swimlanesWrap.innerHTML = '<div style="color:var(--text-muted); font-size:12px; padding:20px; white-space:nowrap;">未检测到待验证声明</div>';
        }
        return;
    }

    // =====================================================================
    // 2. claim 级事件 → 路由到对应泳道
    // =====================================================================
    if (claimId && swimlanes.has(claimId)) {
        if (type === 'status' && stage === 'verify') {
            setSwimlaneStatus(claimId, 'running');
            return;
        }

        if (type === 'thinking') {
            var thought = event.thought || event.message || '';
            addToSwimlane(claimId, 'think', 'think', escapeHtml(thought));
            return;
        }

        if (type === 'tool_call') {
            var toolHtml =
                '<span class="sw-tool-name">' + escapeHtml(event.tool_name) + '</span>' +
                '<span style="font-size:9px; color:var(--text-muted);">' + escapeHtml((event.tool_output || '').substring(0, 80)) + '</span>';
            addToSwimlane(claimId, 'tool', 'tool', toolHtml);
            return;
        }

        if (type === 'debate') {
            var content = event.message || (event.payload && event.payload.content) || event.thought || '辩论推演中...';
            addToSwimlane(claimId, 'debate', 'debate', escapeHtml(content));
            return;
        }

        if (type === 'annotation') {
            var isError = event.error_type && event.error_type !== 'null';
            if (isError) {
                setSwimlaneStatus(claimId, 'fail');
                var confPct = ((event.confidence || 0) * 100).toFixed(0);
                addToSwimlane(claimId, 'result-err', 'result-err',
                    escapeHtml(event.error_type) + ' · ' + confPct + '%' +
                    '<div class="sw-result-detail">' + escapeHtml((event.reasoning || '').substring(0, 100)) + '</div>'
                );
            } else {
                setSwimlaneStatus(claimId, 'pass');
                addToSwimlane(claimId, 'result-ok', 'result-ok', '✅ 验证通过');
            }
            showSwimlaneActions(claimId);
            return;
        }

        if (type === 'error') {
            setSwimlaneStatus(claimId, 'error');
            addToSwimlane(claimId, 'result-err', 'result-err', '⚠️ ' + escapeHtml(event.message || '未知错误'));
            showSwimlaneActions(claimId);
            return;
        }

        if (type === 'status') {
            addToSwimlane(claimId, 'think', 'think', escapeHtml(event.message || ''));
            return;
        }
        return;
    }

    // =====================================================================
    // 3. 全局事件 → global-log 或 global-footer
    // =====================================================================

    if (type === 'status') {
        if (stage === 'route') {
            var d = event.details || {};
            var extra = '';
            if (d.disabled_tools) {
                extra += '<div class="route-detail"><span class="rd-disabled">已禁用: ' + escapeHtml(d.disabled_tools) + '</span></div>';
            }
            if (d.overlays) {
                extra += '<div class="route-detail">Skill: ' + escapeHtml(d.overlays) + '</div>';
            }
            addGlobalLog('route', event.message || ('匹配领域: ' + (d.skill || 'unknown')), extra);
            return;
        }
        if (stage === 'heartbeat') return;
        if (stage === 'complete') {
            addGlobalLog('complete', event.message || '分析完成');
            return;
        }
        addGlobalLog(stage || 'heartbeat', event.message || '');
        return;
    }

    if (type === 'summary') {
        var conclusion = event.overall_conclusion || (event.payload && event.payload.overall_conclusion) || '总结已生成';
        var totalClaims = event.total_claims || (event.payload && event.payload.total_claims) || 0;
        var totalErrors = event.total_annotations || (event.payload && event.payload.total_errors) || (event.payload && event.payload.total_annotations) || 0;
        addGlobalFooter(
            '<div class="summary-card">' +
                '<div class="s-title">全局分析总结</div>' +
                '<div class="s-conclusion">' + escapeHtml(conclusion) + '</div>' +
                '<div class="s-stats">' +
                    '<span>总声明: <b>' + totalClaims + '</b></span>' +
                    '<span>发现错误: <b style="color:var(--accent-red)">' + totalErrors + '</b></span>' +
                    '<span>通过: <b style="color:var(--accent-green)">' + (totalClaims - totalErrors) + '</b></span>' +
                '</div>' +
            '</div>'
        );
        return;
    }

    if (type === 'done') {
        addGlobalFooter('<div class="done-notice">分析任务已完成，可输入问题进行追问。</div>');
        // 如果泳道区仍为占位文字（无声明），更新提示
        if (swimlanes.size === 0) {
            swimlanesWrap.innerHTML = '<div style="color:var(--text-muted); font-size:12px; padding:20px; white-space:nowrap;">未检测到待验证声明</div>';
        }
        if (statusText) statusText.innerText = 'Done';
        if (mainDot) {
            mainDot.style.backgroundColor = 'var(--accent-green)';
            mainDot.style.boxShadow = '0 0 10px rgba(16, 185, 129, 0.4)';
            mainDot.classList.remove('pulsing');
        }
        return;
    }

    if (type === 'error' && !claimId) {
        addGlobalLog('error', '❌ ' + escapeHtml(event.message || '未知错误'));
        return;
    }

    if (type === 'chat' || type === 'chat_response') {
        var role = event.role || (event.payload && event.payload.role) || 'assistant';
        var content = event.content || event.message || (event.payload && event.payload.content) || '';
        if (role === 'user') {
            addGlobalFooter(
                '<div class="chat-bubble chat-bubble-right">' +
                    '<div class="chat-sender user">我</div>' +
                    '<div class="chat-text">' + escapeHtml(content) + '</div>' +
                '</div>'
            );
        } else {
            addGlobalFooter(
                '<div class="chat-bubble chat-bubble-left">' +
                    '<div class="chat-sender agent">Agent</div>' +
                    '<div class="chat-text">' + renderMarkdown(content) + '</div>' +
                '</div>'
            );
        }
        return;
    }

    if (type === 'chat_chunk') {
        if (!streamingMessageEl || streamingMessageId !== event.message_id) {
            streamingMessageId = event.message_id;
            streamingContent = '';
            streamingMessageEl = el('div', 'chat-bubble chat-bubble-left streaming');
            streamingMessageEl.innerHTML =
                '<div class="chat-sender agent">Agent</div>' +
                '<div class="chat-text" id="stream-text-' + streamingMessageId + '"></div>';
            globalFooter.appendChild(streamingMessageEl);
        }
        streamingContent += event.content || '';
        var stEl = document.getElementById('stream-text-' + streamingMessageId);
        if (stEl) stEl.innerHTML = renderMarkdown(streamingContent);
        window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
        if (chatInput) chatInput.disabled = true;
        if (chatSendBtn) chatSendBtn.disabled = true;
        return;
    }

    if (type === 'chat_done') {
        if (streamingMessageEl) streamingMessageEl.classList.remove('streaming');
        streamingMessageEl = null;
        streamingContent = '';
        streamingMessageId = null;
        if (chatInput) chatInput.disabled = false;
        if (chatSendBtn) chatSendBtn.disabled = false;
        chatInput && chatInput.focus();
        return;
    }

    addGlobalLog('heartbeat', type + ': ' + escapeHtml(event.message || JSON.stringify(event).substring(0, 120)));
}

// =============================================================================
// 清理
// =============================================================================

function clearTimeline() {
    globalLog.innerHTML = '';
    swimlanesWrap.innerHTML = '<div style="color:var(--text-muted); font-size:12px; padding:20px; white-space:nowrap;">等待 plan 事件以创建泳道...</div>';
    globalFooter.innerHTML = '';
    swimlanes.clear();
    isFirstLog = true;
    currentSessionId = null;
    renderedEventIds.clear();
    streamingMessageEl = null;
    streamingContent = '';
    streamingMessageId = null;
    if (mainTitle && mainTitle.childNodes[0]) mainTitle.childNodes[0].nodeValue = 'Agent 会话 ';
    articleTitle = '';
    if (statusText) statusText.innerText = 'Standby';
    if (sessionIdDisplay) sessionIdDisplay.innerText = '';
    if (mainDot) {
        mainDot.style.backgroundColor = 'var(--text-muted)';
        mainDot.style.boxShadow = 'none';
        mainDot.classList.remove('pulsing');
    }
    if (chatInput) chatInput.disabled = true;
    if (chatSendBtn) chatSendBtn.disabled = true;
    if (ragDocListEl) ragDocListEl.innerHTML = '';
    if (ragDocCountEl) ragDocCountEl.innerText = '';
    if (ragUploadStatusEl) { ragUploadStatusEl.style.display = 'none'; ragUploadStatusEl.innerHTML = ''; }
}

// =============================================================================
// 追问与交互
// =============================================================================

function sendChat() {
    var text = chatInput && chatInput.value.trim();
    if (!text || !currentSessionId) return;
    renderEvent({ type: 'chat', role: 'user', content: text, session_id: currentSessionId });
    if (chatInput) chatInput.value = '';
    chatInput.focus();
    chrome.runtime.sendMessage({
        action: 'CALL_API',
        endpoint: '/api/v1/sessions/' + currentSessionId + '/chat',
        method: 'POST',
        body: { message: text, mode: 'explain' },
        sessionId: currentSessionId,
    });
}

if (chatSendBtn) chatSendBtn.addEventListener('click', sendChat);
if (chatInput) {
    chatInput.addEventListener('keypress', function(e) { if (e.key === 'Enter') sendChat(); });
}

// =============================================================================
// RAG 参考文档管理（CSP-safe：全部使用 addEventListener，无内联 handler）
// =============================================================================

var ragSectionEl = document.getElementById('rag-section');
var ragDocListEl = document.getElementById('rag-doc-list');
var ragUploadStatusEl = document.getElementById('rag-upload-status');
var ragDocCountEl = document.getElementById('rag-doc-count');
var ragFileInput = document.getElementById('rag-file-input');
var ragUploadBtn = document.getElementById('rag-upload-btn');

function showRagSection() {
    // RAG 区域始终可见（含上传按钮），无需操作
}

function hideRagSection() {
    // RAG 区域始终可见，只清空文档列表
    if (ragDocListEl) ragDocListEl.innerHTML = '';
    if (ragDocCountEl) ragDocCountEl.innerText = '';
}

// 上传按钮 → 触发文件选择
if (ragUploadBtn && ragFileInput) {
    ragUploadBtn.addEventListener('click', function() {
        ragFileInput.click();
    });
}

// 文件选择 → 读取并上传
if (ragFileInput) {
    ragFileInput.addEventListener('change', function(event) {
        var file = event.target.files && event.target.files[0];
        if (!file) return;

        if (!file.name.endsWith('.txt')) {
            alert('目前仅支持 .txt 文本文件');
            ragFileInput.value = '';
            return;
        }

        if (ragUploadStatusEl) {
            ragUploadStatusEl.style.display = 'block';
            ragUploadStatusEl.innerHTML = '⏳ 正在上传并处理: ' + escapeHtml(file.name) + '...';
        }

        var reader = new FileReader();
        reader.onload = function(e) {
            var content = e.target.result;
            uploadRagDocument(file.name, content);
        };
        reader.onerror = function() {
            if (ragUploadStatusEl) {
                ragUploadStatusEl.innerHTML = '❌ 文件读取失败';
            }
        };
        reader.readAsText(file);

        // 重置 file input 以便重复选择同一文件
        ragFileInput.value = '';
    });
}

// 文档列表中的删除按钮（事件委托，无内联 onclick）
if (ragDocListEl) {
    ragDocListEl.addEventListener('click', function(event) {
        var btn = event.target.closest('[data-rag-delete]');
        if (!btn) return;
        var docId = btn.getAttribute('data-rag-delete');
        if (docId) deleteRagDocument(docId);
    });
}

function refreshRagDocList() {
    if (!currentSessionId) return;

    // 先清空旧会话的残留状态，防止闪烁
    if (ragDocListEl) ragDocListEl.innerHTML = '';
    if (ragDocCountEl) ragDocCountEl.innerText = '';

    fetch('http://localhost:8000/api/v1/sessions/' + currentSessionId + '/rag/documents')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var items = data.items || [];
            renderRagDocList(items);
        })
        .catch(function(e) {
            console.warn('RAG 文档列表加载失败:', e);
            if (ragDocListEl) ragDocListEl.innerHTML = '';
        });
}

function renderRagDocList(items) {
    if (!ragDocListEl || !ragDocCountEl) return;

    if (items.length === 0) {
        // 无文档：只清空列表，上传按钮始终可用
        ragDocListEl.innerHTML = '';
        ragDocCountEl.innerText = '';
        return;
    }

    ragDocCountEl.innerText = ' (' + items.length + ' 篇)';
    var html = '';
    items.forEach(function(doc) {
        html +=
            '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid rgba(0,0,0,0.05);">' +
                '<span title="' + escapeHtml(doc.filename) + '">' + escapeHtml(doc.filename) +
                    ' <span style="color:var(--text-muted);">(' + doc.chunk_count + ' 块)</span></span>' +
                '<button data-rag-delete="' + escapeHtml(doc.document_id) + '" ' +
                    'style="background:none;border:none;color:var(--accent-red);cursor:pointer;font-size:14px;padding:2px 6px;" ' +
                    'title="删除文档">✕</button>' +
            '</div>';
    });
    ragDocListEl.innerHTML = html;
    showRagSection();
}

function uploadRagDocument(filename, content) {
    if (!currentSessionId) {
        if (ragUploadStatusEl) ragUploadStatusEl.innerHTML = '❌ 无活跃会话';
        return;
    }

    fetch('http://localhost:8000/api/v1/sessions/' + currentSessionId + '/rag/documents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: filename, content: content })
    })
    .then(function(r) {
        if (!r.ok) {
            return r.json().then(function(e) { throw new Error(e.detail?.error?.message || '上传失败'); });
        }
        return r.json();
    })
    .then(function(data) {
        if (ragUploadStatusEl) {
            ragUploadStatusEl.innerHTML = '✅ 已上传: ' + escapeHtml(filename) + ' (' + data.chunk_count + ' 个片段)';
        }
        // 刷新列表
        setTimeout(refreshRagDocList, 500);
    })
    .catch(function(err) {
        if (ragUploadStatusEl) {
            ragUploadStatusEl.innerHTML = '❌ 上传失败: ' + err.message;
        }
    });
}

function deleteRagDocument(docId) {
    if (!currentSessionId) return;
    if (!confirm('确定要删除这篇参考文档吗？')) return;

    fetch('http://localhost:8000/api/v1/sessions/' + currentSessionId + '/rag/documents/' + docId, {
        method: 'DELETE'
    })
    .then(function(r) {
        if (!r.ok && r.status !== 204) {
            return r.json().then(function(e) { throw new Error(e.detail?.error?.message || '删除失败'); });
        }
        refreshRagDocList();
    })
    .catch(function(err) {
        console.warn('RAG 文档删除失败:', err);
        refreshRagDocList();
    });
}

// =============================================================================
// 消息监听 + 恢复 + 轮询
// =============================================================================

chrome.runtime.onMessage.addListener(function(message) {
    if (message.action === 'AGENT_EVENT') {
        renderEvent(message.payload, false);
    } else if (message.action === 'TIMELINE_RESET') {
        clearTimeline();
    }
});

async function restoreTimeline() {
    try {
        var data = await chrome.storage.local.get(['agentTimeline', 'activeSessionId']);
        var history = Array.isArray(data.agentTimeline) ? data.agentTimeline : [];
        var activeSessionId = data.activeSessionId;
        if (!activeSessionId) { clearTimeline(); return; }
        var filtered = history.filter(function(e) { return e.session_id === activeSessionId; });
        if (filtered.length === 0) { clearTimeline(); return; }

        clearTimeline();
        isFirstLog = false;
        currentSessionId = activeSessionId;
        if (sessionIdDisplay) sessionIdDisplay.innerText = 'ID: ' + currentSessionId.substring(0, 8);
        if (chatInput) chatInput.disabled = false;
        if (chatSendBtn) chatSendBtn.disabled = false;

        for (var i = 0; i < filtered.length; i++) renderEvent(filtered[i], true);
        window.scrollTo({ top: document.body.scrollHeight });
    } catch (error) {
        console.error('恢复时间线失败:', error);
        clearTimeline();
    }
}

restoreTimeline();

chrome.storage.onChanged.addListener(async function(changes, area) {
    if (area !== 'local') return;
    if (!changes.agentTimeline && !changes.activeSessionId) return;
    var data = await chrome.storage.local.get(['agentTimeline', 'activeSessionId']);
    var history = Array.isArray(data.agentTimeline) ? data.agentTimeline : [];
    var sid = data.activeSessionId;
    if (!sid) return;
    if (currentSessionId && currentSessionId !== sid) clearTimeline();
    var newEvents = history.filter(function(e) { return e.session_id === sid && !renderedEventIds.has(e._eventId); });
    for (var i = 0; i < newEvents.length; i++) renderEvent(newEvents[i], true);
    if (newEvents.length > 0) window.scrollTo({ top: document.body.scrollHeight });
});

async function pullMissedEvents() {
    var data = await chrome.storage.local.get(['agentTimeline', 'activeSessionId']);
    var history = Array.isArray(data.agentTimeline) ? data.agentTimeline : [];
    var sid = data.activeSessionId;
    if (!sid) return;
    var missed = history.filter(function(e) { return e.session_id === sid && !renderedEventIds.has(e._eventId); });
    for (var i = 0; i < missed.length; i++) renderEvent(missed[i], true);
    if (missed.length > 0) window.scrollTo({ top: document.body.scrollHeight });
}

chrome.runtime.onMessage.addListener(function(message) {
    if (message.action === 'AGENT_EVENT') {
        renderEvent(message.payload, false);
    } else if (message.action === 'TIMELINE_RESET') {
        clearTimeline();
        [300, 800, 2000].forEach(function(ms) { setTimeout(pullMissedEvents, ms); });
    }
});
