// 采用 Shadow DOM 隔离 CSS，防止被页面的样式影响
const host = document.createElement('div');
host.id = 'agent-factcheck-ui';
// 让 host 成为定位参考点，覆盖整个 viewport，但不拦截点击
host.style.position = 'fixed';
host.style.top = '0';
host.style.left = '0';
host.style.width = '100%';
host.style.height = '100%';
host.style.pointerEvents = 'none';
host.style.zIndex = '999999';
document.documentElement.appendChild(host);
const shadow = host.attachShadow({ mode: 'open' });

shadow.innerHTML = `
  <style>
    /* 弹性进场动画 */
    @keyframes popIn {
      0% { opacity: 0; transform: scale(0.9) translateY(10px); }
      100% { opacity: 1; transform: scale(1) translateY(0); }
    }

    /* 🌟 核心：毛玻璃 Tooltip */
    #tooltip {
      position: absolute; display: none; z-index: 999999;
      pointer-events: auto;
      background: rgba(255, 255, 255, 0.85);
      backdrop-filter: blur(16px) saturate(180%);
      -webkit-backdrop-filter: blur(16px) saturate(180%);
      border: 1px solid rgba(255, 255, 255, 0.4);
      border-radius: 16px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.08), 0 0 0 1px rgba(0,0,0,0.02);
      padding: 16px; width: 280px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px; color: #1e293b;
      animation: popIn 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    }
    
    .badge { 
      padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; 
      color: #fff; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
    }
    
    /* 置信度进度条设计 */
    .confidence-track { width: 100%; height: 6px; background: #e2e8f0; border-radius: 10px; margin-top: 12px; overflow: hidden; }
    .confidence-fill { height: 100%; background: linear-gradient(90deg, #10b981, #3b82f6); border-radius: 10px; transition: width 1s ease-out; }
    
    .reasoning { margin-top: 12px; line-height: 1.5; color: #475569; }
    .source { margin-top: 12px; display: inline-block; padding: 6px 12px; background: #f1f5f9; border-radius: 8px; font-size: 12px; color: #2563eb; cursor: pointer; text-decoration: none; font-weight: 500; transition: all 0.2s;}
    .source:hover { background: #e2e8f0; transform: translateY(-1px); }

    /* 灵动岛样式 */
    #dynamic-island {
      position: fixed; top: -50px; left: 50%; transform: translateX(-50%);
      background: #000; color: #fff; padding: 10px 24px; border-radius: 30px;
      font-size: 14px; font-weight: 500; font-family: sans-serif; z-index: 999999;
      box-shadow: 0 10px 30px rgba(0,0,0,0.2);
      display: flex; align-items: center; gap: 10px;
      transition: all 0.5s cubic-bezier(0.68, -0.55, 0.26, 1.55);
    }
    .island-show { top: 20px !important; }
    .spinner { width: 14px; height: 14px; border: 2px solid #333; border-top-color: #fff; border-radius: 50%; animation: spin 1s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>

  <div id="tooltip"></div>
  <div id="dynamic-island">
    <div class="spinner"></div>
    <span>Agent 正在扫描全网核查此页面...</span>
  </div>
`;

const tooltip = shadow.getElementById('tooltip');

// tooltip 延迟隐藏，让鼠标有时间从 mark 移入 tooltip
let tooltipHideTimer = null;
let currentMark = null;

function showTooltip(mark, data) {
    // 清除之前的隐藏计时器
    if (tooltipHideTimer) { clearTimeout(tooltipHideTimer); tooltipHideTimer = null; }
    currentMark = mark;

    // 处理有没有证据链接的情况
    let evidenceHtml = '';
    if (data.evidence_urls && data.evidence_urls.length > 0) {
        evidenceHtml = `<a class="source" href="${data.evidence_urls[0]}" target="_blank">📄 查看原始出处 &rarr;</a>`;
    }

    // 根据错误类型动态改变标签颜色
    const badgeColor = data.error_type === 'factual_error' ? '#ef4444' : 
                       data.error_type === 'logical_fallacy' ? '#f59e0b' : '#3b82f6';

    tooltip.innerHTML = `
        <div><span class="badge" style="background:${badgeColor}">${data.error_type}</span></div>
        
        <div style="margin-top:12px; font-size:12px; font-weight:600; color:#64748b; display:flex; justify-content:space-between;">
            <span>AI 置信度</span>
            <span>${(data.confidence * 100).toFixed(0)}%</span>
        </div>
        <div class="confidence-track">
            <div class="confidence-fill" style="width: ${(data.confidence * 100)}%;"></div>
        </div>

        <div class="reasoning">${data.reasoning}</div>
        ${evidenceHtml}
    `;
    
    // host 是 position:fixed 覆盖整个 viewport，tooltip 的 absolute 以它为参考
    // rect 坐标是 viewport 相对坐标，直接使用即可
    const rect = mark.getBoundingClientRect();
    tooltip.style.left = `${rect.left}px`;
    tooltip.style.top = `${rect.bottom + 8}px`;
    tooltip.style.display = 'block';
}

function hideTooltip() {
    tooltip.style.display = 'none';
    currentMark = null;
}

// ---- 鼠标事件：延迟隐藏，让鼠标能从 mark 移入 tooltip ----
const HIDE_DELAY = 250; // ms

document.body.addEventListener('mouseover', (e) => {
    const mark = e.target.closest('mark.agent-highlight');
    if (mark && mark.dataset.claimId) {
        // 清除隐藏计时器
        if (tooltipHideTimer) { clearTimeout(tooltipHideTimer); tooltipHideTimer = null; }
        const data = window.ANNOTATIONS[mark.dataset.claimId];
        if (!data) return;
        showTooltip(mark, data);
    }
});

document.body.addEventListener('mouseout', (e) => {
    const mark = e.target.closest('mark.agent-highlight');
    if (mark) {
        // 延迟隐藏，给鼠标时间进入 tooltip
        tooltipHideTimer = setTimeout(() => {
            hideTooltip();
            tooltipHideTimer = null;
        }, HIDE_DELAY);
    }
});

// 鼠标进入 tooltip → 取消隐藏，保持展示
tooltip.addEventListener('mouseenter', () => {
    if (tooltipHideTimer) { clearTimeout(tooltipHideTimer); tooltipHideTimer = null; }
});

// 鼠标离开 tooltip → 立即隐藏
tooltip.addEventListener('mouseleave', () => {
    hideTooltip();
});