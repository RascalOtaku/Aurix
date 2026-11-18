"""Pin the 2026-11-17 review fixes so they do not regress.

- A password change signs out the user's other sessions (but keeps the caller's own).
- An unknown username costs a bcrypt check too (no username enumeration by timing).
- New usernames are limited to a plain charset.
- TOTP backup codes are single-use and matched exactly.
- auth.json / sessions.json are written owner-only; concurrent writers in one process do not share a temp file.
- The internal-tool token check is constant-time and rejects empty / wrong values.
- SSH targets that ssh would read as an option are refused.

These load the real modules from disk by path, because other test files stub `core.auth` in sys.modules.
"""

import importlib.util
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

bcrypt = pytest.importorskip("bcrypt")
if not hasattr(bcrypt, "gensalt") or type(bcrypt).__name__ == "MagicMock":
    pytest.skip("real bcrypt needed", allow_module_level=True)


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


atomic_io = _load("_review_atomic_io", "core/atomic_io.py")
sys.modules.setdefault("core.atomic_io", atomic_io)
auth = _load("_review_core_auth", "core/auth.py")


@pytest.fixture
def mgr(tmp_path):
    m = auth.AuthManager(auth_path=str(tmp_path / "auth.json"))
    assert m.create_user("owner", "correct horse 1", is_admin=True)
    return m


def test_password_change_revokes_other_sessions_keeps_own(mgr):
    mine = mgr.create_session("owner", "correct horse 1")
    stolen = mgr.create_session("owner", "correct horse 1")
    assert mgr.validate_token(mine) and mgr.validate_token(stolen)
    assert mgr.change_password("owner", "correct horse 1", "battery staple 2", keep_token=mine)
    assert mgr.validate_token(mine)
    assert not mgr.validate_token(stolen)


def test_password_change_with_wrong_current_password_keeps_sessions(mgr):
    tok = mgr.create_session("owner", "correct horse 1")
    assert not mgr.change_password("owner", "wrong", "battery staple 2")
    assert mgr.validate_token(tok)


def test_unknown_user_still_runs_bcrypt(mgr, monkeypatch):
    calls = []
    real = auth._verify_password
    monkeypatch.setattr(auth, "_verify_password", lambda p, h: calls.append(h) or real(p, h))
    assert not mgr.verify_password("nobody", "whatever")
    assert calls == [auth._DUMMY_HASH]


@pytest.mark.parametrize("name", ["", "-oProxyCommand", ".hidden", "a b", "x" * 65, "bob;rm", "über"])
def test_invalid_usernames_refused(mgr, name):
    assert not auth.valid_username(name)
    assert not mgr.create_user(name, "long enough pw")


@pytest.mark.parametrize("name", ["alice", "Bob", "first.last", "a_b-c", "me@example.test"])
def test_valid_usernames_accepted(name):
    assert auth.valid_username(name)


def test_rename_to_invalid_name_refused(mgr):
    assert mgr.create_user("alice", "long enough pw")
    assert not mgr.rename_user("alice", "-bad", "owner")
    assert "alice" in mgr.users


class _FakeTOTP:
    """Deterministic stand-in for pyotp.TOTP (pyotp may be a conftest stub): the code for step N is f"{N:06d}"."""
    step = 500_000

    def __init__(self, _secret):
        pass

    def timecode(self, _now):
        return _FakeTOTP.step

    def at(self, _now, offset=0):
        return f"{(_FakeTOTP.step + offset) % 1_000_000:06d}"


@pytest.fixture
def totp_user(mgr, monkeypatch):
    monkeypatch.setattr(auth.pyotp, "TOTP", _FakeTOTP)
    monkeypatch.setattr(_FakeTOTP, "step", 500_000)
    mgr._config["users"]["owner"].update(totp_enabled=True, totp_secret="JBSWY3DPEHPK3PXP", totp_backup_codes=[])
    return mgr


def test_backup_codes_single_use_and_exact_legacy_plaintext(totp_user):
    mgr = totp_user
    mgr._config["users"]["owner"]["totp_backup_codes"] = ["deadbeef", "cafef00d"]
    assert not mgr.totp_verify("owner", "deadbee")      # prefix does not match
    assert not mgr.totp_verify("owner", "")
    assert mgr.totp_verify("owner", " deadbeef ")       # surrounding whitespace tolerated
    assert not mgr.totp_verify("owner", "deadbeef")     # used up
    assert mgr.users["owner"]["totp_backup_codes"] == ["cafef00d"]


