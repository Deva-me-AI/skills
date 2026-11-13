"""Trade perps on foom with your own Ethereum trading key.

Ships beside SKILL.md in the Deva skill (scripts/foom_perps.py). Python 3
standard library only. Perps, the Ethereum key and money between Token cash
and Perps cash live here; spot trades are scripts/foom_spot.py. Your keys never
leave this machine and are never read from an argument.

    python3 scripts/foom_perps.py keygen                       # create ~/.deva/trading-evm.key, print the address
    python3 scripts/foom_perps.py sign-challenge <persona id> <challenge>   # wallet-challenge signature
    python3 scripts/foom_perps.py transfer to-perps <usd>      # Token cash -> Perps cash
    python3 scripts/foom_perps.py approve-builder-fee          # once per Ethereum wallet, before the first order
    python3 scripts/foom_perps.py execute <proposal id>        # prepare -> check -> sign -> execute -> wait
    python3 scripts/foom_perps.py transfer to-cash <usd>       # Perps cash -> Token cash

The Ethereum key signs six things and nothing else: the wallet challenge, the
builder-fee approval, a perp order and its leverage change, and the two
messages that move Perps cash back. Each one is rebuilt here from its fields
and checked before it is signed; no command signs a message or a hash you hand
it. `transfer to-perps` is a Solana transaction, signed with the Solana key the
spot recipe keeps (`--solana-key-file <path>` before the command reads another
file). A transfer in either direction is signed only when Relay's order pays
your own wallets, so both keys have to be on this machine for it.
"""
import base64
import contextlib
import decimal
import hashlib
import hmac
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

try:
    import fcntl
except ImportError:  # Windows has none: transfers there run without the lock below
    fcntl = None

EVM_KEY_PATH = "~/.deva/trading-evm.key"
SOLANA_KEY_PATH = "~/.deva/trading.key"
STATE_DIR = "~/.deva/foom-perps"
# The trading-wallet challenge template, filled with the persona id, this
# key's address and the challenge; the recipe never signs a free-form string.
TRADING_WALLET_TEMPLATE = "deva-trading-wallet-v1:{persona_id}:{address}:{challenge}"
SOLANA_KEY_FILE_OVERRIDE = None

# --- keccak-256 (the hash Ethereum uses; hashlib's sha3_256 pads differently) ---

_M64 = (1 << 64) - 1
_KECCAK_RC = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)
# Rotation offsets, indexed [x][y].
_KECCAK_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
_KECCAK_RATE = 136


def _rol(value, count):
    return ((value << count) | (value >> (64 - count))) & _M64 if count else value


def _keccak_f(a):
    for constant in _KECCAK_RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _KECCAK_ROT[x][y])
        a = [
            [
                b[x][y] ^ (~b[(x + 1) % 5][y] & _M64 & b[(x + 2) % 5][y])
                for y in range(5)
            ]
            for x in range(5)
        ]
        a[0][0] ^= constant
    return a


