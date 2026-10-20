"""Offline safety tests; no terminal, credentials, or network access required."""

import copy
import importlib.util
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
