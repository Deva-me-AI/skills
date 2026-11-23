# Trade on foom with your own account

This file is for an agent that holds its own `deva_` key and wants to trade spot markets and perps
on foom and earn BIP on every fill, with no person approving your trades. The one thing you need
from a person is SOL and USDC for your trading wallet (section 5). Read it once before you connect; the foom
connector tells you the rest step by step once you are connected.

## 1. What the account is

- Your account is your agent persona, the one `POST /v1/agents/register` created. There is no
  user and no second persona.
- Your `deva_` key is the only credential, for Deva and for foom.
- Your trading wallet is a Solana key you generate and keep. It signs every spot trade and pays every spot
  fee. foom never holds, derives or escrows a key, cannot sign for you, and never pays gas, rent or
  token accounts for you.
- For perps you add a second trading wallet, an Ethereum key you generate and keep the same way
  (section 7). Token cash is the USDC in your Solana wallet; Perps cash is the USDC on Hyperliquid
  under your Ethereum wallet.
- Your BIP goes to the payout wallet you bound in SKILL.md §8, not to a trading wallet.

## 2. Your own account, or a person's agent?

Connect first (section 3) and read what the connector says on connect:

- If it says a person approves your trades and lists no `open_foom_account` or
  `execute_trade_proposal` tool, your key was created by a person in foom settings. You propose
  trades and the person approves them on foomfamily.ai. Nothing below applies to you: propose, then
  stop.
- If it tells you to set up your account, or says the account is yours, continue here.

A person claiming you later changes nothing on foom: the account, the wallet and the BIP stay yours.

## 3. Connect

Add foom's MCP server with the same key you use for Deva. Claude Code:

```bash
DEVA_API_KEY=${DEVA_API_KEY:-$(LC_ALL=C tr -d '\357\273\277[:space:]' < ~/.deva/api_key)}
claude mcp add --transport http --scope user foom https://foomfamily.ai/mcp \
  --header "Authorization: Bearer ${DEVA_API_KEY}"
```

This is the same form as Deva's server in SKILL.md §2. Line 1 takes `DEVA_API_KEY` when it is
set, else `~/.deva/api_key` (where registration saved your key) without spaces, line breaks or a
byte-order mark. Unlike the recipes, it does not check who owns the file or who else can reach it:
run it right after you saved the key as SKILL.md §1 says. Check first that one of the two holds
your key, or the header is empty.

What it exposes: the key is not in the command text or your transcript, but while the command runs
it is in `claude`'s arguments, which other users of this machine can list. Afterwards Claude Code
keeps it in its user config (`~/.claude.json`, readable only by you). Never `--scope project`: that
writes the key into a project `.mcp.json`, a file that is often committed.

To replace an entry (a new key, or a server added earlier at the default local scope, which wins
over user scope), remove it from both scopes, then add it again: `claude mcp remove foom -s local`
and `claude mcp remove foom -s user` ("No MCP server named" only means there was none there). Do
the same for `deva`.

Other harnesses: the server URL is `https://foomfamily.ai/mcp`, transport streamable HTTP, header
`Authorization: Bearer <your deva_ key>`, the key taken in the same order (`DEVA_API_KEY`, else
`~/.deva/api_key`). Keep it only in the harness's own config on this machine, never in a file
that is committed or shared.

## 4. Set up, in this order

1. **Bind your payout wallet** (SKILL.md §8, `python3 scripts/payout_bind.py`) before your first
   trade. BIP earned while no payout wallet is bound is not paid, and is not paid back later.
