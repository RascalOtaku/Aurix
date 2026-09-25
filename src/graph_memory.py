"""src/graph_memory.py - AURIX's knowledge graph (Neo4j): pages, links, chunks, and the owner's profile.

Why a graph next to the vector store: vector search finds the passages closest to a question; it cannot follow
"Reynolds Gang" -> "Geneva Gulch" -> "Colorado ghost towns". The wiki already has [[wikilinks]]; this turns them
into edges so a retrieved page also brings its neighbours (GraphRAG-lite, no LLM in the loop).

    (:Page {key,title,type,updated,stub})-[:HAS_CHUNK]->(:Chunk {id,text,pos,embedding})
    (:Page)-[:LINKS_TO]->(:Page)        from [[wikilinks]] (unresolved targets become stub pages)
    (:Page)-[:TAGGED]->(:Tag {name})
    (:RascalProfile {profile_id:'core_identity'})-[:HAS_PROJECT|HAS_PREFERENCE]->(:RascalProfile)

Design rules (learned from what the previous Neo4j wiring did wrong):
  * NO credentials in code: NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD come from the environment only.
  * NEVER on the chat critical path: short timeouts, every read fails soft (returns "" / []), and failures are
    logged once instead of silently swallowed forever.
  * Writes (ingest, seeding) raise loudly - they run from scripts, where an error should be seen.
  * Retrieved text is untrusted data; callers wrap it with prompt_security.untrusted_context_message.

The parsing/chunking/link-resolution below is pure and unit-tested without a database.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

EMBEDDING_DIMENSIONS = 384                       # sentence-transformers/all-MiniLM-L6-v2 (FastEmbed default)
MAX_CHUNK_CHARS = 900
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.M)

SCHEMA = [
    "CREATE CONSTRAINT profile_id IF NOT EXISTS FOR (n:RascalProfile) REQUIRE n.profile_id IS UNIQUE",
    "CREATE CONSTRAINT page_key IF NOT EXISTS FOR (p:Page) REQUIRE p.key IS UNIQUE",
    "CREATE CONSTRAINT tag_name IF NOT EXISTS FOR (t:Tag) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
    ("CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS FOR (c:Chunk) ON (c.embedding) "
     "OPTIONS {indexConfig: {`vector.dimensions`: %d, `vector.similarity_function`: 'cosine'}}" % EMBEDDING_DIMENSIONS),
    "CREATE FULLTEXT INDEX chunk_text IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text]",
]


# ---------------------------------------------------------------------------
# pure parsing
# ---------------------------------------------------------------------------

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


@dataclass
class ParsedPage:
    key: str                       # relative path, e.g. "wiki/concepts/chromadb.md"
    title: str
    type: str = ""
    updated: str = ""
    tags: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)      # raw wikilink targets
    body: str = ""


def _front_matter(text: str):
    lines = (text or "").splitlines()
    meta: Dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 40)):
            if lines[i].strip() == "---":
                for raw in lines[1:i]:
                    k, sep, v = raw.partition(":")
                    if sep:
                        meta[k.strip().lower()] = v.strip()
                return meta, "\n".join(lines[i + 1:])
    return meta, "\n".join(lines)


def parse_page(key: str, text: str) -> ParsedPage:
    meta, body = _front_matter(text)
    m = _H1.search(body)
    stem = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    title = meta.get("title") or (m.group(1) if m else stem.replace("_", " ").replace("-", " "))
    tags_raw = meta.get("tags", "")
    tags = [t.strip().strip("#") for t in re.split(r"[,\[\]]", tags_raw) if t.strip().strip("#")]
    links = []
    for target in _WIKILINK.findall(body):
        t = target.strip()
        if t and t not in links:
            links.append(t)
    return ParsedPage(key=key, title=re.sub(r"\[\[|\]\]", "", title).strip(), type=meta.get("type", ""),
                      updated=meta.get("updated", "") or meta.get("created", ""), tags=tags, links=links, body=body.strip())


def chunk_text(body: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    """Paragraph-aware chunks up to `max_chars` (a single huge paragraph is hard-split)."""
    chunks: List[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", body or ""):
        para = para.strip()
        if not para:
            continue
        while len(para) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(para[:max_chars])
            para = para[max_chars:]
        if cur and len(cur) + len(para) + 2 > max_chars:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


def chunk_id(page_key: str, pos: int, text: str) -> str:
    return hashlib.sha256(f"{page_key}\x1f{pos}\x1f{text}".encode("utf-8")).hexdigest()[:32]


def resolve_links(pages: Sequence[ParsedPage]) -> Dict[str, List[Dict[str, Any]]]:
    """page key -> [{key, title, stub}] for every wikilink. A link resolves to a page whose title, filename stem or
    path-slug matches (case/punctuation-insensitive); anything unresolved becomes a stub page (`stub:<slug>`)."""
    index: Dict[str, ParsedPage] = {}
    for p in pages:
        stem = p.key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        for s in {slug(p.title), slug(stem), slug(p.key.rsplit(".", 1)[0])}:
            index.setdefault(s, p)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for p in pages:
        resolved: List[Dict[str, Any]] = []
        seen = set()
        for target in p.links:
            hit = index.get(slug(target)) or index.get(slug(target.rsplit("/", 1)[-1]))
            if hit is not None:
                if hit.key == p.key or hit.key in seen:
                    continue
                seen.add(hit.key)
                resolved.append({"key": hit.key, "title": hit.title, "stub": False})
            else:
                skey = "stub:" + slug(target.rsplit("/", 1)[-1])
                if skey != "stub:" and skey not in seen:
                    seen.add(skey)
                    resolved.append({"key": skey, "title": target.rsplit("/", 1)[-1].strip(), "stub": True})
        out[p.key] = resolved
    return out


# ---------------------------------------------------------------------------
# the connection
# ---------------------------------------------------------------------------

class Graph:
    """Thin wrapper over a neo4j driver. `driver` is injectable so tests need no database."""

    def __init__(self, driver: Any, database: str = "neo4j"):
        self._driver = driver
        self.database = database

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> Optional["Graph"]:
        env = os.environ if env is None else env
        uri, password = env.get("NEO4J_URI", ""), env.get("NEO4J_PASSWORD", "")
        if not uri or not password:
            return None
        try:
            from neo4j import GraphDatabase
            # A fresh graph has no HAS_PROJECT/HAS_PREFERENCE yet; the server would warn about those on every chat
            # turn ("unknown relationship type"), which is noise, not a fault.
            logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
            kwargs = {"connection_timeout": 3.0, "max_connection_lifetime": 600}
            try:
                driver = GraphDatabase.driver(uri, auth=(env.get("NEO4J_USER", "neo4j"), password),
                                              notifications_min_severity="OFF", **kwargs)
            except TypeError:                                   # driver older than 5.7 has no such option
                driver = GraphDatabase.driver(uri, auth=(env.get("NEO4J_USER", "neo4j"), password), **kwargs)
        except Exception as e:                                  # package missing or bad URI
            _warn_once("driver", f"Neo4j driver unavailable: {type(e).__name__}: {e}")
            return None
        return cls(driver, env.get("NEO4J_DATABASE", "neo4j"))

    def run(self, cypher: str, **params) -> List[Dict[str, Any]]:
        """Run a query; raises on failure (ingest/seed use this)."""
        with self._driver.session(database=self.database) as session:
            return [dict(r) for r in session.run(cypher, **params)]

    def read(self, cypher: str, **params) -> Optional[List[Dict[str, Any]]]:
        """Fail-soft read for the chat path: None on any error (logged once per error text)."""
        try:
            return self.run(cypher, **params)
        except Exception as e:
            _warn_once("read", f"Neo4j read failed: {type(e).__name__}: {str(e)[:120]}")
            return None

    def close(self) -> None:
        try:
            self._driver.close()
        except Exception:
            pass


_warned: Dict[str, float] = {}


def _warn_once(kind: str, message: str, every: float = 900.0) -> None:
    key = f"{kind}:{message}"
    now = time.time()
    if now - _warned.get(key, 0) > every:
        _warned[key] = now
        logger.warning(message)


_singleton: Dict[str, Any] = {"graph": None, "at": 0.0}


def get_graph(retry_seconds: float = 30.0) -> Optional[Graph]:
    """Process-wide Graph (or None when Neo4j is not configured/available); re-tries at most every `retry_seconds`."""
    now = time.time()
    if _singleton["graph"] is not None:
        return _singleton["graph"]
    if now - _singleton["at"] < retry_seconds:
        return None
    _singleton["at"] = now
    _singleton["graph"] = Graph.from_env()
    return _singleton["graph"]


# ---------------------------------------------------------------------------
# schema, ingest, seeding (raise on failure)
# ---------------------------------------------------------------------------

def ensure_schema(graph: Graph) -> None:
    for stmt in SCHEMA:
        graph.run(stmt)


def ingest_pages(graph: Graph, pages: Sequence[ParsedPage], embed: Callable[[List[str]], Any],
                 batch: int = 64) -> Dict[str, int]:
    """Idempotently write pages, chunks (with embeddings), tags and links. Re-running replaces a page's chunks."""
    links = resolve_links(pages)
    stats = {"pages": 0, "chunks": 0, "links": 0, "stubs": 0}
    for p in pages:
        graph.run("MERGE (p:Page {key:$key}) SET p.title=$title, p.type=$type, p.updated=$updated, p.stub=false",
                  key=p.key, title=p.title, type=p.type, updated=p.updated)
        if p.tags:
            graph.run("MATCH (p:Page {key:$key}) UNWIND $tags AS t MERGE (g:Tag {name:t}) MERGE (p)-[:TAGGED]->(g)",
                      key=p.key, tags=p.tags)
        graph.run("MATCH (p:Page {key:$key})-[:HAS_CHUNK]->(c:Chunk) DETACH DELETE c", key=p.key)
        texts = chunk_text(p.body)
        rows = []
        for i in range(0, len(texts), batch):
            part = texts[i:i + batch]
            vectors = embed(part)
            for j, (text, vec) in enumerate(zip(part, vectors)):
                pos = i + j
                rows.append({"id": chunk_id(p.key, pos, text), "text": text, "pos": pos,
                             "embedding": [float(x) for x in vec]})
        if rows:
            graph.run("MATCH (p:Page {key:$key}) UNWIND $rows AS r "
                      "MERGE (c:Chunk {id:r.id}) SET c.text=r.text, c.pos=r.pos, c.embedding=r.embedding "
                      "MERGE (p)-[:HAS_CHUNK]->(c)", key=p.key, rows=rows)
        graph.run("MATCH (p:Page {key:$key})-[r:LINKS_TO]->() DELETE r", key=p.key)
        targets = links.get(p.key, [])
        if targets:
            graph.run("MATCH (a:Page {key:$key}) UNWIND $links AS l "
                      "MERGE (b:Page {key:l.key}) ON CREATE SET b.title=l.title, b.stub=l.stub "
                      "MERGE (a)-[:LINKS_TO]->(b)", key=p.key, links=targets)
            stats["links"] += len(targets)
            stats["stubs"] += sum(1 for t in targets if t["stub"])
        stats["pages"] += 1
        stats["chunks"] += len(rows)
    return stats


