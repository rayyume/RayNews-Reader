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

RayNews v6 generates all AI content on the server using administrator-managed credentials under **Admin settings → AI services → Connection settings**. Personal API configuration, browser-direct requests, personal relays, result sharing/uploads, and personal connectivity checks have been removed. OpenAI-compatible and Claude/Anthropic protocols remain supported on the server.

Administrators grant each user access to **article summaries, shortened titles, translation (headlines and bodies), daily digests, and TTS** independently. Administrators always have all grants. Users then choose their own display preferences under **Account | Notifications | Theme | AI features**. Grants do not enable display automatically. Unauthorized enable attempts are rejected by both the UI and server.

The administrator menu is **Users | AI services | Sources | Publish notifications | System maintenance**. Original-language short titles, full translated titles, and translated short titles are separate results. Public article APIs expose original content only; viewing cached AI content never initiates a provider call.

The list prefetches existing title results for adjacent pages. Navigation waits up to 600 ms for titles, then allows browsing original headlines if results are slow or unavailable. Later results update only the affected text without rebuilding the list. Protected titles stay in the current account session's memory and are never written to public article caches.

Daily-digest access and both delivery channels require the grant and the user's digest master switch. Disabling it pauses viewing and delivery while preserving channel preferences. Re-enabling does not backfill missed deliveries.

Article source names prioritize administrator labels, shared source merges, and custom domain mappings. Lists, article details, search, favorites, the audio player, and system media metadata use the same name for signed-in readers and guests. Curated articles do not display old source names as additional labels or tooltips. Public article APIs include the resolved name so a source-metadata request failure does not expose the old label.

**TTS audio:** Configure the independent service under **Admin settings → AI services → Audio reading**. Enter a Base URL such as `https://tts.example.com/v1` (the server appends `/audio/speech`) and authentication details. The UI fixes the model to `kokoro`; the voice defaults to the mix `zf_001(0.5)+zf_022(0.5)`, `lang_code` to `z`, and synthesis speed to `1.3`. Administrators can configure the mixed voice, language code (blank lets the provider infer it), synthesis speed (0.25–4), volume multiplier, streaming, download-link request, and each text-normalization option. Blank volume and the "Use provider defaults" normalization setting retain provider defaults. Playback and downloads support MP3 only; older settings that select other formats automatically fall back to MP3. RayNews does not display the upstream download link. Use Basic Auth or a complete custom Authorization header, save and test the credentials, then enable the service. Secrets remain server-side and are never returned by configuration reads. RayNews omits the undocumented `x-raw-response` header.

Authorized users enable TTS in AI features and click the speaker before the summary button. The spoken title follows the page's display settings and priority: shortened translated title, full translated title, shortened original title, then original title. With body translation enabled, available translated content is always used; otherwise the original body is read. Audio also includes an existing summary when the viewer has summary access enabled. Title and translation updates invalidate old audio; new results for the current article regenerate its audio while preserving pause state. Every audio request rechecks the permissions and preferences required by the selected variants. Images, raw URLs, navigation text and formatting noise are removed without rewriting facts. No extra summary or translation calls are made. The cover disc opens a player with seeking, pause, playback speed and previous/next articles from the initially loaded list. Playback continues while browsing; the next article is generated only on explicit selection. Missing covers fall back to the site image; generation failures offer a retry.

New configurations default to upstream `stream:true`. The server forwards MP3 bytes as they arrive; browsers supporting MP3 ManagedMediaSource or MediaSource start after buffering approximately five seconds of playback, adjusted for playback speed, while the rest continues loading. Short articles start as soon as complete. Pause and speed controls work during generation; full seeking and total duration become available after reception completes. Midstream failures stop playback and offer a retry. iOS/iPadOS prefer ManagedMediaSource and disable remote playback for that streaming source as required by WebKit. Unsupported browsers automatically wait for the complete audio. **Existing configurations with `stream:false` must enable streaming under Admin settings → AI services → Audio reading and save.** The provider must actually support streaming; otherwise playback still waits for upstream generation. The container's Nginx already disables buffering for `/ai/`; any additional reverse proxy must also disable response buffering for that path. Streaming reduces the initial wait, not synthesis time; playback may still stall if generation remains slower than consumption.

