"""Sign and execute a FOOM spot trade with your own Solana trading key.

Spot only. Perps, the Ethereum key and money between Token cash and Perps cash
are scripts/foom_perps.py; `execute` here points a perp proposal at it.

Ships beside SKILL.md in the Deva skill (scripts/foom_spot.py). Python 3
standard library only. Your trading key never leaves this machine and is
never read from an argument.

    python3 scripts/foom_spot.py keygen                   # create ~/.deva/trading.key, print the address
    python3 scripts/foom_spot.py sign-challenge <persona id> <challenge>   # wallet-challenge signature
    python3 scripts/foom_spot.py sign <proposal id>       # prepare -> check -> sign, print execute's arguments
    python3 scripts/foom_spot.py execute <proposal id>    # prepare -> check -> sign -> execute -> wait

`--key-file <path>` before the command uses another seed file (for example
your payout key). `execute` talks to the FOOM connector over MCP with your Deva key, keeps a
state file per proposal so a re-run resends the transaction it already signed
(it signs a replacement only after FOOM reports the earlier bytes cannot land).

Before it signs, the recipe checks the prepared transaction against your own
proposal (get_proposal's summary) and the market's mint and decimals
(get_market): your trading key is the only signer and the fee payer; the one
swap is a Jupiter route between your own token accounts for that market, for
exactly the proposal's amount, with no slippage beyond the floor written into
it; that floor is within max_slippage_bps plus 5% of quote_usd when the market
price is live; foom's fee is one USDC transfer from your own USDC account of at
most the larger of $0.95 and 2% of the trade; the priority fee is at most
FOOM_TRADE_MAX_PRIORITY_LAMPORTS (5,000,000 lamports unless set); at most 4
token accounts are opened, each your own; wrapped SOL moves only between your
wallet and its own wrapped SOL account; and the swap's owner, source and
destination, foom's fee source and recipient, and every account of yours that
sends or receives SOL or closes are written in the transaction itself, not read
from a lookup table. Anything else is refused before a signature exists.
"""
import base64
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from fractions import Fraction

MCP = "https://foomfamily.ai/mcp"
TOOL_GET = "get_proposal"
TOOL_PREPARE = "prepare_trade_proposal"
TOOL_EXECUTE = "execute_trade_proposal"
TOOL_MARKET = "get_market"
# What a prepare answer must carry, and the statuses FOOM reports. Pinned by
# a contract test on the server side: a change on FOOM's side changes this
# recipe and bumps the skill version.
PREPARE_FIELDS = ("prepared", "approval_jwt", "expires_at", "last_valid_block_height")
EXECUTE_STATUSES = ("pending", "filled", "failed")
PROPOSAL_STATUSES = (
    "pending",
    "approved",
    "filled",
    "failed",
    "rejected",
    "expired",
    "cancelled",
)
TERMINAL_STATUSES = ("filled", "failed", "rejected", "expired", "cancelled")
KEY_PATH = "~/.deva/trading.key"
STATE_DIR = "~/.deva/foom-trade"
TOKEN_PROGRAM = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # pragma: allowlist secret
)
TOKEN_2022_PROGRAM = (
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"  # pragma: allowlist secret
)
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"  # pragma: allowlist secret
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
JUPITER_PROGRAM = (
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"  # pragma: allowlist secret
)
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # pragma: allowlist secret
WSOL_MINT = "So11111111111111111111111111111111111111112"  # pragma: allowlist secret
ROUTE_V2 = bytes([187, 100, 250, 204, 49, 196, 175, 20])
SHARED_ROUTE_V2 = bytes([209, 152, 83, 147, 124, 254, 216, 233])
USDC_DECIMALS = 6
MAX_FEE_FLOOR_ATOMIC = 950_000
MAX_FEE_BPS = 200
MAX_PRIORITY_LAMPORTS = "5000000"
MAX_COMPUTE_UNITS = 1_400_000
MAX_TOKEN_ACCOUNTS = 4
MAX_PLATFORM_FEE_BPS = 50
MAX_SLIPPAGE_BPS = 500
PRICE_MARGIN_BPS = 500
# The trading-wallet challenge template, filled with the persona id, this
# key's address and the challenge; the recipe never signs a free-form string.
TRADING_WALLET_TEMPLATE = "deva-trading-wallet-v1:{persona_id}:{address}:{challenge}"
KEY_FILE_OVERRIDE = None
POLL_SECONDS = float(os.environ.get("FOOM_TRADE_POLL_SECONDS", "3"))
MAX_WAIT_SECONDS = float(os.environ.get("FOOM_TRADE_MAX_WAIT_SECONDS", "180"))
# When FOOM cannot get a fresh quote it answers quote_unavailable and changes
# nothing, so the same proposal is prepared again. FOOM's own wait is used when
# it sends one; a wait longer than a minute is not sat out, since a proposal
# only lives a few minutes.
QUOTE_WAIT_SECONDS = float(os.environ.get("FOOM_TRADE_QUOTE_WAIT_SECONDS", "20"))
MAX_QUOTE_WAIT_SECONDS = 60
MAX_PREPARES = 5
# Execute checks the quote again before it relays. The signed bytes only live
# about a minute, so the resend waits are short and few.
RESEND_WAIT_SECONDS = float(os.environ.get("FOOM_TRADE_RESEND_WAIT_SECONDS", "10"))
MAX_RESEND_WAIT_SECONDS = 15
MAX_RESENDS = 2

DEVA_KEY_PATH = os.path.expanduser("~/.deva/api_key")


