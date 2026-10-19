"""Bounded document validation and private, transactional attachment storage."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import tempfile
import unicodedata
import zipfile
import zlib
from xml.etree import ElementTree


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 5
MAX_REQUEST_BYTES = 101 * 1024 * 1024
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
ATTACHMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size > 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_task ON attachments(task_id);
"""


class AttachmentValidationError(ValueError):
    """A safe, user-facing error for a rejected upload."""


@dataclass
class StagedAttachment:
    name: str
    stored_name: str
    media_type: str
    size: int
    path: Path


class StagedUploads:
    def __init__(self):
        self.files: list[StagedAttachment] = []
        self.published_paths: list[Path] = []
        self.committed = False

    def publish(self, database, task_id: int, created_at: str, folder: Path):
        """Called while holding SQLite's write transaction."""
        for item in self.files:
            destination = storage_path(folder, item.stored_name)
            # A hard link publishes atomically without overwriting even an
            # extraordinarily unlikely random-name collision. Both paths live
            # on the same filesystem and only our private directory is writable.
            os.link(item.path, destination)
            self.published_paths.append(destination)
            item.path.unlink()
            item.path = destination
            database.execute(
                "INSERT INTO attachments (task_id, name, stored_name, media_type, size, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, item.name, item.stored_name, item.media_type, item.size, created_at),
            )


def storage_path(folder: Path, stored_name: str) -> Path:
    """Resolve only our generated basenames, including for legacy/tampered rows."""
    if not re.fullmatch(r"[0-9a-f]{32}\.(pdf|doc|docx)", stored_name):
        raise AttachmentValidationError("附件存储信息无效，请联系管理员。")
    path = folder / stored_name
    if path.is_symlink():
        raise AttachmentValidationError("附件存储信息无效，请联系管理员。")
    return path


