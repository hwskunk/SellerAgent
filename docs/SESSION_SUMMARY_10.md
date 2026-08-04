# SellerAgent 会话总结 #10

> 日期: 2026-08-04 | 会话重点: 企业微信「微信客服」未认证测试路径打通、回调调试排障、feat/wecom-integration 分支

---

## 一、核心结论

**未认证企业 + 公网 IP 的微信客服测试路径可行**：

- 未认证企业可以用**公网 IP 形式回调 URL** 配置微信客服回调（无需域名 / 无需 HTTPS）
- 回调 URL 支持 **HTTP**（不强制 HTTPS，免去 IP 配 SSL 证书的坑）
- 已认证企微则强制要求备案域名（备案主体须与企业认证主体一致）
- ⚠️ 未认证企业的微信客服有**接待 100 客户上限**——测试够用，生产上线仍需**企业认证 + 域名备案**（认证后免费 2000 客户，可扩容 5 万+）

---

## 二、代码改动（feat/wecom-integration 分支）

| 提交 | 内容 |
|---|---|
| `bddfdb2` | 微信客服接入准备版：回调端点 + 未认证无签名兼容 + 自建应用 Secret 支持 |
| `2258425` | 未认证企微 echostr 密文兼容：无签名时先尝试解密、失败回显明文 |

### 具体改动文件

- **`src/wecom/` 全套**（crypto / handler / kf_handler）——会话 9 已完成，本次纳入版本控制
- **`app.py`**: 新增 `GET/POST /api/wecom/callback`
  - GET 兼容两种模式：已认证（验签解密）/ 未认证（无签名时先解密、失败回显明文）
  - POST 方式 B：回调立即返回 200，后台拉取消息 + RAG 回复
- **`src/wecom/kf_handler.py`**: 支持 `WECOM_APP_SECRET`（新规范优先），未配置时回退 `WECOM_KF_SECRET`
- **`src/wecom/crypto.py`**: 新增 `decrypt_echostr()`（不验签直接解密，供未认证 URL 验证）
- **`.env.example`**: 新增 `WECOM_APP_SECRET` 说明

### 对原有功能：纯增量

- 原有端点（chat / threads / kb）**零改动**
- 唯一改动 `static/js/app.js`：会话列表过滤 `wecom_` 前缀会话（企微会话不混入网页管理界面），普通会话不受影响
- 新增依赖仅 `pycryptodome`（AES 加密库）
- 回调端点测试 6 项全过（`test_wecom_callback.py`）

---

## 三、部署踩坑记录（最重要）

按时间顺序遇到并解决：

### 1. openapi回调地址请求不通过（第一波）
- **现象**：验证 GET 打到 `/?...`（根路径），返回首页 200
- **根因**：回调 URL 只填了 `http://IP:8080`，**少了 `/api/wecom/callback` 路径**
- **修复**：回调 URL 补全为 `http://公网IP:8080/api/wecom/callback`

### 2. No module named 'Crypto'
- **根因**：`pycryptodome` 已加进 requirements.txt，但服务器部署时**没重新装依赖**
- **修复**：`./venv/bin/python3 -m pip install pycryptodome`
- **教训**：**requirements.txt 有变化时必须重新 `pip install`**，仅覆盖代码文件不够

### 3. 微信客服配置缺失: WECOM_CORP_ID, WECOM_TOKEN, WECOM_ENCODING_AES_KEY
- **根因**：`.env` 里企微配置项的分隔符是**全角冒号 `：`**，dotenv 只认等号 `=`
- **修复**：`：` 全部换成 `=`（所有配置项必须用等号分隔，且不留行内注释）

### 4. 回调验证通过但仍收不到消息 / 服务器无日志
- **根因排查中**（见待办）：
  - 客服账号无接待人员（需企业微信 App 接受邀请激活）
  - 回调配置需点"完成/开始使用"才真正生效
  - 未认证账号可能显示"当前账号异常"

### 其他要点
- 回调 URL 验证通过 ≠ 已启用，还需点**"完成/开始使用"**
- 微信客服账号**必须有接待人员**（至少一个企业员工，企业微信 App 里接受邀请）才能接客
- 接待人员 ≠ 客户：接待人员是后台客服角色（AI 自动回复即扮演此角色），客户永远是外部微信用户扫码
- `WECOM_OPEN_KFID` 可留空（代码从回调事件自动获取 open_kfid，不依赖配置）

---

## 四、服务器 .env 配置清单

```bash
WECOM_CORP_ID=企业ID
WECOM_APP_SECRET=自建应用Secret（新规范，优先）
WECOM_KF_SECRET=            # 可留空（回退用）
WECOM_TOKEN=回调Token
WECOM_ENCODING_AES_KEY=43位密钥
WECOM_OPEN_KFID=            # 可留空
```

⚠️ 所有行用 `=` 分隔，**不留行内注释**。

---

## 五、GitHub 状态

- **feat/wecom-integration**: `bddfdb2` → `2258425`（企微接入准备版，main 未动）
- **main**: `dac522a`（会话总结 8/9 存档）
- 本次部署包 `SellerAgent-deploy.zip` 已重建（45 文件，含最新代码）

---

## 六、待办 / 遗留

- [ ] **"当前账号异常"待排查**：大概率需企业完成**免费"验证"**（营业执照/法人扫脸，区别于付费认证），未验证企业客服功能受限
- [ ] 客服账号**接待人员激活**：企业微信 App 接受邀请
- [ ] 回调配置点**"完成/开始使用"**
- [ ] 生产上线：**企业认证**（300 元/年）+ **域名备案**（公司主体）→ 解锁客户上限
- [ ] 安全提醒：本次对话中暴露的密钥（DashScope / HF / 企微）建议测试后**轮换**

---

## 七、相关文档

- [[SESSION_SUMMARY_9]] — 微信客服方案调研与实现
- [[RAG_SYSTEM_ARCHITECTURE]] — RAG 检索架构