def _key_from_file(path):
    fix = "Run: chmod 700 ~/.deva && chmod 600 ~/.deva/api_key"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return ""
    except OSError as e:
        sys.stderr.write(
            "WARNING: cannot read ~/.deva/api_key (%s). It must be a file of your "
            "own, not a link, holding only the key.\n" % type(e).__name__
        )
        return ""
    try:
        with os.fdopen(fd, encoding="utf-8-sig") as f:
            info = os.fstat(f.fileno())
            words = f.read().split()
        folder = os.stat(os.path.dirname(path))
    except (OSError, UnicodeDecodeError) as e:
        sys.stderr.write(
            "WARNING: cannot read ~/.deva/api_key (%s). Save the key there again, "
            "alone on one line.\n" % type(e).__name__
        )
        return ""
    if os.name == "posix" and (
        info.st_uid != os.getuid()
        or folder.st_uid != os.getuid()
        or (info.st_mode | folder.st_mode) & 0o077
    ):
        sys.stderr.write(
            "WARNING: ~/.deva/api_key is not used: ~/.deva and the file must be "
            "yours and private to you. %s\n" % fix
        )
        return ""
    return words[0] if words else ""


# The dashboard substitutes the placeholder below; when you copied this from
# the skill it is still a placeholder, so the key is read from DEVA_API_KEY,
# else from ~/.deva/api_key.
KEY = "$DEVA_API_KEY"
if KEY.startswith("$") or KEY.startswith("YOUR_"):
    KEY = os.environ.get("DEVA_API_KEY", "").strip() or _key_from_file(DEVA_KEY_PATH)

# --- ed25519 (RFC 8032), stdlib fallback; `cryptography` used when present ---
_p = 2**255 - 19
_q = 2**252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_sq = pow(2, (_p - 1) // 4, _p)


def _rx(y, s):
    x2 = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p) % _p
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p:
        x = x * _sq % _p
    return _p - x if (x & 1) != s else x


_gy = 4 * pow(5, _p - 2, _p) % _p
_G = (_rx(_gy, 0), _gy, 1, _rx(_gy, 0) * _gy % _p)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _comp(P):
    z = pow(P[2], _p - 2, _p)
    x, y = P[0] * z % _p, P[1] * z % _p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _expand(seed):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    return a, h[32:]


def _modq(b):
    return int.from_bytes(hashlib.sha512(b).digest(), "little") % _q


def pubkey(seed):
    try:
        from cryptography.hazmat.primitives import serialization as _s
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return (
            Ed25519PrivateKey.from_private_bytes(seed)
            .public_key()
            .public_bytes(_s.Encoding.Raw, _s.PublicFormat.Raw)
        )
    except Exception:
        return _comp(_mul(_expand(seed)[0], _G))


def sign(seed, msg):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return Ed25519PrivateKey.from_private_bytes(seed).sign(msg)
    except Exception:
        a, prefix = _expand(seed)
        A = _comp(_mul(a, _G))
        r = _modq(prefix + msg)
        Rs = _comp(_mul(r, _G))
        return Rs + ((r + _modq(Rs + A + msg) * a) % _q).to_bytes(32, "little")


_ALPH = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"  # pragma: allowlist secret


