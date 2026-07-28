/**
 * SellerAgent — 销售智能体 前端逻辑
 * 面向零基础用户：所有操作都有明确的按钮和提示
 */

// ═══════════════════════════════════════════════════════════════
// 全局状态
// ═══════════════════════════════════════════════════════════════

const STATE = {
    threadId: 'default',
    isProcessing: false,
};

// SVG 图标快捷引入
function icon(name) {
    return `<img src="/static/icons/${name}.svg" class="icon-img" alt="">`;
}

// ═══════════════════════════════════════════════════════════════
// 初始化
// ═══════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
    initChat();
    refreshDocList();
    refreshStats();
});

// ═══════════════════════════════════════════════════════════════
// 对话功能
// ═══════════════════════════════════════════════════════════════

function initChat() {
    const input = document.getElementById('chatInput');
    const sendBtn = document.getElementById('sendBtn');

    // 发送按钮
    sendBtn.addEventListener('click', () => sendMessage());

    // Enter 发送，Shift+Enter 换行
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // 自动调整输入框高度
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });
}

async function sendMessage() {
    const input = document.getElementById('chatInput');
    const sendBtn = document.getElementById('sendBtn');
    const message = input.value.trim();

    if (!message || STATE.isProcessing) return;

    // 添加用户消息
    addMessage('user', message);
    input.value = '';
    input.style.height = 'auto';

    STATE.isProcessing = true;
    sendBtn.disabled = true;

    // 创建智能体消息占位（流式追加内容）
    const msgEl = addStreamingMessage();

    try {
        const response = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: message, thread_id: STATE.threadId }),
        });

        if (!response.ok) throw new Error('请求失败: ' + response.status);

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let fullContent = '';
        let sources = [];

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (!line.startsWith('data: ') || line === 'data: [DONE]') continue;

                try {
                    const data = JSON.parse(line.slice(6));
                    if (data.type === 'meta') {
                        sources = data.sources || [];
                    } else if (data.type === 'token') {
                        fullContent += data.content;
                        updateStreamingMessage(msgEl, fullContent, sources);
                    } else if (data.type === 'error') {
                        fullContent = '抱歉，处理您的请求时出现了错误：' + data.content;
                        updateStreamingMessage(msgEl, fullContent, sources);
                    }
                } catch (e) {
                    // 忽略解析失败的行
                }
            }
        }

        // 流结束，最终渲染
        finalizeStreamingMessage(msgEl, fullContent, sources);

    } catch (error) {
        updateStreamingMessage(msgEl, '抱歉，处理您的请求时出现了错误：' + error.message, []);
        finalizeStreamingMessage(msgEl, '抱歉，处理您的请求时出现了错误：' + error.message, []);
        console.error('Chat error:', error);
    } finally {
        STATE.isProcessing = false;
        sendBtn.disabled = false;
        input.focus();
    }
}

function addMessage(role, content, sources) {
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = `message ${role}`;

    const avatarIcon = role === 'user' ? icon('user') : icon('bot');
    const avatarLabel = role === 'user' ? '我' : '销售顾问';

    let sourcesHtml = '';
    if (sources && sources.length > 0) {
        const tags = sources.map(s =>
            `<span class="source-tag" title="相关度: ${(s.score * 100).toFixed(0)}%">${icon('docs')} ${escapeHtml(s.title || s.source)}</span>`
        ).join('');
        sourcesHtml = `<div class="message-sources">${icon('resources')} 参考来源: ${tags}</div>`;
    }

    div.innerHTML = `
        <div class="message-avatar" title="${avatarLabel}">${avatarIcon}</div>
        <div>
            <div class="message-bubble">${formatContent(content)}</div>
            ${sourcesHtml}
        </div>
    `;

    container.appendChild(div);
    scrollToBottom();
}

// ── 流式消息 ──

