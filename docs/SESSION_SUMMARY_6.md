# SellerAgent 会话总结 #6

> 日期: 2026-07-30 | 会话重点: 手机端响应式真机修复、微信 WebView 缓存策略、部署流程优化

---

## 一、手机端滚动失效 — DevTools vs 真机

### 现象

Chrome DevTools 模拟手机一切正常，但真机（iOS Safari / Android Chrome / 微信 WebView）完全滑不动。

### 根因分析

| # | 问题 | DevTools 模拟 | 真机 |
|---|---|---|---|
| 1 | `body { overflow: hidden }` | 子元素可独立滚动 | iOS Safari 阻止**所有子元素**的原生滚动层创建 |
| 2 | `.mobile-overlay` 遮罩层 `visibility: hidden` 但无 `pointer-events: none` | 不可见 = 不拦截 | 依然覆盖全屏，拦截所有触摸事件 |
| 3 | flex 子元素无 `min-height: 0` | 正常收缩 | Safari 让内容撑开容器，不触发 `overflow-y: auto` |
| 4 | `100vh` 包含地址栏高度 | 不体现 | 手机浏览器地址栏收缩/展开导致布局偏移 |
| 5 | 缺少 `touch-action` / `overscroll-behavior` | 不影响 | 移动端手势被浏览器默认行为拦截 |

### 修复方案

**`static/css/style.css`（已重命名为 `style-v2.css`）**，768px 媒体查询内：

1. **body 覆盖**：`position: static; overflow: visible; overscroll-behavior: none; height: 100dvh`
2. **遮罩层**：添加 `pointer-events: none`（默认），打开时 `pointer-events: auto`
3. **可滚动区域**：`.chat-messages`、`.session-list`、`.kb-section:last-child` 都加 `min-height: 0; -webkit-overflow-scrolling: touch; overscroll-behavior: contain; touch-action: pan-y`
4. **dvh 降级**：`height: calc(100vh - 48px); height: calc(100dvh - 48px)` — 支持 `dvh` 的浏览器用后者覆盖
5. **关闭按钮位置**：KB 面板右上角 `✕` 从 `top: 10px` 调为 `top: 2px`

---

## 二、微信 WebView 缓存 — 文件名策略

### 现象

微信内置浏览器打开服务器页面，CSS 样式是旧的。换手机自带浏览器正常。无论怎么刷新、退出微信重进都不更新。

### 根因

微信 X5/WebView 的缓存策略极其激进：
- **Query string 版本号无效**（`style.css?v=2` 对微信没意义）
- **HTTP 缓存头无效**（微信忽略 `Cache-Control`）
- 文件名是微信判断"是否为新资源"的唯一依据

### 解决方案

**直接改文件名**：`style.css` → `style-v2.css`

```html
<!-- index.html -->
<link rel="stylesheet" href="/static/css/style-v2.css">
```

文件名不同 → 微信认为是新文件 → 一定重新下载。以后每次改 CSS 递增版本号即可。

---

## 三、start.sh 简化

之前 `start.sh` 包含自动杀进程 (`pkill`)、后台启动 (`nohup`)、自动验证等逻辑，但用户更习惯自己手动管理进程：

```bash
#!/bin/bash
cd "$(dirname "$0")"

export HF_ENDPOINT=https://hf-mirror.com
export CUDA_VISIBLE_DEVICES=""
export PYTHONIOENCODING=utf-8
export MILVUS_DB_PATH="$(pwd)/milvus_fresh/seller_kb.db"

if [ ! -f .env ]; then
    echo "错误: .env 文件不存在，请先配置"
    exit 1
fi

if [ ! -f "venv/bin/python3" ]; then
    echo "错误: 找不到 venv/bin/python3"
    exit 1
fi

./venv/bin/python3 run.py
```

**操作流程**：
1. SSH 登录 → `Ctrl+C` 关旧进程
2. `ss -tlnp | grep 8080` 确认端口已释放（必要时 `kill -9 PID`）
3. `unzip -o SellerAgent-deploy.zip`
4. `bash start.sh`

---

## 四、本地开发环境

| 项目 | 值 |
|---|---|
| Python | `c:\Users\15613\Desktop\LangchainRAGtrain\rag_env\Scripts\python` |
| 启动命令 | `source rag_env/Scripts/activate && cd SellerAgent && python run.py` |
| 局域网 IP | `10.204.42.152` |
| 访问地址 | `http://10.204.42.152:8080` |
| 手机测试 | 连同一 WiFi，浏览器访问上述地址 |

---

## 五、修改文件清单

| 文件 | 改动 |
|---|---|
| `static/css/style.css` → `static/css/style-v2.css` | 重命名（微信缓存策略）；主体 CSS 不变，移动端媒体查询大幅改写（dvh、pointer-events、min-height、touch-action、iOS Safari 兼容） |
| `static/index.html` | CSS 引用改为 `style-v2.css` |
| `static/js/app.js` | 面板控制函数（toggleSidebar/toggleKBPanel/closeAllPanels）；resize 监听自动关闭面板 |
| `start.sh` | 大幅简化，去掉 pkill/nohup/自动验证 |
| `.gitignore` | 新增 `server.log` |

---

## 六、仓库状态

- **GitHub**: https://github.com/hwskunk/SellerAgent
- **最新 commit**: `e6b00fd` — 手机端响应式适配 + 部署优化
- **部署包**: `SellerAgent-deploy.zip`（~90KB），不含 `knowledge/`、`__pycache__`、`*.db`