def seed_profile(graph: Graph, full_name: str, location: str = "", **extra: str) -> None:
    """Create/update the owner's `core_identity` node (only what is passed - never invents facts)."""
    props = {"full_name": full_name, "location": location, **extra}
    graph.run("MERGE (n:RascalProfile {profile_id:'core_identity'}) SET n += $props, n.updated_at=$now",
              props={k: v for k, v in props.items() if v}, now=time.strftime("%Y-%m-%dT%H:%M:%S"))


def sync_projects(graph: Graph, projects: Iterable[Any]) -> int:
    """Mirror the Unfinished Projects registry onto core_identity -[:HAS_PROJECT]-> (name, status, note)."""
    rows = [{"id": f"project:{p.id}", "name": p.name, "status": p.status, "note": p.note} for p in projects]
    if not rows:
        return 0
    graph.run("MERGE (core:RascalProfile {profile_id:'core_identity'}) WITH core UNWIND $rows AS r "
              "MERGE (n:RascalProfile {profile_id:r.id}) SET n.name=r.name, n.status=r.status, n.value=r.note, n.type='project' "
              "MERGE (core)-[:HAS_PROJECT]->(n)", rows=rows)
    return len(rows)


# ---------------------------------------------------------------------------
# reads for the chat path (fail soft)
# ---------------------------------------------------------------------------

