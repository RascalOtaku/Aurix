"""Regression 2026-09-29: routes/shell_routes.py::_require_admin began with a bare `return` and forced current_user="rascal", so ANY
logged-in account could run shell commands and pip installs via /api/shell/* and /api/cookbook/packages/install. Pin the guard."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from routes import shell_routes  # noqa: E402


class _Auth:
    def __init__(self, admins):
        self.admins = set(admins)

    def is_admin(self, user):
        return user in self.admins


def _req(user, auth=True):
    app = types.SimpleNamespace(state=types.SimpleNamespace(auth_manager=_Auth({"admin", "rascal"}) if auth else None))
    return types.SimpleNamespace(app=app, state=types.SimpleNamespace(current_user=user))


class RequireAdmin(unittest.TestCase):
    def test_admins_pass(self):
        for user in ("admin", "rascal"):
            shell_routes._require_admin(_req(user))

    def test_a_non_admin_account_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            shell_routes._require_admin(_req("guest"))
        self.assertEqual(cm.exception.status_code, 403)

    def test_no_identity_is_refused(self):
        for user in (None, ""):
            with self.assertRaises(HTTPException):
                shell_routes._require_admin(_req(user))

    def test_the_caller_is_never_rewritten(self):
        r = _req("guest")
        with self.assertRaises(HTTPException):
            shell_routes._require_admin(r)
        self.assertEqual(r.state.current_user, "guest")          # the old code overwrote this with "rascal"

    def test_internal_tool_loopback_and_no_auth_dev_mode_are_unchanged(self):
        shell_routes._require_admin(_req("internal-tool"))
        shell_routes._require_admin(_req("anyone", auth=False))

    def test_every_dangerous_route_calls_the_guard(self):
        import inspect
        src = inspect.getsource(shell_routes)
        for route in ('"/api/shell/exec"', '"/api/shell/stream"', '"/api/cookbook/packages/install"'):
            body = src.split(route, 1)[1].split("@router.", 1)[0]
            self.assertIn("_require_admin(request)", body, route)


if __name__ == "__main__":
    unittest.main()
