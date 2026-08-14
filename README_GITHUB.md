# 磁材竞社情报站 · 云端自动更新部署指南（GitHub Pages + Actions）

本指南帮助你把现有网站迁移到 **GitHub Pages + GitHub Actions**，实现：

- ✅ **网站读取数据完全在云端**（GitHub Pages 公网直链）
- ✅ **每天自动抓取+更新数据完全在云端运行**（GitHub Actions 定时任务）
- ✅ **不再依赖你本地电脑开机 / WorkBuddy 在线**

> 迁移后，原 CloudStudio 分享链接可以下线，访问地址变为你的 GitHub Pages 链接。

---

## 一、前置条件

1. 一个 **GitHub 账号**（免费）。
2. 一个 **免费且支持联网的 LLM 方案**（二选一，均免费、无需绑卡）：
   - **方案 A（国内免翻墙，推荐）**：`LLM_PROVIDER=cn-free`，需两个免费 Key：
     - **豆包搜索 API Key**（火山引擎控制台获取，每月 500 次免费联网搜索）→ `DOUBAO_SEARCH_API_KEY`
     - **智谱 GLM API Key**（`open.bigmodel.cn` 获取，`glm-4-flash` 永久免费）→ `ZHIPU_API_KEY`
   - **方案 B（单 Key，最省事）**：`LLM_PROVIDER=gemini`，需 **Gemini API Key**（`aistudio.google.com` 免费获取，gemini-2.5-flash 自带 Google 搜索联网）→ `GEMINI_API_KEY`。注意：需能访问 Google（国内注册可能需翻墙）。
   - （仍支持付费的 `openai` / `perplexity`，在 Secrets 设 `LLM_PROVIDER` 并填对应 Key 即可。）

---

## 二、部署步骤

### 步骤 1：在 GitHub 新建仓库

- 登录 GitHub → 右上角 **New repository**
- 仓库名随意（如 `magnet-intel`），**务必设为 Public**（免费版 GitHub Pages 需要公开仓库）
- 不要勾选 "A dd a README"（我们已有文件），直接 Create repository

### 步骤 2：上传本目录所有文件到仓库

两种方式任选其一：

**方式 A（最简单，无需本地 git）：GitHub 网页上传**

- 进入刚建的空仓库 → 点击 **Add file → Upload files**
- 把本目录（`magnet-companies`）下的 **全部内容** 拖进去上传，确保包含：
  - `index.html`
  - `data/intelligence.json`
  - `scripts/update_intel.py`、`scripts/requirements.txt`
  - `.github/workflows/daily-update.yml`
  - `.gitignore`、`README_GITHUB.md`
- 提交（Commit）

**方式 B（本地 git push）：**

```bash
cd magnet-companies
git init
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git add .
git commit -m "init magnet intel site"
git branch -M main
git push -u origin main
```

### 步骤 3：启用 GitHub Pages

- 仓库 → **Settings → Pages**
- Source 选择 **Deploy from a branch**
- Branch 选 **main**，目录选 **/ (root)**
- 保存后，稍等 1~2 分钟，访问地址为：
  ```
  https://<你的用户名>.github.io/<仓库名>/
  ```

### 步骤 4：配置 Secrets（LLM API Key）

- 仓库 → **Settings → Secrets and variables → Actions → New repository secret**
- 添加以下 Secret（选一种 Provider，填对应的 Key 即可）：

**若用方案 A（cn-free，国内免翻墙，推荐）：**

| Name                    | 说明                                        | 获取地址 |
| ----------------------- | ----------------------------------------- | -------- |
| `LLM_PROVIDER`          | 填 `cn-free`                                | —        |
| `DOUBAO_SEARCH_API_KEY` | 豆包搜索 API Key（每月 500 次免费）              | 火山引擎控制台 → 豆包搜索 → 创建 API Key |
| `ZHIPU_API_KEY`         | 智谱 API Key（`glm-4-flash` 永久免费）          | open.bigmodel.cn → 控制台 → API 密钥 |

**若用方案 B（gemini，单 Key）：**

| Name            | 说明                              | 获取地址 |
| --------------- | ------------------------------- | -------- |
| `LLM_PROVIDER`  | 填 `gemini`                       | —        |
| `GEMINI_API_KEY` | Gemini API Key（免费，自带联网）    | aistudio.google.com → Get API Key |

> Secrets 中 `LLM_PROVIDER` 留空时，workflow 默认使用 `cn-free`。

### 步骤 5：启用 Actions

- 仓库 → **Actions** 标签
- 若提示 "Workflows aren't enabled"，点击 **I understand... enable** 启用
- 找到 **Daily Magnet Intel Update** workflow

### 步骤 6：手动触发一次测试

- 在 Actions 页面，进入 **Daily Magnet Intel Update** → **Run workflow**
- 观察运行日志：应能看到 LLM 调用、写入 `data/intelligence.json`、自动 commit & push
- 约 1 分钟后，刷新 GitHub Pages 页面，确认数据日期已更新为当天

---

## 三、日常使用

- **访问**：直接打开 GitHub Pages 链接即可，任何设备、任何时间都可访问，与你电脑无关。
- **更新**：每天 **北京时间 12:00** 由 GitHub Actions 自动运行（UTC 04:00），无需你任何操作。
- **手动更新**：随时可在 Actions 页面手动 Run workflow。
- **下线旧链接**：原 CloudStudio 分享链接可在 WorkBuddy「设置 - 数据管理 - 我发布的应用」中下线。

---

## 四、工作原理

```
GitHub Actions (云端, 每天12:00)
   │  运行 scripts/update_intel.py
   │   ├─ 读取仓库中的 data/intelligence.json（保留历史结构）
   │   ├─ 调用 LLM（联网 web_search）抓取最新稀土价格 + 竞社情报
   │   └─ 生成完整 JSON 写回 data/intelligence.json
   ▼
git commit & push 到 main 分支
   ▼
GitHub Pages 自动用最新文件  →  网站 fetch 同源 data/intelligence.json
```

- 网站 `index.html` 优先 `fetch('data/intelligence.json')`（同源），失败时回退到内嵌兜底数据。
- `update_intel.py` 对关键数组字段做缺失保护：LLM 返回异常时**不会破坏现有历史数据**。

---

## 五、费用与说明

- **GitHub Actions / Pages**：公开仓库免费（每月额度远超本任务所需）。
- **LLM API**：按调用量计费（每次更新约 1 次对话），量很小；如介意可在 Secrets 中调整模型或频率。
- **兜底**：`index.html` 内嵌了最近一次数据，极端情况下 JSON 加载失败也不会白屏。
- **过渡方案**：若暂时不想配置 LLM Key，可保留原 WorkBuddy 本地自动化，把它改为把更新后的 `data/intelligence.json` **push 到本仓库**（而非部署 CloudStudio），这样"读取"已云端化，仅"抓取"仍本地跑。

---

## 六、可调项

- **更新时间**：编辑 `.github/workflows/daily-update.yml` 的 `cron: '0 4 * * *'`（UTC）。北京时间 = UTC + 8，故 `0 4 * * *` = 北京 12:00。
- **模型**：在 Secrets 设 `LLM_MODEL`。
- **Provider 切换**：在 Secrets 设 `LLM_PROVIDER` 为 `cn-free`（默认，国内免翻墙）、`gemini`（单 Key 免费）、`openai` 或 `perplexity`（付费）。
