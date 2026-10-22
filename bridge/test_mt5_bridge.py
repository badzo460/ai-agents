"""Offline safety tests; no terminal, credentials, or network access required."""

import copy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib.util
import inspect
import io
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


_SPEC = importlib.util.spec_from_file_location(
    "mt5_bridge", Path(__file__).with_name("mt5_bridge.py")
)
bridge = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bridge
_SPEC.loader.exec_module(bridge)

NOW = 1_790_000_000_000


def snapshot():
    return {
        "symbol": "XAUUSD",
        "timestamp": NOW,
        "bid": 2350.00,
        "ask": 2350.20,
        "point": 0.01,
        "tickSize": 0.01,
        "tickValue": 1,
        "volumeMin": 0.01,
        "volumeMax": 100,
        "volumeStep": 0.01,
        "balance": 10000,
        "equity": 10000,
        "accountId": "123456",
        "accountMode": "demo",
        "currency": "USD",
        "dailyPnl": 0,
        "positions": [],
        "candles": [
            {
                "time": NOW // 1000 - (119 - n) * 300,
                "open": 2350,
                "high": 2351,
                "low": 2349,
                "close": 2350,
                "volume": 100,
            }
            for n in range(120)
        ],
    }


def command():
    return {
        "id": "00000000-1111-2222-3333-444444444444",
        "createdAt": NOW - 500,
        "expiresAt": NOW + 30000,
        "symbol": "XAUUSD",
        "side": "BUY",
        "volume": 0.1,
        "stopLoss": 2345.20,
        "takeProfit": 2360.20,
        "maxSpreadPoints": 50,
        "accountId": "123456",
        "accountMode": "demo",
        "referencePrice": 2350.20,
        "maxDeviationPoints": 50,
    }


class GoldSymbolTests(unittest.TestCase):
    def test_gold_and_explicit_suffixes_are_supported(self):
        for value in ("XAUUSD", "xauusd", "XAUUSD.m", "XAUUSD-pro", "XAUUSD_STD"):
            with self.subTest(value=value):
                self.assertTrue(bridge.is_gold_symbol(value))

    def test_arbitrary_symbols_and_ambiguous_suffixes_are_blocked(self):
        for value in (
            "EURUSD", "XAGUSD", "GOLD", "XAUUSDJPY", "XAUUSD;EURUSD",
            " XAUUSD", "XAUUSD ", "XAUUSD.", "XAUUSD../EURUSD", "", None,
        ):
            with self.subTest(value=value):
                self.assertFalse(bridge.is_gold_symbol(value))


class AccountIdentityTests(unittest.TestCase):
    def test_broker_server_is_part_of_identity(self):
        demo = bridge.account_identity(SimpleNamespace(login=123456, server="PUPrime-Demo"))
        live = bridge.account_identity(SimpleNamespace(login=123456, server="PUPrime-Live"))
        self.assertEqual(demo, "123456@PUPrime-Demo")
        self.assertNotEqual(demo, live)

    def test_missing_malformed_or_unbounded_server_is_rejected(self):
        for server in ("", None, " PUPrime", "PUPrime\nLive", "@Live", "X" * 101):
            with self.subTest(server=server), self.assertRaises(bridge.BridgeError):
                bridge.account_identity(SimpleNamespace(login=123456, server=server))


