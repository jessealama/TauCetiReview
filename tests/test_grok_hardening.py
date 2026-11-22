#!/usr/bin/env python3
"""Native Grok Build reviewer hardening: kernel sandbox profile, scratch CWD, checkout integrity,
no API-key fallback, and fail-closed subscription-credit pacing via `x.ai/billing`. All mocked: no
`grok` binary, login, network or credits are used (tests/live_grok_sandbox_probe.py is the live one)."""

import datetime
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import grok_billing  # noqa: E402
import reviewers  # noqa: E402
import review  # noqa: E402
from test_grok import _END, _READ_TOOLS_EVENT, _run_rubric, _stream, private_dir  # noqa: E402

UTC = datetime.timezone.utc
START = datetime.datetime(2026, 11, 16, tzinfo=UTC)
END = datetime.datetime(2026, 11, 23, tzinfo=UTC)


def _billing(percent=10.0, on_demand=False, period_type="USAGE_PERIOD_TYPE_WEEKLY", key="on_demand_enabled",
             start=START, end=END):
    period = {"type": period_type, "start": start.isoformat().replace("+00:00", "Z"),
              "end": end.isoformat().replace("+00:00", "Z")}
    out = {"config": {"creditUsagePercent": percent, "currentPeriod": period,
                      "isUnifiedBillingUser": True}, "subscription_tier": "SuperGrok"}
    if key is not None:
        out[key] = on_demand
    return out


def _at(fraction):
    return START + (END - START) * fraction


# --- pacing curve ---------------------------------------------------------------------------------

def test_parse_pace_matches_the_worker_syntax():
    assert grok_billing.parse_pace("60:40") == [(0.0, 0.0), (60.0, 40.0), (100.0, 100.0)]
    assert grok_billing.parse_pace("0:10,50:70,90:90") == [(0.0, 10.0), (50.0, 70.0), (90.0, 90.0),
                                                           (100.0, 100.0)]
    for bad in ("", "x", "60", "120:40", "50:-1", "60:nan", "nan:40", "60:inf", "inf:40"):
        try:
            grok_billing.parse_pace(bad)
            assert False, bad
        except ValueError:
            pass


def test_pace_budget_interpolates():
    curve = grok_billing.parse_pace("60:40")
    assert grok_billing.pace_budget(curve, 0.0) == 0.0
    assert abs(grok_billing.pace_budget(curve, 0.3) - 20.0) < 1e-9
    assert abs(grok_billing.pace_budget(curve, 0.6) - 40.0) < 1e-9
    assert abs(grok_billing.pace_budget(curve, 0.8) - 70.0) < 1e-9
    assert grok_billing.pace_budget(curve, 1.0) == 100.0


# --- decide: every doubt is a refusal -------------------------------------------------------------

def test_decide_allows_usage_below_the_pace_line():
    ok, reason, summary = grok_billing.decide(_billing(10.0), pace="60:40", now=_at(0.5))
    assert ok and not reason, reason
    assert summary == {"percent": 10.0, "period": "weekly", "budget": 33.3}, summary


def test_decide_holds_at_or_above_the_pace_line():
    ok, reason, summary = grok_billing.decide(_billing(34.0), pace="60:40", now=_at(0.5))
    assert not ok and "pace budget" in reason, reason
    assert summary["percent"] == 34.0


def test_decide_holds_when_exhausted_even_with_no_soft_cap():
    ok, reason, _ = grok_billing.decide(_billing(100.0), pace="0:100", now=_at(0.9))
    assert not ok and "exhausted" in reason, reason


def test_decide_on_demand_flag_when_present_must_be_exactly_false():
    for kwargs in ({"on_demand": True}, {"on_demand": None}, {"on_demand": "false"}, {"on_demand": 0},
                   {"key": "onDemandEnabled", "on_demand": True}):
        ok, reason, _ = grok_billing.decide(_billing(**kwargs), pace="60:40", now=_at(0.5))
        assert not ok and "on-demand" in reason, (kwargs, reason)
    assert grok_billing.decide(_billing(), pace="60:40", now=_at(0.5))[0]
    assert grok_billing.decide(_billing(key="onDemandEnabled"), pace="60:40", now=_at(0.5))[0]
    # Every spelling present must say false.
    assert not grok_billing.decide(dict(_billing(), onDemandEnabled=True), pace="60:40", now=_at(0.5))[0]
    assert grok_billing.decide(dict(_billing(), onDemandEnabled=False), pace="60:40", now=_at(0.5))[0]


