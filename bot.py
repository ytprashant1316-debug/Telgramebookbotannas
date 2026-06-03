#!/usr/bin/env python3
"""
Anna's Archive Telegram Bot
Multi-user, with download + upload progress bars, admin controls,
configurable search modes, welcome messages, and group/PM routing.
"""

import asyncio
import logging
import os
import re
import json
import time
import urllib.request
import uuid
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor
import pymongo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMemberAdministrator,
    ChatMemberOwner,
)
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
import cloudscraper
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = "8994067851:AAGVoAaaKTPhOY4EZn4gRm1tuOJ5yHU7YNo"

# MongoDB Connection Setup
MONGO_URI = "mongodb+srv://soniprashant671_db_user:ritik1103@cluster0.j5hwlec.mongodb.net/?appName=Cluster0"
mongo_client = None
settings_col = None
users_col = None

try:
    mongo_client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client["annas_downloader"]
    settings_col = db["settings"]
    users_col = db["users"]
    print("[mongodb] Connected successfully to Atlas cluster")
except Exception as e:
    print(f"[mongodb] Error initializing connection: {e}")

C_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(C_DIR, "config.json")
PM_USERS_PATH = os.path.join(C_DIR, "pm_users.json")
DL_PATH = os.path.join(C_DIR, "assets")
os.makedirs(DL_PATH, exist_ok=True)

executor = ThreadPoolExecutor(max_workers=20)

# Semaphore to limit concurrent downloads (max 3 at once)
dl_semaphore = asyncio.Semaphore(3)

# In-memory per-user state:  user_id -> {"results": [...]}
user_sessions: dict = {}

# ---------------------------------------------------------------------------
# Domain auto-discovery
# ---------------------------------------------------------------------------
DOMAIN_SOURCE = "https://shadowlibraries.github.io/DirectDownloads/AnnasArchive/"
FALLBACK_DOMAINS = [
    "https://annas-archive.se",
    "https://annas-archive.li",
    "https://annas-archive.gs",
    "https://annas-archive.gl",
    "https://annas-archive.pk",
    "https://annas-archive.gd",
    "https://annas-archive.org",
]
LIBGEN_SOURCE = "https://shadowlibraries.github.io/DirectDownloads/libgen/"
LIBGEN_FALLBACK_DOMAINS = [
    "http://libgen.li",
    "http://libgen.vg",
    "http://libgen.la",
    "http://libgen.bz",
    "http://libgen.gl",
]


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)


def _fetch_html_links(url: str) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; annadl/1.0)"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    p = _LinkParser()
    p.feed(html)
    return p.links


def _test_domain(domain: str) -> bool:
    """Check if a domain is reachable, using cloudscraper to bypass Cloudflare."""
    try:
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'linux', 'desktop': True}
        )
        try:
            resp = scraper.get(domain + "/", timeout=10)
            return resp.status_code < 400
        finally:
            scraper.close()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_cfg_cache = {}

def _init_cfg():
    global _cfg_cache
    local_cfg = {}
    if Path(CONFIG_PATH).is_file():
        try:
            with open(CONFIG_PATH) as f:
                local_cfg = json.load(f)
        except Exception:
            pass

    db_cfg = {}
    if settings_col is not None:
        try:
            doc = settings_col.find_one({"_id": "global_config"})
            if doc:
                db_cfg = {k: v for k, v in doc.items() if k != "_id"}
                print("[mongodb] Loaded config from database")
        except Exception as e:
            print(f"[mongodb] Error loading config from database: {e}")

    merged = {
        "owner_ids": [5450311131, 915392007],
        "service_enabled": True,
        "search_mode": "all",
        "delivery_mode": "group",
        "read_button": True,
        "welcome": {},
        "welcome_enabled": True,
        "auto_delete": False,
        "delete_time": 120,
        "dump_channel": None,
        "pm_search": True,
        "daily_dl_limit": 0,  # 0 = unlimited
    }
    merged.update(local_cfg)
    merged.update(db_cfg)
    _cfg_cache = merged

    # Persist locally for immediate availability and fallback
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(_cfg_cache, f, indent=2)
    except Exception:
        pass

    # Synchronously sync to MongoDB Atlas during initialization
    if settings_col is not None:
        try:
            settings_col.replace_one({"_id": "global_config"}, _cfg_cache, upsert=True)
            print("[mongodb] Synced local & remote config")
        except Exception as e:
            print(f"[mongodb] Error syncing config to database on init: {e}")

# Initialize config cache immediately on import/startup
_init_cfg()

def _load_cfg() -> dict:
    global _cfg_cache
    return _cfg_cache


def _save_cfg(cfg: dict):
    global _cfg_cache
    _cfg_cache = cfg
    # Save locally synchronously
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(_cfg_cache, f, indent=2)
    except Exception:
        pass
    
    # Save to MongoDB Atlas in the background
    if settings_col is not None:
        def _bg_save():
            try:
                settings_col.replace_one({"_id": "global_config"}, cfg, upsert=True)
            except Exception as e:
                logging.error(f"[mongodb] Error saving config in background: {e}")
        executor.submit(_bg_save)


def _get_cfg_value(key: str, default=None):
    return _load_cfg().get(key, default)


def _set_cfg_value(key: str, value):
    cfg = dict(_load_cfg())
    cfg[key] = value
    _save_cfg(cfg)


# PM User Tracking Cache and Helpers
_pm_users_cache = {}

def _init_pm_users():
    global _pm_users_cache
    if Path(PM_USERS_PATH).is_file():
        try:
            with open(PM_USERS_PATH) as f:
                data = json.load(f)
                _pm_users_cache = {int(k): v for k, v in data.items()}
        except Exception:
            pass

def _save_pm_users_local():
    global _pm_users_cache
    try:
        with open(PM_USERS_PATH, "w") as f:
            json.dump(_pm_users_cache, f, indent=2)
    except Exception:
        pass

# Initialize PM users cache
_init_pm_users()

def _db_register_pm_user(user_id: int, username: str, first_name: str):
    """Saves PM user registration to MongoDB in background."""
    global _pm_users_cache
    _pm_users_cache[user_id] = True
    _save_pm_users_local()
    if users_col is not None:
        try:
            users_col.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "user_id": user_id,
                        "username": username or "",
                        "first_name": first_name or "",
                        "started_at": time.time()
                    }
                },
                upsert=True
            )
        except Exception as e:
            logging.error(f"[mongodb] Error registering user: {e}")

def _db_has_started_pm(user_id: int) -> bool:
    """Synchronously checks cache first, then MongoDB if cache miss."""
    global _pm_users_cache
    if user_id in _pm_users_cache:
        return _pm_users_cache[user_id]
        
    if users_col is not None:
        try:
            doc = users_col.find_one({"user_id": user_id})
            if doc:
                _pm_users_cache[user_id] = True
                return True
        except Exception as e:
            logging.error(f"[mongodb] Error checking user in DB: {e}")
            
    return False

async def _register_pm_user(user_id: int, username: str, first_name: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_register_pm_user, user_id, username, first_name)

async def _has_started_pm(user_id: int) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _db_has_started_pm, user_id)

def _register_pm_if_private(update: Update):
    """Sync helper to auto-register user on any private chat update."""
    if update and update.effective_chat and update.effective_chat.type == "private":
        user = update.effective_user
        if user:
            executor.submit(_db_register_pm_user, user.id, user.username or "", user.first_name or "")


# ---------------------------------------------------------------------------
# Daily download limit tracking
# ---------------------------------------------------------------------------

# In-memory cache: user_id -> list of UTC timestamps (floats) of downloads in last 24h
_dl_timestamps_cache: dict = {}
_TWENTY_FOUR_HOURS = 86400  # seconds