2. **Register your trading wallet.** Generate a Solana key on your machine
   (`python3 scripts/foom_spot.py keygen` writes `~/.deva/trading.key` and prints the address).
   Then, through the connector: `create_trading_wallet_challenge` answers with a `challenge`, a
   `message_template` and your `persona_id` (the same value as `agent.id` from registration,
   SKILL.md §1); run `python3 scripts/foom_spot.py sign-challenge <persona_id> <challenge>`
   (it fills the template with your persona id, your own address and the challenge, prints the exact message it signed
   and the base58 signature); then `register_trading_wallet` with
   `{chain: "SOLANA", address, signature}`. Without the connector, the same two calls are
   `POST https://api.deva.me/v1/agents/trading-wallets/challenge` (empty JSON body, `{}`) and
   `PUT https://api.deva.me/v1/agents/trading-wallets`, with your `deva_` key. The challenge lives
   five minutes and is single use. Your trading key may be the same key as your payout key
   (`python3 scripts/foom_spot.py --key-file ~/.deva/payout.key ...`), but a separate key is
   safer: the payout key only ever signs claims.
3. **Open the account:** `open_foom_account` (or `POST https://api.deva.me/v1/agents/foom-account`).
   It answers 201 when it opens the account and 200 when it was already open, so it is safe to
   repeat.

Registering a different trading address later replaces the live one and closes the account; open it
again afterwards. Your positions and BIP are not affected. An address can never be registered again,
not even by you (`trading_wallet_address_taken`), so you cannot switch back to an old wallet: move
its funds out first. Anything left there stays under your own key, and only you can move it.

**Two sessions.** Most harnesses load a new skill and new MCP servers only when a session starts,
so the foom tools are usually missing until your next one. Plan for it:

1. In this session, do everything that works over REST or a recipe: register (SKILL.md §1 saves
   your key to `~/.deva/api_key`), bind your payout wallet (`python3 scripts/payout_bind.py`),
   generate and register your trading wallet (step 2 above, without the connector:
   `POST https://api.deva.me/v1/agents/trading-wallets/challenge`, then
   `python3 scripts/foom_spot.py sign-challenge`, then
   `PUT https://api.deva.me/v1/agents/trading-wallets`), open the account
   (`POST https://api.deva.me/v1/agents/foom-account`; for perps, also register the Ethereum
   wallet the same way and open the account again, section 7 step 1), install the skill and add
   both MCP servers (SKILL.md §2 and section 3 here), and write your identifiers to `~/.deva/`
   (SKILL.md, "Keep this skill for future sessions").
2. Then, in one message, ask your person to start a new session and to send SOL and USDC on
   Solana to your trading wallet address (section 5).
3. In the next session, call `get_payout_status` on Deva's server (`earning` should be true) and
   `get_agent_limits` on foom's. If a foom tool is still missing, replace the server (section 3)
   and ask for one more new session. Then check your funds (section 5) and trade (section 6).

If the foom tools already work in this session, skip the restart and carry on.

## 5. Fund your trading wallet

Send both of these to your trading wallet address, on Solana:

- **SOL** for network fees, priority fees and the rent of the token account each new token needs.
  Keep some SOL even when you only hold USDC: a proposal the wallet cannot cover in SOL is refused
  with `insufficient_sol`. A buy refused with `sponsor_refused` while the wallet is low on SOL has
  the same cause: add SOL, then propose again.
- **USDC** for buys. A buy spends at least $3 of USDC, the foom fee included; a sell is at least $2.

Nothing is sponsored: the trade, the foom fee, the network fees and any rent all come out of this
wallet. Funding the wallet is crypto only; the card route in SKILL.md funds your Deva key, not the
wallet.

Check that the funds arrived before you propose. Both reads below come from the live wallet:

- `get_positions` lists your SOL as a row in `spot` with `mint: native-sol` (`amount_atomic` in
  lamports, `quantity_decimal` in SOL), next to the other non-USDC tokens the wallet holds. No
  `native-sol` row means 0 SOL. Send native SOL, not wrapped SOL: SOL sells spend native SOL.
- `get_portfolio` shows your USDC as `cash.tokens_usd`.

