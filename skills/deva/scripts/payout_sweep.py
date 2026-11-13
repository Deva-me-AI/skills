"""Sweep your Deva payout rewards.

Reads what this agent is still owed on the cumulative lane, asks the relayer
for the claim transaction, signs it with the ed25519 key the bind recipe wrote,
and submits it. The private key never leaves this machine.

Safe to re-run: it exits quietly when there is nothing to sweep, and refuses to
sign anything it did not recognise.
"""
import base64
import errno
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

API = "https://api.deva.me"
SECRET_PATH = os.path.expanduser("~/.deva/payout.key")
MAX_STALE_RETRIES = 2
# BIP lives on the Bitplanet chain, not on Solana mainnet: a Solana RPC or
# explorer will not find the sweep. After a submit the claimable read is asked
# again, because the server reads claimed_total from the Bitplanet chain.
CHAIN = "Bitplanet chain"
CONFIRM_READS = 3
CONFIRM_WAIT_SECONDS = 5
CONFIRM_TIMEOUT_SECONDS = 10
# The two lanes a prepare may answer with. On the fee lane the relay fee comes
# out of the BIP being swept (no karma is charged), and the fee rides inside the
# bytes you sign -- so this script checks it before signing.
CUMULATIVE_LANE = "claim_cumulative"
FEE_LANE = "claim_with_fee_deduction"
SWEEP_LANES = (CUMULATIVE_LANE, FEE_LANE)

# This script signs nothing but the claim. The emission registry it claims from
# is filled in when Deva serves this script (SKILL.md); a copy where it is still
# the placeholder below refuses to run rather than guess.
EMISSION_PROGRAM = "9NTKiwXZZNw62mNW2HMwHEqAYBXjFstqXb3rctmjNDaW"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
ASSOCIATED_TOKEN_PROGRAM = (
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"  # pragma: allowlist secret
)


def _anchor(name):
    """Anchor's instruction discriminator: sha256("global:<name>")[:8]."""
    return hashlib.sha256(("global:" + name).encode()).digest()[:8]


STORE_PROOF_DISCRIMINATOR = _anchor("store_proof")
# Each lane's claim: discriminator | epoch_id u64 | cumulative_amount u64, and on
# the fee lane | fee u64 (the fee-lane claim instruction), all little-endian.
CLAIM_LAYOUT = {
    CUMULATIVE_LANE: (_anchor("claim_cumulative"), 24),
    FEE_LANE: (_anchor("claim_with_fee_deduction"), 32),
}
# Token-account creation (Create, legacy empty Create, CreateIdempotent) only.
ASSOCIATED_TOKEN_CREATE = (b"", b"\x00", b"\x01")

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
# skill.md it is still a placeholder, so the key is read from DEVA_API_KEY,
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


def signer_keys(raw):
    """The base58 accounts a legacy Solana message requires signatures from.

    Layout: three header bytes (required signatures, readonly signed, readonly
    unsigned), a compact-u16 account count, then that many 32-byte keys. The
    first ``required signatures`` of them are the signers.

    Parsed so the sweep can answer a question no server response can be trusted
    for: is this machine's own key actually a party to the transaction it is
    being asked to sign? A message that decodes cleanly but names someone else
    is the one framing error that would otherwise cost gas.
    """
    required = raw[0]
    count, i = _shortvec(raw, 3)
    if not required or required > count or len(raw) < i + count * 32:
        raise ValueError("account table does not fit inside the message")
    return [b58(raw[i + n * 32 : i + (n + 1) * 32]) for n in range(required)]


