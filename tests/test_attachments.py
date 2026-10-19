"""Attachment transport, storage isolation and mutation atomicity tests."""

import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from werkzeug.datastructures import MultiDict

from app import create_app
from attachments import MAX_ATTACHMENT_BYTES, MAX_REQUEST_BYTES, StagedUploads
from document_preview import DocumentPreviewError


PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
CONTENT_TYPES = b'''<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'''
DOCUMENT = b'''<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello attachment</w:t></w:r></w:p></w:body></w:document>'''


def docx_bytes(extra=None, *, document=DOCUMENT, content_types=CONTENT_TYPES):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return stream.getvalue()


class AttachmentAPITestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            "TESTING": True, "SECRET_KEY": "isolated-attachments-tests",
            "DATABASE": str(self.root / "notices.sqlite3"),
        }
        self.app = create_app(self.config)
        with sqlite3.connect(self.config["DATABASE"]) as database:
            database.executemany(
                "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, ?)",
                [(1, "manager", "test-placeholder", "admin"), (2, "reader", "test-placeholder", "user")],
            )
        self.client = self.app.test_client()
        self.token = "attachment-csrf-token"
        self.authenticate()

    def authenticate(self, client=None, user_id=1):
        with (client or self.client).session_transaction() as browser:
            browser["user_id"] = user_id
            browser["csrf_token"] = self.token

    def payload(self, **changes):
        result = {"title": "文档通知", "content": "请阅读附件。", "category": "course"}
        result.update(changes)
        return result

    def submit(self, files=(), payload=None, task_id=None, client=None, token=True):
        data = MultiDict([("payload", json.dumps(self.payload() if payload is None else payload))])
        for item in files:
            content, name, *mimetype = item
            file = (io.BytesIO(content), name)
            if mimetype:
                file += (mimetype[0],)
            data.add("attachments", file)
        return (client or self.client).open(
            "/api/tasks" if task_id is None else f"/api/tasks/{task_id}",
            method="POST" if task_id is None else "PATCH",
            data=data, content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.token} if token else {},
        )

    def create(self, files=((PDF, "课程说明.pdf"),)):
        response = self.submit(files)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["task"]

    def stored_files(self):
        folder = self.root / "uploads"
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    def tasks(self):
        return self.client.get("/api/tasks").get_json()["tasks"]

    def test_upload_metadata_public_download_preview_and_restart(self):
        task = self.create()
        self.assertEqual(len(task["attachments"]), 1)
        item = task["attachments"][0]
        self.assertEqual(set(item), {"id", "name", "size", "extension", "previewUrl", "downloadUrl"})
        self.assertEqual((item["name"], item["size"], item["extension"]), ("课程说明.pdf", len(PDF), "pdf"))
        self.assertEqual(self.app.config["UPLOAD_FOLDER"], str(self.root / "uploads"))
        self.assertRegex(self.stored_files()[0], r"^[0-9a-f]{32}\.pdf$")
        public = self.app.test_client()
        download = public.get(item["downloadUrl"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, PDF)
        self.assertEqual(download.mimetype, "application/pdf")
        self.assertTrue(download.headers["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(download.headers["X-Frame-Options"], "DENY")
        download.close()
        preview = public.get(item["previewUrl"])
        self.assertEqual(preview.data, PDF)
        self.assertTrue(preview.headers["Content-Disposition"].startswith("inline;"))
        self.assertEqual(preview.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(preview.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'self'", preview.headers["Content-Security-Policy"])
        preview.close()
        restarted = create_app(self.config).test_client()
        self.assertEqual(restarted.get("/api/tasks").get_json()["tasks"][0], task)
        response = restarted.get(item["downloadUrl"])
        self.assertEqual(response.data, PDF)
        response.close()

    def test_multipart_requires_admin_and_csrf(self):
        anonymous = self.app.test_client()
        reader = self.app.test_client()
        self.authenticate(reader, user_id=2)
        for client, token, expected in ((anonymous, True, 401), (reader, True, 403), (self.client, False, 403)):
            with self.subTest(status=expected, token=token):
                response = self.submit([(PDF, "test.pdf")], client=client, token=token)
                self.assertEqual(response.status_code, expected)
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.stored_files(), [])

    def test_json_clients_work_and_preserve_attachments(self):
        task = self.create()
        response = self.client.patch(f"/api/tasks/{task['id']}", json={"pinned": True}, headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["task"]["attachments"], task["attachments"])
        response = self.client.post("/api/tasks", json=self.payload(), headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["task"]["attachments"], [])

    def test_edit_append_remove_attachment_only_and_delete_notice(self):
        task = self.create()
        old = task["attachments"][0]
        original_disk = self.stored_files()[0]
        response = self.submit([(docx_bytes(), "报告.docx")], payload={"title": "新标题"}, task_id=task["id"])
        self.assertEqual(response.status_code, 200, response.get_json())
        updated = response.get_json()["task"]
        self.assertEqual(updated["title"], "新标题")
        self.assertEqual(len(updated["attachments"]), 2)
        response = self.submit(payload={"removedAttachmentIds": [old["id"]]}, task_id=task["id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["task"]["attachments"]), 1)
        self.assertNotIn(original_disk, self.stored_files())
        self.assertEqual(self.client.get(old["downloadUrl"]).status_code, 404)
        self.assertEqual(self.client.get(old["previewUrl"]).status_code, 404)
        response = self.client.delete(f"/api/tasks/{task['id']}", headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.stored_files(), [])
        with sqlite3.connect(self.config["DATABASE"]) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], 0)

    def test_invalid_second_file_rolls_back_create_and_edit(self):
        response = self.submit([(PDF, "ok.pdf"), (b"not a PDF", "broken.pdf")])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.stored_files(), [])
        task = self.create()
        before_files = self.stored_files()
        response = self.submit([(PDF, "valid.pdf"), (b"bad", "invalid.docx")],
                               payload={"title": "must roll back", "removedAttachmentIds": [task["attachments"][0]["id"]]}, task_id=task["id"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.tasks(), [task])
        self.assertEqual(self.stored_files(), before_files)

    def test_published_files_and_metadata_roll_back_on_storage_failure(self):
        task = self.create()
        before = self.stored_files()
        original_publish = StagedUploads.publish
        def fail_after_publish(staged, *args, **kwargs):
            original_publish(staged, *args, **kwargs)
            raise OSError("simulated disk failure")
        with self.assertLogs(self.app.logger, level="ERROR"), patch.object(StagedUploads, "publish", fail_after_publish):
            response = self.submit([(PDF, "new.pdf")], payload={"title": "bad edit", "removedAttachmentIds": [task["attachments"][0]["id"]]}, task_id=task["id"])
            self.assertEqual(response.status_code, 503)
            response = self.submit([(PDF, "new.pdf")])
            self.assertEqual(response.status_code, 503)
        self.assertEqual(self.tasks(), [task])
        self.assertEqual(self.stored_files(), before)

    def test_five_attachment_limit_and_replacement_at_limit(self):
        files = [(PDF, f"part-{number}.pdf") for number in range(5)]
        task = self.create(files)
        before = self.stored_files()
        response = self.submit([(PDF, "sixth.pdf")], payload={"title": "no change"}, task_id=task["id"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.tasks(), [task])
        self.assertEqual(self.stored_files(), before)
        response = self.submit(files + [(PDF, "sixth.pdf")])
        self.assertEqual(response.status_code, 400)
        response = self.submit([(PDF, "replacement.pdf")], payload={"removedAttachmentIds": [task["attachments"][0]["id"]]}, task_id=task["id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["task"]["attachments"]), 5)
        self.assertEqual(len(self.stored_files()), 5)

    def test_filename_type_signature_and_empty_file_validation(self):
        cases = [
            (PDF, "../../escape.pdf"), (PDF, r"C:\fakepath\escape.pdf"),
            (PDF, "bad\nname.pdf"), (PDF, ".hidden.pdf"), (PDF, "name.html"),
            (PDF, "name.pdf.exe"), (b"", "empty.pdf"),
            (b"<script>alert(1)</script>", "fake.pdf"),
            (PDF, "fake.doc"), (b"PK0000", "fake.docx"),
            (PDF, "wrong.pdf", "text/html"),
        ]
        for item in cases:
            with self.subTest(filename=item[1]):
                self.assertEqual(self.submit([item]).status_code, 400)
                self.assertEqual(self.stored_files(), [])
        self.assertEqual(self.tasks(), [])
        task = self.create([(PDF, "<通知>.PDF")])
        self.assertEqual(task["attachments"][0]["extension"], "pdf")

    def test_docx_rejects_zip_traversal_macros_duplicates_and_entity_xml(self):
        invalid = [
            docx_bytes({"../../outside": b"bad"}),
            docx_bytes({"word/vbaProject.bin": b"macro"}),
            docx_bytes(document=b"<!DOCTYPE x [<!ENTITY x 'oops'>]><x/>"),
            docx_bytes(document="<?xml version='1.0' encoding='utf-16'?><!DOCTYPE x [<!ENTITY x 'oops'>]><x/>".encode("utf-16")),
            docx_bytes(document=b"<notword/>"),
            docx_bytes(content_types=CONTENT_TYPES.replace(b"wordprocessingml.document.main", b"wordprocessingml.template.main")),
            docx_bytes({"word/media/bomb": b"0" * 2_000_000}),
        ]
        for index, data in enumerate(invalid):
            with self.subTest(case=index):
                self.assertEqual(self.submit([(data, "broken.docx")]).status_code, 400)
                self.assertEqual(self.stored_files(), [])

    def test_file_and_request_limits_leave_no_partial_data(self):
        too_large = PDF + b" " * (MAX_ATTACHMENT_BYTES + 1 - len(PDF))
        response = self.submit([(too_large, "large.pdf")])
        self.assertEqual(response.status_code, 400)
        self.assertIn("20 MB", response.get_json()["error"])
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(self.app.config["MAX_CONTENT_LENGTH"], MAX_REQUEST_BYTES)
        self.app.config["MAX_CONTENT_LENGTH"] = 1024
        response = self.submit([(PDF + b" " * 2048, "large.pdf")])
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.stored_files(), [])

    def test_json_and_unrelated_endpoints_keep_small_request_limit(self):
        response = self.client.post("/api/tasks", json=self.payload(content="x" * (128 * 1024)), headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 413)
        response = self.client.post("/api/login", data={"payload": "x" * (128 * 1024)}, content_type="multipart/form-data", headers={"X-CSRF-Token": self.token})
        # Login refuses multipart before parsing any oversized form body.
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.tasks(), [])

    def test_removal_ids_must_be_unique_and_belong_to_notice(self):
        first = self.create()
        second = self.create()
        before = self.stored_files()
        for removed in (None, "1", [True], [-1], [first["attachments"][0]["id"]] * 2, [second["attachments"][0]["id"]]):
            with self.subTest(removed=removed):
                response = self.submit([(PDF, "new.pdf")], payload={"removedAttachmentIds": removed}, task_id=first["id"])
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored_files(), before)
        self.assertEqual(self.tasks(), [second, first])
        response = self.submit([(PDF, "new.pdf")], payload={"title": "missing"}, task_id=9999)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stored_files(), before)

    def test_bad_multipart_payload_and_unknown_fields(self):
        for payload in ("no json", "[]", "null"):
            response = self.client.post("/api/tasks", data={"payload": payload}, content_type="multipart/form-data", headers={"X-CSRF-Token": self.token})
            self.assertEqual(response.status_code, 400)
        response = self.submit(payload=self.payload(removedAttachmentIds=[]))
        self.assertEqual(response.status_code, 400)
        response = self.submit(payload=self.payload(unknown=True))
        self.assertEqual(response.status_code, 400)

    def test_word_preview_uses_private_temporary_output_and_never_changes_download(self):
        original = docx_bytes()
        task = self.create([(original, "报告.docx")])
        item = task["attachments"][0]
        paths = []
        def render(source, extension, output_dir):
            self.assertEqual(extension, "docx")
            self.assertEqual(source.read_bytes(), original)
            paths.append(output_dir)
            destination = output_dir / "preview.html"
            destination.write_text("<!doctype html><p>报告内容</p>", encoding="utf-8")
            return destination
        with patch("app.render_word_preview", render):
            response = self.app.test_client().get(item["previewUrl"])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "text/html")
            self.assertIn("报告内容", response.get_data(as_text=True))
            self.assertIn("sandbox", response.headers["Content-Security-Policy"])
        self.assertTrue(paths)
        self.assertFalse(paths[0].exists())
        with patch("app.render_word_preview", side_effect=DocumentPreviewError("无法预览，请下载原文件查看。")):
            response = self.client.get(item["previewUrl"])
            self.assertEqual(response.status_code, 503)
            self.assertIn("下载", response.get_json()["error"])
        download = self.client.get(item["downloadUrl"])
        self.assertEqual(download.data, original)
        download.close()

    def test_real_docx_preview_fallback_works_through_public_endpoint(self):
        task = self.create([(docx_bytes(), "报告.docx")])
        with patch("document_preview.shutil.which", return_value=None):
            response = self.app.test_client().get(task["attachments"][0]["previewUrl"])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.mimetype, "text/html")
        self.assertIn("Hello attachment", response.get_data(as_text=True))
        self.assertIn("sandbox", response.headers["Content-Security-Policy"])

    def test_preview_never_serves_renderer_path_outside_private_directory(self):
        task = self.create([(docx_bytes(), "报告.docx")])
        outside = self.root / "private.html"
        outside.write_text("secret")
        with patch("app.render_word_preview", return_value=outside):
            response = self.client.get(task["attachments"][0]["previewUrl"])
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("secret", response.get_data(as_text=True))

    def test_corrupt_storage_metadata_or_symlinks_cannot_escape_upload_directory(self):
        task = self.create()
        item = task["attachments"][0]
        file = self.root / "uploads" / self.stored_files()[0]
        outside = self.root / "secret.pdf"
        outside.write_bytes(b"secret")
        file.unlink()
        file.symlink_to(outside)
        for url in (item["previewUrl"], item["downloadUrl"]):
            self.assertEqual(self.client.get(url).status_code, 400)
        with sqlite3.connect(self.config["DATABASE"]) as database:
            database.execute("UPDATE attachments SET stored_name = ? WHERE id = ?", ("../secret.pdf", item["id"]))
        self.assertEqual(self.client.get(item["downloadUrl"]).status_code, 400)
        self.assertEqual(outside.read_bytes(), b"secret")


if __name__ == "__main__":
    unittest.main()