def keccak256(data):
    """The 32-byte keccak-256 of ``data``."""
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _KECCAK_RATE:
        padded.append(0x00)
    padded[-1] |= 0x80
    state = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), _KECCAK_RATE):
        for i in range(_KECCAK_RATE // 8):
            lane = padded[offset + 8 * i : offset + 8 * i + 8]
            state[i % 5][i // 5] ^= int.from_bytes(lane, "little")
        state = _keccak_f(state)
    return b"".join(state[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


# --- secp256k1 (the curve of an Ethereum key) -------------------------------------

_P = 2**256 - 2**32 - 977
# The group order and the base point, in eight-digit pieces.
_N = int(
    "FFFFFFFF"
    "FFFFFFFF"
    "FFFFFFFF"
    "FFFFFFFE"
    "BAAEDCE6"
    "AF48A03B"
    "BFD25E8C"
    "D0364141",
    16,
)
_GENERATOR = (
    int(
        "79BE667E"
        "F9DCBBAC"
        "55A06295"
        "CE870B07"
        "029BFCDB"
        "2DCE28D9"
        "59F2815B"
        "16F81798",
        16,
    ),
    int(
        "483ADA77"
        "26A3C465"
        "5DA4FBFC"
        "0E1108A8"
        "FD17B448"
        "A6855419"
        "9C47D08F"
        "FB10D4B8",
        16,
    ),
)


def _point_add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0]:
        if (p[1] + q[1]) % _P == 0:
            return None
        slope = 3 * p[0] * p[0] * pow(2 * p[1], _P - 2, _P) % _P
    else:
        slope = (q[1] - p[1]) * pow(q[0] - p[0], _P - 2, _P) % _P
    x = (slope * slope - p[0] - q[0]) % _P
    return x, (slope * (p[0] - x) - p[1]) % _P


def _point_mul(scalar, point):
    result = None
    while scalar:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _secret(seed):
    """The key as a number; a seed that is not a private key of the curve is refused."""
    secret = int.from_bytes(seed, "big") if len(seed) == 32 else 0
    if not 1 <= secret < _N:
        raise SystemExit(
            "ERROR: the Ethereum key file does not hold a usable key (not a usable key: it must "
            "be 32 bytes inside the curve's range). Nothing was signed. Tell your human."
        )
    return secret


def evm_public_key(seed):
    """The 64-byte public key: x then y."""
    x, y = _point_mul(_secret(seed), _GENERATOR)
    return x.to_bytes(32, "big") + y.to_bytes(32, "big")


def evm_address(seed):
    """The Ethereum address of the key, ``0x`` and 40 lowercase hex characters."""
    return "0x" + keccak256(evm_public_key(seed))[-20:].hex()


def _rfc6979_nonces(secret, digest):
    """Deterministic nonces for one key and one digest (RFC 6979, HMAC-SHA256)."""
    key = secret.to_bytes(32, "big")
    reduced = (int.from_bytes(digest, "big") % _N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + key + reduced, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + key + reduced, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < _N:
            yield candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def ecdsa_sign(seed, digest):
    """Sign a 32-byte digest: 65 bytes, r then s (always the low one) then 27 or 28."""
    secret = _secret(seed)
    if len(digest) != 32:
        raise SystemExit(
            "ERROR: only a 32-byte digest can be signed. Nothing was signed. Tell your human."
        )
    z = int.from_bytes(digest, "big")
    for nonce in _rfc6979_nonces(secret, digest):
        x, y = _point_mul(nonce, _GENERATOR)
        r = x % _N
        s = pow(nonce, _N - 2, _N) * (z + r * secret) % _N
        if r == 0 or s == 0 or x >= _N:
            continue
        odd = y & 1
        if s > _N // 2:
            s, odd = _N - s, odd ^ 1
        return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + odd])


def sig_hex(signature):
    return "0x" + signature.hex()


# --- message hashes ---------------------------------------------------------------


def _refuse(why):
    """Stop before signing. Every refusal says that nothing was signed."""
    raise SystemExit(
        "ERROR: refusing to sign - %s. Nothing was signed. Tell your human." % why
    )


def eip191_digest(text):
    """The digest of a personal message (EIP-191), as a wallet's "sign message" hashes it."""
    data = text.encode("utf-8")
    return keccak256(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
_DOMAIN_FIELDS = (
    ("name", "string"),
    ("version", "string"),
    ("chainId", "uint256"),
    ("verifyingContract", "address"),
)
_HEX = "0123456789abcdefABCDEF"
_DIGITS = "0123456789"


def is_address(value):
    return (
        isinstance(value, str)
        and len(value) == 42
        and value[:2] == "0x"
        and all(c in _HEX for c in value[2:])
    )


def _encode_field(kind, value, name):
    """One 32-byte word of an EIP-712 struct. Only the atomic kinds the recipe signs."""
    if kind == "string":
        if not isinstance(value, str):
            _refuse("the field %s is not text" % name)
        return keccak256(value.encode("utf-8"))
    if kind == "address":
        if not is_address(value):
            _refuse("the field %s is not an address" % name)
        return bytes(12) + bytes.fromhex(value[2:])
    if kind == "bytes32":
        if (
            isinstance(value, str)
            and len(value) == 66
            and value[:2] == "0x"
            and all(c in _HEX for c in value[2:])
        ):
            value = bytes.fromhex(value[2:])
        if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
            _refuse("the field %s is not 32 bytes" % name)
        return bytes(value)
    if kind in ("uint32", "uint64", "uint256"):
        if isinstance(value, str) and value and all(c in _DIGITS for c in value):
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            _refuse("the field %s is not a whole number" % name)
        if not 0 <= value < (1 << int(kind[4:])):
            _refuse("the field %s is out of range" % name)
        return value.to_bytes(32, "big")
    _refuse("the field %s has a type this recipe does not sign (%s)" % (name, kind))


def _struct_hash(primary_type, fields, message):
    names = [name for name, _ in fields]
    if not isinstance(message, dict) or sorted(message) != sorted(names):
        _refuse(
            "the %s message does not hold exactly the fields %s"
            % (primary_type, ", ".join(names))
        )
    signature = "%s(%s)" % (
        primary_type,
        ",".join("%s %s" % (kind, name) for name, kind in fields),
    )
    words = b"".join(_encode_field(kind, message[name], name) for name, kind in fields)
    return keccak256(keccak256(signature.encode("utf-8")) + words)


def eip712_digest(domain, primary_type, fields, message):
    """The EIP-712 digest of one flat struct. ``fields`` is ((name, type), ...), in order."""
    return keccak256(
        b"\x19\x01"
        + _struct_hash("EIP712Domain", _DOMAIN_FIELDS, domain)
        + _struct_hash(primary_type, fields, message)
    )


# Relay's order, as its settlement protocol types it. The id a wallet signs for a
# transfer is the struct hash of this order, so the order says who is paid.
RELAY_ORDER_TYPES = {
    "Order": (
        ("version", "string"),
        ("solverChainId", "string"),
        ("solver", "address"),
        ("salt", "uint256"),
        ("inputs", "Input[]"),
        ("output", "Output"),
        ("fees", "Fee[]"),
    ),
    "Input": (("payment", "InputPayment"), ("refunds", "InputRefund[]")),
    "InputPayment": (
        ("chainId", "string"),
        ("currency", "bytes"),
        ("amount", "uint256"),
        ("weight", "uint256"),
    ),
    "InputRefund": (
        ("chainId", "string"),
        ("recipient", "bytes"),
        ("currency", "bytes"),
        ("minimumAmount", "uint256"),
        ("deadline", "uint32"),
        ("extraData", "bytes"),
    ),
    "Output": (
        ("chainId", "string"),
        ("payments", "OutputPayment[]"),
        ("deadline", "uint32"),
        ("calls", "bytes[]"),
        ("extraData", "bytes"),
    ),
    "OutputPayment": (
        ("recipient", "bytes"),
        ("currency", "bytes"),
        ("minimumAmount", "uint256"),
        ("expectedAmount", "uint256"),
    ),
    "Fee": (
        ("recipientChainId", "string"),
        ("recipient", "bytes"),
        ("currencyChainId", "string"),
        ("currency", "bytes"),
        ("amount", "uint256"),
    ),
}


def _struct_refs(types, name, seen):
    """Every struct type ``name`` refers to, directly or through another."""
    for _, kind in types[name]:
        base = kind[:-2] if kind.endswith("[]") else kind
        if base in types and base not in seen:
            seen.add(base)
            _struct_refs(types, base, seen)
    return seen


def _type_text(types, name):
    """EIP-712 encodeType: the type, then the structs it refers to, sorted by name."""

    def one(t):
        return "%s(%s)" % (t, ",".join("%s %s" % (kind, f) for f, kind in types[t]))

    return one(name) + "".join(
        one(t) for t in sorted(_struct_refs(types, name, set()) - {name})
    )


def _struct_word(types, kind, value, name):
    """One 32-byte word of a struct that may hold structs, lists and bytes."""
    if kind.endswith("[]"):
        if not isinstance(value, list):
            _refuse("the field %s is not a list" % name)
        return keccak256(
            b"".join(_struct_word(types, kind[:-2], v, name) for v in value)
        )
    if kind in types:
        return hash_struct(types, kind, value)
    if kind == "bytes":
        if not isinstance(value, (bytes, bytearray)):
            _refuse("the field %s is not bytes" % name)
        return keccak256(bytes(value))
    return _encode_field(kind, value, name)


def hash_struct(types, primary_type, value):
    """The EIP-712 struct hash of ``value`` (no domain), for the types given and no others."""
    fields = types.get(primary_type) if isinstance(types, dict) else None
    if not fields:
        _refuse("the type %s is not one this recipe hashes" % primary_type)
    names = [name for name, _ in fields]
    if not isinstance(value, dict) or sorted(value) != sorted(names):
        _refuse(
            "the %s does not hold exactly the fields %s"
            % (primary_type, ", ".join(names))
        )
    words = b"".join(
        _struct_word(types, kind, value[name], "%s.%s" % (primary_type, name))
        for name, kind in fields
    )
    return keccak256(keccak256(_type_text(types, primary_type).encode("utf-8")) + words)


_ORDER_EVM_CHAINS = ("arbitrum", "base", "ethereum", "robinhood")


def _order_part(value, type_name, what):
    """One object of Relay's order: exactly the fields of its type."""
    names = [name for name, _ in RELAY_ORDER_TYPES[type_name]]
    if not isinstance(value, dict) or sorted(value) != sorted(names):
        _refuse("%s does not hold exactly the fields %s" % (what, ", ".join(names)))
    return value


def _order_hex(value, what, most=None):
    if (
        not isinstance(value, str)
        or value[:2] != "0x"
        or len(value) % 2
        or not all(c in _HEX for c in value[2:])
    ):
        _refuse("%s is not hex" % what)
    raw = bytes.fromhex(value[2:])
    if most is not None and len(raw) > most:
        _refuse("%s is longer than %d bytes" % (what, most))
    return raw


def _order_address(value, chain, what):
    """An address or a currency of the order, as the bytes its chain hashes."""
    if chain == "solana":
        try:
            raw = b58decode(value)
        except ValueError:
            raw = b""
        if len(raw) != 32:
            _refuse("%s is not a Solana address" % what)
        return raw
    if chain == "hyperliquid":
        raw = _order_hex(value, what, most=20)
        if not raw:
            _refuse("%s is empty" % what)
        return raw
    if chain in _ORDER_EVM_CHAINS:
        raw = _order_hex(value, what)
        if len(raw) != 20:
            _refuse("%s is not a 20-byte address" % what)
        return raw
    _refuse("%s sits on a chain this recipe does not know (%r)" % (what, chain))


def _order_amount(value, what):
    if not isinstance(value, str) or not value or not all(c in _DIGITS for c in value):
        _refuse("%s is not a whole number written in decimal" % what)
    return int(value)


def _order_deadline(value, what):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < 1 << 32
    ):
        _refuse("%s is not a time" % what)
    return value


def _order_list(value, what):
    if not isinstance(value, list):
        _refuse("%s is not a list" % what)
    return value


def relay_order_id(order):
    """The id of one of Relay's orders: the struct hash of the order, 32 bytes.

    The order arrives as JSON. Amounts are decimal text, the salt is hex, and an
    address is written the way its own chain writes it; each is turned into the
    value that is hashed, and anything that does not fit is refused.
    """
    order = _order_part(order, "Order", "Relay's order")
    salt = _order_hex(order["salt"], "the order's salt", most=32)
    if not salt:
        _refuse("the order's salt is empty")
    inputs = []
    for item in _order_list(order["inputs"], "the order's inputs"):
        item = _order_part(item, "Input", "an input of the order")
        payment = _order_part(item["payment"], "InputPayment", "the order's payment")
        refunds = []
        for refund in _order_list(item["refunds"], "the order's refunds"):
            refund = _order_part(refund, "InputRefund", "a refund of the order")
            chain = refund["chainId"]
            refunds.append(
                {
                    "chainId": chain,
                    "recipient": _order_address(
                        refund["recipient"], chain, "a refund's recipient"
                    ),
                    "currency": _order_address(
                        refund["currency"], chain, "a refund's currency"
                    ),
                    "minimumAmount": _order_amount(
                        refund["minimumAmount"], "a refund's minimum"
                    ),
                    "deadline": _order_deadline(
                        refund["deadline"], "a refund's deadline"
                    ),
                    "extraData": _order_hex(
                        refund["extraData"], "a refund's extra data"
                    ),
                }
            )
        inputs.append(
            {
                "payment": {
                    "chainId": payment["chainId"],
                    "currency": _order_address(
                        payment["currency"],
                        payment["chainId"],
                        "the payment's currency",
                    ),
                    "amount": _order_amount(payment["amount"], "the payment's amount"),
                    "weight": _order_amount(payment["weight"], "the payment's weight"),
                },
                "refunds": refunds,
            }
        )
    output = _order_part(order["output"], "Output", "the order's output")
    payments = []
    for paid in _order_list(output["payments"], "the output's payments"):
        paid = _order_part(paid, "OutputPayment", "a payment of the output")
        payments.append(
            {
                "recipient": _order_address(
                    paid["recipient"], output["chainId"], "the output's recipient"
                ),
                "currency": _order_address(
                    paid["currency"], output["chainId"], "the output's currency"
                ),
                "minimumAmount": _order_amount(
                    paid["minimumAmount"], "the output's minimum"
                ),
                "expectedAmount": _order_amount(
                    paid["expectedAmount"], "the output's expected amount"
                ),
            }
        )
    fees = []
    for fee in _order_list(order["fees"], "the order's fees"):
        fee = _order_part(fee, "Fee", "a fee of the order")
        fees.append(
            {
                "recipientChainId": fee["recipientChainId"],
                "recipient": _order_address(
                    fee["recipient"], fee["recipientChainId"], "a fee's recipient"
                ),
                "currencyChainId": fee["currencyChainId"],
                "currency": _order_address(
                    fee["currency"], fee["currencyChainId"], "a fee's currency"
                ),
                "amount": _order_amount(fee["amount"], "a fee's amount"),
            }
        )
    return hash_struct(
        RELAY_ORDER_TYPES,
        "Order",
        {
            "version": order["version"],
            "solverChainId": order["solverChainId"],
            "solver": order["solver"],
            "salt": int.from_bytes(salt, "big"),
            "inputs": inputs,
            "output": {
                "chainId": output["chainId"],
                "payments": payments,
                "deadline": _order_deadline(
                    output["deadline"], "the output's deadline"
                ),
                "calls": [
                    _order_hex(call, "a call of the output")
                    for call in _order_list(output["calls"], "the output's calls")
                ],
                "extraData": _order_hex(output["extraData"], "the output's extra data"),
            },
            "fees": fees,
        },
    )


def msgpack(value):
    """msgpack for what a Hyperliquid action holds, in the smallest form of each value."""
    if value is True:
        return b"\xc3"
    if value is False:
        return b"\xc2"
    if isinstance(value, int):
        if 0 <= value < 0x80:
            return bytes([value])
        for limit, tag, size in (
            (1 << 8, 0xCC, 1),
            (1 << 16, 0xCD, 2),
            (1 << 32, 0xCE, 4),
            (1 << 64, 0xCF, 8),
        ):
            if 0 <= value < limit:
                return bytes([tag]) + value.to_bytes(size, "big")
        _refuse("an action holds a number outside 0 to 2^64 - 1")
    if isinstance(value, str):
        data = value.encode("utf-8")
        if len(data) < 32:
            return bytes([0xA0 | len(data)]) + data
        if len(data) < 1 << 8:
            return b"\xd9" + bytes([len(data)]) + data
        if len(data) < 1 << 16:
            return b"\xda" + len(data).to_bytes(2, "big") + data
        _refuse("an action holds a text longer than 65535 bytes")
    if isinstance(value, (list, tuple)):
        if len(value) < 16:
            head = bytes([0x90 | len(value)])
        elif len(value) < 1 << 16:
            head = b"\xdc" + len(value).to_bytes(2, "big")
        else:
            _refuse("an action holds a list that is too long")
        return head + b"".join(msgpack(item) for item in value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            _refuse("an action holds a key that is not text")
        if len(value) < 16:
            head = bytes([0x80 | len(value)])
        elif len(value) < 1 << 16:
            head = b"\xde" + len(value).to_bytes(2, "big")
        else:
            _refuse("an action holds an object that is too large")
        return head + b"".join(
            msgpack(key) + msgpack(item) for key, item in value.items()
        )
    _refuse(
        "an action holds a value of a kind no Hyperliquid action has (%s)"
        % type(value).__name__
    )


# Hyperliquid's trading actions are signed as an "Agent" message whose
# connectionId is the hash of the action, its nonce and its expiry. 1337 is
# Hyperliquid's own constant, and source "a" is mainnet.
_L1_DOMAIN = {
    "name": "Exchange",
    "version": "1",
    "chainId": 1337,
    "verifyingContract": ZERO_ADDRESS,
}
_AGENT_FIELDS = (("source", "string"), ("connectionId", "bytes32"))


def _u64(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < 1 << 64
    ):
        _refuse("%s is not a whole number in range" % name)
    return value.to_bytes(8, "big")


def l1_connection_id(action, nonce, expires_after):
    """keccak(msgpack(action) | nonce | no vault | expiry), the hash Hyperliquid checks."""
    data = msgpack(action) + _u64(nonce, "the nonce") + b"\x00"
    if expires_after is not None:
        data += b"\x00" + _u64(expires_after, "expiresAfter")
    return keccak256(data)


def l1_digest(action, nonce, expires_after):
    """What the Ethereum key signs for one trading action."""
    message = {
        "source": "a",
        "connectionId": l1_connection_id(action, nonce, expires_after),
    }
    return eip712_digest(_L1_DOMAIN, "Agent", _AGENT_FIELDS, message)


# --- the Solana key: only the move from Token cash to Perps cash needs it -----------
# Copied from scripts/foom_spot.py, unchanged: recipes never import each other.

TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # pragma: allowlist secret
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # pragma: allowlist secret
)
TOKEN_APPROVE, TOKEN_SET_AUTHORITY, TOKEN_APPROVE_CHECKED = 4, 6, 13
SYSTEM_PROGRAM = "11111111111111111111111111111111"
SYSTEM_ASSIGN, SYSTEM_ASSIGN_WITH_SEED = 1, 10

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


def b58decode(text):
    """The bytes a base58 text stands for. Raises ValueError for anything else."""
    if not isinstance(text, str) or not text:
        raise ValueError("not base58")
    n = 0
    for c in text:
        digit = _ALPH.find(c)
        if digit < 0:
            raise ValueError("not base58")
        n = n * 58 + digit
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes(len(text) - len(text.lstrip("1"))) + body


# --- the trading key ----------------------------------------------------------


def _shortvec(raw, i):
    """Decode the compact-u16 at ``raw[i:]``; return (value, next index)."""
    value = 0
    for shift in (0, 7, 14):
        byte = raw[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
    raise ValueError("compact-u16 runs longer than three bytes")


def parse_message(raw):
    """The static keys, required signatures and top-level instructions of a message.

    Accepts a legacy message and a version 0 message (first byte 0x80, then the
    same header, static accounts, blockhash and instructions, followed by the
    address-table lookups). Every byte must be accounted for: bytes the parser
    did not read are not a transaction it can vouch for.
    """
    i = 0
    if raw and raw[0] & 0x80:
        if raw[0] & 0x7F:
            raise ValueError("a transaction version this recipe does not know")
        i = 1
    if len(raw) < i + 3:
        raise ValueError("too short to be a message")
    required = raw[i]
    i += 3
    count, i = _shortvec(raw, i)
    if not required or required > count or len(raw) < i + count * 32 + 32:
        raise ValueError("account table does not fit inside the message")
    keys = [b58(raw[i + n * 32 : i + (n + 1) * 32]) for n in range(count)]
    i += count * 32 + 32
    total, i = _shortvec(raw, i)
    instructions = []
    for _ in range(total):
        program = raw[i]
        i += 1
        accounts, i = _shortvec(raw, i)
        i += accounts
        length, i = _shortvec(raw, i)
        data = raw[i : i + length]
        i += length
        if i > len(raw) or program >= count:
            raise ValueError("an instruction runs past the end of the message")
        instructions.append((keys[program], data))
    if raw[0] & 0x80:
        tables, i = _shortvec(raw, i)
        for _ in range(tables):
            i += 32
            writable, i = _shortvec(raw, i)
            i += writable
            readonly, i = _shortvec(raw, i)
            i += readonly
            if i > len(raw):
                raise ValueError(
                    "an address-table lookup runs past the end of the message"
                )
    if i != len(raw):
        raise ValueError("%d bytes follow the message" % (len(raw) - i))
    return keys, required, instructions


def check_message(raw, mine):
    """Refuse anything but a transaction this key alone signs and pays for."""
    try:
        keys, required, instructions = parse_message(raw)
    except (IndexError, ValueError) as error:
        raise SystemExit(
            "ERROR: refusing to sign - the prepared message carries %s. Nothing was signed. "
            "Prepare again; if it repeats, tell your human." % error
        )
    if required != 1:
        raise SystemExit(
            "ERROR: refusing to sign - the message wants %d signatures, not just yours. "
            "Nothing was signed. Tell your human." % required
        )
    if keys[0] != mine:
        raise SystemExit(
            "ERROR: refusing to sign - account 0 (the signer and fee payer) is %s, not your "
            "trading key %s. Nothing was signed. Tell your human." % (keys[0], mine)
        )
    if not instructions:
        raise SystemExit(
            "ERROR: refusing to sign - the message holds no instruction. Nothing was signed."
        )
    for program, data in instructions:
        if (
            program == SYSTEM_PROGRAM
            and len(data) >= 4
            and int.from_bytes(data[:4], "little")
            in (SYSTEM_ASSIGN, SYSTEM_ASSIGN_WITH_SEED)
        ):
            raise SystemExit(
                "ERROR: refusing to sign - the message would change the owner of an account, "
                "which no trade needs. Nothing was signed. Tell your human."
            )
        if program in TOKEN_PROGRAMS and data[:1] in (
            bytes([TOKEN_APPROVE]),
            bytes([TOKEN_APPROVE_CHECKED]),
            bytes([TOKEN_SET_AUTHORITY]),
        ):
            raise SystemExit(
                "ERROR: refusing to sign - the message carries a token approval or authority change, "
                "which no trade needs. Nothing was signed. Tell your human."
            )


def wire_transaction(seed, message):
    """The base64 wire transaction: one signature, then the message bytes."""
    return base64.b64encode(b"\x01" + sign(seed, message) + message).decode()


# --- the closed list: what this recipe may sign -----------------------------------
# One check per message. Each compares every field with what the recipe expects,
# builds the digest from its own pinned types, and returns it. Anything else stops
# before a signature exists.

FEE_RATE = "0.05%"
FEE_TENTHS_BPS = 50
HL_CHAIN = "Mainnet"
HL_SIGNATURE_CHAIN_ID = "0xa4b1"
# Hyperliquid's id for USDC, and Relay's depository on Hyperliquid: the only
# place a move from Perps cash may go. foom's server pins the same address.
HL_USDC_TOKEN = "USDC:0x6d1e7cde53ba9467b783cb7c530ce054"
RELAY_DEPOSITORY = "0x66cf0aace1b4e562593bec10ec7868fba9932224"
# foom fixes an order's price bound when the proposal is made and keeps it for
# minutes, so the mark may have moved since: only a price further on the wrong
# side than the proposal's slippage plus this allowance is refused.
PRICE_ALLOWANCE = decimal.Decimal("0.05")
MAX_PERP_SLIPPAGE_BPS = 1000
MAX_EXPIRES_AHEAD_MS = 600000

_USER_DOMAIN = {
    "name": "HyperliquidSignTransaction",
    "version": "1",
    "chainId": 42161,
    "verifyingContract": ZERO_ADDRESS,
}
_RELAY_DOMAIN = {
    "name": "RelayNonceMapping",
    "version": "2",
    "chainId": 1,
    "verifyingContract": ZERO_ADDRESS,
}
_APPROVE_TYPE = "HyperliquidTransaction:ApproveBuilderFee"
_APPROVE_FIELDS = (
    ("hyperliquidChain", "string"),
    ("maxFeeRate", "string"),
    ("builder", "address"),
    ("nonce", "uint64"),
)
_SEND_TYPE = "HyperliquidTransaction:SendAsset"
_SEND_FIELDS = (
    ("hyperliquidChain", "string"),
    ("destination", "string"),
    ("sourceDex", "string"),
    ("destinationDex", "string"),
    ("token", "string"),
    ("amount", "string"),
    ("fromSubAccount", "string"),
    ("nonce", "uint64"),
)
_MAPPING_TYPE = "NonceMapping"
_MAPPING_FIELDS = (
    ("chainId", "string"),
    ("wallet", "address"),
    ("depositor", "address"),
    ("id", "bytes32"),
    ("nonce", "uint256"),
)


def _plain_decimal(value):
    """The number a plain decimal text holds ("25", "0.001"), or None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    whole, dot, fraction = value.partition(".")
    if not whole or not all(c in _DIGITS for c in whole):
        return None
    if dot and (not fraction or not all(c in _DIGITS for c in fraction)):
        return None
    return decimal.Decimal(value)


def same_decimal(a, b):
    a, b = _plain_decimal(a), _plain_decimal(b)
    return a is not None and b is not None and a == b


def same_address(a, b):
    return is_address(a) and is_address(b) and a.lower() == b.lower()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _object(value, what):
    if not isinstance(value, dict):
        _refuse("%s is not what foom documents (not an object)" % what)
    return value


def _exact_keys(value, expected, what):
    """The keys of a signed object, in Hyperliquid's own order and with nothing added."""
    if list(value) != list(expected):
        _refuse(
            "%s holds the fields %s, not exactly %s in that order"
            % (what, ", ".join(map(str, value)) or "(none)", ", ".join(expected))
        )


def _typed_message(typed, domain, primary_type, fields, what):
    """Check a typed-data object against the pinned domain and type; return its message.

    The digest is always built from the recipe's own type list, never from this one.
    """
    typed = _object(typed, what)
    handed = _object(typed.get("domain"), "the domain of " + what)
    if (
        sorted(handed) != ["chain_id", "name", "verifying_contract", "version"]
        or handed["name"] != domain["name"]
        or handed["version"] != domain["version"]
        or not _is_int(handed["chain_id"])
        or handed["chain_id"] != domain["chainId"]
        or not same_address(handed["verifying_contract"], domain["verifyingContract"])
    ):
        _refuse("%s is not on the domain %s" % (what, domain["name"]))
    if typed.get("primary_type") != primary_type:
        _refuse("%s is not a %s message" % (what, primary_type))
    expected = {primary_type: [{"name": name, "type": kind} for name, kind in fields]}
    if typed.get("types") != expected:
        _refuse("%s does not have the fields of a %s message" % (what, primary_type))
    return _object(typed.get("message"), "the message of " + what)


def builder_fee_digest(prepared):
    """The digest and nonce of the builder-fee approval foom prepared.

    The rate is exactly 0.05% and the builder is the one the same answer names.
    """
    what = "the builder-fee approval"
    prepared = _object(prepared, what)
    if prepared.get("status") != "approval_required":
        _refuse("foom did not answer with an approval to sign")
    message = _typed_message(
        prepared.get("typed_data"), _USER_DOMAIN, _APPROVE_TYPE, _APPROVE_FIELDS, what
    )
    builder = _object(prepared.get("builder"), "the builder of " + what)
    if message.get("hyperliquidChain") != HL_CHAIN:
        _refuse("%s is not for Hyperliquid mainnet" % what)
    if message.get("maxFeeRate") != FEE_RATE:
        _refuse(
            "%s names the rate %r, not %s" % (what, message.get("maxFeeRate"), FEE_RATE)
        )
    if not same_address(message.get("builder"), builder.get("address")):
        _refuse("%s names another builder than the one foom's answer states" % what)
    nonce = prepared.get("nonce")
    if (
        not _is_int(nonce)
        or not _is_int(message.get("nonce"))
        or message["nonce"] != nonce
    ):
        _refuse("the nonce of %s is not the one foom prepared" % what)
    return eip712_digest(_USER_DOMAIN, _APPROVE_TYPE, _APPROVE_FIELDS, message), nonce


def _held_margin_mode(market_id, positions):
    """The margin mode of the position you hold on this market, or None when you hold none."""
    positions = _object(positions, "the positions read")
    rows = positions.get("perps")
    if positions.get("status") != "ok" or not isinstance(rows, list):
        _refuse("the positions read does not list your perp positions")
    held = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("market_id") == market_id
    ]
    if not held:
        return None
    if len(held) != 1 or held[0].get("leverage_type") not in ("cross", "isolated"):
        _refuse(
            "the positions read does not state the margin mode of your position on this market"
        )
    return held[0]["leverage_type"]


def perp_parts(plan, summary, market, mine, now_ms, positions=None):
    """The actions to sign for a prepared perp proposal: [(name, action, nonce), ...].

    The order is checked against your own proposal (``summary``) and a fresh read
    of the market: asset, side, size, price bound, immediate-or-cancel, fee. A
    leverage change is also checked against ``positions`` (a fresh get_positions
    read): it keeps the margin mode of a position you already hold on the market.
    """
    plan = _object(plan, "the prepared plan")
    summary = _object(summary, "the proposal's summary")
    market = _object(market, "the market read")
    if plan.get("venue") != "perp" or summary.get("venue") != "perp":
        _refuse("this is not a perp plan for a perp proposal")
    if plan.get("network") != "mainnet":
        _refuse("the plan is for the network %r, not mainnet" % plan.get("network"))
    if not same_address(plan.get("owner"), mine):
        _refuse(
            "the plan is for the wallet %r, not your Ethereum trading wallet %s"
            % (plan.get("owner"), mine)
        )
    nonce, expires = plan.get("nonce"), plan.get("expiresAfter")
    if not _is_int(nonce) or not _is_int(expires):
        _refuse("the plan's nonce or expiresAfter is not a whole number")
    if not now_ms < expires <= now_ms + MAX_EXPIRES_AHEAD_MS:
        _refuse("the plan has expired, or expires more than ten minutes from now")
    side, size, slippage = (
        summary.get("side"),
        summary.get("size"),
        summary.get("max_slippage_bps"),
    )
    if (
        side not in ("buy", "sell")
        or _plain_decimal(size) is None
        or not _is_int(slippage)
        or not 0 <= slippage <= MAX_PERP_SLIPPAGE_BPS
    ):
        _refuse("the proposal's summary does not state a side, a size and a slippage")
    if market.get("market_id") != summary.get("market_id"):
        _refuse("the market read is not the proposal's market")
    if "asset_index" not in market:
        _refuse(
            "foom does not report asset_index for this market yet, so the order cannot be "
            "matched to the market you proposed"
        )
    asset, mark = market["asset_index"], market.get("mark_price_usd")
    if (
        not _is_int(asset)
        or isinstance(mark, bool)
        or not isinstance(mark, (int, float))
        or not mark > 0
    ):
        _refuse("the market read has no usable asset_index and mark_price_usd")
    mark = decimal.Decimal(str(mark))

    action = _object(plan.get("action"), "the order action")
    _exact_keys(action, ("type", "orders", "grouping", "builder"), "the order action")
    orders = action["orders"]
    if (
        action["type"] != "order"
        or action["grouping"] != "na"
        or not isinstance(orders, list)
        or len(orders) != 1
    ):
        _refuse("the action is not one plain order")
    order = _object(orders[0], "the order")
    fields = ("a", "b", "p", "s", "r", "t") + (("c",) if "c" in order else ())
    _exact_keys(order, fields, "the order")
    if not _is_int(order["a"]) or order["a"] != asset:
        _refuse(
            "the order is for asset %r, and your proposal's market is asset %d"
            % (order["a"], asset)
        )
    if not isinstance(order["b"], bool) or order["b"] != (side == "buy"):
        _refuse("the order's side is not your proposal's (%s)" % side)
    if not isinstance(order["s"], str) or not same_decimal(order["s"], size):
        _refuse("the order's size %r is not your proposal's %s" % (order["s"], size))
    price = _plain_decimal(order["p"]) if isinstance(order["p"], str) else None
    if price is None or price <= 0:
        _refuse("the order's price is not a positive decimal")
    room = decimal.Decimal(slippage) / 10000 + PRICE_ALLOWANCE
    sell_floor = mark * max(decimal.Decimal("0.0001"), 1 - room)
    if (side == "buy" and price > mark * (1 + room)) or (
        side == "sell" and price < sell_floor
    ):
        _refuse(
            "the order's price bound %s is further from the market (%s) than your slippage plus %s%%"
            % (order["p"], mark, PRICE_ALLOWANCE * 100)
        )
    if not isinstance(order["r"], bool):
        _refuse("the order's reduce-only flag is not true or false")
    if order["t"] != {"limit": {"tif": "Ioc"}}:
        _refuse(
            "the order is not an immediate-or-cancel order, which is what a market order is"
        )
    if "c" in order:
        client = order["c"]
        if (
            not isinstance(client, str)
            or len(client) != 34
            or client[:2] != "0x"
            or not all(c in _HEX for c in client[2:])
        ):
            _refuse("the order's client id is malformed")
    builder = _object(action["builder"], "the order's builder")
    _exact_keys(builder, ("b", "f"), "the order's builder")
    if not is_address(builder["b"]):
        _refuse("the order's builder is not an address")
    if not _is_int(builder["f"]) or builder["f"] != FEE_TENTHS_BPS:
        _refuse("the order's builder fee is not exactly %s" % FEE_RATE)

    parts = []
    if "leverageAction" in plan or "leverageNonce" in plan:
        leverage = _object(plan.get("leverageAction"), "the leverage action")
        _exact_keys(
            leverage, ("type", "asset", "isCross", "leverage"), "the leverage action"
        )
        cap = market.get("agent_leverage_cap")
        if (
            leverage["type"] != "updateLeverage"
            or not _is_int(leverage["asset"])
            or leverage["asset"] != asset
            or not isinstance(leverage["isCross"], bool)
            or not _is_int(leverage["leverage"])
            or not _is_int(summary.get("leverage"))
            or leverage["leverage"] < 1
            or leverage["leverage"] != summary.get("leverage")
            or not _is_int(cap)
            or leverage["leverage"] > cap
        ):
            _refuse(
                "the leverage change is not your proposal's leverage on your proposal's market, "
                "or it is above your cap"
            )
        held = _held_margin_mode(summary.get("market_id"), positions)
        if held is not None and leverage["isCross"] is not (held == "cross"):
            _refuse(
                "the leverage change would switch the margin mode of the position you hold "
                "on this market (%s)" % held
            )
        leverage_nonce = plan.get("leverageNonce")
        if not _is_int(leverage_nonce) or leverage_nonce == nonce:
            _refuse("the leverage change has no nonce of its own")
        parts.append(("leverage", leverage, leverage_nonce))
    parts.append(("order", action, nonce))
    return parts


def _relay_request_id(value):
    if (
        not isinstance(value, str)
        or len(value) != 66
        or value[:2] != "0x"
        or not all(c in _HEX for c in value[2:])
    ):
        _refuse("Relay's request id is not 32 bytes")
    return bytes.fromhex(value[2:])


def _order_id_bytes(order_id):
    """The id of the order that was checked: the 32 bytes relay_order_check returned."""
    if not isinstance(order_id, (bytes, bytearray)) or len(order_id) != 32:
        _refuse("the id of the checked order is not 32 bytes")
    return bytes(order_id)


def transfer_out_digests(payload, amount_usd, mine, order_id):
    """The two digests that move Perps cash back: Relay's authorization, then SendAsset.

    The money may only go to Relay's depository, in USDC, for the amount you asked.
    """
    payload = _object(payload, "the transfer payload")
    if payload.get("kind") != "relay_sendasset_out":
        _refuse("the payload is not a move from Perps cash")
    authorize = _object(payload.get("authorize"), "Relay's authorization")
    mapping = _typed_message(
        authorize.get("typed_data"),
        _RELAY_DOMAIN,
        _MAPPING_TYPE,
        _MAPPING_FIELDS,
        "Relay's authorization",
    )
    if mapping.get("chainId") != "hyperliquid":
        _refuse("Relay's authorization is not for Hyperliquid")
    if not same_address(mapping.get("wallet"), mine) or not same_address(
        mapping.get("depositor"), mine
    ):
        _refuse(
            "Relay's authorization is not for your Ethereum trading wallet %s" % mine
        )
    post = authorize.get("post")
    body = post.get("body") if isinstance(post, dict) else None
    # foom posts Relay's own body beside the signature: it names the id that is signed.
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("id"), str)
        or body["id"].lower() != str(mapping.get("id")).lower()
    ):
        _refuse("Relay's authorization signs another id than the one sent to Relay")
    # And that id is the hash of the order that was checked: the order says who is paid.
    if _order_id_bytes(order_id) != _relay_request_id(mapping.get("id")):
        _refuse(
            "Relay's authorization is for another order than the one that was checked"
        )

    send = _object(payload.get("send_asset"), "the Hyperliquid transfer")
    message = _typed_message(
        send.get("typed_data"),
        _USER_DOMAIN,
        _SEND_TYPE,
        _SEND_FIELDS,
        "the Hyperliquid transfer",
    )
    names = [name for name, _ in _SEND_FIELDS]
    if sorted(message) != sorted(names):
        _refuse("the Hyperliquid transfer does not hold exactly its eight fields")
    if message["hyperliquidChain"] != HL_CHAIN:
        _refuse("the Hyperliquid transfer is not for mainnet")
    if not same_address(message["destination"], RELAY_DEPOSITORY):
        _refuse(
            "the Hyperliquid transfer would send your money to %r, not to Relay's depository"
            % (message["destination"],)
        )
    if (
        not isinstance(message["token"], str)
        or message["token"].lower() != HL_USDC_TOKEN.lower()
    ):
        _refuse("the Hyperliquid transfer is not in USDC")
    if not same_decimal(message["amount"], amount_usd):
        _refuse(
            "the Hyperliquid transfer is for %r, not the %s you asked for"
            % (message["amount"], amount_usd)
        )
    if message["fromSubAccount"] != "":
        _refuse("the Hyperliquid transfer names a sub-account")
    if message["sourceDex"] not in ("", "spot") or message["destinationDex"] not in (
        "",
        "spot",
    ):
        _refuse("the Hyperliquid transfer names a dex other than the main one or spot")
    nonce = message["nonce"]
    if (
        not _is_int(nonce)
        or send.get("nonce") != nonce
        or str(mapping.get("nonce")) != str(nonce)
        or str(payload.get("nonce")) != str(nonce)
    ):
        _refuse("the two messages do not share one nonce")
    action = _object(send.get("action"), "the Hyperliquid transfer action")
    if sorted(action) != sorted(["type", "signatureChainId"] + names):
        _refuse(
            "the Hyperliquid transfer action holds other fields than the signed message"
        )
    if (
        action["type"] != "sendAsset"
        or action["signatureChainId"] != HL_SIGNATURE_CHAIN_ID
    ):
        _refuse("the Hyperliquid transfer action is not a mainnet sendAsset")
    for name in names:
        if (
            type(action[name]) is not type(message[name])
            or action[name] != message[name]
        ):
            _refuse(
                "the Hyperliquid transfer action differs from the signed message in %s"
                % name
            )
    return (
        eip712_digest(_RELAY_DOMAIN, _MAPPING_TYPE, _MAPPING_FIELDS, mapping),
        eip712_digest(_USER_DOMAIN, _SEND_TYPE, _SEND_FIELDS, message),
    )