def test_decide_absent_flag_is_off_only_without_on_demand_fields():
    # The live shape for an account with pay-as-you-go off: no flag, no on-demand fields.
    assert grok_billing.decide(_billing(key=None), pace="60:40", now=_at(0.5))[0]
    assert grok_billing.decide(dict(_billing(key=None), config=dict(_billing()["config"], history=[
        {"billingCycle": {"year": 2026, "month": 10}, "includedUsed": {"val": 900}}])),
        pace="60:40", now=_at(0.5))[0]
    for field in ("onDemandCap", "onDemandUsed", "on_demand_cap", "on_demand_used"):
        for value in ({"val": 500}, {}, None, 0):
            b = _billing(key=None)
            b["config"][field] = value
            ok, reason, _ = grok_billing.decide(b, pace="60:40", now=_at(0.5))
            assert not ok and "on-demand" in reason, (field, value)
        b = _billing(key=None)
        b["config"]["history"] = [{"includedUsed": {"val": 1}}, {field: {"val": 3}}]
        assert not grok_billing.decide(b, pace="60:40", now=_at(0.5))[0], field
    for bad_history in ("x", [1], [None]):
        b = _billing(key=None)
        b["config"]["history"] = bad_history
        assert not grok_billing.decide(b, pace="60:40", now=_at(0.5))[0], bad_history
    # A cap or usage field refuses even when the flag says false.
    b = _billing(on_demand=False)
    b["config"]["onDemandCap"] = {"val": 500}
    assert not grok_billing.decide(b, pace="60:40", now=_at(0.5))[0], "an on-demand cap must refuse"


def test_decide_fails_closed_on_malformed_responses():
    cases = [None, [], {}, {"on_demand_enabled": False},
             dict(_billing(), config={"currentPeriod": _billing()["config"]["currentPeriod"]}),
             _billing(percent=True), _billing(percent=-1), _billing(percent=101), _billing(percent="9"),
             _billing(period_type="USAGE_PERIOD_TYPE_DAILY"), _billing(period_type=None),
             _billing(period_type=["USAGE_PERIOD_TYPE_WEEKLY"]), _billing(period_type={"a": 1}),
             _billing(start=END, end=START)]
    no_period = _billing()
    del no_period["config"]["currentPeriod"]
    bad_time = _billing()
    bad_time["config"]["currentPeriod"]["end"] = "soon"
    naive = _billing()
    naive["config"]["currentPeriod"]["start"] = "2026-11-16T00:00:00"
    cases += [no_period, bad_time, naive]
    for c in cases:
        ok, reason, _ = grok_billing.decide(c, pace="60:40", now=_at(0.5))
        assert not ok and reason, c


def test_decide_refuses_stale_periods_and_bad_curves():
    ok, reason, _ = grok_billing.decide(_billing(), pace="60:40", now=END + datetime.timedelta(hours=1))
    assert not ok and "stale" in reason, reason
    ok, reason, _ = grok_billing.decide(_billing(), pace="nonsense", now=_at(0.5))
    assert not ok and "TAUCETI_GROK_PACE" in reason, reason


def test_monthly_period_is_named():
    ok, _, summary = grok_billing.decide(_billing(period_type="USAGE_PERIOD_TYPE_MONTHLY"),
                                         pace="60:40", now=_at(0.5))
    assert ok and summary["period"] == "monthly"


def test_check_prefixes_refusals_and_treats_reader_errors_as_holds():
    def boom(env):
        raise RuntimeError("billing read refused (JSON-RPC error -32000)")
    ok, msg, _ = grok_billing.check({}, pace="60:40", reader=boom, now=_at(0.5))
    assert not ok and msg.startswith(grok_billing.GROK_PACING_HOLD) and "unreadable" in msg, msg
    ok, msg, _ = grok_billing.check({}, pace="60:40", reader=lambda env: _billing(on_demand=True),
                                    now=_at(0.5))
    assert not ok and msg.startswith(grok_billing.GROK_PACING_HOLD), msg
    ok, msg, summary = grok_billing.check({}, pace="60:40", reader=lambda env: _billing(), now=_at(0.5))
    assert ok and msg == "" and summary["period"] == "weekly"


