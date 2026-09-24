<p align="center">
  <b>🇨🇳 中文</b> | <a href="README.en.md">🇺🇸 English</a>
</p>

# RayNews 📡 🤖

RayNews 是一个以 Telegram 公开频道为数据入口的自托管新闻聚合阅读器。它可以增量抓取频道消息、提取 Telegraph 等文章全文，并在条件允许时尝试抓取微信公众号文章；同时提供 AI 摘要、翻译、每日总结、订阅源管理、收藏和图片持久缓存。

前端为响应式 PWA，支持跟随系统、明色和暗色三种主题。

**示例网站：** [https://news.rayyu.me](https://news.rayyu.me)

## 页面预览

<table>
  <tr>
    <th width="72%">桌面端</th>
    <th width="28%">移动端 PWA</th>
  </tr>
  <tr>
    <td align="center" valign="top">
      <img src="https://img.rayyu.me/file/1781248235309_PC.png" alt="RayNews 桌面端页面" width="100%">
    </td>
    <td align="center" valign="top">
      <img src="https://img.rayyu.me/file/1781248235931_PWA.jpg" alt="RayNews 移动端 PWA 页面" width="100%">
    </td>
  </tr>
</table>

## 主要功能

### 阅读与整理

- 从 Telegram 公开频道每 15 分钟增量抓取新闻，也支持手动刷新。首次运行仅导入北京时间当天的消息；后续按消息 ID 增量抓取，保留中断期间的跨日消息，每轮最多 200 页；补抓文章按入库时间进入日报窗口
- 提取 Telegraph 等网页全文；消息正文过短且含微信文章链接时尽力抓取微信全文，可能受源站限制
- 按媒体来源和可配置分类筛选文章；Telegram/RSS 入口与实际发布媒体分别记录
- 搜索文章标题、来源和摘要
- 收藏文章并在多设备登录后同步
- 管理员可识别、分类、合并和删除订阅源及历史文章；“资源占用”页可查看实时资源与存储明细
- 媒体按发布域名归组：内置域名映射可在管理页覆盖，未知媒体按注册域名稳定归组；旧文章会在升级后自动分批回填
- 来源分类与媒体身份相互独立；未登记来源显示为“待分类”，AI 仅辅助分类，高置信度建议自动应用，低置信度建议由管理员确认
- 删除记录写入 tombstone，避免文章在后续刷新时重新出现

### AI 能力

RayNews 提供“用户端 AI”和“服务端 AI”两套独立能力；两者的 API Key、执行位置和费用归属不同。

| 对比项 | 用户端 AI | 服务端 AI |
|--------|-----------|-----------|
| 配置位置 | 用户设置 → AI | 管理员设置 → 服务端 API |
| 配置权限 | 普通用户、管理员 | 仅管理员 |
| 支持协议 | OpenAI 兼容、Claude/Anthropic | OpenAI 兼容、Claude/Anthropic |
| API Key 与调用费用 | 使用当前用户自己的 Key；手动摘要/翻译由浏览器直接调用服务商 | 使用管理员配置的系统 Key；由服务器后台调用 |
| 主要用途 | 手动摘要、手动翻译、按需生成每日总结 | 自动摘要、自动翻译标题/正文、自动短标题、全局每日总结、订阅源 AI 分类 |
| 结果范围 | 可选择共享摘要、翻译和标题结果；共享前需验证自己的 AI 连通性 | 生成结果写入全局缓存，所有用户可复用 |

**用户端 AI：** 在“用户设置 → AI”配置并启用自己的 Endpoint、模型、协议和 Key 后，可在文章详情中手动生成摘要或翻译；也可按需生成每日总结。浏览器会将 Key 用于直接请求所选 AI 服务商，因此请只在受信任设备上配置。生成的结果会保存到 RayNews；开启“共享 AI 结果”前会进行连通性验证，之后默认每小时复核一次（`AI_SHARE_REVALIDATION_INTERVAL_HOURS`，支持小数，最短 5 分钟）。一次定时复核失败会被容忍；连续两次定时复核失败才会暂停共享并发送站内通知和邮件。用户主动发起的校验失败会立即暂停共享；后续校验成功会清除失败计数，并自动恢复此前暂停的共享及发送恢复通知。

**服务端 AI：** 管理员在“管理员设置 → 服务端 API”配置系统 Endpoint、模型、协议和 Key，并在摘要/翻译设置中开启对应后台任务。系统会以小批次处理新文章的自动摘要、英文标题或正文翻译、过长标题简写；系统每日总结使用同一系统 AI 生成一次，再向开启每日总结邮件的用户发送。服务端 AI 的 Key 不会提供给普通用户浏览器。

AI 结果会写入数据库以减少重复调用。管理员应根据所选服务商的隐私政策、配额和计费规则决定是否开启自动任务。

### 图片缓存

- 文章图片统一经过服务端缓存，降低历史图片失效和防盗链影响
- 新文章抓取后后台预热封面和正文前几张图片
- 普通图片按缓存容量自动清理，默认上限为 `5120 MB`
- 收藏文章的全部图片会被标记为永久保护，不参与普通缓存清理
- 图片缓存保存在 `/app/data/image_cache`

### 用户与通知

- 第一个注册账号自动成为管理员
- 后续用户需要邀请码注册，初始角色为普通用户
- 管理员可以管理用户角色、订阅源及全局文章删除
- 支持通过 Resend 发送邀请码、注册成功通知、定时每日摘要和历史清理结果邮件
- 每日摘要由服务端在北京时间每天 21:00 用管理员配置的服务端 API 统一生成一次，不支持手动触发；
  每次统计前一日 21:00 至当日 21:00 入库的新闻，21:00 后获取的新闻进入下一次摘要。
  服务端先从单篇摘要提取事件线索，按具体事件合并、根据事件影响和独立来源数排序；
  每天选出相对最重要的最多 60 个不同事件，低于绝对重要性阈值时仍按相对排名补足；
  栏目沿用管理员配置的来源分类。管理员可通过 `GET /admin/daily-digest/events?date=YYYY-MM-DD`
  查看每个事件的分数和入选原因。
  生成后默认推送到每位用户的站内通知（头像菜单 → 我的通知），邮件推送另需在 用户设置 → 通知 中单独开启
- 服务端 AI 连续 3 次调用失败（自动摘要／翻译／标题精简／订阅源分类／每日摘要合并计数）时，
  给所有管理员发送一次邮件和站内通知（含失败原因和受影响任务）；恢复后再发一次恢复通知。
  阈值可用 `SYSTEM_AI_FAILURE_ALERT_THRESHOLD` 调整
- 开启了自动 AI 任务但服务端 API 被清空或关闭时，后台任务无法运行且不会产生调用，此状态本身同样计入上面的连续失败计数，约 1 分钟内即可告警（不额外发送任何探测请求）
- 服务端与用户端的 AI 故障都只在状态翻转时通知：一次故障只发一条告警、恢复时再发一条，期间持续失败不会重复打扰；服务端的「已告警」标记落库，容器重启也不会重复发送
- 每日摘要生成失败后每 10 分钟自动重试一次，重试 3 次仍失败则停止重试，并给所有管理员发送
  邮件和站内通知（含失败原因）；管理员可在首页 ✨ 每日摘要面板看到失败原因并点击「重试生成」手动重试
  （该按钮仅在失败后对管理员显示）

## 架构

跨模块的设计理由与安全边界见[当前架构说明](docs/architecture.md)。

```text
新闻源 (RSS / 网页 / API)
          │
          ▼
RSS-to-Telegram-Bot ──推送──▶ Telegram 公开频道
                                      │
                                      ▼
                              RayNews Fetcher
                                      │
                     ┌────────────────┴────────────────┐
                     ▼                                 ▼
              news.db / news.json              图片缓存目录
                     │                                 │
                     └────────────────┬────────────────┘
                                      ▼
             Nginx ──▶ refresh_server.py + web_server.py
                                      │
                                      ▼
                              原生 JavaScript SPA
```

- `refresh_server.py`：多线程文章 API、刷新任务、文章详情和图片缓存
- `web_server.py`：登录、用户、AI、收藏、设置、邮件和订阅源管理
- `Nginx`：静态文件、SPA 路由和后端反向代理
- `SQLite`：文章、用户、AI 结果、设置、收藏及删除记录

> RayNews 只读取 Telegram 频道，不负责把 RSS 内容推送到频道。你可以使用 [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)、其他机器人或手工发消息。

## 快速开始

### 1. 前置条件

- Docker 和 Docker Compose
- 一个 Telegram 公开频道
- 可选：用于向频道推送新闻的 [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)
- 可选：AI API 和 Resend 邮件 API

### 2. 克隆项目

```bash
git clone https://github.com/rayyume/RayNews-Reader.git
cd RayNews-Reader
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

至少在 `.env` 中填写：

```dotenv
TELEGRAM_CHANNEL_URL=https://telegram.me/s/your_public_channel
RAYNEWS_PUBLIC_URL=https://news.example.com
```

`RAYNEWS_PUBLIC_URL` 是必填项，用于邮件页脚等对外站点链接。不要保留示例域名。

如需邮件功能，再配置：

```dotenv
RESEND_API_KEY=re_xxxxxxxxx
RAYNEWS_ADMIN_EMAIL=admin@example.com
RAYNEWS_FROM_EMAIL=news@example.com
```

### 4. 启动

```bash
docker compose up -d
```

访问 `http://<服务器地址>:8090`。容器会先启动服务，立即读取并提供持久化目录中的已有内容，同时在后台执行首次抓取；之后每 15 分钟自动刷新。全新部署在首次抓取完成前可能暂无文章，但 Web 和 API 不会等待抓取完成才启动。

第一个成功注册的账号会成为管理员。管理员邮箱建议与 `RAYNEWS_ADMIN_EMAIL` 保持一致。

## 镜像与更新

仓库中的 `docker-compose.yml` 默认使用 `build: .` 构建当前代码。

- 稳定版：注释 `build: .`，启用 `image: ghcr.io/rayyume/raynews:latest`
- 开发测试版：使用 `image: ghcr.io/rayyume/raynews:dev`

更新预构建镜像：

```bash
docker compose pull
docker compose up -d
```

如果使用 Watchtower，请确认容器由 `ghcr.io/rayyume/raynews` 镜像创建，而不是 Compose 自动生成的本地镜像名。

## 数据持久化

Compose 默认挂载：

```yaml
volumes:
  - ./data:/app/data
```

`/app/data` 中包含文章数据库、用户和 AI 设置、登录密钥、头像、图片缓存及抓取状态。升级或重建容器时必须保留整个目录。

## 环境变量

### 必填与核心配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TELEGRAM_CHANNEL_URL` | 无 | 完整频道链接，如 `https://telegram.me/s/your_channel`；域名可换成镜像域名，推荐使用 |
| `TELEGRAM_CHANNEL` | `your_channel` | （旧配置方式）仅频道名，域名固定为 `t.me`；未设置 `TELEGRAM_CHANNEL_URL` 时生效 |
| `RAYNEWS_PUBLIC_URL` | 无 | 对外访问地址；Compose 中为必填，用于邮件页脚等场景 |
| `TZ` | `Asia/Shanghai` | 容器时区 |
| `DATA_DIR` | `/app/data` | 持久化数据目录 |
| `RAYNEWS_SECRET` | 自动生成 | JWT 签名密钥；未设置时保存到 `/app/data/raynews_secret` |
| `RAYNEWS_TOKEN_EXPIRY_SECONDS` | `2592000` | 登录 Token 有效期，单位秒 |

### 邮件配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RESEND_API_KEY` | 空 | Resend API Key，用于邀请码、注册通知、测试邮件、每日摘要和历史清理结果通知 |
| `RAYNEWS_ADMIN_EMAIL` | 首个管理员邮箱 | 接收邀请码申请、新用户注册和历史清理结果通知 |
| `RAYNEWS_FROM_EMAIL` | `onboarding@resend.dev` | 发件人；生产环境应使用 Resend 已验证域名 |

### AI 与后台任务

AI Endpoint、API Key、模型和供应商由用户在网页的“设置 → AI”中配置，不通过 Compose 共用。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AI_REQUEST_TIMEOUT_SECONDS` | `300` | AI 请求超时，单位秒 |
| `AI_SOURCE_CLASSIFY_MAX_TOKENS` | `2048` | 订阅源分类的最大输出 token；推理模型返回空内容时可适当增大 |
| `AI_SUMMARY_MAX_TOKENS` | `4096` | 单篇文章自动摘要与事件信号的最大输出 token |
| `AI_TITLE_MAX_TOKENS` | `4096` | 标题翻译和精简的最大输出 token |
| `AUTO_SUMMARY_BATCH_LIMIT` | `20` | 每轮自动生成文章摘要的文章数 |
| `AUTO_SUMMARY_INTERVAL_SECONDS` | `30` | 自动摘要轮询间隔，单位秒 |
| `AUTO_TRANSLATION_BATCH_LIMIT` | `5` | 每轮后台翻译文章数 |
| `AUTO_TRANSLATION_INTERVAL_SECONDS` | `30` | 后台翻译轮询间隔，单位秒 |
| `AUTO_TITLE_PROCESS_BATCH_LIMIT` | `20` | 每轮标题翻译或简写数量 |
| `AUTO_TITLE_PROCESS_INTERVAL_SECONDS` | `10` | 标题后台处理间隔，单位秒 |
| `AUTO_TITLE_PROCESS_SCAN_LIMIT` | `1000` | 每轮标题任务最大扫描数 |
| `TITLE_SUMMARY_MAX_CHARS` | `30` | AI 短标题目标中文字符数 |
| `TITLE_SUMMARY_MAX_TOTAL_CHARS` | `40` | 短标题允许的加权总长度 |
| `AUTO_SOURCE_CLASSIFY_BATCH_LIMIT` | `50` | 每轮用服务端 API 分类的订阅源数量 |
| `AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS` | `60` | 订阅源分类轮询间隔，单位秒 |
| `AI_SHARE_REVALIDATION_INTERVAL_HOURS` | `1` | 复核开启共享用户的个人 AI 连通性的间隔，支持小数，最短 5 分钟 |
| `SYSTEM_AI_FAILURE_ALERT_THRESHOLD` | `3` | 服务端 AI 连续失败多少次后给管理员发告警 |
| `SYSTEM_AI_RECOVERY_STABILITY_SECONDS` | `3600` | 故障后至少有一次真实成功，且最近失败后稳定达到该秒数，才发送恢复通知；设为 `0` 表示真实成功后无需额外稳定等待 |
| `DAILY_SUMMARY_RETRY_INTERVAL_SECONDS` | `600` | 每日摘要生成失败后的重试间隔，单位秒 |
| `DAILY_SUMMARY_MAX_RETRIES` | `3` | 每日摘要生成失败后的最大重试次数，用尽后告警管理员 |
| `TELEGRAM_EMBED_TIMEOUT_SECONDS` | `12` | 读取 Telegram 嵌入页面的超时 |

每日总结还有 `AI_DAILY_*` 高级调优变量。通常保留代码默认值即可；如需使用，请将变量显式加入 Compose 的 `environment`。

旧变量 `SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD` 已废弃且不会恢复按调用次数判断健康的路径；若仍设置，启动日志会提示改用 `SYSTEM_AI_RECOVERY_STABILITY_SECONDS`。

### 图片缓存

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `IMAGE_CACHE_ENABLED` | `true` | 启用服务端图片缓存 |
| `IMAGE_CACHE_MAX_MB` | `5120` | 普通图片缓存容量上限，单位 MB |
| `IMAGE_CACHE_MAX_FILE_MB` | `10` | 单张图片最大缓存大小，单位 MB |
| `IMAGE_CACHE_PREFETCH_BODY_LIMIT` | `3` | 新文章后台预热的正文图片数量 |
| `IMAGE_CACHE_PREFETCH_WORKERS` | `2` | 图片预热 worker 数量 |
| `IMAGE_CACHE_PREFETCH_QUEUE_SIZE` | `3000` | 图片预热队列容量 |

收藏文章图片不计入 `IMAGE_CACHE_MAX_MB` 的自动清理范围，因此实际目录占用可能超过该值。管理员按日期清理历史文章时会保留收藏文章及其图片缓存；清理任务在后台分批执行，页面会显示进度，且完成、部分失败或失败时都会尝试邮件通知发起管理员。清理不可恢复，请先使用预览确认日期和数量。

如需处理历史遗留的无引用图片缓存，请通过后端维护操作执行孤儿缓存扫描；不要直接删除 `image_cache` 目录或 `cache.db`，以免文件与缓存索引不一致。

### 页面与网络

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CUSTOM_HEAD_HTML` | 空 | 注入页面 `<head>` 的受信任 HTML，例如统计脚本 |
| `CUSTOM_FOOTER_HTML` | 空 | 替换主页底部版本号和 GitHub 链接前方内容的受信任 HTML；支持 `<script>`，固定保留版本号与 GitHub 链接 |
| `HTTP_PROXY` | 空 | HTTP 代理 |
| `HTTPS_PROXY` | 空 | HTTPS 代理 |
| `NO_PROXY` | `localhost,127.0.0.1` | 不使用代理的地址 |

## 权限说明

当前项目仅有 **普通用户（user）** 和 **管理员（admin）** 两种登录角色；旧版“预览用户”会在数据库迁移时自动升级为普通用户。

| 能力 | 未登录 | 普通用户 | 管理员 |
|------|--------|----------|--------|
| 浏览与阅读文章 | 仅首页前 3 篇；其余内容锁定 | ✓ | ✓ |
| 搜索和翻页 | — | ✓ | ✓ |
| 手动刷新 | — | ✓ | ✓ |
| 收藏、个人 AI 配置与 AI 操作 | — | ✓ | ✓ |
| 个人资料、通知与分享设置 | — | ✓ | ✓ |
| 管理订阅源、全局 AI 设置与资源占用 | — | — | ✓ |
| 清理订阅源和历史文章 | — | — | ✓ |
| 用户、角色、邀请码与邀请审核 | — | — | ✓ |

订阅源标签和分类是全局设置，所有用户看到的结果以管理员维护的数据为准。接口鉴权会以数据库中的当前角色为准，不信任旧 JWT 中的角色声明。

## 常用接口

- 健康检查：`GET /health`
- 手动刷新：普通用户或管理员登录后使用页面刷新按钮，也可调用需要认证的 `POST /auth/refresh`。请求会立即返回后台任务状态，不等待抓取完成；任务运行期间可继续阅读。页面通过受保护的 `GET /auth/refresh/status` 查询进度，完成后提示刷新结果和新增文章数量（如有）。
- Logo：只用于轻量地返回“全部”第一页、滚动到顶部并检查新文章；不会触发服务端全量抓取。
- 文章列表：`GET /api/news`
- 图片缓存：`GET /img-cache?url=<encoded-url>`

## 路线图

### 短期

- [x] **订阅源分类** — 按来源/标签对文章进行分组和筛选
- [x] **微信公众号文章尽力抓取** — 符合条件时尝试提取全文；源站拦截时保留原消息内容
- [x] **新闻收藏夹** — 文章详情页增加收藏功能，并新增收藏夹界面
- [x] **英文标题及文章自动翻译** — 自动将英文内容翻译为中文
- [x] **自定义 AI API** — 接入自定义 AI API，支持文章摘要和每日综述
- [ ] **自定义可见订阅源** — 支持用户自定义可见订阅源，建立订阅源市场
- [ ] **关注订阅源新文章通知** — 支持关注订阅源，抓取到新文章时推送通知（PWA 应用）

### 长期

- [ ] **集成 [RSStT](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)** — 用户无需额外部署 RSStT 项目，支持开箱即用
- [ ] **关键词过滤** — 增加文章关键词过滤功能，不显示含有特定关键词的文章
- [ ] **新闻 Podcast 生成** — 自动生成新闻 Podcast，听新闻
- [ ] **iOS 客户端** — 原生 iOS 应用

## 技术栈

- Python 3.12
- Flask + Python `ThreadingHTTPServer`
- Nginx
- SQLite
- 原生 HTML / CSS / JavaScript PWA
- BeautifulSoup

## License

MIT