On these balance reads (`get_portfolio`, `get_positions`, `get_pnl`), `stale: true` means a price
in the answer may be old, or of unknown age. The balances themselves come from the wallet. A
token foom cannot price has a null price; that does not make the answer stale. The profit and
loss totals are null when foom does not know what your tokens cost,
and `cost_basis` then says `incomplete` (always, today, for tokens in your own wallet): that is
no reason to distrust the balances. An older foom answers `stale: true` on every balance read and
sends no `cost_basis`; there, too, the balances are current. `stale` on `get_market` is different:
it means that market's price is old.

An `insufficient_sol` refusal from `propose_trade` may include `balance_lamports`, the SOL balance
foom read (over REST and through the connector). When it is missing, read the `native-sol` row.

Your key: an unclaimed key that was never funded stops working 14 days after registration. Your
first landed trade marks the key as funded and keeps it alive; so does a card payment or a USDC
purchase on Deva. Trade, or fund the key, before day 14.

## 6. Trade

**Which recipe.** Two recipes sit beside this file, one per key:

- `scripts/foom_spot.py`: spot trades, signed with your Solana trading key. This section.
- `scripts/foom_perps.py`: perps, signed with your Ethereum trading key, and money between Token
  cash and Perps cash. Section 7.

Each one refuses a proposal of the other kind and names the recipe to run instead. No `scripts/`
directory beside this file? They are served at
https://api.deva.me/v1/agents/skill/scripts/foom_spot.py and
https://api.deva.me/v1/agents/skill/scripts/foom_perps.py.

Read your limits first with `get_agent_limits`; change them with `set_agent_policy` if you want a
different per-trade cap, daily cap, market list or slippage bound.

Before each proposal, read the market with `get_market` and decide on its `quote_status`:

- `tradable`: foom verified this market recently. Propose.
- `unknown`: foom could not verify it right now; `quote_status_reason` says why, for example
  `jupiter_quote_rate_limited`. Propose anyway: the quote foom takes for your proposal decides.
- `unavailable`: foom cannot open a trade on this market now (no route, or a perp whose openings
  are disabled). Do not propose.

`tradable` is `true` only when `quote_status` is `tradable`, so `tradable: false` alone is not a
refusal. An older foom answers `tradable: false` without `quote_status`: read that as `unknown`.
That rule is for `get_market` only. A row in `get_positions` with `tradable: false` is a token
foom has no market for, and it cannot be sold here.

**The foom fee.** Every spot trade pays foom a fee in USDC, in the same transaction. On a buy it
comes on top of `quote_amount`, so a $3 buy of SOL spends $3.50; on a sell it comes out of the
USDC the sale returns. The fee depends on the token and the size of the trade (on a buy,
`quote_amount` plus the fee; on a sell, the USDC the sale returns):

| Token | Trade size | foom fee |
|---|---|---|
| SOL, JUP, BTC and ETH (Portal) | under $1,000 | $0.50 flat |
| | $1,000 and above | 0.05% |
| Every other token | under $5 | $0.10 flat |
| | $5 to under $47.50 | 2% |
| | $47.50 to under $190 | $0.95 flat |
| | $190 and above | 0.5% |

On a small trade a flat fee is a large share: a $3 round trip on SOL pays $1.00 in fees, a third
of the money moved. Choose the size with that in mind. The answer of `propose_trade`, and
`get_proposal` while the proposal is `pending`, give the fee of that proposal as
`summary.foom_fee_usd` (USDC), before anything is signed. That value is the one charged: when it
differs from the table, it wins. If the trade is not worth its fee, withdraw it with
`cancel_proposal` instead of executing. After the fill, `execution_result.foom_fee_usd` is the fee
paid. An older foom does not send `foom_fee_usd`: use the table.

Then:

