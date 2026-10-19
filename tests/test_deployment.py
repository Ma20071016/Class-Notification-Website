"""Production configuration tests; all state lives in temporary directories."""

import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask, jsonify, request

from app import create_app, initialize_admin


class DeploymentConfigurationTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.instance = Path(temporary.name) / "persistent"
        environment = patch.dict(os.environ, {
            "CLASS_NOTICE_INSTANCE_PATH": str(self.instance),
            "CLASS_NOTICE_SECRET_KEY": "",
            "CLASS_NOTICE_SECURE_COOKIE": "0",
            "CLASS_NOTICE_PROXY_HOPS": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def app(self, **overrides):
        def isolated_flask(*args, **kwargs):
            app = Flask(*args, **kwargs)
            # Abort before create_app can read a secret or open a database if
            # persistent-directory support ever regresses to the real instance.
            self.assertEqual(Path(app.instance_path), self.instance)
            return app

        with patch("app.Flask", side_effect=isolated_flask):
            return create_app({"TESTING": True, **overrides})

    def csrf(self, client):
        response = client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["csrfToken"]

    def add_request_probe(self, app):
        @app.get("/_deployment_request_probe")
        def probe():
            return jsonify(address=request.remote_addr, secure=request.is_secure,
                           host=request.host, port=request.environ["SERVER_PORT"],
                           script_root=request.script_root)

    def test_persistent_instance_keeps_accounts_tasks_attachments_favorites_and_signed_session(self):
        first_app = self.app()
        database = self.instance / "class_notice.sqlite3"
        self.assertEqual(Path(first_app.instance_path), self.instance)
        self.assertEqual(Path(first_app.config["DATABASE"]), database)
        secret_file = self.instance / "secret_key"
        self.assertTrue(secret_file.is_file())
        first_secret = secret_file.read_text(encoding="utf-8")
        initialize_admin(first_app, "deployment-admin", "temporary-admin-password")
        admin = first_app.test_client()
        response = admin.post("/api/login", json={
            "username": "deployment-admin", "password": "temporary-admin-password", "role": "admin",
        }, headers={"X-CSRF-Token": self.csrf(admin)})
        self.assertEqual(response.status_code, 200)
        original = b"%PDF-1.4\n% persistent attachment fixture\n%%EOF\n"
        response = admin.post("/api/tasks", data={
            "payload": json.dumps({
                "title": "部署后保留的通知", "content": "更新程序不应删除已有通知。",
                "category": "course", "deadline": None, "pinned": True,
            }),
            "attachments": (io.BytesIO(original), "持久化附件.pdf", "application/pdf"),
        }, headers={"X-CSRF-Token": response.get_json()["csrfToken"]})
        self.assertEqual(response.status_code, 201, response.get_json())
        task = response.get_json()["task"]
        self.assertEqual(len(task["attachments"]), 1)
        self.assertEqual(Path(first_app.config["UPLOAD_FOLDER"]), self.instance / "uploads")
        original_paths = list((self.instance / "uploads").iterdir())
        self.assertEqual(len(original_paths), 1)
        self.assertEqual(original_paths[0].read_bytes(), original)
        reader = first_app.test_client()
        response = reader.post("/api/register", json={
            "username": "deployment-reader", "password": "temporary-reader-password",
            "passwordConfirm": "temporary-reader-password",
        }, headers={"X-CSRF-Token": self.csrf(reader)})
        self.assertEqual(response.status_code, 201)
        reader_user = response.get_json()["user"]
        response = reader.put(f"/api/favorites/{task['id']}", json={},
                              headers={"X-CSRF-Token": response.get_json()["csrfToken"]})
        self.assertEqual(response.status_code, 200)
        cookie = reader.get_cookie(first_app.config["SESSION_COOKIE_NAME"])
        self.assertIsNotNone(cookie)
        with sqlite3.connect(database) as connection:
            before = {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                      for table in ("users", "tasks", "favorites", "attachments")}
        for _ in range(2):
            reopened_app = self.app()
            self.assertEqual(reopened_app.config["SECRET_KEY"], first_app.config["SECRET_KEY"])
            self.assertEqual(secret_file.read_text(encoding="utf-8"), first_secret)
            reopened = reopened_app.test_client()
            reopened.set_cookie(reopened_app.config["SESSION_COOKIE_NAME"], cookie.value)
            self.assertEqual(reopened.get("/api/session").get_json()["user"], reader_user)
            self.assertEqual(reopened.get("/api/tasks").get_json()["tasks"], [task])
            self.assertEqual(reopened.get("/api/favorites").get_json()["taskIds"], [task["id"]])
            with reopened.get(task["attachments"][0]["downloadUrl"]) as download:
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.data, original)
            with reopened.get(task["attachments"][0]["previewUrl"]) as preview:
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(preview.mimetype, "application/pdf")
                self.assertEqual(preview.data, original)
            with sqlite3.connect(database) as connection:
                after = {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                         for table in before}
                self.assertEqual(after, before)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_secure_cookie_environment_is_applied_to_browser_cookie(self):
        with patch.dict(os.environ, {"CLASS_NOTICE_SECURE_COOKIE": "1"}):
            app = self.app()
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        response = app.test_client().get("/api/session", base_url="https://notices.example")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("; Secure", cookie)
        self.assertIn("; HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_default_proxy_configuration_ignores_forged_forwarded_headers(self):
        os.environ.pop("CLASS_NOTICE_PROXY_HOPS")
        app = self.app()
        self.add_request_probe(app)
        response = app.test_client().get("/_deployment_request_probe",
            base_url="http://notices.example", environ_overrides={"REMOTE_ADDR": "198.51.100.10"},
            headers={
                "X-Forwarded-For": "203.0.113.20", "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "untrusted.example", "X-Forwarded-Port": "8443",
                "X-Forwarded-Prefix": "/untrusted",
            })
        self.assertEqual(response.get_json(), {
            "address": "198.51.100.10", "secure": False,
            "host": "notices.example", "port": "80", "script_root": "",
        })

    def test_explicit_proxy_hops_trust_only_address_and_scheme(self):
        with patch.dict(os.environ, {"CLASS_NOTICE_PROXY_HOPS": "2"}):
            app = self.app()
        self.add_request_probe(app)
        response = app.test_client().get("/_deployment_request_probe",
            base_url="http://notices.example", environ_overrides={"REMOTE_ADDR": "192.0.2.30"},
            headers={
                "X-Forwarded-For": "untrusted-client-value, 203.0.113.20, 192.0.2.10",
                "X-Forwarded-Proto": "untrusted-client-value, https, http",
                "X-Forwarded-Host": "untrusted.example, other.example",
                "X-Forwarded-Port": "8443, 9443", "X-Forwarded-Prefix": "/untrusted, /other",
            })
        self.assertEqual(response.get_json(), {
            "address": "203.0.113.20", "secure": True,
            "host": "notices.example", "port": "80", "script_root": "",
        })

    def test_invalid_deployment_configuration_is_rejected(self):
        for hops in ("-1", "1.5", "not-a-number", "", "+1", " 1 "):
            with self.subTest(proxy_hops=hops):
                with patch.dict(os.environ, {"CLASS_NOTICE_PROXY_HOPS": hops}):
                    with self.assertRaises(RuntimeError):
                        self.app()
        with patch.dict(os.environ, {"CLASS_NOTICE_INSTANCE_PATH": "relative-instance"}):
            with self.assertRaises(RuntimeError):
                self.app()
        self.assertFalse(self.instance.exists(), "Invalid configuration should fail before creating persistent state.")

    def test_explicit_config_overrides_database_secret_and_cookie_environment(self):
        alternate_database = self.instance.parent / "isolated-database.sqlite3"
        with patch.dict(os.environ, {
            "CLASS_NOTICE_SECRET_KEY": "environment-secret-that-will-be-overridden",
            "CLASS_NOTICE_SECURE_COOKIE": "1",
        }):
            app = self.app(DATABASE=str(alternate_database),
                           SECRET_KEY="isolated-test-secret", SESSION_COOKIE_SECURE=False)
        self.assertEqual(app.config["DATABASE"], str(alternate_database))
        self.assertTrue(alternate_database.is_file())
        self.assertEqual(app.config["SECRET_KEY"], "isolated-test-secret")
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])
        self.assertFalse((self.instance / "class_notice.sqlite3").exists())
        self.assertFalse((self.instance / "secret_key").exists())


if __name__ == "__main__":
    unittest.main()
