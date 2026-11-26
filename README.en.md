# RayNews 📡 🤖

<p align="center">
  <a href="README.md">🇨🇳 中文</a> | <b>🇺🇸 English</b>
</p>

A self-hosted news reader: it pulls news from a public Telegram channel and serves it as a responsive web app and PWA, with optional AI summaries, translation, daily digests and text-to-speech.

> **⚠️ Prerequisite: RayNews only reads a public Telegram channel — it does not push RSS content into one.**
> First set up a public channel, then use [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot), another bot, or manual posts to turn your news sources into channel messages. RayNews reads from that channel.

**Demo:** [https://news.rayyu.me](https://news.rayyu.me)

## Preview

<table>
  <tr>
    <th width="72%">Desktop</th>
    <th width="28%">Mobile PWA</th>
  </tr>
  <tr>
    <td align="center" valign="top">
      <img src="https://img.rayyu.me/file/1781248235309_PC.png" alt="RayNews on desktop" width="100%">
    </td>
    <td align="center" valign="top">
      <img src="https://img.rayyu.me/file/1781248235931_PWA.jpg" alt="RayNews PWA on mobile" width="100%">
    </td>
  </tr>
</table>

## Features

### Reading

- **Fetching**: new articles arrive automatically every 15 minutes, and you can refresh on demand
- **Full text**: extracts full articles from Telegraph and similar pages; WeChat article links are fetched on a best-effort basis
- **Filtering**: browse by publisher and by your own categories, with publishers and categories managed separately
- **Search and paging**: search titles, sources and summaries, with cached results marked when you are offline
- **Swipe navigation**: swipe left or right on the home page to switch categories; the current one stays until the next is ready
- **Favorites**: save articles and have them sync across your logged-in devices
- **Sharing**: share a link, or generate a share image with title, source, summary and QR code
- **Image cache**: article images are cached server-side; images of favorited articles are kept permanently and never evicted
- **Themes and offline**: light, dark or system theme, installable as a PWA, and previously read content stays available offline

### AI

The server-side API is configured once by the administrator and granted per user; each user decides whether to display the result.

- **Summary**: a key-points summary for each article
- **Title shortening**: long headlines compressed into a short title
- **Translation**: English titles and bodies translated into Chinese, original and translation shown independently
- **Daily digest**: a daily roundup of the previous day's news, delivered in-app, by email, or both
- **Text-to-speech**: reads the title, summary and body aloud, with segmented streaming, playback speed, previous/next article and background playback

## Quick Start

You need Docker with Docker Compose, and a public Telegram channel that already contains news posts (see the prerequisite above).

```bash
git clone https://github.com/rayyume/RayNews-Reader.git
cd RayNews-Reader
cp .env.example .env
```

Fill in at least these two values in `.env`:

```dotenv
TELEGRAM_CHANNEL_URL=https://telegram.me/s/your_public_channel
RAYNEWS_PUBLIC_URL=https://news.example.com
```

Then start it:

```bash
docker compose up -d
```

Open `http://<server-address>:8090`. The web app is available immediately; the first fetch runs in the background, so article lists may be empty for a moment.

**The first account to sign up becomes the administrator.** Later users register with an invite code and need the administrator to grant AI permissions.

## Configuration

The two values above are required; everything else is optional.

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `Asia/Shanghai` | Container timezone, also the schedule timezone for daily digests |
| `RAYNEWS_SECRET` | generated | Token signing key; generated and stored in `/app/data` when unset |
| `DATA_DIR` | `/app/data` | Data directory |
| `RESEND_API_KEY` | empty | Enables email: invite codes, signup notices and daily digest mail |
| `RAYNEWS_ADMIN_EMAIL` | first admin's email | Receives invite requests, signup notices and cleanup results |
| `RAYNEWS_FROM_EMAIL` | `onboarding@resend.dev` | Sender address; use a verified domain in production |
| `IMAGE_CACHE_MAX_MB` | `5120` | Image cache size limit; favorited articles' images are excluded |
| `TTS_WORKERS` | `2` | Number of simultaneous speech jobs (1–8) |
| `TTS_CACHE_MAX_MB` | `256` | Temporary audio size limit (64–2048) |
| `TTS_REQUEST_TIMEOUT_SECONDS` | `300` | Per-request synthesis timeout |
| `HTTP_PROXY` / `HTTPS_PROXY` | empty | Proxy used when fetching articles and images |
| `CUSTOM_HEAD_HTML` / `CUSTOM_FOOTER_HTML` | empty | Custom HTML injected into the page, e.g. analytics |

The AI endpoint, model and key are not environment variables — the administrator sets them in the UI. **See [configuration reference](docs/configuration.en.md) for the full variable list and advanced tuning.**

## Usage

### Administrator

Open **Admin settings** and work through three steps:

1. **Sources**: review fetched publishers, set categories, merge or delete publishers and their past articles
2. **AI service**: enter the endpoint, model and key, then enable summaries, titles, translation, daily digest and text-to-speech individually
3. **Users**: grant each regular user the five permissions — summary, title shortening, translation, daily digest, TTS

Other common tasks:

- **Digest settings**: change the generation time and delivery channel under **AI service → Daily digest**; saved settings apply immediately without a restart
- **Resources and cleanup**: check resource usage and temporary audio size under **System maintenance**, and delete historical articles by date
- **Invites**: generate invite codes for new users; the first account to register becomes the administrator

### Regular user

- **Enable features**: turn on each granted feature under **User settings → AI features**
- **Text-to-speech**: tap the speaker before the summary button in an article to start; a spinning disc appears bottom-right, tap it to open the player with pause, seek, speed and previous/next controls; returning to the list or browsing other articles does not stop playback
- **Daily digest**: in-app notices live under **Avatar menu → My notifications**; email delivery is enabled separately under **User settings → Notifications**
- **Theme**: switch light, dark or system under **User settings → Theme**

## Permissions

There are only two roles: regular user and administrator.

| Capability | Anonymous | User | Admin |
|------------|-----------|------|-------|
| Read articles | First 3 on the home page only | ✓ | ✓ |
| Search, paging, favorites, manual refresh | — | ✓ | ✓ |
| AI summaries, translation, digest, TTS | — | Granted per feature and switched on | ✓ |
| Theme, notifications and AI display settings | — | ✓ | ✓ |
| Manage publishers, AI service, users and cleanup | — | — | ✓ |

## Images and Updates

`docker-compose.yml` builds the current source with `build: .` by default. To use prebuilt images instead, comment out `build` and set:

```yaml
image: ghcr.io/rayyume/raynews:latest   # stable
image: ghcr.io/rayyume/raynews:dev      # development
```

Updating:

```bash
docker compose pull && docker compose up -d
```

## Data and Backup

```yaml
volumes:
  - ./data:/app/data
```

`/app/data` holds the article database, users and AI settings, the login secret, avatars, the image cache and fetch state. **Keep the whole directory when upgrading or recreating the container** — backing it up is backing up RayNews. Temporary audio lives inside the container, never in `/app/data`, and needs no backup.

## Upgrading from v5

Back up the whole `/app/data` directory first. After upgrading:

- Existing users receive all five AI permissions once; regular users created later start with none and must be granted access
- Existing translation, summary and title display preferences are kept; the old title toggle becomes separate translation and shortening switches
- Existing users keep the daily digest enabled with their subscription choices; text-to-speech starts disabled
- AI summaries, translations and titles generated under v5 remain available, governed by the new permissions and switches

See [architecture and migration notes](docs/architecture.md) for details.

## Roadmap

### Short term

- [x] Publisher categories
- [x] Best-effort full text for WeChat articles
- [x] Favorites
- [x] Automatic English title and body translation
- [x] Custom AI endpoints
- [x] Daily digest and text-to-speech
- [ ] User-selected visible publishers
- [ ] New-article notifications for followed publishers

### Long term

- [ ] Bundled [RSStT](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) integration, no separate deployment
- [ ] Keyword filtering
- [ ] News podcast generation
- [ ] Native iOS client

## License

MIT
