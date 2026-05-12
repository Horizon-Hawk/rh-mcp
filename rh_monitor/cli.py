"""Price + news + volume + schedule monitor with Claude Code GUI injection.

Ported from price_alert_monitor.py. Polls Robinhood every 5 seconds. When an
alert fires:
  1. Windows toast notification
  2. ntfy.sh phone push (optional, off by default — set NTFY_TOPIC env var)
  3. Auto-types the alert into the Claude Code window + Enter (Windows GUI)

State files live in $RH_MONITOR_DATA_DIR (default: current working directory).
Required: price_alerts.json + rh_config.json (or the rh-mcp auth path).
Optional: stock_universe.txt, fundamentals_cache.json, news_cache.json,
scheduled_messages.json, alert_inbox.json.

Run: rh-monitor              # main loop
Run: rh-monitor --list-windows  # debug: print all window titles
Run: rh-monitor --test          # fire the first active scheduled message
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import robin_stocks.robinhood as rh
    import pyautogui
    import pygetwindow as gw
    import pyperclip
    import keyboard
    from plyer import notification
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install rh-mcp[monitor]")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration — all paths and topics are env-overridable
# ---------------------------------------------------------------------------

# State directory. Defaults to current working directory so a fresh checkout
# can be run from anywhere without hardcoded absolute paths.
DATA_DIR = Path(os.environ.get("RH_MONITOR_DATA_DIR", Path.cwd()))
ALERTS_FILE = DATA_DIR / "price_alerts.json"
SCHEDULE_FILE = DATA_DIR / "scheduled_messages.json"
UNIVERSE_FILE = DATA_DIR / "stock_universe.txt"
CACHE_FILE = DATA_DIR / "fundamentals_cache.json"
NEWS_CACHE = DATA_DIR / "news_cache.json"
INBOX_FILE = DATA_DIR / "alert_inbox.json"
LOG_FILE = DATA_DIR / "alert_monitor.log"

# Robinhood credentials path — same env var rh-mcp uses, so a single
# rh_config.json works for both server and monitor.
CREDENTIALS_FILE = Path(os.environ.get("RH_CONFIG_PATH", DATA_DIR / "rh_config.json"))

POLL_INTERVAL = int(os.environ.get("RH_MONITOR_POLL_SECS", "5"))
VOL_POLL_MINS = int(os.environ.get("RH_MONITOR_VOL_POLL_MINS", "5"))
NEWS_POLL_MINS = int(os.environ.get("RH_MONITOR_NEWS_POLL_MINS", "1"))
NEWS_BATCH_SIZE = int(os.environ.get("RH_MONITOR_NEWS_BATCH", "200"))
NEWS_FRESH_HOURS = int(os.environ.get("RH_MONITOR_NEWS_FRESH_HOURS", "1"))
DIGEST_INTERVAL_MINS = int(os.environ.get("RH_MONITOR_DIGEST_MINS", "10"))
LARGE_PT_CHANGE_PCT = float(os.environ.get("RH_MONITOR_LARGE_PT_PCT", "25.0"))
DIGEST_MAX_ITEMS = int(os.environ.get("RH_MONITOR_DIGEST_MAX", "12"))
VOL_SPIKE_X = float(os.environ.get("RH_MONITOR_VOL_SPIKE_X", "3.0"))

BREAKING_CATALYSTS = {"UPGRADE", "DOWNGRADE", "WARNING", "FDA", "ACQUISITION"}
TIME_SENSITIVE_CATALYSTS = {"BEAT", "MISS"}
DROP_CATALYSTS = {"NEWS", "INSIDER"}
MARKET_OPEN_PT = (6, 30)
MARKET_CLOSE_PT = (13, 0)

# Catalyst keyword patterns. Mirror rh_mcp/analysis/news.py so the monitor and
# the in-process MCP news tool agree on classification.
CATALYST_PATTERNS = [
    ("ACQUISITION", r"\bagrees to (acquire|be acquired)|to acquire .{1,30} for|definitive agreement to acquire|merger with|takeover (bid|offer)|going private|all[- ]cash deal|tender offer|hostile bid"),
    ("DOWNGRADE",   r"\bdowngrade[ds]?\b|price target (lowered|cut|reduced) (to|from)|cut[s]? .{0,15}target to|lower[s]? .{0,15}target to|reduces? price target"),
    ("UPGRADE",     r"\bupgrade[ds]?\b|price target raised (to|from)|raises? .{0,15}target to|lift[s]? .{0,15}target to|increases? price target"),
    ("WARNING",     r"guidance cut|lowers? guidance|withdraws? guidance|earnings warning|profit warning|cuts? full[- ]year|slashes? outlook"),
    ("BEAT",        r"\b(beats?|tops|exceeds?) (q[1-4]|expect|estimate|consensus|forecast)|earnings beat|q[1-4] beat|revenue beat"),
    ("MISS",        r"\b(misses?|missed|fell short of) (q[1-4]|expect|estimate|consensus|forecast)|earnings miss|q[1-4] miss|revenue miss"),
    ("FDA",         r"\bfda (approves|approval|rejects|grants|clearance|authorizes)|clinical trial (result|fail|success)|phase [123] (result|fail)"),
    ("INSIDER",     r"\binsider (buying|selling|purchase|sells)|ceo (sells|buys|purchases) .{1,20} shares"),
]

# Phone push backup channel — defaults to disabled. Set NTFY_TOPIC to any
# unguessable string and subscribe on your phone via the ntfy.sh app.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_BASE_URL = os.environ.get("NTFY_BASE_URL", "https://ntfy.sh")

# Claude Code launcher. Override via CLAUDE_EXE env var if your install path differs.
# Common defaults:
#   Windows (npm global): %APPDATA%\npm\claude.cmd  →  C:\Users\<you>\AppData\Roaming\npm\claude.cmd
#   Windows (Native UI):  installed via Anthropic desktop installer
DEFAULT_CLAUDE_EXE = str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd")
CLAUDE_EXE = os.environ.get("CLAUDE_EXE", DEFAULT_CLAUDE_EXE)
CLAUDE_LAUNCH_WAIT = int(os.environ.get("RH_MONITOR_CLAUDE_LAUNCH_WAIT", "12"))
CLAUDE_WINDOW_TITLE = "Claude"  # fallback substring search if Braille match fails

pyautogui.FAILSAFE = False

# Ensure data dir exists (so logging.FileHandler can open the log file)
DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def classify_catalyst(title: str, preview: str) -> str:
    # Only check title — previews contain marketing fluff that triggers false positives
    text = title.lower()
    for label, pattern in CATALYST_PATTERNS:
        if re.search(pattern, text):
            return label
    return "NEWS"


# ---------------------------------------------------------------------------
# ntfy.sh phone push — broker-independent backup channel
# ---------------------------------------------------------------------------

def send_ntfy(title: str, body: str, priority: str = "default", tags: str = "") -> None:
    """Send a push notification via ntfy.sh. No-op if NTFY_TOPIC is empty.

    priority: 'min' | 'low' | 'default' | 'high' | 'urgent'
    tags: comma-separated emoji shortcodes from https://docs.ntfy.sh/emojis/
    """
    if not NTFY_TOPIC:
        return
    try:
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        req = urllib.request.Request(
            f"{NTFY_BASE_URL}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.warning(f"ntfy push failed: {e}")


# ---------------------------------------------------------------------------
# Window helpers — Windows-specific GUI automation
# ---------------------------------------------------------------------------

def list_windows():
    """Print all open window titles — use this to find your CLAUDE_WINDOW_TITLE."""
    for w in gw.getAllWindows():
        if w.title:
            print(w.title.encode("ascii", errors="replace").decode("ascii"))


def is_claude_window(title: str) -> bool:
    """Claude Code windows start with a Braille character (U+2800–U+28FF)."""
    if not title:
        return False
    if "⠀" <= title[0] <= "⣿":
        return True
    return CLAUDE_WINDOW_TITLE.lower() in title.lower()


def find_claude_window():
    for w in gw.getAllWindows():
        if w.title and is_claude_window(w.title):
            return w
    return None


def claude_is_open() -> bool:
    return find_claude_window() is not None


def claude_process_running() -> bool:
    """True if any node.exe / claude.cmd process is alive running Claude Code."""
    try:
        import psutil
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if name in ("node.exe", "claude.cmd") and "claude" in cmdline:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
    except ImportError:
        pass
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process -Filter \"Name='node.exe' or Name='claude.cmd'\" | Where-Object { $_.CommandLine -match 'claude' }).Count"],
            capture_output=True, text=True, timeout=8
        )
        return int(result.stdout.strip() or "0") > 0
    except Exception as e:
        log.debug(f"Process check failed: {e}")
        return False


def ensure_claude_open() -> bool:
    """Launch Claude Code only if no window AND no process is found."""
    if claude_is_open():
        return True
    if claude_process_running():
        log.info("Claude process is running but window not found — likely minimized "
                 "or on another virtual desktop. Skipping launch to avoid duplicate.")
        return True
    if not CLAUDE_EXE or not Path(CLAUDE_EXE).exists():
        log.warning(f"CLAUDE_EXE not found at {CLAUDE_EXE!r}. "
                    "Set CLAUDE_EXE env var to your claude launcher path.")
        return False
    log.info("Claude window not found and no claude process detected — launching Claude Code...")
    try:
        subprocess.Popen(
            ["cmd.exe", "/k", CLAUDE_EXE],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except Exception as e:
        log.error(f"Failed to launch Claude: {e}")
        return False
    for _ in range(CLAUDE_LAUNCH_WAIT * 2):
        time.sleep(0.5)
        if claude_is_open():
            log.info("Claude window found.")
            time.sleep(3)
            return True
    log.warning("Claude window did not appear after launch.")
    return False


def _read_inbox() -> list:
    if not INBOX_FILE.exists():
        return []
    try:
        return json.loads(INBOX_FILE.read_text())
    except Exception:
        return []


def _append_inbox(message: str) -> None:
    try:
        inbox = _read_inbox()
        inbox.append({"ts": datetime.now().isoformat(), "message": message})
        INBOX_FILE.write_text(json.dumps(inbox[-100:], indent=2))
        log.info(f"Appended to inbox ({len(inbox)} pending): {message[:60]}")
    except Exception as e:
        log.warning(f"Inbox append failed: {e}")


def _clear_inbox() -> None:
    try:
        INBOX_FILE.write_text("[]")
    except Exception:
        pass


def _do_paste_and_enter(message: str) -> bool:
    """Paste + enter step. Caller already focused the window."""
    single_line = " | ".join(line.strip() for line in message.splitlines() if line.strip())
    try:
        pyperclip.copy(single_line)
        time.sleep(0.15)
        keyboard.press_and_release("ctrl+v")
        time.sleep(0.4)
        keyboard.press_and_release("enter")
        return True
    except Exception as e:
        log.warning(f"Paste failed, falling back to typing: {e}")
        try:
            keyboard.write(single_line, delay=0.03)
            time.sleep(0.3)
            keyboard.press_and_release("enter")
            return True
        except Exception as e2:
            log.warning(f"Typing fallback also failed: {e2}")
            return False


def _focus_and_click(win) -> bool:
    """Restore if minimized, activate, re-fetch position, click input field."""
    try:
        if getattr(win, "isMinimized", False):
            try:
                win.restore()
                time.sleep(0.4)
            except Exception:
                pass
        win.activate()
    except Exception:
        pass
    time.sleep(0.6)
    win = find_claude_window()
    if not win:
        return False
    click_x = win.left + win.width // 2
    click_y = win.top + win.height - 60
    try:
        pyautogui.click(click_x, click_y)
        time.sleep(0.5)
        return True
    except Exception as e:
        log.warning(f"Click failed: {e}")
        return False


def inject_to_claude(message: str) -> bool:
    """Find Claude window, focus, paste message, send Enter.

    On failure, persist to alert_inbox.json so the message isn't lost.
    On success, drain any backlog as a combined follow-up.
    """
    win = find_claude_window()
    if not win:
        log.warning("No Claude Code window found. Persisting to inbox for later.")
        _append_inbox(message)
        return False
    if not _focus_and_click(win):
        _append_inbox(message)
        return False
    if not _do_paste_and_enter(message):
        _append_inbox(message)
        return False
    log.info(f"Injected -> Claude: {message[:80]}")
    backlog = _read_inbox()
    if backlog:
        time.sleep(1.5)
        combined = f"BACKLOG ({len(backlog)} missed alerts): " + " || ".join(
            f"[{e.get('ts', '?')[11:19]}] {e.get('message', '')[:200]}" for e in backlog
        )
        win2 = find_claude_window()
        if win2 and _focus_and_click(win2) and _do_paste_and_enter(combined[:3000]):
            _clear_inbox()
            log.info(f"Drained {len(backlog)} backlogged messages")
        else:
            log.warning(f"Backlog drain failed; {len(backlog)} messages remain in inbox")
    return True


# ---------------------------------------------------------------------------
# Alert file helpers
# ---------------------------------------------------------------------------

def load_alerts():
    if not ALERTS_FILE.exists():
        return []
    try:
        return json.loads(ALERTS_FILE.read_text())
    except Exception:
        return []


def save_alerts(alerts):
    ALERTS_FILE.write_text(json.dumps(alerts, indent=2))


def get_prices(tickers: list) -> dict:
    prices = {}
    for ticker in tickers:
        try:
            result = rh.get_latest_price(ticker)
            if result and result[0]:
                prices[ticker] = float(result[0])
        except Exception as e:
            log.warning(f"Price fetch failed for {ticker}: {e}")
    return prices


def should_fire(alert: dict, prices: dict) -> bool:
    if alert.get("fired") or not alert.get("active", True):
        return False
    ticker = alert["ticker"]
    if ticker not in prices:
        return False
    price = prices[ticker]
    direction = alert.get("direction", "above")
    if direction == "above":
        return price >= alert["target"]
    if direction == "below":
        return price <= alert["target"]
    return False


def fire_alert(alert: dict, price: float):
    ticker = alert["ticker"]
    target = alert["target"]
    direction = alert.get("direction", "above")
    grade = alert.get("grade", "").upper()
    note = alert.get("note", "")
    arrow = ">=" if direction == "above" else "<="

    log.info(f"*** ALERT FIRED: {ticker} ${price:.2f} {arrow} target ${target} [{grade}] ***")
    try:
        notification.notify(
            title=f"ALERT [{grade}]  {ticker}  ${price:.2f}  {arrow}  ${target}",
            message=note[:100] if note else f"{ticker} hit ${price:.2f}",
            timeout=15,
        )
    except Exception as e:
        log.warning(f"Toast failed: {e}")

    sep = "=" * 48
    msg = (
        f"{sep}\n"
        f"ALERT  [{grade}]  {ticker}  ${price:.2f}  {arrow}  target ${target}\n"
        + (f"{note}\n" if note else "") +
        f"{sep}"
    )
    inject_to_claude(msg)

    is_stop = grade.lower() in ("stop", "stop-loss")
    push_priority = "urgent" if is_stop else "high"
    push_tags = "rotating_light" if is_stop else ("chart_with_upwards_trend" if direction == "above" else "chart_with_downwards_trend")
    send_ntfy(
        title=f"{ticker} {arrow} ${target:.2f}  [{grade or 'alert'}]",
        body=f"${price:.2f}  {arrow}  ${target}\n{note[:140] if note else ''}",
        priority=push_priority,
        tags=push_tags,
    )

    alert["fired"] = True
    alert["fired_at"] = datetime.now().isoformat()
    alert["fired_price"] = price


# ---------------------------------------------------------------------------
# Scheduled messages
# ---------------------------------------------------------------------------

def load_schedule() -> list:
    if not SCHEDULE_FILE.exists():
        return []
    try:
        return json.loads(SCHEDULE_FILE.read_text())
    except Exception:
        return []


def check_schedule(fired_today: set) -> set:
    now = datetime.now()
    day_abbr = now.strftime("%a")
    current_time = now.strftime("%H:%M")
    today_key = now.strftime("%Y-%m-%d")

    schedule = load_schedule()
    for item in schedule:
        if not item.get("active", True):
            continue
        if day_abbr not in item.get("days", []):
            continue
        label = item.get("label", item.get("message", ""))
        fire_key = f"{today_key}:{label}"
        if fire_key in fired_today:
            continue
        sched_h, sched_m = map(int, item["time"].split(":"))
        cur_h, cur_m = map(int, current_time.split(":"))
        minutes_past = (cur_h * 60 + cur_m) - (sched_h * 60 + sched_m)
        if 0 <= minutes_past <= 30:
            message = item["message"]
            log.info(f"*** SCHEDULED: [{label}] firing -> {message!r} ***")
            if not ensure_claude_open():
                log.error(f"Skipping [{label}] — could not open Claude.")
                continue
            try:
                notification.notify(
                    title=f"Scheduled: {label}",
                    message=message[:100],
                    timeout=8,
                )
            except Exception:
                pass
            sep = "=" * 48
            inject_to_claude(
                f"{sep}\n"
                f"SCHEDULED [{label}]  {now.strftime('%H:%M')}\n"
                f"{message}\n"
                f"{sep}"
            )
            send_ntfy(
                title=f"⏰ {label}  {now.strftime('%H:%M')}",
                body=message[:180],
                priority="default",
                tags="alarm_clock",
            )
            fired_today.add(fire_key)

    fired_today = {k for k in fired_today if k.startswith(today_key)}
    return fired_today


# ---------------------------------------------------------------------------
# News monitoring + cache
# ---------------------------------------------------------------------------

def load_news_cache() -> dict:
    if not NEWS_CACHE.exists():
        return {}
    try:
        return json.loads(NEWS_CACHE.read_text())
    except Exception:
        return {}


def save_news_cache(cache: dict):
    NEWS_CACHE.write_text(json.dumps(cache, indent=2))


def news_universe() -> list:
    tickers = set()
    try:
        for a in load_alerts():
            if a.get("active"):
                tickers.add(a["ticker"])
    except Exception:
        pass
    if UNIVERSE_FILE.exists():
        for line in UNIVERSE_FILE.read_text().splitlines():
            line = line.split("#")[0].strip()
            tickers.update(t.strip().upper() for t in line.split() if t.strip())
    return sorted(tickers)


def should_inject(catalyst: str, age_hours: float) -> bool:
    if catalyst in DROP_CATALYSTS:
        return False
    if catalyst in BREAKING_CATALYSTS:
        return age_hours <= NEWS_FRESH_HOURS * 4
    if catalyst in TIME_SENSITIVE_CATALYSTS:
        return age_hours <= NEWS_FRESH_HOURS
    return False


def parse_pt_change_pct(title: str) -> float:
    m = re.search(r"\$(\d+(?:\.\d+)?)\s+(?:from|to)\s+\$(\d+(?:\.\d+)?)", title)
    if not m:
        return 0.0
    a, b = float(m.group(1)), float(m.group(2))
    if min(a, b) == 0:
        return 0.0
    return abs((a - b) / min(a, b) * 100)


def active_alert_tickers() -> set:
    tickers = set()
    try:
        for a in load_alerts():
            if a.get("active"):
                tickers.add(a["ticker"])
    except Exception:
        pass
    return tickers


def is_after_hours(pub: str) -> bool:
    try:
        dt = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone()
        h = dt.hour
        return h >= 13 or h < 6
    except Exception:
        return False


def get_news_tier(ticker: str, catalyst: str, title: str, pub: str, active_set: set) -> int:
    if ticker in active_set:
        return 1
    if catalyst in ("ACQUISITION", "FDA", "WARNING"):
        return 1
    if catalyst in ("UPGRADE", "DOWNGRADE"):
        if parse_pt_change_pct(title) >= LARGE_PT_CHANGE_PCT:
            return 1
    if catalyst in ("BEAT", "MISS") and is_after_hours(pub):
        return 1
    return 2


def fire_digest(pending: list) -> list:
    if not pending:
        return []
    catalyst_order = {"ACQUISITION": 0, "FDA": 1, "WARNING": 2, "UPGRADE": 3,
                      "DOWNGRADE": 4, "BEAT": 5, "MISS": 6}
    pending.sort(key=lambda x: (catalyst_order.get(x["catalyst"], 9), x["ticker"]))
    items = pending[:DIGEST_MAX_ITEMS]
    timestamp = datetime.now().strftime("%H:%M")
    sep = "=" * 48
    lines = [
        sep,
        f"NEWS DIGEST  |  {timestamp}  |  {len(pending)} catalysts past {DIGEST_INTERVAL_MINS}m",
        sep,
    ]
    for it in items:
        lines.append(f"[{it['catalyst']}]  {it['ticker']:<6}  {it['title'][:70]}")
    if len(pending) > DIGEST_MAX_ITEMS:
        lines.append(f"... +{len(pending) - DIGEST_MAX_ITEMS} more (see news_cache.json)")
    lines.append(sep)
    log.info(f"Digest firing: {len(pending)} items")
    inject_to_claude("\n".join(lines))
    digest_lines = [f"[{it['catalyst']}] {it['ticker']}  {it['title'][:55]}" for it in items]
    send_ntfy(
        title=f"📰 News digest  {timestamp}  ({len(pending)})",
        body="\n".join(digest_lines)[:600],
        priority="default",
        tags="newspaper",
    )
    return []


def check_news(news_seen: set, cache: dict, batch_offset: int, digest_pending: list):
    universe = news_universe()
    if not universe:
        return news_seen, cache, 0, digest_pending
    if batch_offset >= len(universe):
        batch_offset = 0
    batch = universe[batch_offset:batch_offset + NEWS_BATCH_SIZE]
    next_offset = batch_offset + NEWS_BATCH_SIZE
    active_set = active_alert_tickers()
    for ticker in batch:
        try:
            articles = rh.stocks.get_news(ticker) or []
        except Exception:
            continue
        if ticker not in cache:
            cache[ticker] = []
        existing_uuids = {a.get("uuid") for a in cache[ticker]}
        for art in articles:
            uuid = art.get("uuid", "")
            if not uuid or uuid in existing_uuids or uuid in news_seen:
                continue
            title = art.get("title", "").strip()
            preview = (art.get("preview_text") or art.get("summary") or "").strip()
            source = art.get("source", "")
            pub_raw = art.get("published_at", "")
            pub = pub_raw[:16].replace("T", " ")
            catalyst = classify_catalyst(title, preview)
            entry = {
                "uuid": uuid, "ticker": ticker, "title": title, "source": source,
                "preview": preview[:200], "published": pub, "catalyst": catalyst,
            }
            cache[ticker].insert(0, entry)
            cache[ticker] = cache[ticker][:20]
            news_seen.add(uuid)
            existing_uuids.add(uuid)
            try:
                dt = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            except Exception:
                age_hours = 999
            if not should_inject(catalyst, age_hours):
                continue
            tier = get_news_tier(ticker, catalyst, title, pub_raw, active_set)
            if tier == 1:
                sep = "=" * 48
                inject_to_claude(
                    f"{sep}\n"
                    f"BREAKING T1 [{catalyst}]  {ticker}  {pub}  —  {source}\n"
                    f"{title}\n"
                    + (f"{preview[:120]}\n" if preview else "") +
                    f"{sep}"
                )
                catalyst_tags = {
                    "ACQUISITION": "moneybag", "FDA": "pill", "WARNING": "warning",
                    "UPGRADE": "arrow_double_up", "DOWNGRADE": "arrow_double_down",
                    "BEAT": "white_check_mark", "MISS": "x",
                }.get(catalyst, "newspaper")
                send_ntfy(
                    title=f"🚨 T1 [{catalyst}]  {ticker}",
                    body=f"{title[:200]}\n{source}  {pub}",
                    priority="high",
                    tags=catalyst_tags,
                )
                log.info(f"T1 injected: {ticker} [{catalyst}] {title[:60]}")
            else:
                digest_pending.append({
                    "ticker": ticker, "catalyst": catalyst, "title": title,
                    "source": source, "published": pub,
                })
                log.info(f"T2 queued: {ticker} [{catalyst}] {title[:50]}")
    save_news_cache(cache)
    return news_seen, cache, next_offset, digest_pending


# ---------------------------------------------------------------------------
# Halt + volume spike checks
# ---------------------------------------------------------------------------

def check_halts(halt_state: dict) -> dict:
    tickers = list(active_alert_tickers())
    if not tickers:
        return halt_state
    try:
        results = rh.stocks.get_quotes(tickers, info=None) or []
    except Exception:
        return halt_state
    for q in results:
        if not q:
            continue
        sym = q.get("symbol", "").upper()
        halted = bool(q.get("trading_halted", False))
        was_halted = halt_state.get(sym, False)
        if halted != was_halted:
            try:
                price = float(q.get("last_trade_price") or 0)
            except (ValueError, TypeError):
                price = 0
            if halted:
                msg = f"HALT  {sym}  TRADING HALTED at ${price:.2f}"
            else:
                msg = f"HALT  {sym}  RESUMED — now ${price:.2f}"
            sep = "=" * 48
            inject_to_claude(f"{sep}\n{msg}\n{sep}")
            log.info(f"Halt status change: {sym} halted={halted}")
        halt_state[sym] = halted
    return halt_state


def market_is_open() -> bool:
    now = datetime.now()
    h, m = now.hour, now.minute
    open_mins = MARKET_OPEN_PT[0] * 60 + MARKET_OPEN_PT[1]
    close_mins = MARKET_CLOSE_PT[0] * 60 + MARKET_CLOSE_PT[1]
    current_mins = h * 60 + m
    return open_mins <= current_mins < close_mins and now.weekday() < 5


def elapsed_session_minutes() -> float:
    now = datetime.now()
    open_mins = MARKET_OPEN_PT[0] * 60 + MARKET_OPEN_PT[1]
    current_mins = now.hour * 60 + now.minute
    return max(1.0, current_mins - open_mins)


def load_universe() -> list:
    if not UNIVERSE_FILE.exists():
        return []
    tickers = []
    for line in UNIVERSE_FILE.read_text().splitlines():
        line = line.split("#")[0].strip()
        tickers.extend(t.strip().upper() for t in line.split() if t.strip())
    return list(dict.fromkeys(tickers))


def load_fund_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def fetch_universe_quotes(tickers: list) -> dict:
    quotes = {}
    chunk_size = 75
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        try:
            results = rh.stocks.get_quotes(chunk, info=None)
            if not results:
                continue
            for q in results:
                if not q:
                    continue
                try:
                    sym = q.get("symbol", "").upper()
                    price = float(q.get("last_trade_price") or 0)
                    prev = float(q.get("adjusted_previous_close") or 0)
                    vol = int(float(q.get("volume") or 0))
                    if sym and price and prev:
                        quotes[sym] = {
                            "price": price,
                            "pct": round((price - prev) / prev * 100, 2),
                            "volume": vol,
                        }
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            log.warning(f"Volume quote chunk error: {e}")
        time.sleep(0.1)
    return quotes


def check_volume_spikes(vol_fired: set, cache: dict) -> set:
    if not market_is_open():
        return vol_fired
    universe = load_universe()
    if not universe:
        return vol_fired
    elapsed = elapsed_session_minutes()
    session_total = 390.0
    fraction_done = elapsed / session_total
    quotes = fetch_universe_quotes(universe)
    for ticker, q in quotes.items():
        if ticker in vol_fired:
            continue
        vol = q["volume"]
        if vol == 0:
            continue
        fund = cache.get(ticker, {})
        avg_vol = fund.get("avg_volume_30d") or fund.get("avg_volume") or 0
        if avg_vol < 200_000:
            continue
        expected_vol = avg_vol * fraction_done
        if expected_vol <= 0:
            continue
        pace_ratio = vol / expected_vol
        if pace_ratio >= VOL_SPIKE_X:
            price = q["price"]
            pct = q["pct"]
            sector = fund.get("sector", "")
            log.info(f"*** VOLUME SPIKE: {ticker} {pace_ratio:.1f}x pace | ${price:.2f} {pct:+.1f}% | {sector} ***")
            try:
                notification.notify(
                    title=f"VOL SPIKE: {ticker}  {pace_ratio:.1f}x",
                    message=f"${price:.2f}  {pct:+.1f}%  |  {sector}",
                    timeout=12,
                )
            except Exception:
                pass
            spike_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            sep = "=" * 48
            inject_to_claude(
                f"{sep}\n"
                f"VOLUME SPIKE  {ticker}  spike_price=${price:.2f}  {pct:+.1f}%"
                f"  |  {pace_ratio:.1f}x avg pace  |  {sector}  |  ts={spike_ts}\n"
                f"Run: spike_check {ticker} {price:.2f} {spike_ts} — then eval pipeline if OK\n"
                f"{sep}"
            )
            send_ntfy(
                title=f"⚡ VOL SPIKE  {ticker}  {pace_ratio:.1f}x",
                body=f"${price:.2f}  {pct:+.1f}%  |  {sector}",
                priority="high",
                tags="zap",
            )
            vol_fired.add(ticker)
    return vol_fired


# ---------------------------------------------------------------------------
# Login + main loop
# ---------------------------------------------------------------------------

def _login_robinhood() -> None:
    """Use rh-mcp's shared auth if available, else log in directly from creds file."""
    try:
        from rh_mcp.auth import login as rh_mcp_login
        rh_mcp_login()
        log.info("Logged in via rh_mcp.auth.")
        return
    except Exception as e:
        log.debug(f"rh_mcp.auth.login failed ({e}); falling back to direct login.")
    if not CREDENTIALS_FILE.exists():
        print(f"Create {CREDENTIALS_FILE} with:")
        print('  {"username": "your@email.com", "password": "yourpassword"}')
        sys.exit(1)
    creds = json.loads(CREDENTIALS_FILE.read_text())
    log.info("Logging into Robinhood (direct)...")
    rh.login(creds["username"], creds["password"])
    log.info("Logged in.")