It uses two workers by default and one concurrent job per user. Audio is disposable: files live under container `/tmp/raynews-tts-<uid>/`, outside `/app/data`, for up to one hour, with a default 256 MB total limit and a 64 MB per-file limit. `TTS_CACHE_MAX_MB` configures the total limit (64–2048 MB) and `TTS_WORKERS` configures worker count (1–8). Cleanup runs every ten minutes and on requests; restart removes orphaned files. Identical content/settings reuse audio, while changed content/configuration generates a new version. Every audio request checks current grants/preferences. Browser audio stays in memory and is released on logout or ending playback. `TTS_REQUEST_TIMEOUT_SECONDS` sets the synthesis timeout (default 300 seconds).

**Upgrading from v5:** Back up the complete `/app/data` directory. Existing accounts receive all five grants once; future ordinary accounts start without grants and with AI preferences off. Existing summary, translation, title, and delivery display preferences migrate; the old title-view preference enables both title translation and shortening. Daily digests start enabled for existing accounts, while TTS starts disabled. Revocation disables the corresponding preferences; regranting requires the user to enable them again. Personal credentials and sharing health state are removed. Treat the v5 shared article summaries, translations, and AI titles as trusted historical results: keep them in `ai_results` for readers with the corresponding v6 grants and display preferences, and retain a pre-migration snapshot in `ai_results_legacy_v5`. Move AI titles formerly written into `articles.title` into the permissioned title cache while restoring the public article title from its original snapshot. If that original snapshot is missing, the public title becomes “无标题文章”, but the previous AI title remains available through the authorized result and in `ai_titles_legacy_v5` for offline inspection. Server-generated global digests are preserved.

Before deployment, back up the data and run the read-only preview `python3 server_ai_store.py --db /path/to/backup/news.db` against a backup copy. This preservation migration does not require `AI_V6_ACCEPT_LEGACY_LOSS`. Build from code that includes this change; the already released v6.0.0 image still uses the loss-acceptance migration. Reserve disk space for the legacy result snapshot. The internal `news.json` mirror is no longer publicly served. Rollback requires restoring both the database backup and the matching old application.

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
- Under **Admin settings → AI services → Daily digest**, administrators can enable or pause server generation and delivery, choose the daily time, and select in-app notifications, email, or both. Saving takes effect without restarting and persists in the accounts database. The schedule follows the deployment's `TZ` (displayed in the form), defaulting to `Asia/Shanghai` at 21:00 with both delivery methods enabled. Existing `DAILY_SUMMARY_HOUR` / `DAILY_SUMMARY_MINUTE` environment values seed the time until settings are saved. Pausing also stops automatic retries; previously generated content remains readable.
  Each run covers news ingested between consecutive local cutoffs, including daylight-saving transitions; when the time changes, the next window starts at the previous digest's saved cutoff to avoid gaps or overlap. Later arrivals enter the next digest.
  Home-page status, manual retries, and the scheduler use the task date of the latest scheduled local cutoff, switching at the next cutoff. A 23:58 task therefore remains visible and retryable at 00:01 the following day.
  Postponing the schedule after today's task starts preserves its result, retry controls, and original news window. The new time applies to subsequent tasks without moving the current task back to yesterday.
  Article summaries yield event signals, which are grouped into concrete events and ranked by impact and independent publisher coverage.
  Up to 60 events appear under the administrator's source categories. Administrators can inspect scores and selection reasons at
  `GET /admin/daily-digest/events?date=YYYY-MM-DD`.
  Delivery follows both the administrator's selected methods and each user's permission, daily-digest master preference, and subscriptions. In-app copies appear under avatar menu → My Notifications; email additionally requires server `RESEND_API_KEY`, a valid recipient address, and the user's email subscription under Settings → Notifications.
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
| `TZ` | `Asia/Shanghai` | Container timezone and daily-digest scheduling timezone (IANA name) |
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

AI endpoints, keys, models, and providers are configured only by administrators under **AI services → Connection settings**.

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
| Favorites | — | ✓ | ✓ |
| View server AI results | — | Requires a grant and enabled preference | ✓ |
| Profile, notifications, theme, and AI display preferences | — | ✓ | ✓ |
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
