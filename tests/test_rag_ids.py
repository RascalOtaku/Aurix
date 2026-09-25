"""RAG chunk ids must survive a restart (re-indexing is a no-op) and must not collide across owners/sources."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.rag_ids import stable_doc_id  # noqa: E402

PROBE = ("import sys; sys.path.insert(0, %r); from src.rag_ids import stable_doc_id; "
         "print(stable_doc_id('hello wiki', {'owner': 'rascal', 'source': '/aurix/wiki/a.md', 'chunk_id': 3}))" % ROOT)


class StableIdTests(unittest.TestCase):
    def test_same_chunk_same_id(self):
        m = {"owner": "rascal", "source": "/a.md", "chunk_id": 0}
        self.assertEqual(stable_doc_id("text", m), stable_doc_id("text", dict(m)))

    def test_identical_across_processes_with_different_hash_seeds(self):
        """The bug: Python's hash() is salted per process, so ids changed on every restart."""
        ids = set()
        for seed in ("1", "2", "random"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            ids.add(subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, check=True).stdout.strip())
        self.assertEqual(len(ids), 1, ids)
        self.assertEqual(ids.pop(), stable_doc_id("hello wiki", {"owner": "rascal", "source": "/aurix/wiki/a.md", "chunk_id": 3}))

    def test_owner_source_and_position_all_matter(self):
        base = stable_doc_id("same text", {"owner": "rascal", "source": "/a.md", "chunk_id": 0})
        self.assertNotEqual(base, stable_doc_id("same text", {"owner": "admin", "source": "/a.md", "chunk_id": 0}))
        self.assertNotEqual(base, stable_doc_id("same text", {"owner": "rascal", "source": "/b.md", "chunk_id": 0}))
        self.assertNotEqual(base, stable_doc_id("same text", {"owner": "rascal", "source": "/a.md", "chunk_id": 1}))
        self.assertNotEqual(base, stable_doc_id("other text", {"owner": "rascal", "source": "/a.md", "chunk_id": 0}))

    def test_separator_prevents_field_smearing(self):
        self.assertNotEqual(stable_doc_id("b", {"owner": "a"}), stable_doc_id("", {"owner": "a", "source": "b"}))
        self.assertNotEqual(stable_doc_id("x", {"owner": "ab", "source": "c"}), stable_doc_id("x", {"owner": "a", "source": "bc"}))

    def test_shape_and_missing_metadata(self):
        i = stable_doc_id("t")
        self.assertTrue(i.startswith("doc_") and len(i) == 36)
        self.assertEqual(stable_doc_id("t"), stable_doc_id("t", {}))
        self.assertEqual(stable_doc_id("t", None), i)
        self.assertTrue(stable_doc_id(None).startswith("doc_"))                # never raises


if __name__ == "__main__":
    unittest.main()
