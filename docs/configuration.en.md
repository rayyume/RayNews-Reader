# Configuration

The complete list of environment variables. In most deployments only the required values in `.env` matter; keep the rest at their defaults.

## Required and core

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_CHANNEL_URL` | none | Full channel URL such as `https://telegram.me/s/your_channel`; a mirror domain also works and is recommended |
| `TELEGRAM_CHANNEL` | `your_channel` | Legacy form: channel name only, host fixed to `t.me`; used when `TELEGRAM_CHANNEL_URL` is unset |
| `RAYNEWS_PUBLIC_URL` | none | Public site URL; required in Compose, used in email footers and similar |
| `TZ` | `Asia/Shanghai` | Container timezone and daily digest schedule timezone (IANA name) |
| `DATA_DIR` | `/app/data` | Persistence directory |
| `RAYNEWS_SECRET` | generated | JWT signing key; generated into `/app/data/raynews_secret` when unset |
| `RAYNEWS_TOKEN_EXPIRY_SECONDS` | `2592000` | Login token lifetime in seconds |

## Email

| Variable | Default | Description |
|----------|---------|-------------|
| `RESEND_API_KEY` | empty | Resend API key for invite codes, signup notices, test mail, daily digest and cleanup results |
| `RAYNEWS_ADMIN_EMAIL` | first admin's email | Receives invite requests, signup notices and cleanup results |
| `RAYNEWS_FROM_EMAIL` | `onboarding@resend.dev` | Sender address; use a verified Resend domain in production |

## AI and background jobs

The AI endpoint, key, model and provider are configured only by the administrator under **AI service → Endpoint settings**, not through environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_REQUEST_TIMEOUT_SECONDS` | `300` | AI request timeout in seconds |
| `AI_ALLOW_PRIVATE_ENDPOINTS` | empty (off) | Allow AI endpoints on private networks; by default only public addresses are permitted |
| `AI_SOURCE_CLASSIFY_MAX_TOKENS` | `2048` | Max output tokens for publisher classification; raise it if a reasoning model returns empty content |
| `AI_SUMMARY_MAX_TOKENS` | `4096` | Max output tokens for article summaries and event signals |
| `AI_TITLE_MAX_TOKENS` | `4096` | Max output tokens for title translation and shortening |
| `AUTO_SUMMARY_BATCH_LIMIT` | `20` | Articles summarized per round |
| `AUTO_SUMMARY_INTERVAL_SECONDS` | `30` | Summary polling interval in seconds |
| `AUTO_TRANSLATION_BATCH_LIMIT` | `5` | Articles translated per round |
| `AUTO_TRANSLATION_INTERVAL_SECONDS` | `30` | Translation polling interval in seconds |
| `AUTO_TITLE_PROCESS_BATCH_LIMIT` | `20` | Titles translated or shortened per round |
| `AUTO_TITLE_PROCESS_INTERVAL_SECONDS` | `10` | Title job interval in seconds |
| `AUTO_TITLE_PROCESS_SCAN_LIMIT` | `1000` | Maximum titles scanned per round |
| `TITLE_SUMMARY_MAX_CHARS` | `30` | Target length of an AI short title, in Chinese characters |
| `TITLE_SUMMARY_MAX_TOTAL_CHARS` | `40` | Weighted total length allowed for short titles |
| `AUTO_SOURCE_CLASSIFY_BATCH_LIMIT` | `50` | Publishers classified per round |
| `AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS` | `60` | Publisher classification interval in seconds |
| `SYSTEM_AI_FAILURE_ALERT_THRESHOLD` | `3` | Consecutive server-side AI failures before alerting administrators |
| `SYSTEM_AI_RECOVERY_STABILITY_SECONDS` | `3600` | Recovery notice requires at least one real success and this many seconds of stability after the last failure; `0` skips the extra wait |
| `DAILY_SUMMARY_RETRY_INTERVAL_SECONDS` | `600` | Retry interval after a failed digest, in seconds |
| `DAILY_SUMMARY_MAX_RETRIES` | `3` | Maximum digest retries before administrators are alerted |
| `DAILY_SUMMARY_HOUR` / `DAILY_SUMMARY_MINUTE` | `21` / `0` | Only used as the initial default before a time is saved in the UI |
| `TELEGRAM_EMBED_TIMEOUT_SECONDS` | `12` | Timeout when reading the Telegram embed page |

Daily digests have additional `AI_DAILY_*` tuning variables. Keep the code defaults unless you have a specific need; add them explicitly to the Compose `environment` block when you do.

The legacy variable `SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD` is deprecated; if set, the startup log points you to `SYSTEM_AI_RECOVERY_STABILITY_SECONDS`.

## Text-to-speech

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_WORKERS` | `2` | Simultaneous speech jobs (1–8) |
| `TTS_CACHE_MAX_MB` | `256` | Temporary audio size limit (64–2048) |
| `TTS_REQUEST_TIMEOUT_SECONDS` | `300` | Per-request synthesis timeout |

Audio and partial files stay in a temporary directory inside the container and are removed after one hour or when the size limit is reached; they never enter `/app/data`.

## Image cache

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAGE_CACHE_ENABLED` | `true` | Enable the server-side image cache |
| `IMAGE_CACHE_MAX_MB` | `5120` | Cache size limit for regular images, in MB |
| `IMAGE_CACHE_MAX_FILE_MB` | `10` | Maximum size of a single cached image, in MB |
| `IMAGE_CACHE_PREFETCH_BODY_LIMIT` | `3` | Body images prefetched for new articles |
| `IMAGE_CACHE_PREFETCH_WORKERS` | `2` | Prefetch worker count |
| `IMAGE_CACHE_PREFETCH_QUEUE_SIZE` | `3000` | Prefetch queue capacity |

Images of favorited articles are excluded from `IMAGE_CACHE_MAX_MB` eviction, so the directory may exceed that value. When an administrator deletes historical articles by date, favorited articles and their images are preserved; the job runs in batches with visible progress, and completion, partial failure and failure all trigger an email attempt to the initiating administrator. Deletion is irreversible — use the preview to confirm dates and counts first.

To clean up historical unreferenced image cache, run the orphan scan through the backend maintenance action; do not delete the `image_cache` directory or `cache.db` directly, or the files and the cache index will disagree.

## Page and network

| Variable | Default | Description |
|----------|---------|-------------|
| `CUSTOM_HEAD_HTML` | empty | Trusted HTML injected into `<head>`, e.g. analytics |
| `CUSTOM_FOOTER_HTML` | empty | Trusted HTML replacing the content before the version number and GitHub link in the footer; `<script>` allowed, version and link are always kept |
| `HTTP_PROXY` | empty | HTTP proxy |
| `HTTPS_PROXY` | empty | HTTPS proxy |
| `NO_PROXY` | `localhost,127.0.0.1` | Addresses that bypass the proxy |
