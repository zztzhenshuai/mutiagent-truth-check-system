// content/highlighter.js

const ERROR_COLORS = {
    factual_error: '#FFB3B3',      // 红
    logical_fallacy: '#FFE0B3',    // 橙
    contradiction: '#FFF9B3',      // 黄
    unsupported_claim: '#B3D4FF',  // 蓝
};

// 全局记录已经被处理过的 claim_id，防止重复高亮同一个事件
window.HIGHLIGHTED_CLAIMS = new Set();

/**
 * 在 DOM 中查找并高亮指定的错误文本
 */
function highlightTextInDOM(annotation) {
    if (!annotation.text || !annotation.text.trim()) return;
    if (window.HIGHLIGHTED_CLAIMS.has(annotation.claim_id)) return; // 已经高亮过就不再处理
    
    // 清理后端返回文本前后的空白和标点，提高匹配成功率
    const targetText = annotation.text.trim();

    // 遍历网页上所有可见的文本节点
    const treeWalker = document.createTreeWalker(
        document.body,
        NodeFilter.SHOW_TEXT,
        {
            acceptNode: (node) => {
                // 排除已经被我们包裹过的 mark 标签内部的文本
                if (node.parentElement && node.parentElement.tagName.toLowerCase() === 'mark') {
                    return NodeFilter.FILTER_REJECT;
                }
                // 排除 script, style 等不可见标签
                const parentTag = node.parentElement ? node.parentElement.tagName.toLowerCase() : '';
                if (['script', 'style', 'noscript', 'textarea'].includes(parentTag)) {
                    return NodeFilter.FILTER_REJECT;
                }
                return node.nodeValue.trim().length > 0 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
            }
        }
    );

    let currentNode;
    let found = false;

    // 尝试在单一节点内寻找
    while ((currentNode = treeWalker.nextNode())) {
        const text = currentNode.nodeValue;
        const index = text.indexOf(targetText);
        
        if (index !== -1) {
            // 找到了！精确包裹
            wrapTextNode(currentNode, index, targetText.length, annotation);
            found = true;
            window.HIGHLIGHTED_CLAIMS.add(annotation.claim_id);
            break; // 这句话找到了就跳出，继续等后端的下一个 annotation
        }
    }

    // 如果在单一节点里没找到，可能是大模型只给了一段很长的话的一部分
    // 增加一个降级策略：如果没找到完整的原句，尝试把句子劈开查找（仅为保证演示效果）
    if (!found) {
        console.warn(`[Highlighter] 无法在单节点内精确匹配完整句子: "${targetText.substring(0, 20)}..."。可能文本横跨了HTML标签。`);
        
        // 尝试只找前 10 个字定位（作为兜底容错，保证演示时一定能亮起一块）
        const fallbackText = targetText.substring(0, 10);
        if (fallbackText.length >= 5) {
            treeWalker.currentNode = document.body; // 重置 walker
            while ((currentNode = treeWalker.nextNode())) {
                const text = currentNode.nodeValue;
                const index = text.indexOf(fallbackText);
                if (index !== -1) {
                    wrapTextNode(currentNode, index, fallbackText.length, annotation);
                    window.HIGHLIGHTED_CLAIMS.add(annotation.claim_id);
                    break;
                }
            }
        }
    }
}

/**
 * 提取出的独立包裹逻辑
 */
function wrapTextNode(textNode, startIndex, length, annotation) {
    const range = document.createRange();
    range.setStart(textNode, startIndex);
    range.setEnd(textNode, startIndex + length);

    const mark = document.createElement('mark');
    mark.className = 'agent-highlight';
    mark.dataset.claimId = annotation.claim_id;
    
    // 👇 魔法下划线样式 👇
    mark.style.background = 'transparent'; // 去掉背景色
    mark.style.color = 'inherit';
    
    // 根据错误类型给不同的流光下划线
    const lineColors = {
        factual_error: 'linear-gradient(90deg, #ff4d4f, #ff7a45)', // 警报红
        logical_fallacy: 'linear-gradient(90deg, #faad14, #ffc53d)', // 警告橙
    };
    const gradient = lineColors[annotation.error_type] || 'linear-gradient(90deg, #94a3b8, #cbd5e1)';
    
    mark.style.borderBottom = '2px solid transparent';
    mark.style.borderImage = `${gradient} 1`;
    mark.style.textShadow = '0 0 8px rgba(255, 77, 79, 0.2)'; // 字体微微发光
    mark.style.cursor = 'help';
    mark.style.position = 'relative';

    try {
        range.surroundContents(mark);
        
        // 高级交互：在文字末尾加一个跳动的红点（呼吸灯）
        const dot = document.createElement('span');
        dot.innerHTML = '●';
        dot.style.position = 'absolute';
        dot.style.top = '-8px';
        dot.style.right = '-12px';
        dot.style.fontSize = '12px';
        dot.style.animation = 'pulse 1.5s infinite';
        mark.appendChild(dot);
        
    } catch (e) {
        console.error(e);
    }
}