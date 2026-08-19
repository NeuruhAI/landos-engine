"""Where contact intelligence lives, now that it no longer lives on the Desktop.

The old defaults pointed at ``~/Desktop/DEEDSONAR_*``. On this host ``~/Desktop``
is the iCloud Desktop -- same inodes -- so every run published its output to a
consumer cloud. The layout below replaces that:

    ~/neuruh-private/contact-intelligence/
        estate/<export>/     the estate export the matcher reads
        runs/<run-name>/     one immutable directory per enrichment run
        CANONICAL -> runs/<current>   a symlink, so "canonical" is a pointer and
                                      never a directory that silently goes stale

``CANONICAL`` being a symlink is deliberate. The directory literally named
``DEEDSONAR_FREE_CONTACT_CANONICAL`` had held the T20C output (918 rows) long
after T20D superseded it, so anything reading "the canonical path" read stale
data. A pointer cannot drift: repointing it is the act of promoting a run.

Override the root with ``NEURUH_CONTACT_CUSTODY_ROOT``. It is checked for cloud
reachability before it is honoured -- see :mod:`app.custody.sync_guard`.
"""
from __future__ import annotations

from pathlib import Path

from .sync_guard import custody_root

CANONICAL_LINK_NAME = "CANONICAL"
ESTATE_EXPORT_NAME = "DEEDSONAR_CONTACT_EXPORT_PREVIEW_20260817"


def contact_root() -> Path:
    return custody_root()


def runs_root() -> Path:
    return contact_root() / "runs"


def estate_root() -> Path:
    return contact_root() / "estate"


def run_dir(name: str) -> Path:
    return runs_root() / name


def canonical_bundle() -> Path:
    """The current bundle, followed through the pointer.

    Falls back to the pointer's own path when it is not a link yet, so a fresh
    custody root does not raise before the first run has been promoted.
    """
    link = contact_root() / CANONICAL_LINK_NAME
    return link.resolve() if link.exists() else link


def estate_export() -> Path:
    return estate_root() / ESTATE_EXPORT_NAME


def promote_canonical(target: Path) -> Path:
    """Point CANONICAL at a run directory. The runs themselves stay immutable."""
    link = contact_root() / CANONICAL_LINK_NAME
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    return link
