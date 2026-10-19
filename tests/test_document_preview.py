"""Word preview safety, fallback and optional real-converter checks."""
import base64
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import document_preview as preview


CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
ROOT_RELS = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jfuoAAAAASUVORK5CYII=")


def make_docx(path, body=None, extras=None):
    body = body or '<w:p><w:r><w:t>通知测试 Preview test</w:t></w:r></w:p>'
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
           'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>'
           + body + '</w:body></w:document>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/document.xml", xml)
        for name, content in (extras or {}).items():
            archive.writestr(name, content)
    return path


class WordPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.source = self.folder / "附件.docx"
        self.output = self.folder / "previews"
        self.no_office = patch.object(preview, "_soffice_binary", return_value=None)
        self.no_office.start()
        self.addCleanup(self.no_office.stop)

    def render(self, **kwargs):
        make_docx(self.source, **kwargs)
        path = preview.render_word_preview(self.source, ".DOCX", self.output)
        self.assertEqual(path.parent, self.output.resolve())
        self.assertEqual(path.suffix, ".html")
        self.assertEqual(list(self.output.iterdir()), [path])
        return path.read_text(encoding="utf-8")

    def test_fallback_renders_chinese_basic_styles_table_and_safe_text(self):
        html = self.render(body='<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>中文 &lt;script&gt;alert(1)&lt;/script&gt;</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        self.assertIn('<meta charset="utf-8">', html)
        self.assertIn("<strong>中文 &lt;script&gt;alert(1)&lt;/script&gt;</strong>", html)
        self.assertNotIn("<script>", html)
        self.assertIn("<table><tr><td>", html)
        self.assertIn("简化预览", html)
        self.assertIn("default-src 'none'", html)

    def test_embedded_png_is_inline_and_svg_is_omitted(self):
        rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="png" Target="media/a.png"/><Relationship Id="svg" Target="media/b.svg"/></Relationships>'
        html = self.render(body='<w:p><w:r><w:drawing><a:blip r:embed="png"/><a:blip r:embed="svg"/></w:drawing></w:r></w:p>', extras={"word/_rels/document.xml.rels": rels, "word/media/a.png": PNG, "word/media/b.svg": '<svg onload="alert(1)"/>'})
        self.assertIn('src="data:image/png;base64,', html)
        self.assertEqual(html.count("<img "), 1)
        self.assertNotIn("onload", html)

    def test_external_resources_never_reach_converter(self):
        rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="link" TargetMode="External" Target="http://127.0.0.1/secrets"/></Relationships>'
        with patch.object(preview, "_libreoffice_pdf") as convert:
            html = self.render(body='<w:p><w:hyperlink r:id="link"><w:r><w:t>链接文字</w:t></w:r></w:hyperlink></w:p>', extras={"word/_rels/document.xml.rels": rels})
        convert.assert_not_called()
        self.assertIn("链接文字", html)
        self.assertNotIn("127.0.0.1", html)
        self.assertNotIn("<a ", html)

    def test_strict_ooxml_fallback_preserves_text_and_styles(self):
        xml = ('<w:document xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main">'
               '<w:body><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>严格格式</w:t>'
               '</w:r></w:p></w:body></w:document>')
        root = preview._parse_xml(xml.encode())
        html = preview._docx_html(root, {})
        self.assertIn("<strong>严格格式</strong>", html)

    def test_declared_entities_and_utf16_entities_are_rejected(self):
        for xml in (b'<!DOCTYPE x [<!ENTITY x "boom">]><x>&x;</x>', '<!DOCTYPE x [<!ENTITY x "boom">]><x>&x;</x>'.encode("utf-16")):
            with self.subTest(xml=xml[:20]), self.assertRaises(preview.DocumentPreviewError):
                preview._parse_xml(xml)

    def test_deep_xml_is_rejected(self):
        with self.assertRaises(preview.DocumentPreviewError):
            preview._parse_xml(("<a>" * 102 + "</a>" * 102).encode())

    def test_invalid_xml_and_missing_word_document_are_rejected(self):
        with self.assertRaises(preview.DocumentPreviewError):
            preview._parse_xml(b"<unclosed>")
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        with self.assertRaises(preview.DocumentPreviewError):
            preview.render_word_preview(self.source, "docx", self.output)

    def test_traversal_and_macros_are_rejected(self):
        for extras in ({"../escape": "bad"}, {"word/vbaProject.bin": b"bad"}):
            with self.subTest(extras=list(extras)):
                make_docx(self.source, extras=extras)
                with self.assertRaises(preview.DocumentPreviewError):
                    preview.render_word_preview(self.source, "docx", self.output)
        self.assertFalse((self.folder / "escape").exists())

    def test_zip_and_xml_resource_limits(self):
        make_docx(self.source)
        for limit, value in (("MAX_ZIP_ENTRIES", 1), ("MAX_EXPANDED_BYTES", 10), ("MAX_XML_BYTES", 10)):
            with self.subTest(limit=limit), patch.object(preview, limit, value):
                with self.assertRaises(preview.DocumentPreviewError):
                    preview.render_word_preview(self.source, "docx", self.output)

    def test_oversize_missing_empty_and_invalid_sources(self):
        with self.assertRaises(preview.DocumentPreviewError):
            preview.render_word_preview(self.source, "docx", self.output)
        self.source.write_bytes(b"")
        with self.assertRaises(preview.DocumentPreviewError):
            preview.render_word_preview(self.source, "docx", self.output)
        make_docx(self.source)
        with patch.object(preview, "MAX_SOURCE_BYTES", 1), self.assertRaises(preview.DocumentPreviewError):
            preview.render_word_preview(self.source, "docx", self.output)
        with self.assertRaises(preview.DocumentPreviewError):
            preview.render_word_preview(self.source, "html", self.output)
        with self.assertRaises(preview.DocumentPreviewError):
            preview.render_word_preview(self.source, "doc", self.output)

    def test_timeout_kills_converter_process_group(self):
        process = MagicMock()
        process.pid = 12345
        process.wait.side_effect = [subprocess.TimeoutExpired(["soffice"], 35), 0]
        with patch.object(preview.subprocess, "Popen", return_value=process) as popen, patch.object(preview.os, "killpg") as kill:
            with self.assertRaisesRegex(preview.DocumentPreviewError, "超时"):
                preview._run(["soffice", "file.docx"], self.folder)
        kill.assert_called_once_with(12345, preview.signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_converter_failure_falls_back_and_cleans_profile(self):
        make_docx(self.source)
        with patch.object(preview, "_soffice_binary", return_value="/fake/soffice"), patch.object(preview, "_network_sandbox", side_effect=lambda command, work: command), patch.object(preview, "_run", side_effect=preview.DocumentPreviewError("失败")):
            path = preview.render_word_preview(self.source, "docx", self.output)
        self.assertEqual(path.suffix, ".html")
        self.assertEqual(list(self.output.iterdir()), [path])

    def test_converter_without_network_sandbox_never_runs(self):
        make_docx(self.source)
        with patch.object(preview, "_soffice_binary", return_value="/fake/soffice"), patch.object(preview, "_network_sandbox", return_value=None), patch.object(preview, "_run") as run:
            path = preview.render_word_preview(self.source, "docx", self.output)
        run.assert_not_called()
        self.assertEqual(path.suffix, ".html")

    def test_successful_pdf_is_validated_and_temp_files_removed(self):
        make_docx(self.source)
        def convert(command, work):
            self.assertIn("--headless", command)
            self.assertIn("-env:UserInstallation=", " ".join(command))
            (work / "document.pdf").write_bytes(b"%PDF-1.4\nfixture")
        with patch.object(preview, "_soffice_binary", return_value="/fake/soffice"), patch.object(preview, "_network_sandbox", side_effect=lambda command, work: command), patch.object(preview, "_run", side_effect=convert):
            path = preview.render_word_preview(self.source, "docx", self.output)
        self.assertEqual(path.suffix, ".pdf")
        self.assertEqual(list(self.output.iterdir()), [path])

    def test_converter_zero_exit_without_pdf_falls_back(self):
        make_docx(self.source)
        with patch.object(preview, "_soffice_binary", return_value="/fake/soffice"), patch.object(preview, "_network_sandbox", side_effect=lambda command, work: command), patch.object(preview, "_run", return_value=b""):
            path = preview.render_word_preview(self.source, "docx", self.output)
        self.assertEqual(path.suffix, ".html")

    def test_legacy_doc_text_is_escaped(self):
        self.source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504)
        with patch.object(preview.shutil, "which", side_effect=lambda name: "/usr/bin/antiword" if name == "antiword" else None), patch.object(preview, "_run", return_value="通知 <script>bad</script>".encode()):
            result = preview.render_word_preview(self.source, "doc", self.output).read_text()
        self.assertIn("通知 &lt;script&gt;bad&lt;/script&gt;", result)
        self.assertNotIn("<script>", result)

    def test_legacy_doc_without_converter_has_clear_error(self):
        self.source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504)
        with patch.object(preview.shutil, "which", return_value=None), self.assertRaisesRegex(preview.DocumentPreviewError, "旧版 DOC"):
            preview.render_word_preview(self.source, "doc", self.output)


