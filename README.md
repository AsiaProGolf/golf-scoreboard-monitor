# golf-scoreboard-monitor

A tiny, dependency-free status watcher for live golf scoreboards served by the
**wegolf / iyoupin** ("China Tour") live-scoring platform. It polls an event's
public JSON feed, watches a small set of fields (the weather/notice banner,
event status, current round), and **pings Telegram only when something
changes** — so you hear about a suspension, resumption, or weather delay the
moment it happens instead of refreshing a leaderboard all day.

- **Zero dependencies.** Python 3 standard library only — no `pip install`.
- **Config-driven.** One small JSON file says which event to watch and where to
  notify.
- **Secret-free repo.** The Telegram bot token is read from an environment
  variable, never stored in config or code.
- **Cron/launchd friendly.** Transient network errors exit quietly and retry on
  the next tick.

---

## How it works

1. Fetch the event profile JSON from the iyoupin endpoint.
2. Hash a configurable set of watched fields (default:
   `notice`, `eventStatus`, `recentlyEventRoundId`) into one sha256 key.
3. Compare that key to the last one saved in a `.state` file.
4. If it changed (or it's the very first run), send a Telegram message.
   - **First run** sends a one-time "monitor is live" baseline so you know it
     works.
   - Every later run is **silent unless the state actually moved.**

---

## The data source

This tool reads the public live-scoring API used by the wegolf / iyoupin
scoring platform:

```
https://scoringlive.iyoupin.top/api/wegolf/event/simple/profile?eventId={eventId}&shopId={shopId}
```

The response's `data` object includes:

| Field                       | Meaning                                                  |
| --------------------------- | -------------------------------------------------------- |
| `data.notice`               | The banner text (weather, suspension, general info)      |
| `data.eventStatus`          | A status code for the event                              |
| `data.recentlyEventRoundId` | The id of the round currently in focus                   |
| `data.rounds[]`             | List of rounds, each `{ eventRoundId, eventRoundName }`  |

The tool resolves `recentlyEventRoundId` to a readable round name via
`data.rounds[]`.

### Getting `eventId` and `shopId` from a scoreboard URL

A public scoreboard page carries both ids in a single query parameter named
`serid`, formatted as `{shopId}_{eventId}`:

```
https://<scoreboard-host>/...?serid={shopId}_{eventId}
```

So if a scoreboard link ends in `?serid=12345_67890`:

- `shopId`  = `12345` (the part **before** the underscore)
- `eventId` = `67890` (the part **after** the underscore)

Put those two values into your config.

---

## Setup

### 1. Create your config

Copy the example and fill in your values:

```bash
cp config.example.json config.json
```

```json
{
  "event_label": "My Tournament",
  "eventId": "67890",
  "shopId": "12345",
  "watch_fields": ["notice", "eventStatus", "recentlyEventRoundId"],
  "telegram_chat_id": "YOUR_CHAT_ID"
}
```

| Field              | Required | Notes                                                                 |
| ------------------ | -------- | --------------------------------------------------------------------- |
| `eventId`          | yes      | From the scoreboard `serid` (after the underscore).                   |
| `shopId`           | yes      | From the scoreboard `serid` (before the underscore).                  |
| `telegram_chat_id` | yes      | The chat/channel id the bot should message.                           |
| `watch_fields`     | no       | Fields under `data` to hash; defaults to the three shown above.       |
| `event_label`      | no       | A friendly name used in the message headline.                         |

`config.json` is git-ignored so your real ids never get committed.

### 2. Set the bot token (environment variable)

The Telegram bot token is **never** stored in the config or the code. Export it
in the environment the script runs in:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABCdef_your_bot_token_here"
```

> **To get a token:** message [@BotFather](https://t.me/BotFather) on Telegram,
> create a bot, and copy the token it gives you. To find a `chat_id`, message
> your bot, then read
> `https://api.telegram.org/bot<TOKEN>/getUpdates`.

### 3. Try a dry run (no token needed)

```bash
python3 scoreboard_monitor.py --config config.json --dry-run
```

This prints the message it *would* send and exits without contacting Telegram
and without touching the state file.

### 4. Run for real

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
python3 scoreboard_monitor.py --config config.json
```

The first run sends the "monitor is live" baseline; subsequent runs are silent
until the watched fields change.

### CLI options

```
--config PATH    Path to the JSON config (default: config.json)
--state PATH     Path to the state file (default: <config dir>/scoreboard_monitor.state)
--dry-run        Print what would be sent and exit; no token required
```

---

## Deploy

Run it on a timer (every few minutes during the event window). Two examples
follow; adjust paths, the polling interval, and the event window to taste.

### Linux — cron

Edit your crontab (`crontab -e`) and add a line that runs every 5 minutes. Note
that `cron` has a minimal environment, so set the token and use absolute paths:

```cron
# Poll the scoreboard every 5 minutes. Replace the token and paths.
*/5 * * * * TELEGRAM_BOT_TOKEN='123456:ABC...' /usr/bin/python3 /opt/golf-scoreboard-monitor/scoreboard_monitor.py --config /opt/golf-scoreboard-monitor/config.json >> /var/log/scoreboard-monitor.log 2>&1
```

For better secret hygiene, keep the token out of the crontab line and source it
from a file the cron user owns, e.g.:

```cron
*/5 * * * * . /home/youruser/.scoreboard-monitor.env && /usr/bin/python3 /opt/golf-scoreboard-monitor/scoreboard_monitor.py --config /opt/golf-scoreboard-monitor/config.json
```

where `~/.scoreboard-monitor.env` contains:

```bash
export TELEGRAM_BOT_TOKEN='123456:ABC...'
```

Remove the cron entry once the tournament is over.

### macOS — launchd

Save a plist like the one below to
`~/Library/LaunchAgents/com.example.scoreboard-monitor.plist`, then load it with
`launchctl load ~/Library/LaunchAgents/com.example.scoreboard-monitor.plist`.
It runs every 300 seconds (5 minutes).

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.scoreboard-monitor</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/golf-scoreboard-monitor/scoreboard_monitor.py</string>
        <string>--config</string>
        <string>/path/to/golf-scoreboard-monitor/config.json</string>
    </array>

    <!-- Provide the bot token to the job's environment. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>TELEGRAM_BOT_TOKEN</key>
        <string>123456:ABC...</string>
    </dict>

    <key>StartInterval</key>
    <integer>300</integer>

    <key>StandardOutPath</key>
    <string>/tmp/scoreboard-monitor.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/scoreboard-monitor.err.log</string>
</dict>
</plist>
```

Unload it when the event ends:
`launchctl unload ~/Library/LaunchAgents/com.example.scoreboard-monitor.plist`.

---

## Notes & limitations

- This watches **status-level** fields, not the full leaderboard. It tells you
  *that* something changed (play suspended, round rolled over), not the scores.
- The `notice` banner is whatever the scoring operator publishes; its wording
  and language are out of this tool's control.
- It depends on the public iyoupin endpoint remaining reachable and
  unchanged. If the API shape changes, the watched fields may need updating.

## License

[MIT](./LICENSE) — Copyright (c) 2026 Asia Pro Golf.