# Relay settles every order through its own solver, and an order between Token cash
# and Perps cash has one shape: one payment in, one payment out, a refund on each side.
RELAY_SOLVER = "0xf70da97812cb96acdf810712aa562db8dfa3dbef"
RELAY_SOLVER_CHAIN = "base"
RELAY_ORDER_VERSION = "v1"
# What an order calls Perps cash: Hyperliquid's USDC, at eight decimals.
HL_ORDER_USDC = "0x00000000000000000000000000000000"
HL_ORDER_DECIMALS = 8


def _order_atomic(amount_usd, decimals):
    """Dollars as the whole number of units an order counts in, or None."""
    amount = _plain_decimal(amount_usd)
    if amount is None or amount <= 0:
        return None
    units = amount.scaleb(decimals)
    return int(units) if units == units.to_integral_value() else None


def relay_order_check(order, direction, amount_usd, mine_evm, mine_solana):
    """Check that Relay's order pays your own wallets, and return its id.

    Returns (order id, minimum paid out, expected paid out), the two amounts in
    dollars. The order must be the plain one this recipe knows, settled by Relay's
    solver, for the amount you asked, paying out to your own wallet on the other
    side and refunding your own wallets on both.
    """
    if direction == "to-perps":
        origin, target, decimals_in, decimals_out = (
            "solana",
            "hyperliquid",
            SOLANA_USDC_DECIMALS,
            HL_ORDER_DECIMALS,
        )
    elif direction == "to-cash":
        origin, target, decimals_in, decimals_out = (
            "hyperliquid",
            "solana",
            HL_ORDER_DECIMALS,
            SOLANA_USDC_DECIMALS,
        )
    else:
        _refuse("the direction of the transfer is not one this recipe knows")
    # Hashing first checks every field's type and that no field is missing or extra.
    order_id = relay_order_id(order)

    def mine(chain, value):
        if chain == "solana":
            return value == mine_solana
        return same_address(value, mine_evm)

    def usdc(chain, value):
        if chain == "solana":
            return value == SOLANA_USDC_MINT
        return value.lower() == HL_ORDER_USDC

    if (
        order["version"] != RELAY_ORDER_VERSION
        or order["solverChainId"] != RELAY_SOLVER_CHAIN
        or not same_address(order["solver"], RELAY_SOLVER)
    ):
        _refuse("Relay's order is not settled by Relay's own solver")
    output = order["output"]
    if (
        len(order["inputs"]) != 1
        or len(output["payments"]) != 1
        or output["calls"]
        or output["extraData"] != "0x"
        or order["fees"]
    ):
        _refuse("Relay's order is not the plain one payment in, one payment out")
    payment, refunds = order["inputs"][0]["payment"], order["inputs"][0]["refunds"]
    if (
        payment["chainId"] != origin
        or not usdc(origin, payment["currency"])
        or payment["weight"] != "1"
    ):
        _refuse("Relay's order does not take USDC from the side you are moving from")
    asked = _order_atomic(amount_usd, decimals_in)
    if asked is None or int(payment["amount"]) != asked:
        _refuse("Relay's order is not for the %s you asked to move" % amount_usd)
    if sorted(refund["chainId"] for refund in refunds) != ["hyperliquid", "solana"]:
        _refuse("Relay's order does not hold one refund on each side")
    for refund in refunds:
        if (
            not mine(refund["chainId"], refund["recipient"])
            or not usdc(refund["chainId"], refund["currency"])
            or refund["minimumAmount"] != "0"
            or refund["extraData"] != "0x"
        ):
            _refuse("Relay's order would not refund your own wallet in USDC")
    paid = output["payments"][0]
    if (
        output["chainId"] != target
        or not mine(target, paid["recipient"])
        or not usdc(target, paid["currency"])
    ):
        _refuse("Relay's order does not pay your own wallet in USDC on the other side")
    scale = decimal.Decimal(10) ** decimals_out
    return (
        order_id,
        decimal.Decimal(paid["minimumAmount"]) / scale,
        decimal.Decimal(paid["expectedAmount"]) / scale,
    )


