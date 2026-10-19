#!/usr/bin/env python3
"""Build a source-only release archive from an explicit allowlist."""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import tarfile
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_FILES = (
    "app.py", "attachments.py", "document_preview.py", "requirements.txt", "requirements-production.txt",
    "gunicorn.conf.py", "README.md", "CONTRIBUTING.md",
)
RELEASE_DIRECTORIES = ("templates", "static", "deploy", "tests", "database")
EXCLUDED_NAMES = {
    "instance", "uploads", "backups", "dist", ".venv", "venv", ".openai", ".git",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
    "node_modules", ".DS_Store", "secret_key", "admin-credentials.txt",
}
EXCLUDED_SUFFIXES = (
    ".pyc", ".pyo", ".sqlite", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm",
    ".sqlite3-journal", ".db", ".db-wal", ".db-shm", ".db-journal",
    ".tar", ".tar.gz", ".tgz", ".zip", ".tar.bz2", ".tar.xz",
)


def _excluded(path: Path) -> bool:
    return (
        path.name in EXCLUDED_NAMES
        or path.name == ".env"
        or path.name.startswith(".env.")
        or path.name.startswith("._")
        or path.name.endswith(EXCLUDED_SUFFIXES)
    )


def _release_members(project_root: Path) -> list[Path]:
    members = []

    def visit(path: Path) -> None:
        if path.is_symlink():
            raise ValueError(f"Symbolic links are not allowed in a release: {path}")
        # Browser screenshots are generated evidence, not source release assets.
        if path == project_root / "tests" / "artifacts" or _excluded(path):
            return
        if path.is_dir():
            members.append(path)
            for child in sorted(path.iterdir()):
                visit(child)
        elif path.is_file():
            members.append(path)
        else:
            raise ValueError(f"Release member must be a regular file or directory: {path}")

    for name in RELEASE_FILES + RELEASE_DIRECTORIES:
        path = project_root / name
        if path.is_symlink():
            raise ValueError(f"Symbolic links are not allowed in a release: {path}")
        expected_type = path.is_file if name in RELEASE_FILES else path.is_dir
        if not expected_type():
            raise ValueError(f"Required release entry is missing or has the wrong type: {path}")
        visit(path)
    return members


def build_release(project_root: Path = PROJECT_ROOT, output: Path | None = None) -> Path:
    """Return a new archive path; never include data or replace an existing file."""
    project_root = Path(project_root).expanduser().absolute()
    members = _release_members(project_root)
    created_at = datetime.now(timezone.utc)
    if output is None:
        name = created_at.strftime("class-notice-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12] + ".tar.gz"
        output = project_root / "dist" / name
    else:
        output = Path(output).expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL also refuses an existing symlink; code archives are owner-only at rest.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            with tarfile.open(fileobj=destination, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
                top = tarfile.TarInfo("class-notice")
                top.type = tarfile.DIRTYPE
                top.mode = 0o755
                top.mtime = int(created_at.timestamp())
                archive.addfile(top)
                for path in members:
                    entry_stat = path.lstat()
                    name = "class-notice/" + path.relative_to(project_root).as_posix()
                    info = tarfile.TarInfo(name)
                    info.mtime = int(entry_stat.st_mtime)
                    if stat.S_ISDIR(entry_stat.st_mode):
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        archive.addfile(info)
                    elif stat.S_ISREG(entry_stat.st_mode):
                        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                        with os.fdopen(os.open(path, flags), "rb") as source:
                            actual = os.fstat(source.fileno())
                            if not stat.S_ISREG(actual.st_mode):
                                raise ValueError(f"Release source is not a regular file: {path}")
                            info.size = actual.st_size
                            info.mode = 0o755 if actual.st_mode & 0o111 else 0o644
                            archive.addfile(info, source)
                    else:
                        raise ValueError(f"Symbolic links or special files are not allowed: {path}")
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New .tar.gz file (default: project dist/class-notice-UTC-UNIQUE.tar.gz).")
    args = parser.parse_args()
    try:
        output = build_release(output=args.output)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"Release build failed: {error}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
