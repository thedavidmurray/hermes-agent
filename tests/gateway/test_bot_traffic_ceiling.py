"""Fleet-wide bot-traffic ceiling and the bot-only kill switch (``BOT_ESTOP``).

The per-process ``BotLoopGuard`` only cools down and is multiplied by the number of gateways. These tests
drive two gateways (two runners sharing one Hermes root, as the 25 profile gateways share ``~/.hermes``)
through a bot ping-pong and require a finite number of admitted messages followed by a latch that stays
engaged until an operator removes the sentinel.
"""

import logging
from types import SimpleNamespace

import discord
import pytest

from gateway.bot_loop_guard import BotLoopGuard, BotLoopGuardSettings
from gateway.session import Platform, SessionSource

GROUP_CHAT = "-1001234567890"
OTHER_GROUP = "-1009876543210"
BOT_A = "111111111"
BOT_B = "222222222"
HUMAN = "100200300"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    for var in ("TELEGRAM_ALLOW_BOTS", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_ALLOW_ALL_USERS", "TELEGRAM_GROUP_ALLOWED_USERS",
                "TELEGRAM_GROUP_ALLOWED_CHATS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("TELEGRAM_ALLOW_BOTS", "mentions")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", HUMAN)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", f"{GROUP_CHAT},{OTHER_GROUP}")
    return root


@pytest.fixture
def clock():
    state = {"t": 1_900_000_000.0}
    return SimpleNamespace(now=lambda: state["t"], advance=lambda secs: state.__setitem__("t", state["t"] + secs))


def _ceiling(clock, max_events=6, window_seconds=600.0):
    from gateway.bot_traffic_ceiling import BotTrafficCeiling, CeilingSettings

    return BotTrafficCeiling(
        settings=lambda: CeilingSettings(enabled=True, max_events=max_events, window_seconds=window_seconds),
        clock=clock.now,
    )


def _runner(clock, profile, **ceiling_kw):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    # Generous per-process budget so the fleet ceiling is the limit under test.
    runner._bot_loop_guard = BotLoopGuard(
        settings=lambda: BotLoopGuardSettings(max_events=1000, window_seconds=60, cooldown_seconds=60), clock=clock.now)
    runner._bot_traffic_ceiling = _ceiling(clock, **ceiling_kw)
    runner._adapter_profile_for_source = lambda source: profile
    return runner


def _bot(user_id, message_id, chat_id=GROUP_CHAT):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="group", user_id=user_id,
                         user_name=f"Bot{user_id}", is_bot=True, message_id=message_id)


def _human(message_id="h1", chat_id=GROUP_CHAT):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="group", user_id=HUMAN,
                         user_name="David", is_bot=False, message_id=message_id)


def _inbound(runner, source):
    return runner._is_user_authorized(source) and runner._admit_bot_message(source)


def _sentinel(root):
    return root / "BOT_ESTOP"


def test_two_bots_acknowledging_each_other_admit_a_finite_count_then_latch(_isolate_env, clock, caplog):
    """Hive and Beau (separate gateways) volley acks: the fleet admits max_events, then latches for good."""
    hive, beau = _runner(clock, "hive"), _runner(clock, "beau")
    caplog.set_level(logging.WARNING)

    verdicts = []
    for turn in range(60):
        # Beau's message lands on Hive's gateway and vice versa.
        gateway, sender = (hive, BOT_B) if turn % 2 == 0 else (beau, BOT_A)
        verdicts.append(_inbound(gateway, _bot(sender, f"m{turn}")))
        clock.advance(5)

    assert sum(verdicts) == 6
    assert verdicts[:6] == [True] * 6
    assert _sentinel(_isolate_env).exists()

    # A latch, not a cooldown: hours later, in another chat, on either gateway, bots stay out.
    clock.advance(6 * 3600)
    assert _inbound(hive, _bot(BOT_B, "late-1")) is False
    assert _inbound(beau, _bot(BOT_A, "late-2", chat_id=OTHER_GROUP)) is False

    # Exactly one operator alert fleet-wide, and dropping more bot traffic never re-emits it.
    alerts = [r for r in caplog.records if "BOT TRAFFIC CEILING LATCHED" in r.getMessage()]
    assert len(alerts) == 1
    assert alerts[0].levelno == logging.ERROR

    # Manual reset re-arms admission.
    _sentinel(_isolate_env).unlink()
    assert _inbound(hive, _bot(BOT_B, "after-reset", chat_id=OTHER_GROUP)) is True


