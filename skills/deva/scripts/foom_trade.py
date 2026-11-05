"""Sign and execute a FOOM trade with your own trading key.

Ships beside SKILL.md in the Deva skill (scripts/foom_trade.py). Python 3
standard library only. Your trading key never leaves this machine and is
never read from an argument.

    python3 scripts/foom_trade.py keygen                   # create ~/.deva/trading.key, print the address
    python3 scripts/foom_trade.py sign-challenge <persona id> <challenge>   # wallet-challenge signature
    python3 scripts/foom_trade.py sign --message "<b64>"   # signed transaction for a prepared message
    python3 scripts/foom_trade.py execute <proposal id>    # prepare -> check -> sign -> execute -> wait

`--key-file <path>` before the command uses another seed file (for example
your payout key). `execute` talks to the FOOM connector over MCP with your Deva key, keeps a
state file per proposal so a re-run never signs the same proposal twice, and
only ever signs a transaction whose single signer and fee payer is your own
trading key.
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

MCP = "https://foomfamily.ai/mcp"
TOOL_GET = "get_proposal"
TOOL_PREPARE = "prepare_trade_proposal"
TOOL_EXECUTE = "execute_trade_proposal"
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
# A swap moves tokens; it never delegates them or hands a token account to
# someone else. A top-level Approve, ApproveChecked or SetAuthority from either
# token program is refused before signing, whatever else the message carries.
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # pragma: allowlist secret
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # pragma: allowlist secret
)
TOKEN_APPROVE, TOKEN_SET_AUTHORITY, TOKEN_APPROVE_CHECKED = 4, 6, 13
# A trade never changes who owns your wallet account either: a System program
# Assign / AssignWithSeed that names your key is refused.
SYSTEM_PROGRAM = "11111111111111111111111111111111"
SYSTEM_ASSIGN, SYSTEM_ASSIGN_WITH_SEED = 1, 10
# The trading-wallet challenge template, filled with the persona id, this
# key's address and the challenge; the recipe never signs a free-form string.
TRADING_WALLET_TEMPLATE = "deva-trading-wallet-v1:{persona_id}:{address}:{challenge}"
KEY_FILE_OVERRIDE = None
POLL_SECONDS = float(os.environ.get("FOOM_TRADE_POLL_SECONDS", "3"))
MAX_WAIT_SECONDS = 180

# The dashboard substitutes the placeholder below; when you copied this from
# the skill it is still a placeholder, so the key is read from your environment.
KEY = "$DEVA_API_KEY"
if KEY.startswith("$") or KEY.startswith("YOUR_"):
    KEY = os.environ.get("DEVA_API_KEY", "")

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
            "ERROR: no trading key at %s. Run `python3 scripts/foom_trade.py keygen` "
            "first, then register the printed address as your trading wallet."
            % KEY_PATH
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
                % KEY_PATH
            )
        os.makedirs(os.path.dirname(key_path()), mode=0o700, exist_ok=True)
        seed = os.urandom(32)
        fd = os.open(key_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(seed.hex() + "\n")
        print(
            "Created %s (keep it chmod 600, back it up, never print or commit it)."
            % KEY_PATH
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


def prepare_and_sign(proposal_id, seed, mine):
    """Ask FOOM for the bytes, check them, sign, and record the state before anything is sent."""
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
    check_message(message, mine)
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


def execute(proposal_id):
    seed = need_seed()
    mine = b58(pubkey(seed))
    if not KEY:
        raise SystemExit("ERROR: no Deva API key. Set DEVA_API_KEY and run again.")
    proposal, refusal = mcp_call(TOOL_GET, {"id": proposal_id})
    if refusal is not None:
        raise SystemExit(
            "ERROR: FOOM refused to show proposal %s: %s"
            % (proposal_id, refusal.get("error"))
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

    state = load_state(proposal_id)
    if status == "approved" and state is None:
        raise SystemExit(
            "ERROR: proposal %s was already prepared, but not by this recipe on this machine "
            "(no state file). Signing it again could mean a second trade. Wait for it to settle, "
            "or pause the account and tell your human." % proposal_id
        )
    retried = False
    if status == "pending" or state is None:
        state, refusal = prepare_and_sign(proposal_id, seed, mine)
        state, refusal = _retry_prepare(proposal_id, seed, mine, state, refusal)
        if refusal is not None:
            return _stop_on(refusal, proposal_id)
        retried = state.get("_retried", False)
    else:
        print(
            "Resuming proposal %s with the transaction signed earlier; nothing is signed twice."
            % proposal_id
        )

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
        if (
            code in ("preparation_expired", "invalid_approval", "preparation_pending")
            and not retried
        ):
            retried = True
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
            state, refusal = prepare_and_sign(proposal_id, seed, mine)
            state, refusal = _retry_prepare(
                proposal_id, seed, mine, state, refusal, allow=False
            )
            if refusal is not None:
                return _stop_on(refusal, proposal_id)
            continue
        if code == "approval_used":
            print(
                "FOOM says this approval was already used by other bytes, so these were not "
                "sent; reading the recorded status."
            )
            break
        return _stop_on(refusal, proposal_id)

    status = need(executed, "status", TOOL_EXECUTE) if refusal is None else "pending"
    print("Sent. FOOM reports %s; waiting for it to settle." % status)
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
        "Proposal %s is still %s after %d s. The transaction was sent; run this again later to "
        "read the result (nothing will be signed twice)."
        % (proposal_id, status, MAX_WAIT_SECONDS)
    )
    return 0


def _retry_prepare(proposal_id, seed, mine, state, refusal, allow=True):
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
    state, refusal = prepare_and_sign(proposal_id, seed, mine)
    if state is not None:
        state["_retried"] = True
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
    if code in ("preparation_expired", "invalid_approval", "preparation_pending"):
        raise SystemExit(
            "ERROR: FOOM answered %s twice for proposal %s; the signing window kept closing. "
            "Nothing landed. Run this again, or pause the account and tell your human. %s"
            % (code, proposal_id, message)
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
                "(hex, chmod 600) and run again." % KEY_PATH
            )


def main(argv):
    global KEY_FILE_OVERRIDE
    _refuse_key_arguments(argv)
    if argv[:1] == ["--key-file"]:
        if len(argv) < 3:
            raise SystemExit("usage: foom_trade.py --key-file <path> <command> ...")
        KEY_FILE_OVERRIDE = argv[1]
        argv = argv[2:]
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
                "usage: foom_trade.py sign-challenge <persona id> <challenge>  (the two values "
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
        if rest[:1] == ["--message"] and len(rest) == 2:
            message_b64 = rest[1]
        elif not rest:
            message_b64 = sys.stdin.read().strip()
        else:
            raise SystemExit(
                "usage: foom_trade.py sign [--message <base64>]  (or the message on stdin)"
            )
        try:
            message = base64.b64decode(message_b64, validate=True)
        except (ValueError, TypeError):
            raise SystemExit("ERROR: the message is not base64.")
        check_message(message, mine)
        print(wire_transaction(seed, message))
        return 0
    if command == "execute":
        if len(rest) != 1 or not rest[0]:
            raise SystemExit("usage: foom_trade.py execute <proposal id>")
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
