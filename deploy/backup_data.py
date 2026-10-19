#!/usr/bin/env python3
"""Back up an existing instance without importing or initializing the app."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_NAME = "class_notice.sqlite3"
SECRET_NAME = "secret_key"
UPLOAD_DIRECTORY = "uploads"
EXTERNAL_SECRET_NOTE = (
    "No secret_key file was present. If CLASS_NOTICE_SECRET_KEY is configured "
    "externally, keep that value separately in secure storage; it is not in this backup."
)


def _private_file(path: Path):
    """Create a new owner-only file, refusing existing paths."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _existing_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected an existing regular file: {path}")


def _copy_attachments(snapshot: sqlite3.Connection, instance: Path, backup: Path) -> list[dict]:
    """Copy only immutable originals referenced by the locked database snapshot."""
    if snapshot.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'attachments'"
    ).fetchone() is None:
        return []
    attachments = snapshot.execute(
        "SELECT id, stored_name, size FROM attachments ORDER BY id"
    ).fetchall()
    if not attachments:
        return []
    seen = set()
    for attachment_id, name, size in attachments:
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or "/" in name or "\\" in name or "\x00" in name or name in seen):
            raise ValueError(f"Unsafe or duplicate stored attachment name for id {attachment_id}.")
        if not isinstance(size, int) or size < 0:
            raise ValueError(f"Invalid attachment size for id {attachment_id}.")
        seen.add(name)
    source_directory = instance / UPLOAD_DIRECTORY
    if source_directory.is_symlink() or not source_directory.is_dir():
        raise ValueError("Attachments must be stored in an existing regular uploads directory.")
    destination = backup / UPLOAD_DIRECTORY
    destination.mkdir(mode=0o700)
    # Pin the directory descriptor and never follow links in stored filenames.
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory_descriptor = os.open(source_directory, directory_flags)
    manifest = []
    try:
        for attachment_id, name, size in attachments:
            with os.fdopen(os.open(name, file_flags, dir_fd=directory_descriptor), "rb") as original:
                actual = os.fstat(original.fileno())
                if not stat.S_ISREG(actual.st_mode) or actual.st_size != size:
                    raise ValueError(f"Missing, non-regular or size-mismatched attachment: {name}")
                with _private_file(destination / name) as copied:
                    shutil.copyfileobj(original, copied)
            if (destination / name).stat().st_size != size:
                raise ValueError(f"Attachment changed while being backed up: {name}")
            manifest.append({"id": attachment_id, "stored_name": name, "size": size})
    finally:
        os.close(directory_descriptor)
    return manifest


def create_backup(instance: Path, output: Path | None = None) -> Path:
    """Return a private, verified SQLite snapshot and its referenced attachments."""
    instance = Path(instance).expanduser().absolute()
    source = instance / DATABASE_NAME
    _existing_regular_file(source)
    secret = instance / SECRET_NAME
    if secret.exists() or secret.is_symlink():
        _existing_regular_file(secret)
    output = Path(output).expanduser().absolute() if output is not None else instance / "backups"
    created_at = datetime.now(timezone.utc)
    backup_directory = None
    # mode=rw refuses a missing source. The reserved write lock blocks attachment
    # metadata changes/deletes until the snapshot and its files are copied.
    # A separate read-only connection performs backup: backing up from a
    # connection holding a write transaction itself would wait indefinitely.
    with closing(sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=10)) as lock:
        try:
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup_directory = Path(tempfile.mkdtemp(
                prefix=created_at.strftime("%Y%m%dT%H%M%SZ-"), dir=output
            ))
            backup_directory.chmod(0o700)
            lock.execute("BEGIN IMMEDIATE")
            destination = backup_directory / DATABASE_NAME
            with _private_file(destination):
                pass
            with closing(sqlite3.connect(destination)) as snapshot:
                with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as original:
                    original.backup(snapshot)
                integrity = snapshot.execute("PRAGMA integrity_check").fetchall()
                if integrity != [("ok",)]:
                    raise ValueError("Database integrity check failed; no backup was retained.")
                if snapshot.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Database foreign key check failed; no backup was retained.")
                tables = snapshot.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT GLOB 'sqlite_*' ORDER BY name"
                ).fetchall()
                table_rows = {}
                for (name,) in tables:
                    quoted = '"' + name.replace('"', '""') + '"'
                    table_rows[name] = snapshot.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                attachment_files = _copy_attachments(snapshot, instance, backup_directory)
            copied_secret = False
            if secret.exists():
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                with os.fdopen(os.open(secret, flags), "rb") as source_secret:
                    if not stat.S_ISREG(os.fstat(source_secret.fileno()).st_mode):
                        raise ValueError("The secret_key source must be a regular file.")
                    with _private_file(backup_directory / SECRET_NAME) as saved_secret:
                        shutil.copyfileobj(source_secret, saved_secret)
                copied_secret = True
            metadata = {
                "created_at_utc": created_at.isoformat().replace("+00:00", "Z"),
                "database_file": DATABASE_NAME,
                "table_row_counts": table_rows,
                "secret_key_copied": copied_secret,
                "attachment_files": attachment_files,
                "attachment_count": len(attachment_files),
                "attachment_bytes": sum(item["size"] for item in attachment_files),
            }
            if not copied_secret:
                metadata["external_secret_key_note"] = EXTERNAL_SECRET_NOTE
            with _private_file(backup_directory / "backup-info.json") as info:
                info.write((json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            lock.rollback()
        except Exception:
            if backup_directory is not None:
                shutil.rmtree(backup_directory)
            raise
    return backup_directory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--instance", type=Path,
        default=Path(os.environ.get("CLASS_NOTICE_INSTANCE_PATH") or PROJECT_ROOT / "instance"),
        help="Existing instance directory (default: CLASS_NOTICE_INSTANCE_PATH or project instance).",
    )
    parser.add_argument("--output", type=Path, help="Backup parent directory (default: INSTANCE/backups).")
    args = parser.parse_args()
    try:
        backup = create_backup(args.instance, args.output)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"Backup failed: {error}\n")
    print(backup)
    if not (backup / SECRET_NAME).exists():
        print(EXTERNAL_SECRET_NOTE, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
