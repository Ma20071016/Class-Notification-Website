"""Bounded local Word previews, without uploading documents to another service.

LibreOffice produces PDF inside a network sandbox. The stdlib DOCX fallback
keeps text, basic formatting, tables and embedded raster images. Legacy DOC
needs LibreOffice, antiword or macOS textutil. Serve HTML with a sandbox CSP.
"""
from __future__ import annotations

import base64
from html import escape
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib

MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_ZIP_ENTRIES = 2048
MAX_EXPANDED_BYTES = 80 * 1024 * 1024
MAX_XML_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_TOTAL = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 80 * 1024 * 1024
CONVERSION_TIMEOUT = 35
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class DocumentPreviewError(ValueError):
    """A conversion failure whose message is safe to show to visitors."""


def _parse_xml(data: bytes) -> ET.Element:
    if len(data) > MAX_XML_BYTES:
        raise DocumentPreviewError("Word 文档内容过大，无法在线预览，请下载查看。")
    # Catch DTD/entity declarations in UTF-8, UTF-16 and UTF-32 before parsing.
    normalized = data.replace(b"\x00", b"").lower()
    if b"<!doctype" in normalized or b"<!entity" in normalized:
        raise DocumentPreviewError("Word 文档包含不支持的 XML 声明，无法在线预览。")
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError) as error:
        raise DocumentPreviewError("Word 文档内容损坏，无法在线预览。") from error
    pending = [(root, 0)]
    count = 0
    while pending:
        element, depth = pending.pop()
        count += 1
        if depth > 100 or count > 200000:
            raise DocumentPreviewError("Word 文档结构过于复杂，无法在线预览。")
        pending.extend((child, depth + 1) for child in element)
    # Strict OOXML uses different namespace URIs for the same body structure.
    namespaces = {
        "http://purl.oclc.org/ooxml/wordprocessingml/main": W[1:-1],
        "http://purl.oclc.org/ooxml/officeDocument/relationships": R[1:-1],
        "http://purl.oclc.org/ooxml/drawingml/main": A[1:-1],
    }
    for node in root.iter():
        for strict, transitional in namespaces.items():
            node.tag = node.tag.replace("{" + strict + "}", "{" + transitional + "}")
            node.attrib = {key.replace("{" + strict + "}", "{" + transitional + "}"): value
                           for key, value in node.attrib.items()}
    return root


def _read_docx(source: Path) -> tuple[ET.Element, dict[str, str], bool]:
    """Validate without extracting ZIP entries or resolving external resources."""
    images: dict[str, str] = {}
    external = False
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise DocumentPreviewError("Word 文档包含过多文件，无法在线预览。")
            if sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise DocumentPreviewError("Word 文档解压后过大，无法在线预览。")
            names = set()
            for entry in entries:
                name = entry.filename
                mode = entry.external_attr >> 16
                if (name in names or name.startswith("/") or "\\" in name
                        or ".." in PurePosixPath(name).parts or "\x00" in name
                        or entry.flag_bits & 1 or stat.S_ISLNK(mode)):
                    raise DocumentPreviewError("Word 文档压缩结构不受支持，无法在线预览。")
                names.add(name)
                if name.lower().endswith("vbaproject.bin"):
                    raise DocumentPreviewError("含宏的 Word 文档不支持在线预览。")
            if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                raise DocumentPreviewError("文件不是有效的 Word DOCX 文档。")

            def read_xml(name: str) -> ET.Element:
                if archive.getinfo(name).file_size > MAX_XML_BYTES:
                    raise DocumentPreviewError("Word 文档内容过大，无法在线预览，请下载查看。")
                return _parse_xml(archive.read(name))

            content_types = read_xml("[Content_Types].xml")
            if b"macroenabled" in ET.tostring(content_types).lower():
                raise DocumentPreviewError("含宏的 Word 文档不支持在线预览。")
            root = read_xml("word/document.xml")
            if root.tag != W + "document" or root.find(W + "body") is None:
                raise DocumentPreviewError("文件不是有效的 Word DOCX 文档。")
            main_relationships = None
            for name in names:
                if name.endswith(".rels"):
                    relationships = read_xml(name)
                    for relation in relationships:
                        target = relation.get("Target", "")
                        if (relation.get("TargetMode", "").lower() == "external"
                                or ":" in target or target.startswith("/")):
                            external = True
                    if name == "word/_rels/document.xml.rels":
                        main_relationships = relationships
            image_bytes = 0
            if main_relationships is not None:
                for relation in main_relationships:
                    if relation.tag != REL + "Relationship" or relation.get("TargetMode", "").lower() == "external":
                        continue
                    target = PurePosixPath(relation.get("Target", ""))
                    if target.is_absolute() or ".." in target.parts or not str(target).startswith("media/"):
                        continue
                    name = "word/" + str(target)
                    if name not in names or archive.getinfo(name).file_size > MAX_IMAGE_BYTES:
                        continue
                    if image_bytes + archive.getinfo(name).file_size > MAX_IMAGE_TOTAL:
                        continue
                    data = archive.read(name)
                    mime = _image_type(data)
                    if mime:
                        image_bytes += len(data)
                        images[relation.get("Id", "")] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            return root, images, external
    except DocumentPreviewError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyError, NotImplementedError, zlib.error) as error:
        raise DocumentPreviewError("Word 文档已损坏或已加密，无法在线预览。") from error