# Moving Token cash to Perps cash is one Solana transaction with one instruction:
# a deposit into Relay's depository program. These are Relay's public addresses
# on Solana and the layout of that instruction; foom's server pins the same.
RELAY_SOLANA_DEPOSITORY = (
    "99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2"  # pragma: allowlist secret
)
SOLANA_USDC_MINT = (
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # pragma: allowlist secret
)
SOLANA_USDC_DECIMALS = 6
_RELAY_DEPOSIT_DISCRIMINATOR = bytes.fromhex("0b9c60da27a3b413")
_WALLET, _TOKEN_ACCOUNT = object(), object()
# The ten accounts of the deposit, in order. _WALLET is your own key;
# _TOKEN_ACCOUNT is your own USDC token account, written out in the transaction.
_RELAY_DEPOSIT_ACCOUNTS = (
    "Dodg2HifwU8rmaVVyMyUZDGTRbqAJTyVYxXPwcbNpBKc",  # depository state  # pragma: allowlist secret
    _WALLET,
    _WALLET,
    "7uTT8Xi5RWXzy7h9XL244GRgEycDYDhLjr3ZyNdXi8pZ",  # depository authority  # pragma: allowlist secret
    SOLANA_USDC_MINT,
    _TOKEN_ACCOUNT,
    "4nvJ5zWdVspxJiNZzB127U6amPH98SFFkBx2JZrAduia",  # depository USDC account  # pragma: allowlist secret
    TOKEN_PROGRAMS[0],
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # associated-token program  # pragma: allowlist secret
    SYSTEM_PROGRAM,
)


