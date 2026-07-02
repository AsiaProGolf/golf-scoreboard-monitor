#!/usr/bin/env python3
"""
golf-scoreboard-monitor — a tiny, config-driven status watcher for live golf
scoreboards served by the wegolf / iyoupin "China Tour" scoring platform.

It polls the public live-scoring JSON API for an event, hashes a small set of
watched fields (the notice/weather banner, the event status, the current round,
etc.), compares that hash to a saved state file, and only pings Telegram when
something changes. The first run sends a one-time "monitor is live" baseline so
you know it's working.

Why it exists: during a tournament you want to know the moment play is
suspended, resumed, delayed for weather, or the round rolls over — without
sitting on the leaderboard refreshing it yourself. This turns the scoreboard
into a quiet notifier that only speaks when the state actually moves.

Design goals:
  * Python 3 standard library ONLY — no pip install, no external deps.
  * Config-driven via a small JSON file (which event, which fields to watch,
    which Telegram chat to notify).
  * Zero secrets in the repo: the Telegram bot token is read from the
    TELEGRAM_BOT_TOKEN environment variable, never from config or code.
  * Safe to run on a cron / launchd timer: transient network errors exit
    quietly so the next tick retries.

Data source
-----------
Endpoint:
    https://scoringlive.iyoupin.top/api/wegolf/event/simple/profile
        ?eventId={eventId}&shopId={shopId}

The JSON response's `data` object carries (among other things):
    data.notice                -> the banner text (weather / suspension / info)
    data.eventStatus           -> a numeric/string status code for the event
    data.recentlyEventRoundId  -> id of the round currently in focus
    data.rounds[]              -> list of {eventRoundId, eventRoundName, ...}

The public scoreboard page URL carries the two ids together as a single query
param: `?serid={shopId}_{eventId}`. So from a scoreboard link like
`...?serid=12345_67890` you have shopId=12345 and eventId=67890. See the README.

Usage
-----
    export TELEGRAM_BOT_TOKEN="123456:ABC..."        # never commit this
    python3 scoreboard_monitor.py --config config.json

    # See exactly what it would send, no token required:
    python3 scoreboard_monitor.py --config config.json --dry-run
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request

# Base of the wegolf / iyoupin live-scoring profile endpoint.
API_BASE = "https://scoringlive.iyoupin.top/api/wegolf/event/simple/profile"

# Fields under `data` that, by default, we hash and watch for changes.
DEFAULT_WATCH_FIELDS = ["notice", "eventStatus", "recentlyEventRoundId"]

# Where the last-seen hash is stored if --state isn't given.
DEFAULT_STATE_PATH = "scoreboard_monitor.state"


def load_config(path):
    """Read and minimally validate the JSON config file."""
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    missing = [k for k in ("eventId", "shopId", "telegram_chat_id") if k not in cfg]
    if missing:
        sys.exit(
            "Config is missing required field(s): "
            + ", ".join(missing)
            + f" (in {path})"
        )

    # watch_fields is optional; fall back to the sensible default set.
    cfg.setdefault("watch_fields", list(DEFAULT_WATCH_FIELDS))
    if not isinstance(cfg["watch_fields"], list) or not cfg["watch_fields"]:
        sys.exit("Config 'watch_fields' must be a non-empty list of field names.")

    return cfg


def fetch(event_id, shop_id):
    """Fetch the event profile and return its `data` object (a dict)."""
    query = urllib.parse.urlencode({"eventId": event_id, "shopId": shop_id})
    url = f"{API_BASE}?{query}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    return payload.get("data", {}) or {}


def round_name(data, round_id):
    """Resolve a round id to its human-readable name, if present."""
    rounds = {
        str(r.get("eventRoundId")): r.get("eventRoundName")
        for r in data.get("rounds", []) or []
    }
    return rounds.get(str(round_id), str(round_id))


def compute_state(data, watch_fields):
    """Hash the watched fields into a single sha256 key for change detection."""
    parts = []
    for field in watch_fields:
        value = data.get(field)
        # Normalise: strip strings, stringify everything for a stable hash input.
        if isinstance(value, str):
            value = value.strip()
        parts.append(f"{field}={value}")
    blob = "|".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_message(cfg, data, first_run):
    """Compose the human-facing Telegram text for the current state."""
    label = cfg.get("event_label") or "Scoreboard"
    notice = (data.get("notice") or "").strip()
    status = data.get("eventStatus")
    round_id = data.get("recentlyEventRoundId")
    rname = round_name(data, round_id)

    if first_run:
        head = (
            f"{label} monitor is live. I'll only ping you when the status "
            f"changes. Current state:"
        )
    else:
        head = f"{label} — STATUS CHANGE:"

    notice_block = notice[:1500] if notice else "(no active notice)"
    return f"{head}\n\n{notice_block}\n\n[event status {status} | current round {rname}]"


def send_telegram(token, chat_id, text):
    """POST a message to the Telegram Bot API. Raises on transport error."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": str(chat_id),
            "text": text[:3900],  # stay under Telegram's 4096-char limit
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30):
        pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Config-driven change monitor for wegolf/iyoupin live-scoring events."
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to the JSON config file (default: config.json).",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="Path to the state file (default: <config dir>/"
        + DEFAULT_STATE_PATH
        + ").",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the message that WOULD be sent and exit without sending "
        "(no token required).",
    )
    return parser.parse_args(argv)


def resolve_state_path(args, config_path):
    """Default the state file next to the config file unless overridden."""
    if args.state:
        return args.state
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), DEFAULT_STATE_PATH)


def main(argv=None):
    args = parse_args(argv)
    cfg = load_config(args.config)
    state_path = resolve_state_path(args, args.config)

    # Fetch current state. Network hiccups are transient: exit 0 so the next
    # scheduled run simply tries again, without alarming logs.
    try:
        data = fetch(cfg["eventId"], cfg["shopId"])
    except Exception as exc:  # noqa: BLE001 - intentional broad catch for cron safety
        print(f"fetch failed (will retry next run): {exc}", file=sys.stderr)
        return 0

    key = compute_state(data, cfg["watch_fields"])

    first_run = not os.path.exists(state_path)
    prev = ""
    if not first_run:
        with open(state_path, "r", encoding="utf-8") as fh:
            prev = fh.read().strip()

    if key == prev:
        return 0  # nothing changed; stay silent

    message = build_message(cfg, data, first_run)

    if args.dry_run:
        print("--- DRY RUN: would send the following Telegram message ---")
        print(f"chat_id: {cfg['telegram_chat_id']}")
        print(message)
        print("--- (state file NOT updated in dry-run) ---")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit(
            "TELEGRAM_BOT_TOKEN is not set. Export your bot token, e.g.\n"
            "  export TELEGRAM_BOT_TOKEN='123456:ABC...'\n"
            "or run with --dry-run to preview without sending."
        )

    # Advance the saved state first so a flaky send doesn't loop on the same
    # change forever; the next genuine change will still notify.
    with open(state_path, "w", encoding="utf-8") as fh:
        fh.write(key)

    try:
        send_telegram(token, cfg["telegram_chat_id"], message)
    except Exception as exc:  # noqa: BLE001 - don't crash the timer on a send error
        print(f"telegram send failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