def test_totp_code_cannot_be_replayed(totp_user):
    mgr = totp_user
    assert mgr.totp_verify("owner", "500000")
    assert not mgr.totp_verify("owner", "500000")       # same step again
    assert not mgr.totp_verify("owner", "499999")       # an older step inside the window
    _FakeTOTP.step = 500_001
    assert mgr.totp_verify("owner", "500001")           # the next step works


def test_totp_wrong_code_refused(totp_user):
    assert not totp_user.totp_verify("owner", "123456")


def test_totp_enabled_without_secret_fails_closed(totp_user):
    totp_user._config["users"]["owner"]["totp_secret"] = ""
    assert not totp_user.totp_verify("owner", "500000")


def test_confirm_returns_codes_once_and_stores_only_hashes(totp_user):
    mgr = totp_user
    u = mgr._config["users"]["owner"]
    u.pop("totp_enabled")
    u["totp_secret_pending"] = "JBSWY3DPEHPK3PXP"
    assert mgr.totp_confirm_enable("owner", "111111") is None
    codes = mgr.totp_confirm_enable("owner", "500000")
    assert codes and len(codes) == 8 and all(len(c) == 10 for c in codes)
    stored = mgr.users["owner"]["totp_backup_codes"]
    assert all(s.startswith("bcrypt:") for s in stored)
    assert not any(c in json.dumps(mgr._config) for c in codes)
    assert not mgr.totp_verify("owner", "500000")       # the confirming code can't log in again
    assert mgr.totp_verify("owner", codes[3])
    assert not mgr.totp_verify("owner", codes[3])
    assert len(mgr.users["owner"]["totp_backup_codes"]) == 7


def test_plaintext_backup_codes_are_hashed_on_load(tmp_path, monkeypatch):
    monkeypatch.setattr(auth.pyotp, "TOTP", _FakeTOTP)
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"users": {"owner": {
        "password_hash": auth._hash_password("pw-long-enough"), "is_admin": True,
        "totp_enabled": True, "totp_secret": "JBSWY3DPEHPK3PXP", "totp_backup_codes": ["deadbeef"],
    }}}))
    m = auth.AuthManager(auth_path=str(path))
    assert "deadbeef" not in path.read_text()
    assert m.totp_verify("owner", "deadbeef")


def test_rate_limiter_counts_only_hits():
    rl_mod = _load("_review_rate_limiter", "src/rate_limiter.py")
    rl = rl_mod.RateLimiter(max_requests=2, window_seconds=60)
    assert not rl.blocked("u")
    rl.hit("u")
    assert not rl.blocked("u")
    rl.hit("u")
    assert rl.blocked("u")
    assert not rl.blocked("other")
    rl.reset("u")
    assert not rl.blocked("u")


def test_ssh_hostkey_policy(monkeypatch):
    so = _load("_review_ssh_opts", "src/ssh_opts.py")
    monkeypatch.delenv("AURIX_SSH_HOSTKEY_POLICY", raising=False)
    assert so.hostkey_option() == "StrictHostKeyChecking=accept-new"
    monkeypatch.setenv("AURIX_SSH_HOSTKEY_POLICY", "no")
    assert so.hostkey_option() == "StrictHostKeyChecking=no"
    monkeypatch.setenv("AURIX_SSH_HOSTKEY_POLICY", "yes; rm -rf /")
    assert so.hostkey_option() == "StrictHostKeyChecking=accept-new"


def _code_strings(tree):
    """Every string literal in the module EXCEPT docstrings (text that merely talks about a flag is not a call)."""
    import ast
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
                docs.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.value