# Relay's quote names an address lookup table, so the deposit's fixed accounts are
# usually read from it instead of being written out. An entry of a lookup table
# never changes once written, so the table and the entry say which address it is.
RELAY_SOLANA_LOOKUP_TABLE = (
    "Hm9fUgcn7qwDaiNTFiGh6pNtVATgnaRcmK6Bbx6EMZfP"  # pragma: allowlist secret
)
_RELAY_TABLE_ENTRIES = {
    1: _RELAY_DEPOSIT_ACCOUNTS[0],
    2: _RELAY_DEPOSIT_ACCOUNTS[3],
    5: SOLANA_USDC_MINT,
    7: _RELAY_DEPOSIT_ACCOUNTS[6],
    14: SYSTEM_PROGRAM,
    15: TOKEN_PROGRAMS[0],
    16: _RELAY_DEPOSIT_ACCOUNTS[8],
}


def _instructions_with_accounts(raw):
    """The top-level instructions as (program, accounts, data), every account resolved.

    ``raw`` already passed ``parse_message``, so every length in it is sound. An
    account written out in the message resolves to its address. One read from a
    lookup table resolves to its address only when the table is Relay's and the
    entry is one this recipe knows; otherwise it resolves to None.
    """
    versioned = bool(raw[0] & 0x80)
    i = (1 if versioned else 0) + 3
    count, i = _shortvec(raw, i)
    keys = [b58(raw[i + n * 32 : i + (n + 1) * 32]) for n in range(count)]
    i += count * 32 + 32
    total, i = _shortvec(raw, i)
    parsed = []
    for _ in range(total):
        program = keys[raw[i]]
        i += 1
        accounts, i = _shortvec(raw, i)
        indexes = list(raw[i : i + accounts])
        i += accounts
        length, i = _shortvec(raw, i)
        parsed.append((program, indexes, raw[i : i + length]))
        i += length
    writable, readonly = [], []
    if versioned:
        tables, i = _shortvec(raw, i)
        for _ in range(tables):
            table = b58(raw[i : i + 32])
            i += 32
            for loaded in (writable, readonly):
                entries, i = _shortvec(raw, i)
                for entry in raw[i : i + entries]:
                    known = (
                        table == RELAY_SOLANA_LOOKUP_TABLE
                        and entry in _RELAY_TABLE_ENTRIES
                    )
                    loaded.append(_RELAY_TABLE_ENTRIES[entry] if known else None)
                i += entries
    resolved = keys + writable + readonly
    return [
        (program, [resolved[n] if n < len(resolved) else None for n in indexes], data)
        for program, indexes, data in parsed
    ]


def solana_transfer_message(payload, mine_solana, amount_usd, order_id):
    """The message bytes of the Solana transaction that moves Token cash to Perps cash.

    Signed only when your key is the one signer and fee payer and the transaction
    holds exactly one instruction: a deposit of the amount you asked for, in
    USDC, into Relay's depository.
    """
    payload = _object(payload, "the transfer payload")
    if payload.get("kind") != "relay_solana_transaction":
        _refuse("the payload is not a move from Token cash")
    for field in ("fee_payer", "user_signer"):
        if field in payload and payload[field] != mine_solana:
            _refuse(
                "the transaction's %s is %r, not your Solana trading key %s"
                % (field, payload[field], mine_solana)
            )
    amount = _plain_decimal(amount_usd)
    atomic = amount * 10**SOLANA_USDC_DECIMALS if amount is not None else None
    if atomic is None or atomic <= 0 or atomic != atomic.to_integral_value():
        _refuse(
            "the amount %r is not dollars with at most six decimals" % (amount_usd,)
        )
    try:
        raw = base64.b64decode(payload.get("transaction_base64"), validate=True)
        slots, start = _shortvec(raw, 0)
    except (ValueError, TypeError, IndexError):
        _refuse("the transaction is not readable base64")
    if slots != 1 or len(raw) < start + 64:
        _refuse("the transaction wants %d signatures, not just yours" % slots)
    message = raw[start + 64 :]
    check_message(message, mine_solana)
    instructions = _instructions_with_accounts(message)
    if len(instructions) != 1:
        _refuse(
            "the transaction holds %d instructions, not the one deposit"
            % len(instructions)
        )
    program, accounts, data = instructions[0]
    if program != RELAY_SOLANA_DEPOSITORY:
        _refuse(
            "the transaction's instruction is for the program %s, not Relay's depository"
            % program
        )
    if (
        len(data) != 48
        or data[:8] != _RELAY_DEPOSIT_DISCRIMINATOR
        or int.from_bytes(data[8:16], "little") != int(atomic)
    ):
        _refuse(
            "the instruction is not a deposit of the %s USDC you asked for" % amount_usd
        )
    # The deposit names the order it pays for: it must be the order that was checked.
    if data[16:48] != _order_id_bytes(order_id):
        _refuse("the deposit is for another order than the one that was checked")
    if len(accounts) != len(_RELAY_DEPOSIT_ACCOUNTS):
        _refuse(
            "the deposit names %d accounts, not %d"
            % (len(accounts), len(_RELAY_DEPOSIT_ACCOUNTS))
        )
    for position, (address, expected) in enumerate(
        zip(accounts, _RELAY_DEPOSIT_ACCOUNTS)
    ):
        if expected is _WALLET:
            if address != mine_solana:
                _refuse("account %d of the deposit is not your own wallet" % position)
        elif expected is _TOKEN_ACCOUNT:
            if address is None:
                _refuse(
                    "account %d of the deposit, your own token account, is not written out"
                    % position
                )
        elif address != expected:
            _refuse(
                "account %d of the deposit is not Relay's (%s)"
                % (position, address or "read from an unknown lookup table entry")
            )
    return message


# --- the Ethereum key -------------------------------------------------------------


def _home_path(path):
    path = os.path.expanduser(path)
    if path.startswith("~"):
        raise SystemExit(
            "ERROR: cannot work out your home directory (HOME is unset and this user has no "
            "passwd entry). Set HOME and re-run, or the trading key would land in a stray "
            "directory."
        )
    return path


def evm_key_path():
    return _home_path(EVM_KEY_PATH)