1. `propose_trade` with an `idempotency_key` and a `proposal` object. The trade fields go inside
   `proposal`, not beside the key, and `max_slippage_bps` (1 to 500 for spot) is required:

   ```json
   {"idempotency_key": "<a new key for each proposal>", "proposal": {"market_id": "sol-usdc",
     "venue": "spot", "side": "buy", "order_type": "market", "quote_amount": "3",
     "max_slippage_bps": 100}}
   ```

   A buy takes `quote_amount` (USDC to spend); a sell takes `side: "sell"` and `base_amount`
   instead. Amounts are decimal strings. Nobody approves it: the answer is your proposal id,
   status `pending`. `invalid_arguments` means the call does not match this shape; its `message`
   says what to fix (an older foom answers the code alone: compare your call with the example).
   Reuse a key only to resend a request whose answer you never got; a key that was refused can
   keep getting the same refusal, so after a refusal fix the cause and propose with a new key.
   `rate_limited` or `quote_unavailable` means foom or its quote source is busy: wait
   `retry_after_seconds` when the answer gives it, else about a minute, then propose again with a
   new key.
2. Execute it. The simplest way is one run of the recipe beside this file:

   ```bash
   python3 scripts/foom_spot.py execute <proposal id>
   ```

   It reads the market with `get_market`, asks foom to prepare the transaction, checks it against
   your proposal, prints one line saying what it signs, signs it with your trading key, sends it
   back through the connector and waits for `filled` or `failed`. It is safe to re-run: a rerun
   resends the transaction it already signed, and it signs a replacement only after foom reports
   that the earlier bytes cannot land (`preparation_expired`, `invalid_approval`, or
   `preparation_pending` once its wait has passed). To send it yourself, run
   `python3 scripts/foom_spot.py sign <proposal id>`: it prepares and checks the same way, signs,
   and prints the arguments for `execute_trade_proposal` (`{id, proposal: {approval_jwt,
   signed_transaction}}`) on its last line. The recipe signs only a transaction foom prepared for
   your own proposal, after the checks below.
3. Time matters: a prepared transaction is signable for at most 120 seconds and can land only until
   the chain passes `last_valid_block_height`, usually about a minute. If you are too slow the
   connector answers `preparation_expired`; prepare again. `preparation_pending` means the last
   transaction can still land: wait `retry_after_seconds`, then prepare again.
4. foom takes a fresh quote when it prepares and again before it relays. When it cannot get one
   it answers `quote_unavailable` and changes nothing. The recipe prepares the same proposal
   again by itself: it waits `retry_after_seconds` when the answer gives it, else about 20 seconds,
   up to 5 times; after signing it sends the same signed transaction again and signs nothing
   new. Calling the tools yourself, do the same until foom answers `proposal_unavailable` (the
   proposal has too little time left), then propose again. Any other refused prepare, for example
   `fee_mismatch`, `execution_mismatch` or `execution_cost_changed`, means this proposal cannot
   run as it was frozen: propose again with a new key.

To sell all of a token, find its row in `get_positions` (`spot`, matched by `mint`) and propose
on that row's `market_id` with its `quantity_decimal` as `base_amount`. `quantity_decimal` is the
row's `amount_atomic` in whole tokens, the unit `base_amount` takes; never send `amount_atomic`
itself. A row with `tradable: false` cannot be sold here, and a sell under $2 is refused.

SOL is sold like any other token: the `native-sol` row carries `market_id: sol-usdc`. That row
also pays your fees, so it is never a "sell all" amount: sell at most its `quantity_decimal`
minus 0.01, which keeps about 0.01 SOL for fees and token-account rent. If that leaves nothing,
or less than $2 worth, do not sell SOL. To sell SOL you bought, also sell no more than
what the buy added (compare `amount_atomic` before and after).

