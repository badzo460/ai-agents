"""Aurum HTTPS bridge for one locally authenticated Windows MT5 terminal.

No MT5 login or password leaves the terminal. The web pairing token is read only
from an environment variable. News is deliberately absent: this bridge has no
economic-calendar feed. Run --help for the supported arguments.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit


class BridgeError(Exception):
    """A safe, human-readable bridge rejection (never contains credentials)."""


class ApiError(BridgeError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def is_gold_symbol(symbol: object) -> bool:
    return isinstance(symbol, str) and bool(re.fullmatch(r"XAUUSD(?:[._-][A-Za-z0-9]+)?", symbol, re.I))


def number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BridgeError(f"Neispravna vrijednost: {label}.")
    value = float(value)
    if positive and value <= 0:
        raise BridgeError(f"Vrijednost mora biti pozitivna: {label}.")
    return value


def on_step(value: float, step: float) -> bool:
    try:
        quotient = Decimal(str(value)) / Decimal(str(step))
        return abs(quotient - quotient.to_integral_value()) <= Decimal("0.000001")
    except (InvalidOperation, ZeroDivisionError):
        return False


def validate_command(command: dict, snapshot: dict, now_ms: int, allow_live: bool = False, max_positions: int = 1) -> dict:
    """Validate a command again against fresh terminal data, before order_check."""
    if not isinstance(command, dict) or not isinstance(snapshot, dict):
        raise BridgeError("Neispravan nalog ili tržišni podaci.")
    if not isinstance(command.get("id"), str) or not 1 <= len(command["id"]) <= 120:
        raise BridgeError("Nedostaje ID naloga.")
    symbol = command.get("symbol")
    if not is_gold_symbol(symbol) or symbol != snapshot.get("symbol"):
        raise BridgeError("Dopušten je samo odabrani XAUUSD simbol.")
    if str(command.get("accountId", "")) != str(snapshot.get("accountId", "")) or not snapshot.get("accountId"):
        raise BridgeError("Nalog pripada drugom računu.")
    if command.get("accountMode") != snapshot.get("accountMode") or command.get("accountMode") not in ("demo", "real"):
        raise BridgeError("Vrsta računa ne odgovara nalogu.")
    if snapshot["accountMode"] == "real" and not allow_live:
        raise BridgeError("Stvarno trgovanje nije dopušteno: pokrenite most s --allow-live tek nakon provjere demo računa.")
    created = number(command.get("createdAt"), "createdAt", positive=True)
    expiry = number(command.get("expiresAt"), "expiresAt", positive=True)
    if created > now_ms + 5000 or expiry <= now_ms or expiry <= created or expiry - created > 120000:
        raise BridgeError("Nalog je istekao ili ima neispravno vrijeme.")
    stamp = number(snapshot.get("timestamp"), "timestamp", positive=True)
    if stamp > now_ms + 5000 or now_ms - stamp > 30000:
        raise BridgeError("Tržišni podaci su zastarjeli.")
    side = command.get("side")
    if side not in ("BUY", "SELL"):
        raise BridgeError("Dopušten je samo BUY ili SELL tržišni nalog.")
    positions = snapshot.get("positions")
    if not isinstance(positions, list) or len(positions) >= max_positions:
        raise BridgeError("Dosegnut je lokalni limit otvorenih pozicija.")
    point = number(snapshot.get("point"), "point", positive=True)
    tick_size = number(snapshot.get("tickSize"), "tickSize", positive=True)
    bid = number(snapshot.get("bid"), "bid", positive=True)
    ask = number(snapshot.get("ask"), "ask", positive=True)
    max_spread = number(command.get("maxSpreadPoints"), "maxSpreadPoints", positive=True)
    if max_spread > 1000 or ask < bid or (ask - bid) / point > max_spread + 1e-7:
        raise BridgeError("Spread je iznad dopuštenog limita.")
    price = ask if side == "BUY" else bid
    reference = number(command.get("referencePrice"), "referencePrice", positive=True)
    deviation = number(command.get("maxDeviationPoints"), "maxDeviationPoints", positive=True)
    if deviation > 1000 or abs(price - reference) / point > deviation + 1e-7:
        raise BridgeError("Cijena se previše promijenila od izdavanja naloga.")
    volume = number(command.get("volume"), "volume", positive=True)
    minimum = number(snapshot.get("volumeMin"), "volumeMin", positive=True)
    maximum = number(snapshot.get("volumeMax"), "volumeMax", positive=True)
    step = number(snapshot.get("volumeStep"), "volumeStep", positive=True)
    if volume < minimum - 1e-9 or volume > maximum + 1e-9 or not on_step(volume, step):
        raise BridgeError("Volumen nije unutar brokerovih limita ili koraka.")
    sl = number(command.get("stopLoss"), "stopLoss", positive=True)
    tp = number(command.get("takeProfit"), "takeProfit", positive=True)
    if not on_step(sl, tick_size) or not on_step(tp, tick_size):
        raise BridgeError("SL/TP nije poravnat s brokerovim tickom.")
    if (side == "BUY" and not sl < bid < ask < tp) or (side == "SELL" and not tp < bid < ask < sl):
        raise BridgeError("SL/TP nije na ispravnoj strani trenutne cijene.")
    return dict(command)


def choose_filling_mode(info: object, mt5: object) -> int:
    """SYMBOL_FILLING_* are bit flags, ORDER_FILLING_* are different enum values."""
    flags = int(getattr(info, "filling_mode", 0))
    if flags & 1:  # SYMBOL_FILLING_FOK
        return mt5.ORDER_FILLING_FOK
    if flags & 2:  # SYMBOL_FILLING_IOC
        return mt5.ORDER_FILLING_IOC
    # RETURN is forbidden for market execution; do not guess a filling policy.
    raise BridgeError("Broker nije oglasio podržani FOK/IOC način izvršenja.")


class Ledger:
    """Durable claim before order_send; a crash can never cause blind resubmission."""

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS safety (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS acknowledgments (command_id TEXT PRIMARY KEY, acknowledged REAL NOT NULL)")
        self.connection.commit()

    def claim(self, command_id: str, account_id: str) -> bool:
        try:
            self.connection.execute("INSERT INTO commands VALUES (?, ?, 'pending', '{}', ?)", (command_id, str(account_id), time.time()))
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def complete(self, command_id: str, status: str, payload: dict) -> None:
        if status not in ("filled", "rejected"):
            raise BridgeError("Neispravan status dnevnika naloga.")
        self.connection.execute("UPDATE commands SET status=?, payload=? WHERE id=?", (status, json.dumps(payload, allow_nan=False), command_id))
        self.connection.commit()

    def lookup(self, command_id: str) -> dict | None:
        row = self.connection.execute("SELECT account_id,status,payload FROM commands WHERE id=?", (command_id,)).fetchone()
        return {"account_id": row[0], "status": row[1], "payload": json.loads(row[2])} if row else None

    def unacknowledged(self, account_id: str) -> list[dict]:
        rows = self.connection.execute("SELECT c.payload FROM commands c LEFT JOIN acknowledgments a ON a.command_id=c.id WHERE c.account_id=? AND c.status IN ('filled','rejected') AND a.command_id IS NULL ORDER BY c.created LIMIT 100", (str(account_id),)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def acknowledge(self, command_id: str) -> None:
        self.connection.execute("INSERT OR IGNORE INTO acknowledgments VALUES (?, ?)", (command_id, time.time()))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def halt(self) -> None:
        self.connection.execute("INSERT OR REPLACE INTO safety VALUES ('uncertain_execution', '1')")
        self.connection.commit()

    def halted(self) -> bool:
        return self.connection.execute("SELECT 1 FROM safety WHERE key='uncertain_execution'").fetchone() is not None

    def has_pending(self) -> bool:
        return self.connection.execute("SELECT 1 FROM commands WHERE status='pending' LIMIT 1").fetchone() is not None


def account_mode(account: object, mt5: object) -> str:
    if account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO:
        return "demo"
    if account.trade_mode == mt5.ACCOUNT_TRADE_MODE_REAL:
        return "real"
    raise BridgeError("Natjecateljski ili nepoznati račun nije podržan.")


def account_identity(account: object) -> str:
    """MT5 login numbers are unique only within a broker server."""
    login = str(getattr(account, "login", ""))
    server = getattr(account, "server", "")
    if not re.fullmatch(r"[0-9]{1,20}", login) or not isinstance(server, str) or not server or len(server) > 100 or server != server.strip() or "@" in server or any(ord(char) < 32 or ord(char) > 126 for char in server):
        raise BridgeError("MT5 nije vratio valjan broj računa i naziv brokerova servera.")
    identity = login + "@" + server
    if len(identity) > 128:
        raise BridgeError("Identitet MT5 računa je predug.")
    return identity


def tick_timestamp_ms(tick: object) -> tuple[int, str]:
    """Use the actual broker timestamp; never synthesize or offset a quote."""
    milliseconds = getattr(tick, "time_msc", None)
    if milliseconds is not None and milliseconds != 0:
        return int(number(milliseconds, "time_msc", positive=True)), "time_msc"
    seconds = number(getattr(tick, "time", None), "time", positive=True)
    return int(seconds * 1000), "time (fallback)"


def broker_offset_ms(broker_utc_offset_hours: int = 0) -> int:
    if isinstance(broker_utc_offset_hours, bool) or not isinstance(broker_utc_offset_hours, int) or not -12 <= broker_utc_offset_hours <= 14:
        raise BridgeError("Brokerov UTC pomak mora biti cijeli broj sati od -12 do 14.")
    return broker_utc_offset_hours * 3600000


def normalize_broker_timestamp(raw_ms: int, broker_utc_offset_hours: int = 0) -> int:
    """Convert only the explicitly configured broker clock to UTC."""
    normalized = int(number(raw_ms, "broker timestamp", positive=True)) - broker_offset_ms(broker_utc_offset_hours)
    if normalized <= 0:
        raise BridgeError("Brokerova vremenska oznaka nakon UTC konverzije nije valjana.")
    return normalized


def expanded_history_bounds(start: datetime, end: datetime, broker_utc_offset_hours: int = 0) -> tuple[datetime, datetime]:
    """Query both interpretations, then filter records by normalized UTC time."""
    padding = timedelta(milliseconds=abs(broker_offset_ms(broker_utc_offset_hours)))
    return start - padding, end + padding


def validate_tick_time(stamp: int, now_ms: int) -> float:
    """Return signed age, retaining the existing +30 s / -5 s limits."""
    age_seconds = (now_ms - stamp) / 1000
    if stamp > now_ms + 5000:
        raise BridgeError(f"Vrijeme XAUUSD ticka je u budućnosti (PC - tick: {age_seconds:+.3f} s). Provjerite UTC sat računala i izričito odabrani brokerov UTC pomak; trgovanje čeka ispravne podatke.")
    if now_ms - stamp > 30000:
        raise BridgeError(f"Posljednji XAUUSD tick je stariji od 30 sekundi (PC - tick: {age_seconds:+.3f} s). Provjerite MT5 kotacije, UTC sat računala i odabrani brokerov UTC pomak; trgovanje čeka svježe podatke.")
    return age_seconds


def format_utc_ms(stamp: int) -> str:
    try:
        return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        raise BridgeError("Vremenska oznaka ticka nije valjan UTC datum.") from None


def diagnose_terminal(mt5: object, symbol: str, interval: float = 5, broker_utc_offset_hours: int = 0) -> int:
    """Inspect two quote samples without account data, mutation or execution."""
    if not is_gold_symbol(symbol):
        raise BridgeError("Dijagnostika je dopuštena samo za XAUUSD i izričit brokerov sufiks.")
    broker_offset_ms(broker_utc_offset_hours)
    terminal = mt5.terminal_info()
    print("Dijagnostika MT5: samo čitanje; podaci računa i tajne se ne ispisuju.")
    print("Terminal povezan: " + ("DA" if terminal is not None and getattr(terminal, "connected", False) else "NE"))
    if terminal is None:
        raise BridgeError("MT5 terminal nije dostupan. Pokrenite željeni terminal ili navedite --terminal-path.")
    info = mt5.symbol_info(symbol)
    if info is None:
        raise BridgeError("Odabrani XAUUSD simbol nije pronađen. Provjerite njegov točan naziv u MT5 Market Watchu.")
    actual_symbol = getattr(info, "name", symbol)
    if not is_gold_symbol(actual_symbol) or actual_symbol != symbol:
        raise BridgeError("MT5 je vratio drugi simbol; dijagnostika neće pretpostaviti naziv brokerova instrumenta.")
    print("Simbol: " + actual_symbol)
    print(f"Brokerov UTC pomak: {broker_utc_offset_hours:+d} h (izričita postavka)")
    stamps = []
    for sample in range(2):
        if sample:
            time.sleep(interval)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise BridgeError("MT5 nema tick za odabrani simbol. Otvorite ga u Market Watchu i provjerite kotacije.")
        raw_stamp, source = tick_timestamp_ms(tick)
        stamp = normalize_broker_timestamp(raw_stamp, broker_utc_offset_hours)
        now_ms = int(time.time() * 1000)
        stamps.append(stamp)
        print(f"Uzorak {sample + 1}/2")
        print("PC UTC: " + format_utc_ms(now_ms))
        print(f"Sirovi broker timestamp: {raw_stamp}")
        print("Vrijeme brokera prije UTC konverzije: " + format_utc_ms(raw_stamp).removesuffix("Z"))
        print("Tick UTC: " + format_utc_ms(stamp))
        print("Izvor vremena: " + source)
        print(f"Starost ticka (PC - tick): {(now_ms - stamp) / 1000:+.3f} s")
        try:
            validate_tick_time(stamp, now_ms)
            print("Svježina ticka: unutar dopuštenih granica.")
        except BridgeError as error:
            print(str(error))
    progression = "DA" if stamps[1] > stamps[0] else "NE" if stamps[1] == stamps[0] else "NE (vrijeme se vratilo)"
    print("Tick napreduje: " + progression)
    print("Ako broker promijeni UTC pomak ili ljetno vrijeme, provjerite postavku; stari pomak ne zaobilazi provjere svježine.")
    print("Dijagnostika završena. Nisu poslani mrežni zahtjevi ni nalozi.")
    return 0


def snapshot_from_terminal(mt5: object, symbol: str, require_fresh: bool = True, broker_utc_offset_hours: int = 0) -> dict:
    offset_ms = broker_offset_ms(broker_utc_offset_hours)
    account = mt5.account_info()
    terminal = mt5.terminal_info()
    if account is None or terminal is None or not terminal.connected:
        raise BridgeError("MT5 nije povezan. Prijavite se lokalno u terminal.")
    if not is_gold_symbol(symbol):
        raise BridgeError("Dopušten je samo XAUUSD ili njegov izričit brokerov sufiks.")
    info = mt5.symbol_info(symbol)
    if info is None or not mt5.symbol_select(symbol, True):
        raise BridgeError("XAUUSD simbol nije pronađen u MT5 Market Watchu.")
    if getattr(info, "currency_base", "") not in ("", "XAU") or getattr(info, "currency_profit", "") not in ("", "USD"):
        raise BridgeError("Brokerovi podaci ne prepoznaju odabrani simbol kao zlato/USD.")
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 120)
    if rates is None or len(rates) < 40:
        raise BridgeError("Nedostaje najmanje 40 M5 svijeća. Otvorite XAUUSD M5 graf u terminalu.")
    positions = mt5.positions_get()
    if positions is None:
        raise BridgeError("MT5 nije vratio popis otvorenih pozicija.")
    now = datetime.fromtimestamp(time.time(), timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    history_start, history_end = expanded_history_bounds(today, now, broker_utc_offset_hours)
    deals = mt5.history_deals_get(history_start, history_end)
    if deals is None:
        raise BridgeError("MT5 nije vratio današnju povijest potrebnu za limit gubitka.")
    day_start_ms, day_end_ms = int(today.timestamp() * 1000), int(now.timestamp() * 1000)
    daily_pnl = 0.0
    daily_pnl_upper = 0.0
    for deal in deals:
        if deal.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            continue
        raw_deal_ms, _ = tick_timestamp_ms(deal)
        deal_utc_ms = normalize_broker_timestamp(raw_deal_ms, broker_utc_offset_hours)
        raw_in_day = day_start_ms <= raw_deal_ms <= day_end_ms
        converted_in_day = day_start_ms <= deal_utc_ms <= day_end_ms
        net = sum(number(getattr(deal, key, 0), key) for key in ("profit", "commission", "swap", "fee"))
        # Quote clock measurements do not prove the history API's encoding.
        # Until deal-clock provenance is verified, retain a lower bound under
        # both possible interpretations: ambiguous losses count, ambiguous
        # gains do not. At offset zero the interpretations are identical.
        if (net < 0 and (raw_in_day or converted_in_day)) or (net >= 0 and raw_in_day and converted_in_day):
            daily_pnl += net
        # The upper bound also constrains the opening-balance estimate used
        # for the daily risk budget; a lower P&L alone could inflate that base.
        if (net < 0 and raw_in_day and converted_in_day) or (net >= 0 and (raw_in_day or converted_in_day)):
            daily_pnl_upper += net
    # History and initial candle loading can block. Read the broker's latest
    # actual tick after that work so validation measures the current quote.
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise BridgeError("Broker još nema cijenu za XAUUSD.")
    raw_stamp, _ = tick_timestamp_ms(tick)
    stamp = normalize_broker_timestamp(raw_stamp, broker_utc_offset_hours)
    snapshot = {
        "symbol": symbol, "timestamp": stamp,
        "brokerTimestamp": raw_stamp, "brokerUtcOffsetMinutes": broker_utc_offset_hours * 60,
        "bid": float(tick.bid), "ask": float(tick.ask),
        "point": float(info.point), "tickSize": float(info.trade_tick_size),
        # Use the larger adverse/profit tick valuation when available.
        "tickValue": max(float(getattr(info, "trade_tick_value_loss", 0)), float(info.trade_tick_value)),
        "volumeMin": float(info.volume_min), "volumeMax": float(info.volume_max), "volumeStep": float(info.volume_step),
        "balance": float(account.balance), "equity": float(account.equity),
        "accountId": account_identity(account), "accountMode": account_mode(account, mt5),
        "currency": account.currency, "dailyPnl": daily_pnl,
        "dailyPnlConservative": broker_utc_offset_hours != 0,
        "dailyPnlUpperBound": daily_pnl_upper,
        "positions": [{"ticket": str(p.ticket), "symbol": p.symbol, "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL", "volume": float(p.volume), "openPrice": float(p.price_open), "currentPrice": float(p.price_current), "profit": float(p.profit), "stopLoss": float(p.sl), "takeProfit": float(p.tp)} for p in positions],
        "candles": [{"time": int(r["time"]) - offset_ms // 1000, "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["tick_volume"])} for r in rates],
    }
    for key in ("point", "tickSize", "tickValue", "volumeMin", "volumeMax", "volumeStep", "bid", "ask"):
        number(snapshot[key], key, positive=True)
    if require_fresh:
        validate_tick_time(stamp, int(time.time() * 1000))
    return snapshot


def command_comment(command_id: str) -> str:
    return "AU:" + hashlib.sha256(command_id.encode()).hexdigest()[:20]


def reconcile_pending(mt5: object, command: dict, broker_utc_offset_hours: int = 0) -> dict:
    """Inspect rather than repeat an order with an uncertain prior outcome."""
    broker_offset_ms(broker_utc_offset_hours)
    comment = command_comment(command["id"])
    positions = mt5.positions_get()
    if positions is not None:
        for p in positions:
            if getattr(p, "comment", "") == comment and getattr(p, "symbol", "") == command.get("symbol"):
                return {"commandId": command["id"], "status": "filled", "ticket": str(p.ticket), "price": float(p.price_open), "volume": float(p.volume), "message": "Postojeća pozicija potvrđena nakon ponovnog pokretanja mosta."}
    now_ms = int(time.time() * 1000)
    created_ms = number(command.get("createdAt"), "createdAt", positive=True)
    # Automatic reconciliation is deliberately bounded. An older unresolved
    # execution requires manual inspection, never a repeated submission.
    start_ms = max(0, now_ms - 86400000, min(created_ms - 120000, now_ms))
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    end = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    history_start, history_end = expanded_history_bounds(start, end, broker_utc_offset_hours)
    deals = mt5.history_deals_get(history_start, history_end)
    if deals is not None:
        for d in deals:
            if getattr(d, "comment", "") == comment and getattr(d, "symbol", "") == command.get("symbol"):
                try:
                    raw_deal_ms, _ = tick_timestamp_ms(d)
                    deal_utc_ms = normalize_broker_timestamp(raw_deal_ms, broker_utc_offset_hours)
                except BridgeError:
                    continue
                # The unique command comment and symbol identify execution;
                # accept either bounded time interpretation until this
                # terminal's deal-clock encoding is verified independently.
                if start_ms <= raw_deal_ms <= now_ms or start_ms <= deal_utc_ms <= now_ms:
                    return {"commandId": command["id"], "status": "filled", "ticket": str(d.ticket), "price": float(d.price), "volume": float(d.volume), "message": "Izvršenje potvrđeno u povijesti nakon ponovnog pokretanja mosta."}
    return {"commandId": command["id"], "status": "rejected", "message": "Neizvjestan prethodni pokušaj: nalog nije ponovljen. Ručno provjerite MT5 pozicije i povijest."}


def prepare_request(mt5: object, command: dict, snapshot: dict) -> dict:
    terminal = mt5.terminal_info()
    account = mt5.account_info()
    if terminal is None or account is None or not terminal.connected:
        raise BridgeError("Terminal nije povezan.")
    if account_identity(account) != command["accountId"] or account_mode(account, mt5) != command["accountMode"]:
        raise BridgeError("Račun je promijenjen tijekom obrade naloga.")
    if not getattr(terminal, "trade_allowed", False) or getattr(terminal, "tradeapi_disabled", False) or not getattr(account, "trade_allowed", False) or not getattr(account, "trade_expert", False):
        raise BridgeError("U MT5 terminalu ili računu nije dopušteno algoritamsko trgovanje.")
    info = mt5.symbol_info(command["symbol"])
    if info is None:
        raise BridgeError("XAUUSD simbol nije dostupan.")
    mode = getattr(info, "trade_mode", None)
    full = getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)
    allowed_side = getattr(mt5, "SYMBOL_TRADE_MODE_LONGONLY", 1) if command["side"] == "BUY" else getattr(mt5, "SYMBOL_TRADE_MODE_SHORTONLY", 2)
    if mode not in (full, allowed_side):
        raise BridgeError("Broker ne dopušta otvaranje ove strane XAUUSD pozicije.")
    current_positions = mt5.positions_get()
    if current_positions is None or len(current_positions) >= 1:
        raise BridgeError("Lokalna zaštita dopušta najviše jednu otvorenu poziciju na cijelom računu.")
    pending_orders = mt5.orders_get()
    if pending_orders is None or len(pending_orders) > 0:
        raise BridgeError("Na računu postoje nalozi na čekanju ili ih nije moguće potvrditi; nova pozicija nije dopuštena.")
    price = snapshot["ask"] if command["side"] == "BUY" else snapshot["bid"]
    stop_distance = float(getattr(info, "trade_stops_level", 0)) * info.point
    relevant = snapshot["bid"] if command["side"] == "BUY" else snapshot["ask"]
    if min(abs(relevant - command["stopLoss"]), abs(relevant - command["takeProfit"])) + 1e-8 < stop_distance:
        raise BridgeError("SL/TP je preblizu cijeni prema brokerovim pravilima.")
    order_type = mt5.ORDER_TYPE_BUY if command["side"] == "BUY" else mt5.ORDER_TYPE_SELL
    margin = mt5.order_calc_margin(order_type, command["symbol"], command["volume"], price)
    if margin is None or not math.isfinite(margin) or margin < 0 or margin > account.margin_free:
        raise BridgeError("Nema dovoljno slobodne margine ili broker nije izračunao marginu.")
    request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": command["symbol"], "volume": command["volume"], "type": order_type, "price": price, "sl": command["stopLoss"], "tp": command["takeProfit"], "deviation": int(command["maxDeviationPoints"]), "magic": 260020, "comment": command_comment(command["id"]), "type_time": mt5.ORDER_TIME_GTC, "type_filling": choose_filling_mode(info, mt5)}
    check = mt5.order_check(request)
    if check is None or check.retcode != 0:
        raise BridgeError(f"Brokerova order_check provjera je odbila nalog (kod {getattr(check, 'retcode', 'nedostupan')}).")
    # order_check can make a round trip to the broker; verify account identity
    # and command lifetime again immediately before the durable send claim.
    final_account = mt5.account_info()
    if final_account is None or account_identity(final_account) != command["accountId"] or account_mode(final_account, mt5) != command["accountMode"]:
        raise BridgeError("Račun je promijenjen tijekom brokerove provjere naloga.")
    if int(time.time() * 1000) >= command["expiresAt"]:
        raise BridgeError("Nalog je istekao tijekom brokerove provjere.")
    return request


def process_command(mt5: object, ledger: Ledger, command: dict, symbol: str, allow_live: bool, broker_utc_offset_hours: int = 0) -> dict:
    broker_offset_ms(broker_utc_offset_hours)
    command_id = command.get("id")
    if not isinstance(command_id, str) or not 1 <= len(command_id) <= 120:
        raise BridgeError("Poslužitelj je poslao neispravan ID naloga.")
    previous = ledger.lookup(command_id)
    if previous:
        if previous["status"] != "pending":
            return previous["payload"]
        result = reconcile_pending(mt5, command, broker_utc_offset_hours=broker_utc_offset_hours)
        ledger.complete(command_id, result["status"], result)
        if result["status"] == "rejected":
            ledger.halt()
        return result
    snapshot = snapshot_from_terminal(mt5, symbol, require_fresh=True, broker_utc_offset_hours=broker_utc_offset_hours)
    try:
        if ledger.has_pending():
            # A different expired server command may have executed immediately
            # before a crash. Never treat its replacement ID as permission to
            # trade again while the earlier execution remains unresolved.
            ledger.halt()
        if ledger.halted():
            raise BridgeError("Most je trajno pauziran zbog neizvjesnog prethodnog izvršenja. Ručno provjerite MT5 pozicije i povijest prije obnove lokalnog dnevnika.")
        validate_command(command, snapshot, int(time.time() * 1000), allow_live)
        request = prepare_request(mt5, command, snapshot)
        # The broker's margin/order_check call can block. A fresh terminal read
        # rechecks expiry, spread, account, positions and price deviation before
        # committing the send; updating price never widens allowed deviation.
        fresh_snapshot = snapshot_from_terminal(mt5, symbol, require_fresh=True, broker_utc_offset_hours=broker_utc_offset_hours)
        validate_command(command, fresh_snapshot, int(time.time() * 1000), allow_live)
        request["price"] = fresh_snapshot["ask"] if command["side"] == "BUY" else fresh_snapshot["bid"]
    except BridgeError as error:
        result = {"commandId": command_id, "status": "rejected", "message": str(error)}
        if ledger.claim(command_id, str(snapshot["accountId"])):
            ledger.complete(command_id, "rejected", result)
        return result
    if not ledger.claim(command_id, str(snapshot["accountId"])):
        raise BridgeError("Nalog je već rezerviran; izvršenje nije ponovljeno.")
    try:
        sent = mt5.order_send(request)
    except Exception:
        sent = None
    if sent is None:
        result = reconcile_pending(mt5, command, broker_utc_offset_hours=broker_utc_offset_hours)
    elif sent.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
        partial = sent.retcode == mt5.TRADE_RETCODE_DONE_PARTIAL
        result = {"commandId": command_id, "status": "filled", "ticket": str(sent.order or sent.deal), "price": float(sent.price), "volume": float(getattr(sent, "volume", command["volume"])), "message": "Djelomično izvršenje; ostatak nije ponovljen." if partial else "MT5 je potvrdio izvršenje."}
    else:
        uncertain = sent.retcode in (getattr(mt5, "TRADE_RETCODE_TIMEOUT", 10012), getattr(mt5, "TRADE_RETCODE_CONNECTION", 10031), getattr(mt5, "TRADE_RETCODE_ERROR", 10011))
        result = reconcile_pending(mt5, command, broker_utc_offset_hours=broker_utc_offset_hours) if uncertain else {"commandId": command_id, "status": "rejected", "message": f"MT5 nije potvrdio izvršenje (kod {sent.retcode}); nalog nije ponovljen."}
    if result["status"] == "rejected" and result.get("message", "").startswith("Neizvjestan"):
        ledger.halt()
    ledger.complete(command_id, result["status"], result)
    return result


class Api:
    def __init__(self, session: object, base_url: str, token: str):
        parsed = urlsplit(base_url)
        local = parsed.hostname in ("localhost", "127.0.0.1")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname or (parsed.scheme != "https" and not (local and parsed.scheme == "http")):
            raise BridgeError("URL mora biti HTTPS adresa vašeg Aurum /api/bridge endpointa.")
        if not parsed.path.rstrip("/").endswith("/api/bridge"):
            raise BridgeError("URL mosta mora završiti s /api/bridge.")
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.session.headers.update({"Authorization": "Bearer " + token, "Content-Type": "application/json"})

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            response = self.session.request(method, self.base_url + path, json=payload, timeout=(5, 15), allow_redirects=False, verify=True)
        except Exception:
            # Exceptions can embed URLs and proxy details; do not print them.
            raise BridgeError("HTTPS zahtjev nije uspio. Provjerite mrežu, URL i valjanost certifikata.") from None
        if response.status_code in (401, 403):
            raise ApiError("Most nije autoriziran. Zaustavljen je; prije novog uparivanja provjerite postojeće pozicije i stanje neisporučenih potvrda.", response.status_code)
        if not 200 <= response.status_code < 300:
            raise ApiError(f"Poslužitelj je odbio zahtjev (HTTP {response.status_code}).", response.status_code)
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise BridgeError("Poslužitelj nije vratio valjan JSON.") from None
        if not isinstance(data, dict):
            raise BridgeError("Neispravan odgovor poslužitelja.")
        return data


def flush_acknowledgments(api: Api, ledger: Ledger, account_id: str) -> int:
    """A successful local fill must survive a lost HTTP response or restart.

    Result delivery is retried, order execution is never retried. The server
    must acknowledge duplicate results idempotently for the same command ID.
    """
    count = 0
    for result in ledger.unacknowledged(account_id):
        try:
            api.request("POST", "/result", result)
        except ApiError as error:
            if error.status == 409:
                ledger.halt()
            raise
        ledger.acknowledge(result["commandId"])
        count += 1
    if ledger.unacknowledged(account_id):
        raise BridgeError("Postoje dodatne neisporučene potvrde; novi nalozi čekaju sljedeći ciklus dostave.")
    return count


def ledger_location(account_id: str, url: str) -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "AurumMT5"
    identity = hashlib.sha256((url.rstrip("/") + "|" + account_id).encode()).hexdigest()[:24]
    return root / (identity + ".sqlite3")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aurum XAUUSD / PU Prime MT5 HTTPS most (Windows x64, Python 3.12). Lozinka MT5 računa ostaje u lokalnom terminalu.")
    parser.add_argument("--url", help="HTTPS URL iz prozora Poveži MT5, završava /api/bridge (obavezno izvan --diagnose načina)")
    parser.add_argument("--token-env", default="AURUM_BRIDGE_TOKEN", help="Naziv varijable okruženja koja sadrži token; vrijednost nije CLI argument")
    parser.add_argument("--symbol", default="XAUUSD", help="Točan brokerov XAUUSD simbol; dopušten sufiks npr. XAUUSD.m")
    parser.add_argument("--allow-live", action="store_true", help="Izričito dopusti izvršenje na stvarnom računu; web također mora potvrditi LIVE XAUUSD")
    parser.add_argument("--terminal-path", help="Opcionalna putanja postojećeg MT5 terminal64.exe")
    parser.add_argument("--interval", type=float, default=5, help="Razmak provjere u sekundama, 2–60 (zadano 5)")
    parser.add_argument("--diagnose", action="store_true", help="Samo pročitaj dvije MT5 kotacije i UTC vremena; ne treba URL/token, ne šalje mrežne zahtjeve ni naloge")
    parser.add_argument("--broker-utc-offset-hours", type=int, choices=range(-12, 15), default=0, help="Izričiti UTC pomak brokerovih tickova, svijeća i deal vremena (zadano 0 = UTC); npr. 3 za potvrđeni brokerov UTC+3 sat")
    args = parser.parse_args(argv)
    if not is_gold_symbol(args.symbol):
        parser.error("dopušten je samo XAUUSD ili eksplicitan sufiks, npr. XAUUSD.m")
    if not math.isfinite(args.interval) or not 2 <= args.interval <= 60:
        parser.error("--interval mora biti između 2 i 60 sekundi")
    if args.diagnose:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            print("Za dijagnostiku instalirajte MetaTrader5 paket u Windows x64 Python 3.12 okruženju; requests i web token nisu potrebni.", file=sys.stderr)
            return 2
        try:
            initialized = mt5.initialize(args.terminal_path) if args.terminal_path else mt5.initialize()
            if not initialized:
                raise BridgeError("MT5 initialize nije uspio. Pokrenite željeni terminal ili navedite --terminal-path.")
            return diagnose_terminal(mt5, args.symbol, args.interval, broker_utc_offset_hours=args.broker_utc_offset_hours)
        except BridgeError as error:
            print(str(error), file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\nDijagnostika je prekinuta; nalozi nisu slani.")
            return 0
        except Exception:
            print("Dijagnostika nije mogla pročitati MT5 podatke. Provjerite terminal i naziv simbola; nalozi nisu slani.", file=sys.stderr)
            return 2
        finally:
            try:
                mt5.shutdown()
            except Exception:
                print("MT5 dijagnostičku vezu nije bilo moguće uredno zatvoriti.", file=sys.stderr)
    if not args.url:
        parser.error("--url je obavezan za povezivanje; za lokalnu provjeru bez tokena upotrijebite --diagnose")
    token = os.environ.get(args.token_env, "")
    if not token:
        parser.error(f"postavite token u varijablu {args.token_env}; ne šaljite MT5 lozinku web stranici")
    try:
        import requests
        import MetaTrader5 as mt5
    except ImportError:
        print("Instalirajte bridge/requirements.txt u Windows x64 Python 3.12 okruženju. MT5 terminal mora biti instaliran.", file=sys.stderr)
        return 2
    session = requests.Session()
    ledger = None
    try:
        api = Api(session, args.url, token)
        initialized = mt5.initialize(args.terminal_path) if args.terminal_path else mt5.initialize()
        if not initialized:
            raise BridgeError("MT5 initialize nije uspio. Pokrenite terminal i prijavite PU Prime račun lokalno.")
        account = mt5.account_info()
        if account is None:
            raise BridgeError("MT5 račun nije dostupan.")
        initial_account_id = account_identity(account)
        ledger = Ledger(ledger_location(initial_account_id, args.url))
        if ledger.has_pending():
            ledger.halt()
            raise BridgeError("Prethodno izvršenje je ostalo bez potvrđenog ishoda. Most je zaustavljen radi ručne provjere MT5 pozicija i povijesti; ne brišite dnevnik niti ponovo uparujte račun prije provjere.")
        print("Aurum most je pokrenut. Samo XAUUSD; lokalni limit jedna ukupna pozicija. Ctrl+C za prekid.")
        print(f"Brokerov UTC pomak: {args.broker_utc_offset_hours:+d} h. Ako broker promijeni pomak ili ljetno vrijeme, provjerite postavku; zaštita od zastarjelih podataka ostaje uključena.")
        if args.broker_utc_offset_hours:
            print("Dnevni P&L koristi konzervativnu procjenu dok format vremena povijesti dealova nije potvrđen: nejasni gubici se uključuju, nejasni dobici se izostavljaju.")
        print("Stvarni računi: " + ("izričito dopušteni; web potvrda je također obavezna." if args.allow_live else "izvršavanje blokirano; telemetrija je dopuštena."))
        print("Nema izvora vijesti: zadani News filter blokira MT5 naloge dok ne dodate feed ili ga izričito isključite u postavkama.")
        snapshot_sent = False
        last_status_log = 0.0
        quote_time_blocked = False
        last_quote_warning = 0.0
        risk_accounting_blocked = False
        last_risk_warning = 0.0
        while True:
            started = time.monotonic()
            try:
                # Known execution results are deliverable even if the market
                # has since closed or a fresh terminal read is unavailable.
                flush_acknowledgments(api, ledger, initial_account_id)
                # Telemetry includes the raw broker timestamp and its explicit
                # UTC conversion. Order execution uses the same conversion
                # with strict freshness checks and unchanged UTC order expiry.
                snapshot = snapshot_from_terminal(mt5, args.symbol, require_fresh=False, broker_utc_offset_hours=args.broker_utc_offset_hours)
                if snapshot["accountId"] != initial_account_id:
                    raise BridgeError("MT5 račun je promijenjen. Most je potrebno zaustaviti i ponovo upariti.")
                try:
                    validate_tick_time(snapshot["timestamp"], int(time.time() * 1000))
                    if quote_time_blocked:
                        print("Vrijeme XAUUSD kotacija ponovo je unutar dopuštenih granica.")
                    quote_time_blocked = False
                except BridgeError as error:
                    if not quote_time_blocked or time.monotonic() - last_quote_warning >= 60:
                        print(str(error), file=sys.stderr)
                        last_quote_warning = time.monotonic()
                    quote_time_blocked = True
                snapshot_response = api.request("POST", "/snapshot", snapshot)
                if not snapshot_sent or time.monotonic() - last_status_log >= 60:
                    suffix = " Izvršavanje čeka ispravno vrijeme ticka." if quote_time_blocked else " Čekam provjeru agenata ili nalog."
                    print("MT5 podaci poslani; most povezan." + suffix)
                    last_status_log = time.monotonic()
                snapshot_sent = True
                ambiguous_daily_pnl = snapshot.get("dailyPnlUpperBound", snapshot["dailyPnl"]) != snapshot["dailyPnl"]
                if ambiguous_daily_pnl and snapshot_response.get("riskAccountingVersion") != 2:
                    if not risk_accounting_blocked or time.monotonic() - last_risk_warning >= 60:
                        print("Izvršavanje pauzirano: ažurirajte Cloudflare Worker na podršku za dnevni rizik verzije 2. Dnevni P&L ima neizvjesne vremenske granice; telemetrija se nastavlja, nalozi se ne preuzimaju.", file=sys.stderr)
                        last_risk_warning = time.monotonic()
                    risk_accounting_blocked = True
                    command = None
                else:
                    if risk_accounting_blocked:
                        print("Workerova obrada dnevnog rizika je usklađena; provjera naloga se nastavlja.")
                    risk_accounting_blocked = False
                    command = api.request("GET", "/command").get("command")
                if command is not None:
                    if not isinstance(command, dict):
                        raise BridgeError("Neispravan format naloga.")
                    result = process_command(mt5, ledger, command, args.symbol, args.allow_live, broker_utc_offset_hours=args.broker_utc_offset_hours)
                    flush_acknowledgments(api, ledger, initial_account_id)
                    print("Nalog: " + result["status"] + ". " + result.get("message", ""))
            except ApiError as error:
                snapshot_sent = False
                print(str(error), file=sys.stderr)
                if error.status in (401, 403, 409):
                    if error.status == 409:
                        ledger.halt()
                        print("Potvrda izvršenja nije prihvaćena. Provjerite MT5 povijest i poslužiteljski nalog prije ponovnog uparivanja; lokalni dnevnik je sačuvan.", file=sys.stderr)
                    return 2
            except BridgeError as error:
                snapshot_sent = False
                print(str(error), file=sys.stderr)
            except Exception:
                snapshot_sent = False
                print("Neočekivana greška terminala ili podataka; nalog nije automatski ponovljen.", file=sys.stderr)
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except BridgeError as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nMost je zaustavljen. Otvorene pozicije ostaju pod brokerovim SL/TP pravilima.")
        return 0
    finally:
        if ledger:
            ledger.close()
        mt5.shutdown()
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