def clean_filename(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise AttachmentValidationError("请选择需要上传的 Word 或 PDF 文件。")
    name = value.strip()
    if (len(name) > 180 or len(name.encode("utf-8")) > 512
            or any(char in name for char in ("/", "\\", ":"))
            or any(unicodedata.category(char).startswith("C") for char in name)
            or name.startswith(".")):
        raise AttachmentValidationError("附件文件名无效，请使用不含路径或控制字符的简短文件名。")
    extension = Path(name).suffix.lower().lstrip(".")
    if extension not in MEDIA_TYPES:
        raise AttachmentValidationError("只支持 PDF、Word（.doc、.docx）附件。")
    return name, extension


class _SafeXMLBuilder(ElementTree.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise AttachmentValidationError("Word 文件包含不支持的 XML 定义，请另存为普通 .docx 后上传。")


def _xml(data: bytes):
    return ElementTree.fromstring(data, parser=ElementTree.XMLParser(target=_SafeXMLBuilder()))


def validate_docx(path: Path) -> None:
    """Reject malformed packages, macros, traversal and ZIP expansion attacks."""
    try:
        with zipfile.ZipFile(path) as package:
            entries = package.infolist()
            if not 2 <= len(entries) <= 2048:
                raise AttachmentValidationError("Word 文件结构过于复杂或不完整。")
            names = set()
            expanded = 0
            xml_parts = {}
            for entry in entries:
                name = entry.filename
                member = PurePosixPath(name)
                expanded += entry.file_size
                if (name in names or not name or member.is_absolute()
                        or ".." in member.parts or "\\" in name or ":" in name
                        or any(ord(char) < 32 for char in name)
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or entry.flag_bits & 1
                        or expanded > 80 * 1024 * 1024
                        or entry.file_size > 40 * 1024 * 1024
                        or entry.file_size > max(entry.compress_size, 1) * 1000
                        or name.lower().endswith("vbaproject.bin")):
                    raise AttachmentValidationError("Word 文件包含不安全或过大的压缩内容。")
                names.add(name)
                if entry.is_dir():
                    continue
                # Reading every member verifies CRC and rejects overlapping ZIP
                # entries. Streaming keeps images/embedded data out of memory.
                collected = bytearray()
                actual_size = 0
                with package.open(entry) as stream:
                    while chunk := stream.read(64 * 1024):
                        actual_size += len(chunk)
                        if actual_size > entry.file_size:
                            raise AttachmentValidationError("Word 文件压缩内容无效。")
                        if name in {"[Content_Types].xml", "word/document.xml"}:
                            collected.extend(chunk)
                if name in {"[Content_Types].xml", "word/document.xml"}:
                    xml_parts[name] = _xml(bytes(collected))
            if set(xml_parts) != {"[Content_Types].xml", "word/document.xml"}:
                raise AttachmentValidationError("该文件不是有效的 Word .docx 文档。")
            content_types = xml_parts["[Content_Types].xml"]
            expected = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
            if not any(
                node.get("PartName") == "/word/document.xml" and node.get("ContentType") == expected
                for node in content_types
            ):
                raise AttachmentValidationError("只支持普通 Word .docx 文档，不支持宏或其他 Office 文件。")
            document = xml_parts["word/document.xml"]
            namespaces = {
                "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                "http://purl.oclc.org/ooxml/wordprocessingml/main",
            }
            if not any(document.tag == f"{{{ns}}}document" and document.find(f"{{{ns}}}body") is not None for ns in namespaces):
                raise AttachmentValidationError("Word 文档正文结构不正确。")
    except AttachmentValidationError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, ElementTree.ParseError, KeyError, RuntimeError, ValueError, NotImplementedError, EOFError) as exc:
        raise AttachmentValidationError("Word 文件损坏或格式不正确，请重新保存后上传。") from exc


def validate_document(path: Path, extension: str) -> None:
    with path.open("rb") as stream:
        header = stream.read(512)
    if extension == "pdf":
        if not re.match(rb"%PDF-[12]\.[0-9](?:\r|\n|[ \t])", header):
            raise AttachmentValidationError("PDF 文件内容与扩展名不符，请上传有效的 PDF。")
    elif extension == "doc":
        if (len(header) != 512 or header[:8] != bytes.fromhex("d0cf11e0a1b11ae1")
                or header[28:30] != b"\xfe\xff"
                or int.from_bytes(header[30:32], "little") not in {9, 12}
                or "WordDocument".encode("utf-16-le") not in path.read_bytes()):
            raise AttachmentValidationError("该文件不是有效的 Word .doc 文档，请另存为 .docx 后上传。")
    else:
        validate_docx(path)


@contextmanager
def stage_uploads(files, folder: Path):
    if len(files) > MAX_ATTACHMENTS:
        raise AttachmentValidationError(f"每条通知最多保留 {MAX_ATTACHMENTS} 个附件。")
    staged = StagedUploads()
    try:
        if files:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        for upload in files:
            name, extension = clean_filename(upload.filename)
            supplied_type = (upload.mimetype or "").lower()
            accepted_types = {"", "application/octet-stream", MEDIA_TYPES[extension]}
            if extension == "docx":
                accepted_types.update({"application/zip", "application/x-zip-compressed"})
            if supplied_type not in accepted_types:
                raise AttachmentValidationError("附件类型与文件扩展名不符，请重新选择 Word 或 PDF 文件。")
            with tempfile.NamedTemporaryFile(prefix=".upload-", dir=folder, delete=False) as stream:
                item = StagedAttachment(name, secrets.token_hex(16) + "." + extension, MEDIA_TYPES[extension], 0, Path(stream.name))
                staged.files.append(item)
                while chunk := upload.stream.read(64 * 1024):
                    item.size += len(chunk)
                    if item.size > MAX_ATTACHMENT_BYTES:
                        raise AttachmentValidationError("单个附件不能超过 20 MB。")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if not item.size:
                raise AttachmentValidationError("不能上传空文件。")
            validate_document(item.path, extension)
        yield staged
    finally:
        if not staged.committed:
            for item in staged.files:
                item.path.unlink(missing_ok=True)
            for path in staged.published_paths:
                path.unlink(missing_ok=True)