def _db_get_dl_timestamps(user_id: int) -> list:
    """Load recent download timestamps from MongoDB for a user."""
    if users_col is None:
        return []
    try:
        doc = users_col.find_one({"user_id": user_id}, {"dl_timestamps": 1})
        if doc and "dl_timestamps" in doc:
            cutoff = time.time() - _TWENTY_FOUR_HOURS
            return [ts for ts in doc["dl_timestamps"] if ts > cutoff]
    except Exception as e:
        logging.error(f"[dl_limit] Error fetching timestamps for {user_id}: {e}")
    return []


def _db_append_dl_timestamp(user_id: int, ts: float):
    """Append a new download timestamp and trim entries older than 24h in MongoDB."""
    if users_col is None:
        return
    try:
        cutoff = ts - _TWENTY_FOUR_HOURS
        users_col.update_one(
            {"user_id": user_id},
            {
                "$push": {
                    "dl_timestamps": {
                        "$each": [ts],
                        "$slice": -500,  # safety cap – never store more than 500 raw entries
                    }
                },
                "$set": {"user_id": user_id},
            },
            upsert=True,
        )
        # Also prune stale timestamps from the stored list
        users_col.update_one(
            {"user_id": user_id},
            {"$pull": {"dl_timestamps": {"$lt": cutoff}}},
        )
    except Exception as e:
        logging.error(f"[dl_limit] Error appending timestamp for {user_id}: {e}")


def _get_user_dl_count_24h(user_id: int) -> int:
    """Return how many downloads this user has completed in the past 24 hours."""
    cutoff = time.time() - _TWENTY_FOUR_HOURS
    cached = _dl_timestamps_cache.get(user_id)

    # Warm cache from DB on first access
    if cached is None:
        cached = _db_get_dl_timestamps(user_id)
        _dl_timestamps_cache[user_id] = cached

    # Prune stale in-memory entries
    fresh = [ts for ts in cached if ts > cutoff]
    _dl_timestamps_cache[user_id] = fresh
    return len(fresh)


def _record_user_download(user_id: int):
    """Record a completed download for rate-limit accounting (sync, called in executor)."""
    ts = time.time()
    cutoff = ts - _TWENTY_FOUR_HOURS

    # Update in-memory cache
    cached = _dl_timestamps_cache.get(user_id, [])
    cached = [t for t in cached if t > cutoff]
    cached.append(ts)
    _dl_timestamps_cache[user_id] = cached

    # Persist to MongoDB
    _db_append_dl_timestamp(user_id, ts)


async def _check_dl_limit(user_id: int) -> tuple[bool, int, int]:
    """
    Check whether the user is within their daily download limit.

    Returns:
        (allowed: bool, used: int, limit: int)
        limit == 0 means unlimited.
    """
    limit = _get_cfg_value("daily_dl_limit", 0)
    if limit == 0:
        return True, 0, 0  # unlimited

    loop = asyncio.get_running_loop()
    used = await loop.run_in_executor(None, _get_user_dl_count_24h, user_id)
    return used < limit, used, limit


def get_active_domain() -> str:
    cfg = _load_cfg()
    cached = cfg.get("base_url", "").rstrip("/")
    if cached and _test_domain(cached):
        return cached
    try:
        links = _fetch_html_links(DOMAIN_SOURCE)
        candidates = [
            h.split("?")[0].rstrip("/")
            for h in links
            if "annas-archive" in h and "shadowlibraries" not in h and h.startswith("http")
        ]
        candidates = list(dict.fromkeys(candidates))
    except Exception:
        candidates = []
    for d in candidates or FALLBACK_DOMAINS:
        if _test_domain(d):
            cfg["base_url"] = d
            _save_cfg(cfg)
            return d
    return FALLBACK_DOMAINS[0]


def get_active_libgen_domain() -> str:
    cfg = _load_cfg()
    cached = cfg.get("libgen_base_url", "").rstrip("/")
    if cached and _test_domain(cached):
        return cached
    try:
        links = _fetch_html_links(LIBGEN_SOURCE)
        candidates = [
            h.split("?")[0].rstrip("/")
            for h in links
            if "libgen" in h and "shadowlibraries" not in h and h.startswith("http")
        ]
        candidates = list(dict.fromkeys(candidates))
    except Exception:
        candidates = []
    for d in candidates or LIBGEN_FALLBACK_DOMAINS:
        if _test_domain(d):
            cfg["libgen_base_url"] = d
            _save_cfg(cfg)
            return d
    return LIBGEN_FALLBACK_DOMAINS[0]


# ---------------------------------------------------------------------------
# Dynamic domain manager – auto-heals when a domain goes down
# ---------------------------------------------------------------------------
import threading

class _DomainManager:
    """Thread-safe, self-healing domain cache.

    On first access the domain is resolved exactly like the old startup code.
    When a caller reports a failure via `invalidate()`, the cached value is
    cleared so the *next* access triggers rediscovery – no restart needed.
    """

    def __init__(self, discover_fn, fallbacks, label):
        self._discover_fn = discover_fn
        self._fallbacks = fallbacks
        self._label = label
        self._lock = threading.Lock()
        self._value = None

    @property
    def url(self) -> str:
        if self._value is not None:
            return self._value
        with self._lock:
            if self._value is None:  # double-check after acquiring lock
                self._value = self._discover_fn()
                print(f"[{self._label}] resolved → {self._value}")
        return self._value

    def invalidate(self, bad_url: str | None = None):
        """Mark the current domain as dead so the next `.url` rediscovers."""
        with self._lock:
            if bad_url is None or self._value == bad_url:
                print(f"[{self._label}] invalidated {self._value!r}, will rediscover")
                self._value = None
                # Also clear the cached value in config so rediscovery is forced
                cfg = _load_cfg()
                cfg_key = "base_url" if "domain" in self._label else "libgen_base_url"
                if cfg.get(cfg_key):
                    cfg[cfg_key] = ""
                    _save_cfg(cfg)

    @property
    def fallbacks(self) -> list:
        return list(self._fallbacks)


_domain_mgr = _DomainManager(get_active_domain, FALLBACK_DOMAINS, "domain")
_libgen_mgr = _DomainManager(get_active_libgen_domain, LIBGEN_FALLBACK_DOMAINS, "libgen")

# Warm the caches eagerly (same behaviour as the old code)
print(f"[domain] {_domain_mgr.url}")
print(f"[libgen] {_libgen_mgr.url}")

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def make_bar(current: int, total: int, width: int = 18) -> str:
    if total <= 0:
        return "⏳ …"
    pct = current / total
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    done_mb = current / 1_048_576
    tot_mb = total / 1_048_576
    return f"[{bar}] {pct*100:.1f}%  ({done_mb:.1f} / {tot_mb:.1f} MB)"


def _esc(text: str) -> str:
    """Escape Telegram Markdown v1 special characters in dynamic content."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# ---------------------------------------------------------------------------
# Admin / state helpers
# ---------------------------------------------------------------------------

async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if the user is a bot owner or a group/supergroup admin."""
    user_id = update.effective_user.id
    # Support both legacy single owner_id and new owner_ids list
    owner_ids = _get_cfg_value("owner_ids", [])
    if isinstance(owner_ids, (int, str)):
        owner_ids = [int(owner_ids)]
    if user_id in [int(oid) for oid in owner_ids]:
        return True
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        try:
            member = await context.bot.get_chat_member(chat.id, user_id)
            return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))
        except Exception:
            pass
    return False


def _is_service_enabled() -> bool:
    return _get_cfg_value("service_enabled", True)


