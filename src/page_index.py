"""src/page_index.py - vectorless, reasoning-based retrieval over the wiki (the PageIndex idea).

PageIndex (github.com/VectifyAI/PageIndex, MIT) replaces "embed chunks, take the nearest k" with "build a
table-of-contents tree from the document's own structure, let the model reason its way down it". Aurix's wiki
is already structured (folders -> pages -> markdown headings), so the tree is free: no embeddings, no LLM to
build it, nothing to re-index when a page changes.

Two operations, exposed as MCP tools by mcp_servers/rag_server.py:
  outline(query, path, depth) -> a compact ToC with node ids (files and headings), ranked toward the query
  read(node_id)               -> the text of exactly that section (heading down to the next same-level heading)

The agent reads the outline, picks the sections that answer the question, and reads only those: precise
citations ("wiki/projects/x.md#3 Deployment"), and it works when similarity search fails (numbers, names,
"what did we decide about ...").
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

MAX_FILES = 3000
MAX_FILE_BYTES = 2 * 1024 * 1024
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_WORD = re.compile(r"[a-z0-9]{3,}")


@dataclass
class Node:
    id: str              # "<relpath>#<n>" (n = heading index, 0 = whole file)
    path: str            # relpath inside the root
    title: str
    level: int           # 0 = file, 1..6 = heading level
    start: int           # first line (0-based) of the section
    end: int             # one past the last line
    gist: str            # first line of prose, for the outline


def roots() -> List[Path]:
    raw = os.environ.get("AURIX_PAGE_INDEX_DIRS", "")
    if raw.strip():
        dirs = [Path(p.strip()) for p in raw.split(os.pathsep if os.pathsep in raw else ",") if p.strip()]
    else:
        brain = Path(os.environ.get("AURIX_BRAIN", "/aurix"))
        dirs = [brain / "wiki", brain / "memory" / "longterm"]
    return [d for d in dirs if d.is_dir()]


def _gist(lines: List[str], start: int, end: int, skip_headings: bool = False) -> str:
    """First line of prose in a section's OWN text: stops at the next heading, so a parent never borrows a
    child section's line (files skip their headings, since a file's text usually starts with its # title)."""
    for line in lines[start:end]:
        s = line.strip()
        if _HEADING.match(s):
            if skip_headings:
                continue
            return ""
        if s and not _FENCE.match(s) and not s.startswith("---"):
            return (s[:117] + "...") if len(s) > 120 else s
    return ""


def parse(text: str, relpath: str) -> List[Node]:
    lines = text.splitlines()
    heads: List[Tuple[int, int, str]] = []
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else _HEADING.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    title = next((t for _, lvl, t in heads if lvl == 1), Path(relpath).stem.replace("-", " ").replace("_", " "))
    nodes = [Node(f"{relpath}#0", relpath, title, 0, 0, len(lines), _gist(lines, 0, len(lines), skip_headings=True))]
    for n, (line_no, level, htitle) in enumerate(heads, start=1):
        end = next((l for l, lv, _ in heads[n:] if lv <= level), len(lines))
        nodes.append(Node(f"{relpath}#{n}", relpath, htitle, level, line_no, end, _gist(lines, line_no + 1, end)))
    return nodes


def _files(root: Path) -> Iterable[Path]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(filenames):
            if f.lower().endswith(".md"):
                count += 1
                if count > MAX_FILES:
                    return
                yield Path(dirpath) / f


_cache: Dict[str, Tuple[float, List[Node]]] = {}


def _nodes_for(root: Path, path: Path) -> List[Node]:
    key = str(path)
    try:
        st = path.stat()
        if st.st_size > MAX_FILE_BYTES:
            return []
        hit = _cache.get(key)
        if hit and hit[0] == st.st_mtime:
            return hit[1]
        rel = f"{root.name}/{path.relative_to(root).as_posix()}"
        nodes = parse(path.read_text(encoding="utf-8", errors="replace"), rel)
        _cache[key] = (st.st_mtime, nodes)
        return nodes
    except OSError:
        return []


def _score(words: set, node: Node) -> int:
    if not words:
        return 0
    hay = f"{node.path} {node.title} {node.gist}".lower()
    return sum(3 if w in node.title.lower() else 1 for w in words if w in hay)


def outline(query: str = "", path: str = "", depth: int = 2, limit: int = 120,
            dirs: Optional[List[Path]] = None) -> str:
    """Compact ToC. With a query, files whose titles/headings/gists share words with it come first."""
    words = set(_WORD.findall((query or "").lower()))
    depth = max(0, min(int(depth or 2), 6))
    files: List[Tuple[int, List[Node]]] = []
    for root in (dirs if dirs is not None else roots()):
        for f in _files(root):
            nodes = _nodes_for(root, f)
            if not nodes or (path and not nodes[0].path.startswith(path.strip("/"))):
                continue
            files.append((max(_score(words, n) for n in nodes), nodes))
    if not files:
        return "No wiki pages found" + (f" under '{path}'" if path else "") + "."
    if words:
        files.sort(key=lambda fn: -fn[0])
        if any(s for s, _ in files):
            files = [fn for fn in files if fn[0]] or files
    out: List[str] = []
    for _, nodes in files:
        for n in nodes:
            if n.level > depth:
                continue
            indent = "  " * max(0, n.level - 1) if n.level else ""
            label = f"[{n.id}] {n.title}" if n.level else f"[{n.id}] {n.path} - {n.title}"
            out.append(f"{indent}{label}" + (f" - {n.gist}" if n.gist and n.level else ""))
            if len(out) >= limit:
                out.append(f"... (more: narrow with path= or a more specific query)")
                return "\n".join(out)
    return "\n".join(out)


def read(node_id: str, max_chars: int = 6000, dirs: Optional[List[Path]] = None) -> str:
    """Text of one node from `outline` (e.g. 'wiki/projects/aurix.md#3')."""
    rel, _, idx = (node_id or "").partition("#")
    rootname, _, inner = rel.partition("/")
    if not inner or ".." in Path(inner).parts:
        return f"Unknown node: {node_id}"
    for root in (dirs if dirs is not None else roots()):
        if root.name != rootname:
            continue
        f = (root / inner).resolve()
        if root.resolve() not in f.parents or not f.is_file():
            continue
        nodes = _nodes_for(root, f)
        try:
            n = nodes[int(idx or 0)]
        except (ValueError, IndexError):
            return f"Unknown node: {node_id}"
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[n.start:n.end]
        text = "\n".join(lines).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... (truncated; read a sub-heading of {n.id} for the rest)"
        return f"Source: {n.id} ({n.title})\n\n{text}"
    return f"Unknown node: {node_id}"
