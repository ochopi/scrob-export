#!/usr/bin/env python3
"""scrob-export: turn your Scrob watch data into plain text you can paste into an AI chat.

Standard library only. It logs in to Scrob through the web login form (the same
way a browser does), keeps the session cookie, and serves a few JSON endpoints
plus one static page.
"""
import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

SCROB_URL = os.environ.get("SCROB_URL", "").rstrip("/")
SCROB_USERNAME = os.environ.get("SCROB_USERNAME", "")
SCROB_PASSWORD = os.environ.get("SCROB_PASSWORD", "")
PORT = int(os.environ.get("PORT", "7329"))
try:
    TZ = ZoneInfo(os.environ.get("TZ") or "UTC")
except Exception:  # unknown zone or no tzdata installed: fall back to UTC
    TZ = timezone.utc
MAX_PAGES = int(os.environ.get("MAX_PAGES", "200"))  # 200 pages x 100 events = 20,000 events
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))  # seconds, per Scrob query
FETCH_WORKERS = int(os.environ.get("FETCH_WORKERS", "6"))  # parallel history page fetches
INDEX_TTL = 300  # seconds before the search-box title index is refreshed
NEXT_UP_LIMIT = 500  # /history/next-up has no default limit; ask for "everything"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Output text is Spanish on purpose: it is meant to be pasted into a Spanish-language chat.
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


# --------------------------------------------------------------------------
# Scrob client (web form login + session cookie)
# --------------------------------------------------------------------------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow redirects: a 302 is how Scrob tells us the session is gone."""

    def redirect_request(self, *a, **k):
        return None


class ScrobError(Exception):
    pass


class Scrob:
    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.lock = threading.Lock()
        self.logged = False
        self.gen = 0  # bumped on every login, so parallel requests renew the session only once
        self.cache = {}

    def _open(self, req, timeout=60):
        try:
            return self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:  # 3xx/4xx/5xx are raised; treat them as plain responses
            return e

    @staticmethod
    def _body(resp):
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw

    def login(self):
        """Must be called with self.lock held."""
        if not SCROB_URL:
            raise ScrobError("Falta SCROB_URL en el entorno")
        if not SCROB_USERNAME or not SCROB_PASSWORD:
            raise ScrobError("Faltan SCROB_USERNAME / SCROB_PASSWORD en el entorno")
        data = urllib.parse.urlencode(
            {"_step": "login", "_next": "/", "username": SCROB_USERNAME, "password": SCROB_PASSWORD}
        ).encode()
        req = urllib.request.Request(
            SCROB_URL + "/login", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept-Encoding": "identity"},
        )
        resp = self._open(req)
        code = resp.getcode()
        loc = resp.headers.get("Location", "")
        # Success is a redirect away from /login; a redirect back to /login means bad credentials.
        if code != 302 or loc.rstrip("/").endswith("login"):
            raise ScrobError(f"Login rechazado por Scrob (HTTP {code}, Location={loc!r})")
        self.logged = True
        self.gen += 1

    def get(self, path, params=None, use_cache=True, _retry=True):
        qs = ""
        if params:
            qs = "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True
            )
        url = f"{SCROB_URL}/api/proxy{path}{qs}"
        if use_cache:
            hit = self.cache.get(url)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
        with self.lock:
            if not self.logged:
                self.login()
            gen = self.gen
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
        )
        resp = self._open(req)
        code = resp.getcode()
        if code in (301, 302, 401, 403) and _retry:
            with self.lock:
                if self.gen == gen:  # nobody renewed the session in the meantime
                    self.login()
            return self.get(path, params, use_cache, _retry=False)
        body = self._body(resp)
        if code != 200:
            raise ScrobError(f"Scrob respondio HTTP {code} en {path}: {body[:200]!r}")
        try:
            data = json.loads(body)
        except ValueError:
            raise ScrobError(f"Respuesta no-JSON en {path}: {body[:120]!r}")
        if use_cache:
            if len(self.cache) > 300:
                self.cache.clear()
            self.cache[url] = (time.time(), data)
        return data


