window.ANNOTATIONS = {};

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === 'PING') {
        sendResponse({ status: 'ok' });
    } else if (request.action === 'START_ANALYSIS') {
        startAnalysis(request.config).catch((error) => {
            console.error("启动分析失败:", error);
            emitError(`启动分析失败：${error.message || error}`);
            emitDone();
        });
    } else if (request.action === 'AGENT_EVENT') {
        processAgentEvent(request.payload);
    }
});

// 带重试的消息发送，解决 MV3 service worker 冷启动时消息丢失问题
async function sendMessageWithRetry(message, maxRetries = 5, delayMs = 300) {
    for (let i = 0; i < maxRetries; i++) {
        try {
            return await chrome.runtime.sendMessage(message);
        } catch (e) {
            if (i === maxRetries - 1) throw e;
            await new Promise(r => setTimeout(r, delayMs));
        }
    }
}

async function startAnalysis(config) {
    // fire-and-forget：popup 已经发过一次，不等待避免阻塞
    chrome.runtime.sendMessage({ action: 'RESET_AGENT_TIMELINE' }).catch(() => {});

    emitStatus('frontend', '收到开始分析指令，准备提取页面正文');

    const text = extractArticleText();
    const title = document.title || "未命名网页";
    const url = window.location.href || "";

    if (!text || text.trim() === "") {
        alert("未检测到有效文章正文，请在包含文章的网页中使用该插件！");
        emitError("未检测到有效文章正文");
        emitDone();
        return;
    }

    emitStatus('frontend', `正文提取完成，准备请求后端`);
    updateDynamicIsland('show', 'Agent 正在深度核查此页面...');
    
    // 带重试发送，防止 service worker 冷启动时消息丢失
    await sendMessageWithRetry({ 
        action: 'START_BACKEND_ANALYSIS', 
        articleText: text,
        articleTitle: title,
        sourceUrl: url,
        skillId: config.skill_id,
        overlays: config.overlays
    });
}

function extractArticleText() {
    try {
        emitStatus('frontend', '正在使用 Readability 提取正文');
        const documentClone = document.cloneNode(true);
        const reader = new Readability(documentClone);
        const article = reader.parse();

        if (article && article.textContent) {
            emitStatus('frontend', `Readability 提取成功：${article.title || '未命名文章'}`);
            const cleanText = article.textContent.replace(/\n\s*\n/g, '\n').trim();
            return cleanText.substring(0, 8000);
        }
    } catch (e) {
        emitStatus('frontend', `Readability 提取失败，降级到普通抓取：${e.message || e}`);
    }

    emitStatus('frontend', '使用页面 innerText 兜底提取正文');
    return document.body.innerText.substring(0, 5000); 
}

function processAgentEvent(event) {
    if (event.type === 'annotation') {
        window.ANNOTATIONS[event.claim_id] = event;
        if (event.error_type && event.error_type !== 'null') {
            highlightTextInDOM(event);
        }
    } 
    else if (event.type === 'done') {
        updateDynamicIsland('done', '核查完毕，请查看页面批注');
    } 
    else if (event.type === 'error') {
        updateDynamicIsland('error', '分析中断：' + (event.message || '未知错误'));
    }
}

function handleAgentEvent(event) {
    chrome.runtime.sendMessage({ action: "PUSH_AGENT_EVENT", payload: event });
    processAgentEvent(event);
}

function emitStatus(stage, message, details = {}) {
    handleAgentEvent({ type: 'status', stage, message, details });
}

function emitError(message, claimId = null) {
    handleAgentEvent({ type: 'error', claim_id: claimId, message });
}

function emitDone(totalAnnotations = 0) {
    handleAgentEvent({ type: 'done', total_annotations: totalAnnotations });
}

// 🌟 动态岛控制
function updateDynamicIsland(status, text) {
    const host = document.getElementById('agent-factcheck-ui');
    if (!host || !host.shadowRoot) return;
    
    const island = host.shadowRoot.getElementById('dynamic-island');
    if (!island) return;

    if (status === 'show') {
        island.innerHTML = `<div class="spinner"></div><span>${text}</span>`;
        island.classList.add('island-show');
    } 
    else if (status === 'done') {
        island.innerHTML = `✅ <span style="font-weight:bold; color:#a7f3d0;">${text}</span>`;
        island.style.background = '#065f46';
        setTimeout(() => {
            island.classList.remove('island-show');
            setTimeout(() => island.style.background = '#000', 500); 
        }, 3000);
    } 
    else if (status === 'error') {
        island.innerHTML = `❌ <span style="font-weight:bold; color:#fecaca;">${text}</span>`;
        island.style.background = '#991b1b';
        setTimeout(() => {
            island.classList.remove('island-show');
            setTimeout(() => island.style.background = '#000', 500);
        }, 3000);
    }
}