def run():
    _login_robinhood()
    log.info(f"Monitor running. DATA_DIR={DATA_DIR}")
    log.info(f"NTFY_TOPIC={'(set)' if NTFY_TOPIC else '(disabled)'}  CLAUDE_EXE={CLAUDE_EXE}")
    if not ALERTS_FILE.exists():
        save_alerts([])
        log.info(f"Created empty {ALERTS_FILE}")

    fired_today: set = set()
    today_key = datetime.now().strftime("%Y-%m-%d")
    startup_time = datetime.now().strftime("%H:%M")
    for item in load_schedule():
        if item.get("active", True) and startup_time > item["time"]:
            label = item.get("label", item.get("message", ""))
            fired_today.add(f"{today_key}:{label}")
            log.info(f"Startup: marking [{label}] as already fired (past {item['time']})")

    vol_fired: set = set()
    vol_last_check: float = 0.0
    fund_cache: dict = load_fund_cache()
    news_seen: set = set()
    news_last_check: float = 0.0
    news_batch_offset: int = 0
    news_cache: dict = load_news_cache()
    digest_pending: list = []
    digest_last_fire: float = time.monotonic()
    halt_state: dict = {}
    halt_last_check: float = 0.0
    for articles in news_cache.values():
        for a in articles:
            news_seen.add(a.get("uuid", ""))

    while True:
        try:
            fired_today = check_schedule(fired_today)
            now_ts = time.monotonic()
            new_day = datetime.now().strftime("%Y-%m-%d")
            if new_day != today_key:
                vol_fired = set()
                news_seen = set()
                today_key = new_day
                fund_cache = load_fund_cache()
                news_cache = {}
            if now_ts - news_last_check >= NEWS_POLL_MINS * 60:
                news_seen, news_cache, news_batch_offset, digest_pending = check_news(
                    news_seen, news_cache, news_batch_offset, digest_pending
                )
                news_last_check = now_ts
            if now_ts - digest_last_fire >= DIGEST_INTERVAL_MINS * 60:
                digest_pending = fire_digest(digest_pending)
                digest_last_fire = now_ts
            if now_ts - vol_last_check >= VOL_POLL_MINS * 60:
                vol_fired = check_volume_spikes(vol_fired, fund_cache)
                vol_last_check = now_ts
            if market_is_open() and now_ts - halt_last_check >= 60:
                halt_state = check_halts(halt_state)
                halt_last_check = now_ts
            alerts = load_alerts()
            active = [a for a in alerts if not a.get("fired") and a.get("active", True)]
            if active:
                tickers = list({a["ticker"] for a in active})
                prices = get_prices(tickers)
                changed = False
                for alert in alerts:
                    if should_fire(alert, prices):
                        fire_alert(alert, prices[alert["ticker"]])
                        changed = True
                if changed:
                    save_alerts(alerts)
                status = "  ".join(
                    f"{a['ticker']} ${prices.get(a['ticker'], '?'):.2f}->${a['target']}"
                    for a in active if a["ticker"] in prices
                )
                if status:
                    log.info(status)
        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(POLL_INTERVAL)


def main():
    """rh-monitor CLI entry point."""
    if "--list-windows" in sys.argv:
        list_windows()
        return
    if "--test" in sys.argv:
        schedule = load_schedule()
        active = [s for s in schedule if s.get("active", True)]
        if not active:
            print("No active scheduled messages found.")
            return
        item = active[0]
        label = item.get("label", "test")
        message = item["message"]
        print(f"Test firing: [{label}] -> {message!r}")
        print(f"Claude open: {claude_is_open()}")
        if ensure_claude_open():
            sep = "=" * 48
            inject_to_claude(
                f"{sep}\n"
                f"SCHEDULED [{label}]  TEST\n"
                f"{message}\n"
                f"{sep}"
            )
            print("Injected.")
        else:
            print("Could not open Claude window.")
        return
    run()


if __name__ == "__main__":
    main()