def test_summary_and_refusals_carry_no_identifiers():
    resp = _billing()
    resp["config"]["userId"] = "user-SECRET"
    ok, msg, summary = grok_billing.check({}, pace="60:40", reader=lambda env: resp, now=_at(0.5))
    assert set(summary) == {"percent", "period", "budget"}
    assert "SECRET" not in json.dumps(summary) and "SuperGrok" not in json.dumps(summary)


# --- read_billing over a fake `grok agent stdio` --------------------------------------------------

def _fake_grok(tmp, body):
    """A `grok` on PATH that speaks just enough JSON-RPC: logs its argv/requests, then runs `body`."""
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    script = os.path.join(bindir, "grok")
    with open(script, "w") as f:
        f.write("#!" + sys.executable + "\nimport json, os, sys\n"
                "log = open(os.path.join(os.environ['HOME'], 'calls.jsonl'), 'a')\n"
                "log.write(json.dumps({'argv': sys.argv[1:], 'xai': 'XAI_API_KEY' in os.environ}) + '\\n')\n"
                + body)
    os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
    return bindir


_SERVE = ("for line in iter(sys.stdin.readline, ''):\n"
          "    req = json.loads(line)\n"
          "    log.write(json.dumps({'method': req['method']}) + '\\n'); log.flush()\n"
          "    if req['method'] == 'initialize':\n"
          "        print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': {'protocolVersion': 1}}), flush=True)\n"
          "    elif req['method'] == '_x.ai/billing':\n"
          "        print('not json noise', flush=True)\n"
          "        print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': RESULT}), flush=True)\n")