PROFILE_QUERY = (
    "MATCH (core:RascalProfile {profile_id:'core_identity'}) "
    "OPTIONAL MATCH (core)-[:HAS_PROJECT]->(proj:RascalProfile) WHERE proj.status IS NULL OR proj.status <> 'done' "
    "OPTIONAL MATCH (core)-[:HAS_PREFERENCE]->(pref:RascalProfile) "
    "RETURN core, collect(DISTINCT proj.name) AS projects, collect(DISTINCT pref.value) AS prefs")


def profile_context(graph: Optional[Graph] = None) -> str:
    """The 'RASCAL LIVE PROFILE' system text, or '' if the graph is off / has no profile yet."""
    graph = graph or get_graph()
    if graph is None:
        return ""
    rows = graph.read(PROFILE_QUERY)
    if not rows:
        return ""
    core = dict(rows[0].get("core") or {})
    if not core:
        return ""
    parts = ["RASCAL LIVE PROFILE:"]
    who = " · ".join(x for x in (core.get("full_name"), core.get("location")) if x)
    if who:
        parts.append(f"Owner: {who}")
    if core.get("philosophy"):
        parts.append(f"Philosophy: {core['philosophy']}")
    projects = [x for x in rows[0].get("projects", []) if x]
    if projects:
        parts.append("Projects: " + ", ".join(projects))
    prefs = [x for x in rows[0].get("prefs", []) if x]
    if prefs:
        parts.append("Prefs: " + " | ".join(prefs))
    return "\n".join(parts) if len(parts) > 1 else ""


