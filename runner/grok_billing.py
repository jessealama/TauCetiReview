"""Subscription-credit pacing for the native Grok Build reviewer.

A `grok login` review spends the user's X/SuperGrok credit pool, not API money, so it is paced the
way TauCetiWorker paces Codex and Claude: a piecewise-linear `time%:budget%` curve over the current
usage period, with usage required to stay strictly below the budget for the elapsed fraction.

The only machine-readable usage signal Grok Build exposes is the `x.ai/billing` extension of its ACP
agent (`grok agent stdio`, sent on the wire as `_x.ai/billing`), which proxies
`GET {cli_chat_proxy}/billing?format=credits`. It is an INTERNAL, UNDOCUMENTED interface of the
open-source client (xai-grok-shell/src/extensions/billing.rs), not a published API, so every read
fails closed: anything this module cannot parse, any missing field, or any doubt about on-demand
billing is a refusal, never "free".

On-demand (pay-as-you-go) credit: when it is enabled, usage past the included allowance bills real
money, which a subscription reviewer must never do. So a run is refused unless the response says
on-demand is exactly `false` (`on_demand_enabled` on the wire); absent or null counts as enabled.
That flag comes from the client's remote settings, so a cold agent that has not loaded them yet
reads as null and is refused too.

Run as a script (runner/ on sys.path), so imports are flat siblings, not package-relative."""

import datetime, json, os, select, subprocess, time

# The fixed prefix of a refusal's error_message. review.error_kind keys the non-retried,
# provider-down `pacing_hold` kind off it, read from error_message only (never from stderr), so
# provider text cannot claim it.
GROK_PACING_HOLD = "grok subscription pacing refused the run"

# TauCetiWorker's default curve: 40% of the period's credits by 60% of the period, then a ramp to
# the full allowance by the reset (a reserve for work that arrives late). Overridable with the same
# syntax as the Worker's --pace, via TAUCETI_GROK_PACE.
DEFAULT_PACE = "60:40"

# One read must not stall a round: an ACP initialize + one HTTP fetch.
BILLING_TIMEOUT_S = 30

_PERIOD_TYPES = {"USAGE_PERIOD_TYPE_WEEKLY": "weekly", "USAGE_PERIOD_TYPE_MONTHLY": "monthly"}


def parse_pace(spec):
    """`"60:40"` / `"0:10,50:70,90:90"` -> sorted [(time%, budget%)], anchored at 0:0 and 100:100
    unless given. Raises ValueError on anything malformed (the caller refuses the run)."""
    points = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        t, _, b = part.partition(":")
        t, b = float(t), float(b)
        if not (0 <= t <= 100) or b < 0:
            raise ValueError(f"pace point {part!r} out of range")
        points.append((t, b))
    if not points:
        raise ValueError("empty pace curve")
    points.sort()
    if points[0][0] > 0:
        points.insert(0, (0.0, 0.0))
    if points[-1][0] < 100:
        points.append((100.0, 100.0))
    return points


def pace_budget(curve, elapsed):
    """Budget % allowed at `elapsed` (0..1 of the period), interpolated along the curve."""
    pct = max(0.0, min(100.0, elapsed * 100.0))
    for (t0, b0), (t1, b1) in zip(curve, curve[1:]):
        if t0 <= pct <= t1:
            return b0 if t1 == t0 else b0 + (b1 - b0) * (pct - t0) / (t1 - t0)
    return curve[-1][1]


