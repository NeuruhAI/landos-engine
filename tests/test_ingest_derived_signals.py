"""
test_ingest_derived_signals.py — architecture guards for the SIGNALS-ONLY lane (Option B).

Locks the decision in code, no DB needed:
  * derived overlay adapters run ONLY through scripts/ingest_derived_signals, NEVER
    through the probate->owner_leads minter (which reaches the paid ParcelProviderChain);
  * the signals-only runner imports NO provider chain, so a paid tier is unreachable.
Assumes the repo .env is present (same as the rest of the suite: app.config._require).
"""
import ast
import importlib
import inspect


def test_derived_adapters_are_not_registered_in_the_minter():
    minter = importlib.import_module("scripts.ingest_discovery")
    assert "estate_sale_ext" not in minter.ADAPTERS
    assert "heirs_of_tangled_title" not in minter.ADAPTERS
    assert "probate_in" in minter.ADAPTERS          # minter still serves probate


def test_signals_only_runner_registers_exactly_the_derived_adapters():
    runner = importlib.import_module("scripts.ingest_derived_signals")
    assert set(runner.DERIVED) == {"estate_sale_ext", "heirs_of_tangled_title"}


def test_signals_only_runner_imports_no_provider_chain():
    # The whole point of Option B: no paid tier reachable from the derived path.
    runner = importlib.import_module("scripts.ingest_derived_signals")
    tree = ast.parse(inspect.getsource(runner))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(n.name for n in node.names)
    assert not any("providers" in m for m in imported), \
        f"signals-only runner must not import providers.*: {sorted(imported)}"
