# scrob-export

A tiny web page that reads the [Scrob](https://github.com/ellite/scrob) API and copies plain-text summaries of your watch data (currently watching, history, a single title, upcoming episodes) to the clipboard, ready to paste into an AI chat so you can talk about your shows without re-typing where you are and without spoilers.

Python standard library only, no dependencies.

> The web UI and the generated text are in Spanish (the text is meant to be pasted into a Spanish-language chat). Code, comments and docs are in English.

## Tabs

| Tab | What it copies |
| --- | --- |
| Currently watching (*En curso*) | Shows you still have episodes left in: last watched episode (S/E, title, date) and progress. Finished shows are omitted. Window: 7, 15, 30, 60, 90 days, 6 months, 1 year or everything. Optional "next up" list (may contain episode-title spoilers) that uses the same window. |
| History (*Historial*) | Date range (quick chips or from/to pickers). Compact (one line per show) or detailed (per day) format. |
| Single title (*Una serie*) | Everything you watched of a show or movie, with search-as-you-type suggestions. Pick a suggestion (arrows + Enter) to filter to exactly that title; leave the text free to get everything that matches. Enter runs the query. |
| Upcoming (*Estrenos*) | Yesterday, today, tomorrow, 3 days (yesterday to tomorrow), 7 or 14 days, from Scrob's calendar. |
| Bundle (*Paquete*) | An editable anti-spoiler header (hidden by default, always prepended) plus whichever blocks you tick, in a single copy. Each block has its own options, shown only while its checkbox is ticked. |
| Diagnostics (*Diagnóstico*) | Trimmed raw response of each Scrob endpoint, for debugging. |

Every result shows its size in characters and approximate tokens, with a warning when it gets very long.

## Environment variables

| Variable | Required | Default | Description |
| --- | :---: | --- | --- |
| `SCROB_URL` | yes | – | Base URL of your Scrob instance, no trailing slash |
| `SCROB_USERNAME` | yes | – | Scrob username |
| `SCROB_PASSWORD` | yes | – | Scrob password |
| `HOST_PORT` | no | `7329` | Port published on the host (the container always listens on 7329) |
| `TZ` | no | `UTC` | Time zone used for dates and ranges, e.g. `America/El_Salvador` |
| `MAX_PAGES` | no | `200` | Cap on history pages fetched (100 events each) |
| `CACHE_TTL` | no | `60` | Seconds to cache each Scrob query |
| `FETCH_WORKERS` | no | `6` | History pages fetched in parallel |

## Deploy

### Docker Compose

```bash
cp .env.example .env   # edit the values
docker compose up -d --build
```

### Portainer (from a Git repository)

1. **Stacks → Add stack → Repository**, point it at this repo and `docker-compose.yml`.
2. Under **Environment variables** set `SCROB_URL`, `SCROB_USERNAME`, `SCROB_PASSWORD` (and optionally `HOST_PORT`, `TZ`).
3. Deploy. After each push use **Pull and redeploy** and make sure the image is rebuilt (the compose file uses `build: .`).

Health check: `http://<host>:<HOST_PORT>/health` → `{"ok": true}`.

## Notes

- **Serve it over HTTPS.** Browsers only allow clipboard access in a secure context (a plain-HTTP fallback exists but is less reliable).
- There is no authentication of its own: do not expose it to the internet. Keep it on a private network or behind a VPN.
- Authentication against Scrob uses the web login form plus the session cookie, and renews itself when the session expires. If you change your Scrob password, update `SCROB_PASSWORD`.
- Scrob's OpenAPI does not describe response bodies, so the reader is tolerant about field names. If a block comes out with empty fields, check the **Diagnostics** tab.
- Season/episode numbering follows TMDB; for anime it can differ from other sources.
- The search suggestions come from an index of your history that the server builds in the background at startup (refreshed every 5 minutes). With a large history the first build takes a few seconds; until then the box shows "Indexando…".
- Scrob has no date filter on the history endpoint, so it is paged 100 at a time and the crawl stops once it leaves the requested range. Long ranges ("Todo") fetch pages in parallel.