**What the recipe checks before it signs.** The transaction must be the swap of your proposal
(`get_proposal`'s `summary`): your trading key is its only signer and pays its fee; it holds one
Jupiter route between your own token accounts for the proposal's market (from `get_market`'s
`mint`), spending exactly the proposal's amount (`quote_amount` USDC for a buy, the sell's size in
whole tokens using `get_market`'s `decimals`), with no slippage beyond the minimum written into it;
that minimum is within your `max_slippage_bps` plus 5% of `quote_usd` when `quote_usd` is set and
the market is not `stale`; foom's fee is one USDC transfer from your own USDC account of at most the larger of $0.95
and 2% of the trade; the priority fee is at most 5,000,000 lamports (set
`FOOM_TRADE_MAX_PRIORITY_LAMPORTS` to change it); it opens at most 4 token accounts, all yours;
wrapped SOL moves only between your wallet and your own wrapped SOL account; and the swap's owner,
source and destination, foom's fee source and recipient, and every account of yours that sends or
receives SOL or closes are written in the transaction, not read from a lookup table. A refusal
says `refusing to sign` and which of these failed, and nothing was signed: prepare again, and if
the same refusal repeats, tell your human.

`execute` sends with `broadcast: foom` (foom sends it). The connector's execute tool also accepts
`broadcast: agent` for an agent that sends the transaction with its own RPC; if you do that,
report the exact bytes right away, or foom never sees the trade.

## 7. Perps

Perps trade on Hyperliquid from an Ethereum trading wallet whose key you hold. Everything in this
section runs through `scripts/foom_perps.py`. Read `get_agent_limits` first. `perps_enabled` is
foom's switch for agent perps: while it is false, opening orders and moves into Perps cash are
refused with `agent_perps_disabled`, and closing orders and moves back to Token cash still work.
`builder_fee` says whether your Ethereum wallet has approved foom's fee (step 3).
`builder_fee.status: "unavailable"` means foom could not read it just now (for example
Hyperliquid did not answer, foom's per-minute limit on Hyperliquid reads for agents or your
account's share of it was used up, your limits were already read 6 times in the last minute, or
another money check of your account was running) or this deployment cannot offer the approval;
it says nothing about the approval, and right after step 3 it is common. Do not approve again
because of it: read `get_agent_limits` again in a minute. If it still reads `unavailable`
minutes later, run step 3 again: it answers approved if the approval is already in place,
approves it if not, or says why it cannot.

Two balances: **Token cash** is the USDC in your Solana trading wallet, **Perps cash** is the USDC
on Hyperliquid under your Ethereum trading wallet. Spot spends Token cash, perps use Perps cash,
and you move USDC between them yourself.

1. **Ethereum wallet, once.** `python3 scripts/foom_perps.py keygen` writes
   `~/.deva/trading-evm.key` and prints the address. Register it like the Solana wallet:
   `create_trading_wallet_challenge`, then
   `python3 scripts/foom_perps.py sign-challenge <persona_id> <challenge>`, then
   `register_trading_wallet` with `{chain: "ETHEREUM", address, signature}`. Then call
   `open_foom_account` again, with no arguments: it adds the wallet to your open account. Until
   then the perps tools answer `ethereum_wallet_required`.
2. **Move money in.** `python3 scripts/foom_perps.py transfer to-perps 25` moves $25 of Token cash
   to Perps cash (at least $5; your Solana wallet pays the network fee in SOL). A wallet's first
   move in also pays Hyperliquid's one-time activation fee, about $1.20 through Relay today (the
   quote states the amount): it comes out of the amount, so $5 arrives as about $3.80. Size the
   first move with that in mind: an order is worth at least $10 (step 5). The recipe asks foom
   for a quote, prints every cost the quote lists, checks it, signs with your Solana trading key
   and waits until the money has arrived. When the one-time fee is more than 10% of the amount,
   it prints a warning and goes on.
   It signs only when Relay's order pays your own wallet: the money lands in Perps cash under
   your Ethereum trading wallet, so both keys have to be on this machine.
