# 配置说明

环境变量的完整清单。多数情况下只需要填 `.env` 里的必填项，其余保留默认值。

## 必填与核心

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TELEGRAM_CHANNEL_URL` | 无 | 完整频道链接，如 `https://telegram.me/s/your_channel`；域名可换成镜像域名，推荐使用 |
| `TELEGRAM_CHANNEL` | `your_channel` | （旧配置方式）仅频道名，域名固定为 `t.me`；未设置 `TELEGRAM_CHANNEL_URL` 时生效 |
| `RAYNEWS_PUBLIC_URL` | 无 | 对外访问地址；Compose 中为必填，用于邮件页脚等场景 |
| `TZ` | `Asia/Shanghai` | 容器时区与每日总结计划时区，使用 IANA 时区名 |
| `DATA_DIR` | `/app/data` | 持久化数据目录 |
| `RAYNEWS_SECRET` | 自动生成 | JWT 签名密钥；未设置时保存到 `/app/data/raynews_secret` |
| `RAYNEWS_TOKEN_EXPIRY_SECONDS` | `2592000` | 登录 Token 有效期，单位秒 |

## 邮件

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RESEND_API_KEY` | 空 | Resend API Key，用于邀请码、注册通知、测试邮件、每日总结和历史清理结果通知 |
| `RAYNEWS_ADMIN_EMAIL` | 首个管理员邮箱 | 接收邀请码申请、新用户注册和历史清理结果通知 |
| `RAYNEWS_FROM_EMAIL` | `onboarding@resend.dev` | 发件人；生产环境应使用 Resend 已验证域名 |

## AI 与后台任务

AI Endpoint、API Key、模型和供应商仅由管理员在「AI 服务 → 接口配置」中配置，不通过环境变量设置。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AI_REQUEST_TIMEOUT_SECONDS` | `300` | AI 请求超时，单位秒 |
| `AI_ALLOW_PRIVATE_ENDPOINTS` | 空（关闭） | 允许 AI 接口指向内网地址；默认只允许公网地址 |
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
| `SYSTEM_AI_FAILURE_ALERT_THRESHOLD` | `3` | 服务端 AI 连续失败多少次后给管理员发告警 |
| `SYSTEM_AI_RECOVERY_STABILITY_SECONDS` | `3600` | 故障后至少有一次真实成功，且最近失败后稳定达到该秒数，才发送恢复通知；设为 `0` 表示真实成功后无需额外稳定等待 |
| `DAILY_SUMMARY_RETRY_INTERVAL_SECONDS` | `600` | 每日总结生成失败后的重试间隔，单位秒 |
| `DAILY_SUMMARY_MAX_RETRIES` | `3` | 每日总结生成失败后的最大重试次数，用尽后告警管理员 |
| `DAILY_SUMMARY_HOUR` / `DAILY_SUMMARY_MINUTE` | `21` / `0` | 仅在页面尚未保存生成时间时作为默认值 |
| `TELEGRAM_EMBED_TIMEOUT_SECONDS` | `12` | 读取 Telegram 嵌入页面的超时 |

每日总结还有 `AI_DAILY_*` 高级调优变量。通常保留代码默认值即可；如需使用，请将变量显式加入 Compose 的 `environment`。

旧变量 `SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD` 已废弃；若仍设置，启动日志会提示改用 `SYSTEM_AI_RECOVERY_STABILITY_SECONDS`。

## 语音朗读

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TTS_WORKERS` | `2` | 同时生成的语音数量（1–8） |
| `TTS_CACHE_MAX_MB` | `256` | 临时音频容量上限（64–2048） |
| `TTS_REQUEST_TIMEOUT_SECONDS` | `300` | 单次合成超时 |

音频与未完成文件只保存在容器内的临时目录，1 小时后或超过容量上限自动清理，不进入 `/app/data`。

## 图片缓存

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

## 页面与网络

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CUSTOM_HEAD_HTML` | 空 | 注入页面 `<head>` 的受信任 HTML，例如统计脚本 |
| `CUSTOM_FOOTER_HTML` | 空 | 替换主页底部版本号和 GitHub 链接前方内容的受信任 HTML；支持 `<script>`，固定保留版本号与 GitHub 链接 |
| `HTTP_PROXY` | 空 | HTTP 代理 |
| `HTTPS_PROXY` | 空 | HTTPS 代理 |
| `NO_PROXY` | `localhost,127.0.0.1` | 不使用代理的地址 |
