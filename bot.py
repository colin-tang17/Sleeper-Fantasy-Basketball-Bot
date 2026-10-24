"""Sleeper NBA bench alert bot.

Run every ~5 minutes. If any NBA game tips off within LEAD_MINUTES and you have
a bench player on a team in that game, post a Discord webhook message.

Settings come from config.json next to this file. Environment variables
(e.g. GitHub Actions secrets) override anything in the file.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"config.json is not valid JSON: {e}")


CFG = load_config()


def setting(env_name, key, default=""):
    """Env var wins, then config.json, then the default."""
    val = os.environ.get(env_name)
    if val not in (None, ""):
        return val
    val = CFG.get(key)
    return default if val in (None, "") else val


SLEEPER = "https://api.sleeper.app/v1"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}"

USERNAME = str(setting("SLEEPER_USERNAME", "sleeper_username"))
WEBHOOK = str(setting("DISCORD_WEBHOOK_URL", "discord_webhook_url"))
DISCORD_USER_ID = str(setting("DISCORD_USER_ID", "discord_user_id"))  # optional, to @ you
LEAD_MINUTES = int(setting("LEAD_MINUTES", "lead_minutes", 15))
DRY_RUN = str(setting("DRY_RUN", "dry_run", False)).lower() in ("1", "true", "yes")

_ignore = setting("IGNORE_PLAYERS", "ignore_players", [])
if isinstance(_ignore, str):
    _ignore = _ignore.split(",")
IGNORE = {n.strip().lower() for n in _ignore if n.strip()}

STATE_FILE = BASE / "state" / "sent.json"
DIRTY_MARKER = BASE / ".dirty"
LOCAL_TZ = ZoneInfo("America/New_York")

# Sleeper team abbreviations -> ESPN/NBA tricodes where they differ.
TEAM_MAP = {
    "GS": "GSW",
    "NO": "NOP",
    "NY": "NYK",
    "SA": "SAS",
    "PHO": "PHX",
    "UTAH": "UTA",
    "WSH": "WAS",
}


def get(url, headers=None):
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def norm_team(abbr):
    if not abbr:
        return None
    abbr = abbr.upper()
    return TEAM_MAP.get(abbr, abbr)


def upcoming_games(now):
    """Games tipping off within the next LEAD_MINUTES."""
    today = now.astimezone(LOCAL_TZ).date()
    out, seen = [], set()
    # Check today and tomorrow (ET), since ESPN groups games by Eastern date.
    for offset in (0, 1):
        d = (today + timedelta(days=offset)).strftime("%Y%m%d")
        for ev in get(ESPN_URL.format(d=d)).get("events", []):
            if ev["id"] in seen:
                continue
            seen.add(ev["id"])
            tip = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            mins = (tip - now).total_seconds() / 60
            if 0 < mins <= LEAD_MINUTES:
                comps = ev["competitions"][0]["competitors"]
                teams = {norm_team(c["team"]["abbreviation"]) for c in comps}
                out.append({"id": ev["id"], "tip": tip, "teams": teams})
    return out


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    cutoff = time.time() - 2 * 86400
    state = {k: v for k, v in state.items() if v > cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def send(content):
    if DRY_RUN or not WEBHOOK:
        print("[dry run] would send:\n" + content)
        return
    r = requests.post(WEBHOOK, json={"content": content}, timeout=30)
    r.raise_for_status()


def main():
    if not USERNAME:
        sys.exit("No Sleeper username set. Fill in sleeper_username in config.json.")

    now = datetime.now(timezone.utc)
    games = upcoming_games(now)
    if not games:
        print("No games tipping off soon.")
        return

    # Only hit Sleeper (and the big players file) when there's something to check.
    user_id = get(f"{SLEEPER}/user/{USERNAME}")["user_id"]
    season = get(f"{SLEEPER}/state/nba")["season"]
    leagues = get(f"{SLEEPER}/user/{user_id}/leagues/nba/{season}")
    players = get(f"{SLEEPER}/players/nba")

    state = load_state()
    sent_any = False

    for game in games:
        tip_local = game["tip"].astimezone(LOCAL_TZ)
        tip_str = f"{tip_local.hour % 12 or 12}:{tip_local:%M} {tip_local:%p} ET"
        lines = []
        new_keys = []

        for lg in leagues:
            rosters = get(f"{SLEEPER}/league/{lg['league_id']}/rosters")
            mine = next(
                (
                    r
                    for r in rosters
                    if r.get("owner_id") == user_id or user_id in (r.get("co_owners") or [])
                ),
                None,
            )
            if not mine:
                continue

            not_bench = (
                set(mine.get("starters") or [])
                | set(mine.get("reserve") or [])
                | set(mine.get("taxi") or [])
            )
            bench = [p for p in (mine.get("players") or []) if p not in not_bench]

            for pid in bench:
                p = players.get(pid) or {}
                if norm_team(p.get("team")) not in game["teams"]:
                    continue
                name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                if p.get("injury_status") == "Out" or name.lower() in IGNORE:
                    continue
                key = f"{game['id']}:{lg['league_id']}:{pid}"
                if key in state:
                    continue
                lines.append(f"• **{name}** ({p.get('team')}) on the bench in *{lg['name']}*")
                new_keys.append(key)

        if lines:
            ping = f"<@{DISCORD_USER_ID}> " if DISCORD_USER_ID else ""
            send(f"{ping}🏀 Tip-off at {tip_str}:\n" + "\n".join(lines))
            if not DRY_RUN:
                for k in new_keys:
                    state[k] = time.time()
                sent_any = True

    if sent_any:
        save_state(state)
        DIRTY_MARKER.write_text("1")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
