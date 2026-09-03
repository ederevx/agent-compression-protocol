"""Repo-local runtime containment: `.acp/{state,metrics,raw,logs}/`.

Mirrors AALP's `.aalp/state/` root-resolution pattern (`ACP_HOME` env
var if set, else `Path.cwd()`) and its atomic-write discipline
(`aalp/credential.py`, `aalp/ingress.py::_atomic_write`) — reimplemented
here independently; ACP has no runtime import dependency on AALP.

`.acp/` is already covered by this repo's `.gitignore`; nothing in this
module writes outside that prefix.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

_SUBDIRS = ("state", "metrics", "raw", "logs")


class AcpContainmentError(ValueError):
    """A stable containment-store validation error."""


def resolve_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    configured = os.environ.get("ACP_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.cwd()


def ensure_dirs(root: str | Path | None = None) -> dict[str, Path]:
    """Create (if missing) and return the four `.acp/<name>/` paths, 0700."""
    base = resolve_root(root) / ".acp"
    paths: dict[str, Path] = {}
    for name in _SUBDIRS:
        path = base / name
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        paths[name] = path
    return paths


def _raw_dir(root: str | Path | None) -> Path:
    path = resolve_root(root) / ".acp" / "raw"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _validate_source_hash(source_hash: str) -> str:
    if not source_hash or any(
            character not in "0123456789abcdef" for character in source_hash):
        raise AcpContainmentError(
            f"source_hash must be a lowercase hex digest, got {source_hash!r}")
    return source_hash


def _raw_path(root: str | Path | None, source_hash: str) -> Path:
    return _raw_dir(root) / _validate_source_hash(source_hash)


def store_raw(root: str | Path | None, content: bytes, source_hash: str) -> Path:
    """Content-addressed, atomic write of `content` under `.acp/raw/`.

    Identical payloads (same `source_hash`) hash-dedupe for free: if a
    file for this hash already exists, it is left untouched rather than
    rewritten.
    """
    path = _raw_path(root, source_hash)
    if path.exists():
        return path
    directory = path.parent
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "wb")
        descriptor = -1
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        return path
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    else:
        temporary_path.unlink(missing_ok=True)


def read_raw(root: str | Path | None, source_hash: str) -> bytes:
    path = _raw_path(root, source_hash)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise AcpContainmentError(
            f"no raw payload stored for source_hash {source_hash!r}") from None


def expire_stale(
    root: str | Path | None,
    max_age_seconds: float,
    clock=time.time,
) -> list[Path]:
    """Delete `.acp/raw/` files older than `max_age_seconds`; return removed paths.

    Each file is a single content-addressed blob (not part of a
    multi-file transaction), so a plain `unlink` per file is
    crash-safe: an interrupted run simply leaves the remaining stale
    files for the next call.
    """
    directory = _raw_dir(root)
    now = clock()
    removed: list[Path] = []
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        age = now - entry.stat().st_mtime
        if age > max_age_seconds:
            entry.unlink()
            removed.append(entry)
    return removed