def test_duplicate_events_are_counted_once(_isolate_env, clock):
    hive = _runner(clock, "hive", max_events=2)

    for _ in range(4):  # a redelivered event (reconnect replay, busy-path drain) is not a new message
        assert hive._admit_bot_message(_bot(BOT_B, "m1")) is True
    assert hive._admit_bot_message(_bot(BOT_B, "m2")) is True
    assert not _sentinel(_isolate_env).exists()
    assert hive._admit_bot_message(_bot(BOT_B, "m3")) is False
    assert _sentinel(_isolate_env).exists()


def test_window_slides_so_slow_legitimate_traffic_never_latches(_isolate_env, clock):
    hive = _runner(clock, "hive", max_events=3, window_seconds=600)
    for i in range(20):
        assert _inbound(hive, _bot(BOT_B, f"slow{i}")) is True
        clock.advance(250)
    assert not _sentinel(_isolate_env).exists()


def test_ceiling_is_per_channel(_isolate_env, clock):
    hive = _runner(clock, "hive", max_events=2)
    assert [_inbound(hive, _bot(BOT_B, f"a{i}")) for i in range(2)] == [True, True]
    assert [_inbound(hive, _bot(BOT_B, f"b{i}", chat_id=OTHER_GROUP)) for i in range(2)] == [True, True]
    assert not _sentinel(_isolate_env).exists()


def test_human_message_is_still_admitted_under_latch(_isolate_env, clock):
    hive = _runner(clock, "hive", max_events=1)
    assert _inbound(hive, _bot(BOT_B, "m1")) is True
    assert _inbound(hive, _bot(BOT_B, "m2")) is False
    assert _sentinel(_isolate_env).exists()

    for i in range(10):
        assert _inbound(hive, _human(f"h{i}")) is True


def test_manual_kill_switch_drops_bots_and_keeps_humans(_isolate_env, clock):
    hive = _runner(clock, "hive")
    _sentinel(_isolate_env).touch()  # the operator's `touch ~/.hermes/BOT_ESTOP`, empty body

    assert hive._is_user_authorized(_bot(BOT_B, "m1")) is False
    assert hive._admit_bot_message(_bot(BOT_B, "m1")) is False
    assert _inbound(hive, _human()) is True


def test_kill_switch_in_profile_home_is_honored_by_that_profile(tmp_path, monkeypatch, clock):
    profile_home = tmp_path / "hermes-root" / "profiles" / "hive"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    (profile_home / "BOT_ESTOP").touch()
    hive = _runner(clock, "hive")
    assert _inbound(hive, _bot(BOT_B, "m1")) is False
    assert _inbound(hive, _human()) is True


@pytest.mark.asyncio
async def test_kill_switch_works_at_ingress_while_the_model_is_unavailable(_isolate_env, clock, monkeypatch):
    """The switch is a file stat in the ingress gate: no model, provider or tool is consulted."""
    from gateway.platforms.base import MessageEvent

    hive = _runner(clock, "hive")

    def _model_down(*_a, **_kw):
        raise RuntimeError("model unavailable")

    for name in ("_run_agent", "_handle_message_with_agent", "_create_agent"):
        setattr(hive, name, _model_down)
    hive._scale_to_zero_note_real_inbound = lambda: None

    async def _passthrough_hook(event, source):
        return event
    hive._hm_pre_gateway_dispatch_hook = _passthrough_hook

    _sentinel(_isolate_env).touch()
    assert await hive._hm_admit_event(MessageEvent(text="<@hive> ack", message_id="b1", source=_bot(BOT_B, "b1"))) is None
    admitted = await hive._hm_admit_event(MessageEvent(text="status?", message_id="h1", source=_human("h1")))
    assert admitted is not None and admitted[0].source.user_id == HUMAN


def test_unwritable_counter_fails_closed_for_bots_only(_isolate_env, clock, monkeypatch):
    hive = _runner(clock, "hive")
    monkeypatch.setattr(hive._bot_traffic_ceiling, "_db_path", lambda: _isolate_env / "missing-dir" / "x" / "db")
    import gateway.bot_traffic_ceiling as btc
    monkeypatch.setattr(btc.Path, "mkdir", lambda *a, **kw: (_ for _ in ()).throw(OSError("read-only")))
    assert _inbound(hive, _bot(BOT_B, "m1")) is False
    assert _inbound(hive, _human()) is True