def _get_search_mode() -> str:
    """Returns: all | slash | hashtag | text"""
    return _get_cfg_value("search_mode", "all")


def _get_delivery_mode() -> str:
    """Returns: pm | group"""
    return _get_cfg_value("delivery_mode", "group")


def _get_read_button() -> bool:
    return _get_cfg_value("read_button", True)


def _get_connected_group():
    val = _get_cfg_value("connected_group_id")
    return int(val) if val else None


def _is_chat_allowed(update: Update, allow_connect: bool = False) -> bool:
    if not update or not update.effective_chat:
        return True
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        connected = _get_connected_group()
        if connected is None:
            return allow_connect
        return (chat.id == connected) or allow_connect
    return True


# ---------------------------------------------------------------------------
# Sync worker: search
# ---------------------------------------------------------------------------

def _sync_search(query: str, count: int = 10) -> list:
    from urllib.parse import quote_plus
    quoted_query = quote_plus(query)

    # Build ordered list: current cached domain first, then all fallbacks
    primary = _domain_mgr.url
    domains_to_try = [primary] + [
        d for d in _domain_mgr.fallbacks if d != primary
    ]

    # Also try to fetch live domains from shadowlibraries (appended at end)
    try:
        live_links = _fetch_html_links(DOMAIN_SOURCE)
        live_domains = [
            h.split("?")[0].rstrip("/")
            for h in live_links
            if "annas-archive" in h and "shadowlibraries" not in h and h.startswith("http")
        ]
        for ld in live_domains:
            if ld not in domains_to_try:
                domains_to_try.append(ld)
    except Exception:
        pass

    resp = None
    working_domain = primary

    # Fresh scraper per search with Chrome fingerprint — avoids stale TLS
    # fingerprints and Cloudflare blocks.
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'linux', 'desktop': True}
    )
    try:
        for domain in domains_to_try:
            try:
                url = f"{domain}/search?index=&page=1&sort=&src=lgli&display=&q={quoted_query}"
                resp = scraper.get(url, timeout=20)
                if resp.status_code == 200:
                    working_domain = domain
                    break
            except Exception as exc:
                logging.warning(f"[search] {domain} failed: {exc}")
                if domain == primary:
                    _domain_mgr.invalidate(domain)
                resp = None
                continue
    finally:
        scraper.close()

    if resp is None or resp.status_code != 200:
        raise ConnectionError("All Anna's Archive domains are unreachable")

    # If a fallback domain succeeded, promote it as the new cached primary
    if working_domain != primary:
        cfg = _load_cfg()
        cfg["base_url"] = working_domain
        _save_cfg(cfg)
        _domain_mgr.invalidate()  # force reload from config next time
        logging.info(f"[search] Switched primary domain → {working_domain}")

    soup = BeautifulSoup(resp.text, "html.parser")
    links = soup.find_all("a", class_=["js-vim-focus", "custom-a"])
    links = [
        lnk for lnk in links
        if "js-vim-focus" in lnk.get("class", []) and "custom-a" in lnk.get("class", [])
    ]

    results = []
    for lnk in links[:count]:
        try:
            href = lnk.get("href") or ""
            if href and not href.startswith("http"):
                href = urljoin(working_domain, href)
            title = lnk.get_text().strip()

            container = lnk
            for _ in range(5):
                parent = container.parent
                if not parent:
                    break
                cls = " ".join(parent.get("class", []))
                if "flex" in cls and ("pt-3" in cls or "border-b" in cls):
                    container = parent
                    break
                container = parent

            all_text = container.get_text() if container else ""

            author = "Unknown"
            if container:
                for a_tag in container.find_all("a"):
                    hv = a_tag.get("href") or ""
                    tx = a_tag.get_text().strip()
                    if "search?q=" in hv and tx and tx != title:
                        if len(tx.split(",")) <= 2 and len(tx.split()) <= 4:
                            author = tx
                            break

            year = "Unknown"
            ym = re.search(r"\b(19|20)\d{2}\b", all_text)
            if ym:
                year = ym.group(0)

            language = "Unknown"
            lm = re.search(r"(\w+)\s+\[[a-z]{2}\]", all_text)
            if lm:
                language = lm.group(1)

            fmt = "Unknown"
            fm = re.search(r"\b(EPUB|PDF|MOBI|AZW3|TXT|DOC|DOCX)\b", all_text, re.I)
            if fm:
                fmt = fm.group(1).upper()

            size = "Unknown"
            sm = re.search(r"(\d+\.?\d*\s*[MKG]B)", all_text, re.I)
            if sm:
                size = sm.group(1)

            md5 = ""
            md5_m = re.search(r"/md5/([a-fA-F0-9]{32})", href)
            if md5_m:
                md5 = md5_m.group(1)

            results.append(
                dict(url=href, title=title, author=author, year=year,
                     language=language, format=fmt, size=size, md5=md5)
            )
        except Exception:
            pass

    return results


# ---------------------------------------------------------------------------
# Sync worker: resolve libgen direct download link
# ---------------------------------------------------------------------------

