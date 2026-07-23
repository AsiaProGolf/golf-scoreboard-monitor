import hashlib
import io
import json
import urllib.parse

import pytest

import scoreboard_monitor


def _write_config(tmp_path, **overrides):
    config = {
        "eventId": "event-7",
        "shopId": "shop-3",
        "telegram_chat_id": "-100123",
        "event_label": "Open Championship",
    }
    config.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_load_config_applies_default_watch_fields_without_sharing_the_constant(
    tmp_path,
):
    path = _write_config(tmp_path)

    config = scoreboard_monitor.load_config(path)

    assert config["watch_fields"] == scoreboard_monitor.DEFAULT_WATCH_FIELDS
    assert config["watch_fields"] is not scoreboard_monitor.DEFAULT_WATCH_FIELDS


@pytest.mark.parametrize(
    "payload",
    [
        {"eventId": "1", "shopId": "2"},
        {
            "eventId": "1",
            "shopId": "2",
            "telegram_chat_id": "3",
            "watch_fields": [],
        },
        {
            "eventId": "1",
            "shopId": "2",
            "telegram_chat_id": "3",
            "watch_fields": "notice",
        },
    ],
)
def test_load_config_rejects_missing_or_invalid_fields(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SystemExit):
        scoreboard_monitor.load_config(path)


def test_fetch_builds_encoded_request_and_returns_data(monkeypatch):
    captured = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return Response(b'{"data": {"notice": "Play suspended"}}')

    monkeypatch.setattr(scoreboard_monitor.urllib.request, "urlopen", fake_urlopen)

    data = scoreboard_monitor.fetch("event id", "shop/3")

    assert data == {"notice": "Play suspended"}
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
    assert query == {"eventId": ["event id"], "shopId": ["shop/3"]}
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["timeout"] == 30


def test_fetch_normalizes_missing_or_null_data_to_empty_dict(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        scoreboard_monitor.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b'{"data": null}'),
    )

    assert scoreboard_monitor.fetch("event", "shop") == {}


def test_round_name_matches_mixed_id_types_and_falls_back_to_raw_id():
    data = {
        "rounds": [
            {"eventRoundId": 4, "eventRoundName": "Final Round"},
            {"eventRoundId": "3", "eventRoundName": "Round Three"},
        ]
    }

    assert scoreboard_monitor.round_name(data, "4") == "Final Round"
    assert scoreboard_monitor.round_name(data, 3) == "Round Three"
    assert scoreboard_monitor.round_name(data, 99) == "99"
    assert scoreboard_monitor.round_name({"rounds": None}, "x") == "x"


def test_compute_state_normalizes_string_whitespace_and_honors_field_order():
    first = scoreboard_monitor.compute_state(
        {"notice": "  Delayed  ", "eventStatus": 2},
        ["notice", "eventStatus"],
    )
    same = scoreboard_monitor.compute_state(
        {"notice": "Delayed", "eventStatus": 2},
        ["notice", "eventStatus"],
    )
    reversed_fields = scoreboard_monitor.compute_state(
        {"notice": "Delayed", "eventStatus": 2},
        ["eventStatus", "notice"],
    )

    expected_blob = "notice=Delayed|eventStatus=2"
    assert first == same == hashlib.sha256(expected_blob.encode()).hexdigest()
    assert reversed_fields != first


def test_build_message_covers_first_run_change_round_and_notice_limit():
    cfg = {"event_label": "Bangkok Open"}
    data = {
        "notice": "A" * 1600,
        "eventStatus": 5,
        "recentlyEventRoundId": 2,
        "rounds": [{"eventRoundId": 2, "eventRoundName": "Round 2"}],
    }

    baseline = scoreboard_monitor.build_message(cfg, data, first_run=True)
    changed = scoreboard_monitor.build_message(cfg, data, first_run=False)

    assert "monitor is live" in baseline
    assert "Bangkok Open — STATUS CHANGE:" in changed
    assert "current round Round 2" in changed
    assert "A" * 1500 in changed
    assert "A" * 1501 not in changed
    assert "(no active notice)" in scoreboard_monitor.build_message(
        {}, {}, first_run=False
    )


