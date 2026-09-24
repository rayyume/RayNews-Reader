<p align="center">
  <a href="README.md">🇨🇳 中文</a> | <b>🇺🇸 English</b>
</p>

# RayNews 📡 🤖

RayNews is a self-hosted news aggregator that uses a public Telegram channel as its content entry point. It incrementally fetches channel messages, extracts full articles from Telegraph and other pages, and attempts WeChat article extraction when the source is accessible. It also provides AI summaries, translation, daily digests, source management, favorites, and persistent image caching.

The responsive PWA supports system, light, and dark themes.

**Demo:** [https://news.rayyu.me](https://news.rayyu.me)

## Preview

<table>
  <tr>
    <th width="72%">Desktop</th>
    <th width="28%">Mobile PWA</th>
  </tr>
  <tr>
    <td align="center" valign="top">
      <img src="https://img.rayyu.me/file/1781248235309_PC.png" alt="RayNews desktop interface" width="100%">
    </td>
    <td align="center" valign="top">
      <img src="https://img.rayyu.me/file/1781248235931_PWA.jpg" alt="RayNews mobile PWA interface" width="100%">
    </td>
  </tr>
</table>

## Features

### Reading and organization

- Incremental refresh from a public Telegram channel every 15 minutes, plus manual refresh. The first run imports only today's messages in Beijing time; later runs use message IDs to recover cross-day arrivals, up to 200 pages per run. Recovered articles enter digest windows by ingestion time
- Full-article extraction for Telegraph and other pages; best-effort WeChat extraction when a short message contains an article link, subject to source-site access
- Article filtering by publisher and configurable categories; the Telegram/RSS intake feed is stored separately from the publisher
- Search across article titles, sources, and summaries
- Account-based favorites synchronized across devices
- Admin tools for source detection, categorization, merging, and article deletion; the **Resource Usage** page shows live resource and storage details
- Publishers are grouped by publication domain: built-in mappings can be overridden in the admin UI, and unknown publishers use their registrable domain; existing articles are backfilled automatically in resumable batches after an upgrade
- Source categories are independent of publisher identity. Unregistered sources appear as Uncategorized; AI only assists with category suggestions, applying high-confidence results automatically and leaving lower-confidence suggestions for admin review
- Deletion tombstones prevent removed articles from returning after a later refresh

### AI

RayNews has two independent AI paths: **user AI** and **server AI**. They use different API keys, execution locations, and billing owners.

| Aspect | User AI | Server AI |
|--------|---------|-----------|
| Configuration | **Settings → AI** | **Admin Settings → Server API** |
| Who can configure it | User or Admin | Admin only |
| Supported protocols | OpenAI-compatible and Claude/Anthropic | OpenAI-compatible and Claude/Anthropic |
| API key and billing | The current user's own key; manual summary/translation calls the provider directly from the browser | The admin-managed system key; calls run in server background jobs |
| Main uses | Manual summary, manual translation, on-demand daily digest | Automatic summaries, title/body translation, title shortening, global daily digest, and AI source classification |
| Result scope | The user can opt in to sharing summary, translation, and title results after a connectivity check | Results are stored in the shared cache for every user to reuse |

**User AI:** Configure and enable your own endpoint, model, protocol, and key under **Settings → AI**. You can manually summarize or translate an article and request an on-demand daily digest. The browser uses the key to call your selected provider directly, so configure it only on trusted devices. Generated results are saved in RayNews; enabling **Share AI results** requires a live connectivity check and is revalidated hourly by default (`AI_SHARE_REVALIDATION_INTERVAL_HOURS`, fractions allowed, floored at 5 minutes). One scheduled revalidation failure is tolerated; two consecutive scheduled failures suspend sharing and notify that user in-app and by email. A user-initiated validation failure suspends sharing immediately. A later successful validation clears the failure streak, restores a suspended share, and sends the recovery notification.

**Server AI:** An administrator configures the system endpoint, model, protocol, and key under **Admin Settings → Server API**, then enables the needed background jobs under the summary and translation settings. The server processes new articles in small batches for automatic summaries, English title/body translation, and long-title shortening. The same system AI generates the global daily digest once before it is emailed to subscribed users. The server AI key is never sent to regular-user browsers.

AI results are stored in the database to avoid duplicate calls. Administrators should evaluate their selected provider's privacy policy, quota, and billing before enabling automatic jobs.

### Image cache

- Article images are served through the RayNews cache to reduce broken historical images and hotlink failures
- New articles prefetch their cover and the first few body images in the background
- Normal images are evicted according to a configurable cache limit, defaulting to `5120 MB`
- All images belonging to a favorited article are pinned and excluded from normal eviction
- Cached files are stored in `/app/data/image_cache`

### Users and email

- The first registered account becomes the administrator
- Later registrations require an invitation code and start with the User role
- Administrators manage user roles, global sources, and global article deletion
- Resend can deliver invitation codes, registration notices, test messages, scheduled daily digests, and historical-purge result emails
- The daily digest is generated server-side exactly once per day at 21:00 Beijing time using the admin-configured server API; it cannot be triggered manually.
  Each run covers news ingested from the previous 21:00 through the current 21:00; later arrivals enter the next digest.
  Article summaries yield event signals, which are grouped into concrete events and ranked by impact and independent publisher coverage.
  Up to 60 events appear under the administrator's source categories. Administrators can inspect scores and selection reasons at
  `GET /admin/daily-digest/events?date=YYYY-MM-DD`.
  Every user gets an in-app copy by default (avatar menu -> My Notifications); the email copy is opted into separately under Settings -> Notifications
- After 3 consecutive system-AI call failures (counted across auto summary/translation/title/source classification and the
  daily digest), every admin gets one email + in-app alert naming the affected jobs and the reason; recovery sends one more.
  Tune with `SYSTEM_AI_FAILURE_ALERT_THRESHOLD`
- If an auto AI job is enabled while the server API config is cleared or disabled, nothing calls the provider at all, so
  that state is itself counted toward the same streak — the alert lands within about a minute, with no probe requests
- Both the server-side and user-side AI alerts are edge-triggered: one alert per outage, one notice on recovery, nothing
  in between. The server-side "already alerted" flag is persisted, so a container restart mid-outage does not re-send it
- A failed generation is retried every 10 minutes; after 3 failed retries the scheduler stops for the day and alerts every admin
  by email and in-app notification with the reason. Admins then see the reason and a "retry" button on the home page ✨ daily
  digest panel (the button appears only after a failure, and only for admins)

## Architecture

See the [current architecture notes](docs/architecture.md) for cross-component design and security boundaries.

```text
News sources (RSS / Web / API)
          │
          ▼
RSS-to-Telegram-Bot ──push──▶ Public Telegram channel
                                      │
                                      ▼
                              RayNews Fetcher
                                      │
                     ┌────────────────┴────────────────┐
                     ▼                                 ▼
              news.db / news.json              Image cache
                     │                                 │
                     └────────────────┬────────────────┘
                                      ▼
             Nginx ──▶ refresh_server.py + web_server.py
                                      │
                                      ▼
                              Vanilla JavaScript SPA
```

- `refresh_server.py`: threaded article API, refresh jobs, article details, and image caching
- `web_server.py`: authentication, users, AI, favorites, settings, email, and source management
- `Nginx`: static files, SPA routing, and reverse proxy
- `SQLite`: articles, users, AI results, settings, favorites, and deletion records

> RayNews reads from Telegram but does not push RSS content into the channel. Use [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot), another bot, or manual channel posts.

## Quick Start

### 1. Prerequisites

- Docker and Docker Compose
- A public Telegram channel
- Optional: [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) to populate the channel
- Optional: an AI API and a Resend account

### 2. Clone

```bash
git clone https://github.com/rayyume/RayNews-Reader.git
cd RayNews-Reader
```

### 3. Configure

```bash
cp .env.example .env
```

At minimum, set:

```dotenv
TELEGRAM_CHANNEL_URL=https://telegram.me/s/your_public_channel
RAYNEWS_PUBLIC_URL=https://news.example.com
```

`RAYNEWS_PUBLIC_URL` is required for public links such as the email footer. Replace the example domain.

To enable email features, also configure:

```dotenv
RESEND_API_KEY=re_xxxxxxxxx
RAYNEWS_ADMIN_EMAIL=admin@example.com
RAYNEWS_FROM_EMAIL=news@example.com
```

### 4. Start

```bash
docker compose up -d
```

Open `http://<server-address>:8090`. The container starts serving persisted content immediately, while its initial fetch runs in the background; it then refreshes every 15 minutes. A new deployment may have no articles until that first fetch completes, but the Web and API services do not wait for it before starting.

The first successful registration becomes the administrator. It is recommended to use the same address as `RAYNEWS_ADMIN_EMAIL`.

## Container Images and Updates

The repository's `docker-compose.yml` uses `build: .` by default.

- Stable image: comment out `build: .` and enable `image: ghcr.io/rayyume/raynews:latest`
- Development image: use `image: ghcr.io/rayyume/raynews:dev`

Update a pre-built deployment with:

```bash
docker compose pull
docker compose up -d
```

When using Watchtower, make sure the running container was created from `ghcr.io/rayyume/raynews`, not from a Compose-generated local image name.

## Persistence

The default Compose mount is:

```yaml
volumes:
  - ./data:/app/data
```

`/app/data` contains article databases, users and AI settings, the login secret, avatars, cached images, and fetch state. Preserve the complete directory when rebuilding or upgrading the container.

## Environment Variables

### Required and core

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_CHANNEL_URL` | none | Full channel URL, e.g. `https://telegram.me/s/your_channel`; domain can be swapped for a mirror, recommended |
| `TELEGRAM_CHANNEL` | `your_channel` | (Legacy) channel name only, domain fixed to `t.me`; used when `TELEGRAM_CHANNEL_URL` is unset |
| `RAYNEWS_PUBLIC_URL` | none | Required public URL used for public links such as the email footer |
| `TZ` | `Asia/Shanghai` | Container timezone |
| `DATA_DIR` | `/app/data` | Persistent data directory |
| `RAYNEWS_SECRET` | generated | JWT signing secret; saved to `/app/data/raynews_secret` when omitted |
| `RAYNEWS_TOKEN_EXPIRY_SECONDS` | `2592000` | Login token lifetime in seconds |

### Email

| Variable | Default | Description |
|----------|---------|-------------|
| `RESEND_API_KEY` | empty | Resend key for invitations, registration notices, test email, daily digests, and historical-purge result emails |
| `RAYNEWS_ADMIN_EMAIL` | first admin email | Receives invitation requests, new-registration notices, and historical-purge results |
| `RAYNEWS_FROM_EMAIL` | `onboarding@resend.dev` | Sender address; use a verified Resend domain in production |

### AI and background jobs

AI endpoints, keys, models, and providers are configured per user under **Settings → AI**. They are not shared through Docker Compose.

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_REQUEST_TIMEOUT_SECONDS` | `300` | AI request timeout in seconds |
| `AI_SOURCE_CLASSIFY_MAX_TOKENS` | `2048` | Maximum source-classification output tokens; increase for reasoning models that return empty content |
| `AI_TITLE_MAX_TOKENS` | `4096` | Maximum title translation/shortening output tokens |
| `AUTO_SUMMARY_BATCH_LIMIT` | `20` | Articles per automatic summary batch |
| `AUTO_SUMMARY_INTERVAL_SECONDS` | `30` | Automatic summary polling interval in seconds |
| `AUTO_TRANSLATION_BATCH_LIMIT` | `5` | Articles per background translation batch |
| `AUTO_TRANSLATION_INTERVAL_SECONDS` | `30` | Background translation polling interval in seconds |
| `AUTO_TITLE_PROCESS_BATCH_LIMIT` | `20` | Titles processed per translation/shortening batch |
| `AUTO_TITLE_PROCESS_INTERVAL_SECONDS` | `10` | Background title-processing interval in seconds |
| `AUTO_TITLE_PROCESS_SCAN_LIMIT` | `1000` | Maximum titles scanned per pass |
| `TITLE_SUMMARY_MAX_CHARS` | `30` | Target Chinese-character length for shortened titles |
| `TITLE_SUMMARY_MAX_TOTAL_CHARS` | `40` | Maximum weighted shortened-title length |
| `AUTO_SOURCE_CLASSIFY_BATCH_LIMIT` | `20` | Sources per administrator AI classification batch |
| `AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS` | `120` | Source classification polling interval in seconds |
| `SYSTEM_AI_FAILURE_ALERT_THRESHOLD` | `3` | Consecutive server-AI failures before administrators are alerted |
| `SYSTEM_AI_RECOVERY_STABILITY_SECONDS` | `3600` | Send recovery only after a real success and this many seconds without a new failure; `0` removes the extra stability delay after a real success |
| `TELEGRAM_EMBED_TIMEOUT_SECONDS` | `12` | Telegram embed fetch timeout |

Additional `AI_DAILY_*` variables tune daily-digest candidate and output limits. Keep the built-in defaults unless needed; add any override explicitly to the Compose `environment`.

The legacy `SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD` variable is deprecated and no longer enables call-count-based recovery. If it remains configured, startup logs direct the operator to `SYSTEM_AI_RECOVERY_STABILITY_SECONDS`.

### Image cache

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAGE_CACHE_ENABLED` | `true` | Enable persistent server-side image caching |
| `IMAGE_CACHE_MAX_MB` | `5120` | Normal image cache limit in MB |
| `IMAGE_CACHE_MAX_FILE_MB` | `10` | Maximum cached size for one image in MB |
| `IMAGE_CACHE_PREFETCH_BODY_LIMIT` | `3` | Number of body images prefetched for each new article |
| `IMAGE_CACHE_PREFETCH_WORKERS` | `2` | Number of image prefetch workers |
| `IMAGE_CACHE_PREFETCH_QUEUE_SIZE` | `3000` | Image prefetch queue capacity |

Favorited article images are excluded from `IMAGE_CACHE_MAX_MB` eviction, so the actual directory size may exceed the configured limit. Date-based historical purges preserve favorited articles and their image caches; they run in background batches, show progress in the UI, and attempt to email the initiating administrator on completion, partial failure, or failure. Purges cannot be undone: always use the preview to verify the date and count first.

To remove historical unreferenced image cache entries, run the orphan-cache scan through a backend maintenance operation. Do not directly delete the `image_cache` directory or `cache.db`, as that can leave files and cache indexes inconsistent.

### Page and network

| Variable | Default | Description |
|----------|---------|-------------|
| `CUSTOM_HEAD_HTML` | empty | Trusted HTML injected into `<head>`, such as an analytics script |
| `CUSTOM_FOOTER_HTML` | empty | Trusted HTML that replaces the home-page footer content before the version and GitHub link; supports `<script>` while keeping the fixed suffix |
| `HTTP_PROXY` | empty | HTTP proxy |
| `HTTPS_PROXY` | empty | HTTPS proxy |
| `NO_PROXY` | `localhost,127.0.0.1` | Addresses that bypass the proxy |

## Permissions

The current project has only two signed-in roles: **User** and **Admin**. The retired **Preview** role is automatically migrated to User.

| Capability | Guest | User | Admin |
|------------|-------|------|-------|
| Browse and read articles | First 3 home-page articles only; remaining content is locked | ✓ | ✓ |
| Search and pagination | — | ✓ | ✓ |
| Manual refresh | — | ✓ | ✓ |
| Favorites, personal AI configuration, and AI actions | — | ✓ | ✓ |
| Profile, notification, and sharing settings | — | ✓ | ✓ |
| Source management, global AI settings, and resource usage | — | — | ✓ |
| Delete sources and historical articles | — | — | ✓ |
| Manage users, roles, invitation codes, and invitation review | — | — | ✓ |

Source labels and categories are global. Every user sees the source structure maintained by the administrator. API authorization always uses the current role from the database rather than a stale JWT role claim.

## Common Endpoints

- Health check: `GET /health`
- Manual refresh: signed-in users with the **User** or **Admin** role can use the refresh control or call the authenticated `POST /auth/refresh`. It returns the background-job state immediately instead of waiting for the fetch to finish, so reading remains available. The page polls the authenticated `GET /auth/refresh/status` endpoint and reports completion plus the number of new articles, when any are found.
- Logo: this is only a lightweight return to the first **All** page, scroll to the top, and check for new articles; it does not start a full server-side fetch.
- Article list: `GET /api/news`
- Image cache: `GET /img-cache?url=<encoded-url>`

## Roadmap

### Short-term

- [x] **Source Categorization** — Group and filter articles by source and tag
- [x] **Best-effort WeChat Articles** — Attempt full-text extraction when eligible; retain the original message if the source blocks access
- [x] **Favorites** — Bookmark articles and manage them in a dedicated favorites panel
- [x] **Automatic English Translation** — Automatically translate English titles and articles into Chinese
- [x] **Custom AI API** — Use a custom AI API for article summaries and daily digests
- [ ] **Custom Visible Sources** — Let users select visible sources and build a source marketplace
- [ ] **New Article Notifications** — Follow sources and receive PWA notifications when new articles are fetched

### Long-term

- [ ] **Integrate [RSStT](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)** — Remove the need for a separate RSStT deployment
- [ ] **Keyword Filtering** — Hide articles containing configured keywords
- [ ] **News Podcast Generation** — Automatically generate a podcast from the news
- [ ] **iOS Client** — Native iOS application

## Tech Stack

- Python 3.12
- Flask + Python `ThreadingHTTPServer`
- Nginx
- SQLite
- Vanilla HTML / CSS / JavaScript PWA
- BeautifulSoup

## License

MIT