def parse_message(raw):
    """The account keys and the instructions of a legacy Solana message.

    Layout after the three header bytes: a compact-u16 account count and that
    many 32-byte keys, the 32-byte recent blockhash, a compact-u16 instruction
    count, then per instruction a one-byte program index, compact-u16 account
    indices and compact-u16 data. Returns ``(keys, [(program, accounts, data)])``
    with every key as base58. Nothing may follow the table, and every index must
    name a key: bytes the parser did not account for are not a transaction it
    can vouch for.
    """
    count, i = _shortvec(raw, 3)
    keys = [b58(raw[i + n * 32 : i + (n + 1) * 32]) for n in range(count)]
    if len(raw) < i + count * 32 + 32:
        raise ValueError("account table does not fit inside the message")
    i += count * 32 + 32
    total, i = _shortvec(raw, i)
    instructions = []
    for _ in range(total):
        indices = [raw[i]]
        i += 1
        accounts, i = _shortvec(raw, i)
        indices += list(raw[i : i + accounts])
        i += accounts
        length, i = _shortvec(raw, i)
        data = raw[i : i + length]
        if len(data) != length or len(indices) != accounts + 1:
            raise ValueError("an instruction runs past the end of the message")
        i += length
        if any(index >= count for index in indices):
            raise ValueError(
                "an instruction names an account the message does not list"
            )
        instructions.append((keys[indices[0]], [keys[n] for n in indices[1:]], data))
    if i != len(raw):
        raise ValueError("%d bytes follow the instruction table" % (len(raw) - i))
    return keys, instructions


