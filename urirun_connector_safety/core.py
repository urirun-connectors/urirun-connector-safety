# Author: Tom Sapletta · Part of the ifURI solution.
"""urirun-connector-safety — silnik ZAUFANIA: kiedy autonomia jest PROWADNIE bezpieczna (`safe://`).

Brama „człowiek zatwierdza" to proxy dla „nie wiemy, czy to bezpieczne". Zamiast blokować
wszystko, ten connector OBLICZA per-akcję werdykt bezpieczeństwa z mierzalnych czynników:

  * **izolacja** (blast radius) — czy akcja biegnie w wyrzucalnym worktree/sandboxie? (zawarta)
  * **odwracalność** (`inverse` / git) — czy da się cofnąć?
  * **weryfikowalność** (`verify://`) — czy potwierdzimy sukces/porażkę?
  * **twin środowiska** — czy env jest known-good (nie degraded)?
  * **twin zachowania** (mind/episodic) — czy ta klasa akcji zachowywała się bezpiecznie?

Reguła: **izolacja + weryfikowalność ⇒ SAFE-AUTO** — bo blast radius=0 (worktree do wyrzucenia),
a verify mówi czy zadziałało; jeśli verify czerwony → wyrzuć worktree = idealny rollback. Tak
„niebezpieczne" `--dangerously-skip-permissions` STAJE SIĘ bezpieczne, gdy zawarte i sprawdzalne.

Zwraca też **ranking STRATEGII (fallbacków)**: gdy jedna zawiedzie walidację, próbuj następnej.
Read-only ocena; koperta nie rzuca; twin/mind opcjonalne (degradacja, nie błąd).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

import urirun

CONNECTOR_ID = "safety"
conn = urirun.connector(CONNECTOR_ID, scheme="safe")


def _ok(**kw: Any) -> dict[str, Any]:
    return urirun.ok(connector=CONNECTOR_ID, **kw)


def _fail(msg: str, action: str) -> dict[str, Any]:
    return urirun.fail(msg, connector=CONNECTOR_ID, action=action)


def _project(project: str = "") -> str:
    return project or os.environ.get("URIRUN_KORU_PROJECT") or os.path.expanduser("~/github/if-uri")


# ---------------------------------------------------------------- czynniki (mierzalne)

def _git_repo(project: str) -> bool:
    return os.path.isdir(os.path.join(_project(project), ".git"))


import re

# Jedyna generyczna strategia wbudowana: git (bo repo to uniwersalny inverse dla zmian plików).
# CAŁA reszta (send→delete, move→move-back, publish→unpublish) jest DEKLAROWANA przez connectory
# w meta.inverse — safe:// to CZYTA, nie zgaduje. Rozbudowa = deklaracja w connectorze, nie edycja tu.
_GIT_STRATEGIES = [{"technique": "git-revert", "how": "git revert / git checkout do stanu sprzed"},
                   {"technique": "discard-worktree", "how": "wyrzuć izolowany worktree (blast radius=0)"}]
_KNOWN_SCHEMES = ("signal", "email", "calendar", "whatsapp", "telegram", "slack", "sms",
                  "kvm", "cron", "pdf", "http", "fs")


def _scheme_of(blob: str) -> str:
    """Docelowy scheme zadania — z jawnego URI w opisie albo z labeli (tylko DETEKCJA, nie logika)."""
    b = (blob or "").lower()
    m = re.search(r"\b([a-z][a-z0-9]+)://", b)
    if m:
        return m.group(1)
    for s in _KNOWN_SCHEMES:
        if s in b:
            return s
    return ""


def _route_matches(route: str, blob: str) -> bool:
    """Czy KONKRETNA trasa (np. .../command/delete) jest tym, o co chodzi w zadaniu?"""
    tail = route.rstrip("/").split("/")[-1]          # move / delete / send / send-reply
    return bool(tail) and tail in blob


def _declared_inverse(scheme: str, blob: str) -> dict | None:
    """Czyta reversible/inverse z bindings connectora `scheme://` — dla KONKRETNEJ trasy (STANDARD, nie hardkod)."""
    if not scheme:
        return None
    try:
        mod = __import__(f"urirun_connector_{scheme}", fromlist=["core"])
        binds = mod.core.urirun_bindings().get("bindings", {})
    except Exception:  # noqa: BLE001
        return None
    declared = {r: s for r, s in binds.items()
                if ("reversible" in (s.get("meta") or {}) or "inverse" in (s.get("meta") or {}))}
    if not declared:
        return None
    b = (blob or "").lower()
    # 1) dopasuj konkretną trasę wspomnianą w zadaniu (delete vs move ma RÓŻNĄ odwracalność)
    for route, spec in declared.items():
        if _route_matches(route, b):
            meta = spec["meta"]
            inv = meta.get("inverse") or []
            return {"reversible": bool(meta.get("reversible", bool(inv))),
                    "strategies": inv if isinstance(inv, list) else [{"technique": "declared", "how": str(inv)}],
                    "route": route}
    # 2) brak dopasowania trasy → NAJBARDZIEJ OSTROŻNA deklaracja (jeśli którakolwiek irreversible → False)
    revs = [bool((s["meta"]).get("reversible", bool((s["meta"]).get("inverse")))) for s in declared.values()]
    return {"reversible": all(revs) if revs else None, "strategies": [], "route": "*"}


def _resolve_reversibility(action: str, blob: str, git: bool) -> dict:
    """Odwracalność ze ŹRÓDŁA prawdy: deklaracja connectora (meta.inverse) → git → nieznane."""
    scheme = _scheme_of(blob)
    if scheme:
        decl = _declared_inverse(scheme, blob)
        if decl is not None:
            strat = list(decl["strategies"]) + (_GIT_STRATEGIES if (git and decl["reversible"]) else [])
            return {"reversible": decl["reversible"], "strategies": strat,
                    "source": f"deklaracja {scheme}://"}
        return {"reversible": None, "strategies": [],
                "source": f"brak deklaracji dla {scheme}:// — dodaj meta.inverse w connectorze"}
    if git and action in ("run-agent", "code", "edit", "write"):
        return {"reversible": True, "strategies": list(_GIT_STRATEGIES), "source": "git (repo)"}
    return {"reversible": None, "strategies": [], "source": "nieznane"}


def _ticket_name(project: str, tid: str) -> str:
    if not tid:
        return ""
    b = shutil.which("planfile") or os.path.expanduser("~/github/if-uri/venv/bin/planfile")
    try:
        import json
        cp = subprocess.run([b, "ticket", "show", tid, "--format", "json"],
                            capture_output=True, text=True, timeout=12, cwd=_project(project))
        raw = cp.stdout
        d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1]) if "{" in raw else {}
        return f"{d.get('name', '')} {d.get('description', '')} {' '.join(d.get('labels') or [])}"
    except Exception:  # noqa: BLE001
        return ""




def _has_verify(tid: str) -> bool:
    try:
        from urirun_connector_verify import core as vf
        return bool(vf._load().get(tid))
    except Exception:  # noqa: BLE001
        return False


def _worktree_possible(project: str) -> bool:
    return _git_repo(project) and shutil.which("git") is not None


def _env_known_good(project: str) -> bool | None:
    """Twin środowiska. True gdy brak zdegradowanych przebiegów; None (advisory, NIE blokuj) gdy
    historia degradacji istnieje ale nie umiemy jej zawęzić do TEJ akcji — bo stare/nieistotne
    degradacje nie mogą paraliżować autonomii na zawsze. Twarde False zostaje na jawny sygnał."""
    try:
        from urirun.node.twin_store import durable_memory  # type: ignore
        mem = durable_memory()
        deg = mem.degraded_flows() if hasattr(mem, "degraded_flows") else []
        return True if not deg else None  # degradacja w historii → advisory, nie hard-block
    except Exception:  # noqa: BLE001
        return None


def _behavior_trusted(action: str) -> bool | None:
    """Twin zachowania (mind/episodic) — czy ta klasa akcji zachowywała się bezpiecznie? None = brak danych."""
    try:
        from urirun_mind import core as mind  # type: ignore
        return mind.action_class_safe(action)  # jeśli mind udostępnia; inaczej wyjątek → None
    except Exception:  # noqa: BLE001
        return None


def factors(action: str, ticket_id: str, project: str = "") -> dict[str, Any]:
    proj = _project(project)
    repo_action = action in ("run-agent", "code", "edit", "write")
    blob = _ticket_name(proj, ticket_id)
    rev = _resolve_reversibility(action, blob, _git_repo(proj))
    return {
        "isolated_possible": _worktree_possible(proj),
        # odwracalne = umiemy ODTWORZYĆ cechy stanu sprzed (dowolną strategią). Źródło: deklaracja.
        "reversible": rev["reversible"],
        "inverse_strategies": rev["strategies"],
        "reversibility_source": rev["source"],
        "verifiable": _has_verify(ticket_id),
        "env_known_good": _env_known_good(proj),
        "behavior_trusted": _behavior_trusted(action),
        "repo_action": repo_action,
    }


# ---------------------------------------------------------------- strategie (fallback chain)

def _strategies(f: dict) -> list[dict]:
    """Ranking strategii wykonania — każda z werdyktem i sposobem walidacji. Próbuj po kolei."""
    out = []
    env_ok = f["env_known_good"] is not False  # None (nieznane) nie blokuje
    if f["isolated_possible"] and f["verifiable"] and env_ok:
        out.append({"id": "autonomous-worktree", "verdict": "safe-auto",
                    "why": "izolowany worktree (blast radius=0) + verify:// (potwierdzalne) + git-reversible",
                    "how": "agent w świeżym worktree → verify → merge gdy zielone, wyrzuć gdy czerwone",
                    "validation": "verify:// po wykonaniu; brak zielonego = discard worktree"})
    if f["reversible"] and f["verifiable"] and env_ok:
        out.append({"id": "autonomous-inplace", "verdict": "safe-auto",
                    "why": "odwracalne (git/inverse) + weryfikowalne",
                    "how": "agent w repo → verify → git-rollback gdy czerwone",
                    "validation": "verify:// po wykonaniu; czerwone = rollback"})
    if f["verifiable"]:
        out.append({"id": "dry-run-verify-human", "verdict": "needs-human",
                    "why": "plan bez zapisu + verify na sucho; człowiek zatwierdza zmiany",
                    "how": "agent planuje → operator approve → apply → verify",
                    "validation": "verify:// + zgoda operatora"})
    out.append({"id": "human-manual", "verdict": "human",
                "why": "brak przesłanek do autonomii — człowiek wykonuje ręcznie",
                "how": "operator wykonuje zadanie", "validation": "operator potwierdza"})
    return out


def assess(action: str, ticket_id: str = "", project: str = "") -> dict[str, Any]:
    """Werdykt bezpieczeństwa akcji + ranking strategii. Verdict = najlepsza dostępna strategia."""
    f = factors(action, ticket_id, project)
    strategies = _strategies(f)
    best = strategies[0]
    blockers = []
    if not f["verifiable"]:
        blockers.append("brak verify:// (nie potwierdzimy sukcesu) — dodaj acceptance_criteria")
    if not f["isolated_possible"] and not f["reversible"]:
        blockers.append("brak izolacji ani odwracalności — nieznany blast radius")
    if f["env_known_good"] is False:
        blockers.append("środowisko DEGRADED (twin) — najpierw uzdrów")
    return {"action": action, "ticket": ticket_id, "verdict": best["verdict"],
            "strategy": best["id"], "factors": f, "strategies": strategies, "blockers": blockers,
            "auto_safe": best["verdict"] == "safe-auto"}


@conn.handler("action/query/assess", isolated=False,
              meta={"label": "Werdykt bezpieczeństwa akcji: czy autonomia jest prowadnie bezpieczna + strategie"})
def action_query_assess(action: str = "run-agent", ticket_id: str = "", project: str = "") -> dict[str, Any]:
    try:
        return _ok(action="safe-assess", **assess(action, (ticket_id or "").strip(), project))
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), "safe-assess")


@conn.handler("strategy/query/plan", isolated=False,
              meta={"label": "Ranking strategii wykonania (fallback: gdy jedna zawiedzie, następna)"})
def strategy_query_plan(action: str = "run-agent", ticket_id: str = "", project: str = "") -> dict[str, Any]:
    try:
        f = factors(action, (ticket_id or "").strip(), project)
        return _ok(action="safe-strategy", strategies=_strategies(f), factors=f)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), "safe-strategy")


def urirun_bindings() -> dict[str, Any]:
    return conn.bindings()


def connector_manifest() -> dict[str, Any]:
    return urirun.load_manifest(__package__) or {"id": CONNECTOR_ID}


def main(argv: list[str] | None = None) -> int:
    return conn.cli(argv, manifest_prose=urirun.load_manifest(__package__))


if __name__ == "__main__":
    raise SystemExit(main())