scrob = Scrob()


# --------------------------------------------------------------------------
# Tolerant field extraction. Scrob's OpenAPI does not describe response bodies,
# so field names are looked up under several known/likely spellings.
# --------------------------------------------------------------------------
def dig(d, path):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def pick(d, *paths):
    """First non-empty value among several dotted paths."""
    for p in paths:
        v = dig(d, p)
        if v not in (None, ""):
            return v
    return None


def items_of(resp):
    """Find the list of rows inside a response, whatever the wrapper key is."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("items", "results", "events", "history", "data", "entries", "shows", "lists", "ratings"):
            if isinstance(resp.get(k), list):
                return resp[k]
        for v in resp.values():  # any list of objects
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in resp.values():  # one level deeper
            if isinstance(v, dict):
                r = items_of(v)
                if r:
                    return r
    return []


def parse_dt(v):
    """Parse an ISO timestamp. Naive values are UTC (that is what Scrob sends)."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except ValueError:
        return None


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def norm(e):
    """Flatten one event/row into a plain dict. Returns None if it is not a dict."""
    if not isinstance(e, dict):
        return None
    mtype = pick(e, "media_type", "type", "media.media_type", "media.type")
    show = pick(e, "show_title", "series_title", "series_name", "show_name", "show.title", "show.name",
                "media.show_title", "media.series_name", "tmdb_data.show_title",
                "media.tmdb_data.show_title")
    season = to_int(pick(e, "season_number", "season", "media.season_number", "episode.season_number",
                         "tmdb_data.season_number", "media.tmdb_data.season_number"))
    ep = to_int(pick(e, "episode_number", "episode", "media.episode_number", "episode.episode_number",
                     "tmdb_data.episode_number", "media.tmdb_data.episode_number"))
    ep_title = pick(e, "episode_title", "episode_name", "episode.title", "episode.name",
                    "media.episode_title", "media.name", "tmdb_data.name", "media.tmdb_data.name")
    title = pick(e, "title", "name", "media.title", "media.name", "tmdb_data.title", "media.tmdb_data.title")
    year = pick(e, "year", "media.year")
    if year is None:
        release = pick(e, "release_date", "media.release_date")
        year = str(release)[:4] if release else None
    when = parse_dt(pick(e, "watched_at", "created_at", "date", "timestamp", "media.watched_at"))
    is_ep = (str(mtype).lower() == "episode") or (season is not None and ep is not None)
    if is_ep and not show and title and ep_title and title != ep_title:
        show = title  # some payloads put the show name in "title"
    if is_ep and not show:
        show = title
    if is_ep and not ep_title and title and title != show:
        ep_title = title  # "title" holds the episode title when a show title is present
    return {
        "kind": "episode" if is_ep else "movie",
        "show": str(show) if show else None,
        "season": season, "episode": ep,
        "ep_title": str(ep_title) if ep_title else None,
        "title": str(title) if title else None,
        "year": year,
        "when": when,
        "rating": pick(e, "rating", "user_rating"),
        "raw": e,
    }