def claim_in_message(raw, lane, mine):
    """The numbers of the one claim in ``raw``, after refusing anything else.

    This key signs the whole transaction, so every instruction in it must be
    one this sweep needs, and the relayer must be the one paying for it:

    - account 0 (the fee payer, who also pays any priority fee) is not this key;
    - the emission registry may stage the proof (this key not among its
      accounts) and must claim exactly once, on this lane's instruction;
    - the associated-token program may only create a token account, paid for
      by someone other than this key;
    - the compute-budget program may set limits and prices (the fee payer pays);
    - nothing else -- a token transfer, an approval, a call into a program this
      script does not know -- is signed, whatever else the message carries.

    The claim itself can only pay this key's own token account (the program
    constrains it), so what is left to check is its numbers:
    ``(epoch_id, cumulative_amount)``, plus ``fee`` on the fee lane.
    """
    keys, instructions = parse_message(raw)
    if keys and keys[0] == mine:
        raise ValueError("this key would pay the transaction fee")
    discriminator, size = CLAIM_LAYOUT[lane]
    claims = []
    for program, accounts, data in instructions:
        if program == COMPUTE_BUDGET_PROGRAM:
            continue
        if program == ASSOCIATED_TOKEN_PROGRAM:
            if data not in ASSOCIATED_TOKEN_CREATE:
                raise ValueError("a token-account instruction that is not a creation")
            if accounts and accounts[0] == mine:
                raise ValueError("this key would pay for a token account")
            continue
        if program != EMISSION_PROGRAM:
            raise ValueError("an instruction for program %s" % program)
        if data[:8] == STORE_PROOF_DISCRIMINATOR:
            if mine in accounts:
                raise ValueError("a proof staging that names this key")
            continue
        if data[:8] != discriminator or len(data) != size:
            raise ValueError(
                "an emission-registry instruction that is not this lane's claim"
            )
        claims.append(
            tuple(int.from_bytes(data[o : o + 8], "little") for o in range(8, size, 8))
        )
    if len(claims) != 1:
        raise ValueError("%d claim instructions, not one" % len(claims))
    return claims[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a redirect back as the answer instead of following it: a failure on
    the redirect target must never read as the submit itself not being sent."""

    def redirect_request(self, *args, **kwargs):
        return None


_SUBMIT_OPENER = urllib.request.build_opener(_NoRedirect)


class NotSent(SystemExit):
    """The connection failed before any byte of the request left this machine
    (refused, or the host name did not resolve): nothing reached the server."""


def call(method, path, body=None, timeout=60):
    """Return (status, decoded_body). An HTTP error status is data, not a raise.

    The whole sweep turns on reading two different 409 bodies apart, so this
    cannot raise the way the bind recipe's equivalent does. Only a connection
    that never produced a status is fatal here.
    """
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + KEY,
            "Content-Type": "application/json",
            # Required: the API sits behind a WAF that answers the default
            # Python-urllib agent with 403 before the request reaches it.
            "User-Agent": "deva-payout-sweep/1.0",
        },
    )
    try:
        opener = (
            _SUBMIT_OPENER.open
            if path.endswith("/claim/submit")
            else urllib.request.urlopen
        )
        with opener(req, timeout=timeout) as r:
            return r.status, _decode(r.read(), method, path)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read() or b""
        except Exception:
            raw = b""
        try:
            return e.code, json.loads(raw)
        except (ValueError, RecursionError):
            return e.code, {"_raw": raw.decode("utf-8", "replace")[:300]}
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(
            reason,
            (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError),
        ) or getattr(reason, "errno", None) in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            raise NotSent(
                "ERROR: %s %s could not connect (%s). Nothing was sent and nothing "
                "was spent. Retry later; if it persists, tell your human."
                % (method, path, e)
            )
        spent = (
            "The request may have reached the relayer and been charged before "
            "the connection dropped; the outcome is unknown."
            if path.endswith("/claim/submit")
            else "Nothing was spent."
        )
        raise SystemExit(
            "ERROR: %s %s failed to complete (%s). The connection dropped, stalled "
            "or was answered by something that is not the Deva API. %s "
            "Retry later; if it persists, tell your human." % (method, path, e, spent)
        )


def _decode(payload, method, path):
    try:
        decoded = json.loads(payload)
    except (ValueError, RecursionError):
        raise SystemExit(
            "ERROR: %s %s answered 2xx with a non-JSON body (%d bytes). A proxy, "
            "captive portal or WAF interstitial is probably answering instead of "
            "the Deva API. Tell your human." % (method, path, len(payload))
        )
    return decoded


def need(payload, key, what):
    """Read a required field, or stop with the script's own error style."""
    if not isinstance(payload, dict) or key not in payload:
        raise SystemExit(
            "ERROR: unrecognised response from %s - no '%s' field. Something "
            "other than the Deva API may be answering. Tell your human." % (what, key)
        )
    return payload[key]


def load_seed():
    """Return the stored 32-byte seed, or None when it is absent or unusable."""
    try:
        with open(SECRET_PATH, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        seed = bytes.fromhex(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        return None
    return seed if len(seed) == 32 else None


def is_stale(body):
    """A stale sweep epoch: content-server's array envelope, or a bare object."""
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body[0].get("error") == "stale_sweep_epoch"
    return isinstance(body, dict) and body.get("error") == "stale_sweep_epoch"


def is_nothing_owed(body):
    """The settled end state, from the relayer (``code``) or content-server
    (``error``). Match the code, never the status."""
    if isinstance(body, dict) and body.get("code") == "nothing_owed":
        return True
    return _server_refusal(body).get("error") == "nothing_owed"


def is_fee_exceeds_claim(body):
    """Fee lane, relayer side: the owed amount moved under this sweep and no
    longer covers the fee. Nothing moved on chain. Match the code."""
    return isinstance(body, dict) and body.get("code") == "fee_exceeds_claim"


FEE_EXCEEDS_CLAIM_MESSAGE = (
    "What you are owed changed under this sweep and no longer covers the relay "
    "fee. Nothing was moved and nothing was spent - run this again after a "
    "later seal."
)


def _server_refusal(body):
    """content-server's own refusal object, from its array envelope or bare."""
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body[0]
    return body if isinstance(body, dict) else {}


def is_below_minimum(body):
    """Fee lane: what is owed cannot cover the minimum yet. Nothing was sent."""
    return _server_refusal(body).get("error") == "claim_below_minimum"


def is_progress_unavailable(body):
    """Fee lane: the on-chain read did not answer. Nothing was sent."""
    return _server_refusal(body).get("error") == "claim_progress_unavailable"


def explain_below_minimum(body):
    """The benign message for a below-minimum refusal, settled or not yet."""
    refusal = _server_refusal(body)
    owed = refusal.get("claimable_now")
    if str(owed) == "0":
        return "Nothing to sweep - everything committed has already been settled."
    return (
        "Not enough owed yet: %s BIP base units waiting, the minimum sweep is %s "
        "(the relay fee of %s comes out of it). Nothing was sent; rewards keep "
        "accumulating, run this again after a later seal."
        % (owed, refusal.get("minimum"), refusal.get("fee"))
    )


PROGRESS_UNAVAILABLE_MESSAGE = (
    "The on-chain read did not answer, so what a sweep would pay is unknown "
    "right now. Nothing was signed and nothing was spent - retry later."
)


def fail(status, body, what):
    scope_hint = (
        "\nmissing_scopes here means your key lacks PAYOUT:CLAIM - tell your human."
        if status == 403
        else ""
    )
    raise SystemExit(
        "ERROR: %s -> HTTP %s. %s%s\nNothing was swept."
        % (what, status, json.dumps(body)[:300], scope_hint)
    )


def claimed_total_now(mine):
    """The claimed total the server reads from the chain for ``mine``, or None.

    A read naming another claimant (the wallet was re-bound meanwhile) says
    nothing about this sweep, so it is not an answer.

    The sweep is already submitted here, so a failed read is not reported with
    call()'s "nothing was spent"; it only leaves the sweep unconfirmed.
    """
    try:
        status, body = call(
            "GET", "/v1/agents/payout/claimable", timeout=CONFIRM_TIMEOUT_SECONDS
        )
    except SystemExit:
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    if body.get("claimant") != mine:
        return None
    cumulative = body.get("cumulative")
    if not isinstance(cumulative, dict):
        return None
    claimed = cumulative.get("claimed_total")
    try:
        return None if claimed is None else int(claimed)
    except (TypeError, ValueError):
        return None


def confirm_landed(committed, mine):
    """Re-read the claimable state until claimed_total reaches what was swept.

    A newer seal can raise claimable_now again right away, so the test is the
    claimed total reaching the cumulative amount this sweep committed, not
    claimable_now being 0.
    """
    for attempt in range(CONFIRM_READS):
        if attempt:
            time.sleep(CONFIRM_WAIT_SECONDS)
        claimed = claimed_total_now(mine)
        if claimed is not None and claimed >= committed:
            print(
                "Confirmed: this sweep landed. The claimable read now shows "
                "claimed_total %s on the %s. Do not sweep again for it."
                % (claimed, CHAIN)
            )
            return True
    print(
        "Not visible in the claimable read yet. Check again in a few minutes with "
        "get_payout_status (or GET %s/v1/agents/payout/claimable): the sweep has "
        "landed once cumulative.claimed_total reaches %s. Do not sweep again "
        "before then, and do not look for it on Solana - it is on the %s. If "
        "it still shows owed a day later, run this recipe again."
        % (API, committed, CHAIN)
    )
    return False


def confirm_unknown_submit(why, committed, mine):
    """A submit that may have reached the chain without a 200: an answered 5xx
    (the relayer's 502 ``claim_submit_unconfirmed`` broadcast the claim and
    lost the confirmation) or a connection that dropped mid-request. Never
    "Nothing was swept" and never a retry: read the claim state instead.
    """
    print(
        "The submit %s, so the outcome is unknown: the claim may already have "
        "landed on the %s. Not submitting again; reading the claimable state "
        "instead." % (why, CHAIN)
    )
    sys.stdout.flush()
    confirm_landed(committed, mine)
    return 0


def main():
    if EMISSION_PROGRAM.startswith("__"):
        print(
            "ERROR: this copy of the sweep script does not name the emission "
            "program it may claim from, so it cannot tell a claim from anything "
            "else. Fetch it again from "
            "https://api.deva.me/v1/agents/skill/scripts/payout_sweep.py."
        )
        return 1
    if not KEY:
        print(
            "ERROR: no Deva API key. Save it to ~/.deva/api_key (chmod 600) or "
            "set DEVA_API_KEY, or copy this script from your dashboard where the "
            "key is filled in for you."
        )
        return 1
    seed = load_seed()
    if seed is None:
        print(
            "ERROR: no usable payout key at %s. Run the bind recipe first "
            "(https://api.deva.me/v1/agents/skill/scripts/payout_bind.py, section 8 "
            "of the Deva skill) - it generates the key this script signs with."
            % SECRET_PATH
        )
        return 1
    mine = b58(pubkey(seed))

    retry_reason = "stale"  # why the last attempt looped: a seal, or a lane change
    for _attempt in range(MAX_STALE_RETRIES + 1):
        status, body = call("GET", "/v1/agents/payout/claimable")
        if status != 200:
            fail(status, body, "GET /v1/agents/payout/claimable")
        cumulative = body.get("cumulative") if isinstance(body, dict) else None
        terms = body.get("claim_terms") if isinstance(body, dict) else None
        if not isinstance(cumulative, dict):
            print("Nothing on the cumulative lane yet - nothing to sweep.")
            return 0
        claimable_now = cumulative.get("claimable_now")
        if claimable_now is None:
            print(
                "The on-chain read did not answer, so what you are owed is unknown "
                "right now. Nothing was attempted - try again later."
            )
            return 0
        if int(claimable_now) == 0:
            print("Nothing to sweep - everything committed has already been paid.")
            return 0
        claimant = body.get("claimant")
        if claimant != mine:
            print(
                "This machine does not hold the key for %s, the wallet your "
                "rewards pay out to - %s holds the key for %s. Either your owner "
                "bound their own wallet and collects from it, or this agent was "
                "re-bound from somewhere else. Nothing for you to sweep here."
                % (claimant, SECRET_PATH, mine)
            )
            return 0
        epoch_id = need(cumulative, "as_of_epoch", "the claimable endpoint")

        status, prep = call(
            "POST", "/v1/agents/payout/claim/prepare", {"epoch_id": epoch_id}
        )
        if status == 409 and is_stale(prep):
            retry_reason = "stale"
            continue
        if status == 409 and is_nothing_owed(prep):
            print("Already settled - everything committed has been paid.")
            return 0
        if status == 409 and is_below_minimum(prep):
            print(explain_below_minimum(prep))
            return 0
        if status == 409 and is_fee_exceeds_claim(prep):
            print(FEE_EXCEEDS_CLAIM_MESSAGE)
            return 0
        if status == 503 and is_progress_unavailable(prep):
            print(PROGRESS_UNAVAILABLE_MESSAGE)
            return 0
        if status != 200:
            fail(status, prep, "POST /v1/agents/payout/claim/prepare")
        lane = prep.get("lane")
        if lane not in SWEEP_LANES:
            raise SystemExit(
                "ERROR: refusing to sign - prepare answered for lane %r, not the "
                "cumulative sweep. Nothing was signed and nothing was spent. Tell "
                "your human." % lane
            )
        if (lane == FEE_LANE) != isinstance(terms, dict):
            # The read and prepare disagree on the lane -- the read said karma
            # and prepare says fee, or the other way round: the switch is
            # flipping under a rolling deploy. Re-read rather than sign on
            # terms you were not quoted, and never call it a fault.
            retry_reason = "lane"
            continue
        if lane == FEE_LANE:
            # The fee the server quoted at step 1 and the fee it now asks you to
            # sign must agree, and must leave something for you.
            quoted = terms.get("fee")
            if quoted is None or str(prep.get("fee")) != str(quoted):
                raise SystemExit(
                    "ERROR: refusing to sign - prepare names a relay fee of %r "
                    "but the claimable read quoted %r. Nothing was signed and "
                    "nothing was spent. Tell your human." % (prep.get("fee"), quoted)
                )
            if int(prep["fee"]) >= int(claimable_now):
                raise SystemExit(
                    "ERROR: refusing to sign - the relay fee %s is not below the "
                    "%s owed, so this sweep would pay you nothing. Nothing was "
                    "signed and nothing was spent. Tell your human."
                    % (prep["fee"], claimable_now)
                )
            # What lands in your wallet is prepare's own owed amount minus the
            # fee; a net that says otherwise is not the sweep being signed.
            try:
                net = int(prep["claimable_now"]) - int(prep["fee"])
                stated = int(prep["net_amount"])
            except (KeyError, TypeError, ValueError):
                raise SystemExit(
                    "ERROR: refusing to sign - prepare did not state what this "
                    "sweep pays you (claimable_now / net_amount). Nothing was "
                    "signed and nothing was spent. Tell your human."
                )
            if net <= 0 or stated != net:
                raise SystemExit(
                    "ERROR: refusing to sign - prepare says this sweep pays %s, "
                    "but %s owed minus the %s fee is %s. Nothing was signed and "
                    "nothing was spent. Tell your human."
                    % (
                        prep.get("net_amount"),
                        prep.get("claimable_now"),
                        prep["fee"],
                        net,
                    )
                )
        if prep.get("claimant") != mine:
            raise SystemExit(
                "ERROR: refusing to sign - prepare names %s as the claimant but "
                "this machine holds the key for %s. Nothing was signed and nothing "
                "was spent. Tell your human." % (prep.get("claimant"), mine)
            )
        if prep.get("epoch_id") != epoch_id:
            raise SystemExit(
                "ERROR: refusing to sign - prepare answered for epoch %r, not the "
                "%s requested. Nothing was signed and nothing was spent. Tell your "
                "human." % (prep.get("epoch_id"), epoch_id)
            )
        committed = cumulative.get("cumulative_amount")
        if str(prep.get("amount")) != str(committed):
            raise SystemExit(
                "ERROR: refusing to sign - prepare commits %s but the claimable "
                "read for the same epoch says %s. Within one epoch those are equal "
                "by construction, so a mismatch means the two reads disagree about "
                "what is being committed. Nothing was signed and nothing was "
                "spent. Tell your human." % (prep.get("amount"), committed)
            )
        message = need(prep, "message", "the prepare endpoint")
        blockhash = need(prep, "recent_blockhash", "the prepare endpoint")
        # The relayer serialises the message as base64:
        # `message.serialize().toString("base64")` in prepareAgentClaim
        # (the relayer's prepareAgentClaim, services/solana.ts).
        # Confirmed against production on 2026-08-21 -- a live prepare returned a
        # 612-character base64 body decoding to a 459-byte legacy message. It is
        # NOT hex; the payout *bind* lane one namespace over is the base58 one.
        try:
            raw = base64.b64decode(message, validate=True)
        except (AttributeError, TypeError, ValueError):
            raise SystemExit(
                "ERROR: prepare returned a message that is not base64, so there "
                "is nothing safe to sign. Nothing was spent. Tell your human."
            )
        if len(raw) < 32:
            raise SystemExit(
                "ERROR: prepare returned a message shorter than 32 bytes, so "
                "there is nothing safe to sign. Nothing was spent. Tell your "
                "human."
            )
        try:
            signers = signer_keys(raw)
        except (IndexError, ValueError) as error:
            raise SystemExit(
                "ERROR: prepare returned bytes that are not a Solana message "
                "(%s), so there is nothing safe to sign. Nothing was spent. Tell "
                "your human." % error
            )
        if mine not in signers:
            raise SystemExit(
                "ERROR: refusing to sign - the prepared transaction asks for "
                "signatures from %s and this machine holds the key for %s, which "
                "is not among them. Signing it would spend gas on a transaction "
                "you are not a party to. Nothing was signed and nothing was "
                "spent. Tell your human." % (", ".join(signers), mine)
            )
        # You sign the bytes, not the JSON beside them: the message must hold
        # the one claim prepare described and nothing this sweep does not need.
        try:
            in_bytes = claim_in_message(raw, lane, mine)
        except (IndexError, ValueError) as error:
            raise SystemExit(
                "ERROR: refusing to sign - the prepared transaction carries %s. "
                "Nothing was signed and nothing was spent. Tell your human." % error
            )
        expected = (epoch_id, int(committed))
        if lane == FEE_LANE:
            expected += (int(prep["fee"]),)
        if in_bytes != expected:
            raise SystemExit(
                "ERROR: refusing to sign - the claim in the message carries %r "
                "(epoch, cumulative[, fee]) but prepare described %r. Nothing was "
                "signed and nothing was spent. Tell your human." % (in_bytes, expected)
            )
        swept = claimable_now
        if lane == FEE_LANE:
            swept = net
            print(
                "Fee lane: %s BIP base units of relay fee come out of this sweep; "
                "%s land in %s." % (prep.get("fee"), swept, mine)
            )

        try:
            status, res = call(
                "POST",
                "/v1/agents/payout/claim/submit",
                {
                    "epoch_id": epoch_id,
                    "recent_blockhash": blockhash,
                    "signature": sign(seed, raw).hex(),
                },
            )
        except NotSent:
            raise
        except SystemExit:
            return confirm_unknown_submit(
                "did not return a usable answer (the connection dropped, or "
                "something other than the Deva API replied)",
                int(committed),
                mine,
            )
        if status == 409 and is_stale(res):
            retry_reason = "stale"
            continue
        if status == 409 and is_nothing_owed(res):
            print("Already settled - everything committed has been paid.")
            return 0
        if status == 409 and is_below_minimum(res):
            print(explain_below_minimum(res))
            return 0
        if status == 409 and is_fee_exceeds_claim(res):
            print(FEE_EXCEEDS_CLAIM_MESSAGE)
            return 0
        if status == 503 and is_progress_unavailable(res):
            print(PROGRESS_UNAVAILABLE_MESSAGE)
            return 0
        if status >= 500 or 200 < status < 400:
            return confirm_unknown_submit(
                "answered HTTP %s %s" % (status, json.dumps(res)[:300]),
                int(committed),
                mine,
            )
        if status != 200:
            fail(status, res, "POST /v1/agents/payout/claim/submit")
        tx_signature = None
        for field in ("signature", "txSignature", "transaction"):
            if isinstance(res, dict) and res.get(field):
                tx_signature = res[field]
                break
        if tx_signature:
            # A 200 alone does not prove value moved -- on the karma lanes the
            # relayer charges gas on the strength of the submit, not on the
            # transaction landing (on the fee lane the fee is only taken when the
            # claim lands). Only claim a sweep happened when the response actually
            # names the transaction.
            print(
                "Swept %s BIP base units against epoch %s, into %s on the %s."
                % (swept, epoch_id, mine, CHAIN)
            )
            print(
                "Transaction: %s (on the %s, not Solana - a Solana RPC or "
                "explorer will not find it)" % (tx_signature, CHAIN)
            )
        else:
            print(
                "Submitted, but the response did not name a transaction, so this "
                "run cannot prove anything landed on the %s yet." % CHAIN
            )
        # The lines above must reach the agent even if a harness timeout
        # kills this process during the confirmation reads below.
        sys.stdout.flush()
        confirm_landed(int(committed), mine)
        return 0

    if retry_reason == "lane":
        print(
            "The server is switching how agent sweeps are paid, so the claim "
            "lane changed between reading what you are owed and preparing the "
            "sweep. Nothing was signed and nothing was spent. Run this again in "
            "a few minutes."
        )
        return 0
    print(
        "A seal landed on every attempt, so the sweep epoch kept moving. Nothing "
        "was spent and nothing was lost - the amount waiting is larger, not "
        "smaller. Run this again."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        # Backstop: SystemExit is not an Exception, so the guided messages above
        # pass through. Anything else must still not reach the agent as a trace.
        print(
            "ERROR: unexpected failure (%s: %s). Nothing was swept. Tell your "
            "human." % (type(exc).__name__, exc)
        )
        sys.exit(1)
