#!/usr/bin/env python3
"""Index the mounted AURIX brain (wiki + long-term memory) into the vector RAG store.

    docker compose exec odysseus python scripts/index_brain.py            # dry run: shows what it would index
    docker compose exec odysseus python scripts/index_brain.py --apply    # index it (safe to re-run: ids are stable)

The brain is mounted read-only at /aurix (docker-compose.yml). Chunks are tagged with an owner so the normal
per-user RAG retrieval finds them; by default it indexes for every owner listed with --owner (default: rascal, admin).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DIRS = ["/aurix/wiki", "/aurix/memory/longterm"]


def count_files(directory):
    n = 0
    for _root, _dirs, files in os.walk(directory):
        n += sum(1 for f in files if f.lower().endswith((".md", ".txt")))
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually index (default is a dry run)")
    ap.add_argument("--dir", action="append", help="directory to index (repeatable); default: the mounted wiki + longterm")
    ap.add_argument("--owner", action="append", help="owner tag for the chunks (repeatable); default: rascal, admin")
    a = ap.parse_args(argv)
    dirs = a.dir or DEFAULT_DIRS
    owners = a.owner or ["rascal", "admin"]

    for d in dirs:
        print(f"{d}: {'missing' if not os.path.isdir(d) else str(count_files(d)) + ' text files'}")
    if not a.apply:
        print("dry run - add --apply to index")
        return 0

    from src.rag_singleton import get_rag_manager
    rag = get_rag_manager()
    if rag is None:
        print("RAG store is not available (is the chromadb container up?)")
        return 2
    before = rag.get_stats()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for owner in owners:
            res = rag.index_personal_documents(d, owner=owner)
            print(f"  {d} as {owner}: {res.get('message')} (failed: {res.get('failed_count', 0)})")
    print("stats before:", before)
    print("stats after: ", rag.get_stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
