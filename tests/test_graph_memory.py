"""Graph memory: pure parsing/linking, idempotent ingest, fail-soft reads, no credentials in code."""
import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import graph_memory as gm  # noqa: E402

WIKI_A = """---
title: Reynolds Gang
type: people
created: 2026-04-21
updated: 2026-05-03
tags: [reynolds-gang, history, colorado]
---
# [[Reynolds Gang]]

Confederate-sympathizing outlaws active in Colorado in 1864. See [[Geneva Gulch]] and [[brain/wiki/concepts/colorado-ghost-towns|ghost towns]].

Later research links back to [[Reynolds Gang#Timeline]] and to [[Some Missing Topic]].
"""
WIKI_B = "# Geneva Gulch\n\nA gold-mining gulch. Related: [[Reynolds Gang]]."
WIKI_C = "---\ntags: history\n---\n# Colorado ghost towns\n\nA list."


class FakeSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        if self.driver.fail:
            raise RuntimeError("boom: connection refused")
        self.driver.calls.append((cypher, params))
        return list(self.driver.results.pop(0)) if self.driver.results else []


class FakeDriver:
    def __init__(self, results=None, fail=False):
        self.calls, self.results, self.fail, self.closed = [], list(results or []), fail, False

    def session(self, database=None):
        return FakeSession(self)

    def close(self):
        self.closed = True


def embed(texts):
    return [[float(len(t)), 0.5, 0.25] for t in texts]


class ParsingTests(unittest.TestCase):
    def test_page_metadata_title_tags_and_links(self):
        p = gm.parse_page("wiki/people/reynolds-gang.md", WIKI_A)
        self.assertEqual(p.title, "Reynolds Gang")
        self.assertEqual(p.type, "people")
        self.assertEqual(p.updated, "2026-05-03")
        self.assertEqual(p.tags, ["reynolds-gang", "history", "colorado"])
        self.assertEqual(p.links, ["Reynolds Gang", "Geneva Gulch", "brain/wiki/concepts/colorado-ghost-towns", "Some Missing Topic"])

    def test_title_falls_back_to_h1_then_filename(self):
        self.assertEqual(gm.parse_page("wiki/x.md", WIKI_B).title, "Geneva Gulch")
        self.assertEqual(gm.parse_page("wiki/some_page-name.md", "no heading here").title, "some page name")
        self.assertEqual(gm.parse_page("wiki/x.md", "").links, [])

    def test_tags_scalar_and_list(self):
        self.assertEqual(gm.parse_page("k.md", WIKI_C).tags, ["history"])
        self.assertEqual(gm.parse_page("k.md", "---\ntags: [#a, b ]\n---\nx").tags, ["a", "b"])

    def test_chunking_respects_size_and_never_loses_text(self):
        body = "\n\n".join(f"paragraph {i} " + "word " * 60 for i in range(12))
        chunks = gm.chunk_text(body, max_chars=500)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        joined = " ".join(chunks)
        for i in range(12):
            self.assertIn(f"paragraph {i} ", joined)
        self.assertEqual(gm.chunk_text("x" * 2500, max_chars=1000), ["x" * 1000, "x" * 1000, "x" * 500])
        self.assertEqual(gm.chunk_text("   \n\n  "), [])

    def test_chunk_ids_are_stable_and_position_sensitive(self):
        a = gm.chunk_id("p.md", 0, "text")
        self.assertEqual(a, gm.chunk_id("p.md", 0, "text"))
        self.assertNotEqual(a, gm.chunk_id("p.md", 1, "text"))
        self.assertNotEqual(a, gm.chunk_id("q.md", 0, "text"))


