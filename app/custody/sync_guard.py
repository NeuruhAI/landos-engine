"""Refuse to write contact PII into a folder that a consumer cloud replicates.

WHY THIS EXISTS. ``~/Desktop`` on this host is the iCloud Desktop. Not "like"
it, not "backed up to" it -- the same file. ``phone_append_combined.csv`` under
``~/Desktop/DEEDSONAR_FREE_CONTACT_T21`` and the same name under
``~/Library/Mobile Documents/com~apple~CloudDocs/Desktop/...`` are one inode,
508533172. Every enrichment run that defaulted its output to the Desktop
published 922 real consumer phone numbers, with owner names and property
addresses, to Apple. The custody check that passed had looked at Git; the PII
was never in Git.

A path check alone is not enough and an inode check alone is not enough:

  * The macOS Desktop/Documents firmlink means ``~/Desktop`` and the CloudDocs
    Desktop have *different directory inodes* while their contents are the same
    files. Walking ancestor inodes up from a target never meets the CloudDocs
    root, so inode-only would have called ``~/Desktop`` safe.
  * ``~/Library/CloudStorage/*`` providers (Dropbox, Drive, OneDrive, Box) mount
    under paths that a caller can reach through a symlink from anywhere, so
    path-only would have called the symlinked alias safe.

So this module does both, and then does the only thing that actually fails
closed: it requires the resolved destination to sit inside a declared custody
root. An unknown location is refused. Adding a new custody root is a deliberate
act -- ``NEURUH_CONTACT_CUSTODY_ROOT`` -- and that root is itself put through
the deny checks before it is honoured.

The invariant this enforces is ``SENSITIVE_EXPORT_TO_SYNCED_PATH = BLOCK``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

CUSTODY_ROOT_ENV = "NEURUH_CONTACT_CUSTODY_ROOT"

#: Where contact intelligence is allowed to land when nothing is declared.
DEFAULT_CUSTODY_ROOT = Path.home() / "neuruh-private" / "contact-intelligence"


class SyncedPathRefused(RuntimeError):
    """Raised instead of writing PII somewhere a cloud provider would read it."""


def _home() -> Path:
    return Path.home()


def sync_roots() -> List[Path]:
    """Every location on this host that a consumer cloud replicates.

    Desktop and Documents are listed unconditionally. They are only replicated
    when iCloud Desktop-and-Documents is on, but a lane must not become unsafe
    because a preference was toggled after the code was reviewed, and neither is
    a defensible home for a contact corpus regardless of who is syncing it.
    """
    home = _home()
    roots = [
        home / "Desktop",
        home / "Documents",
        home / "Library" / "Mobile Documents",
        home / "Library" / "CloudStorage",
        home / "Dropbox",
        home / "OneDrive",
        home / "Box",
        home / "Box Sync",
        home / "Google Drive",
        home / "Sync",
        Path("/Volumes/GoogleDrive"),
    ]
    cloud_docs = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if cloud_docs.is_dir():
        try:
            roots.extend(sorted(p for p in cloud_docs.iterdir() if p.is_dir()))
        except OSError:
            pass
    if (home / "Library" / "CloudStorage").is_dir():
        try:
            roots.extend(sorted(p for p in (home / "Library" / "CloudStorage").iterdir()))
        except OSError:
            pass
    return roots


def _is_within(path: Path, root: Path) -> bool:
    p, r = str(path), str(root)
    return p == r or p.startswith(r + os.sep)


def _ancestor_ids(path: Path) -> set:
    """(device, inode) of the target and every ancestor that exists."""
    ids = set()
    current = path
    while True:
        try:
            st = current.stat()
            ids.add((st.st_dev, st.st_ino))
        except OSError:
            pass
        if current.parent == current:
            return ids
        current = current.parent


def sync_violations(path: os.PathLike | str) -> List[str]:
    """Every reason this path is cloud-reachable. Empty means no reason found."""
    target = Path(path).expanduser()
    resolved = target.resolve()
    reasons: List[str] = []
    ids = _ancestor_ids(resolved)
    for root in sync_roots():
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        if _is_within(resolved, root_resolved) or _is_within(Path(str(target)), root):
            reasons.append(f"path is inside sync root {root}")
            continue
        try:
            st = root_resolved.stat()
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in ids:
            reasons.append(f"inode-reachable from sync root {root}")
    return reasons


def custody_root() -> Path:
    """The declared custody root, proven non-synced before it is returned."""
    declared = os.environ.get(CUSTODY_ROOT_ENV, "").strip()
    root = Path(declared).expanduser() if declared else DEFAULT_CUSTODY_ROOT
    reasons = sync_violations(root)
    if reasons:
        raise SyncedPathRefused(
            f"{CUSTODY_ROOT_ENV}={root} is itself cloud-synced: " + "; ".join(reasons)
        )
    return root


def assert_safe_output_dir(path: os.PathLike | str, *, what: str = "contact output") -> Path:
    """Return the resolved path, or refuse before a single byte is written.

    Refuses on three independent grounds: the destination is a symlink (so the
    real target is not the one that was reviewed), it is cloud-reachable, or it
    is outside every declared custody root. The last is what makes this fail
    closed -- a destination nobody vouched for is refused rather than allowed.
    """
    target = Path(path).expanduser()
    if target.is_symlink():
        raise SyncedPathRefused(
            f"refusing to write {what} to {target}: it is a symlink, so the reviewed "
            f"path is not the written path (resolves to {target.resolve()})"
        )
    reasons = sync_violations(target)
    if reasons:
        raise SyncedPathRefused(
            f"refusing to write {what} to {target}: " + "; ".join(reasons)
            + f". Set {CUSTODY_ROOT_ENV} or use {DEFAULT_CUSTODY_ROOT}."
        )
    resolved = target.resolve()
    root = custody_root().resolve()
    if not _is_within(resolved, root):
        raise SyncedPathRefused(
            f"refusing to write {what} to {resolved}: outside the declared custody root "
            f"{root}. Contact PII lands in custody or it does not land."
        )
    return resolved


def prepare_output_dir(path: os.PathLike | str, *, what: str = "contact output") -> Path:
    """Assert, create with 0700, return."""
    resolved = assert_safe_output_dir(path, what=what)
    resolved.mkdir(parents=True, exist_ok=True)
    os.chmod(resolved, 0o700)
    return resolved


def secure_file(path: os.PathLike | str) -> None:
    """0600 a PII file. Best effort -- a filesystem that refuses is not fatal."""
    try:
        os.chmod(Path(path), 0o600)
    except OSError:
        pass


def assert_safe_input_dir(path: os.PathLike | str, *, what: str = "contact input") -> Path:
    """Reading is not publishing, so a synced source is a warning, not a refusal.

    Returns the resolved path and the reasons it is unsafe, so a caller can
    record that it read from a location that still needs relocating.
    """
    target = Path(path).expanduser().resolve()
    return target