class TerminalSnapshotTests(unittest.TestCase):
    """Exercise the actual MT5 adapter instead of mocking its whole result."""

    def terminal(self, tick=None):
        mt5 = mock.Mock()
        mt5.ACCOUNT_TRADE_MODE_DEMO = 0
        mt5.ACCOUNT_TRADE_MODE_REAL = 2
        mt5.POSITION_TYPE_BUY = 0
        mt5.TIMEFRAME_M5 = 5
        mt5.DEAL_TYPE_BUY = 0
        mt5.DEAL_TYPE_SELL = 1
        mt5.account_info.return_value = SimpleNamespace(
            login=123456, server="PUPrime-Demo", trade_mode=0,
            balance=10000, equity=10000, currency="USD",
        )
        mt5.terminal_info.return_value = SimpleNamespace(connected=True)
        mt5.symbol_info.return_value = SimpleNamespace(
            currency_base="XAU", currency_profit="USD", point=0.01,
            trade_tick_size=0.01, trade_tick_value=1,
            trade_tick_value_loss=1, volume_min=0.01,
            volume_max=100, volume_step=0.01,
        )
        mt5.symbol_select.return_value = True
        mt5.symbol_info_tick.return_value = tick or SimpleNamespace(
            time_msc=NOW - 1000, time=NOW // 1000 - 1,
            bid=2350.0, ask=2350.2,
        )
        mt5.copy_rates_from_pos.return_value = [
            {**bar, "tick_volume": bar["volume"]}
            for bar in snapshot()["candles"]
        ]
        mt5.positions_get.return_value = []
        mt5.history_deals_get.return_value = []
        return mt5

    def test_snapshot_uses_actual_millisecond_tick_time(self):
        tick = SimpleNamespace(time_msc=NOW - 1234, time=NOW // 1000 - 99, bid=2350, ask=2350.2)
        mt5 = self.terminal(tick)
        with mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
            result = bridge.snapshot_from_terminal(mt5, "XAUUSD")
        self.assertEqual(result["timestamp"], NOW - 1234)
        self.assertEqual(result["accountId"], "123456@PUPrime-Demo")
        self.assertEqual(len(result["candles"]), 120)

    def test_snapshot_falls_back_to_seconds_when_millisecond_time_absent_or_zero(self):
        for milliseconds in (None, 0):
            tick = SimpleNamespace(time=NOW // 1000 - 2, bid=2350, ask=2350.2)
            if milliseconds is not None:
                tick.time_msc = milliseconds
            with self.subTest(milliseconds=milliseconds), mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                result = bridge.snapshot_from_terminal(self.terminal(tick), "XAUUSD")
            self.assertEqual(result["timestamp"], NOW - 2000)

    def test_snapshot_rejects_old_price_and_reports_distinct_future_clock_error(self):
        messages = []
        for stamp in (NOW - 90000, NOW + 90000):
            tick = SimpleNamespace(time_msc=stamp, time=stamp // 1000, bid=2350, ask=2350.2)
            with mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                with self.assertRaises(bridge.BridgeError) as caught:
                    bridge.snapshot_from_terminal(self.terminal(tick), "XAUUSD")
            messages.append(str(caught.exception))
        self.assertNotEqual(*messages)
        self.assertTrue("buduć" in messages[1].lower() or "ispred" in messages[1].lower(), messages[1])
        self.assertIn("90", messages[0])

    def test_snapshot_preserves_existing_thirty_second_and_five_second_boundaries(self):
        for offset, accepted in ((-30000, True), (-30001, False), (5000, True), (5001, False)):
            tick = SimpleNamespace(time_msc=NOW + offset, time=(NOW + offset) // 1000, bid=2350, ask=2350.2)
            with self.subTest(offset=offset), mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                if accepted:
                    self.assertEqual(bridge.snapshot_from_terminal(self.terminal(tick), "XAUUSD")["timestamp"], NOW + offset)
                else:
                    with self.assertRaises(bridge.BridgeError):
                        bridge.snapshot_from_terminal(self.terminal(tick), "XAUUSD")

    def test_explicit_telemetry_mode_preserves_old_or_future_broker_timestamp(self):
        for stamp in (NOW - 90000, NOW + 90000):
            tick = SimpleNamespace(time_msc=stamp, time=stamp // 1000, bid=2350, ask=2350.2)
            with self.subTest(stamp=stamp), mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                result = bridge.snapshot_from_terminal(self.terminal(tick), "XAUUSD", require_fresh=False)
                self.assertEqual(result["timestamp"], stamp)
                self.assertEqual(result["balance"], 10000)
                with self.assertRaises(bridge.BridgeError):
                    bridge.snapshot_from_terminal(self.terminal(tick), "XAUUSD")

    def test_telemetry_mode_still_rejects_missing_or_nonfinite_broker_time(self):
        for value in (0, math.nan, math.inf, -math.inf):
            tick = SimpleNamespace(time_msc=value, time=value, bid=2350, ask=2350.2)
            with self.subTest(value=value), mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                with self.assertRaises(bridge.BridgeError):
                    bridge.snapshot_from_terminal(self.terminal(tick), "XAUUSD", require_fresh=False)

    def test_slow_history_loading_reads_latest_tick_after_loading_finishes(self):
        mt5 = self.terminal()
        clock = [NOW / 1000]
        calls = []

        def load_history(*args):
            calls.append("history")
            clock[0] += 45
            return []

        def current_tick(symbol):
            calls.append("tick")
            stamp = int(clock[0] * 1000)
            return SimpleNamespace(time_msc=stamp, time=stamp // 1000, bid=2350, ask=2350.2)

        mt5.history_deals_get.side_effect = load_history
        mt5.symbol_info_tick.side_effect = current_tick
        with mock.patch.object(bridge.time, "time", side_effect=lambda: clock[0]):
            result = bridge.snapshot_from_terminal(mt5, "XAUUSD")
        self.assertGreater(calls.index("tick"), calls.index("history"))
        self.assertEqual(result["timestamp"], NOW + 45000)
        self.assertEqual(result["bid"], 2350)


class DiagnosticTests(unittest.TestCase):
    terminal = TerminalSnapshotTests.terminal

    def diagnose(self, tick_stamps, broker_utc_offset_hours=0):
        mt5 = self.terminal()
        clock = [NOW / 1000]
        ticks = iter([
            SimpleNamespace(time_msc=stamp, time=stamp // 1000, bid=2350, ask=2350.2)
            for stamp in tick_stamps
        ])
        mt5.symbol_info_tick.side_effect = lambda symbol: next(ticks)
        output = io.StringIO()
        with mock.patch.object(bridge.time, "time", side_effect=lambda: clock[0]), mock.patch.object(bridge.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)) as sleep, redirect_stdout(output):
            result = bridge.diagnose_terminal(mt5, "XAUUSD", interval=5, broker_utc_offset_hours=broker_utc_offset_hours)
        sleep.assert_called_once_with(5)
        self.assertEqual(result, 0)
        for method in (mt5.account_info, mt5.symbol_select, mt5.copy_rates_from_pos, mt5.history_deals_get, mt5.positions_get, mt5.orders_get, mt5.order_send):
            method.assert_not_called()
        return output.getvalue()

    def test_diagnosis_distinguishes_moving_quote_timestamps_from_static_quotes(self):
        moving = self.diagnose([NOW - 1000, NOW + 4000])
        static = self.diagnose([NOW - 1000, NOW - 1000])
        self.assertIn("Tick napreduje: DA", moving)
        self.assertIn("Tick napreduje: NE", static)
        self.assertIn("Terminal povezan: DA", moving)
        self.assertIn("Simbol: XAUUSD", moving)
        self.assertIn("PC UTC:", moving)
        self.assertIn("Tick UTC:", moving)
        self.assertIn("Izvor vremena: time_msc", moving)

    def test_diagnosis_keeps_negative_clock_offset_visible_without_changing_timestamps(self):
        output = self.diagnose([NOW + 60000, NOW + 65000])
        self.assertIn("-60.000", output)
        self.assertIn("Tick napreduje: DA", output)

    def test_explicit_broker_offset_diagnosis_shows_raw_and_converted_quote_time(self):
        broker_offset = 3 * 3600 * 1000
        output = self.diagnose([NOW + broker_offset - 1000, NOW + broker_offset + 4000], broker_utc_offset_hours=3)
        raw_stamp = str(NOW + broker_offset - 1000)
        converted_utc = datetime.fromtimestamp((NOW - 1000) / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.assertIn(raw_stamp, output)
        self.assertIn(converted_utc, output)
        self.assertIn("+1.000", output)
        self.assertIn("Tick napreduje: DA", output)
        self.assertNotIn("-10799.000", output)

    def test_read_only_mode_requires_no_url_token_requests_or_trade_ledger(self):
        mt5 = self.terminal()
        mt5.initialize.return_value = True
        mt5.terminal_info.return_value.path = r"C:\PRIVATE_TERMINAL_PATH"
        mt5.account_info.return_value.password = "PRIVATE_MT5_PASSWORD"
        clock = [NOW / 1000]
        mt5.symbol_info_tick.side_effect = lambda symbol: SimpleNamespace(
            time_msc=int(clock[0] * 1000) - 1000, time=int(clock[0]) - 1,
            bid=2350, ask=2350.2,
        )
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"MetaTrader5": mt5, "requests": None}), mock.patch.dict(bridge.os.environ, {"AURUM_BRIDGE_TOKEN": ""}), mock.patch.object(bridge, "Api") as api, mock.patch.object(bridge, "Ledger") as ledger, mock.patch.object(bridge.time, "time", side_effect=lambda: clock[0]), mock.patch.object(bridge.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), redirect_stdout(out), redirect_stderr(err):
            result = bridge.main(["--diagnose", "--symbol", "XAUUSD", "--terminal-path", r"C:\MetaTrader\terminal64.exe"])
        self.assertEqual(result, 0, err.getvalue())
        mt5.initialize.assert_called_once_with(r"C:\MetaTrader\terminal64.exe")
        mt5.shutdown.assert_called_once()
        mt5.account_info.assert_not_called()
        mt5.order_send.assert_not_called()
        api.assert_not_called()
        ledger.assert_not_called()
        printed = out.getvalue() + err.getvalue()
        for private_value in ("PRIVATE_TERMINAL_PATH", "PRIVATE_MT5_PASSWORD", "123456", "PUPrime-Demo"):
            self.assertNotIn(private_value, printed)

    def test_read_only_mode_cleans_up_terminal_after_diagnostic_error(self):
        mt5 = self.terminal()
        mt5.initialize.return_value = True
        mt5.terminal_info.return_value = None
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"MetaTrader5": mt5, "requests": None}), mock.patch.dict(bridge.os.environ, {"AURUM_BRIDGE_TOKEN": ""}), mock.patch.object(bridge, "Api") as api, mock.patch.object(bridge, "Ledger") as ledger, redirect_stdout(out), redirect_stderr(err):
            result = bridge.main(["--diagnose"])
        self.assertNotEqual(result, 0)
        mt5.shutdown.assert_called_once()
        mt5.order_send.assert_not_called()
        api.assert_not_called()
        ledger.assert_not_called()


class BrokerOffsetTests(unittest.TestCase):
    terminal = TerminalSnapshotTests.terminal

    def offset_terminal(self, hours=3, age_ms=1000):
        stamp = NOW + hours * 3600 * 1000 - age_ms
        tick = SimpleNamespace(time_msc=stamp, time=stamp // 1000, bid=2350, ask=2350.2)
        mt5 = self.terminal(tick)
        for bar in mt5.copy_rates_from_pos.return_value:
            bar["time"] += hours * 3600
        return mt5

    def test_explicit_three_hour_offset_normalizes_fresh_quote_and_preserves_raw_metadata(self):
        mt5 = self.offset_terminal()
        with mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
            result = bridge.snapshot_from_terminal(mt5, "XAUUSD", broker_utc_offset_hours=3)
        self.assertEqual(result["timestamp"], NOW - 1000)
        self.assertEqual(result["brokerTimestamp"], NOW + 3 * 3600 * 1000 - 1000)
        self.assertEqual(result["brokerUtcOffsetMinutes"], 180)
        self.assertEqual(result["candles"][0]["time"], snapshot()["candles"][0]["time"])
        self.assertEqual(result["candles"][-1]["time"], snapshot()["candles"][-1]["time"])
        self.assertEqual(result["bid"], 2350)

    def test_explicit_offset_does_not_accept_truly_stale_or_future_quotes(self):
        for age_ms in (45000, -6000):
            with self.subTest(age_ms=age_ms), mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                with self.assertRaises(bridge.BridgeError):
                    bridge.snapshot_from_terminal(self.offset_terminal(age_ms=age_ms), "XAUUSD", broker_utc_offset_hours=3)

    def test_wrong_or_changed_dst_offset_fails_closed_without_auto_adjustment(self):
        for actual_hours, configured_hours in ((3, 0), (2, 3), (3, 2)):
            with self.subTest(actual=actual_hours, configured=configured_hours), mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                with self.assertRaises(bridge.BridgeError):
                    bridge.snapshot_from_terminal(self.offset_terminal(hours=actual_hours), "XAUUSD", broker_utc_offset_hours=configured_hours)

    def test_invalid_cli_broker_offsets_are_rejected_before_terminal_access(self):
        for value in ("-13", "15", "3.5", "auto", "NaN"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                bridge.main(["--diagnose", "--broker-utc-offset-hours", value])
            self.assertEqual(caught.exception.code, 2)

    def test_offset_helper_is_explicit_bounded_and_does_not_modify_source_value(self):
        raw = NOW + 3 * 3600 * 1000
        self.assertEqual(bridge.normalize_broker_timestamp(raw), raw)
        self.assertEqual(bridge.normalize_broker_timestamp(raw, 3), NOW)
        self.assertEqual(bridge.normalize_broker_timestamp(NOW - 3 * 3600 * 1000, -3), NOW)
        for invalid in (-13, 15, 3.5, "3", True):
            with self.subTest(invalid=invalid), self.assertRaises(bridge.BridgeError):
                bridge.normalize_broker_timestamp(raw, invalid)

    def test_order_execution_passes_explicit_offset_to_both_strict_quote_reads(self):
        mt5 = self.offset_terminal()
        mt5.TRADE_RETCODE_DONE = 10009
        mt5.TRADE_RETCODE_DONE_PARTIAL = 10010
        mt5.order_send.return_value = SimpleNamespace(retcode=10009, order=987, deal=0, price=2350.2, volume=0.1)
        cmd = command()
        cmd["accountId"] = "123456@PUPrime-Demo"
        actual_reader = bridge.snapshot_from_terminal
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            try:
                with mock.patch.object(bridge, "snapshot_from_terminal", wraps=actual_reader) as reads, mock.patch.object(bridge, "prepare_request", return_value={"symbol": "XAUUSD"}), mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                    result = bridge.process_command(mt5, ledger, cmd, "XAUUSD", False, broker_utc_offset_hours=3)
                self.assertEqual(result["status"], "filled")
                self.assertEqual(reads.call_count, 2)
                for call in reads.call_args_list:
                    bound = inspect.signature(actual_reader).bind(*call.args, **call.kwargs)
                    bound.apply_defaults()
                    self.assertEqual(bound.arguments["broker_utc_offset_hours"], 3)
                    self.assertIs(bound.arguments["require_fresh"], True)
                mt5.order_send.assert_called_once()
            finally:
                ledger.close()

    def test_offset_does_not_shift_utc_command_expiration(self):
        mt5 = self.offset_terminal()
        cmd = command()
        cmd.update(accountId="123456@PUPrime-Demo", expiresAt=NOW - 1)
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            try:
                with mock.patch.object(bridge, "prepare_request") as prepare, mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                    result = bridge.process_command(mt5, ledger, cmd, "XAUUSD", False, broker_utc_offset_hours=3)
                self.assertEqual(result["status"], "rejected")
                self.assertIn("istekao", result["message"])
                prepare.assert_not_called()
                mt5.order_send.assert_not_called()
                self.assertEqual(cmd["expiresAt"], NOW - 1)
            finally:
                ledger.close()

    def test_postflight_quote_still_rejects_true_staleness_with_configured_offset(self):
        mt5 = self.offset_terminal()
        raw_fresh = NOW + 3 * 3600 * 1000 - 1000
        raw_old = NOW + 3 * 3600 * 1000 - 45000
        mt5.symbol_info_tick.side_effect = [
            SimpleNamespace(time_msc=stamp, time=stamp // 1000, bid=2350, ask=2350.2)
            for stamp in (raw_fresh, raw_old)
        ]
        cmd = command()
        cmd["accountId"] = "123456@PUPrime-Demo"
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            try:
                with mock.patch.object(bridge, "prepare_request", return_value={"symbol": "XAUUSD"}) as prepare, mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                    result = bridge.process_command(mt5, ledger, cmd, "XAUUSD", False, broker_utc_offset_hours=3)
                self.assertEqual(result["status"], "rejected")
                self.assertIn("30 sekundi", result["message"])
                prepare.assert_called_once()
                self.assertEqual(mt5.symbol_info_tick.call_count, 2)
                mt5.order_send.assert_not_called()
            finally:
                ledger.close()

    def test_daily_pnl_near_utc_midnight_is_conservative_for_either_deal_clock(self):
        utc_now = datetime(2026, 10, 22, 0, 15, tzinfo=timezone.utc)
        utc_midnight = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return utc_now.astimezone(tz) if tz else utc_now.replace(tzinfo=None)

        def deal(at, hours, *, profit=0, commission=0, swap=0, fee=0, kind=0):
            raw_ms = int(at.timestamp() * 1000) + hours * 3600 * 1000
            return SimpleNamespace(type=kind, time_msc=raw_ms, time=raw_ms // 1000,
                                   profit=profit, commission=commission, swap=swap, fee=fee)

        for hours in (3, -3, 0):
            mt5 = self.terminal()
            raw_ms = int(utc_now.timestamp() * 1000) + hours * 3600 * 1000
            mt5.symbol_info_tick.return_value = SimpleNamespace(time_msc=raw_ms, time=raw_ms // 1000, bid=2350, ask=2350.2)
            mt5.history_deals_get.return_value = [
                deal(utc_midnight - timedelta(seconds=1), hours, profit=999, commission=-1),
                deal(utc_midnight, hours, profit=40, commission=-2, swap=-1, fee=-0.5),
                deal(utc_now, hours, profit=-10, commission=-1, fee=-0.25, kind=1),
                deal(utc_now + timedelta(seconds=1), hours, profit=999),
                deal(utc_midnight, hours, profit=10000, kind=2),
            ]
            with self.subTest(hours=hours), mock.patch.object(bridge, "datetime", FrozenDateTime), mock.patch.object(bridge.time, "time", return_value=utc_now.timestamp()):
                result = bridge.snapshot_from_terminal(mt5, "XAUUSD", broker_utc_offset_hours=hours)
            # With nonzero offset, positive credit appears in only one
            # interpretation, while the loss must count in either one.
            self.assertAlmostEqual(result["dailyPnl"], -11.25 if hours else 25.25)
            self.assertAlmostEqual(result["dailyPnlUpperBound"], 36.5 if hours else 25.25)
            self.assertIs(result["dailyPnlConservative"], hours != 0)
            start, end = mt5.history_deals_get.call_args.args
            self.assertLessEqual(start, utc_midnight - timedelta(hours=abs(hours)))
            self.assertGreaterEqual(end, utc_now + timedelta(hours=abs(hours)))

    def test_uncertain_deal_clock_counts_either_loss_and_only_certain_positive_net(self):
        utc_now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        utc_midnight = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return utc_now.astimezone(tz) if tz else utc_now.replace(tzinfo=None)

        def raw_deal(at, *, profit=0, commission=0, swap=0, fee=0, kind=0):
            raw_ms = int(at.timestamp() * 1000)
            return SimpleNamespace(type=kind, time_msc=raw_ms, time=raw_ms // 1000,
                                   profit=profit, commission=commission, swap=swap, fee=fee)

        for hours in (3, -3):
            raw_only = utc_midnight + timedelta(hours=1) if hours > 0 else utc_now - timedelta(hours=1)
            converted_only = utc_now + timedelta(hours=1) if hours > 0 else utc_midnight - timedelta(hours=1)
            both = utc_midnight + timedelta(hours=6)
            neither = utc_midnight - timedelta(hours=10)
            deals = [
                raw_deal(raw_only, profit=-10, commission=-1, swap=-2, fee=-0.5),
                raw_deal(converted_only, profit=-20, commission=-2, swap=-1, fee=-0.25),
                raw_deal(raw_only, profit=100, commission=-1),
                raw_deal(converted_only, profit=200, commission=-2),
                raw_deal(both, profit=50, commission=-2, swap=-1, fee=-0.5),
                # Net, including fees, determines the conservative sign.
                raw_deal(raw_only, profit=10, commission=-12),
                raw_deal(neither, profit=-100),
                raw_deal(both, profit=-10000, kind=2),
            ]
            mt5 = self.terminal()
            mt5.history_deals_get.return_value = deals
            raw_tick = int(utc_now.timestamp() * 1000) + hours * 3600 * 1000
            mt5.symbol_info_tick.return_value = SimpleNamespace(time_msc=raw_tick, time=raw_tick // 1000, bid=2350, ask=2350.2)
            with self.subTest(hours=hours), mock.patch.object(bridge, "datetime", FrozenDateTime), mock.patch.object(bridge.time, "time", return_value=utc_now.timestamp()):
                result = bridge.snapshot_from_terminal(mt5, "XAUUSD", broker_utc_offset_hours=hours)
            self.assertAlmostEqual(result["dailyPnl"], 7.75)
            self.assertAlmostEqual(result["dailyPnlUpperBound"], 343.5)
            self.assertIs(result["dailyPnlConservative"], True)
            for interpretation in (0, hours):
                plausible = 0.0
                for deal in deals:
                    if deal.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
                        continue
                    at = datetime.fromtimestamp(deal.time_msc / 1000, timezone.utc) - timedelta(hours=interpretation)
                    if utc_midnight <= at <= utc_now:
                        plausible += deal.profit + deal.commission + deal.swap + deal.fee
                self.assertLessEqual(result["dailyPnl"], plausible)
                self.assertGreaterEqual(result["dailyPnlUpperBound"], plausible)


class RiskAccountingCapabilityTests(unittest.TestCase):
    terminal = TerminalSnapshotTests.terminal

    def iteration(self, acknowledgment, lower=-10, upper=20):
        mt5 = self.terminal()
        mt5.initialize.return_value = True
        session = mock.Mock()
        requests_module = SimpleNamespace(Session=mock.Mock(return_value=session))
        api = mock.Mock()
        market = snapshot()
        market.update(accountId="123456@PUPrime-Demo", dailyPnl=lower,
                      dailyPnlUpperBound=upper, dailyPnlConservative=True,
                      brokerTimestamp=NOW + 3 * 3600 * 1000,
                      brokerUtcOffsetMinutes=180)

        def respond(method, endpoint, payload=None):
            if method == "POST" and endpoint == "/snapshot":
                return acknowledgment
            if method == "GET" and endpoint == "/command":
                return {"command": None}
            raise AssertionError("Unexpected API operation")

        api.request.side_effect = respond
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(sys.modules, {"MetaTrader5": mt5, "requests": requests_module}), mock.patch.dict(bridge.os.environ, {"AURUM_BRIDGE_TOKEN": "private-pairing-value"}), mock.patch.object(bridge, "Api", return_value=api), mock.patch.object(bridge, "ledger_location", return_value=Path(directory) / "commands.sqlite3"), mock.patch.object(bridge, "snapshot_from_terminal", return_value=market), mock.patch.object(bridge, "process_command") as process, mock.patch.object(bridge.time, "time", return_value=NOW / 1000), mock.patch.object(bridge.time, "sleep", side_effect=KeyboardInterrupt), redirect_stdout(out), redirect_stderr(err):
            result = bridge.main(["--url", "https://aurum.example/api/bridge", "--broker-utc-offset-hours", "3"])
        self.assertEqual(result, 0, err.getvalue())
        api.request.assert_any_call("POST", "/snapshot", market)
        process.assert_not_called()
        mt5.order_send.assert_not_called()
        mt5.shutdown.assert_called_once()
        session.close.assert_called_once()
        self.assertNotIn("private-pairing-value", out.getvalue() + err.getvalue())
        return api.request.call_args_list

    def test_ambiguous_daily_pnl_never_fetches_commands_from_unsupported_worker(self):
        for response in ({"ok": True}, {"riskAccountingVersion": 1}, {"riskAccountingVersion": "2"}, {"riskAccountingVersion": 3}):
            with self.subTest(response=response):
                calls = self.iteration(response)
                self.assertFalse(any(call.args[:2] == ("GET", "/command") for call in calls))

    def test_capability_version_two_allows_command_poll_after_telemetry(self):
        calls = self.iteration({"ok": True, "riskAccountingVersion": 2})
        self.assertEqual([call.args[:2] for call in calls], [("POST", "/snapshot"), ("GET", "/command")])

    def test_equal_daily_pnl_bounds_remain_compatible_with_legacy_worker(self):
        calls = self.iteration({"ok": True}, lower=-10, upper=-10)
        self.assertEqual([call.args[:2] for call in calls], [("POST", "/snapshot"), ("GET", "/command")])


class CommandSafetyTests(unittest.TestCase):
    def validate(self, cmd=None, market=None, **kwargs):
        return bridge.validate_command(
            command() if cmd is None else cmd,
            snapshot() if market is None else market,
            NOW,
            **kwargs,
        )

    def assert_rejected(self, *, command_changes=None, snapshot_changes=None, **kwargs):
        cmd, market = command(), snapshot()
        cmd.update(command_changes or {})
        market.update(snapshot_changes or {})
        with self.assertRaises(bridge.BridgeError):
            self.validate(cmd, market, **kwargs)

    def test_valid_demo_buy_is_preserved(self):
        result = self.validate()
        self.assertEqual(result["symbol"], "XAUUSD")
        self.assertEqual(result["volume"], 0.1)
        self.assertEqual(result["side"], "BUY")

    def test_valid_sell_uses_bid_and_opposite_stops(self):
        cmd = command()
        cmd.update(side="SELL", referencePrice=2350, stopLoss=2355, takeProfit=2340)
        self.assertEqual(self.validate(cmd)["side"], "SELL")

    def test_symbol_must_match_selected_gold_symbol(self):
        self.assert_rejected(command_changes={"symbol": "EURUSD"})
        self.assert_rejected(command_changes={"symbol": "XAUUSD.m"})

    def test_account_id_and_mode_must_match_terminal(self):
        self.assert_rejected(command_changes={"accountId": "999999"})
        self.assert_rejected(command_changes={"accountMode": "real"})
        self.assert_rejected(snapshot_changes={"accountMode": "real"})

    def test_live_orders_require_local_explicit_opt_in(self):
        cmd, market = command(), snapshot()
        cmd["accountMode"] = market["accountMode"] = "real"
        with self.assertRaises(bridge.BridgeError):
            self.validate(cmd, market)
        self.assertEqual(self.validate(cmd, market, allow_live=True)["accountMode"], "real")

    def test_expired_and_stale_data_are_blocked(self):
        self.assert_rejected(command_changes={"expiresAt": NOW - 1})
        self.assert_rejected(snapshot_changes={"timestamp": NOW - 60000})

    def test_nonfinite_numbers_are_blocked(self):
        for field in ("volume", "stopLoss", "takeProfit", "referencePrice", "maxSpreadPoints", "maxDeviationPoints"):
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(field=field, value=value):
                    self.assert_rejected(command_changes={field: value})

    def test_invalid_sides_and_missing_protective_stops_are_blocked(self):
        self.assert_rejected(command_changes={"side": "HOLD"})
        self.assert_rejected(command_changes={"stopLoss": 0})
        self.assert_rejected(command_changes={"takeProfit": 0})
        self.assert_rejected(command_changes={"stopLoss": 2351})
        self.assert_rejected(command_changes={"takeProfit": 2349})

    def test_volume_must_obey_broker_minimum_maximum_and_step(self):
        for value in (0, -0.1, 0.005, 0.105, 100.01):
            with self.subTest(value=value):
                self.assert_rejected(command_changes={"volume": value})

    def test_spread_and_price_deviation_are_rechecked(self):
        self.assert_rejected(snapshot_changes={"ask": 2351})
        self.assert_rejected(command_changes={"referencePrice": 2348})

    def test_positions_on_other_symbols_count_toward_limit(self):
        self.assert_rejected(snapshot_changes={"positions": [{"symbol": "EURUSD", "ticket": "42"}]})

    def test_validation_does_not_mutate_inputs(self):
        cmd, market = command(), snapshot()
        original_cmd, original_market = copy.deepcopy(cmd), copy.deepcopy(market)
        self.validate(cmd, market)
        self.assertEqual(cmd, original_cmd)
        self.assertEqual(market, original_market)


class FillingModeTests(unittest.TestCase):
    mt5 = SimpleNamespace(
        SYMBOL_FILLING_FOK=1,
        SYMBOL_FILLING_IOC=2,
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        SYMBOL_TRADE_EXECUTION_MARKET=2,
    )

    def test_ioc_symbol_flag_is_mapped_to_order_enum(self):
        info = SimpleNamespace(filling_mode=2, trade_exemode=2)
        self.assertEqual(bridge.choose_filling_mode(info, self.mt5), self.mt5.ORDER_FILLING_IOC)

    def test_fok_symbol_flag_is_mapped_to_order_enum(self):
        info = SimpleNamespace(filling_mode=1, trade_exemode=2)
        self.assertEqual(bridge.choose_filling_mode(info, self.mt5), self.mt5.ORDER_FILLING_FOK)

    def test_both_flags_choose_a_supported_order_enum(self):
        info = SimpleNamespace(filling_mode=3, trade_exemode=2)
        self.assertIn(bridge.choose_filling_mode(info, self.mt5), (0, 1))

    def test_market_execution_without_supported_fill_mode_fails_closed(self):
        info = SimpleNamespace(filling_mode=0, trade_exemode=2)
        with self.assertRaises(bridge.BridgeError):
            bridge.choose_filling_mode(info, self.mt5)


class LedgerTests(unittest.TestCase):
    def test_command_is_claimed_once_and_result_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commands.sqlite3"
            ledger = bridge.Ledger(path)
            self.assertTrue(ledger.claim("command-1", "123456"))
            self.assertFalse(ledger.claim("command-1", "123456"))
            self.assertEqual(ledger.lookup("command-1")["status"], "pending")
            payload = {"commandId": "command-1", "status": "filled", "ticket": "987"}
            ledger.complete("command-1", "filled", payload)
            ledger.close()
            reopened = bridge.Ledger(path)
            try:
                self.assertFalse(reopened.claim("command-1", "123456"))
                saved = reopened.lookup("command-1")
                self.assertEqual(saved["status"], "filled")
                self.assertEqual(saved["payload"], payload)
                self.assertIsNone(reopened.lookup("unknown"))
            finally:
                reopened.close()

    def test_unresolved_claim_survives_restart_without_resubmission(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commands.sqlite3"
            ledger = bridge.Ledger(path)
            self.assertTrue(ledger.claim("uncertain-command", "123456"))
            ledger.close()
            reopened = bridge.Ledger(path)
            try:
                self.assertFalse(reopened.claim("uncertain-command", "123456"))
                self.assertEqual(reopened.lookup("uncertain-command")["status"], "pending")
            finally:
                reopened.close()


class ExecutionDeduplicationTests(unittest.TestCase):
    def fake_mt5(self, result=None):
        mt5 = mock.Mock()
        mt5.TRADE_RETCODE_DONE = 10009
        mt5.TRADE_RETCODE_DONE_PARTIAL = 10010
        mt5.order_send.return_value = result or SimpleNamespace(
            retcode=10009, order=987, deal=0, price=2350.20
        )
        return mt5

    def patches(self):
        return (
            mock.patch.object(bridge, "snapshot_from_terminal", return_value=snapshot()),
            mock.patch.object(bridge, "prepare_request", return_value={"symbol": "XAUUSD"}),
            mock.patch.object(bridge.time, "time", return_value=NOW / 1000),
        )

    def test_successful_send_is_not_repeated_after_duplicate_or_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            mt5 = self.fake_mt5()
            market_patch, request_patch, clock_patch = self.patches()
            with market_patch, request_patch, clock_patch:
                result = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
                self.assertEqual(result["status"], "filled")
                self.assertEqual(result["ticket"], "987")
                again = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
                self.assertEqual(again, result)
                ledger.close()
                ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
                after_restart = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
                self.assertEqual(after_restart, result)
            ledger.close()
            mt5.order_send.assert_called_once()

    def test_send_exception_is_reconciled_and_never_blindly_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "commands.sqlite3"
            ledger = bridge.Ledger(ledger_path)
            mt5 = self.fake_mt5()
            mt5.order_send.side_effect = RuntimeError("uncertain terminal transport failure")
            uncertain = {"commandId": command()["id"], "status": "rejected", "message": "uncertain; inspect manually"}
            market_patch, request_patch, clock_patch = self.patches()
            with market_patch, request_patch, clock_patch, mock.patch.object(bridge, "reconcile_pending", return_value=uncertain) as reconcile:
                result = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
                self.assertEqual(result, uncertain)
                reconcile.assert_called_once()
                ledger.close()
                ledger = bridge.Ledger(ledger_path)
                repeated = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
                self.assertEqual(repeated, uncertain)
            ledger.close()
            mt5.order_send.assert_called_once()

    def test_validation_rejection_never_reaches_order_send(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            mt5 = self.fake_mt5()
            invalid = command()
            invalid["symbol"] = "EURUSD"
            market_patch, request_patch, clock_patch = self.patches()
            with market_patch, request_patch as prepare, clock_patch:
                result = bridge.process_command(mt5, ledger, invalid, "XAUUSD", False)
                self.assertEqual(result["status"], "rejected")
                prepare.assert_not_called()
                mt5.order_send.assert_not_called()
            ledger.close()

    def test_partial_fill_is_reported_without_resending_remainder(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            mt5 = self.fake_mt5(SimpleNamespace(retcode=10010, order=987, deal=0, price=2350.20, volume=0.04))
            market_patch, request_patch, clock_patch = self.patches()
            with market_patch, request_patch, clock_patch:
                result = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
                self.assertEqual(result["status"], "filled")
                self.assertEqual(result["volume"], 0.04)
                bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
            ledger.close()
            mt5.order_send.assert_called_once()

    def test_other_pending_command_blocks_replacement_id_and_persists_halt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commands.sqlite3"
            ledger = bridge.Ledger(path)
            self.assertTrue(ledger.claim("earlier-uncertain-command", "123456"))
            self.assertTrue(ledger.has_pending())
            mt5 = self.fake_mt5()
            market_patch, request_patch, clock_patch = self.patches()
            with market_patch, request_patch as prepare, clock_patch:
                result = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
                self.assertEqual(result["status"], "rejected")
                self.assertTrue(ledger.halted())
                prepare.assert_not_called()
                ledger.close()
                ledger = bridge.Ledger(path)
                self.assertTrue(ledger.halted())
                newer = command()
                newer["id"] = "yet-another-server-command"
                repeated = bridge.process_command(mt5, ledger, newer, "XAUUSD", False)
                self.assertEqual(repeated["status"], "rejected")
                mt5.order_send.assert_not_called()
            ledger.close()

    def test_market_is_revalidated_after_broker_preflight_before_send(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            mt5 = self.fake_mt5()
            stale = snapshot()
            stale["timestamp"] = NOW - 60000
            with mock.patch.object(bridge, "snapshot_from_terminal", side_effect=[snapshot(), stale]) as market, mock.patch.object(bridge, "prepare_request", return_value={"symbol": "XAUUSD"}) as prepare, mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                result = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(market.call_count, 2)
                prepare.assert_called_once()
                mt5.order_send.assert_not_called()
            ledger.close()

    def test_expiry_during_broker_preflight_blocks_order_send(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            mt5 = self.fake_mt5()
            later = snapshot()
            later["timestamp"] = NOW + 40000
            with mock.patch.object(bridge.time, "time", return_value=NOW / 1000) as clock:
                def slow_preflight(*args):
                    clock.return_value = (NOW + 40000) / 1000
                    return {"symbol": "XAUUSD"}
                with mock.patch.object(bridge, "snapshot_from_terminal", side_effect=[snapshot(), later]), mock.patch.object(bridge, "prepare_request", side_effect=slow_preflight):
                    result = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
                    self.assertEqual(result["status"], "rejected")
                    mt5.order_send.assert_not_called()
            ledger.close()

    def test_pending_claim_after_crash_is_reconciled_without_order_send(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            self.assertTrue(ledger.claim(command()["id"], "123456"))
            mt5 = self.fake_mt5()
            found = {"commandId": command()["id"], "status": "filled", "ticket": "existing-42"}
            with mock.patch.object(bridge, "reconcile_pending", return_value=found) as reconcile:
                result = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
                self.assertEqual(result, found)
                reconcile.assert_called_once()
                mt5.order_send.assert_not_called()
            ledger.close()


class ResultOutboxTests(unittest.TestCase):
    def completed(self, ledger, command_id="filled-command", account_id="123456", status="filled"):
        payload = {"commandId": command_id, "status": status, "ticket": "987", "price": 2350.20}
        self.assertTrue(ledger.claim(command_id, account_id))
        ledger.complete(command_id, status, payload)
        return payload

    def test_completed_result_survives_restart_until_explicit_acknowledgment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commands.sqlite3"
            ledger = bridge.Ledger(path)
            payload = self.completed(ledger)
            self.assertEqual(ledger.unacknowledged("123456"), [payload])
            ledger.close()
            reopened = bridge.Ledger(path)
            try:
                self.assertEqual(reopened.unacknowledged("123456"), [payload])
                reopened.acknowledge(payload["commandId"])
                self.assertEqual(reopened.unacknowledged("123456"), [])
                self.assertEqual(reopened.lookup(payload["commandId"])["payload"], payload)
            finally:
                reopened.close()

    def test_outbox_is_scoped_to_account_and_excludes_pending_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            try:
                first = self.completed(ledger, "first-account-command", "123456")
                second = self.completed(ledger, "second-account-command", "654321", "rejected")
                self.assertTrue(ledger.claim("not-executed-yet", "123456"))
                self.assertEqual(ledger.unacknowledged("123456"), [first])
                self.assertEqual(ledger.unacknowledged("654321"), [second])
                self.assertEqual(ledger.unacknowledged("unknown-account"), [])
            finally:
                ledger.close()

    def test_lost_result_response_retries_ack_after_restart_without_resending_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commands.sqlite3"
            ledger = bridge.Ledger(path)
            mt5 = mock.Mock()
            mt5.TRADE_RETCODE_DONE = 10009
            mt5.TRADE_RETCODE_DONE_PARTIAL = 10010
            mt5.order_send.return_value = SimpleNamespace(retcode=10009, order=987, deal=0, price=2350.20, volume=0.1)
            with mock.patch.object(bridge, "snapshot_from_terminal", return_value=snapshot()), mock.patch.object(bridge, "prepare_request", return_value={"symbol": "XAUUSD"}), mock.patch.object(bridge.time, "time", return_value=NOW / 1000):
                result = bridge.process_command(mt5, ledger, command(), "XAUUSD", False)
            api = mock.Mock()
            api.request.side_effect = [bridge.BridgeError("response lost after server accepted result"), {"ok": True}]
            with self.assertRaises(bridge.BridgeError):
                bridge.flush_acknowledgments(api, ledger, "123456")
            self.assertEqual(ledger.unacknowledged("123456"), [result])
            ledger.close()
            reopened = bridge.Ledger(path)
            try:
                self.assertEqual(bridge.flush_acknowledgments(api, reopened, "123456"), 1)
                self.assertEqual(reopened.unacknowledged("123456"), [])
                self.assertEqual(api.request.call_args_list, [
                    mock.call("POST", "/result", result),
                    mock.call("POST", "/result", result),
                ])
                mt5.order_send.assert_called_once()
            finally:
                reopened.close()

    def test_flush_sends_only_selected_account_and_marks_only_successful_results(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            try:
                selected = self.completed(ledger, "selected", "123456")
                other = self.completed(ledger, "other-account", "654321")
                api = mock.Mock()
                api.request.return_value = {"ok": True}
                self.assertEqual(bridge.flush_acknowledgments(api, ledger, "123456"), 1)
                api.request.assert_called_once_with("POST", "/result", selected)
                self.assertEqual(ledger.unacknowledged("123456"), [])
                self.assertEqual(ledger.unacknowledged("654321"), [other])
            finally:
                ledger.close()

    def test_result_conflict_halts_bridge_and_preserves_unacknowledged_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = bridge.Ledger(Path(directory) / "commands.sqlite3")
            try:
                payload = self.completed(ledger)
                session = mock.Mock()
                session.headers = {}
                session.request.return_value = SimpleNamespace(status_code=409)
                api = bridge.Api(session, "https://aurum.example/api/bridge", "pairing-secret")
                with self.assertRaises(bridge.ApiError) as caught:
                    bridge.flush_acknowledgments(api, ledger, "123456")
                self.assertEqual(caught.exception.status, 409)
                self.assertTrue(ledger.halted())
                self.assertEqual(ledger.unacknowledged("123456"), [payload])
            finally:
                ledger.close()


class HttpApiSafetyTests(unittest.TestCase):
    def session(self, status=200, data=None):
        session = mock.Mock()
        session.headers = {}
        response = mock.Mock()
        response.status_code = status
        response.json.return_value = {} if data is None else data
        session.request.return_value = response
        return session

    def test_remote_connection_requires_https_without_credentials(self):
        for url in (
            "http://aurum.example/api/bridge",
            "ftp://aurum.example/api/bridge",
            "https://username:password@aurum.example/api/bridge",
            "https://aurum.example/api/bridge?token=secret",
            "https://aurum.example/api/bridge#secret",
            "https://aurum.example/not-the-bridge",
        ):
            with self.subTest(url=url), self.assertRaises(bridge.BridgeError):
                bridge.Api(self.session(), url, "pairing-secret")

    def test_requests_preserve_tls_checks_and_do_not_follow_redirects(self):
        session = self.session(data={"command": None})
        api = bridge.Api(session, "https://aurum.example/api/bridge", "pairing-secret")
        self.assertEqual(api.request("GET", "/command"), {"command": None})
        session.request.assert_called_once_with(
            "GET", "https://aurum.example/api/bridge/command", json=None,
            timeout=(5, 15), allow_redirects=False, verify=True,
        )
        self.assertEqual(session.headers["Authorization"], "Bearer pairing-secret")

    def test_redirect_is_rejected_instead_of_releasing_bearer_to_other_host(self):
        session = self.session(status=302)
        api = bridge.Api(session, "https://aurum.example/api/bridge", "pairing-secret")
        with self.assertRaises(bridge.BridgeError):
            api.request("GET", "/command")
        session.request.assert_called_once()
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])

    def test_transport_exception_does_not_expose_token_or_proxy_details(self):
        session = self.session()
        session.request.side_effect = RuntimeError("pairing-secret proxy-user:proxy-password")
        api = bridge.Api(session, "https://aurum.example/api/bridge", "pairing-secret")
        with self.assertRaises(bridge.BridgeError) as caught:
            api.request("GET", "/command")
        self.assertNotIn("pairing-secret", str(caught.exception))
        self.assertNotIn("proxy-password", str(caught.exception))

    def test_localhost_http_is_supported_for_local_worker_development(self):
        api = bridge.Api(self.session(), "http://127.0.0.1:8787/api/bridge", "pairing-secret")
        self.assertTrue(api.base_url.startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