def test_read_billing_speaks_acp_and_returns_the_result():
    tmp = tempfile.mkdtemp()
    try:
        bindir = _fake_grok(tmp, f"RESULT = {json.dumps(_billing())!r}\nRESULT = json.loads(RESULT)\n" + _SERVE)
        env = {"HOME": tmp, "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
        got = grok_billing.read_billing(env, timeout=10)
        assert got == _billing(), got
        calls = [json.loads(x) for x in open(os.path.join(tmp, "calls.jsonl"))]
        assert calls[0]["argv"] == ["agent", "--no-leader", "stdio"] and calls[0]["xai"] is False, calls[0]
        assert [c.get("method") for c in calls[1:]] == ["initialize", "_x.ai/billing"], calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_read_billing_failures_are_payload_free_errors():
    bodies = {
        "error": ("for line in iter(sys.stdin.readline, ''):\n    req = json.loads(line)\n"
                  "    if req['method'] == '_x.ai/billing':\n"
                  "        print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'error': "
                  "{'code': -32000, 'message': 'Billing service error: user-SECRET'}}), flush=True)\n"),
        "exit": "sys.exit(1)\n",
        "hang": "import time\ntime.sleep(30)\n",
        "no result": ("for line in iter(sys.stdin.readline, ''):\n    req = json.loads(line)\n"
                      "    print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': 7}), flush=True)\n"),
    }
    for name, body in bodies.items():
        tmp = tempfile.mkdtemp()
        try:
            env = {"HOME": tmp, "PATH": _fake_grok(tmp, body) + os.pathsep + os.environ.get("PATH", "")}
            try:
                grok_billing.read_billing(env, timeout=2)
                assert False, name
            except RuntimeError as e:
                assert "SECRET" not in str(e), (name, e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    try:
        grok_billing.read_billing({"HOME": "/", "PATH": "/nonexistent"}, timeout=2)
        assert False
    except RuntimeError as e:
        assert "cannot start" in str(e)


# --- sandbox profile and scratch CWD --------------------------------------------------------------

def test_sandbox_profile_is_strict_readonly_checkout_and_denies_existing_credentials():
    tmp = tempfile.mkdtemp()
    try:
        real_home = os.path.join(tmp, "home")
        for p in (".ssh", ".config/gh", ".grok"):
            os.makedirs(os.path.join(real_home, p))
        checkout = os.path.join(tmp, "work")
        os.makedirs(checkout)
        text = reviewers.grok_sandbox_toml(checkout, real_home=real_home, exe_chain=[])
        assert text.startswith(f"[profiles.{reviewers.GROK_SANDBOX_PROFILE}]\n"), text
        assert 'extends = "strict"' in text and "restrict_network = true" in text
        assert f'read_only = ["{os.path.realpath(checkout)}"]' in text, text
        assert "read_write = []" in text
        deny = text.split("deny = ", 1)[1]
        for p in (".ssh", ".config/gh", ".grok"):
            assert json.dumps(os.path.join(real_home, p)) in deny, (p, deny)
        # Only existing paths: a literal deny that does not exist cannot be bound on Linux.
        assert ".aws" not in deny and ".netrc" not in deny
        assert "*" not in deny  # no globs (Linux expands them at launch; huge trees refuse to start)
        try:
            import tomllib
        except ImportError:  # Python 3.10
            tomllib = None
        if tomllib:
            prof = tomllib.loads(text)["profiles"][reviewers.GROK_SANDBOX_PROFILE]
            assert prof["extends"] == "strict" and prof["read_only"] == [os.path.realpath(checkout)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_deny_keeps_the_grok_executable_chain_reachable():
    # The official installer: ~/.local/bin/grok -> ~/.grok/bin/grok -> ~/.grok/downloads/grok-<arch>.
    tmp = tempfile.mkdtemp()
    try:
        home = os.path.join(tmp, "home")
        for d in (".grok/bin", ".grok/downloads", ".grok/sessions", ".ssh"):
            os.makedirs(os.path.join(home, d))
        for f in (".grok/auth.json", ".grok/config.toml"):
            open(os.path.join(home, f), "w").close()
        chain = [os.path.join(home, ".local/bin/grok"), os.path.join(home, ".grok/bin/grok"),
                 os.path.join(home, ".grok/downloads/grok-linux-x86_64")]
        deny = reviewers._grok_deny_paths(home, chain)
        g = lambda p: os.path.join(home, p)  # noqa: E731
        assert g(".grok") not in deny and g(".grok/bin") not in deny and g(".grok/downloads") not in deny
        for p in (".grok/auth.json", ".grok/config.toml", ".grok/sessions", ".ssh"):
            assert g(p) in deny, (p, deny)
        # Without grok under it, the whole directory is denied.
        assert g(".grok") in reviewers._grok_deny_paths(home, ["/usr/bin/grok"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scratch_links_the_checkout_top_level_and_writes_the_profile():
    tmp = tempfile.mkdtemp()
    try:
        checkout = os.path.join(tmp, "work")
        os.makedirs(os.path.join(checkout, "code"))
        open(os.path.join(checkout, "diff.txt"), "w").close()
        home = os.path.join(tmp, "rev")
        os.makedirs(os.path.join(home, ".grok"))
        env = {"HOME": home, "GROK_HOME": os.path.join(home, ".grok")}
        scratch = reviewers.grok_scratch(env, checkout)
        assert scratch == os.path.join(home, "scratch")
        assert sorted(os.listdir(scratch)) == ["code", "diff.txt"]
        for name in ("code", "diff.txt"):
            link = os.path.join(scratch, name)
            assert os.path.islink(link) and os.readlink(link) == os.path.join(os.path.realpath(checkout), name)
        with open(os.path.join(env["GROK_HOME"], "sandbox.toml")) as f:
            assert os.path.realpath(checkout) in f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_in(checkout, sh):
    home = tempfile.mkdtemp()
    original = reviewers.sh
    reviewers.sh = sh
    try:
        return reviewers.run_grok("PROMPT", checkout, "grok-4.7", {"HOME": home})
    finally:
        reviewers.sh = original
        shutil.rmtree(home, ignore_errors=True)


def test_a_changed_checkout_fails_closed_and_is_not_retried():
    stream = _stream(_READ_TOOLS_EVENT, {"type": "text", "data": "verdict"}, _END)
    for mutate in ("create", "modify"):
        checkout = private_dir()
        try:
            with open(os.path.join(checkout, "a.lean"), "w") as f:
                f.write("x")

            def sh(cmd, cwd=None, env=None, **kw):
                # The sandbox "failed": the run wrote into the checkout behind a scratch symlink.
                if mutate == "create":
                    open(os.path.join(cwd, "pwned.txt"), "w").close()  # lands in scratch: harmless
                    open(os.path.join(checkout, "pwned.txt"), "w").close()
                else:
                    with open(os.path.join(cwd, "a.lean"), "a") as f:
                        f.write("y")
                return types.SimpleNamespace(returncode=0, stdout=stream, stderr="")

            out = _run_in(checkout, sh)
            assert out["is_error"] and out["text"] == "", out
            assert out["error_message"] == reviewers.GROK_CHECKOUT_CHANGED
            assert review.error_kind(out) == "sandbox_violation"
            assert "sandbox_violation" in review.NO_RETRY_KINDS
        finally:
            shutil.rmtree(checkout, ignore_errors=True)


def test_scratch_only_writes_do_not_trip_the_integrity_check():
    stream = _stream(_READ_TOOLS_EVENT, {"type": "text", "data": "verdict"}, _END)
    checkout = private_dir()
    try:
        def sh(cmd, cwd=None, env=None, **kw):
            open(os.path.join(cwd, "notes.txt"), "w").close()  # CWD is the scratch dir
            return types.SimpleNamespace(returncode=0, stdout=stream, stderr="")
        out = _run_in(checkout, sh)
        assert not out.get("is_error"), out
        assert os.listdir(checkout) == []
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def test_checkout_under_a_strict_writable_root_is_refused_before_any_run():
    calls = []

    def sh(cmd, **kw):
        calls.append(cmd)
        raise AssertionError("must not launch grok")
    home = private_dir()
    checkout = tempfile.mkdtemp()  # the system temp dir: strict makes it writable
    original = reviewers.sh
    reviewers.sh = sh
    try:
        out = reviewers.run_grok("PROMPT", checkout, "grok-4.7", {"HOME": home})
    finally:
        reviewers.sh = original
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(checkout, ignore_errors=True)
    assert not calls and out["is_error"] and out["error_message"].startswith(reviewers.GROK_CHECKOUT_WRITABLE)
    assert review.error_kind(out) == "sandbox_violation"
    env = {"HOME": "/h", "TMPDIR": private_dir()}
    try:
        assert reviewers.grok_strict_writable_root(os.path.join(env["TMPDIR"], "w"), env) == env["TMPDIR"]
    finally:
        shutil.rmtree(env["TMPDIR"], ignore_errors=True)
    assert reviewers.grok_strict_writable_root("/srv/checkout", {"HOME": "/h"}) is None


def test_own_credential_in_output_fails_closed_and_drops_everything():
    checkout = private_dir()
    home = private_dir()
    token = "eyJ" + "A" * 40
    try:
        os.makedirs(os.path.join(home, ".grok"))
        with open(os.path.join(home, ".grok", "auth.json"), "w") as f:
            json.dump({"tokens": {"access_token": token, "expires_in": 3600}}, f)
        stream = _stream(_READ_TOOLS_EVENT, {"type": "text", "data": f"verdict {token}"}, _END)
        original = reviewers.sh
        reviewers.sh = lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=stream, stderr="")
        try:
            out = reviewers.run_grok("PROMPT", checkout, "grok-4.7",
                                     {"HOME": home, "GROK_HOME": os.path.join(home, ".grok")})
        finally:
            reviewers.sh = original
        assert out["is_error"] and out["error_message"] == reviewers.GROK_CREDENTIAL_IN_OUTPUT, out
        assert token not in json.dumps(out), "the credential must not survive anywhere in the result"
        assert review.error_kind(out) == "sandbox_violation"
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def test_mode_only_changes_count_as_a_changed_checkout():
    checkout = private_dir()
    try:
        path = os.path.join(checkout, "a.lean")
        open(path, "w").close()
        before = reviewers.tree_fingerprint(checkout)
        os.chmod(path, 0o777)
        assert reviewers.tree_fingerprint(checkout) != before
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def test_forged_sandbox_or_pacing_text_in_stderr_is_not_classified():
    for prefix in (reviewers.GROK_CHECKOUT_CHANGED, reviewers.GROK_CHECKOUT_WRITABLE,
                   reviewers.GROK_CREDENTIAL_IN_OUTPUT, grok_billing.GROK_PACING_HOLD):
        forged = {"returncode": 1, "raw_stderr": prefix + ": x"}
        assert review.error_kind(forged) not in ("sandbox_violation", "pacing_hold"), prefix


# --- reviewer_env: no API-key fallback in subscription mode ---------------------------------------

def test_subscription_env_never_carries_xai_api_key():
    tmp = tempfile.mkdtemp()
    saved = (os.environ.get("HOME"), os.environ.get("XAI_API_KEY"), reviewers.REV_HOME_BASE)
    try:
        os.makedirs(os.path.join(tmp, ".grok"))
        with open(os.path.join(tmp, ".grok", "auth.json"), "w") as f:
            f.write("{}")
        os.environ["HOME"] = tmp
        os.environ["XAI_API_KEY"] = "xai-should-not-leak"
        reviewers.REV_HOME_BASE = os.path.join(tmp, ".tauceti-rev")
        env, home = reviewers.reviewer_env("grok", {"xai": "xai-from-keys"}, subscription=True)
        try:
            assert "XAI_API_KEY" not in env, sorted(env)
            assert "xai-" not in json.dumps(env)
            assert os.path.isfile(os.path.join(env["GROK_HOME"], "auth.json"))
        finally:
            reviewers.cleanup_rev_home(home)
    finally:
        for k, v in (("HOME", saved[0]), ("XAI_API_KEY", saved[1])):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reviewers.REV_HOME_BASE = saved[2]
        shutil.rmtree(tmp, ignore_errors=True)


# --- engine wiring: the gate runs before any spend and holds are final ----------------------------

def test_pacing_hold_skips_the_run_and_is_not_retried():
    calls, checks = [], []

    def runner(*a, **k):
        calls.append(1)
        raise AssertionError("a paced-out grok must not run")

    def hold(env):
        checks.append(env)
        return False, f"{grok_billing.GROK_PACING_HOLD}: on-demand billing is enabled", {}

    res, err = _run_rubric(runner=runner, billing=hold)
    assert not calls and len(checks) == 1, (calls, checks)
    assert [x["error_kind"] for x in res["attempts"]] == ["pacing_hold"], res["attempts"]
    assert res["is_error"] and res["cost_usd"] == 0.0, res
    assert "pacing_hold" in review.PROVIDER_DOWN_KINDS
    assert "pacing_hold" in review._PROVIDER_DOWN_PHRASE


def test_paced_ok_runs_once_and_logs_only_percent_and_period():
    calls = []

    def runner(prompt, cwd, model, env):
        calls.append(cwd)
        return {"returncode": 0, "text": "", "raw_stderr": "", "cost_usd": 0.0}

    ok = (True, "", {"percent": 12.5, "period": "weekly", "budget": 40.0})
    res, err = _run_rubric(runner=runner, billing=ok)
    assert calls, "the run must happen once pacing allows it"
    assert "12.5% of the weekly period used" in err, err


def test_api_auth_is_not_paced():
    # --auth api is explicit API billing: there is no credit pool, so no billing read happens.
    checks = []
    import argparse  # noqa: F401  (parity with _run_rubric's own imports)
    original = review.grok_billing.check
    review.grok_billing.check = lambda env: checks.append(env) or (False, "x", {})
    try:
        # _run_rubric builds a subscription ctx; flip it by wrapping RunContext.
        orig_ctx = review.RunContext

        def api_ctx(**kw):
            kw["subscription"] = False
            return orig_ctx(**kw)
        review.RunContext = api_ctx
        try:
            _run_rubric(runner=lambda *a, **k: {"returncode": 0, "text": "", "raw_stderr": ""},
                        billing=lambda env: checks.append(env) or (False, "x", {}))
        finally:
            review.RunContext = orig_ctx
    finally:
        review.grok_billing.check = original
    assert not checks, "an --auth api grok review must not read subscription billing"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