def test_send_telegram_posts_encoded_and_truncated_message(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(scoreboard_monitor.urllib.request, "urlopen", fake_urlopen)

    scoreboard_monitor.send_telegram("secret-token", -123, "x" * 5000)

    request = captured["request"]
    body = urllib.parse.parse_qs(request.data.decode("utf-8"))
    assert request.full_url.endswith("/botsecret-token/sendMessage")
    assert body["chat_id"] == ["-123"]
    assert body["text"] == ["x" * 3900]
    assert body["disable_web_page_preview"] == ["true"]
    assert captured["timeout"] == 30


def test_resolve_state_path_defaults_next_to_config_and_accepts_override(tmp_path):
    config = tmp_path / "nested" / "config.json"

    default = scoreboard_monitor.resolve_state_path(
        scoreboard_monitor.parse_args(["--config", str(config)]), str(config)
    )
    explicit = scoreboard_monitor.resolve_state_path(
        scoreboard_monitor.parse_args(
            ["--config", str(config), "--state", "custom.state"]
        ),
        str(config),
    )

    assert default == str(config.parent / scoreboard_monitor.DEFAULT_STATE_PATH)
    assert explicit == "custom.state"


def test_main_dry_run_prints_message_without_state_or_token(
    tmp_path, monkeypatch, capsys
):
    config = _write_config(tmp_path)
    state = tmp_path / "state"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(
        scoreboard_monitor,
        "fetch",
        lambda event_id, shop_id: {
            "notice": "Weather delay",
            "eventStatus": 1,
            "recentlyEventRoundId": "R1",
        },
    )

    result = scoreboard_monitor.main(
        ["--config", str(config), "--state", str(state), "--dry-run"]
    )

    assert result == 0
    assert not state.exists()
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Weather delay" in output
    assert "chat_id: -100123" in output


def test_main_stays_silent_when_state_is_unchanged(tmp_path, monkeypatch, capsys):
    config = _write_config(tmp_path)
    state = tmp_path / "state"
    data = {"notice": "Clear", "eventStatus": 2, "recentlyEventRoundId": 1}
    cfg = scoreboard_monitor.load_config(config)
    state.write_text(
        scoreboard_monitor.compute_state(data, cfg["watch_fields"]),
        encoding="utf-8",
    )
    monkeypatch.setattr(scoreboard_monitor, "fetch", lambda *_args: data)
    monkeypatch.setattr(
        scoreboard_monitor,
        "send_telegram",
        lambda *_args: pytest.fail("unchanged state must not send"),
    )

    assert scoreboard_monitor.main(
        ["--config", str(config), "--state", str(state)]
    ) == 0
    assert capsys.readouterr().out == ""


def test_main_sends_change_and_persists_new_state(tmp_path, monkeypatch):
    config = _write_config(tmp_path)
    state = tmp_path / "state"
    data = {"notice": "Play resumed", "eventStatus": 3, "recentlyEventRoundId": 2}
    sent = {}
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-123")
    monkeypatch.setattr(scoreboard_monitor, "fetch", lambda *_args: data)
    monkeypatch.setattr(
        scoreboard_monitor,
        "send_telegram",
        lambda token, chat_id, text: sent.update(
            token=token, chat_id=chat_id, text=text
        ),
    )

    assert scoreboard_monitor.main(
        ["--config", str(config), "--state", str(state)]
    ) == 0

    cfg = scoreboard_monitor.load_config(config)
    assert state.read_text(encoding="utf-8") == scoreboard_monitor.compute_state(
        data, cfg["watch_fields"]
    )
    assert sent["token"] == "token-123"
    assert sent["chat_id"] == "-100123"
    assert "Play resumed" in sent["text"]


def test_main_handles_fetch_and_send_failures_with_timer_friendly_codes(
    tmp_path, monkeypatch, capsys
):
    config = _write_config(tmp_path)
    state = tmp_path / "state"

    monkeypatch.setattr(
        scoreboard_monitor,
        "fetch",
        lambda *_args: (_ for _ in ()).throw(OSError("offline")),
    )
    assert scoreboard_monitor.main(["--config", str(config)]) == 0
    assert "fetch failed" in capsys.readouterr().err

    monkeypatch.setattr(scoreboard_monitor, "fetch", lambda *_args: {"notice": "x"})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(
        scoreboard_monitor,
        "send_telegram",
        lambda *_args: (_ for _ in ()).throw(OSError("telegram down")),
    )
    assert scoreboard_monitor.main(
        ["--config", str(config), "--state", str(state)]
    ) == 1
    assert state.exists()
    assert "telegram send failed" in capsys.readouterr().err


def test_main_requires_token_for_a_real_change(tmp_path, monkeypatch):
    config = _write_config(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(scoreboard_monitor, "fetch", lambda *_args: {"notice": "x"})

    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN is not set"):
        scoreboard_monitor.main(["--config", str(config)])