def test_no_ssh_call_disables_hostkey_checking():
    import ast
    offenders = []
    for p in list(ROOT.glob("routes/*.py")) + list(ROOT.glob("src/**/*.py")) + list(ROOT.glob("services/**/*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        if any("StrictHostKeyChecking=no" in s for s in _code_strings(tree)):
            offenders.append(str(p.relative_to(ROOT)))
    assert offenders == [], offenders


def test_hostkey_scan_ignores_docstrings_but_sees_code():
    import ast
    doc_only = ast.parse('"""Never use `ssh -o StrictHostKeyChecking=no`."""\nx = 1\n')
    in_code = ast.parse('cmd = f"ssh -o StrictHostKeyChecking=no {h}"\n')
    as_arg = ast.parse('argv = ["ssh", "-o", "StrictHostKeyChecking=no", h]\n')
    assert not any("StrictHostKeyChecking=no" in s for s in _code_strings(doc_only))
    assert any("StrictHostKeyChecking=no" in s for s in _code_strings(in_code))
    assert any("StrictHostKeyChecking=no" in s for s in _code_strings(as_arg))


class _H(dict):
    """Case-insensitive header map like Starlette's."""
    def __init__(self, **kw):
        super().__init__({k.lower().replace("_", "-"): v for k, v in kw.items()})

    def get(self, k, d=None):
        return super().get(k.lower(), d)


@pytest.fixture
def csrf(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("AURIX_PUBLIC_URL", raising=False)
    return _load("_review_csrf", "core/csrf.py")


def test_csrf_same_origin_and_scripts_pass(csrf):
    ok = csrf.request_allowed
    assert ok("POST", _H(host="box:8000", origin="http://box:8000"), True)
    assert ok("POST", _H(host="box:8000", referer="http://box:8000/settings"), True)
    assert ok("POST", _H(host="box:8000"), True)                                  # curl / launcher: no Origin
    assert ok("GET", _H(host="box:8000", origin="https://evil.test"), True)       # reads are not checked
    assert ok("POST", _H(host="box:8000", origin="https://evil.test"), has_session_cookie=False)  # bearer / no cookie


def test_csrf_cross_origin_refused(csrf):
    ok = csrf.request_allowed
    assert not ok("POST", _H(host="localhost:8000", origin="http://localhost:9999"), True)   # other local port
    assert not ok("DELETE", _H(host="box:8000", origin="null"), True)
    assert not ok("PUT", _H(host="box:8000", origin="file://x"), True)
    assert not ok("POST", _H(host="box:8000", referer="https://evil.test/page"), True)


def test_csrf_proxy_and_configured_origins(csrf, monkeypatch):
    ok = csrf.request_allowed
    # Reverse proxy rewrote Host but forwarded the public one.
    assert ok("POST", _H(host="127.0.0.1:8000", x_forwarded_host="aurix.tailnet.test",
                         origin="https://aurix.tailnet.test"), True)
    assert ok("POST", _H(host="aurix.test:443", origin="https://aurix.test"), True)  # default port normalised
    assert not ok("POST", _H(host="127.0.0.1:8000", origin="https://aurix.tailnet.test"), True)
    monkeypatch.setenv("AURIX_PUBLIC_URL", "https://aurix.tailnet.test")
    assert ok("POST", _H(host="127.0.0.1:8000", origin="https://aurix.tailnet.test"), True)
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:7000, http://other.test")
    assert ok("POST", _H(host="127.0.0.1:8000", origin="http://other.test"), True)


def test_csrf_off_switch(csrf, monkeypatch):
    assert csrf.enabled()
    monkeypatch.setenv("AURIX_CSRF_ORIGIN_CHECK", "0")
    assert not csrf.enabled()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_secret_files_owner_only(mgr, tmp_path):
    mgr.create_session("owner", "correct horse 1")
    for f in ("auth.json", "sessions.json"):
        mode = stat.S_IMODE(os.stat(tmp_path / f).st_mode)
        assert mode == 0o600, (f, oct(mode))


@pytest.mark.skipif(os.name == "nt", reason="Windows os.replace refuses a target another thread is replacing")
def test_atomic_write_concurrent_threads_same_file(tmp_path):
    target = str(tmp_path / "s.json")
    errors = []

    def writer(i):
        try:
            for _ in range(40):
                atomic_io.atomic_write_json(target, {"writer": i, "pad": "x" * 2000})
        except Exception as e:  # pragma: no cover - the failure being pinned
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    data = json.loads(Path(target).read_text())
    assert data["pad"] == "x" * 2000
    assert not [p for p in os.listdir(tmp_path) if ".tmp." in p]


def test_atomic_write_removes_tmp_on_failure(tmp_path):
    target = tmp_path / "bad.json"
    with pytest.raises(TypeError):
        atomic_io.atomic_write_json(str(target), {1, 2})
    assert not target.exists()
    assert os.listdir(tmp_path) == []


def test_internal_tool_token_check():
    pytest.importorskip("starlette")
    mw = _load("_review_core_middleware", "core/middleware.py")
    assert mw.is_internal_tool_token(mw.INTERNAL_TOOL_TOKEN)
    assert not mw.is_internal_tool_token("")
    assert not mw.is_internal_tool_token(None)
    assert not mw.is_internal_tool_token(mw.INTERNAL_TOOL_TOKEN[:-1])


def test_ssh_option_hosts_refused():
    src = (ROOT / "src" / "builtin_actions.py").read_text(encoding="utf-8")
    # Exercise the helper without importing the heavy module.
    start = src.index("def _ssh_host_ok")
    end = src.index("\nasync def ", start)
    ns: dict = {}
    exec(src[start:end], ns)
    ok = ns["_ssh_host_ok"]
    assert ok("user@gpu-box") and ok("gpu-box") and ok("10.0.0.5")
    for bad in ("-oProxyCommand=calc", "host;id", "a b", "", "$(id)"):
        assert not ok(bad), bad
