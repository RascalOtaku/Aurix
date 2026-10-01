"""src/foundation/evolve.py - a general-purpose evolutionary optimizer: Population-Based Training (exploit the best, explore
mutated copies of them) over a Genetic Algorithm's operators (selection, mutation, crossover), with a REAL measured result as
the only fitness that counts - never a self-report. This is deliberately NOT built for one feature: it is a reusable engine
any part of AURIX can plug a genome and a real fitness measurement into (see DOMAINS below for what is plugged in so far).

A "domain" is the only thing a caller needs to supply (register() below):
    random_genome()                 -> a brand-new candidate
    mutate(genome, llm)             -> a varied copy (LLM-driven text edit for a prompt genome; numeric jitter elsewhere)
    crossover(a, b, llm)            -> a genome combining traits of two parents
    evaluate(genome, llm) -> (fitness, meta)   the REAL measurement - must actually run the thing, never estimate it
    describe(genome)                -> a short owner-readable summary
    worth_promoting(genome)         -> bool: is this good enough to even ask the owner about it
    apply(genome)                   -> make it live (only ever called after owner approval)

Exactly like forge/upgrades/land: evolution SEARCHES and TESTS completely on its own (sandboxed, nothing production-facing
changes), but turning a winning genome into the thing that actually runs always goes through propose_promotion() ->
owner reads the card -> approve()/decline() - the model proposes, a real measured result narrows the field, the owner decides.

First domain wired here: self-improve prompts (src/foundation/evolve_domains.py) - forge.FORGE_SYSTEM's guidance suffix,
fitness = real sandbox pass rate on evals.py's hidden "code" tier tasks. The registry below is exactly how a second domain
(LandPilot, a money workstream, any other project) gets added later without touching this engine.

NOTE ON SCOPE (2026-09-30): the owner also asked for this to evolve the Alpaca paper-trading strategy. That one is NOT
wired through this registry - it lives outside this container entirely (tasks/strategy_evolve.py, on the host, next to
tasks/trade_agent.py) because its genome is NUMERIC trading parameters, not text, and its fitness is a real backtest against
historical market bars that this sandboxed container has no path to fetch. Running the search fast needs a backtest; trusting
the winner needs a REAL 30-day forward paper-trading probation before promotion - see that module's own docstring for the
full design. Two engines, same philosophy (search freely, measure for real, owner approves before anything goes live),
implemented separately because they live in genuinely different runtimes.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from src.foundation import audit

e = html.escape
EID = re.compile(r"^e-[0-9a-f]{6}$")
LLM = Callable[[str, str], Awaitable[str]]


@dataclass
class Domain:
    name: str
    random_genome: Callable[[], Any]
    mutate: Callable[[Any, Optional[LLM]], Awaitable[Any]]
    crossover: Callable[[Any, Any, Optional[LLM]], Awaitable[Any]]
    evaluate: Callable[[Any, Optional[LLM]], Awaitable[Tuple[float, dict]]]
    describe: Callable[[Any], str]
    worth_promoting: Callable[[float], bool]
    apply: Callable[[Any], None]
    pop_size: int = 6
    keep_top: int = 2


_DOMAINS: Dict[str, Domain] = {}


def register(domain: Domain) -> None:
    _DOMAINS[domain.name] = domain


def domains() -> List[str]:
    return sorted(_DOMAINS)


def _domain(name: str) -> Domain:
    d = _DOMAINS.get(name)
    if d is None:
        raise KeyError(f"no evolve domain registered as {name!r} (have: {', '.join(domains()) or 'none'})")
    return d


# ---------------------------------------------------------------------------------------------------------------------------------
# where things live
# ---------------------------------------------------------------------------------------------------------------------------------

def _data(name: str) -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "evolve" / name


def _pop_path(name: str) -> Path:
    return _data(name) / "population.json"


def _proposals_path(name: str) -> Path:
    return _data(name) / "proposals.json"


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _load_pop(name: str) -> dict:
    return _read(_pop_path(name), {"generation": 0, "genomes": []})


def _save_pop(name: str, pop: dict) -> None:
    _atomic(_pop_path(name), pop)


# ---------------------------------------------------------------------------------------------------------------------------------
# PBT/GA loop
# ---------------------------------------------------------------------------------------------------------------------------------

async def advance(name: str, llm: Optional[LLM] = None, generations: int = 1) -> dict:
    """Evaluate the current population for real, EXPLOIT (keep the top performers), EXPLORE (refill the rest with
    mutated/crossed-over copies of them), repeat. Every evaluate() call actually runs the thing being optimized (a real
    sandbox test pass rate, a real backtest) - nothing here is ever a guess at what might work."""
    d = _domain(name)
    pop = _load_pop(name)
    genomes = [g["payload"] for g in pop.get("genomes", [])]
    while len(genomes) < d.pop_size:
        genomes.append(d.random_genome())

    for _ in range(max(1, generations)):
        scored = []
        for payload in genomes:
            fitness, meta = await d.evaluate(payload, llm)
            scored.append({"payload": payload, "fitness": fitness, "meta": meta})
        scored.sort(key=lambda g: g["fitness"], reverse=True)
        survivors = scored[: d.keep_top]
        children: List[Any] = []
        while len(children) < d.pop_size - len(survivors):
            a, b = survivors[0], survivors[min(1, len(survivors) - 1)]
            child = await d.crossover(a["payload"], b["payload"], llm) if len(survivors) > 1 else a["payload"]
            child = await d.mutate(child, llm)
            children.append(child)
        genomes = [s["payload"] for s in survivors] + children
        pop["generation"] = pop.get("generation", 0) + 1

    final_scored = []
    for payload in genomes:
        fitness, meta = await d.evaluate(payload, llm)
        final_scored.append({"payload": payload, "fitness": fitness, "meta": meta, "generation": pop["generation"]})
    final_scored.sort(key=lambda g: g["fitness"], reverse=True)
    pop["genomes"] = final_scored
    _save_pop(name, pop)
    audit.append("evolve_generation", domain=name, generation=pop["generation"],
                 best_fitness=final_scored[0]["fitness"] if final_scored else None)
    return pop


def best(name: str) -> Optional[dict]:
    pop = _load_pop(name)
    genomes = pop.get("genomes") or []
    return genomes[0] if genomes else None


def status(name: str) -> dict:
    d = _domain(name)
    pop = _load_pop(name)
    b = best(name)
    return {"domain": name, "generation": pop.get("generation", 0), "population_size": len(pop.get("genomes", [])),
            "best_fitness": b["fitness"] if b else None, "best_description": d.describe(b["payload"]) if b else None,
            "pending_proposal": next((p for p in pending(name)), None)}


def status_text(name: str) -> str:
    s = status(name)
    lines = [f"\U0001f9ec <b>{e(name)}</b> - generation {s['generation']}, population {s['population_size']}"]
    if s["best_fitness"] is None:
        lines.append(f"no genomes evaluated yet - <code>evolve run {e(name)}</code>")
    else:
        lines.append(f"best fitness {s['best_fitness']:.3f}: {e(s['best_description'])}")
    pend = pending(name)
    if pend:
        lines.append(_card(pend[0]))
    return "\n".join(lines)


def overview_text() -> str:
    names = domains()
    if not names:
        return "No evolve domains are registered."
    return "\n\n".join(status_text(n) for n in names)


def panel() -> Dict[str, Any]:
    """The dashboard's view: every registered domain's generation/best-fitness/description, plus every pending
    promotion card across all domains (see static/command.html's evolve card)."""
    return {"domains": [status(n) for n in domains()], "pending": all_pending()}


# ---------------------------------------------------------------------------------------------------------------------------------
# owner approval gate - the model (and the GA) proposes, the owner decides, exactly like forge/upgrades/land
# ---------------------------------------------------------------------------------------------------------------------------------

def proposals(name: str) -> List[dict]:
    return _read(_proposals_path(name), [])


def pending(name: str) -> List[dict]:
    return [p for p in proposals(name) if p["status"] == "pending"]


def _save_proposals(name: str, items: List[dict]) -> None:
    _atomic(_proposals_path(name), items[-200:])


def propose_promotion(name: str) -> str:
    """Only ever makes a card when the best genome found so far clears the domain's own bar for "worth bothering the owner
    about" - evolution can run forever in the background without ever asking anything, and usually should."""
    d = _domain(name)
    b = best(name)
    if b is None:
        return f"No evolved genome yet for {name!r} - run advance() first."
    if not d.worth_promoting(b["fitness"]):
        return f"The best {name} genome so far (fitness {b['fitness']:.3f}) isn't clearly better yet - not proposing."
    items = proposals(name)
    for p in items:
        if p["status"] == "pending" and p["fitness"] == b["fitness"]:
            return _card(p)
    card = {"id": "e-" + secrets.token_hex(3), "domain": name, "fitness": b["fitness"], "description": d.describe(b["payload"]),
            "generation": b.get("generation"), "status": "pending", "created": time.time(), "_payload": b["payload"]}
    items.append(card)
    _save_proposals(name, items)
    audit.append("evolve_proposed", id=card["id"], domain=name, fitness=b["fitness"])
    return _card(card)


def _card(c: dict) -> str:
    return (f"🧬 Evolved {e(c['domain'])} candidate (generation {c.get('generation', '?')}, fitness {c['fitness']:.3f})\n"
            f"{e(c['description'])}\n"
            f"<code>yes evolve {c['id']}</code> · <code>no evolve {c['id']}</code>")


def _decide(name: str, pid: str, approve: bool, decided_by: str) -> str:
    if not EID.match(pid or ""):
        return "That is not an evolve card id (e-xxxxxx)."
    items = proposals(name)
    c = next((x for x in items if x["id"] == pid), None)
    if c is None:
        audit.append("evolve_refused", id=pid, domain=name, why="no such card", attempted_by=decided_by)
        return f"No evolve card <code>{e(pid)}</code>."
    if c["status"] != "pending":
        audit.append("evolve_refused", id=pid, domain=name, why=f"card is {c['status']}", attempted_by=decided_by)
        return f"<code>{pid}</code> was already {e(c['status'])}."
    c.update(status="approved" if approve else "declined", decided_by=decided_by, decided_at=time.time())
    _save_proposals(name, items)
    audit.append("evolve_approved" if approve else "evolve_declined", id=pid, domain=name, fitness=c["fitness"], decided_by=decided_by)
    if not approve:
        return f"❌ Declined the evolved {e(name)} candidate. Logged."
    _domain(name).apply(c["_payload"])
    return f"✅ OWNER APPROVED - the evolved {e(name)} candidate (fitness {c['fitness']:.3f}) is now live. Logged to the audit chain."


def approve(name: str, pid: str, decided_by: str = "owner:telegram") -> str:
    return _decide(name, pid, True, decided_by)


def decline(name: str, pid: str, decided_by: str = "owner:telegram") -> str:
    return _decide(name, pid, False, decided_by)


def _domain_for_pending(pid: str) -> Optional[str]:
    return next((n for n in domains() if any(p["id"] == pid for p in pending(n))), None)


def approve_any(pid: str, decided_by: str = "owner:telegram") -> str:
    """Like approve(), but for a caller (a dashboard button, a bare id) that only has the card id, not which domain it
    belongs to - looks it up across every registered domain's pending list."""
    name = _domain_for_pending(pid)
    if name is None:
        return f"No pending evolve card <code>{e(pid)}</code>."
    return approve(name, pid, decided_by)


def decline_any(pid: str, decided_by: str = "owner:telegram") -> str:
    name = _domain_for_pending(pid)
    if name is None:
        return f"No pending evolve card <code>{e(pid)}</code>."
    return decline(name, pid, decided_by)


def all_pending() -> List[dict]:
    out = []
    for n in domains():
        out.extend(pending(n))
    return out
