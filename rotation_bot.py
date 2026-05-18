#!/usr/bin/env python3
"""
Rotation Number Telegram Bot
============================

Send the bot a game line in this exact format:

    Philadelphia Phillies -117, Mau = .552 Poly

and it replies with the rotation number prepended:

    952 Philadelphia Phillies -117, Mau = .552 Poly

It also handles totals (over/under). For a total it scrapes the game's
base rotation number, then builds the totals-block number using the
parity rules you described:

    - odd number  = OVER
    - even number = UNDER
    - home team   = even
    - away team   = odd

Example:

    Arsenal/Burnley over 3.5 +117, Mau = .460 Poly
    -> 200073 Arsenal/Burnley over 3.5 +117, Mau = .460 Poly

------------------------------------------------------------------
SETUP (read this once)
------------------------------------------------------------------
1. Create a bot:
   - In Telegram, message @BotFather
   - Send /newbot, follow prompts, copy the token it gives you
2. Put the token below in BOT_TOKEN (or set env var TELEGRAM_BOT_TOKEN)
3. Install deps:
       pip install python-telegram-bot requests beautifulsoup4
4. Run:
       python rotation_bot.py
5. In Telegram, open your bot and send it a game line.
------------------------------------------------------------------
"""

import os
import re
import logging

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

# Put your token here, or set the TELEGRAM_BOT_TOKEN environment variable.
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")

# Pages we scrape. The bot tries them in order until it finds the team.
# scoresandodds is by far the most reliable for rotation numbers.
SCRAPE_PAGES = [
    "https://www.scoresandodds.com/mlb",
    "https://www.scoresandodds.com/nba",
    "https://www.scoresandodds.com/nhl",
    "https://www.scoresandodds.com/nfl",
    "https://www.scoresandodds.com/wnba",
    "https://www.scoresandodds.com/ncaab",
    "https://www.scoresandodds.com/ncaaf",
    "https://www.scoresandodds.com/soccer/odds",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# If plain requests gets blocked (HTTP 403 from bot protection), the bot can
# fall back to a real headless browser. This only activates if you install it:
#     pip install playwright && playwright install chromium
USE_PLAYWRIGHT_FALLBACK = True

# On a cloud host (Railway etc.) plain requests is almost always blocked by
# scoresandodds, so trying it first just wastes ~15s per page. Set the env
# var SKIP_REQUESTS=1 (done in the Dockerfile/Railway) to go straight to
# the headless browser. Locally, leave it unset so the fast path is used.
SKIP_REQUESTS = os.environ.get("SKIP_REQUESTS", "").strip() in ("1", "true", "yes")

_session = requests.Session()
_session.headers.update(HEADERS)

# Short-lived cache of fetched pages. A pasted block of many lines should
# scrape each page only once, not once per line. Entries expire so the
# data stays fresh between separate messages.
import time as _time
_PAGE_CACHE = {}            # url -> (timestamp, html)
PAGE_CACHE_SECONDS = 90     # re-fetch a page at most this often


def _fetch_html(url: str):
    """Return page HTML (cached briefly), or None.

    Tries the short-lived cache, then requests, then Playwright fallback.
    """
    now = _time.time()
    cached = _PAGE_CACHE.get(url)
    if cached and (now - cached[0]) < PAGE_CACHE_SECONDS:
        return cached[1]

    html = _fetch_html_uncached(url)
    if html:
        _PAGE_CACHE[url] = (now, html)
    return html


def _fetch_html_uncached(url: str):
    """Return page HTML, or None. Tries requests, then Playwright fallback.

    If SKIP_REQUESTS is set (cloud hosts), skip straight to Playwright.
    """
    if not SKIP_REQUESTS:
        try:
            resp = _session.get(url, timeout=15)
            if resp.status_code == 200 and len(resp.text) > 2000:
                return resp.text
            log.warning("requests got HTTP %s for %s (len=%d)",
                        resp.status_code, url, len(resp.text))
        except requests.RequestException as e:
            log.warning("requests failed for %s: %s", url, e)

    if not USE_PLAYWRIGHT_FALLBACK:
        return None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed; cannot use browser fallback. "
                    "Install with: pip install playwright && "
                    "playwright install chromium")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)  # let odds JS render
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        log.warning("Playwright fallback failed for %s: %s", url, e)
        return None