def _image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _html_page(content: str) -> str:
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'\">"
            "<title>Word 附件预览</title><style>"
            "body{margin:0;background:#f3f4f6;color:#1f2937;font:16px/1.8 system-ui,sans-serif}"
            "main{box-sizing:border-box;max-width:900px;margin:24px auto;padding:32px;background:white;overflow-wrap:anywhere}"
            ".notice{padding:12px 16px;background:#eff6ff;border:1px solid #bfdbfe;font-size:14px;margin-bottom:24px}"
            "p{white-space:pre-wrap;margin:0 0 12px;min-height:1em}table{border-collapse:collapse;max-width:100%;margin:16px 0}"
            "td{border:1px solid #cbd5e1;padding:8px;vertical-align:top}img{max-width:100%;height:auto}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}"
            "@media(max-width:600px){main{margin:0;padding:16px}}"
            "</style></head><body><main><aside class=\"notice\">简化预览：保留正文、表格和部分图片，分页及复杂排版可能与原文不同。请下载附件查看完整内容。</aside>"
            + content + "</main></body></html>")


def _docx_html(root: ET.Element, images: dict[str, str]) -> str:
    def render(node: ET.Element) -> str:
        if node.tag == W + "t":
            return escape(node.text or "")
        if node.tag == W + "tab":
            return "\t"
        if node.tag in {W + "br", W + "cr"}:
            return "<br>"
        if node.tag == A + "blip":
            image = images.get(node.get(R + "embed", ""))
            return f'<img src="{image}" alt="文档内嵌图片">' if image else ""
        if node.tag in {W + "del", W + "instrText", W + "object", W + "altChunk"}:
            return ""
        content = "".join(render(child) for child in node)
        if node.tag == W + "r":
            props = node.find(W + "rPr")
            if props is not None:
                for key, tag in (("b", "strong"), ("i", "em"), ("u", "u")):
                    value = props.find(W + key)
                    if value is not None and value.get(W + "val", "true") not in {"0", "false", "off", "none"}:
                        content = f"<{tag}>{content}</{tag}>"
        tags = {W + "p": "p", W + "tbl": "table", W + "tr": "tr", W + "tc": "td"}
        if node.tag in tags:
            tag = tags[node.tag]
            return f"<{tag}>{content}</{tag}>"
        return content

    return _html_page(render(root.find(W + "body")))


def _soffice_binary() -> str | None:
    configured = os.environ.get("CLASS_NOTICE_SOFFICE")
    if configured:
        return shutil.which(configured)
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    app = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    return str(app) if app.is_file() and os.access(app, os.X_OK) else None


def _network_sandbox(command: list[str], work: Path) -> list[str] | None:
    """Never silently run an office converter with network access."""
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        return ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)", *command]
    bubblewrap = shutil.which("bwrap")
    if bubblewrap:
        return [bubblewrap, "--die-with-parent", "--unshare-net", "--ro-bind", "/", "/",
                "--bind", str(work), str(work), "--proc", "/proc", "--dev", "/dev", "--", *command]
    return None