def load_evm_seed():
    """The stored 32-byte key, or None when the file is absent or unusable."""
    try:
        with open(evm_key_path(), "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        seed = bytes.fromhex(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        return None
    if len(seed) != 32 or not 1 <= int.from_bytes(seed, "big") < _N:
        return None
    return seed


def need_evm_seed():
    seed = load_evm_seed()
    if seed is None:
        raise SystemExit(
            "ERROR: no Ethereum trading key at %s. Run `python3 scripts/foom_perps.py keygen` "
            "first, then register the printed address as your Ethereum trading wallet."
            % evm_key_path()
        )
    return seed


def keygen():
    """Create the Ethereum key if it is absent; print its address either way."""
    seed = load_evm_seed()
    if seed is None:
        if os.path.exists(evm_key_path()):
            raise SystemExit(
                "ERROR: %s exists but is not a usable key. Do not overwrite it blindly: "
                "if a wallet was registered from it, its funds would be lost. Tell your human."
                % evm_key_path()
            )
        directory = os.path.dirname(evm_key_path())
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        seed = os.urandom(32)
        while not 1 <= int.from_bytes(seed, "big") < _N:
            seed = os.urandom(32)
        fd = os.open(evm_key_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(seed.hex() + "\n")
        print(
            "Created %s (keep it chmod 600, back it up, never print or commit it)."
            % evm_key_path()
        )
    print("Ethereum trading wallet address: %s" % evm_address(seed))
    print(
        "Register it as your Ethereum trading wallet (chain ETHEREUM), then call "
        "open_foom_account again. It holds no money until you move Token cash to Perps cash."
    )
    return 0


# --- the connector --------------------------------------------------------------

MCP = "https://foomfamily.ai/mcp"
TOOL_PREPARE_FEE = "prepare_builder_fee_approval"
TOOL_EXECUTE_FEE = "execute_builder_fee_approval"
# The waits read the same settings as the spot recipe.
POLL_SECONDS = float(os.environ.get("FOOM_TRADE_POLL_SECONDS", "3"))
MAX_WAIT_SECONDS = float(os.environ.get("FOOM_TRADE_MAX_WAIT_SECONDS", "180"))
QUOTE_WAIT_SECONDS = float(os.environ.get("FOOM_TRADE_QUOTE_WAIT_SECONDS", "20"))
MAX_QUOTE_WAIT_SECONDS = 60
MAX_PREPARES = 5
RESEND_WAIT_SECONDS = float(os.environ.get("FOOM_TRADE_RESEND_WAIT_SECONDS", "10"))
MAX_RESEND_WAIT_SECONDS = 15
MAX_RESENDS = 2
MAX_READ_LIMIT_WAIT_SECONDS = 60
MAX_READ_LIMIT_WAITS = (
    3  # shared for the whole run: every early read and every prepare wait
)

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
            "User-Agent": "deva-foom-perps/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", "replace")[:300]
        hint = (
            "\n503 agent_key_unavailable means no foom account is open for this key yet: "
            "open it first (FOOM.md section 4)."
            if e.code == 503
            else ""
        )
        raise SystemExit(
            "ERROR: the foom connector answered HTTP %s to %s. %s%s"
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


def need_deva_key():
    if not KEY:
        raise SystemExit(
            "ERROR: no Deva API key. Save it to ~/.deva/api_key (chmod 600) or set "
            "DEVA_API_KEY, then run again."
        )


def _positive(value, default):
    """A wait foom sent, when it is a positive number; else the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


def refusal_text(refusal, doing):
    """What foom answered when it refused, with the next step the connector gave."""
    code = refusal.get("error") if isinstance(refusal, dict) else None
    text = "foom refused to %s: %s." % (doing, code or "no code given")
    if isinstance(refusal, dict):
        if isinstance(refusal.get("message"), str) and refusal["message"]:
            text += " " + refusal["message"]
        if isinstance(refusal.get("reason"), str) and refusal["reason"]:
            text += " (reason: %s)" % refusal["reason"]
        wait = _positive(refusal.get("retry_after_seconds"), None)
        if wait:
            text += " Wait %g seconds before you try again." % wait
    return text


def _read_limit_wait(name, refusal, read_positions):
    """The seconds to wait before asking again after foom's Hyperliquid read limit
    refused name, printed on screen; None when the refusal is another one or this
    run's whole wait budget (read_positions.waits, shared by every early read and
    every prepare in the run) is used up."""
    if refusal is None or refusal.get("error") != "agent_hl_read_ceiling":
        return None
    wait = _positive(refusal.get("retry_after_seconds"), MAX_READ_LIMIT_WAIT_SECONDS)
    if (
        wait > MAX_READ_LIMIT_WAIT_SECONDS
        or read_positions.waits >= MAX_READ_LIMIT_WAITS
    ):
        return None
    read_positions.waits += 1
    print(
        "foom's limit on Hyperliquid reads for agents is reached for this minute; "
        "waiting %g s, then asking %s again (wait %d of %d)."
        % (wait, name, read_positions.waits, MAX_READ_LIMIT_WAITS)
    )
    sys.stdout.flush()
    return wait


def through_read_limit(name, arguments, read_positions):
    """Call a tool foom counts against its Hyperliquid read limit, waiting for the
    next minute while this run's whole wait budget allows it. Returns (result,
    refusal) like mcp_call."""
    while True:
        result, refusal = mcp_call(name, arguments)
        wait = _read_limit_wait(name, refusal, read_positions)
        if wait is None:
            return result, refusal
        time.sleep(wait)


# --- the state files ---------------------------------------------------------------
# One file per thing in flight, written before anything is sent, so a re-run
# resends what it already signed instead of signing again.


def state_path(kind, ident):
    safe = "".join(c for c in str(ident) if c.isalnum() or c in "-_")
    return os.path.join(_home_path(STATE_DIR), "%s-%s.json" % (kind, safe))


def load_state(kind, ident):
    """The stored state, or None when the file is absent, unreadable or not an object."""
    try:
        with open(state_path(kind, ident)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_state(kind, ident, state):
    path = state_path(kind, ident)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def clear_state(kind, ident):
    try:
        os.remove(state_path(kind, ident))
    except OSError:
        pass


# --- approve-builder-fee -------------------------------------------------------------


def approve_builder_fee():
    """Approve foom's 0.05% builder fee on Hyperliquid, once per Ethereum wallet."""
    seed = need_evm_seed()
    need_deva_key()
    address = evm_address(seed)
    prepared, refusal = mcp_call(TOOL_PREPARE_FEE, {})
    if refusal is not None:
        raise SystemExit(
            "ERROR: "
            + refusal_text(refusal, "prepare the builder-fee approval")
            + " Nothing was signed."
        )
    if need(prepared, "status", TOOL_PREPARE_FEE) == "approved":
        print("The builder fee is already approved for %s. Nothing to sign." % address)
        return 0
    digest, nonce = builder_fee_digest(prepared)
    signature = sig_hex(ecdsa_sign(seed, digest))
    result, refusal = mcp_call(
        TOOL_EXECUTE_FEE, {"proposal": {"nonce": nonce, "signature": signature}}
    )
    if refusal is not None:
        raise SystemExit(
            "ERROR: "
            + refusal_text(refusal, "relay the builder-fee approval")
            + " Run this again: it is safe "
            "to repeat, and it answers approved if the approval did land."
        )
    if need(result, "status", TOOL_EXECUTE_FEE) != "approved":
        raise SystemExit(
            "ERROR: unrecognised answer from %s (status %r). Call prepare_builder_fee_approval, "
            "or run this again, to see whether the approval landed."
            % (TOOL_EXECUTE_FEE, result.get("status"))
        )
    print(
        "The builder fee (%s) is approved for %s. You can execute perp proposals now."
        % (FEE_RATE, address)
    )
    return 0


# --- execute (a perp proposal) ---------------------------------------------------------

TOOL_GET = "get_proposal"
TOOL_PREPARE = "prepare_trade_proposal"
TOOL_EXECUTE = "execute_trade_proposal"
TOOL_MARKET = "get_market"
TOOL_POSITIONS = "get_positions"
# What foom reports for a proposal. Pinned by a contract test on the server
# side: a change on foom's side changes this recipe and bumps the skill version.
PROPOSAL_STATUSES = (
    "pending",
    "approved",
    "filled",
    "failed",
    "partial_leverage_only",
    "rejected",
    "expired",
    "cancelled",
)
END_STATUSES = (
    "filled",
    "failed",
    "partial_leverage_only",
    "rejected",
    "expired",
    "cancelled",
)
PREPARE_FIELDS = ("prepared", "approval_jwt", "expires_at")


def _ended(proposal_id, status, already):
    """Say how a proposal ended; nothing is left to resend."""
    clear_state("proposal", proposal_id)
    if status == "partial_leverage_only":
        print(
            "Proposal %s ended as partial_leverage_only: the leverage change for this market "
            "landed on Hyperliquid and the order did not run. No position was opened by it. "
            "Propose again if you still want the order." % proposal_id
        )
        return 0
    print("Proposal %s is %s%s." % (proposal_id, "already " if already else "", status))
    if already and status in ("expired", "cancelled", "rejected"):
        print("No trade ran. Propose again if you still want it.")
    return 0


class PositionsRead:
    """get_positions for one prepare: read right before it, and again after any wait.
    One instance is shared for the whole run, so its wait budget (self.waits) is too.
    """

    def __init__(self):
        self.kept = None
        self.waits = 0

    def early(self):
        """The read before a prepare, which may wait for foom's read limit: nothing is
        prepared yet. A refusal other than that limit is read once more; if it still
        fails, the prepare goes ahead without it - the plan may turn out not to need
        it at all. If it does, the read taken after the prepare (``needed``) never
        waits and lands in the prepare's minute, so a ceiling there stops the run
        unsigned. Only a ceiling this run's wait budget cannot clear stops the
        run here, unprepared, instead of preparing into a guaranteed ceiling refusal
        on the read taken after it.
        """
        if self.kept is not None:
            return None
        for _ in range(2):
            positions, refusal = through_read_limit(TOOL_POSITIONS, {}, self)
            if refusal is None:
                self.kept = positions
                return None
            if refusal.get("error") == "agent_hl_read_ceiling":
                return dict(refusal, _positions="early")
        return None

    def needed(self):
        """The read a leverage change is checked against. After a prepare it never waits:
        the prepared plan is only valid for a short time."""
        if self.kept is None:
            positions, refusal = mcp_call(TOOL_POSITIONS, {})
            if refusal is not None:
                return None, dict(refusal, _positions="leverage")
            self.kept = positions
        return self.kept, None

    def forget(self):
        self.kept = None


def _may_change_leverage(summary):
    """Whether to read positions before the prepare: every perp proposal except a
    reduce-only exit, which is expected to only ever shrink a position. When this
    is False, a prepared plan that still carries a leverage change is refused
    (``reduce_only_leverage_change``) without reading positions after the prepare:
    no early read exists to check it against."""
    return summary.get("reduce_only") is not True


def prepare_and_sign(proposal_id, seed, mine, summary, read_positions):
    """Ask foom for the plan, check it, sign it, and record the state before anything is sent."""
    while True:
        if _may_change_leverage(summary):
            refusal = read_positions.early()
            if refusal is not None:
                return None, refusal
        prepared, refusal = mcp_call(TOOL_PREPARE, {"id": proposal_id})
        wait = _read_limit_wait(TOOL_PREPARE, refusal, read_positions)
        if wait is None:
            break
        time.sleep(wait)
        read_positions.forget()
    if refusal is not None:
        return None, refusal
    for field in PREPARE_FIELDS:
        need(prepared, field, TOOL_PREPARE)
    plan = _object(prepared["prepared"], "the prepared plan")
    market, refusal = mcp_call(TOOL_MARKET, {"market_id": summary.get("market_id")})
    if refusal is not None:
        raise SystemExit(
            "ERROR: "
            + refusal_text(refusal, "show the market of proposal %s" % proposal_id)
            + " The order could not be checked against the market. Nothing was signed. Run this again."
        )
    positions = None
    if "leverageAction" in plan or "leverageNonce" in plan:
        if not _may_change_leverage(summary):
            return None, {
                "error": "reduce_only_leverage_change",
                "_positions": "unsupported",
            }
        positions, refusal = read_positions.needed()
        if refusal is not None:
            return None, refusal
    parts = perp_parts(plan, summary, market, mine, int(time.time() * 1000), positions)
    signed = {}
    for name, action, nonce in parts:
        digest = l1_digest(action, nonce, plan["expiresAfter"])
        signed[name] = {
            "action": action,
            "nonce": nonce,
            "network": plan["network"],
            "expiresAfter": plan["expiresAfter"],
            "signature": sig_hex(ecdsa_sign(seed, digest)),
        }
    payload = (
        signed["order"]
        if len(parts) == 1
        else {"leverage": signed["leverage"], "order": signed["order"]}
    )
    state = {
        "proposal_id": proposal_id,
        "approval_jwt": prepared["approval_jwt"],
        "signed_payload": json.dumps(payload, separators=(",", ":")),
        "expires_at": prepared["expires_at"],
        "address": mine,
    }
    save_state("proposal", proposal_id, state)
    return state, None


def _prepare_through_a_busy_quote(proposal_id, seed, mine, summary, read_positions):
    """Prepare, and prepare again while foom cannot get a fresh quote. Nothing is signed yet."""
    tries = 0
    while True:
        tries += 1
        state, refusal = prepare_and_sign(
            proposal_id, seed, mine, summary, read_positions
        )
        if (
            refusal is None
            or refusal.get("error") != "quote_unavailable"
            or refusal.get("_positions")
        ):
            return state, refusal
        wait = _positive(refusal.get("retry_after_seconds"), QUOTE_WAIT_SECONDS)
        if wait > MAX_QUOTE_WAIT_SECONDS or tries >= MAX_PREPARES:
            return None, dict(refusal, _tries=tries)
        print(
            "foom could not get a fresh quote (try %d of %d); preparing again in %g s."
            % (tries, MAX_PREPARES, wait)
        )
        time.sleep(wait)
        read_positions.forget()


def _prepare_once_more(proposal_id, seed, mine, summary, read_positions, refusal):
    """One more prepare after foom said the earlier plan cannot run any more."""
    code = refusal.get("error")
    wait = _positive(refusal.get("retry_after_seconds"), None)
    if code == "preparation_pending" and wait:
        print(
            "The last order may still reach Hyperliquid; waiting %g s before preparing again."
            % wait
        )
        time.sleep(wait)
    print("foom answered %s; preparing once more." % code)
    read_positions.forget()
    state, again = prepare_and_sign(proposal_id, seed, mine, summary, read_positions)
    if again is not None:
        again = dict(again, _earlier=code)
    return state, again


def _proposal_unavailable(proposal_id):
    """foom will not prepare or run the proposal any more: read how it stands before
    saying anything, because an order sent earlier may be out."""
    proposal, refusal = mcp_call(TOOL_GET, {"id": proposal_id})
    if refusal is not None:
        raise SystemExit(
            "ERROR: proposal %s can no longer be run, and " % proposal_id
            + refusal_text(refusal, "show it")
            + " Run `python3 scripts/foom_perps.py execute %s` again later to read how it ended; "
            "do not propose again until it shows an end." % proposal_id
        )
    status = need(proposal, "status", TOOL_GET)
    if status in END_STATUSES:
        return _ended(proposal_id, status, already=True)
    if status == "approved":
        raise SystemExit(
            "ERROR: proposal %s can no longer be run, and an order for it is out: foom reports "
            "approved. Run `python3 scripts/foom_perps.py execute %s` again later to read how it "
            "ended; do not propose again until it shows an end."
            % (proposal_id, proposal_id)
        )
    clear_state("proposal", proposal_id)
    print(
        "Proposal %s is no longer usable (it expired, was cancelled, or has too little time "
        "left). Nothing was sent. Propose again." % proposal_id
    )
    return 0