# ---- ROTATION NUMBER RULES (confirmed from real scoresandodds data) --------
#
# There is NO arithmetic formula. The rotation numbers are exactly what
# scoresandodds prints. The only logic is *which* number to return:
#
#   On scoresandodds each game is a consecutive pair of rows:
#       <odd number>  AWAY team   (listed first)
#       <even number> HOME team   (listed second)
#       then a "Draw" / total row
#
#   Example (England Premier League):
#       200073  Burnley FC   (away, odd)
#       200074  Arsenal      (home, even)
#
#   Rules:
#     - Moneyline / side on a team -> that team's own number
#     - OVER  total -> the AWAY team's number (the odd one, listed first)
#     - UNDER total -> the HOME team's number (the even one, listed second)
#
#   So "Arsenal/Burnley over 3.5" -> away = Burnley = 200073.  No math.
# ---------------------------------------------------------------------------


# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logs the full request URL, which for Telegram includes your bot
# token. Raise its level so the token never lands in your logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rotation_bot")


# ============================================================
# PARSING THE USER'S MESSAGE
# ============================================================

OVER_UNDER_RE = re.compile(r"\b(over|under|o|u)\b\s*\d", re.IGNORECASE)


def parse_message(text: str):
    """Pull the team/match name out of the user's line.

    Returns a dict:
      {
        "raw": original text,
        "is_total": bool,
        "is_over": bool or None,
        "query": team or matchup string used to find the game,
      }
    """
    raw = text.strip()
    lower = raw.lower()

    is_total = bool(OVER_UNDER_RE.search(raw)) or (" over " in f" {lower} ") or (" under " in f" {lower} ")
    is_over = None
    if is_total:
        is_over = " over " in f" {lower} " or bool(re.search(r"\bo\d", lower))

    # The "name" is everything before the price (the first +/-NNN or a
    # number+price combo). We grab text up to the first odds token.
    # Examples:
    #   "Philadelphia Phillies -117, Mau = .552 Poly" -> "Philadelphia Phillies"
    #   "Arsenal/Burnley over 3.5 +117, ..."          -> "Arsenal/Burnley"
    name_part = raw

    # Cut at the first American odds price like -117 or +117
    m = re.search(r"[+-]\d{2,4}", raw)
    if m:
        name_part = raw[:m.start()]

    # For totals, also strip the "over/under X.X" portion
    name_part = re.split(
        r"\b(over|under|o|u)\b", name_part, maxsplit=1, flags=re.IGNORECASE
    )[0]

    name_part = name_part.strip(" ,")

    return {
        "raw": raw,
        "is_total": is_total,
        "is_over": is_over,
        "query": name_part,
    }


# ============================================================
# SCRAPING
# ============================================================