class LinkResolutionTests(unittest.TestCase):
    def setUp(self):
        self.pages = [gm.parse_page("wiki/people/reynolds-gang.md", WIKI_A),
                      gm.parse_page("wiki/research/geneva-gulch.md", WIKI_B),
                      gm.parse_page("wiki/concepts/colorado-ghost-towns.md", WIKI_C)]
        self.links = gm.resolve_links(self.pages)

    def test_resolves_by_title_stem_and_path_and_ignores_alias_and_heading(self):
        got = {l["key"]: l for l in self.links["wiki/people/reynolds-gang.md"]}
        self.assertIn("wiki/research/geneva-gulch.md", got)                       # by title
        self.assertIn("wiki/concepts/colorado-ghost-towns.md", got)               # by path-ish target with |alias
        self.assertFalse(got["wiki/research/geneva-gulch.md"]["stub"])

    def test_self_links_and_duplicates_dropped_and_unknowns_become_stubs(self):
        keys = [l["key"] for l in self.links["wiki/people/reynolds-gang.md"]]
        self.assertNotIn("wiki/people/reynolds-gang.md", keys)                    # [[Reynolds Gang]] links to itself
        self.assertEqual(len(keys), len(set(keys)))
        stub = next(l for l in self.links["wiki/people/reynolds-gang.md"] if l["stub"])
        self.assertEqual((stub["key"], stub["title"]), ("stub:some-missing-topic", "Some Missing Topic"))

    def test_links_are_directional_in_data_but_back_link_resolves(self):
        self.assertEqual([l["key"] for l in self.links["wiki/research/geneva-gulch.md"]], ["wiki/people/reynolds-gang.md"])


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver()
        self.graph = gm.Graph(self.driver)
        self.pages = [gm.parse_page("wiki/people/reynolds-gang.md", WIKI_A), gm.parse_page("wiki/research/geneva-gulch.md", WIKI_B)]

    def cyphers(self):
        return [c for c, _ in self.driver.calls]

    def test_schema_statements_are_idempotent_and_cover_vector_index(self):
        gm.ensure_schema(self.graph)
        self.assertEqual(len(self.driver.calls), len(gm.SCHEMA))
        self.assertTrue(all("IF NOT EXISTS" in c for c in self.cyphers()))
        self.assertTrue(any("VECTOR INDEX" in c and "384" in c for c in self.cyphers()))

    def test_ingest_replaces_chunks_and_links_so_reruns_do_not_accumulate(self):
        stats = gm.ingest_pages(self.graph, self.pages, embed)
        self.assertEqual(stats["pages"], 2)
        self.assertGreaterEqual(stats["chunks"], 2)
        self.assertEqual(stats["stubs"], 2)                                        # "Some Missing Topic" + the un-ingested ghost-towns page
        cy = self.cyphers()
        self.assertEqual(sum("DETACH DELETE c" in c for c in cy), 2)              # old chunks removed per page
        self.assertEqual(sum("DELETE r" in c for c in cy), 2)                     # old links removed per page
        self.assertTrue(all("MERGE" in c or "DELETE" in c for c in cy))           # no bare CREATE (would duplicate)
        self.assertFalse(any(re.search(r"\bCREATE \(", c) for c in cy))

    def test_ingest_is_fully_parameterised(self):
        gm.ingest_pages(self.graph, self.pages, embed)
        secret = "Confederate-sympathizing"
        for cypher, _params in self.driver.calls:
            self.assertNotIn(secret, cypher)                                      # page text only ever travels as a parameter

    def test_embeddings_are_plain_floats(self):
        gm.ingest_pages(self.graph, self.pages[:1], lambda ts: [[1, 2, 3] for _ in ts])
        rows = next(p["rows"] for c, p in self.driver.calls if "UNWIND $rows" in c)
        self.assertEqual(rows[0]["embedding"], [1.0, 2.0, 3.0])
        self.assertTrue(all(isinstance(x, float) for x in rows[0]["embedding"]))

    def test_ingest_errors_are_loud(self):
        with self.assertRaises(RuntimeError):
            gm.ingest_pages(gm.Graph(FakeDriver(fail=True)), self.pages, embed)

    def test_seed_profile_only_writes_what_it_is_given(self):
        gm.seed_profile(self.graph, "Rascal", "Colorado")
        _, params = self.driver.calls[-1]
        self.assertEqual(params["props"], {"full_name": "Rascal", "location": "Colorado"})
        gm.seed_profile(self.graph, "Rascal", "")
        self.assertEqual(self.driver.calls[-1][1]["props"], {"full_name": "Rascal"})

    def test_sync_projects(self):
        P = type("P", (), {})
        p1, p2 = P(), P()
        p1.id, p1.name, p1.status, p1.note = "p-aaaaaa", "Bike rebuild", "active", "waiting on parts"
        p2.id, p2.name, p2.status, p2.note = "p-bbbbbb", "Old", "done", ""
        self.assertEqual(gm.sync_projects(self.graph, [p1, p2]), 2)
        rows = self.driver.calls[-1][1]["rows"]
        self.assertEqual(rows[0], {"id": "project:p-aaaaaa", "name": "Bike rebuild", "status": "active", "note": "waiting on parts"})
        self.assertEqual(gm.sync_projects(self.graph, []), 0)