def test_ceiling_reads_config_yaml(_isolate_env):
    from gateway.bot_traffic_ceiling import load_settings

    (_isolate_env / "config.yaml").write_text(
        "gateway:\n  bot_traffic_ceiling:\n    max_events: 4\n    window_seconds: 120\n")
    s = load_settings()
    assert (s.enabled, s.max_events, s.window_seconds) == (True, 4, 120.0)


# --- Discord adapter: admission + send path --------------------------------------------------------

def _discord_adapter():
    from unittest.mock import Mock

    from gateway.platforms.helpers import MessageDeduplicator
    from plugins.platforms.discord.adapter import DiscordAdapter

    users = {99: SimpleNamespace(id=99, bot=True, name="hive"), 42: SimpleNamespace(id=42, bot=True, name="beau"),
             7: SimpleNamespace(id=7, bot=False, name="david")}
    adapter = object.__new__(DiscordAdapter)
    adapter.config = SimpleNamespace(extra={})
    adapter._client = SimpleNamespace(user=users[99], get_user=users.get)
    adapter._dedup = MessageDeduplicator()
    adapter._get_allow_bots = Mock(return_value="mentions")
    adapter._is_allowed_user = Mock(return_value=True)
    adapter._text_batch_delay_seconds = 0.6
    adapter._text_batch_split_delay_seconds = 2.0
    adapter._bot_tag_debounce_until = {}
    return adapter


def _discord_msg(author, content, mid=123):
    return SimpleNamespace(id=mid, author=author, channel=SimpleNamespace(id=5), content=content,
                           mentions=[], type=discord.MessageType.default)


def test_discord_admission_drops_mentioned_bot_under_kill_switch(_isolate_env, monkeypatch):
    monkeypatch.delenv("DISCORD_BOTS_REQUIRE_INLINE_MENTION", raising=False)
    monkeypatch.setenv("DISCORD_IGNORE_NO_MENTION", "false")
    adapter = _discord_adapter()
    bot_msg = _discord_msg(SimpleNamespace(id=42, bot=True), "<@99> ACK")
    assert adapter._discord_message_admission(bot_msg, claim=False)[0] is True

    _sentinel(_isolate_env).touch()
    assert adapter._discord_message_admission(bot_msg, claim=False)[0] is False
    human_msg = _discord_msg(SimpleNamespace(id=7, bot=False), "<@99> status?", mid=124)
    assert adapter._discord_message_admission(human_msg, claim=False)[0] is True


def test_discord_outbound_bot_mentions_are_neutralized_under_kill_switch(_isolate_env):
    adapter = _discord_adapter()
    text = "done <@42> and <@!42>, cc <@7>, unknown <@555>"
    assert adapter.format_message(text) == text

    _sentinel(_isolate_env).touch()
    out = adapter.format_message(text)
    assert "<@42>" not in out and "<@!42>" not in out and "<@555>" not in out
    assert "<@7>" in out  # human pings keep working
    assert "@beau" in out


def test_same_message_admitted_by_two_gateways_counts_twice(_isolate_env, clock, monkeypatch):
    """Two non-multiplex gateways (adapter profile "") that both admit one message are two model wakes."""
    import gateway.bot_traffic_ceiling as btc

    hive, beau = _runner(clock, "", max_events=1), _runner(clock, "", max_events=1)
    monkeypatch.setattr(btc.estop, "_hermes_home", lambda: _isolate_env / "profiles" / "hive")
    assert hive._admit_bot_message(_bot(BOT_A, "shared")) is True
    monkeypatch.setattr(btc.estop, "_hermes_home", lambda: _isolate_env / "profiles" / "beau")
    assert beau._admit_bot_message(_bot(BOT_A, "shared")) is False
    assert _sentinel(_isolate_env).exists()


def test_concurrent_gateways_admit_exactly_the_ceiling_and_alert_once(_isolate_env, caplog):
    """Separate connections contend on the shared counter the way 25 gateway processes do."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from gateway.bot_traffic_ceiling import BotTrafficCeiling, CeilingSettings

    caplog.set_level(logging.ERROR)
    ceiling = BotTrafficCeiling(settings=lambda: CeilingSettings(max_events=10, window_seconds=600), clock=time.time)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: ceiling.admit("discord", "backroom", f"m{i}", f"p{i % 8}")[0], range(40)))
    assert sum(results) == 10
    assert _sentinel(_isolate_env).exists()
    assert len([r for r in caplog.records if "BOT TRAFFIC CEILING LATCHED" in r.getMessage()]) == 1