def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _extract_rows(html: str):
    """Return an ordered list of (rotation_int, team_text) for every game
    row on the page, in the order they appear (away then home per game)."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    seen = set()

    for el in soup.find_all(["tr", "li", "div", "a", "span", "p"]):
        txt = " ".join(el.get_text(" ", strip=True).split())
        if not txt:
            continue
        # A team row: rotation number (3-6 digits) then a team name.
        m = re.match(r"^(\d{3,6})\s+([A-Za-z].{1,40})$", txt)
        if not m:
            continue
        rot = int(m.group(1))
        team = m.group(2).strip()
        # Skip junk like "200073 Draw" or pure numbers.
        if team.lower() in ("draw", "over", "under"):
            continue
        if rot in seen:
            continue
        seen.add(rot)
        rows.append((rot, team))

    # Sort by rotation so consecutive pairs (away=odd, home=even) line up.
    rows.sort(key=lambda r: r[0])
    return rows


def _pair_games(rows):
    """Group the ordered rows into games.

    Pattern: an ODD rotation (away) immediately followed by the next
    consecutive EVEN rotation (home) = one game.
    Returns list of dicts: {away:(rot,team), home:(rot,team)}.
    """
    games = []
    i = 0
    while i < len(rows) - 1:
        rot_a, team_a = rows[i]
        rot_b, team_b = rows[i + 1]
        # Away is odd and listed first; home is the next number (even).
        if rot_a % 2 == 1 and rot_b == rot_a + 1:
            games.append({"away": (rot_a, team_a), "home": (rot_b, team_b)})
            i += 2
        else:
            i += 1
    return games


def find_game(query: str):
    """Find the game matching the query.

    Returns (game_dict, matched_side) where matched_side is 'away',
    'home', or 'both' (for a matchup like "Arsenal/Burnley"), or
    (None, None) if not found.
    """
    raw_tokens = re.split(r"[\s/]+", query.strip())
    norm_tokens = [_normalize(t) for t in raw_tokens
                   if _normalize(t) and len(t) >= 3]
    if not norm_tokens:
        return None, None

    for url in SCRAPE_PAGES:
        html = _fetch_html(url)
        if not html:
            continue

        rows = _extract_rows(html)
        games = _pair_games(rows)

        best = None
        for g in games:
            na = _normalize(g["away"][1])
            nh = _normalize(g["home"][1])
            away_hit = any(t in na or na in _normalize(query)
                           for t in norm_tokens)
            home_hit = any(t in nh or nh in _normalize(query)
                           for t in norm_tokens)
            if away_hit and home_hit:
                return g, "both"
            if away_hit:
                best = best or (g, "away")
            elif home_hit:
                best = best or (g, "home")
        if best:
            return best

    return None, None


# ============================================================
# CORE: turn a message into the answer
# ============================================================

def process(text: str) -> str:
    parsed = parse_message(text)
    query = parsed["query"]

    if not query:
        return ("I couldn't read a team name from that. Use the format:\n"
                "Philadelphia Phillies -117, Mau = .552 Poly")

    game, side = find_game(query)

    if game is None:
        return (f"Couldn't find a game for \"{query}\". "
                f"It may not be posted yet, or the name didn't match. "
                f"Try city + team (e.g. 'Philadelphia Phillies') or "
                f"'Away/Home' for a total.")

    if parsed["is_total"]:
        # OVER  -> away team's number (odd, listed first)
        # UNDER -> home team's number (even, listed second)
        if parsed["is_over"]:
            number = game["away"][0]
        else:
            number = game["home"][0]
        return f"{number} {parsed['raw']}"

    # Moneyline / side: use whichever team the user named.
    if side == "home":
        number = game["home"][0]
    elif side == "away":
        number = game["away"][0]
    else:
        # Named both teams but no over/under -> default to away (listed first)
        number = game["away"][0]
    return f"{number} {parsed['raw']}"


def process_block(text: str) -> str:
    """Process a pasted block of multiple game lines.

    Returns the same lines, in order, each prefixed with its rotation
    number. Blank lines are preserved. Lines that can't be matched are
    returned unchanged with a marker so you can eyeball what failed.
    """
    out_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            out_lines.append("")          # keep spacing between groups
            continue
        try:
            result = process(line)
        except Exception as e:
            log.exception("Error on line: %s", line)
            result = f"[ERROR] {line}"
            _ = e
        # If process() returned an error/help message rather than a
        # "<number> <line>" result, the line had no market on the board
        # (futures, props, season bets) or the name didn't match. Either
        # way, return it with the 1000 placeholder so the format stays
        # consistent and you can fill in the real number yourself.
        if result.startswith(("Couldn't find", "I couldn't read")):
            out_lines.append(f"1000 {line}")
        else:
            out_lines.append(result)
    return "\n".join(out_lines)


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a game line and I'll prepend the rotation number.\n\n"
        "Examples:\n"
        "Philadelphia Phillies -117, Mau = .552 Poly\n"
        "Arsenal/Burnley over 3.5 +117, Mau = .460 Poly"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    n_lines = len([l for l in text.splitlines() if l.strip()])
    log.info("IN  (%d line(s))", n_lines)
    try:
        reply = process_block(text)
    except Exception as e:  # never let the bot die on one bad message
        log.exception("Error processing message")
        reply = f"Something went wrong: {e}"
    log.info("OUT (%d line(s))", len(reply.splitlines()))

    # Telegram caps a single message at 4096 chars. For very large
    # blocks, send in chunks split on line boundaries.
    if len(reply) <= 4000:
        await update.message.reply_text(reply)
        return
    chunk, size = [], 0
    for line in reply.split("\n"):
        if size + len(line) + 1 > 4000 and chunk:
            await update.message.reply_text("\n".join(chunk))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        await update.message.reply_text("\n".join(chunk))


def main():
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise SystemExit(
            "Set your bot token: edit BOT_TOKEN in the file, or set the "
            "TELEGRAM_BOT_TOKEN environment variable."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    log.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def _selftest():
    """Offline test using the real rotation data from the screenshots."""
    print("Self-test (offline, using real scoresandodds layout):")

    # Reconstructed exactly as scoresandodds renders it.
    sample_soccer = """
    <div>200073 Burnley FC</div>
    <div>200074 Arsenal</div>
    <div>200073 Draw</div>
    <div>212529 Slavia Sofia</div>
    <div>212530 Lokomotiv Sofia 1929</div>
    <div>212529 Draw</div>
    """
    sample_mlb = """
    <tr><td>951 Reds</td></tr>
    <tr><td>952 Phillies</td></tr>
    """

    import sys as _sys
    g = _sys.modules[__name__]
    orig = g._fetch_html
    orig_pages = g.SCRAPE_PAGES

    def fake(url):
        if "mlb" in url:
            return sample_mlb
        if "soccer" in url:
            return sample_soccer
        return None

    g._fetch_html = fake
    g.SCRAPE_PAGES = ["https://www.scoresandodds.com/mlb",
                      "https://www.scoresandodds.com/soccer/odds"]
    try:
        cases = [
            ("Philadelphia Phillies -117, Mau = .552 Poly",
             "952 Philadelphia Phillies -117, Mau = .552 Poly"),
            ("Arsenal/Burnley over 3.5 +117, Mau = .460 Poly",
             "200073 Arsenal/Burnley over 3.5 +117, Mau = .460 Poly"),
            ("Arsenal/Burnley under 3.5 -110, Mau = .460 Poly",
             "200074 Arsenal/Burnley under 3.5 -110, Mau = .460 Poly"),
            ("Slavia Sofia/Lokomotiv over 2.5 +100, Mau = .5 Poly",
             "212529 Slavia Sofia/Lokomotiv over 2.5 +100, Mau = .5 Poly"),
            ("Slavia Sofia/Lokomotiv under 2.5 +100, Mau = .5 Poly",
             "212530 Slavia Sofia/Lokomotiv under 2.5 +100, Mau = .5 Poly"),
        ]
        ok = True
        for inp, expected in cases:
            got = process(inp)
            mark = "OK " if got == expected else "FAIL"
            if got != expected:
                ok = False
            print(f"  [{mark}] {inp}")
            print(f"         -> {got}")
            if got != expected:
                print(f"         expected: {expected}")
        print("ALL PASSED" if ok else "SOME FAILED")
    finally:
        g._fetch_html = orig
        g.SCRAPE_PAGES = orig_pages


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        main()
