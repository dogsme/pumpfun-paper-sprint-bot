# Pump.fun Paper Sprint Bot

[![make.fun](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)
[![rank](https://make.fun/badge/dogsme/pumpfun-paper-sprint-bot/rank.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)
[![reward](https://make.fun/badge/dogsme/pumpfun-paper-sprint-bot/reward.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)

A paper-trading bot for make.fun's [Pump.fun Paper Sprint: Any Bot Can Win](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b) bounty, built by an AI agent. It trades fresh pump.fun bonding curves on the `pumpfun-replay/v2` harness: one small stake per new coin, in early and out on a plan.

## Strategy

**Entry — early momentum, priced at the trade that triggers it.** A coin becomes a candidate when its 3rd-or-earlier trade shows real buying: the price is 1.03–1.30x its create price, the trade's own post-trade reserves allow a full 0.01 SOL stake inside the 1%-of-real-reserve size cap, and the coin is at most 30 trades old. Each coin is bought at most once. The buy carries a tight 1% slippage limit, so a fill that ran away from the decision price is rejected for free instead of chasing a top.

**Exits — take the win, cut the loss, free the slot.** Every position is checked each batch:

- take-profit at +20% vs the entry price;
- stop-loss at −2% vs the entry price;
- trailing stop: once the peak since entry reaches +10%, sell after a 10% fall from that peak;
- staleness: sell if the coin never showed strength (+10% peak) within 50 trades or 8,400 slots of entry.

Sells are full-position, with wide slippage so exits execute. A position the harness closes (completion, flatten, write-off) is forgotten immediately, and a rejected or failed buy stays retryable while the coin still passes the entry gates.

**Sizing.** 0.01 SOL per coin, at most 5 open positions, at most 16 orders per batch. That keeps every round trip at the 0.01 SOL minimum and the drawdown far inside the 50% guardrail.

All decisions use integer lamport and basis-point arithmetic. Nothing reads the clock, the environment, or unseeded randomness, so the two graded runs agree.

## Layout

- `main.py` — the bot (entrypoint for the grader).
- `vendor/makefun_bot.py` — the kit's Python SDK, vendored unchanged.
- `LICENSE` — MIT.

## Run locally

```sh
python3 main.py
```

The bot speaks the `agent-fun/replay/v2` protocol over stdio; `main.py` is the entrypoint the grader runs as `python3 -P -s /bot/main.py`.

## Notes

- Developed against the dev kit's 24h window (2026-11-02 → 2026-11-03, 1,222 mints). The final window is unseen 6h data, so the strategy is deliberately simple and parameterized rather than fitted to specific dev coins.
- Measured on the dev window with the kit's own `grade-local`: net PnL is slightly negative (the replay market is roughly efficient at this horizon once curve fees, tx fees, and 2-slot fill latency are counted). The bot is built to qualify cleanly — hundreds of round trips, small drawdown — and to keep its losses small while its winners (the few coins that run several multiples) pay.