def b58(raw):
    """Canonical base58: one '1' per leading zero byte, then the digits."""
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = _ALPH[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


# --- the trading key ----------------------------------------------------------


def key_path():
    path = os.path.expanduser(KEY_FILE_OVERRIDE or KEY_PATH)
    if path.startswith("~"):
        raise SystemExit(
            "ERROR: cannot work out your home directory (HOME is unset and this user has no "
            "passwd entry). Set HOME and re-run, or the trading key would land in a stray "
            "directory."
        )
    return path


def load_seed():
    """The stored 32-byte seed, or None when the file is absent or unusable."""
    try:
        with open(key_path(), "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        seed = bytes.fromhex(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        return None
    return seed if len(seed) == 32 else None


def need_seed():
    seed = load_seed()
    if seed is None:
        raise SystemExit(
            "ERROR: no trading key at %s. Run `python3 scripts/foom_spot.py keygen` "
            "first, then register the printed address as your trading wallet."
            % key_path()
        )
    return seed


def keygen():
    """Create the trading key if it is absent; print its address either way."""
    seed = load_seed()
    if seed is None:
        if os.path.exists(key_path()):
            raise SystemExit(
                "ERROR: %s exists but is not a usable key. Do not overwrite it blindly: "
                "if a wallet was registered from it, its funds would be lost. Tell your human."
                % key_path()
            )
        directory = os.path.dirname(key_path())
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        seed = os.urandom(32)
        fd = os.open(key_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(seed.hex() + "\n")
        print(
            "Created %s (keep it chmod 600, back it up, never print or commit it)."
            % key_path()
        )
    print("Trading wallet address: %s" % b58(pubkey(seed)))
    print(
        "Fund this address with SOL and USDC before you trade; register it as your trading wallet."
    )
    return 0


# --- the message -----------------------------------------------------------------


def _shortvec(raw, i):
    """Decode the compact-u16 at ``raw[i:]``; return (value, next index)."""
    value = 0
    for n, shift in enumerate((0, 7, 14)):
        byte = raw[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if n and not byte:
                raise ValueError("a compact length that is not in its shortest form")
            if n == 2 and byte > 3:
                raise ValueError("a compact length above 65535")
            return value, i
    raise ValueError("compact-u16 runs longer than three bytes")


def parse_message(raw):
    """The header, static keys and top-level instructions of a message.

    Accepts a legacy message and a version 0 message. Every byte must be
    accounted for, and every account an instruction names must exist.
    """
    i = 0
    versioned = bool(raw) and bool(raw[0] & 0x80)
    if versioned:
        if raw[0] & 0x7F:
            raise ValueError("a transaction version this recipe does not know")
        i = 1
    if len(raw) < i + 3:
        raise ValueError("too short to be a message")
    header = tuple(raw[i : i + 3])
    i += 3
    count, i = _shortvec(raw, i)
    if not count or len(raw) < i + count * 32 + 32:
        raise ValueError("account table does not fit inside the message")
    keys = [b58(raw[i + n * 32 : i + (n + 1) * 32]) for n in range(count)]
    i += count * 32 + 32
    total, i = _shortvec(raw, i)
    instructions = []
    for _ in range(total):
        program = raw[i]
        i += 1
        accounts, i = _shortvec(raw, i)
        indexes = list(raw[i : i + accounts])
        i += accounts
        length, i = _shortvec(raw, i)
        data = bytes(raw[i : i + length])
        i += length
        if i > len(raw) or program >= count:
            raise ValueError("an instruction runs past the end of the message")
        instructions.append((program, indexes, data))
    loaded = 0
    if versioned:
        tables, i = _shortvec(raw, i)
        for _ in range(tables):
            i += 32
            writable, i = _shortvec(raw, i)
            i += writable
            readonly, i = _shortvec(raw, i)
            i += readonly
            loaded += writable + readonly
            if i > len(raw):
                raise ValueError(
                    "an address-table lookup runs past the end of the message"
                )
    if i != len(raw):
        raise ValueError("%d bytes follow the message" % (len(raw) - i))
    if any(a >= count + loaded for _, indexes, _ in instructions for a in indexes):
        raise ValueError(
            "an instruction that names an account the message does not hold"
        )
    return header, keys, instructions


def _is_code(text):
    return 0 < len(text) <= 64 and all(c == "_" or "a" <= c <= "z" for c in text)


def _refuse(why):
    raise SystemExit(
        "ERROR: refusing to sign - %s. Nothing was signed. Prepare again; if it repeats, "
        "tell your human." % why
    )


def _b58decode(text):
    n = 0
    for char in text:
        n = n * 58 + _ALPH.index(char)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + body


def is_address(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        return False
    if any(char not in _ALPH for char in value):
        return False
    return len(_b58decode(value)) == 32 and b58(_b58decode(value)) == value


def on_curve(raw):
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    x2 = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p) % _p
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p:
        x = x * _sq % _p
    return (x * x - x2) % _p == 0


_DERIVED = {}


def associated_token_account(owner, mint):
    if (owner, mint) not in _DERIVED:
        seeds = _b58decode(owner) + _b58decode(TOKEN_PROGRAM) + _b58decode(mint)
        program = _b58decode(ATA_PROGRAM)
        for bump in range(255, -1, -1):
            candidate = hashlib.sha256(
                seeds + bytes([bump]) + program + b"ProgramDerivedAddress"
            ).digest()
            if not on_curve(candidate):
                _DERIVED[(owner, mint)] = b58(candidate)
                break
        else:
            _refuse("no token account can be derived for %s" % mint)
    return _DERIVED[(owner, mint)]


def _atomic(text, decimals, what):
    if not isinstance(text, str) or not text or len(text) > 60:
        _refuse("the proposal's %s is not a decimal amount" % what)
    whole, _, fraction = text.partition(".")
    digits = "0123456789"
    if (
        not whole
        or any(c not in digits for c in whole + fraction)
        or text.endswith(".")
    ):
        _refuse("the proposal's %s is not a decimal amount" % what)
    if len(fraction.rstrip("0")) > decimals:
        _refuse("the proposal's %s has more decimals than the token" % what)
    value = int(whole) * 10**decimals + int(
        (fraction + "0" * decimals)[:decimals] or "0"
    )
    if not 0 < value < 2**64:
        _refuse("the proposal's %s is out of range" % what)
    return value


def proposal_terms(summary, market):
    if not isinstance(summary, dict) or summary.get("venue") != "spot":
        _refuse(
            "foom sent no spot summary for this proposal to check the transaction against"
        )
    side, market_id = summary.get("side"), summary.get("market_id")
    if side not in ("buy", "sell") or not isinstance(market_id, str):
        _refuse("the proposal's summary names no side or market")
    if not isinstance(market, dict) or market.get("market_id") != market_id:
        _refuse("the market foom described is not the proposal's market")
    mint, decimals = market.get("mint"), market.get("decimals")
    if mint == WSOL_MINT and decimals != 9:
        _refuse("get_market sent decimals other than 9 for wrapped SOL")
    if not is_address(mint) or mint == USDC_MINT:
        _refuse("get_market sent no usable mint for the proposal's market")
    if (
        isinstance(decimals, bool)
        or not isinstance(decimals, int)
        or not 0 <= decimals <= 18
    ):
        _refuse("get_market sent no usable decimals for the proposal's market")
    bps = summary.get("max_slippage_bps")
    if (
        isinstance(bps, bool)
        or not isinstance(bps, int)
        or not 1 <= bps <= MAX_SLIPPAGE_BPS
    ):
        _refuse(
            "the proposal's max_slippage_bps is not between 1 and %d" % MAX_SLIPPAGE_BPS
        )
    if side == "buy":
        amount = _atomic(summary.get("quote_amount"), USDC_DECIMALS, "quote_amount")
    else:
        size = summary.get("size")
        amount = _atomic(
            size if size is not None else summary.get("base_amount"), decimals, "size"
        )
    quote = market.get("quote_usd")
    live = quote is not None and market.get("stale") is not True
    if live and (
        isinstance(quote, bool)
        or not isinstance(quote, (int, float))
        or not 0 < quote < float("inf")
    ):
        _refuse("get_market sent a quote_usd that is not a price")
    return {
        "side": side,
        "mint": mint,
        "decimals": decimals,
        "amount_in": amount,
        "max_slippage_bps": bps,
        "quote_usd": Fraction(quote) if live else None,
    }


def _route(data, accounts, written, expected):
    head = data[:8]
    if head == ROUTE_V2:
        at, places = 8, (0, 1, 2)
    elif head == SHARED_ROUTE_V2:
        at, places = 9, (1, 2, 5)
    else:
        _refuse("the Jupiter instruction is not a route")
    if len(data) < at + 26:
        _refuse("the Jupiter route is shorter than its terms")
    steps = int.from_bytes(data[at + 22 : at + 26], "little")
    if not 1 <= steps <= 64 or len(data) < at + 26 + steps * 5:
        _refuse("the Jupiter route has no usable steps")
    owner, source, destination = (
        written(accounts, n, what)
        for n, what in zip(places, ("owner", "source", "destination"))
    )
    if owner != expected["owner"]:
        _refuse("the swap's owner is not your trading key")
    if source != expected["source"] or destination != expected["destination"]:
        _refuse(
            "the swap does not run between your own token accounts for the proposal's market"
        )
    if head == ROUTE_V2 and written(accounts, 7, "account 7") not in (
        JUPITER_PROGRAM,
        destination,
    ):
        _refuse("the route pays out to an account other than your own")
    amount = int.from_bytes(data[at : at + 8], "little")
    floor = int.from_bytes(data[at + 8 : at + 16], "little")
    slippage = int.from_bytes(data[at + 16 : at + 18], "little")
    platform = int.from_bytes(data[at + 18 : at + 20], "little")
    positive = int.from_bytes(data[at + 20 : at + 22], "little")
    if amount != expected["amount_in"]:
        _refuse("the swap spends an amount other than the proposal's")
    if slippage or positive:
        _refuse("the swap allows slippage beyond the floor written into it")
    if floor < 1:
        _refuse("the swap has no minimum out")
    if platform > MAX_PLATFORM_FEE_BPS:
        _refuse("the route's platform fee is above %d bps" % MAX_PLATFORM_FEE_BPS)
    return floor


def check_swap(raw, mine, summary, market):
    try:
        header, keys, instructions = parse_message(raw)
    except (IndexError, ValueError) as error:
        _refuse("the prepared message carries %s" % error)
    if header[0] != 1 or header[1] != 0:
        _refuse(
            "the message does not want exactly one signature, yours, from a writable account"
        )
    if keys[0] != mine:
        _refuse(
            "account 0 (the signer and fee payer) is not your trading key %s" % mine
        )
    terms = proposal_terms(summary, market)
    buy = terms["side"] == "buy"
    usdc = associated_token_account(mine, USDC_MINT)
    held = associated_token_account(mine, terms["mint"])
    wrapped = associated_token_account(mine, WSOL_MINT)
    native_sell = not buy and terms["mint"] == WSOL_MINT
    expected = {
        "owner": mine,
        "source": usdc if buy else held,
        "destination": held if buy else usdc,
        "amount_in": terms["amount_in"],
    }

    def written(accounts, n, what):
        if n >= len(accounts):
            _refuse("an instruction is missing its %s" % what)
        if accounts[n] >= len(keys):
            _refuse(
                "the %s is read from a lookup table instead of written in the message"
                % what
            )
        return keys[accounts[n]]

    floor, fees, creates, budget = None, [], 0, {}
    wrapped_in, synced, closes = None, False, 0
    for program_index, accounts, data in instructions:
        program = keys[program_index]
        if program == COMPUTE_BUDGET_PROGRAM:
            op = data[:1]
            if op == b"\x02" and len(data) == 5 and 2 not in budget:
                budget[2] = int.from_bytes(data[1:5], "little")
            elif op == b"\x03" and len(data) == 9 and 3 not in budget:
                budget[3] = int.from_bytes(data[1:9], "little")
            else:
                _refuse("the compute budget is not one unit limit and one unit price")
        elif program == ATA_PROGRAM:
            if floor is not None or data != b"\x01" or len(accounts) != 6:
                _refuse(
                    "a token-account instruction other than opening one before the swap"
                )
            if (
                written(accounts, 0, "payer of a new token account") != mine
                or written(accounts, 2, "owner of a new token account") != mine
            ):
                _refuse("a new token account is not paid for and owned by your wallet")
            if written(accounts, 5, "token program") not in (
                TOKEN_PROGRAM,
                TOKEN_2022_PROGRAM,
            ):
                _refuse("a new token account names an unknown token program")
            written(accounts, 1, "new token account")
            creates += 1
            if creates > MAX_TOKEN_ACCOUNTS:
                _refuse(
                    "the transaction opens more than %d token accounts"
                    % MAX_TOKEN_ACCOUNTS
                )
        elif program == SYSTEM_PROGRAM:
            if not native_sell or floor is not None or wrapped_in is not None:
                _refuse("a SOL transfer outside wrapping the SOL you sell")
            if (
                len(data) != 12
                or data[:4] != (2).to_bytes(4, "little")
                or len(accounts) != 2
            ):
                _refuse("a system instruction other than one SOL transfer")
            if (
                written(accounts, 0, "SOL sender") != mine
                or written(accounts, 1, "SOL recipient") != wrapped
            ):
                _refuse(
                    "SOL moves to an account other than your own wrapped SOL account"
                )
            wrapped_in = int.from_bytes(data[4:12], "little")
        elif program == TOKEN_PROGRAM:
            op = data[:1]
            if op == b"\x11":
                if (
                    wrapped_in is None
                    or synced
                    or floor is not None
                    or len(data) != 1
                    or len(accounts) != 1
                    or written(accounts, 0, "wrapped SOL account") != wrapped
                ):
                    _refuse(
                        "a token sync other than the one after wrapping the SOL you sell"
                    )
                synced = True
            elif op == b"\x09":
                if floor is None or closes or len(data) != 1 or len(accounts) != 3:
                    _refuse("a token account is closed other than once, after the swap")
                if (
                    written(accounts, 0, "closed account") != wrapped
                    or written(accounts, 1, "rent recipient") != mine
                    or written(accounts, 2, "close authority") != mine
                ):
                    _refuse(
                        "an account other than your wrapped SOL account is closed, or its rent goes elsewhere"
                    )
                closes += 1
            elif op == b"\x0c":
                if len(data) != 10 or data[9] != USDC_DECIMALS or len(accounts) != 4:
                    _refuse("a token transfer that is not a 6-decimal USDC fee")
                if (
                    written(accounts, 0, "fee source") != usdc
                    or written(accounts, 3, "fee authority") != mine
                ):
                    _refuse(
                        "a token transfer that is not foom's fee from your own USDC account"
                    )
                written(accounts, 2, "fee recipient")
                fees.append(int.from_bytes(data[1:9], "little"))
            else:
                _refuse("a token instruction a swap does not use")
        elif program == JUPITER_PROGRAM:
            if floor is not None:
                _refuse("the transaction holds more than one swap")
            floor = _route(data, accounts, written, expected)
        else:
            _refuse("an instruction for a program a foom swap does not use")
    if floor is None:
        _refuse("the transaction holds no swap")
    if native_sell and (wrapped_in != terms["amount_in"] or not synced):
        _refuse(
            "the SOL you sell is not wrapped into your own account for exactly the proposal's amount"
        )
    if len(fees) != 1:
        _refuse("the transaction does not hold exactly one foom fee")
    fee, bps = fees[0], terms["max_slippage_bps"]
    if buy:
        over = fee * 10000 > (terms["amount_in"] + fee) * MAX_FEE_BPS
    else:
        over = fee * (10000 - bps) > floor * MAX_FEE_BPS
    if fee > MAX_FEE_FLOOR_ATOMIC and over:
        _refuse("foom's fee is above the larger of $0.95 and 2% of the trade")
    units = budget.get(2, MAX_COMPUTE_UNITS)
    priority = -(-budget.get(3, 0) * units // 10**6)
    ceiling = os.environ.get("FOOM_TRADE_MAX_PRIORITY_LAMPORTS", MAX_PRIORITY_LAMPORTS)
    if not ceiling.isascii() or not ceiling.isdigit():
        _refuse("FOOM_TRADE_MAX_PRIORITY_LAMPORTS is not a whole number of lamports")
    if priority > int(ceiling):
        _refuse("the priority fee is above %s lamports" % ceiling)
    quote = terms["quote_usd"]
    if quote is not None:
        tokens = Fraction(floor if buy else terms["amount_in"], 10 ** terms["decimals"])
        usdc_amount = Fraction(
            terms["amount_in"] if buy else floor, 10**USDC_DECIMALS
        )
        implied = usdc_amount / tokens
        if abs(implied - quote) * 10000 > quote * (bps + PRICE_MARGIN_BPS):
            _refuse("the swap's floor implies a price too far from the market price")
    return {
        "side": terms["side"],
        "mint": terms["mint"],
        "decimals": terms["decimals"],
        "amount_in": terms["amount_in"],
        "floor": floor,
        "fee": fee,
        "priority_lamports": priority,
    }


def _amount(atomic, decimals):
    whole, part = divmod(atomic, 10**decimals)
    text = (
        ("%d.%0*d" % (whole, decimals, part)).rstrip("0").rstrip(".")
        if decimals
        else str(whole)
    )
    return text


def _usdc(value):
    return "%s USDC" % _amount(value, USDC_DECIMALS)


def describe(seen):
    decimals = seen["decimals"]
    if seen["side"] == "buy":
        trade = "buy %s with %s, at least %s out" % (
            seen["mint"],
            _usdc(seen["amount_in"]),
            _amount(seen["floor"], decimals),
        )
    else:
        trade = "sell %s of %s for at least %s before foom's fee" % (
            _amount(seen["amount_in"], decimals),
            seen["mint"],
            _usdc(seen["floor"]),
        )
    return "Signing: %s; foom fee %s; priority fee at most %d lamports." % (
        trade,
        _usdc(seen["fee"]),
        seen["priority_lamports"],
    )


def wire_transaction(seed, message):
    """The base64 wire transaction: one signature, then the message bytes."""
    return base64.b64encode(b"\x01" + sign(seed, message) + message).decode()


# --- the connector --------------------------------------------------------------

_rpc_id = 0


def mcp_call(name, arguments):
    """Call one connector tool. Returns (result, refusal): exactly one is set."""
    global _rpc_id
    _rpc_id += 1
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": _rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    ).encode()
    req = urllib.request.Request(
        MCP,
        method="POST",
        data=body,
        headers={
            "Authorization": "Bearer " + KEY,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "deva-foom-trade/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", "replace")[:300]
        hint = (
            "\n503 agent_key_unavailable means no FOOM account is open for this key yet: "
            "open it first (FOOM.md section 4)."
            if e.code == 503
            else ""
        )
        raise SystemExit(
            "ERROR: the FOOM connector answered HTTP %s to %s. %s%s"
            % (e.code, name, detail, hint)
        )
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        raise SystemExit(
            "ERROR: %s did not complete (%s). Retry later; if it persists, tell your human."
            % (name, e)
        )
    try:
        decoded = json.loads(payload)
    except ValueError:
        raise SystemExit(
            "ERROR: the connector answered %s with a non-JSON body. Tell your human."
            % name
        )
    if not isinstance(decoded, dict):
        raise SystemExit(
            "ERROR: the connector answered %s with %s, not an object."
            % (name, type(decoded).__name__)
        )
    if "error" in decoded:
        err = (
            decoded["error"]
            if isinstance(decoded["error"], dict)
            else {"message": decoded["error"]}
        )
        raise SystemExit(
            "ERROR: the connector could not run %s: %s. Nothing was signed or sent beyond this call. "
            "Retry later; if it persists, tell your human."
            % (name, err.get("message", err))
        )
    result = decoded.get("result") or {}
    content = result.get("content") or []
    text = content[0].get("text") if content and isinstance(content[0], dict) else None
    try:
        data = json.loads(text) if text is not None else {}
    except ValueError:
        if result.get("isError"):
            # A refusal the connector wrote as plain text (an argument check,
            # for example): show the agent the words it was given.
            return None, {"error": "tool_error", "message": text}
        raise SystemExit(
            "ERROR: the connector answered %s with an unreadable tool result." % name
        )
    if result.get("isError"):
        return None, data if isinstance(data, dict) else {"error": str(data)}
    return data, None


def need(payload, key, what):
    if not isinstance(payload, dict) or key not in payload:
        raise SystemExit(
            "ERROR: unrecognised answer from %s - no '%s' field. Tell your human."
            % (what, key)
        )
    return payload[key]


# --- the state file ---------------------------------------------------------------


def state_path(proposal_id):
    safe = "".join(c for c in proposal_id if c.isalnum() or c in "-_")
    return os.path.join(os.path.expanduser(STATE_DIR), safe + ".json")


def load_state(proposal_id):
    try:
        with open(state_path(proposal_id)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("signed_transaction") else None


def save_state(state):
    path = state_path(state["proposal_id"])
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def clear_state(proposal_id):
    try:
        os.remove(state_path(proposal_id))
    except OSError:
        pass


# --- execute ----------------------------------------------------------------------


def market_of(ctx):
    if ctx.get("market") is None:
        summary = ctx.get("summary")
        if not isinstance(summary, dict) or summary.get("venue") != "spot":
            _refuse(
                "foom sent no spot summary for this proposal to check the transaction against"
            )
        market, refusal = mcp_call(TOOL_MARKET, {"market_id": summary.get("market_id")})
        if refusal is not None:
            code = refusal.get("error")
            shown = code if isinstance(code, str) and _is_code(code) else "no code"
            raise SystemExit(
                "ERROR: refusing to sign - foom refused to show the proposal's market (%s). "
                "Nothing was signed. Run this again later." % shown
            )
        proposal_terms(summary, market)
        ctx["market"] = market
    return ctx["market"]


def prepare_and_sign(proposal_id, seed, mine, ctx):
    """Ask FOOM for the bytes, check them, sign, and record the state before anything is sent."""
    market = market_of(ctx)
    prepared, refusal = mcp_call(TOOL_PREPARE, {"id": proposal_id})
    if refusal is not None:
        return None, refusal
    for field in PREPARE_FIELDS:
        need(prepared, field, TOOL_PREPARE)
    message_b64 = need(prepared["prepared"], "message", TOOL_PREPARE)
    try:
        message = base64.b64decode(message_b64, validate=True)
    except (ValueError, TypeError):
        raise SystemExit(
            "ERROR: prepare answered with a message that is not base64. Tell your human."
        )
    seen = check_swap(message, mine, ctx["summary"], market)
    print(describe(seen))
    sys.stdout.flush()
    state = {
        "proposal_id": proposal_id,
        "approval_jwt": prepared["approval_jwt"],
        "message": message_b64,
        "signed_transaction": wire_transaction(seed, message),
        "last_valid_block_height": prepared["last_valid_block_height"],
        "expires_at": prepared["expires_at"],
    }
    save_state(state)
    return state, None


def execute(proposal_id, send=True):
    seed = need_seed()
    mine = b58(pubkey(seed))
    if not KEY:
        raise SystemExit(
            "ERROR: no Deva API key. Save it to ~/.deva/api_key (chmod 600) or set "
            "DEVA_API_KEY, then run again."
        )
    proposal, refusal = mcp_call(TOOL_GET, {"id": proposal_id})
    if refusal is not None:
        raise SystemExit(
            "ERROR: FOOM refused to show proposal %s: %s"
            % (proposal_id, refusal.get("error"))
        )
    # A perp proposal is signed with the Ethereum key, by the other recipe.
    # Checked before the status, because a perp proposal has statuses of its own.
    summary = proposal.get("summary")
    if isinstance(summary, dict) and summary.get("venue") == "perp":
        raise SystemExit(
            "ERROR: %s is a perp proposal and this recipe trades spot only. Nothing was "
            "signed. Run `python3 scripts/foom_perps.py execute %s` instead."
            % (proposal_id, proposal_id)
        )
    status = need(proposal, "status", TOOL_GET)
    if status in TERMINAL_STATUSES:
        clear_state(proposal_id)
        print("Proposal %s is already %s. Nothing to do." % (proposal_id, status))
        if status in ("expired", "cancelled", "rejected"):
            print("No trade ran. Propose again if you still want it.")
        return 0
    if status not in ("pending", "approved"):
        raise SystemExit(
            "ERROR: proposal %s has status %r, which this recipe does not know."
            % (proposal_id, status)
        )

    ctx = {"summary": summary}
    state = load_state(proposal_id)
    if status == "approved" and state is None:
        raise SystemExit(
            "ERROR: proposal %s was already prepared, but not by this recipe on this machine "
            "(no state file). Signing it again could mean a second trade. Wait for it to settle, "
            "or pause the account and tell your human." % proposal_id
        )
    retried = False
    resends = 0
    earlier_code = None
    if state is None:
        state, refusal = _prepare_through_a_busy_quote(proposal_id, seed, mine, ctx)
        state, refusal = _retry_prepare(proposal_id, seed, mine, ctx, state, refusal)
        if refusal is not None:
            return _stop_on(refusal, proposal_id)
        retried = state.get("_retried", False)
        earlier_code = state.get("_earlier")
    elif not send:
        raise SystemExit(
            "ERROR: proposal %s was already signed on this machine. Nothing new was signed. "
            "Run `python3 scripts/foom_spot.py execute %s`: it sends the signed transaction, "
            "and prepares a replacement only after foom says those bytes cannot land."
            % (proposal_id, proposal_id)
        )
    else:
        print(
            "Resuming proposal %s with the transaction signed earlier; nothing is signed twice."
            % proposal_id
        )

    if not send:
        print(
            json.dumps(
                {
                    "id": proposal_id,
                    "proposal": {
                        "approval_jwt": state["approval_jwt"],
                        "signed_transaction": state["signed_transaction"],
                    },
                }
            )
        )
        return 0

    while True:
        executed, refusal = mcp_call(
            TOOL_EXECUTE,
            {
                "id": proposal_id,
                "proposal": {
                    "approval_jwt": state["approval_jwt"],
                    "signed_transaction": state["signed_transaction"],
                    "broadcast": "foom",
                },
            },
        )
        if refusal is None:
            break
        code = refusal.get("error")
        if code == "quote_unavailable":
            # FOOM refused before relaying anything, so the same signed bytes can go
            # again. A wait longer than the bytes live is not sat out or cut short.
            wait = _positive(refusal.get("retry_after_seconds"), RESEND_WAIT_SECONDS)
            if resends >= MAX_RESENDS or wait > MAX_RESEND_WAIT_SECONDS:
                return _stop_on(dict(refusal, _signed=True, _wait=wait), proposal_id)
            resends += 1
            print(
                "FOOM could not get a fresh quote; sending the same signed transaction "
                "again in %g s. Nothing is signed twice." % wait
            )
            time.sleep(wait)
            continue
        if (
            code in ("preparation_expired", "invalid_approval", "preparation_pending")
            and not retried
        ):
            retried = True
            earlier_code = code
            wait = refusal.get("retry_after_seconds")
            if (
                code == "preparation_pending"
                and isinstance(wait, (int, float))
                and wait > 0
            ):
                print(
                    "The last transaction may still land; waiting %s s before preparing again."
                    % wait
                )
                time.sleep(float(wait))
            print("FOOM answered %s; preparing once more." % code)
            # The earlier state file stays until the new prepare succeeds, so a
            # refused re-prepare leaves bytes a rerun can resend.
            state, refusal = prepare_and_sign(proposal_id, seed, mine, ctx)
            state, refusal = _retry_prepare(
                proposal_id, seed, mine, ctx, state, refusal, allow=False
            )
            if refusal is not None:
                return _stop_on(dict(refusal, _earlier=earlier_code), proposal_id)
            continue
        if code == "approval_used":
            print(
                "FOOM says this approval was already used by other bytes, so these were not "
                "sent; reading the recorded status."
            )
            break
        return _stop_on(dict(refusal, _earlier=earlier_code), proposal_id)

    sent = refusal is None
    status = need(executed, "status", TOOL_EXECUTE) if sent else "pending"
    if sent:
        print("Sent. FOOM reports %s; waiting for it to settle." % status)
    else:
        print("Waiting for FOOM to settle the bytes it already holds.")
    deadline = time.time() + MAX_WAIT_SECONDS
    while status not in TERMINAL_STATUSES and time.time() < deadline:
        time.sleep(POLL_SECONDS)
        proposal, refusal = mcp_call(TOOL_GET, {"id": proposal_id})
        if refusal is not None:
            raise SystemExit(
                "ERROR: could not read proposal %s while waiting: %s"
                % (proposal_id, refusal.get("error"))
            )
        status = need(proposal, "status", TOOL_GET)
    if status in TERMINAL_STATUSES:
        clear_state(proposal_id)
        print("Proposal %s is %s." % (proposal_id, status))
        return 0
    print(
        "Proposal %s is still %s after %d s. %s; run this again later to read the result "
        "(nothing will be signed twice)."
        % (
            proposal_id,
            status,
            MAX_WAIT_SECONDS,
            (
                "The transaction was sent"
                if sent
                else "FOOM holds other bytes for this approval"
            ),
        )
    )
    return 0


def _positive(value, default):
    """A wait FOOM sent, when it is a positive number; else the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


def _prepare_through_a_busy_quote(proposal_id, seed, mine, ctx):
    """Prepare, and prepare again while FOOM cannot get a fresh quote.

    Runs only before anything was signed for this proposal. FOOM ends it by
    itself with proposal_unavailable once the proposal has too little time left.
    """
    tries = 0
    while True:
        tries += 1
        state, refusal = prepare_and_sign(proposal_id, seed, mine, ctx)
        if refusal is None or refusal.get("error") != "quote_unavailable":
            return state, refusal
        wait = _positive(refusal.get("retry_after_seconds"), QUOTE_WAIT_SECONDS)
        if wait > MAX_QUOTE_WAIT_SECONDS:
            return None, dict(refusal, _tries=tries, _long_wait=wait)
        if tries >= MAX_PREPARES:
            return None, dict(refusal, _tries=tries)
        print(
            "FOOM could not get a fresh quote (try %d of %d); preparing again in %g s."
            % (tries, MAX_PREPARES, wait)
        )
        time.sleep(wait)


def _retry_prepare(proposal_id, seed, mine, ctx, state, refusal, allow=True):
    """One more prepare when the first one was refused for a retryable reason."""
    if refusal is None or not allow:
        return state, refusal
    code = refusal.get("error")
    if code not in ("preparation_pending", "preparation_expired", "invalid_approval"):
        return state, refusal
    wait = refusal.get("retry_after_seconds")
    if code == "preparation_pending" and isinstance(wait, (int, float)) and wait > 0:
        print(
            "The last transaction may still land; waiting %s s before preparing again."
            % wait
        )
        time.sleep(float(wait))
    print("FOOM answered %s; preparing once more." % code)
    state, refusal = prepare_and_sign(proposal_id, seed, mine, ctx)
    if state is not None:
        state["_retried"] = True
        state["_earlier"] = code
    if refusal is not None:
        refusal = dict(refusal, _earlier=code)
    return state, refusal


def _stop_on(refusal, proposal_id):
    code = refusal.get("error")
    message = refusal.get("message") or ""
    if code == "proposal_unavailable":
        print(
            "Proposal %s is no longer usable (it expired, was cancelled, or has too little time "
            "left). Nothing was sent. Propose again. %s" % (proposal_id, message)
        )
        return 0
    if code == "quote_unavailable" and refusal.get("_signed"):
        raise SystemExit(
            "ERROR: FOOM could not get a fresh quote for proposal %s and relayed nothing. The "
            "signed transaction is kept: run `python3 scripts/foom_spot.py execute %s` again "
            "in %g s to resend it (nothing will be signed twice). %s"
            % (proposal_id, proposal_id, max(refusal.get("_wait") or 0, 60), message)
        )
    if code == "quote_unavailable" and refusal.get("_long_wait"):
        raise SystemExit(
            "ERROR: FOOM cannot quote proposal %s now and asks to wait %g s, longer than this "
            "proposal can wait. Nothing was signed. Propose again after that wait. %s"
            % (proposal_id, refusal["_long_wait"], message)
        )
    if code == "quote_unavailable" and refusal.get("_tries"):
        raise SystemExit(
            "ERROR: FOOM could not get a fresh quote for proposal %s after %d tries. Nothing was "
            "signed. Wait a minute, then run this again while the proposal is still pending, or "
            "propose again. %s" % (proposal_id, refusal["_tries"], message)
        )
    if code in ("preparation_expired", "invalid_approval", "preparation_pending"):
        earlier = refusal.get("_earlier")
        answered = (
            "%s twice" % code
            if earlier in (None, code)
            else "%s, then %s" % (earlier, code)
        )
        if code == "preparation_pending":
            wait = refusal.get("retry_after_seconds")
            raise SystemExit(
                "ERROR: FOOM answered %s for proposal %s; the signing window kept closing. "
                "FOOM relayed nothing from this run, but the earlier bytes it still tracks "
                "may land. Run `python3 scripts/foom_spot.py execute %s` again "
                "after %s s to read the proposal before you propose another trade. %s"
                % (
                    answered,
                    proposal_id,
                    proposal_id,
                    wait if isinstance(wait, (int, float)) else "a few",
                    message,
                )
            )
        raise SystemExit(
            "ERROR: FOOM answered %s for proposal %s; the signing window kept closing. "
            "Nothing landed. Run this again, or pause the account and tell your human. %s"
            % (answered, proposal_id, message)
        )
    raise SystemExit(
        "ERROR: FOOM refused proposal %s with %s. %s" % (proposal_id, code, message)
    )


# --- the command line --------------------------------------------------------------


def _refuse_key_arguments(argv):
    for arg in argv:
        if arg in ("--key", "--seed", "--private-key") or arg.startswith(
            ("--key=", "--seed=")
        ):
            raise SystemExit(
                "ERROR: this recipe never takes a key as an argument. Put the seed in %s "
                "(hex, chmod 600) and run again." % key_path()
            )


def main(argv):
    global KEY_FILE_OVERRIDE
    if argv[:1] == ["--key-file"]:
        if len(argv) < 3:
            raise SystemExit("usage: foom_spot.py --key-file <path> <command> ...")
        KEY_FILE_OVERRIDE = argv[1]
        argv = argv[2:]
    # The guard also sees the --key-file value, so a flag cannot hide there.
    _refuse_key_arguments(([KEY_FILE_OVERRIDE] if KEY_FILE_OVERRIDE else []) + argv)
    if not argv:
        print(__doc__)
        return 1
    command, rest = argv[0], argv[1:]
    if command == "keygen":
        return keygen()
    seed = need_seed()
    mine = b58(pubkey(seed))
    if command == "sign-challenge":
        if len(rest) != 2 or not all(rest) or any(":" in part for part in rest):
            raise SystemExit(
                "usage: foom_spot.py sign-challenge <persona id> <challenge>  (the two values "
                "the challenge call returned; the recipe fills the template with its own "
                "address and never signs a free-form string)"
            )
        message = TRADING_WALLET_TEMPLATE.format(
            persona_id=rest[0], address=mine, challenge=rest[1]
        )
        print("Signed message: %s" % message)
        print(b58(sign(seed, message.encode())))
        return 0
    if command == "sign":
        if len(rest) != 1 or not rest[0] or rest[0].startswith("-"):
            raise SystemExit(
                "usage: foom_spot.py sign <proposal id>  (prepares your proposal, checks the "
                "transaction, signs it and prints the arguments for execute_trade_proposal)"
            )
        return execute(rest[0], send=False)
    if command == "execute":
        if len(rest) != 1 or not rest[0]:
            raise SystemExit("usage: foom_spot.py execute <proposal id>")
        return execute(rest[0])
    raise SystemExit(
        "unknown command %r; see the docstring at the top of this file." % command
    )


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit as stop:
        # Refusals are text for the agent: print them where it reads
        # (stdout) and exit 1, instead of Python's stderr default.
        if isinstance(stop.code, str):
            print(stop.code)
            sys.exit(1)
        raise
