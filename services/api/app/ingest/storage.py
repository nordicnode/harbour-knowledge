from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from ..config import get_settings


def _source_path(tenant_id: UUID | str, source_id: UUID | str, suffix: str) -> Path:
    settings = get_settings()
    safe_suffix = "".join(ch for ch in suffix if ch.isalnum() or ch == ".")[:12]
    directory = settings.storage_dir / str(tenant_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{source_id}{safe_suffix}"


def save_bytes(tenant_id: UUID, source_id: UUID, filename: str, data: bytes) -> str:
    path = _source_path(tenant_id, source_id, Path(filename).suffix.lower())
    path.write_bytes(data)
    return str(path.relative_to(get_settings().storage_dir))


def read_bytes(tenant_id: UUID, source_id: UUID, storage_path: str) -> bytes:
    settings = get_settings()
    path = (settings.storage_dir / storage_path).resolve()
    root = settings.storage_dir.resolve()
    if root not in path.parents:
        raise ValueError("Storage path escapes the configured storage directory")
    return path.read_bytes()


def remove_snapshot(storage_path: str) -> None:
    root = get_settings().storage_dir.resolve()
    path = (root / storage_path).resolve()
    if root not in path.parents:
        raise ValueError("Storage path escapes the configured storage directory")
    path.unlink(missing_ok=True)


def store_snapshot(tenant_id: UUID, source_id: UUID, data: bytes, content_type: str) -> str:
    suffix = ".html"
    if "markdown" in content_type:
        suffix = ".md"
    elif "pdf" in content_type:
        suffix = ".pdf"
    # Snapshots are immutable: replacing the file referenced by the current
    # source before its database transaction commits would corrupt a rollback.
    path = _source_path(tenant_id, f"{source_id}-{uuid4().hex}", suffix)
    try:
        path.write_bytes(data)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return str(path.relative_to(get_settings().storage_dir))
