#!/usr/bin/env python3
"""Build/refresh the knowledge graph (Neo4j) from the mounted AURIX brain.

    docker compose exec odysseus python scripts/graph_ingest.py             # dry run: parse + report only
    docker compose exec odysseus python scripts/graph_ingest.py --apply     # write to Neo4j (safe to re-run)

What it does: creates the schema (constraints + vector/fulltext indexes), reads every markdown page under the brain's
wiki and long-term memory, writes Page/Chunk/Tag nodes with embeddings and LINKS_TO edges from [[wikilinks]], seeds the
owner's `core_identity` node with the facts below (only those), and mirrors the Unfinished Projects registry.
Re-running replaces each page's chunks and links; nothing accumulates.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import graph_memory as gm  # noqa: E402

BRAIN = os.environ.get("AURIX_BRAIN", "/aurix")
SOURCES = [("wiki", os.path.join(BRAIN, "wiki")), ("memory/longterm", os.path.join(BRAIN, "memory", "longterm"))]
# The owner's profile node. Set AURIX_OWNER_NAME / AURIX_OWNER_LOCATION in .env (the repository is public).
OWNER = {"full_name": os.environ.get("AURIX_OWNER_NAME", "Owner"), "location": os.environ.get("AURIX_OWNER_LOCATION", "")}


def collect_pages():
    pages = []
    for prefix, root in SOURCES:
        if not os.path.isdir(root):
            print(f"  (missing) {root}")
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if not name.lower().endswith(".md"):
                    continue
                full = os.path.join(dirpath, name)
                rel = prefix + "/" + os.path.relpath(full, root).replace(os.sep, "/")
                try:
                    with open(full, encoding="utf-8", errors="replace") as f:
                        pages.append(gm.parse_page(rel, f.read()))
                except OSError as e:
                    print(f"  skipped {rel}: {e}")
    return pages


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write to Neo4j (default: dry run)")
    a = ap.parse_args(argv)

    pages = collect_pages()
    links = gm.resolve_links(pages)
    n_links = sum(len(v) for v in links.values())
    n_stubs = len({l["key"] for v in links.values() for l in v if l["stub"]})
    n_chunks = sum(len(gm.chunk_text(p.body)) for p in pages)
    print(f"{len(pages)} pages, {n_chunks} chunks, {n_links} links ({n_stubs} distinct unresolved/stub targets)")
    if not a.apply:
        print("dry run - add --apply to write")
        return 0

    graph = gm.Graph.from_env()
    if graph is None:
        print("Neo4j is not configured (need NEO4J_URI and NEO4J_PASSWORD) or the neo4j package is missing")
        return 2
    from src.embeddings import get_embedding_client
    client = get_embedding_client()
    dim = client.get_sentence_embedding_dimension()
    if dim != gm.EMBEDDING_DIMENSIONS:
        print(f"embedding dimension is {dim} but the vector index is {gm.EMBEDDING_DIMENSIONS}: "
              "change EMBEDDING_DIMENSIONS in src/graph_memory.py (and re-create the index) before ingesting")
        return 3

    def embed(texts):
        return client.encode(texts, normalize_embeddings=True).tolist()

    gm.ensure_schema(graph)
    stats = gm.ingest_pages(graph, pages, embed)
    print("ingested:", stats)
    gm.seed_profile(graph, **OWNER)
    try:
        from src.foundation.projects import Registry
        n = gm.sync_projects(graph, [p for p in Registry().load()["projects"]])
        print(f"profile seeded; {n} project(s) mirrored")
    except Exception as e:
        print(f"profile seeded; projects not mirrored ({type(e).__name__}: {e})")
    counts = graph.run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC")
    print("graph now:", {r["label"]: r["n"] for r in counts})
    graph.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
