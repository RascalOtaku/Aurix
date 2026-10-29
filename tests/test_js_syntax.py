"""Every shipped frontend module must parse.

Pins the 2026-10-02 api.js migration regression: a bad try/catch edit left `static/js/settings.js` and
`static/js/admin.js` unparseable. A browser then refuses to evaluate them, and every module that imports
settings.js (chat renderer, model picker, slash commands, email library, the admin panel) fails with it.
`node --check` on each file is cheap and catches this before anyone opens the UI. Skips when node isn't installed.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

STATIC_JS = Path(__file__).resolve().parents[1] / "static" / "js"
STATIC = Path(__file__).resolve().parents[1] / "static"
NODE = shutil.which("node")


def _module_files():
    return sorted(p for p in STATIC_JS.rglob("*.js") if "lib" not in p.relative_to(STATIC_JS).parts)


class FrontendImportTests(unittest.TestCase):
    """A missing import target breaks a module at runtime without any syntax error, so check the paths too."""

    def test_every_relative_import_resolves(self):
        missing = []
        for path in _module_files():
            source = path.read_text(encoding="utf-8", errors="replace")
            specs = re.findall(r"\bfrom\s*['\"](\.[^'\"]+)['\"]", source)
            specs += re.findall(r"\bimport\s*\(\s*['\"](\.[^'\"]+)['\"]\s*\)", source)
            for spec in specs:
                if not (path.parent / spec).resolve().exists():
                    missing.append(f"{path.relative_to(STATIC_JS)} imports missing {spec}")
        self.assertEqual(missing, [])

    def test_every_script_tag_points_at_a_real_file(self):
        missing = []
        for page in sorted(STATIC.rglob("*.html")):
            source = page.read_text(encoding="utf-8", errors="replace")
            for src in re.findall(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", source, re.I):
                if src.startswith(("http:", "https:", "//")):
                    continue
                clean = src.split("?")[0].lstrip("/")
                target = Path(clean) if clean.startswith("static/") else page.parent / clean.lstrip("./")
                if not target.exists():
                    missing.append(f"{page.relative_to(STATIC)} references missing {src}")
        self.assertEqual(missing, [])

    def test_index_module_scripts_are_precached_for_offline(self):
        """sw.js says to keep PRECACHE in sync with index.html's top-level modules; drifted lists break offline use."""
        index = (STATIC / "index.html").read_text(encoding="utf-8", errors="replace")
        precache = set(re.findall(r"'(/static/[^']+)'", (STATIC / "sw.js").read_text(encoding="utf-8")))
        sources = []
        for attrs in re.findall(r"<script\b([^>]*)>", index, re.I):
            src = re.search(r"\bsrc\s*=\s*[\"']([^\"']+)[\"']", attrs, re.I)
            kind = re.search(r"\btype\s*=\s*[\"']([^\"']+)[\"']", attrs, re.I)
            if src and kind and kind.group(1).lower() == "module":
                sources.append(src.group(1))
        missing = sorted({src.split("?")[0] for src in sources if src.startswith("/static/")} - precache)
        self.assertEqual(missing, [], "index.html loads modules the service worker never precaches")


@unittest.skipUnless(NODE, "node is not installed")
class JavaScriptSyntaxTests(unittest.TestCase):
    def test_every_frontend_module_parses(self):
        files = _module_files()
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