function addStreamingMessage() {
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'message agent';
    div.innerHTML = `
        <div class="message-avatar" title="销售顾问">${icon('bot')}</div>
        <div>
            <div class="message-bubble"><span class="streaming-cursor"></span></div>
        </div>
    `;
    container.appendChild(div);
    scrollToBottom();
    return div;
}

function updateStreamingMessage(el, content, sources) {
    const bubble = el.querySelector('.message-bubble');
    let html = formatContent(content);
    // 流式输出时保留闪烁光标
    html += '<span class="streaming-cursor"></span>';
    bubble.innerHTML = html;
    scrollToBottom();
}

function finalizeStreamingMessage(el, content, sources) {
    const bubble = el.querySelector('.message-bubble');
    // 移除光标，最终渲染
    bubble.innerHTML = formatContent(content);

    // 追加引用来源
    if (sources && sources.length > 0) {
        const tags = sources.map(s =>
            `<span class="source-tag" title="相关度: ${(s.score * 100).toFixed(0)}%">${icon('docs')} ${escapeHtml(s.title || s.source)}</span>`
        ).join('');
        const sourcesDiv = document.createElement('div');
        sourcesDiv.className = 'message-sources';
        sourcesDiv.innerHTML = `${icon('resources')} 参考来源: ${tags}`;
        el.querySelector('div:last-child').appendChild(sourcesDiv);
    }
    scrollToBottom();
}

function scrollToBottom() {
    const container = document.getElementById('chatMessages');
    setTimeout(() => {
        container.scrollTop = container.scrollHeight;
    }, 100);
}

function formatContent(text) {
    // 使用 marked.js 渲染 Markdown（表格、标题、列表等）
    if (typeof marked !== 'undefined') {
        marked.setOptions({ breaks: true, gfm: true });
        return marked.parse(text);
    }
    // 降级：简单转义
    return escapeHtml(text).replace(/\n/g, '<br>');
}

// ═══════════════════════════════════════════════════════════════
// 知识库管理
// ═══════════════════════════════════════════════════════════════

// ── 刷新文档列表 ──

async function refreshDocList() {
    try {
        const response = await fetch('/api/kb/docs');
        const data = await response.json();

        if (data.success) {
            renderDocList(data.data);
        } else {
            showToast('获取文档列表失败: ' + data.error, 'error');
        }
    } catch (error) {
        console.error('获取文档列表失败:', error);
    }
}

