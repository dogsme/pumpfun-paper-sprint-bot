"""Pump.fun paper-trading bot for make.fun's Pump.fun Paper Sprint.

Strategy: early-momentum sniper with disciplined exits.
  - Enter a newly created coin once, early, when its price has already
    risen ~10% above its create price (real buyers are arriving) and the
    size cap allows a full stake. One buy per mint, never a re-entry.
  - Exit on take-profit, stop-loss, a trailing stop off the peak since
    entry, or staleness (too many trades / too many slots without an exit).
  - Never hold more than the harness's 5 open positions; every order is
    sized so the round trip clears the 0.01 SOL minimum by 10x.

All decisions use integer arithmetic on lamports and basis points, and
nothing reads the clock, the environment or unseeded randomness, so the
two graded executions agree byte for byte.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS_PER_SOL = makefun_bot.LAMPORTS_PER_SOL

# --- tunable strategy parameters (basis points where noted) -------------
STAKE_LAMPORTS = LAMPORTS_PER_SOL // 100         # 0.01 SOL per coin
ENTRY_MIN_MULT_BPS = 10_300                      # >= 1.03x create price
ENTRY_MAX_MULT_BPS = 13_000                      # <= 1.30x create price (no chasing tops)
ENTRY_MAX_TRADES = 30                            # still early in the coin's life
TAKE_PROFIT_BPS = 12_000                         # sell at +20% vs entry price
STOP_LOSS_BPS = 9_800                            # sell at -2% vs entry price
TRAIL_ARM_BPS = 11_000                           # trailing stop arms at +10% peak
TRAIL_DROP_BPS = 1_000                           # sell after a 10% fall from peak
MAX_HOLD_TRADES = 50                             # staleness: trades since entry
MAX_HOLD_SLOTS = 8_400                           # staleness: slots since entry
BUY_SLIPPAGE_BPS = 100                           # buy fills rejected if the price ran this far past decision
MAX_SLIPPAGE_BPS = 10_000                        # sells: execute; the exits decide, not the fill

# --- selective entry: only coins whose 3rd trade already shows real
# buying (real reserves) and a real run-up (multiple vs create). Most
# coins die; the filter concentrates the 5 position slots on the few
# that are actually moving. Off by default; the sweep toggles it.
SELECT_ENABLED = False
SELECT_MIN_REAL3_LAMPORTS = 10 * LAMPORTS_PER_SOL
SELECT_MIN_MULT3_BPS = 15_000

# --- per-mint bookkeeping (insertion-ordered dicts: deterministic) ------
# mint -> (create_vq, create_vt): virtual reserves at CreateEvent.
_created = {}
# mint -> True once a buy was sent for it (never re-enter).
_decided = {}
# mint -> [entry_cost, entry_tokens, entry_trades, entry_slot, peak_bps]
_entries = {}
# mint -> trade count seen (in-window mints only).
_trade_count = {}
# mint -> True once its 3rd trade passed the selective filter.
_signals = {}


def _price_bps(curve, cost_lamports, token_amount):
    """Current price as basis points of the entry price (cost/tokens).

    price_now / price_entry = (vq/vt) / (cost/tokens), in bps, integer math.
    Returns None when the ratio is undefined.
    """
    if curve.virtual_token_reserves <= 0 or cost_lamports <= 0 or token_amount <= 0:
        return None
    return (curve.virtual_quote_reserves * token_amount * 10_000) // (
        curve.virtual_token_reserves * cost_lamports
    )


def _event_multiple_bps(event, mint):
    """This trade's price as basis points of the create price, or None.

    Uses the trade's own post-trade reserves, not the end-of-batch curve:
    a hot slot can move the price far between its first and last trade,
    and the entry decision must be priced at the trade that triggered it.
    """
    created = _created.get(mint)
    if created is None:
        return None
    create_vq, create_vt = created
    ev_vq = int(event["virtual_quote_reserves"])
    ev_vt = int(event["virtual_token_reserves"])
    if create_vt <= 0 or ev_vt <= 0:
        return None
    return (ev_vq * create_vt * 10_000) // (ev_vt * create_vq)


def _note_fills(state, batch):
    """Fold this batch's fills into the entry bookkeeping."""
    for fill in batch["fills"]:
        mint = fill["mint"]
        if fill["client_id"].startswith("_"):
            # A harness close (complete / flatten / writeoff): forget the entry.
            _entries.pop(mint, None)
            continue
        if fill["side"] == "buy" and fill["status"] == "filled":
            curve = state.curves.get(mint)
            tokens = int(fill["token_amount"])
            cost = int(fill["quote_lamports"]) + int(fill["tx_fee_lamports"])
            _decided[mint] = True  # filled: never re-enter this mint
            if curve is not None and tokens > 0:
                entry = [cost, tokens, curve.trades, state.slot,
                         _price_bps(curve, cost, tokens) or 10_000]
                _entries[mint] = entry
        elif fill["side"] == "buy":
            # Rejected or failed: the mint stays retryable while it still
            # passes the entry checks (price / trade-count gates).
            pass
        elif fill["side"] == "sell" and fill["status"] == "filled":
            _entries.pop(mint, None)