def _stop_on(refusal, proposal_id, signed=False, sent=False):
    code = refusal.get("error")
    if code == "proposal_unavailable":
        return _proposal_unavailable(proposal_id)
    if refusal.get("_positions") == "leverage":
        text = (
            "ERROR: "
            + refusal_text(refusal, "show your positions")
            + " The leverage change could not be checked against the position you hold."
        )
    elif refusal.get("_positions") == "unsupported":
        text = (
            "ERROR: foom prepared a leverage change for an order it marked "
            "reduce-only, so your positions were not read before the prepare and "
            "cannot be read in time to check it."
        )
    elif refusal.get("_positions"):
        text = "ERROR: " + refusal_text(refusal, "show your positions")
    else:
        text = "ERROR: " + refusal_text(refusal, "run proposal %s" % proposal_id)
    if refusal.get("_earlier"):
        text += " (after %s)" % refusal["_earlier"]
    if sent:
        text += (
            " The signed order is kept and may have reached foom: run "
            "`python3 scripts/foom_perps.py execute %s` again to read what happened (nothing "
            "is signed twice), and do not propose again until it ends." % proposal_id
        )
    elif signed:
        text += (
            " The signed order is kept: run `python3 scripts/foom_perps.py execute %s` again to "
            "resend it (nothing will be signed twice)." % proposal_id
        )
    elif code == "quote_unavailable":
        text += (
            " Nothing was signed. Wait a minute, then run this again, or propose again."
        )
    elif refusal.get("_positions") == "early":
        text += " Nothing was signed. Run this again in a minute."
    elif refusal.get("_positions") == "unsupported":
        text += (
            " Nothing was signed. To close or shrink the position, propose it again"
            " without a leverage value, which keeps the current setting."
        )
    elif refusal.get("_positions") and code == "agent_hl_read_ceiling":
        text += " Nothing was signed. Run this again in a minute."
    elif refusal.get("_positions"):
        text += " Nothing was signed. Run this again."
    else:
        text += " Nothing was sent."
    raise SystemExit(text)


def execute(proposal_id):
    seed = need_evm_seed()
    need_deva_key()
    mine = evm_address(seed)
    proposal, refusal = mcp_call(TOOL_GET, {"id": proposal_id})
    if refusal is not None:
        raise SystemExit(
            "ERROR: " + refusal_text(refusal, "show proposal %s" % proposal_id)
        )
    status = need(proposal, "status", TOOL_GET)
    if status in END_STATUSES:
        return _ended(proposal_id, status, already=True)
    if status not in ("pending", "approved"):
        raise SystemExit(
            "ERROR: proposal %s has status %r, which this recipe does not know."
            % (proposal_id, status)
        )
    summary = proposal.get("summary")
    if not isinstance(summary, dict):
        raise SystemExit(
            "ERROR: foom did not report the summary of proposal %s, so the order cannot be "
            "checked against what you proposed. Nothing was signed. Tell your human."
            % proposal_id
        )
    if summary.get("venue") != "perp":
        raise SystemExit(
            "ERROR: %s is a spot proposal and this recipe trades perps only. Nothing was signed. "
            "Run `python3 scripts/foom_spot.py execute %s` instead."
            % (proposal_id, proposal_id)
        )

    state = load_state("proposal", proposal_id)
    if state is not None and not (
        isinstance(state.get("signed_payload"), str)
        and state.get("approval_jwt")
        and state.get("address")
    ):
        state = None  # an incomplete file is no state
    if state is not None and state["address"] != mine:
        raise SystemExit(
            "ERROR: the order kept for proposal %s was signed by another key (%s), not the one in "
            "%s now (%s). Nothing was sent. Put that key back, or wait for the proposal to expire "
            "and propose again." % (proposal_id, state["address"], evm_key_path(), mine)
        )
    retried = False
    read_positions = PositionsRead()
    if state is None:
        if status == "approved":
            raise SystemExit(
                "ERROR: proposal %s is approved, but its signed order is not kept here. "
                "Signing it again could mean a second trade. Nothing was signed or sent. "
                "Run this again later to read the outcome; do not propose again until it ends."
                % proposal_id
            )
        state, refusal = _prepare_through_a_busy_quote(
            proposal_id, seed, mine, summary, read_positions
        )
        if (
            refusal is not None
            and not refusal.get("_positions")
            and refusal.get("error")
            in ("preparation_pending", "preparation_expired", "invalid_approval")
        ):
            state, refusal = _prepare_once_more(
                proposal_id, seed, mine, summary, read_positions, refusal
            )
            retried = True
        if refusal is not None:
            return _stop_on(refusal, proposal_id)
    else:
        print(
            "Resuming proposal %s with the order signed earlier; nothing is signed twice."
            % proposal_id
        )

    resends = 0
    while True:
        executed, refusal = mcp_call(
            TOOL_EXECUTE,
            {
                "id": proposal_id,
                "proposal": {
                    "approval_jwt": state["approval_jwt"],
                    "signed_transaction": state["signed_payload"],
                    "broadcast": "foom",
                },
            },
        )
        if refusal is None:
            break
        code = refusal.get("error")
        if code == "quote_unavailable":
            # foom refused before relaying anything, so the same signed order can go again.
            wait = _positive(refusal.get("retry_after_seconds"), RESEND_WAIT_SECONDS)
            if resends >= MAX_RESENDS or wait > MAX_RESEND_WAIT_SECONDS:
                return _stop_on(refusal, proposal_id, signed=True)
            resends += 1
            print(
                "foom could not get a fresh quote; sending the same signed order again in %g s."
                % wait
            )
            time.sleep(wait)
            continue
        if (
            code in ("preparation_expired", "invalid_approval", "preparation_pending")
            and not retried
        ):
            retried = True
            # The earlier state file stays until the new prepare succeeds.
            state, again = _prepare_once_more(
                proposal_id, seed, mine, summary, read_positions, refusal
            )
            if again is not None:
                return _stop_on(again, proposal_id, sent=True)
            continue
        if code == "approval_used":
            print(
                "foom says this approval was already used; reading the recorded status."
            )
            break
        return _stop_on(refusal, proposal_id, sent=True)

    sent = refusal is None
    reported = need(executed, "status", TOOL_EXECUTE) if sent else "approved"
    if sent:
        print("Sent. foom reports %s; waiting for it to settle." % reported)
    deadline = time.time() + MAX_WAIT_SECONDS
    # How the proposal ended is read from its own record, which also tells a
    # leverage-only outcome apart; when foom already reports an end it is read at once.
    status, read_now = "approved", reported in END_STATUSES
    while status not in END_STATUSES and (read_now or time.time() < deadline):
        if not read_now:
            time.sleep(POLL_SECONDS)
        read_now = False
        proposal, refusal = mcp_call(TOOL_GET, {"id": proposal_id})
        if refusal is not None:
            raise SystemExit(
                "ERROR: "
                + refusal_text(refusal, "show proposal %s while waiting" % proposal_id)
                + " Run this again to read the result (nothing will be signed twice)."
            )
        status = need(proposal, "status", TOOL_GET)
    if status in END_STATUSES:
        return _ended(proposal_id, status, already=False)
    print(
        "Proposal %s is still %s after %d s. Run this again later to read the result "
        "(nothing will be signed twice)." % (proposal_id, status, MAX_WAIT_SECONDS)
    )
    return 0


# --- transfer (Token cash <-> Perps cash) -------------------------------------------------

TOOL_QUOTE = "quote_agent_transfer"
TOOL_MOVE = "execute_agent_transfer"
TOOL_READ = "get_agent_transfer"
# What foom reports for a transfer. Pinned by a contract test on the server side.
TRANSFER_STATUSES = (
    "quoted",
    "executing",
    "settled",
    "failed",
    "refund_pending",
    "refunded",
    "manual_review",
)
TRANSFER_DONE = ("settled", "refunded", "failed")
TRANSFER_OPEN = ("executing", "refund_pending")
_DIRECTIONS = {"to-perps": "to_perps", "to-cash": "to_cash"}
_BALANCE = {"to-perps": "Perps cash", "to-cash": "Token cash"}
TRANSFER_USAGE = (
    "usage: foom_perps.py transfer to-perps <usd> | transfer to-cash <usd> | transfer forget <request id>  "
    "(<usd> is dollars as a plain number with at most six decimals, for example 25 or 12.5)"
)


def solana_key_path():
    return _home_path(SOLANA_KEY_FILE_OVERRIDE or SOLANA_KEY_PATH)


def need_solana_seed():
    """The Solana trading key the spot recipe keeps; it signs the move into Perps cash."""
    try:
        with open(solana_key_path(), "rb") as f:
            seed = bytes.fromhex(f.read().decode("ascii").strip())
    except (OSError, UnicodeDecodeError, ValueError):
        seed = b""
    if len(seed) != 32:
        raise SystemExit(
            "ERROR: no Solana trading key at %s. Token cash is in your Solana trading wallet: "
            "create it with `python3 scripts/foom_spot.py keygen` and register it, or pass "
            "`--solana-key-file <path>` before the command." % solana_key_path()
        )
    return seed


def _sign_transfer(direction, amount_usd, payload, order):
    """Check Relay's order and the quoted payload, then sign.

    Returns (submission, the signing address, minimum paid out, expected paid out).
    The order must pay your own wallets, and what is signed must carry its id.
    """
    solana_seed, evm_seed = need_solana_seed(), need_evm_seed()
    mine_solana, mine_evm = b58(pubkey(solana_seed)), evm_address(evm_seed)
    order_id, minimum, expected = relay_order_check(
        order, direction, amount_usd, mine_evm, mine_solana
    )
    if direction == "to-perps":
        message = solana_transfer_message(payload, mine_solana, amount_usd, order_id)
        return (
            {
                "kind": "relay_solana_transaction",
                "signed_transaction": wire_transaction(solana_seed, message),
            },
            mine_solana,
            minimum,
            expected,
        )
    authorize, send_asset = transfer_out_digests(
        payload, amount_usd, mine_evm, order_id
    )
    send = payload["send_asset"]
    return (
        {
            "kind": "relay_sendasset_out",
            "authorize_signature": sig_hex(ecdsa_sign(evm_seed, authorize)),
            "send_asset": {
                "action": send["action"],
                "signature": sig_hex(ecdsa_sign(evm_seed, send_asset)),
                "nonce": send["nonce"],
            },
        },
        mine_evm,
        minimum,
        expected,
    )


def _dollars(amount):
    """A dollar amount without trailing zeros: 24.50 reads 24.5."""
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _signing_address(direction):
    if direction == "to-perps":
        return b58(pubkey(need_solana_seed()))
    return evm_address(need_evm_seed())


