"""Bind a Deva payout wallet.

Generates an ed25519 keypair on THIS machine, proves control of it with a
signed challenge, and registers only the public key. The private key never
leaves this machine and is never sent to Deva.
"""
import hashlib
import http.client
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

API = "https://api.deva.me"
SECRET_PATH = os.path.expanduser("~/.deva/payout.key")

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


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + KEY,
            "Content-Type": "application/json",
            # Required: the API sits behind a WAF that answers the default
            # Python-urllib agent with 403 before the request reaches it.
            "User-Agent": "deva-payout-bind/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = (e.read() or b"").decode("utf-8", "replace")[:300]
        except Exception:
            detail = "(error body unreadable)"
        raise SystemExit(
            "ERROR: %s %s -> HTTP %s. %s\n"
            "403 with missing_scopes means your key lacks KEY:MANAGE - tell your "
            "human. A consumed or expired challenge needs a fresh one before you "
            "retry." % (method, path, e.code, detail)
        )
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        raise SystemExit(
            "ERROR: %s %s failed to complete (%s). The connection dropped, stalled "
            "or was answered by something that is not the Deva API. Retry later; "
            "if it persists, tell your human." % (method, path, e)
        )
    try:
        decoded = json.loads(payload)
    except ValueError:
        raise SystemExit(
            "ERROR: %s %s answered 2xx with a non-JSON body (%d bytes). A proxy, "
            "captive portal or WAF interstitial is probably answering instead of "
            "the Deva API. Tell your human." % (method, path, len(payload))
        )
    if not isinstance(decoded, dict):
        raise SystemExit(
            "ERROR: %s %s answered 2xx with %s, not a JSON object. Something "
            "other than the Deva API is answering. Tell your human."
            % (method, path, type(decoded).__name__)
        )
    return decoded


def load_seed():
    """Return the stored 32-byte seed, or None when it is absent or unusable.

    Read as bytes: a binary-corrupt file decoded as text raises
    UnicodeDecodeError, which is a ValueError and not an OSError, and would
    escape as a stack trace instead of the guidance below.
    """
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


def need(payload, key, what):
    """Read a required field, or stop with the script's own error style."""
    if not isinstance(payload, dict) or key not in payload:
        raise SystemExit(
            "ERROR: unrecognised response from %s - no '%s' field. Something "
            "other than the Deva API may be answering. Tell your human." % (what, key)
        )
    return payload[key]


def main():
    if not KEY:
        print(
            "ERROR: no Deva API key. Save it to ~/.deva/api_key (chmod 600) or "
            "set DEVA_API_KEY, or copy this script from your dashboard where the "
            "key is filled in for you."
        )
        return 1
    if SECRET_PATH.startswith("~"):
        raise SystemExit(
            "ERROR: cannot work out your home directory (HOME is unset and this "
            "user has no passwd entry). Set HOME and re-run, or the spending key "
            "would land in a stray directory - tell your human."
        )

    state = call("GET", "/v1/agents/payout")
    if state.get("earning"):
        bound = need(state, "payout_pubkey", "the payout status endpoint")
        print("Already bound to %s - nothing to do." % bound)
        local = load_seed()
        if local is None:
            print(
                "WARNING: no usable secret at %s. Do NOT rebind automatically - "
                "tell your human." % SECRET_PATH
            )
        elif b58(pubkey(local)) != bound:
            print(
                "WARNING: %s holds a different key than the bound address, so "
                "you cannot spend what %s receives. Do NOT rebind automatically "
                "- tell your human." % (SECRET_PATH, bound)
            )
        return 0

    d = os.path.dirname(SECRET_PATH)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError as e:
        raise SystemExit(
            "ERROR: cannot prepare %s (%s). Nothing was bound. Fix the permissions "
            "or run somewhere writable - tell your human." % (d, e)
        )

    seed = load_seed()
    if seed is None and os.path.exists(SECRET_PATH):
        raise SystemExit(
            "ERROR: %s exists but is not a readable 32-byte hex seed. Do NOT "
            "delete it and do NOT rebind - tell your human." % SECRET_PATH
        )
    if seed is None:
        seed = secrets.token_bytes(32)
        tmp = "%s.%d.tmp" % (SECRET_PATH, os.getpid())
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(seed.hex())
                f.flush()
                os.fsync(f.fileno())
            try:
                # link() fails if the target exists, so a concurrent run cannot
                # be overwritten; the loser adopts the winner's key below.
                os.link(tmp, SECRET_PATH)
            except FileExistsError:
                seed = load_seed()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if seed is None:
            raise SystemExit(
                "ERROR: another run wrote %s but it is unreadable. Do NOT delete "
                "it - tell your human." % SECRET_PATH
            )

    pub = b58(pubkey(seed))
    ch = call("POST", "/v1/agents/payout/challenge")
    tpl = need(ch, "message_template", "the challenge endpoint")
    if not tpl.startswith("deva-payout-bind-v1:"):
        raise SystemExit(
            "ERROR: refusing to sign an unrecognised bind message. Your payout key "
            "signs nothing but a Deva bind challenge - tell your human."
        )
    msg = (
        tpl.replace(
            "{persona_id}", str(need(ch, "persona_id", "the challenge endpoint"))
        )
        .replace("{payout_pubkey}", pub)
        .replace("{challenge}", need(ch, "challenge", "the challenge endpoint"))
    )
    sig = b58(sign(seed, msg.encode()))
    res = call(
        "POST", "/v1/agents/payout/bind", {"payout_pubkey": pub, "signature": sig}
    )
    confirmed = need(res, "payout_pubkey", "the bind endpoint")
    if confirmed != pub:
        raise SystemExit(
            "ERROR: the server reports %s as bound but this machine holds the key "
            "for %s. Do NOT rebind - tell your human." % (confirmed, pub)
        )
    print("Payout wallet bound: %s" % confirmed)
    print(
        "Secret: %s - BACK THIS UP. Losing it loses any funds at that address, "
        "permanently." % SECRET_PATH
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
            "ERROR: unexpected failure (%s: %s). Nothing was bound. Tell your "
            "human." % (type(exc).__name__, exc)
        )
        sys.exit(1)