class ReadTests(unittest.TestCase):
    def test_profile_text(self):
        d = FakeDriver(results=[[{"core": {"full_name": "Rascal", "location": "Acres Green, CO", "philosophy": "build it right"},
                                   "projects": ["Bike rebuild", None], "prefs": ["concise", None]}]])
        text = gm.profile_context(gm.Graph(d))
        self.assertEqual(text, "RASCAL LIVE PROFILE:\nOwner: Rascal · Acres Green, CO\nPhilosophy: build it right\n"
                               "Projects: Bike rebuild\nPrefs: concise")

    def test_no_profile_or_empty_profile_is_empty_string(self):
        self.assertEqual(gm.profile_context(gm.Graph(FakeDriver())), "")
        self.assertEqual(gm.profile_context(gm.Graph(FakeDriver(results=[[{"core": {}, "projects": [], "prefs": []}]]))), "")
        self.assertEqual(gm.profile_context(gm.Graph(FakeDriver(results=[[{"core": {"profile_id": "x"}, "projects": [], "prefs": []}]]))), "")

    def test_reads_fail_soft_when_the_database_is_down(self):
        down = gm.Graph(FakeDriver(fail=True))
        self.assertEqual(gm.profile_context(down), "")
        self.assertEqual(gm.related_context(["wiki/a.md"], graph=down), "")
        self.assertIsNone(down.read("RETURN 1"))

    def test_related_context_formats_neighbours_and_dedupes_keys(self):
        d = FakeDriver(results=[[{"key": "wiki/research/geneva-gulch.md", "title": "Geneva Gulch", "lead": "A  gold-mining\ngulch.", "shared": 2}]])
        text = gm.related_context(["wiki/a.md", "wiki/a.md", "", "wiki/b.md"], graph=gm.Graph(d))
        self.assertIn("[Geneva Gulch] (wiki/research/geneva-gulch.md)", text)
        self.assertIn("A gold-mining gulch.", text)
        self.assertEqual(d.calls[0][1]["keys"], ["wiki/a.md", "wiki/b.md"])
        self.assertEqual(gm.related_context([], graph=gm.Graph(FakeDriver())), "")

    def test_page_key_from_rag_source(self):
        self.assertEqual(gm.page_key_from_source("/aurix/wiki/concepts/x.md"), "wiki/concepts/x.md")
        self.assertEqual(gm.page_key_from_source("/aurix/memory/longterm/a.md"), "memory/longterm/a.md")
        self.assertEqual(gm.page_key_from_source("wiki\\y.md"), "wiki/y.md")


class ConfigTests(unittest.TestCase):
    def test_off_without_uri_and_password(self):
        self.assertIsNone(gm.Graph.from_env({}))
        self.assertIsNone(gm.Graph.from_env({"NEO4J_URI": "bolt://neo4j:7687"}))
        self.assertIsNone(gm.Graph.from_env({"NEO4J_PASSWORD": "x"}))

    def test_missing_driver_package_is_soft(self):
        with mock.patch.dict(sys.modules, {"neo4j": None}):
            self.assertIsNone(gm.Graph.from_env({"NEO4J_URI": "bolt://x:1", "NEO4J_PASSWORD": "p"}))

    def test_no_credentials_or_hardcoded_hosts_in_source(self):
        src = open(os.path.join(ROOT, "src", "graph_memory.py"), encoding="utf-8").read()
        self.assertNotIn("aurix_cortex", src)
        self.assertNotRegex(src, r"auth\s*=\s*\(\s*['\"]neo4j['\"]\s*,\s*['\"]")
        self.assertNotIn("localhost:7687", src)
        for path in ("src/chat_processor.py",):
            code = open(os.path.join(ROOT, path), encoding="utf-8").read()
            self.assertNotIn("aurix_cortex", code, path)
            self.assertNotIn("bolt://localhost", code, path)


if __name__ == "__main__":
    unittest.main()
