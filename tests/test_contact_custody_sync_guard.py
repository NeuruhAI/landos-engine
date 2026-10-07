"""The export path must fail closed on a cloud-synced destination.

These tests do not assume anything about this particular Mac. Each one builds
the condition it is testing -- a fake sync root, a symlink, an undeclared
directory -- inside tmp_path, and points the guard at it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.custody import sync_guard
from app.custody.sync_guard import (
    CUSTODY_ROOT_ENV,
    SyncedPathRefused,
    assert_safe_output_dir,
    prepare_output_dir,
    sync_violations,
)


@pytest.fixture
def custody(tmp_path, monkeypatch):
    root = tmp_path / "custody"
    root.mkdir()
    monkeypatch.setenv(CUSTODY_ROOT_ENV, str(root))
    return root


def _fake_sync_roots(monkeypatch, *roots: Path):
    monkeypatch.setattr(sync_guard, "sync_roots", lambda: list(roots))


def test_a_path_inside_a_sync_root_is_refused(tmp_path, custody, monkeypatch):
    cloud = tmp_path / "CloudDocs"
    (cloud / "Desktop" / "EXPORT").mkdir(parents=True)
    _fake_sync_roots(monkeypatch, cloud)

    with pytest.raises(SyncedPathRefused) as err:
        assert_safe_output_dir(cloud / "Desktop" / "EXPORT")
    assert "inside sync root" in str(err.value)


def test_the_desktop_case_a_different_directory_inode_holding_the_same_files(
    tmp_path, custody, monkeypatch
):
    """The failure that actually happened: two paths, one set of inodes.

    ~/Desktop and the CloudDocs Desktop are distinct directory inodes whose
    entries are the same files. A guard that only walked ancestor inodes would
    have called ~/Desktop safe. A path check is what catches it, and both must
    be present for the two shapes below to both fail.
    """
    real = tmp_path / "Desktop"
    real.mkdir()
    _fake_sync_roots(monkeypatch, real)

    with pytest.raises(SyncedPathRefused):
        assert_safe_output_dir(real / "DEEDSONAR_FREE_CONTACT_T21")


def test_an_alias_to_a_sync_root_is_refused_by_inode_not_by_name(
    tmp_path, custody, monkeypatch
):
    """A destination whose name says nothing, whose inode says everything."""
    cloud = tmp_path / "provider"
    cloud.mkdir()
    alias = tmp_path / "innocuous"
    os.symlink(cloud, alias)
    _fake_sync_roots(monkeypatch, cloud)

    reasons = sync_violations(alias / "out")
    assert reasons, "a symlinked alias of a sync root must be detected"


def test_a_symlinked_destination_is_refused_outright(tmp_path, custody, monkeypatch):
    _fake_sync_roots(monkeypatch)
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = custody / "out"
    os.symlink(real, link)

    with pytest.raises(SyncedPathRefused) as err:
        assert_safe_output_dir(link)
    assert "symlink" in str(err.value)


def test_an_undeclared_destination_is_refused_even_when_nothing_syncs_it(
    tmp_path, custody, monkeypatch
):
    """Fail closed. Not-known-to-be-synced is not the same as vouched for."""
    _fake_sync_roots(monkeypatch)
    stray = tmp_path / "somewhere-nobody-declared"

    with pytest.raises(SyncedPathRefused) as err:
        assert_safe_output_dir(stray)
    assert "outside the declared custody root" in str(err.value)


def test_the_custody_root_itself_is_checked(tmp_path, monkeypatch):
    """Declaring a synced folder as custody does not launder it."""
    cloud = tmp_path / "CloudDocs"
    cloud.mkdir()
    _fake_sync_roots(monkeypatch, cloud)
    monkeypatch.setenv(CUSTODY_ROOT_ENV, str(cloud / "custody"))

    # The destination is somewhere the deny-list has nothing to say about, so
    # the only thing that can refuse it is the custody root check.
    elsewhere = tmp_path / "elsewhere"
    with pytest.raises(SyncedPathRefused) as err:
        assert_safe_output_dir(elsewhere)
    assert "itself cloud-synced" in str(err.value)


def test_a_custody_destination_is_allowed_and_created_private(
    tmp_path, custody, monkeypatch
):
    _fake_sync_roots(monkeypatch)
    out = prepare_output_dir(custody / "run-1")
    assert out.is_dir()
    assert oct(out.stat().st_mode & 0o777) == "0o700"


def test_the_real_shipped_default_is_not_synced_on_this_host():
    """No monkeypatching: the default this repo ships must hold on this machine."""
    assert sync_violations(sync_guard.DEFAULT_CUSTODY_ROOT) == []


def test_the_hosts_desktop_would_be_refused_by_the_shipped_roots(tmp_path, monkeypatch):
    """SENSITIVE_EXPORT_TO_SYNCED_PATH = BLOCK, against the real root list."""
    monkeypatch.delenv(CUSTODY_ROOT_ENV, raising=False)
    with pytest.raises(SyncedPathRefused):
        assert_safe_output_dir(Path.home() / "Desktop" / "DEEDSONAR_ANYTHING")


def test_no_shipped_default_points_at_the_desktop():
    """The regression that produced 25 synced export generations.

    Reading the argparse defaults rather than grepping, so a default that is
    computed at import time is still caught.
    """
    import scripts.project_contacts_to_deedsonar as proj
    import scripts.run_nc_free_phone_enrichment as enrich
    import scripts.run_nc_resolution_factory as factory
    from app.custody.locations import canonical_bundle, estate_export

    defaults = [
        enrich.CANONICAL_PRODUCT,
        enrich.T19F_PRODUCT,
        estate_export(),
        canonical_bundle(),
    ]
    for d in defaults:
        assert sync_violations(d) == [], f"{d} is cloud-reachable"
    assert "Desktop" not in str(enrich.CANONICAL_PRODUCT)
    assert proj is not None and factory is not None