3. **Approve the fee, once per wallet.** `python3 scripts/foom_perps.py approve-builder-fee`.
   Every perp order carries foom's 0.05% builder fee, and Hyperliquid takes it only from a wallet
   that approved it. Do this after step 2: a wallet that holds nothing on Hyperliquid (never
   funded, or emptied) is refused with `perps_cash_required`. On `agent_hl_read_ceiling` or
   `rate_limited` it waits as step 4 does and sends the same signature again. A proposal refused
   with `builder_fee_not_approved` means this step is missing.
4. **Switch to a unified account, once.** `python3 scripts/foom_perps.py switch-to-unified`.
   On a unified account one Perps cash balance margins the native markets and builder-dex
   markets such as `xyz-nvda-perp` together; on a standard account an order on such a market is
   refused for margin. Run it after steps 2 and 3: a wallet that holds nothing on Hyperliquid
   (never funded, or emptied) is refused with `perps_cash_required`. It asks foom to prepare the switch
   (`prepare_unified_account`), checks that the message sets your own Ethereum trading wallet to
   `unifiedAccount` on Hyperliquid mainnet with the nonce foom states, that nonce within ten
   minutes of your machine's clock, signs it and sends it
   (`execute_unified_account`). On an account that already is unified there is nothing to sign.
   It is safe to re-run. Right after step 3 foom often answers `agent_hl_read_ceiling` (or
   `rate_limited`): the recipe waits the seconds foom names, when that is a minute or less, and
   sends the same request again, up to three times; once it has signed, that is the same
   signature, so waiting never signs a new message. On `preparation_expired` it prepares and
   signs once more. An account opened earlier: run it once, while it holds no perp position and
   no open order; otherwise foom answers `unified_account_not_eligible`, and its `reason` says
   why. Native perps keep working on a standard account.
5. **Propose.** `propose_trade` with `venue: "perp"`:

   ```json
   {"idempotency_key": "<a new key for each proposal>", "proposal": {"market_id": "btc-perp",
     "venue": "perp", "side": "buy", "order_type": "market", "size": "0.001", "leverage": 2,
     "max_slippage_bps": 100}}
   ```

   `size` is in the market's base asset (BTC here), as a decimal string. Only market orders: a
   limit order answers `order_type_not_supported`. An order is worth at least $10. Leverage is
   capped at 3x until you raise `max_leverage` with `set_agent_policy`, and never goes above the
   market's own maximum; `leverage_cap` says which bound refused you. `insufficient_margin` means
   Perps cash does not cover the order. `insufficient_margin` with `reason: "standard_account"`
   means the order is on a builder-dex market and your account is in standard mode: run step 4,
   then propose again with a new idempotency key. Slippage is at most 1,000 bps (10%). The
   recipe checks the price bound against that slippage plus a 5% allowance for movement since
   the proposal.
   A leverage change keeps the margin mode of a position you already hold on that market. To
   close or shrink a position, propose the opposite side for the size to take off.
6. **Execute.** `python3 scripts/foom_perps.py execute <proposal id>` asks foom to prepare the
   order, checks it against your own proposal and the market (asset, side, size, price bound,
   fee), signs it and waits for `filled` or `failed`. It is safe to re-run: nothing is signed
   twice. `partial_leverage_only` means the leverage change landed and the order did not run;
   propose again if you still want it. Before preparing, the recipe reads your positions,
   retrying once on a failure other than foom's read limit; if that retry fails too, it
   prepares anyway, since the plan may not need the positions check at all. If it does, the
   recipe reads positions once more right after the prepare, and that read never waits: it
   falls in the same minute as the prepare, so foom's read limit usually refuses it, and
   a plan with a leverage change stops there unsigned; run it again in a minute. Only when foom
   marks the proposal reduce-only (`summary.reduce_only`) does the recipe skip the read before
   the prepare; if foom still prepares a leverage change for that order, the recipe signs
   nothing: to close or shrink the position, propose it again without a leverage value, which
   keeps the current setting. When foom
   answers `agent_hl_read_ceiling` (its per-minute limit on Hyperliquid reads for agents)
   before a prepare, the recipe waits for the next minute and asks again, up to three times for the whole run; if it
   still stops there, run it again a minute later.
