"""Demo-only MT5 execution for positions mirrored from the screen observer."""

from __future__ import annotations

import threading

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


COPY_MAGIC = 71212611


class CopyExecution:
    def _connect_demo(self, symbol: str):
        if mt5 is None:
            raise RuntimeError("Falta el paquete MetaTrader5; ejecuta setup.ps1.")
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        if terminal is None or account is None:
            if not mt5.initialize():
                raise RuntimeError(f"MT5 no responde: {mt5.last_error()}")
            terminal = mt5.terminal_info()
            account = mt5.account_info()
        if terminal is None or not terminal.connected or account is None:
            raise RuntimeError("MT5 no está conectado a una cuenta.")
        if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise RuntimeError("Bloqueado: OroCopy solo envía órdenes en cuenta DEMO.")
        if not account.trade_allowed or not terminal.trade_allowed:
            raise RuntimeError("MT5 tiene deshabilitado el trading.")
        hedging = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", None)
        if hedging is not None and account.margin_mode != hedging:
            raise RuntimeError("La cuenta no es Hedge; no se puede espejar el conteo de tickets.")
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"No existe el símbolo {symbol} en MT5.")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"No se pudo activar {symbol} en MT5.")
        return account, info

    def inspect_demo(self, symbol: str) -> str:
        account, _info = self._connect_demo(symbol)
        return f"DEMO · {account.server} · cuenta {account.login} · saldo {account.balance:.2f} {account.currency}"

    def _copy_positions(self, symbol: str):
        positions = mt5.positions_get(symbol=symbol)
        return tuple(p for p in (positions or ()) if int(getattr(p, "magic", 0)) == COPY_MAGIC)

    def _count(self, positions: tuple, side: str) -> int:
        kind = mt5.POSITION_TYPE_BUY if side == "buy" else mt5.POSITION_TYPE_SELL
        return sum(1 for p in positions if p.type == kind)

    def positions_count(self, symbol: str) -> tuple[int, int]:
        """Return BUY/SELL counts belonging only to OroCopy's magic number."""
        positions = self._copy_positions(symbol)
        return self._count(positions, "buy"), self._count(positions, "sell")

    def _send_open(self, symbol: str, side: str, volume: float, index: int) -> None:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError("MT5 no entregó cotización actual.")
        is_buy = side == "buy"
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if is_buy else tick.bid)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "deviation": 30,
            "magic": COPY_MAGIC,
            "comment": f"OroCopy {side.upper()} {index}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        accepted = (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL)
        if result is None or result.retcode not in accepted:
            detail = str(mt5.last_error()) if result is None else f"{result.retcode}: {result.comment}"
            raise RuntimeError(f"Apertura {side.upper()} rechazada: {detail}")

    def _close(self, position) -> None:
        symbol = position.symbol
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError("MT5 no entregó cotización para cerrar.")
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        order_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        price = float(tick.bid if is_buy else tick.ask)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "position": position.ticket,
            "volume": float(position.volume),
            "type": order_type,
            "price": price,
            "deviation": 30,
            "magic": COPY_MAGIC,
            "comment": "OroCopy sync close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        accepted = (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL)
        if result is None or result.retcode not in accepted:
            detail = str(mt5.last_error()) if result is None else f"{result.retcode}: {result.comment}"
            raise RuntimeError(f"Cierre de ticket {position.ticket} rechazado: {detail}")

    def _move_remaining_to_be(self, symbol: str, side: str) -> int:
        positions = self._copy_positions(symbol)
        kind = mt5.POSITION_TYPE_BUY if side == "buy" else mt5.POSITION_TYPE_SELL
        changed = 0
        for position in positions:
            if position.type != kind:
                continue
            entry = float(position.price_open)
            current_sl = float(position.sl or 0.0)
            already_be = ((side == "buy" and current_sl >= entry) or
                          (side == "sell" and current_sl > 0 and current_sl <= entry))
            if already_be:
                continue
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": position.ticket,
                "sl": entry,
                "tp": float(position.tp or 0.0),
                "magic": COPY_MAGIC,
                "comment": "OroCopy breakeven",
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                detail = str(mt5.last_error()) if result is None else f"{result.retcode}: {result.comment}"
                raise RuntimeError(f"BE de ticket {position.ticket} rechazado: {detail}")
            changed += 1
        return changed

    def reconcile(self, symbol: str, volume: float, target: tuple[int, int],
                  cancel_event: threading.Event | None = None) -> str:
        """Match our ticket counts to (buy_count, sell_count), and BE after reductions."""
        def check_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Copia pausada; sincronización interrumpida. Revisa las posiciones en MT5.")

        check_cancelled()
        _account, info = self._connect_demo(symbol)
        buy_target, sell_target = map(int, target)
        if buy_target < 0 or sell_target < 0:
            raise RuntimeError("El conteo de posiciones no puede ser negativo.")
        if buy_target and sell_target:
            raise RuntimeError("El lector detectó compras y ventas simultáneas; revisa el recorte antes de copiar.")
        if not (float(info.volume_min) <= volume <= float(info.volume_max)):
            raise RuntimeError(f"Lote fuera de rango para {symbol}: {info.volume_min:g}–{info.volume_max:g}.")
        if abs(volume / float(info.volume_step) - round(volume / float(info.volume_step))) > 1e-7:
            raise RuntimeError(f"El paso de lote de {symbol} es {info.volume_step:g}.")

        positions = self._copy_positions(symbol)
        initial = {"buy": self._count(positions, "buy"),
                   "sell": self._count(positions, "sell")}
        desired = {"buy": buy_target, "sell": sell_target}
        closed = {"buy": 0, "sell": 0}
        be_moved = {"buy": 0, "sell": 0}
        opened = {"buy": 0, "sell": 0}

        # Close count reductions first, oldest copied tickets first.
        for side in ("buy", "sell"):
            excess = initial[side] - desired[side]
            if excess <= 0:
                continue
            kind = mt5.POSITION_TYPE_BUY if side == "buy" else mt5.POSITION_TYPE_SELL
            tickets = sorted((p for p in positions if p.type == kind),
                             key=lambda p: (getattr(p, "time", 0), p.ticket))
            for position in tickets[:excess]:
                check_cancelled()
                self._close(position)
                closed[side] += 1

        # When the expert cuts exposure, protect the remaining copied tickets at their own entry.
        for side in ("buy", "sell"):
            if closed[side] and desired[side] > 0:
                check_cancelled()
                be_moved[side] = self._move_remaining_to_be(symbol, side)

        positions = self._copy_positions(symbol)
        current = {"buy": self._count(positions, "buy"),
                   "sell": self._count(positions, "sell")}
        for side in ("buy", "sell"):
            difference = desired[side] - current[side]
            if difference <= 0:
                continue
            for index in range(current[side] + 1, desired[side] + 1):
                check_cancelled()
                self._send_open(symbol, side, volume, index)
                opened[side] += 1

        summary = (
            f"Estado copiado · BUY {desired['buy']} / SELL {desired['sell']} · "
            f"abrió {opened['buy'] + opened['sell']} · cerró {closed['buy'] + closed['sell']} · "
            f"BE {be_moved['buy'] + be_moved['sell']}"
        )
        return summary