def fmt_date(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def fmt_short(d):
    return f"{d.day} {MONTHS[d.month - 1]}"


def runs(nums):
    """[1,2,3,5] -> 'E1-E3, E5'"""
    nums = sorted(set(nums))
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append((start, prev))
            start = prev = n
    if start is not None:
        out.append((start, prev))
    return ", ".join(f"E{a}" if a == b else f"E{a}-E{b}" for a, b in out)


def season_runs(pairs):
    """[(season, episode), ...] -> 'T1 E1-E12; T2 E1-E3' (season 0 = specials)"""
    by = {}
    for s, e in pairs:
        by.setdefault(s, set()).add(e)
    parts = []
    for s in sorted(by, key=lambda x: (x is None, x)):
        label = "Especiales" if s == 0 else (f"T{s}" if s is not None else "T?")
        parts.append(f"{label} {runs(by[s])}")
    return "; ".join(parts)


def ep_label(n):
    if n["season"] is None or n["episode"] is None:
        return ""
    return f"T{n['season']} E{n['episode']}"


EPOCH = datetime.min.replace(tzinfo=timezone.utc)  # sort key for rows without a date


# --------------------------------------------------------------------------
# Paged history with a date range
# --------------------------------------------------------------------------
def fetch_history(d_from=None, d_to=None, q=None, max_pages=None, use_cache=True):
    """Return (normalized events, warnings) for the requested date range.

    Scrob has no date filter on /history, so we page through it. Results come
    newest-first, which lets us stop as soon as a whole page is older than
    d_from. After the first page, more pages are fetched in parallel batches.
    """
    limit = max_pages or MAX_PAGES
    out, warnings = [], []

    def load(page):
        resp = scrob.get("/history", {"page": page, "page_size": 100, "q": q}, use_cache=use_cache)
        return resp, [r for r in map(norm, items_of(resp)) if r]

    def consume(rows):
        """Keep the rows inside the range. Return True once the range is exhausted."""
        for r in rows:
            d = r["when"].date() if r["when"] else None
            if d is None or ((not d_to or d <= d_to) and (not d_from or d >= d_from)):
                out.append(r)
        dated = [r["when"].date() for r in rows if r["when"]]
        return bool(d_from and dated and max(dated) < d_from)

    resp, rows = load(1)
    total = to_int(resp.get("total_pages")) if isinstance(resp, dict) else None
    last_page = min(limit, total) if total else limit
    done = consume(rows) or len(rows) < 100
    page, size = 2, 2  # batch size doubles up to FETCH_WORKERS, so short ranges stay cheap
    if not done and last_page >= 2:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            while not done and page <= last_page:
                batch = range(page, min(page + size, last_page + 1))
                for _, rows in pool.map(load, batch):
                    if done:
                        break
                    done = consume(rows) or len(rows) < 100
                page = batch[-1] + 1
                size = min(size * 2, FETCH_WORKERS)
    if not done and page > last_page and last_page == limit and (total is None or total > limit):
        warnings.append(f"Se alcanzo el limite de {limit} paginas; el historial puede estar truncado.")
    out.sort(key=lambda r: r["when"] or EPOCH, reverse=True)
    return out, warnings


def parse_range(qs):
    today = datetime.now(TZ).date()
    d_to = date.fromisoformat(qs["to"][0]) if qs.get("to") and qs["to"][0] else today
    d_from = date.fromisoformat(qs["from"][0]) if qs.get("from") and qs["from"][0] else d_to - timedelta(days=6)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    return d_from, d_to


# --------------------------------------------------------------------------
# Output blocks
# --------------------------------------------------------------------------
def result(text, warnings=None, count=None):
    return {"text": text, "chars": len(text), "tokens": round(len(text) / 3.6),
            "count": count, "warnings": warnings or []}


def block_history(qs):
    d_from, d_to = parse_range(qs)
    mode = (qs.get("format") or ["compact"])[0]
    events, warns = fetch_history(d_from, d_to)
    head = f"HISTORIAL VISTO ({fmt_date(d_from)} a {fmt_date(d_to)}) - {len(events)} registros"
    if not events:
        return result(head + "\nSin actividad en ese rango.", warns, 0)
    unparsed = sum(1 for r in events if r["kind"] == "episode" and (not r["show"] or r["season"] is None))
    if unparsed:
        warns.append(f"{unparsed} episodios sin serie/temporada reconocible; revisa la pestana Diagnostico.")
    lines = [head]
    if mode == "detailed":
        cur = None
        for r in events:
            d = r["when"].date() if r["when"] else None
            if d != cur:
                cur = d
                lines.append(f"\n{d.isoformat() if d else 'Sin fecha'}")
            if r["kind"] == "movie":
                y = f" [{r['year']}]" if r["year"] else ""
                lines.append(f"- Pelicula: {r['title'] or '?'}{y}")
            else:
                t = f" «{r['ep_title']}»" if r["ep_title"] else ""
                lines.append(f"- {r['show'] or r['title'] or '?'} {ep_label(r)}{t}".replace("  ", " "))
    else:
        # Compact: one line per show with episode runs; the word "visto" lives only in the header
        shows, movies = {}, []
        for r in events:
            if r["kind"] == "movie":
                movies.append(r)
                continue
            s = shows.setdefault(r["show"] or r["title"] or "?", {"pairs": [], "dates": []})
            s["pairs"].append((r["season"], r["episode"]))
            if r["when"]:
                s["dates"].append(r["when"].date())
        for name, s in sorted(shows.items(), key=lambda kv: max(kv[1]["dates"] or [date.min]), reverse=True):
            ds = s["dates"]
            span = ""
            if ds:
                a, b = min(ds), max(ds)
                span = f" ({fmt_short(a)})" if a == b else f" ({fmt_short(a)} - {fmt_short(b)})"
            lines.append(f"- {name}: {season_runs([p for p in s['pairs'] if p[0] is not None and p[1] is not None])}{span}")
        for r in movies:
            y = f" [{r['year']}]" if r["year"] else ""
            d = f" ({fmt_short(r['when'])})" if r["when"] else ""
            lines.append(f"- Pelicula: {r['title'] or '?'}{y}{d}")
    return result("\n".join(lines), warns, len(events))


def progress_index():
    """lowercase show name -> {'text': 'N/M episodios vistos', 'complete': bool} from /history/progress."""
    idx = {}
    page = 1
    while page <= 20:
        resp = scrob.get("/history/progress", {"type": "watched", "page": page, "page_size": 200})
        rows = items_of(resp)
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = pick(row, "title", "name", "show_title", "show.title", "show.name")
            seen = to_int(pick(row, "watched_episodes", "watched_count", "watched", "episodes_watched"))
            total = to_int(pick(row, "total_episodes", "total_aired_episodes", "total", "episodes_total"))
            pct = pick(row, "watch_pct", "watched_pct", "percent")
            flag = pick(row, "complete", "completed", "is_complete", "is_completed")
            if not name:
                continue
            complete, text = False, None
            if seen is not None and total:
                text = f"{seen}/{total} episodios vistos"
                complete = seen >= total
            elif pct is not None:
                try:
                    text = f"{round(float(pct))}% visto"
                    complete = float(pct) >= 100
                except (TypeError, ValueError):
                    pass
            if flag is True:
                complete = True
            if text or flag is not None:
                idx[str(name).lower()] = {"text": text, "complete": complete}
        total_pages = to_int(resp.get("total_pages")) if isinstance(resp, dict) else None
        if not rows or (total_pages is not None and page >= total_pages) or (total_pages is None and len(rows) < 200):
            break
        page += 1
    return idx


def next_up(cutoff):
    """Next unwatched episode per show, newest activity first.

    cutoff: only shows last watched on or after this date (None = no limit).
    Returns (last_watched, normalized row, episodes_left) tuples.
    """
    out = []
    for x in items_of(scrob.get("/history/next-up", {"limit": NEXT_UP_LIMIT})):
        if not isinstance(x, dict) or x.get("dropped"):
            continue
        last = parse_dt(x.get("last_watched_at"))
        if cutoff and (last is None or last.date() < cutoff):
            continue
        n = norm(x)
        if n:
            out.append((last, n, to_int(x.get("episodes_left"))))
    out.sort(key=lambda t: t[0] or EPOCH, reverse=True)
    return out


def block_current(qs):
    days = int((qs.get("days") or ["90"])[0])  # 0 = whole history
    with_next = (qs.get("next") or ["0"])[0] == "1"
    today = datetime.now(TZ).date()
    cutoff = today - timedelta(days=days) if days else None
    events, warns = fetch_history(cutoff, today)
    latest = {}
    for r in events:
        if r["kind"] != "episode":
            continue
        name = r["show"] or r["title"] or "?"
        if name not in latest:  # events are newest-first, so the first hit is the latest
            latest[name] = r
    try:
        prog = progress_index()
    except ScrobError as e:
        prog = {}
        warns.append(f"No pude leer el progreso por serie ({e}); se muestran todas.")
    if latest and not prog:
        warns.append("Sin datos de progreso: no se pueden ocultar las series terminadas (revisa Diagnostico > progress).")
    pending = {n: r for n, r in latest.items() if not (prog.get(n.lower()) or {}).get("complete")}
    done = len(latest) - len(pending)
    scope = f"ultimos {days} dias" if days else "todo el historial"
    lines = [f"SERIES EN CURSO (con actividad en {scope}; solo con episodios por ver) - {len(pending)} series"]
    for name, r in sorted(pending.items(), key=lambda kv: kv[1]["when"] or EPOCH, reverse=True):
        t = f" «{r['ep_title']}»" if r["ep_title"] else ""
        when = f" (visto {fmt_short(r['when'])})" if r["when"] else ""
        info = (prog.get(name.lower()) or {}).get("text")
        p = f" - {info}" if info else ""
        lines.append(f"- {name}: ultimo visto {ep_label(r)}{t}{when}{p}")
    if not pending:
        lines.append("Ninguna serie pendiente.")
    if done:
        warns.append(f"{done} series terminadas omitidas (estan en el Historial).")
    # episodes that were started but not finished
    try:
        cw = [x for x in items_of(scrob.get("/history/continue-watching", {"limit": 20})) if isinstance(x, dict)]
        if cw:
            lines.append("\nA MEDIAS")
            for x in cw:
                n = norm(x)
                pct = pick(x, "progress_pct", "progress_percent", "percent", "progress")
                who = n["show"] or n["title"] or "?"
                lab = f" {ep_label(n)}" if n["kind"] == "episode" else ""
                extra = f" ({round(float(pct))}%)" if isinstance(pct, (int, float)) and pct > 1 else ""
                lines.append(f"- {who}{lab}{extra}")
    except ScrobError as e:
        warns.append(f"continue-watching no disponible: {e}")
    if with_next:
        try:
            up = next_up(cutoff)  # same activity window as the list above
            lines.append(f"\nPROXIMO A VER (series con actividad en {scope}; puede contener spoilers del titulo del episodio) - {len(up)} series")
            for _, n, left in up:
                label = " ".join(x for x in (ep_label(n), f"«{n['ep_title']}»" if n["ep_title"] else "") if x)
                line = f"- {n['show'] or n['title'] or '?'}"
                if label:
                    line += f": {label}"
                if left:
                    line += f" - faltan {left}"
                lines.append(line)
            if not up:
                lines.append("Ninguno.")
        except ScrobError as e:
            warns.append(f"next-up no disponible: {e}")
    return result("\n".join(lines), warns, len(pending))


def block_upcoming(qs):
    # from_off/to_off are day offsets relative to "today" (e.g. -1:1 = yesterday..tomorrow)
    if qs.get("from_off") and qs["from_off"][0] != "":
        off_a, off_b = int(qs["from_off"][0]), int((qs.get("to_off") or qs["from_off"])[0])
    else:
        off_a, off_b = 0, int((qs.get("days") or ["7"])[0])
    resp = scrob.get("/calendar", {"cached_only": "true"})
    cal = resp.get("calendar", resp) if isinstance(resp, dict) else {}
    if cal.get("today"):
        base = date.fromisoformat(str(cal["today"])[:10])
    else:
        base = datetime.now(TZ).date()
    d_a, d_b = base + timedelta(days=off_a), base + timedelta(days=off_b)
    rows = []
    for e in cal.get("entries", []) or items_of(cal):
        if not isinstance(e, dict):
            continue
        try:
            ad = date.fromisoformat(str(pick(e, "air_date", "date"))[:10])
        except ValueError:
            continue
        if d_a <= ad <= d_b:
            rows.append((ad, norm(e) or {}))
    rows.sort(key=lambda x: x[0])
    span = fmt_date(d_a) if d_a == d_b else f"{fmt_short(d_a)} a {fmt_short(d_b)}"
    lines = [f"ESTRENOS ({span}) - {len(rows)} episodios"]
    for ad, n in rows:
        t = f" «{n['ep_title']}»" if n.get("ep_title") else ""
        lines.append(f"- {ad.isoformat()} {n.get('show') or n.get('title') or '?'} {ep_label(n)}{t}".replace("  ", " "))
    if not rows:
        lines.append("Nada programado.")
    warns = []
    if not rows and d_a < base:
        warns.append("El calendario de Scrob puede no incluir dias pasados.")
    return result("\n".join(lines), warns, len(rows))


def _ratings_by_name():
    out = {}
    try:
        for x in items_of(scrob.get("/ratings")):
            n = norm(x)
            if n and n["rating"] is not None:
                out.setdefault((n["show"] or n["title"] or "").lower(), []).append(n)
    except ScrobError:
        pass
    return out


def block_series(qs):
    q = (qs.get("q") or [""])[0].strip()
    exact = (qs.get("exact") or ["0"])[0] == "1"  # 1 = the user picked a suggestion
    if len(q) < 2:
        raise ScrobError("Escribe al menos 2 letras del nombre")
    events, warns = fetch_history(q=q, max_pages=50)
    ql = q.lower()

    def match(name):
        return name.lower() == ql if exact else ql in name.lower()

    shows, movies = {}, []
    for r in events:
        if r["kind"] == "episode":
            name = r["show"] or r["title"] or "?"
            if match(name):
                shows.setdefault(name, []).append(r)
        elif r["title"] and match(r["title"]):
            movies.append(r)
    if not shows and not movies:
        return result(f"No encontre nada visto que coincida con «{q}».", warns, 0)
    ratings = _ratings_by_name()
    lines = []
    for name, evs in shows.items():
        evs.sort(key=lambda r: r["when"] or EPOCH, reverse=True)
        pairs = [(r["season"], r["episode"]) for r in evs if r["season"] is not None and r["episode"] is not None]
        dated = [r["when"].date() for r in evs if r["when"]]
        last = evs[0]
        lines.append(f"SERIE: {name}")
        lines.append(f"- Visto: {season_runs(pairs)}")
        if dated:
            lines.append(f"- Primera vez: {fmt_date(min(dated))} / Ultima vez: {fmt_date(max(dated))}")
        t = f" «{last['ep_title']}»" if last["ep_title"] else ""
        lines.append(f"- Ultimo visto: {ep_label(last)}{t}")
        rr = ratings.get(name.lower())
        if rr:
            lines.append("- Mis puntuaciones: " + ", ".join(
                f"{(ep_label(x) or 'serie')}={x['rating']}" for x in rr[:12]))
        recent = [r for r in evs[:5] if r["ep_title"]]
        if recent:
            lines.append("- Ultimos episodios: " + "; ".join(f"{ep_label(r)} «{r['ep_title']}»" for r in recent))
        lines.append("")
    seen_movies = {}
    for r in movies:
        seen_movies.setdefault(r["title"], []).append(r)
    for title, evs in seen_movies.items():
        y = f" ({evs[0]['year']})" if evs[0]["year"] else ""
        lines.append(f"PELICULA: {title}{y}")
        dates = ", ".join(fmt_date(r["when"].date()) for r in evs if r["when"])
        if dates:
            lines.append(f"- Vista: {dates}")
        rr = ratings.get(title.lower())
        if rr:
            lines.append(f"- Mi puntuacion: {rr[0]['rating']}")
        lines.append("")
    return result("\n".join(lines).strip(), warns, sum(len(v) for v in shows.values()) + len(movies))


# --------------------------------------------------------------------------
# Title index for the search-box suggestions
# --------------------------------------------------------------------------
class TitleIndex:
    """Every watched show/movie with its record count.

    Building it means crawling the whole history, so it runs in the background
    (also at startup). Requests never block on it: while the first build runs
    the API answers "loading", and later refreshes serve the previous data.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.at = 0.0
        self.building = False
        self.error = None

    def refresh_async(self):
        with self.lock:
            if self.building:
                return
            self.building = True
        threading.Thread(target=self._build, daemon=True).start()

    def _build(self):
        try:
            events, _ = fetch_history(use_cache=False)  # skip the query cache: this is a big one-off crawl
            acc = {}
            for r in events:
                if r["kind"] == "episode":
                    name, kind = r["show"] or r["title"], "serie"
                else:
                    name, kind = r["title"], "pelicula"
                if name:
                    acc[(name, kind)] = acc.get((name, kind), 0) + 1
            self.data = [{"name": n, "kind": k, "count": c} for (n, k), c in acc.items()]
            self.at = time.time()
            self.error = None
        except Exception as e:  # noqa: BLE001 - reported to the UI, retried on the next request
            self.error = str(e)
        finally:
            self.building = False

    def get(self):
        if self.data is None or time.time() - self.at > INDEX_TTL:
            self.refresh_async()
        return self.data


title_index = TitleIndex()


def suggest(qs):
    q = (qs.get("q") or [""])[0].strip().lower()
    data = title_index.get()  # also (re)starts the build, so an empty query works as a warm-up
    if data is None:
        return {"items": [], "loading": title_index.error is None, "error": title_index.error}
    if len(q) < 2:
        return {"items": [], "loading": False}
    hits = [x for x in data if q in x["name"].lower()]
    hits.sort(key=lambda x: (not x["name"].lower().startswith(q), -x["count"], x["name"].lower()))
    return {"items": hits[:10], "loading": False}


# --------------------------------------------------------------------------
# Raw endpoints for the Diagnostics tab (trimmed so they stay readable)
# --------------------------------------------------------------------------
RAW = {
    "history": ("/history", {"page": 1, "page_size": 5}),
    "next-up": ("/history/next-up", {"limit": 3}),
    "continue-watching": ("/history/continue-watching", {"limit": 3}),
    "progress": ("/history/progress", {"type": "watched", "page_size": 3}),
    "calendar": ("/calendar", {"cached_only": "true"}),
    "ratings": ("/ratings", {}),
    "shows": ("/shows", {"page_size": 3}),
    "lists": ("/lists", {}),
}


def trim(o, n=4):
    if isinstance(o, list):
        return [trim(x, n) for x in o[:n]]
    if isinstance(o, dict):
        return {k: trim(v, n) for k, v in o.items()}
    return o


def raw(name):
    if name not in RAW:
        raise ScrobError("Endpoint no permitido: " + name)
    path, params = RAW[name]
    return result(json.dumps(trim(scrob.get(path, params)), ensure_ascii=False, indent=2))


ROUTES = {
    "/api/history": block_history,
    "/api/current": block_current,
    "/api/upcoming": block_upcoming,
    "/api/series": block_series,
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "scrob-export/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        p = u.path
        try:
            if p == "/health":
                return self._send(200, {"ok": True})
            if p in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if p in ROUTES:
                return self._send(200, ROUTES[p](qs))
            if p == "/api/suggest":
                return self._send(200, suggest(qs))
            if p == "/api/raw":
                return self._send(200, raw((qs.get("name") or [""])[0]))
            if p == "/api/status":
                scrob.get("/history", {"page": 1, "page_size": 1})
                return self._send(200, {"ok": True})
            self._send(404, {"error": "no encontrado"})
        except ScrobError as e:
            self._send(502, {"error": str(e)})
        except ValueError as e:
            self._send(400, {"error": f"Parametro invalido: {e}"})
        except Exception as e:  # noqa: BLE001
            print("ERROR", repr(e), flush=True)
            self._send(500, {"error": f"Error interno: {e}"})


if __name__ == "__main__":
    print(f"scrob-export listening on :{PORT}", flush=True)
    title_index.refresh_async()  # warm the search index so the first keystrokes are instant
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