def _run(command: list[str], work: Path, *, capture: bool = False) -> bytes:
    """Bound wall time and kill the converter process group on timeout."""
    stdout = work / "converter-stdout.txt"
    environment = os.environ.copy()
    environment.update({"HOME": str(work), "TMPDIR": str(work), "SAL_USE_VCLPLUGIN": "svp"})
    if sys.platform == "darwin" and not environment.get("FONTCONFIG_FILE"):
        # Some headless macOS builds only scan bundled Latin fonts. Include
        # system CJK fonts per conversion so Chinese text is not lost in PDF.
        font_config = work / "fonts.conf"
        font_config.write_text(
            '<?xml version="1.0"?><fontconfig>'
            '<dir>/System/Library/Fonts</dir><dir>/System/Library/Fonts/Supplemental</dir>'
            '<dir>/Library/Fonts</dir><cachedir>' + escape(str(work / "font-cache"))
            + '</cachedir></fontconfig>', encoding="utf-8")
        environment["FONTCONFIG_FILE"] = str(font_config)
    with stdout.open("wb") as output:
        try:
            process = subprocess.Popen(command, cwd=work, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                code = process.wait(timeout=CONVERSION_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise DocumentPreviewError("Word 预览转换超时，请下载附件查看。")
        except OSError as error:
            raise DocumentPreviewError("Word 预览转换服务暂不可用，请下载附件查看。") from error
    if code != 0 or stdout.stat().st_size > MAX_OUTPUT_BYTES:
        raise DocumentPreviewError("Word 预览转换失败，请下载附件查看。")
    return stdout.read_bytes() if capture else b""


def _libreoffice_pdf(source: Path, output: Path, extension: str) -> Path | None:
    binary = _soffice_binary()
    if not binary:
        return None
    with tempfile.TemporaryDirectory(prefix="word-convert-", dir=output) as temporary:
        work = Path(temporary).resolve()
        document = work / ("document." + extension)
        shutil.copyfile(source, document)
        profile = work / "profile"
        (profile / "user").mkdir(parents=True)
        (profile / "user/registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>'
            '<item oor:path="/org.openoffice.Office.Writer/Content/Update">'
            '<prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item></oor:items>',
            encoding="utf-8")
        command = [binary, f"-env:UserInstallation={profile.as_uri()}", "--headless",
                   "--nologo", "--nodefault", "--nofirststartwizard", "--norestore",
                   "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(work), str(document)]
        sandboxed = _network_sandbox(command, work)
        if sandboxed is None:
            return None
        try:
            _run(sandboxed, work)
        except DocumentPreviewError:
            return None
        generated = work / "document.pdf"
        if not generated.is_file() or generated.is_symlink() or not 5 <= generated.stat().st_size <= MAX_OUTPUT_BYTES:
            return None
        with generated.open("rb") as result:
            if result.read(5) != b"%PDF-":
                return None
        target = output / f"preview-{uuid.uuid4().hex}.pdf"
        generated.replace(target)
        return target


def _legacy_doc_html(source: Path, output: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="word-text-", dir=output) as temporary:
        work = Path(temporary).resolve()
        antiword = shutil.which("antiword")
        textutil = shutil.which("textutil") if sys.platform == "darwin" else None
        command = None
        if antiword:
            # antiword reads DOC text and does not follow document links.
            command = [antiword, "-m", "UTF-8.txt", str(source)]
        elif textutil:
            command = _network_sandbox([textutil, "-convert", "txt", "-stdout", str(source)], work)
        if command is None:
            raise DocumentPreviewError("此服务器暂不支持旧版 DOC 预览，请下载查看，或请管理员改传 DOCX / PDF。")
        text = _run(command, work, capture=True).decode("utf-8", errors="replace").strip()
        if not text:
            raise DocumentPreviewError("未能读取 DOC 正文，请下载附件查看完整内容。")
        return _html_page("<pre>" + escape(text) + "</pre>")


def render_word_preview(source: Path, extension: str, output_dir: Path) -> Path:
    """Return a local .pdf/.html preview, or raise DocumentPreviewError.

    The caller owns output_dir and its cleanup lifetime. The original is never
    modified; original downloads should stay available when preview fails.
    """
    source = Path(source).resolve()
    output = Path(output_dir).resolve()
    extension = extension.lower().lstrip(".")
    if extension not in {"doc", "docx"}:
        raise DocumentPreviewError("仅支持 DOC 和 DOCX 格式的 Word 预览。")
    try:
        size = source.stat().st_size
    except OSError as error:
        raise DocumentPreviewError("附件文件不存在或无法读取。") from error
    if not source.is_file() or not 0 < size <= MAX_SOURCE_BYTES:
        raise DocumentPreviewError("Word 附件为空或过大，无法在线预览。")
    output.mkdir(parents=True, exist_ok=True)
    root = images = None
    external = False
    if extension == "docx":
        root, images, external = _read_docx(source)
    else:
        with source.open("rb") as file:
            if file.read(8) != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                raise DocumentPreviewError("文件不是有效的旧版 Word DOC 文档。")
    if not external:
        pdf = _libreoffice_pdf(source, output, extension)
        if pdf is not None:
            return pdf
    html = _docx_html(root, images) if extension == "docx" else _legacy_doc_html(source, output)
    if len(html.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise DocumentPreviewError("Word 预览内容过大，请下载附件查看。")
    target = output / f"preview-{uuid.uuid4().hex}.html"
    target.write_text(html, encoding="utf-8")
    return target