def _sync_get_direct_url(book_url: str) -> str:
    md5_m = re.search(r"/md5/([a-fA-F0-9]{32})", book_url)
    if not md5_m:
        raise ValueError("Could not extract MD5 from book URL")
    md5 = md5_m.group(1)

    # Build ordered list: current cached libgen domain first, then fallbacks
    primary = _libgen_mgr.url
    domains_to_try = [primary] + [
        d for d in _libgen_mgr.fallbacks if d != primary
    ]

    last_err = None
    for domain in domains_to_try:
        try:
            ads_url = f"{domain}/ads.php?md5={md5}"
            req = urllib.request.Request(
                ads_url, headers={"User-Agent": "Mozilla/5.0 (compatible; annadl/1.0)"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            m = re.search(r'href=[^>]*?(get\.php\?md5=[^\"\'> ]+)', html)
            if not m:
                raise ValueError("Could not find get.php link in libgen ads page")

            path = m.group(1)
            direct = path if path.startswith("http") else urljoin(ads_url, path)

            # If a fallback domain succeeded, promote it
            if domain != primary:
                cfg = _load_cfg()
                cfg["libgen_base_url"] = domain
                _save_cfg(cfg)
                _libgen_mgr.invalidate()
                logging.info(f"[libgen] Switched primary domain → {domain}")

            return direct
        except Exception as exc:
            logging.warning(f"[libgen] {domain} failed: {exc}")
            if domain == primary:
                _libgen_mgr.invalidate(domain)
            last_err = exc
            continue

    raise last_err or ConnectionError("All libgen domains are unreachable")


# ---------------------------------------------------------------------------
# Sync worker: stream download with progress callback
# ---------------------------------------------------------------------------

def _sync_download(url: str, dest_dir: str, on_progress=None) -> tuple:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; annadl/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        cd = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename[^;=\n]*=[ ]*["\']?([^"\';\n]+)', cd)
        fname = (
            m.group(1).strip().strip('"').strip("'")
            if m
            else (url.split("?")[0].rstrip("/").split("/")[-1] or "download")
        )

        # Make the filename on disk completely unique to prevent concurrent download collisions
        name_part, ext_part = os.path.splitext(fname)
        truncated_name = name_part[:150] + ext_part
        dest_fname = f"{uuid.uuid4().hex}_{truncated_name}"
        dest = os.path.join(dest_dir, dest_fname)
        downloaded = 0
        last_pct = -1

        with open(dest, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                if on_progress and total:
                    pct = int(downloaded / total * 100)
                    if pct != last_pct and pct % 5 == 0:
                        last_pct = pct
                        on_progress(downloaded, total)

    return dest, fname


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)

    # Check for deep linking
    if context.args and len(context.args) > 0:
        arg = context.args[0].strip()
        match = re.match(r'^md5_?([a-fA-F0-9]{32})$', arg, re.IGNORECASE)
        if match:
            if not _is_service_enabled():
                await update.message.reply_text("🔴 Bot service is currently offline.")
                return
            md5 = match.group(1)
            # Before downloading, register them (so future PM checks pass)
            user = update.effective_user
            if user:
                await _register_pm_user(user.id, user.username or "", user.first_name or "")
            
            # Start download/delivery
            await _process_md5_download(md5, update, context)
            return

    cfg = _load_cfg()
    welcome = cfg.get("welcome", {})
    welcome_text = welcome.get("text", "")
    welcome_photo = welcome.get("photo_file_id", None)

    if not welcome_text:
        welcome_text = (
            "📚 *Prashant's Pages Bot*\n\n"
            "Send me a book title or author name to search\\.\n"
            "Example: `Harry Potter`\n\n"
            "Use /help to see all available commands\\."
        )

    try:
        if welcome_photo:
            await update.message.reply_photo(
                photo=welcome_photo,
                caption=welcome_text,
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(welcome_text, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(welcome_text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    search_mode = _get_search_mode()
    delivery_mode = _get_delivery_mode()
    service = "✅ online" if _is_service_enabled() else "🔴 offline"
    connected = _get_connected_group()
    connected_str = f"`{connected}`" if connected else "None"

    auto_delete = _get_cfg_value("auto_delete", False)
    auto_delete_str = "✅ enabled" if auto_delete else "❌ disabled"

    delete_time = _get_cfg_value("delete_time", 120)
    if delete_time >= 3600 and delete_time % 3600 == 0:
        delete_time_str = f"{delete_time // 3600}h"
    elif delete_time >= 60 and delete_time % 60 == 0:
        delete_time_str = f"{delete_time // 60}m"
    else:
        delete_time_str = f"{delete_time}s"

    dump_channel = _get_cfg_value("dump_channel")
    dump_channel_str = f"`{dump_channel}`" if dump_channel else "None"

    pm_search = _get_cfg_value("pm_search", True)
    pm_search_str = "✅ enabled" if pm_search else "❌ disabled"

    daily_limit = _get_cfg_value("daily_dl_limit", 0)
    daily_limit_str = "♾️ unlimited" if daily_limit == 0 else f"{daily_limit} per 24h"

    welcome_enabled = _get_cfg_value("welcome_enabled", True)
    welcome_status_str = "✅ enabled" if welcome_enabled else "❌ disabled"

    mode_desc = {
        "all": "All modes active",
        "slash": "Only /search command",
        "hashtag": "Only #request prefix",
        "text": "Only plain text messages",
    }.get(search_mode, search_mode)

    text = (
        "📚 *Prashant's Pages Bot — Help*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*🔍 Search*\n"
        "• `/search <query>` — slash command search\n"
        "• `#request <book>` — hashtag search \\(groups\\)\n"
        "• Plain text — send book name directly\n"
        "• `/md5_<md5>` — download a book by its MD5 link\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*⚙️ Admin Commands*\n"
        "• `/mode pm|group` — set file delivery target\n"
        "• `/mode search slash|hashtag|text|all` — search mode\n"
        "• `/connect` — register this group as delivery target\n"
        "• `/setwelcome <text>` — set welcome message\n"
        "• `/welcomeoff` — disable welcome message\n"
        "• `/welcomeon` — enable welcome message\n"
        "  _Reply to a photo to include an image_\n"
        "• `/service on|off` — enable or disable the bot\n"
        "• `/auto_delete on|off` — enable/disable auto-delete in PM\n"
        "• `/deletetime <time>` — set delete delay \\(e.g., `2m`, `120s`, `1h`\\)\n"
        "• `/pm_search on|off` — enable/disable searching in PM\n"
        "• `/dump <channel_id>|off` — set or disconnect dump destination\n"
        "• `/dailylimit <N>` — set max downloads per user per 24h \\(0 = unlimited\\)\n"
        "• `/broadcast` — send a message to all bot users \\(PM only\\)\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*📊 Current Status*\n"
        f"• Service: {service}\n"
        f"• Delivery mode: `{delivery_mode}`\n"
        f"• Search mode: `{search_mode}` — {mode_desc}\n"
        f"• PM searching: `{pm_search_str}`\n"
        f"• Connected group: {connected_str}\n"
        f"• Auto-delete in PM: `{auto_delete_str}`\n"
        f"• Delete delay: `{delete_time_str}` \\({delete_time}s\\)\n"
        f"• Dump destination: {dump_channel_str}\n"
        f"• Daily download limit: `{daily_limit_str}`\n"
        f"• Welcome greeting: `{welcome_status_str}`\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        current = "on" if _get_read_button() else "off"
        await update.message.reply_text(
            f"Usage: `/read on|off`\nCurrent: `{current}`",
            parse_mode="Markdown"
        )
        return

    enabled = args[0].lower() == "on"
    _set_cfg_value("read_button", enabled)
    status = "✅ enabled" if enabled else "❌ disabled"
    await update.message.reply_text(
        f"📖 Read Online button: {status}",
        parse_mode="Markdown"
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args:
        delivery = _get_delivery_mode()
        search = _get_search_mode()
        await update.message.reply_text(
            f"*Current modes:*\n"
            f"• Delivery: `{delivery}` — use `/mode pm` or `/mode group`\n"
            f"• Search: `{search}` — use `/mode search slash|hashtag|text|all`",
            parse_mode="Markdown"
        )
        return

    if args[0].lower() in ("pm", "group"):
        mode = args[0].lower()
        _set_cfg_value("delivery_mode", mode)
        icon = "💬" if mode == "pm" else "👥"
        await update.message.reply_text(
            f"{icon} Delivery mode set to: `{mode}`",
            parse_mode="Markdown"
        )
        return

    if args[0].lower() == "search":
        valid = ("slash", "hashtag", "text", "all")
        if len(args) < 2 or args[1].lower() not in valid:
            current = _get_search_mode()
            await update.message.reply_text(
                f"Usage: `/mode search slash|hashtag|text|all`\nCurrent: `{current}`\n\n"
                f"• `slash` — only `/search <query>`\n"
                f"• `hashtag` — only `#request <query>` in groups\n"
                f"• `text` — only plain text messages\n"
                f"• `all` — all three modes active",
                parse_mode="Markdown"
            )
            return
        search_mode = args[1].lower()
        _set_cfg_value("search_mode", search_mode)
        await update.message.reply_text(
            f"🔍 Search mode set to: `{search_mode}`",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        "Usage:\n"
        "• `/mode pm|group` — delivery target\n"
        "• `/mode search slash|hashtag|text|all` — search mode",
        parse_mode="Markdown"
    )


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "❌ `/connect` must be run inside a group chat.",
            parse_mode="Markdown"
        )
        return

    _set_cfg_value("connected_group_id", chat.id)
    await update.message.reply_text(
        f"✅ This group (*{_esc(chat.title or str(chat.id))}*) is now connected as the delivery target\\.\n\n"
        f"Use `/mode group` to route downloads here\\.",
        parse_mode="Markdown"
    )


async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    text = " ".join(context.args).strip()
    photo_file_id = None

    # Check if the command is a reply to a photo message
    replied = update.message.reply_to_message
    if replied and replied.photo:
        photo_file_id = replied.photo[-1].file_id

    if not text and not photo_file_id:
        cfg = _load_cfg()
        current = cfg.get("welcome", {})
        current_text = current.get("text") or "(not set)"
        current_photo = "yes 🖼️" if current.get("photo_file_id") else "no"
        await update.message.reply_text(
            f"Usage: `/setwelcome Your welcome text here`\n"
            f"To include a photo: reply to a photo with `/setwelcome Your text`\n\n"
            f"Current text: _{_esc(current_text)}_\n"
            f"Has photo: {current_photo}\n\n"
            f"You can use `{{name}}` as a placeholder for the new member's name\\.",
            parse_mode="Markdown"
        )
        return

    cfg = _load_cfg()
    welcome = cfg.get("welcome", {})
    if text:
        welcome["text"] = text
    if photo_file_id:
        welcome["photo_file_id"] = photo_file_id
    cfg["welcome"] = welcome
    _save_cfg(cfg)

    photo_note = " \\(with photo 🖼️\\)" if photo_file_id else ""
    preview = text or welcome.get("text", "")
    await update.message.reply_text(
        f"✅ Welcome message updated{photo_note}:\n\n_{_esc(preview)}_",
        parse_mode="Markdown"
    )


async def cmd_welcomeoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /welcomeoff — disable the welcome message."""
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return
    _set_cfg_value("welcome_enabled", False)
    await update.message.reply_text("🔕 Welcome message has been *disabled*.\nUse /welcomeon to re-enable.", parse_mode="Markdown")


async def cmd_welcomeon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /welcomeon — enable the welcome message."""
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return
    _set_cfg_value("welcome_enabled", True)
    await update.message.reply_text("🔔 Welcome message has been *enabled*.\nUse /welcomeoff to disable.", parse_mode="Markdown")


async def cmd_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        current = "on" if _is_service_enabled() else "off"
        await update.message.reply_text(
            f"Usage: `/service on|off`\nCurrent: `{current}`",
            parse_mode="Markdown"
        )
        return

    enabled = args[0].lower() == "on"
    _set_cfg_value("service_enabled", enabled)
    status = "✅ Service is now *online*" if enabled else "🔴 Service is now *offline*"
    await update.message.reply_text(status, parse_mode="Markdown")


async def cmd_auto_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        current = "on" if _get_cfg_value("auto_delete", False) else "off"
        await update.message.reply_text(
            f"Usage: `/auto_delete on|off`\nCurrent auto-delete setting: `{current}`",
            parse_mode="Markdown"
        )
        return

    enabled = args[0].lower() == "on"
    _set_cfg_value("auto_delete", enabled)
    status = "✅ Auto-delete in PM mode is now *enabled*" if enabled else "❌ Auto-delete in PM mode is now *disabled*"
    await update.message.reply_text(status, parse_mode="Markdown")


async def cmd_deletetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args:
        current_seconds = _get_cfg_value("delete_time", 120)
        # format it nicely
        if current_seconds >= 3600 and current_seconds % 3600 == 0:
            formatted = f"{current_seconds // 3600}h"
        elif current_seconds >= 60 and current_seconds % 60 == 0:
            formatted = f"{current_seconds // 60}m"
        else:
            formatted = f"{current_seconds}s"
        await update.message.reply_text(
            f"Usage: `/deletetime <time>` (e.g. `2m`, `120s`, `1h`)\nCurrent delete time: `{formatted}` ({current_seconds} seconds)",
            parse_mode="Markdown"
        )
        return

    time_str = args[0].strip().lower()
    match = re.match(r'^(\d+)\s*([smh]?)$', time_str)
    if not match:
        await update.message.reply_text(
            "❌ Invalid time format. Please use a number followed by `s` (seconds), `m` (minutes), or `h` (hours).\n"
            "Examples: `120s`, `2m`, `1h`",
            parse_mode="Markdown"
        )
        return

    value = int(match.group(1))
    unit = match.group(2) or "s"

    if unit == "h":
        seconds = value * 3600
    elif unit == "m":
        seconds = value * 60
    else:
        seconds = value

    if seconds <= 0:
        await update.message.reply_text("❌ Delete time must be greater than 0.")
        return

    _set_cfg_value("delete_time", seconds)
    
    # format back nicely
    if seconds >= 3600 and seconds % 3600 == 0:
        formatted = f"{seconds // 3600}h"
    elif seconds >= 60 and seconds % 60 == 0:
        formatted = f"{seconds // 60}m"
    else:
        formatted = f"{seconds}s"

    await update.message.reply_text(
        f"⏳ Delete time successfully set to: `{formatted}` ({seconds} seconds).",
        parse_mode="Markdown"
    )


async def cmd_dump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args:
        current = _get_cfg_value("dump_channel")
        await update.message.reply_text(
            f"Usage: `/dump <channel_id_or_group_id>` (e.g. `/dump -1001234567890`) or `/dump off` to disconnect\n"
            f"Current dump destination: `{current or 'None'}`",
            parse_mode="Markdown"
        )
        return

    target = args[0].strip()
    if target.lower() in ("off", "none", "disable", "disconnect"):
        _set_cfg_value("dump_channel", None)
        await update.message.reply_text(
            "🔌 Dump destination disconnected successfully.",
            parse_mode="Markdown"
        )
        return

    # Try converting to integer in case they supplied a numerical group/channel ID
    try:
        if target.startswith("-"):
            channel_id = int(target)
        else:
            channel_id = int(target) if target.isdigit() else target
    except ValueError:
        channel_id = target

    _set_cfg_value("dump_channel", channel_id)

    await update.message.reply_text(
        f"✅ Dump channel successfully set to: `{channel_id}`.\n\n"
        f"⚠️ **IMPORTANT REMINDER:**\n"
        f"Please make sure you have added the bot to this channel/group as an **Administrator** with permission to post/send messages and documents! "
        f"If the bot is not an admin, it will not be able to dump books there.",
        parse_mode="Markdown"
    )


async def cmd_dailylimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args

    # No args — show current setting
    if not args:
        current = _get_cfg_value("daily_dl_limit", 0)
        if current == 0:
            status_str = "♾️ *unlimited*"
        else:
            status_str = f"*{current} downloads per 24 hours*"
        await update.message.reply_text(
            f"Usage: `/dailylimit <number>` or `/dailylimit 0` for unlimited\n"
            f"Current daily download limit: {status_str}\n\n"
            f"Examples:\n"
            f"• `/dailylimit 5` — each user can download 5 books per 24h\n"
            f"• `/dailylimit 0` — no limit \\(unlimited\\)\n\n"
            f"_Bot owners are always exempt from this limit\\._",
            parse_mode="Markdown"
        )
        return

    # Validate argument
    raw = args[0].strip()
    if not raw.isdigit():
        await update.message.reply_text(
            "❌ Invalid value\\. Please provide a whole number \\(e\\.g\\. `5`\\) or `0` for unlimited\\.",
            parse_mode="Markdown"
        )
        return

    limit = int(raw)
    if limit < 0:
        await update.message.reply_text("❌ Limit cannot be negative.")
        return

    _set_cfg_value("daily_dl_limit", limit)

    if limit == 0:
        await update.message.reply_text(
            "♾️ Daily download limit *removed*\\. All users can now download unlimited books\\.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"✅ Daily download limit set to *{limit} book{'s' if limit != 1 else ''} per 24 hours* per user\\.\n\n"
            f"_Bot owners are always exempt from this limit\\._",
            parse_mode="Markdown"
        )


async def cmd_pm_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        current = "on" if _get_cfg_value("pm_search", True) else "off"
        await update.message.reply_text(
            f"Usage: `/pm_search on|off`\nCurrent PM search setting: `{current}`",
            parse_mode="Markdown"
        )
        return

    enabled = args[0].lower() == "on"
    _set_cfg_value("pm_search", enabled)
    status = "✅ Searching in PM is now *enabled*" if enabled else "❌ Searching in PM is now *disabled*"
    await update.message.reply_text(status, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Broadcast command  (admin + private chat only, two-step ConversationHandler)
# ---------------------------------------------------------------------------

BROADCAST_WAITING = 1   # ConversationHandler state


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1 — admin triggers /broadcast in PM."""
    # Private chat only
    if update.effective_chat.type != "private":
        await update.message.reply_text("❌ /broadcast can only be used in my private chat.")
        return

    # Admin only
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    await update.message.reply_text(
        "📢 *Broadcast mode*\n\n"
        "Send me the message you want to broadcast to all bot users\\.\n"
        "It can be *text, a photo, a document, a video* — any message type\\.\n\n"
        "Send /cancel to abort\\.",
        parse_mode="Markdown",
    )
    return BROADCAST_WAITING


async def cmd_broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the broadcast conversation."""
    await update.message.reply_text("❌ Broadcast cancelled.")
    return ConversationHandler.END


async def _do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Step 2 — admin sends the message to broadcast.
    Copies it to every registered PM user and shows a live progress status.
    """
    # Fetch all registered user IDs from MongoDB (primary source)
    user_ids: list[int] = []

    if users_col is not None:
        try:
            loop = asyncio.get_running_loop()
            docs = await loop.run_in_executor(
                None,
                lambda: list(users_col.find({}, {"user_id": 1}))
            )
            user_ids = [int(d["user_id"]) for d in docs if "user_id" in d]
        except Exception as e:
            logging.error(f"[broadcast] MongoDB fetch error: {e}")

    # Fallback: local pm_users.json cache if MongoDB gave nothing
    if not user_ids:
        user_ids = list(_pm_users_cache.keys())

    if not user_ids:
        await update.message.reply_text("❌ No registered users found to broadcast to.")
        return ConversationHandler.END

    total    = len(user_ids)
    sent     = 0
    failed   = 0
    blocked  = 0  # users who blocked the bot
    not_started = 0  # bot never started by them (shouldn't happen, but safety net)

    # Post the live status message
    status_msg = await update.message.reply_text(
        f"📡 *Broadcasting…*\n\n"
        f"👥 Total users: `{total}`\n"
        f"✅ Sent: `0`\n"
        f"❌ Failed: `0`\n"
        f"🚫 Blocked: `0`",
        parse_mode="Markdown",
    )

    last_edit_time = 0.0
    EDIT_INTERVAL  = 3.0   # update status every 3 seconds to avoid flood limits

    async def _update_status(force: bool = False):
        nonlocal last_edit_time
        now = time.monotonic()
        if not force and (now - last_edit_time) < EDIT_INTERVAL:
            return
        last_edit_time = now
        done = sent + failed
        pct  = int(done / total * 100) if total else 0
        bar_filled = int(18 * pct / 100)
        bar = "█" * bar_filled + "░" * (18 - bar_filled)
        try:
            await status_msg.edit_text(
                f"📡 *Broadcasting…*\n\n"
                f"[{bar}] {pct}%\n\n"
                f"👥 Total users: `{total}`\n"
                f"✅ Sent: `{sent}`\n"
                f"❌ Failed: `{failed}` \\(blocked: `{blocked}`\\)\n"
                f"📬 Remaining: `{total - done}`",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    # Broadcast loop — copy_message preserves all media/formatting perfectly
    for uid in user_ids:
        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            sent += 1
        except Exception as e:
            err_str = str(e).lower()
            if "blocked" in err_str or "user is deactivated" in err_str or "bot was blocked" in err_str:
                blocked += 1
            elif "chat not found" in err_str or "not found" in err_str:
                not_started += 1
            failed += 1

        await _update_status()

        # Small delay to stay well inside Telegram's 30 msg/sec global limit
        await asyncio.sleep(0.05)

    # Final status — force update
    await _update_status(force=True)

    summary = (
        f"✅ *Broadcast complete\\!*\n\n"
        f"👥 Total users: `{total}`\n"
        f"✅ Successfully sent: `{sent}`\n"
        f"❌ Failed: `{failed}`\n"
        f"   ├ 🚫 Bot blocked by user: `{blocked}`\n"
        f"   └ 👻 User never started bot: `{not_started}`"
    )
    try:
        await status_msg.edit_text(summary, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(summary, parse_mode="Markdown")

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Search handlers
# ---------------------------------------------------------------------------

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    if not _is_service_enabled():
        await update.message.reply_text("🔴 Bot service is currently offline.")
        return

    search_mode = _get_search_mode()
    if search_mode not in ("slash", "all"):
        await update.message.reply_text(
            f"❌ Slash search is disabled. Current mode: `{search_mode}`\n"
            f"An admin can change it with `/mode search all`",
            parse_mode="Markdown"
        )
        return

    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "Usage: /search <book title or author>\nExample: /search Dune"
        )
        return
    await _run_search(update, context, query)


async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    if not _is_service_enabled():
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    search_mode = _get_search_mode()

    # --- Hashtag mode: #request <book> ---
    if re.match(r"^#request\b", text, re.IGNORECASE):
        if search_mode in ("hashtag", "all"):
            query = re.sub(r"^#request\s*", "", text, flags=re.IGNORECASE).strip()
            if query:
                await _run_search(update, context, query)
        return

    # --- Plain text mode ---
    if not text.startswith("/"):
        if search_mode in ("text", "all"):
            await _run_search(update, context, text)


async def _run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    # Check if searching in PM is disabled
    if update.effective_chat.type == "private":
        pm_search = _get_cfg_value("pm_search", True)
        if not pm_search:
            await update.message.reply_text(
                "❌ Searching in private messages is currently disabled by the administrator.",
                parse_mode="Markdown"
            )
            return

    user_id = update.effective_user.id
    status = await update.message.reply_text(
        f"🔍 Searching for *{_esc(query)}*…", parse_mode="Markdown"
    )

    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(executor, lambda: _sync_search(query, 10))
    except Exception as e:
        await status.edit_text(f"❌ Search error: {e}")
        return

    if not results:
        await status.edit_text("❌ No results found. Try a different query.")
        return

    user_sessions[user_id] = {"results": results}

    bot_info = await context.bot.get_me()
    bot_username = bot_info.username

    # Check if the user has started the bot in PM (or if the chat is private)
    pm_started = (update.effective_chat.type == "private") or await _has_started_pm(user_id)

    lines = [f"📚 *Results for \"{_esc(query)}\"*\n"]

    for i, b in enumerate(results):
        fmt = b['format'].upper() if b['format'] else "UNKNOWN"
        size = b['size'].lower() if b['size'] else "unknown"
        md5 = b.get("md5", "")
        if md5:
            if pm_started:
                md5_link = f"/md5\\_{md5}"
            else:
                md5_link = f"[/start\\_md5\\_{md5}](https://t.me/{bot_username}?start=md5_{md5})"
        else:
            md5_link = ""

        lines.append(
            f"*{i+1}.* 📚 *{_esc(b['title'].upper())} ({_esc(fmt)})*\n"
            f"   {_esc(b['author'])}\n"
            f"   🌐 {_esc(b['language'])}\n"
            f"   {md5_link} ({_esc(fmt.lower())}, {_esc(size)})\n"
        )

    await status.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Callback: Download
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Download & Delivery Core Helper
# ---------------------------------------------------------------------------

async def _process_md5_download(md5: str, update: Update, context: ContextTypes.DEFAULT_TYPE, book: dict = None):
    chat_id = update.effective_chat.id
    clicker_id = update.effective_user.id
    loop = asyncio.get_running_loop()

    if not book:
        # Find book metadata in sessions
        for sess in user_sessions.values():
            for b in sess.get("results", []):
                if b.get("md5") == md5:
                    book = b
                    break
            if book:
                break

    if not book:
        # Fallback: search Anna's Archive for the MD5
        try:
            results = await loop.run_in_executor(executor, lambda: _sync_search(md5, 1))
            if results:
                book = results[0]
        except Exception:
            pass

    if not book:
        book = {
            "title": f"Book_{md5[:8]}",
            "author": "Unknown",
            "year": "Unknown",
            "language": "Unknown",
            "format": "Unknown",
            "size": "Unknown",
            "md5": md5,
            "url": f"{_domain_mgr.url}/md5/{md5}"
        }

    # ---- Daily download limit check ----
    # Owners are exempt from the limit
    owner_ids = _get_cfg_value("owner_ids", [])
    if isinstance(owner_ids, (int, str)):
        owner_ids = [int(owner_ids)]
    is_owner = clicker_id in [int(oid) for oid in owner_ids]

    if not is_owner:
        allowed, used, limit = await _check_dl_limit(clicker_id)
        if not allowed:
            # Calculate reset time (seconds until oldest timestamp expires)
            cutoff = time.time() - _TWENTY_FOUR_HOURS
            cached_ts = _dl_timestamps_cache.get(clicker_id, [])
            fresh_ts = sorted([t for t in cached_ts if t > cutoff])
            if fresh_ts:
                resets_in = int(fresh_ts[0] + _TWENTY_FOUR_HOURS - time.time())
                h, rem = divmod(resets_in, 3600)
                m, s = divmod(rem, 60)
                reset_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
            else:
                reset_str = "less than 24h"

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⛔ *Daily download limit reached!*\n\n"
                    f"You have used *{used}/{limit}* downloads in the last 24 hours\\.\n"
                    f"Your limit resets in: `{reset_str}`\n\n"
                    f"_Contact an admin if you need a higher limit\\._"
                ),
                parse_mode="Markdown",
            )
            return

    # Determine delivery target chat
    delivery_mode = _get_delivery_mode()
    if delivery_mode == "pm":
        target_chat_id = clicker_id          # Send to the user who clicked
        
        # 1. Fast check from MongoDB/Cache
        pm_started = await _has_started_pm(clicker_id)
        
        # 2. If not registered, attempt quiet text message check (self-healing)
        if not pm_started:
            try:
                bot_info = await context.bot.get_me()
                bot_username = bot_info.username
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=f"⏳ *Preparing download for:* {_esc(book['title'])}",
                    parse_mode="Markdown"
                )
                # Success! Auto-register them in DB for subsequent requests
                await _register_pm_user(
                    clicker_id, 
                    update.effective_user.username or "", 
                    update.effective_user.first_name or ""
                )
            except Exception:
                # User has definitely not started bot in PM
                bot_info = await context.bot.get_me()
                bot_username = bot_info.username
                keyboard = [
                    [
                        InlineKeyboardButton(
                            text="🚀 Start Bot & Get Book",
                            url=f"https://t.me/{bot_username}?start=md5_{md5}"
                        )
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ *Could not deliver to DM!*\n\n"
                        f"Hi {update.effective_user.mention_markdown()},\n"
                        f"Please start the bot in PM first to receive files.\n\n"
                        f"Click the button below to start the bot in PM and receive your file instantly!"
                    ),
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
                return
        else:
            # We already know they started the bot. Send preparing message.
            try:
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=f"⏳ *Preparing download for:* {_esc(book['title'])}",
                    parse_mode="Markdown"
                )
            except Exception:
                # Fallback in case they blocked the bot after starting it
                bot_info = await context.bot.get_me()
                bot_username = bot_info.username
                keyboard = [
                    [
                        InlineKeyboardButton(
                            text="🚀 Start Bot & Get Book",
                            url=f"https://t.me/{bot_username}?start=md5_{md5}"
                        )
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ *Could not deliver to DM!*\n\n"
                        f"Hi {update.effective_user.mention_markdown()},\n"
                        f"Please start the bot in PM first to receive files.\n\n"
                        f"Click the button below to start the bot in PM and receive your file instantly!"
                    ),
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
                return
    else:
        connected = _get_connected_group()
        target_chat_id = connected if connected else chat_id

    # ---- Step 1: resolve direct URL ----
    status = await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏳ *Getting download link for:*\n{_esc(book['title'])}",
        parse_mode="Markdown",
    )

    try:
        direct_url = await loop.run_in_executor(
            executor, lambda: _sync_get_direct_url(book["url"])
        )
    except Exception as e:
        await status.edit_text(f"❌ Could not get download link:\n`{e}`", parse_mode="Markdown")
        return

    # ---- Step 2: download with progress (semaphore limits concurrency) ----
    async with dl_semaphore:
        last_bar: list = [""]
        last_edit_time: list = [0.0]
        download_active = [True]

        def on_dl_progress(done: int, total: int):
            bar = make_bar(done, total)
            if bar == last_bar[0]:
                return
            last_bar[0] = bar

            async def _edit():
                if not download_active[0]:
                    return
                now = time.monotonic()
                if now - last_edit_time[0] < 2.5:
                    return
                last_edit_time[0] = now
                try:
                    await status.edit_text(
                        f"⬇️ *Downloading:* {_esc(book['title'])}\n{bar}",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

            asyncio.run_coroutine_threadsafe(_edit(), loop)

        await status.edit_text(
            f"⬇️ *Downloading:* {_esc(book['title'])}\n{make_bar(0, 1)}",
            parse_mode="Markdown",
        )

        try:
            file_path, fname = await loop.run_in_executor(
                executor,
                lambda: _sync_download(direct_url, DL_PATH, on_dl_progress),
            )
        except Exception as e:
            download_active[0] = False
            await status.edit_text(f"❌ Download failed:\n`{e}`", parse_mode="Markdown")
            return
        finally:
            download_active[0] = False

    # ---- Step 3: upload with progress ----
    file_size = os.path.getsize(file_path)
    ul_last_time = [0.0]
    ul_last_pct = [-1]

    async def on_ul_progress(current: int, total: int):
        pct = int(current / total * 100) if total else 0
        if pct == ul_last_pct[0]:
            return
        now = time.monotonic()
        if now - ul_last_time[0] < 2.5 and pct != 100:
            return
        ul_last_time[0] = now
        ul_last_pct[0] = pct
        bar = make_bar(current, total)
        try:
            await status.edit_text(
                f"⬆️ *Uploading:* {_esc(fname)}\n{bar}",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    await status.edit_text(
        f"✅ Download complete!\n⬆️ *Uploading:* {_esc(fname)}\n{make_bar(0, file_size)}",
        parse_mode="Markdown",
    )

    # Notify user if file is going to a different chat
    if target_chat_id != chat_id:
        dest_label = "your DM 💬" if delivery_mode == "pm" else "the connected group 👥"
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📨 Sending *{_esc(fname)}* to {dest_label}…",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    try:
        # Determine caption suffix for auto-delete notice in PM
        auto_delete_enabled = _get_cfg_value("auto_delete", False)
        delete_time_secs = _get_cfg_value("delete_time", 120)
        caption_suffix = ""
        is_pm_delivery = (target_chat_id > 0)
        if is_pm_delivery and auto_delete_enabled:
            # format delete time
            if delete_time_secs >= 3600 and delete_time_secs % 3600 == 0:
                fmt_time = f"{delete_time_secs // 3600} hours"
            elif delete_time_secs >= 60 and delete_time_secs % 60 == 0:
                fmt_time = f"{delete_time_secs // 60} minutes"
            else:
                fmt_time = f"{delete_time_secs} seconds"
            caption_suffix = (
                f"\n\n⚠️ *This file will be automatically deleted in {fmt_time}. *"
                f"Please download it or forward it to your Saved Messages to keep it!"
            )

        with open(file_path, "rb") as fh:
            sent_message = await context.bot.send_document(
                chat_id=target_chat_id,
                document=fh,
                filename=fname,
                caption=(
                    f"📚 *{_esc(book['title'])}*\n"
                    f"👤 {_esc(book['author'])}  •  📅 {book['year']}"
                    f"{caption_suffix}"
                ),
                parse_mode="Markdown",
                read_timeout=600,
                write_timeout=600,
                connect_timeout=30,
                pool_timeout=60,
            )

        # ---- Record completed download against the daily limit ----
        if not is_owner:
            loop.run_in_executor(None, _record_user_download, clicker_id)

        # Mirror file to dump channel if configured
        dump_channel = _get_cfg_value("dump_channel")
        if dump_channel and sent_message and sent_message.document:
            try:
                # Mention user securely
                user_mention = update.effective_user.mention_markdown() if update.effective_user else "Unknown User"
                await context.bot.send_document(
                    chat_id=dump_channel,
                    document=sent_message.document.file_id,
                    filename=fname,
                    caption=(
                        f"📚 *[DUMP] {_esc(book['title'])}*\n"
                        f"👤 {_esc(book['author'])}  •  📅 {book['year']}\n"
                        f"👤 Sent to user: {user_mention}"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as de:
                logging.error(f"[dump] Failed to send copy to dump channel {dump_channel}: {de}")

        # Schedule auto-deletion in PM if enabled
        if is_pm_delivery and auto_delete_enabled and sent_message:
            async def _delayed_delete(c_id: int, m_id: int, delay: int):
                try:
                    await asyncio.sleep(delay)
                    await context.bot.delete_message(chat_id=c_id, message_id=m_id)
                except Exception as ade:
                    logging.error(f"[auto_delete] Failed to delete message {m_id} in {c_id}: {ade}")
            
            asyncio.create_task(_delayed_delete(target_chat_id, sent_message.message_id, delete_time_secs))

        if is_pm_delivery:
            try:
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=f"✅ *{_esc(book['title'])}* has been successfully delivered to your DM!",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            await status.edit_text(
                f"✅ *Done!* {_esc(fname)} has been successfully sent to your DM 💬",
                parse_mode="Markdown"
            )
        else:
            await status.edit_text(f"✅ *Done!* {_esc(fname)} sent.", parse_mode="Markdown")
    except Exception as e:
        await status.edit_text(f"❌ Upload failed:\n`{e}`", parse_mode="Markdown")
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Callback: Download
# ---------------------------------------------------------------------------

async def download_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not _is_service_enabled():
        await query.answer("🔴 Bot service is currently offline.", show_alert=True)
        return

    _, uid_str, idx_str = query.data.split(":")
    uid = int(uid_str)
    idx = int(idx_str)
    chat_id = update.effective_chat.id

    session = user_sessions.get(uid)
    if not session:
        await query.edit_message_text("❌ Session expired. Please search again.")
        return

    book = session["results"][idx]
    await _process_md5_download(book["md5"], update, context, book=book)


# ---------------------------------------------------------------------------
# MD5 Direct Command
# ---------------------------------------------------------------------------

async def md5_download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    if not _is_service_enabled():
        await update.message.reply_text("🔴 Bot service is currently offline.")
        return

    text = (update.message.text or "").strip()
    match = re.search(r'/(?:start_)?md5_?([a-fA-F0-9]{32})', text)
    if not match:
        return

    md5 = match.group(1)
    await _process_md5_download(md5, update, context)


# ---------------------------------------------------------------------------
# Welcome message on new members joining
# ---------------------------------------------------------------------------

async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_service_enabled():
        return

    cfg = _load_cfg()

    # Respect the welcome_enabled toggle
    if not cfg.get("welcome_enabled", True):
        return

    welcome = cfg.get("welcome", {})
    welcome_text = welcome.get("text", "")
    welcome_photo = welcome.get("photo_file_id", None)

    if not welcome_text and not welcome_photo:
        return  # No custom welcome configured

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        name = member.full_name or member.first_name or "there"
        # Support {name} placeholder
        personalized = welcome_text.replace("{name}", name).replace("{first_name}", name)

        try:
            if welcome_photo:
                await update.message.reply_photo(
                    photo=welcome_photo,
                    caption=personalized or None,
                    parse_mode="Markdown",
                )
            elif personalized:
                await update.message.reply_text(personalized, parse_mode="Markdown")
        except Exception as ex:
            logger.warning("Welcome message failed: %s", ex)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger(__name__)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log transient network errors and keep the bot running."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("Transient network error (auto-retry): %s", err)
        return
    logger.error("Unhandled exception", exc_info=err)


def main():
    # Wrap handlers dynamically to enforce connected group restrictions
    def wrap_gated(func, allow_connect=False):
        from functools import wraps
        @wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            if not _is_chat_allowed(update, allow_connect=allow_connect):
                return
            return await func(update, context, *args, **kwargs)
        return wrapper

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    # Core commands
    app.add_handler(CommandHandler("start", wrap_gated(cmd_start)))
    app.add_handler(CommandHandler("help", wrap_gated(cmd_help)))
    app.add_handler(CommandHandler("search", wrap_gated(cmd_search), block=False))

    # Admin commands
    app.add_handler(CommandHandler("mode", wrap_gated(cmd_mode)))
    app.add_handler(CommandHandler("connect", wrap_gated(cmd_connect, allow_connect=True)))
    app.add_handler(CommandHandler("setwelcome", wrap_gated(cmd_setwelcome)))
    app.add_handler(CommandHandler("service", wrap_gated(cmd_service)))
    app.add_handler(CommandHandler("auto_delete", wrap_gated(cmd_auto_delete)))
    app.add_handler(CommandHandler("deletetime", wrap_gated(cmd_deletetime)))
    app.add_handler(CommandHandler("dump", wrap_gated(cmd_dump)))
    app.add_handler(CommandHandler("pm_search", wrap_gated(cmd_pm_search)))
    app.add_handler(CommandHandler("dailylimit", wrap_gated(cmd_dailylimit)))
    app.add_handler(CommandHandler("welcomeoff", wrap_gated(cmd_welcomeoff)))
    app.add_handler(CommandHandler("welcomeon", wrap_gated(cmd_welcomeon)))

    # Broadcast — ConversationHandler (private chat only, no gating needed — enforced inside)
    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", cmd_broadcast)],
        states={
            BROADCAST_WAITING: [
                MessageHandler(
                    filters.ChatType.PRIVATE & ~filters.COMMAND,
                    _do_broadcast,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_broadcast_cancel)],
        per_chat=True,
        per_user=True,
        allow_reentry=True,
    )
    app.add_handler(broadcast_conv)

    # Welcome on new members joining
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, wrap_gated(new_member_handler)))

    # MD5 direct download command handler (must be registered before msg_handler)
    app.add_handler(MessageHandler(filters.Regex(r'^/(?:start_)?md5_?([a-fA-F0-9]{32})(?:@\w+)?$'), wrap_gated(md5_download_handler), block=False))

    # Text / hashtag search
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wrap_gated(msg_handler), block=False))

    app.add_error_handler(_error_handler)

    print("🤖 Bot is running…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