function renderDocList(docs) {
    const container = document.getElementById('docList');

    if (!docs || docs.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="icon">${icon('empty')}</div>
                <p>知识库中还没有文档</p>
                <p style="font-size:12px;margin-top:4px;">在下方添加第一条知识吧</p>
            </div>
        `;
        return;
    }

    container.innerHTML = docs.map(doc => `
        <div class="doc-item" onclick="viewDocument('${doc.id}')" title="点击查看完整内容">
            <div class="doc-item-header">
                <span class="doc-item-title">${icon('docs')} ${escapeHtml(doc.title || '无标题')}</span>
                <button class="btn btn-danger btn-sm" onclick="event.stopPropagation(); deleteDocument('${doc.id}')" title="删除此文档">
                    <img src="/static/icons/delete.svg" class="icon-img" alt=""> 删除
                </button>
            </div>
            <div class="doc-item-meta">
                <span>${icon('chars')} ${(doc.char_count || 0).toLocaleString()} 字</span>
                <span>${icon('time')} ${doc.created_at}</span>
            </div>
        </div>
    `).join('');
}

// ── 添加文本文档 ──

document.addEventListener('DOMContentLoaded', () => {
    const addBtn = document.getElementById('btnAddDoc');
    if (addBtn) {
        addBtn.addEventListener('click', addTextDocument);
    }
});

async function addTextDocument() {
    const titleInput = document.getElementById('docTitle');
    const contentInput = document.getElementById('docContent');
    const addBtn = document.getElementById('btnAddDoc');

    const title = titleInput.value.trim();
    const content = contentInput.value.trim();

    if (!content) {
        showToast('请输入文档内容', 'error');
        contentInput.focus();
        return;
    }

    addBtn.disabled = true;
    addBtn.textContent = '添加中...';

    try {
        const response = await fetch('/api/kb/docs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: title || '未命名文档',
                content: content,
                source: 'manual',
            }),
        });

        const data = await response.json();

        if (data.success) {
            showToast('文档添加成功！', 'success');
            titleInput.value = '';
            contentInput.value = '';
            refreshDocList();
            refreshStats();
        } else {
            showToast('添加失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('添加失败: ' + error.message, 'error');
    } finally {
        addBtn.disabled = false;
        addBtn.textContent = '添加到知识库';
    }
}

// ── 上传文件 ──

function triggerFileUpload() {
    const fileInput = document.getElementById('fileUpload');
    fileInput.click();
}

document.addEventListener('DOMContentLoaded', () => {
    const fileInput = document.getElementById('fileUpload');
    if (fileInput) {
        fileInput.addEventListener('change', uploadFile);
    }
});

function uploadFile(event) {
    const file = event.target.files[0];
    if (!file) return;

    const uploadBtn = document.getElementById('uploadArea');
    const progressContainer = document.getElementById('uploadProgress');
    const progressBar = document.getElementById('progressBarFill');
    const progressText = document.getElementById('progressText');

    // 显示进度条
    uploadBtn.style.display = 'none';
    progressContainer.style.display = 'block';
    progressBar.style.width = '0%';
    progressBar.classList.add('active');
    progressText.textContent = '准备上传...';

    const formData = new FormData();
    formData.append('file', file);
    formData.append('filename', file.name);  // 显式传文件名，避免 HTTP 头编码问题
    const startTime = Date.now();
    const MIN_DISPLAY_MS = 2000; // 最少显示 2 秒，避免一闪而过

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/kb/upload');

    // 上传进度（XHR 层面的文件传输）
    xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
            const pct = Math.round((e.loaded / e.total) * 100 * 0.3); // 上传阶段占 30%
            progressBar.style.width = pct + '%';
            const loadedMB = (e.loaded / 1024 / 1024).toFixed(1);
            const totalMB = (e.total / 1024 / 1024).toFixed(1);
            progressText.textContent = `上传中... ${loadedMB}MB / ${totalMB}MB`;
        }
    });

    // 上传完成 → 进入服务端处理阶段（进度条 30%→90%，脉冲动画）
    xhr.upload.addEventListener('loadend', () => {
        progressBar.style.width = '30%';
        const tick = setInterval(() => {
            const elapsed = (Date.now() - startTime) / 1000;
            // 模拟进度 30%→90%，随耗时增长
            const simPct = Math.min(90, 30 + elapsed * 4); // ~15s 到 90%
            progressBar.style.width = simPct + '%';

            if (elapsed < 4) {
                progressText.textContent = `正在解析文档... (${elapsed.toFixed(0)}s)`;
            } else if (elapsed < 12) {
                progressText.textContent = `正在生成向量... (${elapsed.toFixed(0)}s)`;
            } else {
                progressText.textContent = `正在写入知识库... (${elapsed.toFixed(0)}s)`;
            }
            if (xhr.readyState === XMLHttpRequest.DONE) {
                clearInterval(tick);
            }
        }, 400);
    });

    // 服务端响应到达
    xhr.addEventListener('load', () => {
        progressBar.classList.remove('active');
        progressBar.style.width = '100%';
        const elapsed = Date.now() - startTime;

        try {
            const data = JSON.parse(xhr.responseText);
            if (data.success) {
                progressText.innerHTML = `${icon('success')} 完成！(${(elapsed / 1000).toFixed(1)}s)`;
                showToast(`"${file.name}" 上传成功！`, 'success');
                refreshDocList();
                refreshStats();
            } else {
                progressText.innerHTML = icon('error') + ' 处理失败';
                showToast('处理失败: ' + data.error, 'error');
            }
        } catch (e) {
            progressText.innerHTML = icon('error') + ' 响应异常';
            showToast('响应异常', 'error');
        }

        // 延迟恢复 UI，让用户看到完成状态
        const remaining = Math.max(0, MIN_DISPLAY_MS - elapsed);
        setTimeout(() => {
            progressContainer.style.display = 'none';
            uploadBtn.style.display = 'block';
        }, remaining + 800);

        event.target.value = '';
    });

    xhr.addEventListener('error', () => {
        progressBar.classList.remove('active');
        progressBar.style.width = '100%';
        progressText.innerHTML = icon('error') + ' 网络错误';
        showToast('上传失败: 网络错误', 'error');
        setTimeout(() => {
            progressContainer.style.display = 'none';
            uploadBtn.style.display = 'block';
        }, 2000);
        event.target.value = '';
    });

    xhr.send(formData);
}

// ── 删除文档 ──

async function deleteDocument(docId) {
    if (!confirm('确定要删除这条知识文档吗？\n\n此操作不可撤销，删除后智能体将无法检索到该内容。')) {
        return;
    }

    try {
        const response = await fetch(`/api/kb/docs/${docId}`, {
            method: 'DELETE',
        });

        const data = await response.json();

        if (data.success) {
            showToast('文档已删除', 'success');
            refreshDocList();
            refreshStats();
        } else {
            showToast('删除失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('删除失败: ' + error.message, 'error');
    }
}

// ── 查看文档全文 ──

async function viewDocument(docId) {
    const modal = document.getElementById('docModal');
    const titleEl = document.getElementById('modalTitle');
    const bodyEl = document.getElementById('modalBody');

    modal.style.display = 'flex';
    titleEl.textContent = '加载中...';
    bodyEl.innerHTML = '<div class="loading"></div> 加载中...';

    try {
        const response = await fetch(`/api/kb/docs/${docId}`);
        const data = await response.json();

        if (data.success) {
            const doc = data.data;
            titleEl.innerHTML = `${icon('docs')} ${escapeHtml(doc.title || '无标题')}`;
            const rendered = formatContent(doc.content || '暂无内容');
            bodyEl.innerHTML =
                `<div class="doc-item-meta" style="margin-bottom:16px;">` +
                `${icon('chars')} ${(doc.char_count || 0).toLocaleString()} 字 &nbsp;|&nbsp; ` +
                `${icon('time')} ${escapeHtml(doc.created_at || '')}` +
                `</div>` +
                `<div class="markdown-body">${rendered}</div>`;
        } else {
            bodyEl.innerHTML = '加载失败: ' + escapeHtml(data.error);
        }
    } catch (error) {
        bodyEl.innerHTML = '加载失败: ' + escapeHtml(error.message);
    }
}

function closeModal() {
    document.getElementById('docModal').style.display = 'none';
}

// 点击遮罩关闭弹窗
document.addEventListener('DOMContentLoaded', () => {
    const modal = document.getElementById('docModal');
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) closeModal();
        });
    }
    // ESC 关闭
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeModal();
    });
});

// ── 刷新统计 ──

async function refreshStats() {
    try {
        const response = await fetch('/api/kb/stats');
        const data = await response.json();

        if (data.success) {
            document.getElementById('statDocs').textContent = data.data.total_documents;
            document.getElementById('statChars').textContent = formatNumber(data.data.total_characters);
            document.getElementById('statDocsPanel').textContent = data.data.total_documents;
            document.getElementById('statCharsPanel').textContent = formatNumber(data.data.total_characters);
        }
    } catch (error) {
        console.error('获取统计失败:', error);
    }
}

// ═══════════════════════════════════════════════════════════════
// 工具函数
// ═══════════════════════════════════════════════════════════════

function showToast(message, type) {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s';
        setTimeout(() => {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }, 300);
    }, 3000);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatNumber(num) {
    if (num >= 10000) {
        return (num / 10000).toFixed(1) + '万';
    }
    return num.toLocaleString('zh-CN');
}