def _parse_time(s):
    if not isinstance(s, str) or not s:
        raise ValueError("missing period timestamp")
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def decide(billing, pace=None, now=None):
    """Return `(ok, reason, summary)` for one `x.ai/billing` response.

    `summary` is safe to log ({"percent", "period", "budget"}): no identifiers, no tier, no money.
    Fails closed on every missing or malformed field."""
    try:
        curve = parse_pace(pace if pace is not None else (os.environ.get("TAUCETI_GROK_PACE") or DEFAULT_PACE))
    except ValueError as e:
        return False, f"invalid TAUCETI_GROK_PACE ({e})", {}
    if not isinstance(billing, dict):
        return False, "billing response is not an object", {}
    # The top-level response struct (BillingConfigResponse) has no serde rename, so the wire key is
    # `on_demand_enabled`; the client's own log context spells it `onDemandEnabled`. Accept either,
    # but only an explicit false in every spelling present passes.
    flags = [billing[k] for k in ("on_demand_enabled", "onDemandEnabled") if k in billing]
    if not flags or any(f is not False for f in flags):
        return False, ("on-demand (pay-as-you-go) billing is enabled or not reported; a subscription "
                       "reviewer could spend money past the included credits"), {}
    config = billing.get("config")
    if not isinstance(config, dict):
        return False, "billing response has no config", {}
    used = config.get("creditUsagePercent")
    if not isinstance(used, (int, float)) or isinstance(used, bool) or not 0 <= used <= 100:
        return False, "creditUsagePercent missing or out of range", {}
    period = config.get("currentPeriod")
    if not isinstance(period, dict):
        return False, "currentPeriod missing", {}
    kind = _PERIOD_TYPES.get(period.get("type"))
    if kind is None:
        return False, "currentPeriod type missing or unknown", {}
    try:
        start, end = _parse_time(period.get("start")), _parse_time(period.get("end"))
    except ValueError as e:
        return False, f"currentPeriod unreadable ({e})", {}
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        return False, "currentPeriod bounds invalid", {}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    elapsed = (now - start).total_seconds() / (end - start).total_seconds()
    if not 0 <= elapsed <= 1:
        return False, "now is outside the reported period (stale billing data)", {}
    budget = pace_budget(curve, elapsed)
    summary = {"percent": round(float(used), 1), "period": kind, "budget": round(budget, 1)}
    if used >= 100:
        return False, f"{kind} credits exhausted ({summary['percent']}% used)", summary
    if used >= budget:
        return False, (f"{summary['percent']}% of {kind} credits used, at or above the pace budget "
                       f"{summary['budget']}% for {elapsed:.0%} of the period"), summary
    return True, "", summary


def read_billing(env, timeout=BILLING_TIMEOUT_S):
    """One `x.ai/billing` read through `grok agent stdio`, run in the reviewer's own env (its
    throwaway GROK_HOME and copied login, never XAI_API_KEY). Returns the response's `result`
    object; raises RuntimeError with a short, payload-free reason on any failure."""
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": 1, "clientCapabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}},
    ]
    try:
        p = subprocess.Popen(["grok", "agent", "--no-leader", "stdio"], cwd=env["HOME"], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    except OSError as e:
        raise RuntimeError(f"cannot start `grok agent stdio` ({e.__class__.__name__})") from None
    try:
        p.stdin.write("".join(json.dumps(r) + "\n" for r in reqs).encode())
        p.stdin.flush()
        # Raw os.read on the pipe, not a buffered readline: select() cannot see lines that a text
        # wrapper has already buffered, which would stall the read until the timeout.
        fd, deadline, pending = p.stdout.fileno(), time.monotonic() + timeout, b""
        while True:
            *lines, pending = pending.split(b"\n")
            for raw in lines:
                msg = _response(raw)
                if msg is not None:
                    return msg
            left = deadline - time.monotonic()
            if left <= 0:
                raise RuntimeError("billing read timed out")
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError("`grok agent stdio` exited before answering")
            pending += chunk
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def _response(raw):
    """The `result` of the id-2 response on one stdout line, or None for any other line."""
    line = raw.decode("utf-8", "replace").strip()
    if not line.startswith("{"):
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("id") != 2:
        return None
    if "error" in msg:
        err = msg["error"] if isinstance(msg["error"], dict) else {}
        code = err.get("code") if isinstance(err.get("code"), int) else "?"
        # Only the JSON-RPC code: the message/data may carry provider text and is dropped.
        raise RuntimeError(f"billing read refused (JSON-RPC error {code})")
    if not isinstance(msg.get("result"), dict):
        raise RuntimeError("billing read returned no result object")
    return msg["result"]


def check(env, pace=None, reader=read_billing, now=None):
    """`(ok, error_message, summary)`: gate one native-grok attempt on subscription credits.
    error_message (when not ok) starts with GROK_PACING_HOLD and carries no identifiers."""
    try:
        billing = reader(env)
    except Exception as e:  # noqa: BLE001 — any failure to read is a refusal, never a pass
        return False, f"{GROK_PACING_HOLD}: usage unreadable ({e})", {}
    ok, reason, summary = decide(billing, pace=pace, now=now)
    return (True, "", summary) if ok else (False, f"{GROK_PACING_HOLD}: {reason}", summary)