def _exit_orders(state):
    """Sell orders for positions whose exit fired. Updates peak tracking."""
    pending_sells = {p.order["mint"] for p in state.pending.values()
                     if p.order["side"] == "sell"}
    orders = []
    for mint, holding in list(state.positions.items()):
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        if mint in pending_sells:
            continue  # a sell is already in flight for this mint
        entry = _entries.get(mint)
        if entry is None:
            # A position we hold without entry bookkeeping (should not
            # happen): hold it until the harness closes it.
            continue
        cost, tokens, entry_trades, entry_slot, peak_bps = entry
        if holding.token_amount <= 0:
            continue
        now_bps = _price_bps(curve, cost, holding.token_amount)
        if now_bps is None:
            continue
        if now_bps > peak_bps:
            peak_bps = now_bps
            entry[4] = peak_bps
        sell = False
        if now_bps >= TAKE_PROFIT_BPS:
            sell = True  # take profit
        elif now_bps <= STOP_LOSS_BPS:
            sell = True  # stop loss
        elif peak_bps >= TRAIL_ARM_BPS and now_bps * 10_000 <= peak_bps * (10_000 - TRAIL_DROP_BPS):
            sell = True  # trailing stop: gave back 20% from the peak
        elif curve.trades - entry_trades >= MAX_HOLD_TRADES and peak_bps < TRAIL_ARM_BPS:
            sell = True  # stale and never showed strength: free the slot
        elif state.slot - entry_slot >= MAX_HOLD_SLOTS and peak_bps < TRAIL_ARM_BPS:
            sell = True  # stale and never showed strength: free the slot
        if sell:
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": holding.token_amount,
                           "max_slippage_bps": MAX_SLIPPAGE_BPS})
    return orders


def _entry_orders(state, batch, already_queued):
    """Buy orders for fresh coins showing early momentum.

    Each trade is judged on its own post-trade reserves: the multiple
    against the create price and the size cap at that trade. The fill
    still lands two slots later, so BUY_SLIPPAGE_BPS caps how far the
    fill may run past the decision price; a rejected buy stays retryable
    while the coin still passes the gates.
    """
    orders = []
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    free = state.free_cash_lamports()
    slots = caps["max_open_positions"] - state.committed_position_count()
    pending_buys = {p.order["mint"] for p in state.pending.values()
                    if p.order["side"] == "buy"}
    for event in batch["events"]:
        mint = event["mint"]
        if event["event"] == "CreateEvent":
            _created[mint] = (int(event["virtual_quote_reserves"]),
                              int(event["virtual_token_reserves"]))
            continue
        if event["event"] != "TradeEvent":
            continue
        if mint in _created:
            _trade_count[mint] = _trade_count.get(mint, 0) + 1
            if SELECT_ENABLED and _trade_count[mint] == 3 and mint not in _signals:
                mult3 = _event_multiple_bps(event, mint)
                if (mult3 is not None and mult3 >= SELECT_MIN_MULT3_BPS
                        and int(event["real_quote_reserves"]) >= SELECT_MIN_REAL3_LAMPORTS):
                    _signals[mint] = True
        if mint in _decided or mint in _entries or mint in pending_buys:
            continue
        if SELECT_ENABLED and mint not in _signals:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete or curve.created_slot is None:
            continue
        if curve.trades > ENTRY_MAX_TRADES:
            continue
        mult = _event_multiple_bps(event, mint)
        if mult is None or mult < ENTRY_MIN_MULT_BPS or mult > ENTRY_MAX_MULT_BPS:
            continue
        cap = int(event["real_quote_reserves"]) * caps["max_order_bps_of_real_reserve"] // 10_000
        if cap < STAKE_LAMPORTS:
            continue
        if already_queued + len(orders) >= caps["max_orders_per_batch"]:
            continue
        if slots <= 0 or free < STAKE_LAMPORTS + tx:
            continue
        orders.append({"side": "buy", "mint": mint,
                       "quote_lamports": STAKE_LAMPORTS,
                       "max_slippage_bps": BUY_SLIPPAGE_BPS})
        pending_buys.add(mint)
        free -= STAKE_LAMPORTS + tx
        slots -= 1
    return orders


def on_batch(state, batch):
    _note_fills(state, batch)
    orders = _exit_orders(state)
    orders.extend(_entry_orders(state, batch, len(orders)))
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