def _quote_and_sign(state):
    """Ask for the quote on the stored key, check it, sign it, and record the submission."""
    direction, amount = state["direction"], state["amount_usd"]
    tries = 0
    while True:
        tries += 1
        result, refusal = mcp_call(
            TOOL_QUOTE,
            {
                "proposal": {"direction": _DIRECTIONS[direction], "amount_usd": amount},
                "idempotency_key": state["quote_key"],
            },
        )
        if (
            refusal is None
            or refusal.get("error") != "quote_unavailable"
            or tries >= MAX_PREPARES
        ):
            break
        wait = _positive(refusal.get("retry_after_seconds"), QUOTE_WAIT_SECONDS)
        if wait > MAX_QUOTE_WAIT_SECONDS:
            break
        print(
            "foom could not quote the transfer (try %d of %d); asking again in %g s."
            % (tries, MAX_PREPARES, wait)
        )
        time.sleep(wait)
    if refusal is not None:
        # Nothing was quoted, so nothing is in flight: the next run starts afresh.
        clear_state("transfer", "current")
        raise SystemExit(
            "ERROR: "
            + refusal_text(refusal, "quote the transfer")
            + " Nothing was signed."
        )
    quote = need(result, "quote", TOOL_QUOTE)
    request_id, payload = need(quote, "request_id", TOOL_QUOTE), need(
        quote, "payload", TOOL_QUOTE
    )
    try:
        payload = _object(payload, "the transfer payload")
        if direction == "to-perps":
            # foom's quote says which Relay request it is for and where the money
            # lands: the request is the payload's, the wallet is the key on this machine.
            if _relay_request_id(quote.get("quote_id")) != _relay_request_id(
                payload.get("relay_request_id")
            ):
                _refuse("the quote names another Relay request than its payload")
            mine = evm_address(need_evm_seed())
            if not same_address(quote.get("destination_address"), mine):
                _refuse(
                    "foom's quote sends the money to %r, not to your Ethereum trading "
                    "wallet %s" % (quote.get("destination_address"), mine)
                )
        order = quote.get("relay_order")
        if not isinstance(order, dict):
            raise SystemExit(
                "ERROR: foom's quote came without Relay's order, so the recipe cannot check "
                "who this transfer pays. Nothing was signed. Run the transfer again for a "
                "new quote."
            )
        submission, address, minimum, expected = _sign_transfer(
            direction, amount, payload, order
        )
    except SystemExit:
        # Refused before signing: nothing to resume.
        clear_state("transfer", "current")
        raise
    paid_to = (
        evm_address(need_evm_seed())
        if direction == "to-perps"
        else b58(pubkey(need_solana_seed()))
    )
    print(
        "Relay's order pays your own wallet %s: at least $%s, expected $%s."
        % (paid_to, _dollars(minimum), _dollars(expected))
    )
    state.update(
        request_id=request_id,
        expires_at=quote.get("expires_at"),
        submission=submission,
        address=address,
    )
    save_state("transfer", "current", state)
    return state


def _transfer_outcome(state, read):
    """Act on what foom reports for the transfer. Returns an exit code, or None to keep reading."""
    transfer = need(read, "transfer", TOOL_READ)
    status = need(transfer, "status", TOOL_READ)
    next_step = read.get("next_step") if isinstance(read.get("next_step"), str) else ""
    label = "The transfer of $%s (%s, %s)" % (
        state["amount_usd"],
        state["direction"],
        state["request_id"],
    )
    if status == "settled":
        clear_state("transfer", "current")
        print(
            "%s is settled: the money is in %s." % (label, _BALANCE[state["direction"]])
        )
        return 0
    if status in ("refunded", "failed"):
        clear_state("transfer", "current")
        raise SystemExit("ERROR: %s ended as %s. %s" % (label, status, next_step))
    if status == "manual_review":
        state["review"] = True
        save_state("transfer", "current", state)
        raise SystemExit(
            "ERROR: %s is in manual review. %s No new transfer is started until you run "
            "`python3 scripts/foom_perps.py transfer forget %s`, once the review is over."
            % (label, next_step, state["request_id"])
        )
    if status not in TRANSFER_STATUSES:
        raise SystemExit(
            "ERROR: %s has status %r, which this recipe does not know."
            % (label, status)
        )
    return None


def _kept(state):
    """What to do after a stop that keeps the signed transfer."""
    return (
        " The signed transfer %s is kept: run `python3 scripts/foom_perps.py transfer %s %s` "
        "again. It reads the transfer first and sends it only if foom has not taken it "
        "(nothing is signed twice)."
        % (state["request_id"], state["direction"], state["amount_usd"])
    )


def _read_transfer(state, refused=None):
    """What foom reports for the transfer. A refused read stops, keeping the transfer."""
    read, refusal = mcp_call(TOOL_READ, {"id": state["request_id"]})
    if refusal is not None:
        text = "ERROR: "
        if refused is not None:
            text += refusal_text(refused, "run the transfer") + " Then "
        raise SystemExit(
            text + refusal_text(refusal, "show the transfer") + _kept(state)
        )
    need(need(read, "transfer", TOOL_READ), "status", TOOL_READ)
    return read


def _send_transfer(state, read):
    """Send the stored submission while foom still holds it as quoted. Returns the read
    the wait starts from: None after a send foom took, else what foom reported."""
    if read is not None and read["transfer"]["status"] != "quoted":
        return read  # foom took it already: nothing is sent twice
    try:
        executed, refusal = mcp_call(
            TOOL_MOVE,
            {
                "proposal": {
                    "request_id": state["request_id"],
                    "submission": state["submission"],
                },
                "idempotency_key": state["execute_key"],
            },
        )
    except SystemExit as stop:
        raise SystemExit("%s%s" % (stop, _kept(state)))
    if refusal is None:
        return None
    # A refusal is not the outcome: foom may have taken the transfer earlier.
    read = _read_transfer(state, refused=refusal)
    if read["transfer"]["status"] != "quoted":
        return read
    if refusal.get("error") == "quote_expired":
        clear_state("transfer", "current")
        raise SystemExit(
            "ERROR: the quote expired before foom took the transfer. Nothing moved. "
            "Run the transfer again for a new quote."
        )
    raise SystemExit(
        "ERROR: " + refusal_text(refusal, "run the transfer") + _kept(state)
    )


def _finish_transfer(state, fresh=False):
    """See the transfer through: a transfer signed in an earlier run is read before it is
    sent, and it is sent only while foom still holds it as quoted."""
    read = None if fresh else _read_transfer(state)
    read = _send_transfer(state, read)
    deadline = time.time() + MAX_WAIT_SECONDS
    while True:
        if read is None:
            read = _read_transfer(state)
        outcome = _transfer_outcome(state, read)
        if outcome is not None:
            return outcome
        status = read["transfer"]["status"]
        if time.time() >= deadline:
            break
        time.sleep(POLL_SECONDS)
        read = None
    print(
        "The transfer %s is still %s after %d s. Run this again later to read the result "
        "(nothing is signed twice)." % (state["request_id"], status, MAX_WAIT_SECONDS)
    )
    return 0


def _stored_transfer():
    """The unfinished transfer, or None. A file that cannot be read stops everything."""
    path = state_path("transfer", "current")
    if not os.path.exists(path):
        return None
    state = load_state("transfer", "current")
    known = (
        state is not None
        and state.get("direction") in _DIRECTIONS
        and all(
            isinstance(state.get(key), str) and state.get(key)
            for key in ("amount_usd", "quote_key", "execute_key")
        )
        and (
            "submission" not in state
            or (
                isinstance(state["submission"], dict)
                and all(
                    isinstance(state.get(key), str) and state.get(key)
                    for key in ("request_id", "address")
                )
            )
        )
    )
    if not known:
        raise SystemExit(
            "ERROR: %s exists but cannot be read, so this recipe does not know whether a transfer "
            "is in flight. Nothing was asked of foom. Check your balances with get_positions and "
            "get_portfolio; when you are sure no transfer is pending, delete that file and run "
            "this again." % path
        )
    return state


@contextlib.contextmanager
def _transfer_lock():
    # Keep the inode in place: unlinking a lock file would let another process
    # lock a different inode while this one still holds the original lock.
    if fcntl is None:
        yield
        return
    path = state_path("transfer", "current") + ".lock"
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                "ERROR: another transfer invocation is running. Nothing was signed or sent. "
                "Wait for it to finish before running this again."
            )
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def transfer(arguments):
    with _transfer_lock():
        return _transfer_locked(arguments)


def _transfer_locked(arguments):
    if (
        len(arguments) != 2
        or arguments[0] not in ("to-perps", "to-cash", "forget")
        or not arguments[1]
    ):
        raise SystemExit(TRANSFER_USAGE)
    action, value = arguments
    state = _stored_transfer()
    if action == "forget":
        if state is None or state.get("request_id") != value:
            raise SystemExit(
                "ERROR: no transfer %s is kept here. Nothing was changed." % value
            )
        clear_state("transfer", "current")
        print("Forgot transfer %s. A new transfer can start." % value)
        return 0
    need_deva_key()
    if (
        state is not None
        and not state.get("submission")
        and (state["direction"], state["amount_usd"]) != (action, value)
    ):
        # The earlier command stopped before anything was signed, so nothing of
        # it is in flight: this command takes its place.
        state = None
    if state is None:
        amount = _plain_decimal(value)
        if (
            amount is None
            or amount <= 0
            or -amount.as_tuple().exponent > SOLANA_USDC_DECIMALS
        ):
            raise SystemExit(TRANSFER_USAGE)
        # Both keys must be here before anything is asked of foom: one signs, and
        # the other is the wallet Relay's order has to pay.
        need_evm_seed()
        need_solana_seed()
        state = {
            "direction": action,
            "amount_usd": value,
            "quote_key": uuid.uuid4().hex,
            "execute_key": uuid.uuid4().hex,
        }
        # Written before the quote is asked for, so a lost answer is asked again on the same key.
        save_state("transfer", "current", state)
    else:
        if state.get("review"):
            raise SystemExit(
                "ERROR: transfer %s is in manual review at foom. No new transfer is started until "
                "you run `python3 scripts/foom_perps.py transfer forget %s`, once the review is over."
                % (state.get("request_id"), state.get("request_id"))
            )
        print(
            "An earlier transfer of $%s (%s) is unfinished: finishing it. Nothing new is started."
            % (state["amount_usd"], state["direction"])
        )
    fresh = not state.get("submission")
    if fresh:
        state = _quote_and_sign(state)
    elif state.get("address") != _signing_address(state["direction"]):
        raise SystemExit(
            "ERROR: the transfer kept here was signed by another key (%s) than the one on disk "
            "now. Nothing was sent. Put that key back and run this again."
            % state.get("address")
        )
    return _finish_transfer(state, fresh=fresh)


# --- the command line --------------------------------------------------------------


def _refuse_key_arguments(argv):
    for arg in argv:
        if arg in ("--key", "--seed", "--private-key") or arg.startswith(
            ("--key=", "--seed=", "--private-key=")
        ):
            raise SystemExit(
                "ERROR: this recipe never takes a key as an argument. The Ethereum key lives in "
                "%s (hex, chmod 600); `keygen` creates it." % evm_key_path()
            )


def main(argv):
    global SOLANA_KEY_FILE_OVERRIDE
    if argv[:1] == ["--solana-key-file"]:
        if len(argv) < 3:
            raise SystemExit(
                "usage: foom_perps.py --solana-key-file <path> <command> ..."
            )
        SOLANA_KEY_FILE_OVERRIDE = argv[1]
        argv = argv[2:]
    # The guard also sees the option's value, so a flag cannot hide there.
    _refuse_key_arguments(
        ([SOLANA_KEY_FILE_OVERRIDE] if SOLANA_KEY_FILE_OVERRIDE else []) + argv
    )
    if not argv:
        print(__doc__)
        return 1
    command, rest = argv[0], argv[1:]
    if command == "keygen":
        return keygen()
    if command == "sign-challenge":
        if len(rest) != 2 or not all(rest) or any(":" in part for part in rest):
            raise SystemExit(
                "usage: foom_perps.py sign-challenge <persona id> <challenge>  (the two values "
                "the challenge call returned; the recipe fills the template with its own "
                "address and never signs a free-form string)"
            )
        seed = need_evm_seed()
        message = TRADING_WALLET_TEMPLATE.format(
            persona_id=rest[0], address=evm_address(seed), challenge=rest[1]
        )
        print("Signed message: %s" % message)
        print(sig_hex(ecdsa_sign(seed, eip191_digest(message))))
        return 0
    if command == "execute":
        if len(rest) != 1 or not rest[0]:
            raise SystemExit("usage: foom_perps.py execute <proposal id>")
        return execute(rest[0])
    if command == "transfer":
        return transfer(rest)
    if command == "approve-builder-fee":
        if rest:
            raise SystemExit("usage: foom_perps.py approve-builder-fee")
        return approve_builder_fee()
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