7. **Move money back.** `python3 scripts/foom_perps.py transfer to-cash 25` (at least $2). It
   prints every cost the quote lists, as in step 2. It
   signs only when Relay's order pays your own wallet, so it needs your Solana trading key on
   this machine as well (`~/.deva/trading.key`, or `--solana-key-file <path>` before the command).
   How much: call `get_positions` right before the move and ask for at most its
   `perps_withdrawable_usd`, not `perps_cash_usd`. `perps_cash_usd` is the account value and
   includes the margin your open positions hold, which cannot be moved out
   until the position is reduced or closed; do that only if you or your human want that trade.
   With a position open, what can be moved out moves with the price, so read it again before
   every move instead of reusing an earlier number. Positions use cross margin, so moving money
   out while one is open leaves less behind it and brings liquidation closer: keep a buffer, and
   check the liquidation price in `get_positions` afterwards. A refusal of
   `route_unavailable` with reason `perps_balance_insufficient` means the amount is more than can
   be moved out now: read `get_positions` again and ask for less. Reason
   `perps_balance_insufficient_first_transfer_fee` means Hyperliquid takes its
   one-time activation fee (1 USDC today) on this wallet's first move out, on top of the amount;
   only a wallet first funded some other way than `transfer to-perps` pays it there: one funded
   by `transfer to-perps` paid that fee on its first move in (step 2). Ask for
   at least that fee less than `perps_withdrawable_usd`. When it is below $2 (or below
   $2 plus that fee when the fee applies), nothing can be moved yet. Both reasons can also come
   after the transfer was signed, when foom runs it: nothing moved, and the recipe keeps the
   signed transfer and finishes it before it starts another. Then
   run the same command again (it resends the same amount, which goes through if the balance
   came back); once it says the quote expired, read `get_positions` again and ask for less.

**Waits on a transfer.** In both directions, when foom answers the quote with
`agent_hl_read_ceiling` or `rate_limited`, the recipe waits the seconds foom names, when that is
a minute or less, and asks again for the same quote, up to three times. When foom could not
read the Solana network fee just now (reason `solana_fee_unavailable` or `solana_rpc_unavailable`,
with `quote_unavailable`, or with `funding_failed` from an older foom), it asks once more after
the wait foom names, when that is a minute or less (20 seconds when it names none). Nothing is
signed before a quote arrives. If it still stops there, no transfer was created and there is
nothing to read with `get_agent_transfer`: run the same command again in a minute, or once the
wait foom named is over when that is longer.

One transfer at a time: while one is unfinished, the recipe finishes it before it starts another.
`manual_review` means part of a move ran and foom reviews it by hand. Do not repeat it; when foom
says the review is over, run `python3 scripts/foom_perps.py transfer forget <request id>`.

The recipe signs only these with your Ethereum key: the wallet challenge; the fee approval at
exactly 0.05%; the switch of your own wallet to a unified account on Hyperliquid mainnet; an
order that matches your own proposal, with the leverage change your proposal
asks for; and the two messages that move the amount you asked for to Relay's depository, for
an order of Relay's that pays your own wallets. Anything else is refused before a signature
exists, and foom checks every signed message again on its side.

**Risk.** Leverage multiplies losses as well as gains. Positions use cross margin unless you
changed that on Hyperliquid yourself: all of your Perps cash backs every position, and a
liquidation can take all of it. On a unified account the same Perps cash backs positions on the
native markets and on `xyz` markets together, so a loss on one can liquidate the other. Keep
in Perps cash only what you are ready to lose, and watch `get_positions`, which shows each
position's liquidation price. If foom halts agent accounts, no order goes through foom,
closing ones included, while Hyperliquid keeps marking your positions.
Your key also works on Hyperliquid directly, so you can always close there; orders placed that
way carry no foom fee and earn no BIP.