@unittest.skipUnless(os.environ.get("CLASS_NOTICE_RUN_OFFICE_TESTS") == "1", "Set CLASS_NOTICE_RUN_OFFICE_TESTS=1 for installed LibreOffice integration")
class InstalledLibreOfficeTests(unittest.TestCase):
    def assert_pdf_content(self, path):
        self.assertTrue(path.read_bytes().startswith(b"%PDF-"))
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        text = "".join(page.extract_text() for page in PdfReader(path).pages)
        self.assertIn("通知测试", text)
        self.assertIn("Preview test", text)

    def test_real_docx_and_legacy_doc_generate_pdf_and_text_fallback(self):
        binary = preview._soffice_binary()
        self.assertIsNotNone(binary, "LibreOffice is required for this opt-in test")
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary).resolve()
            source = make_docx(work / "fixture.docx")
            result = preview.render_word_preview(source, "docx", work / "docx-preview")
            self.assertEqual(result.suffix, ".pdf")
            self.assert_pdf_content(result)
            profile = work / "fixture-profile"
            command = preview._network_sandbox([binary, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--convert-to", "doc:MS Word 97", "--outdir", str(work), str(source)], work)
            self.assertIsNotNone(command)
            preview._run(command, work)
            legacy = work / "fixture.doc"
            self.assertTrue(legacy.is_file())
            from attachments import validate_document
            validate_document(legacy, "doc")
            result = preview.render_word_preview(legacy, "doc", work / "doc-preview")
            self.assertEqual(result.suffix, ".pdf")
            self.assert_pdf_content(result)
            if shutil.which("antiword") or shutil.which("textutil"):
                with patch.object(preview, "_soffice_binary", return_value=None):
                    text_result = preview.render_word_preview(legacy, "doc", work / "doc-text")
                self.assertIn("Preview test", text_result.read_text())


if __name__ == "__main__":
    unittest.main()
