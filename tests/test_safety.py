from __future__ import annotations
from urirun_connector_safety import core

def test_bindings():
    b=core.urirun_bindings()["bindings"]
    assert "safe://host/action/query/assess" in b and "safe://host/strategy/query/plan" in b

def test_reversibility_from_git_for_code(monkeypatch):
    monkeypatch.setattr(core,"_git_repo",lambda p:True)
    r=core._resolve_reversibility("run-agent","refactor render module",git=True)
    assert r["reversible"] is True and r["source"].startswith("git")

def test_reversibility_from_connector_declaration(monkeypatch):
    # email:// deklaruje move=reversible, delete=irreversible → resolver to CZYTA (nie hardkod)
    # Zapewnij fallback w izolowanym środowisku testowym (gdy pakiet urirun-connector-email nie jest zainstalowany)
    try:
        import urirun_connector_email  # noqa: F401
    except ImportError:
        monkeypatch.setattr(
            core,
            "_declared_inverse",
            lambda scheme, blob: {
                "reversible": True,
                "strategies": [{"technique": "declared", "how": "move-back"}],
                "route": "email://host/message/command/move",
            }
            if scheme == "email" and "move" in blob
            else None,
        )
    r = core._resolve_reversibility("run-agent", "email://... message move do Junk", git=False)
    assert r["reversible"] is True and "deklaracja" in r["source"]

def test_unknown_scheme_is_unknown_not_assumed(monkeypatch):
    r=core._resolve_reversibility("run-agent","signal:// odpisz na wiadomość",git=False)
    # brak connectora signal → nieznane (None), NIE zakładamy odwracalności
    assert r["reversible"] is None and "brak deklaracji" in r["source"]

def test_verifiable_plus_reversible_is_safe_auto(monkeypatch):
    monkeypatch.setattr(core,"_git_repo",lambda p:True)
    monkeypatch.setattr(core,"_worktree_possible",lambda p:True)
    monkeypatch.setattr(core,"_has_verify",lambda t:True)
    monkeypatch.setattr(core,"_env_known_good",lambda p:None)
    monkeypatch.setattr(core,"_behavior_trusted",lambda a:None)
    monkeypatch.setattr(core,"_ticket_name",lambda p,t:"refactor code")
    r=core.assess("run-agent","T-1")
    assert r["auto_safe"] is True

def test_fallback_ends_with_human(monkeypatch):
    monkeypatch.setattr(core,"_git_repo",lambda p:False)
    monkeypatch.setattr(core,"_worktree_possible",lambda p:False)
    monkeypatch.setattr(core,"_has_verify",lambda t:False)
    monkeypatch.setattr(core,"_env_known_good",lambda p:None)
    monkeypatch.setattr(core,"_behavior_trusted",lambda a:None)
    monkeypatch.setattr(core,"_ticket_name",lambda p,t:"")
    r=core.assess("run-agent","T-2")
    assert r["strategies"][-1]["id"]=="human-manual" and r["auto_safe"] is False