RELATED_QUERY = (
    "MATCH (p:Page) WHERE p.key IN $keys "
    "MATCH (p)-[:LINKS_TO]-(n:Page) WHERE n.stub = false AND NOT n.key IN $keys "
    "OPTIONAL MATCH (n)-[:HAS_CHUNK]->(c:Chunk) WHERE c.pos = 0 "
    "RETURN n.key AS key, n.title AS title, head(collect(c.text)) AS lead, count(DISTINCT p) AS shared "
    "ORDER BY shared DESC, title LIMIT $limit")


def related_context(page_keys: Sequence[str], limit: int = 4, max_chars: int = 500,
                    graph: Optional[Graph] = None) -> str:
    """Linked neighbours of the pages a vector search just returned, as one text block ('' if none/off)."""
    graph = graph or get_graph()
    keys = [k for k in dict.fromkeys(page_keys) if k]
    if graph is None or not keys:
        return ""
    rows = graph.read(RELATED_QUERY, keys=keys, limit=limit)
    if not rows:
        return ""
    lines = []
    for r in rows:
        lead = " ".join(str(r.get("lead") or "").split())[:max_chars]
        lines.append(f"[{r.get('title')}] ({r.get('key')})" + (f"\n{lead}" if lead else ""))
    return "Pages linked to the results above:\n\n" + "\n\n".join(lines)


def page_key_from_source(source: str) -> str:
    """'/aurix/wiki/concepts/x.md' (a RAG chunk's source path) -> 'wiki/concepts/x.md' (the graph's page key)."""
    s = (source or "").replace("\\", "/")
    if s.startswith("/aurix/"):
        return s[len("/aurix/"):]
    return s.lstrip("/")
