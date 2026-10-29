"""Pure frontend modules that the Settings panel depends on, executed with node.

`integrationActions.js` exists because the api.js migration once dropped the per-type guards from the
integration delete handler: removing a calendar integration also cleared contacts, email accounts, MCP servers
and the vault. These tests run the real mapping under node and pin one request per type. Skips without node.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

_SCRIPT = """
import { integrationRemovalRequests as removal } from './static/js/integrationActions.js';
const types = ['api', 'caldav', 'contacts', 'carddav', 'email', 'mcp', 'vault', 'nonsense'];
const out = {};
for (const t of types) out[t] = removal(t, '42');
out.encoded = removal('api', 'a/b');
out.no_id = removal('email', null);
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not installed")
class IntegrationRemovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", _SCRIPT],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise AssertionError(f"node failed:\n{result.stderr}")
        cls.requests = json.loads(result.stdout.strip().splitlines()[-1])

    def one(self, kind):
        rows = self.requests[kind]
        self.assertEqual(len(rows), 1, f"{kind} must issue exactly one request, got {rows}")
        return rows[0]

    def test_each_type_only_touches_its_own_api(self):
        self.assertEqual(self.one("api"), {"method": "DELETE", "path": "/api/auth/integrations/42"})
        self.assertEqual(self.one("caldav")["path"], "/api/calendar/config")
        self.assertEqual(self.one("contacts"), {"method": "DELETE", "path": "/api/contacts/clear"})
        self.assertEqual(self.one("carddav")["path"], "/api/contacts/config")
        self.assertEqual(self.one("email"), {"method": "DELETE", "path": "/api/email/accounts/42"})
        self.assertEqual(self.one("mcp"), {"method": "DELETE", "path": "/api/mcp/servers/42"})
        self.assertEqual(self.one("vault"), {"method": "POST", "path": "/api/vault/logout"})

    def test_the_blanking_bodies_match_the_endpoints(self):
        self.assertEqual(self.one("caldav")["body"], {"url": "", "username": "", "password": ""})
        self.assertEqual(self.one("carddav")["body"],
                         {"carddav_url": "", "carddav_username": "", "carddav_password": ""})

    def test_an_unknown_type_is_a_no_op_never_a_blanket_clear(self):
        self.assertEqual(self.requests["nonsense"], [])

    def test_ids_are_url_encoded(self):
        self.assertEqual(self.requests["encoded"], [{"method": "DELETE", "path": "/api/auth/integrations/a%2Fb"}])
        self.assertEqual(self.requests["no_id"], [{"method": "DELETE", "path": "/api/email/accounts/"}])

    def test_the_settings_panel_uses_the_shared_mapping(self):
        source = (REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")
        self.assertIn("integrationRemovalRequests(type, id)", source)
        self.assertNotIn("'/api/contacts/clear'", source, "the delete mapping must stay in integrationActions.js")


if __name__ == "__main__":
    unittest.main()