## 8. Earn and claim

Every landed fill accrues BIP to your payout wallet at the next seal, with no action from you.
Who claims it depends on who bound that wallet, which `bound_via` in
`GET https://api.deva.me/v1/agents/payout` shows:

- `AGENT_KEY`: you bound your own key with `scripts/payout_bind.py`, so you claim it yourself as
  SKILL.md §8b says (`python3 scripts/payout_sweep.py`), whether or not a person has claimed you.
- `OWNER_SESSION`: your owner bound one of their own wallets. The BIP goes there, they collect
  it, there is nothing for you to claim, and `claim_terms` below is not about you.

BIP has 9 decimals. Every amount in the payout read is in base units, and
1 BIP = 1,000,000,000 base units: 1663188649 base units is 1.663188649 BIP. The recipe prints the
BIP value beside each amount.

What a claim of your `cumulative` balance costs is in `claim_terms` on `get_payout_status`,
from the moment your wallet is bound, before your first seal. That is a tool of the Deva MCP
server, not of the foom connector; over REST it is
`GET https://api.deva.me/v1/agents/payout/claimable`. With `claim_terms` set, the relay `fee`
comes out of the BIP being claimed: no karma is charged, and you need
no credits and no owner. A claim below `minimum` is refused before anything is sent
(`claim_below_minimum`); it stays owed and grows with each later seal that credits you. Once
you are bound, `claim_terms` is `null` only while that fee lane is off; a claim then costs the
relay gas price in karma. A claim when everything is already paid answers `nothing_owed`. BIP
and its claims are on the Bitplanet chain,
not on Solana like your trading wallet, so a Solana RPC or explorer will not find them. Confirm a
claim by reading `get_payout_status` again (`claimed_total` reaching the `cumulative_amount` you
claimed against), never by claiming twice.

## 9. Refusals on the Deva routes

- `trading_wallet_required` (409): open the account only after a trading wallet is registered.
- `trading_wallet_address_taken` (409): that address is registered to someone already; use another.
- `trading_wallet_embedded_exists` (409): this agent already holds an embedded trading wallet,
  which only a person's setup creates; retrying does not help, and that person has to untangle it.
- `trading_wallet_conflict` (409): two registrations raced; request a fresh challenge and retry.
- `sanctioned_address` (403): the address is on the US OFAC SDN list; it is never accepted.
- `agent_persona_required` (403): only an agent persona can hold this account.
- `agent_paused` (403): your owner paused you; nothing opens until they resume you.
- `scope_required` (403): your key was narrowed; opening needs the full key.
- `key_expired` (403): the 14-day rule in section 5 caught you; a person must fund or claim you.

foom's own refusals (`agent_accounts_not_enabled`, `agent_accounts_halted`, `rate_limited`,
`insufficient_sol`, `insufficient_funds`, the policy codes) are named by the connector together with
what to do next.

## 10. Not open

Agents get no referral code and cannot be invited through one. A trading wallet cannot be funded
by card.

## 11. Rollout

While agent-owned accounts are rolling out, you can be stopped at two points:

- Before your account is open, while Deva has not turned account opening on:
  `POST https://api.deva.me/v1/agents/foom-account` answers 404, and through the connector
  `open_foom_account` and the trading tools answer 403 `agent_accounts_not_enabled`. You can still
  register a trading wallet and bind a payout wallet.
- After your account opens, while foom admits only some agents: the trading and read tools (and
  foom's REST routes) answer 403 `agent_accounts_not_enabled`.

Both are a stop, not an outage: retrying does not help until the rollout reaches you. This is not
`foom_account_not_open`, which the trading tools answer until your account is open: call
`open_foom_account` (section 4, step 3), then try again.
