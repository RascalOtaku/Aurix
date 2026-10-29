"""Every shipped frontend module must parse.

Pins the 2026-10-02 api.js migration regression: a bad try/catch edit left `static/js/settings.js` and
`static/js/admin.js` unparseable. A browser then refuses to evaluate them, and every module that imports
settings.js (chat renderer, model picker, slash commands, email library, the admin panel) fails with it.
`node --check` on each file is cheap and catches this before anyone opens the UI. Skips when node isn't installed.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

STATIC_JS = Path(__file__).resolve().parents[1] / "static" / "js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class JavaScriptSyntaxTests(unittest.TestCase):
    def test_every_frontend_module_parses(self):
        files = sorted(p for p in STATIC_JS.rglob("*.js") if "lib" not in p.relative_to(STATIC_JS).parts)
        self.assertTrue(files, f"no JavaScript modules found under {STATIC_JS}")
        broken = []
        # Node treats a file named *.mjs as an ES module in every version that has --check; `node --check
        # file.js` would treat .js as CommonJS on Node < 22 and reject every `import` as a syntax error.
        with tempfile.TemporaryDirectory(prefix="aurix_js_check_") as tmp:
            for path in files:
                staged = Path(tmp) / (path.name + ".mjs")
                staged.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                result = subprocess.run([NODE, "--check", str(staged)], capture_output=True, text=True, timeout=30)
                if result.returncode != 0:
                    reason = next((ln.strip() for ln in result.stderr.splitlines() if "Error" in ln), "unknown error")
                    broken.append(f"{path.relative_to(STATIC_JS)}: {reason}")
        self.assertEqual(broken, [], "these frontend modules do not parse:\n" + "\n".join(broken))


if __name__ == "__main__":
    unittest.main()
