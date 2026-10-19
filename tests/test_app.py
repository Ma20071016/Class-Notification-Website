"""Behavioral tests for the class notice API.

Run from the project directory with:
    python -m unittest discover -s tests -v
"""

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app


class NoticeAPITestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = str(Path(self.temp_dir.name) / "notices.sqlite3")
        self.config = {
            "TESTING": True,
            "SECRET_KEY": "only-for-isolated-tests",
            "DATABASE": self.database,
        }
        self.app = create_app(self.config)
        with sqlite3.connect(self.database) as connection:
            connection.executemany(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                [
                    ("classadmin", generate_password_hash("test-admin-password"), "admin"),
                    ("student", generate_password_hash("test-user-password"), "user"),
                ],
            )
        self.client = self.app.test_client()

    def csrf(self, client=None):
        client = client or self.client
        response = client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        token = response.get_json()["csrfToken"]
        self.assertIsInstance(token, str)
        self.assertTrue(token)
        return token

    def login(self, username="classadmin", password="test-admin-password", client=None, role=None):
        client = client or self.client
        payload = {"username": username, "password": password}
        if role is not None:
            payload["role"] = role
        response = client.post(
            "/api/login",
            json=payload,
            headers={"X-CSRF-Token": self.csrf(client)},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["csrfToken"]

    def task_payload(self, **overrides):
        payload = {
            "title": "提交计算机组成原理作业",
            "content": "请在截止时间前提交实验报告。",
            "category": "homework",
            "deadline": "2026-10-25T23:59:00+08:00",
            "pinned": False,
        }
        payload.update(overrides)
        return payload

    def create_task(self, token, **overrides):
        response = self.client.post(
            "/api/tasks",
            json=self.task_payload(**overrides),
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["task"]

    def tasks(self, client=None):
        response = (client or self.client).get("/api/tasks")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["tasks"]

    def test_fresh_database_is_readable_without_login(self):
        session = self.client.get("/api/session").get_json()
        self.assertIsNone(session["user"])
        self.assertTrue(session["csrfToken"])
        self.assertEqual(self.tasks(), [])

    def test_anonymous_users_cannot_mutate_tasks(self):
        token = self.csrf()
        for method, url in (
            ("POST", "/api/tasks"),
            ("PATCH", "/api/tasks/1"),
            ("DELETE", "/api/tasks/1"),
        ):
            with self.subTest(method=method):
                response = self.client.open(
                    url,
                    method=method,
                    json=self.task_payload(),
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.tasks(), [])

    def test_login_requires_csrf_and_valid_credentials(self):
        response = self.client.post(
            "/api/login",
            json={"username": "classadmin", "password": "test-admin-password"},
        )
        self.assertEqual(response.status_code, 403)
        token = self.csrf()
        for username, password in (
            ("classadmin", "wrong-password"),
            ("unknown-user", "test-admin-password"),
        ):
            with self.subTest(username=username):
                response = self.client.post(
                    "/api/login",
                    json={"username": username, "password": password},
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 401)
                self.assertIsNone(self.client.get("/api/session").get_json()["user"])

    def test_login_and_logout_rotate_csrf_and_revoke_access(self):
        previous_token = self.csrf()
        token = self.login()
        self.assertNotEqual(previous_token, token)
        session = self.client.get("/api/session").get_json()
        self.assertEqual(session["user"]["username"], "classadmin")
        self.assertEqual(session["user"]["role"], "admin")
        self.assertNotIn("password_hash", session["user"])

        rejected = self.client.post(
            "/api/tasks",
            json=self.task_payload(),
            headers={"X-CSRF-Token": previous_token},
        )
        self.assertEqual(rejected.status_code, 403)
        logout = self.client.post(
            "/api/logout", json={}, headers={"X-CSRF-Token": token}
        )
        self.assertEqual(logout.status_code, 200)
        self.assertIsNone(logout.get_json()["user"])
        self.assertNotEqual(logout.get_json()["csrfToken"], token)
        self.assertIsNone(self.client.get("/api/session").get_json()["user"])
        rejected = self.client.post(
            "/api/tasks",
            json=self.task_payload(),
            headers={"X-CSRF-Token": logout.get_json()["csrfToken"]},
        )
        self.assertEqual(rejected.status_code, 401)

    def test_authenticated_mutations_require_matching_csrf(self):
        token = self.login()
        task = self.create_task(token)
        for method, url in (
            ("POST", "/api/tasks"),
            ("PATCH", f"/api/tasks/{task['id']}"),
            ("DELETE", f"/api/tasks/{task['id']}"),
            ("POST", "/api/logout"),
        ):
            for headers in ({}, {"X-CSRF-Token": "incorrect-token"}):
                with self.subTest(method=method, url=url, headers=headers):
                    response = self.client.open(
                        url, method=method, json=self.task_payload(), headers=headers
                    )
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.tasks()), 1)
        self.assertIsNotNone(self.client.get("/api/session").get_json()["user"])

    def test_non_admin_and_demoted_admin_can_read_but_cannot_modify(self):
        admin_token = self.login()
        task = self.create_task(admin_token)
        reader = self.app.test_client()
        response = reader.post(
            "/api/login",
            json={"username": "student", "password": "test-user-password", "role": "user"},
            headers={"X-CSRF-Token": self.csrf(reader)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["user"]["role"], "user")
        self.assertEqual(self.tasks(reader)[0]["id"], task["id"])
        reader_token = response.get_json()["csrfToken"]
        for method, url in (
            ("POST", "/api/tasks"),
            ("PATCH", f"/api/tasks/{task['id']}"),
            ("DELETE", f"/api/tasks/{task['id']}"),
        ):
            with self.subTest(ordinary_user_method=method):
                response = reader.open(
                    url, method=method, json=self.task_payload(),
                    headers={"X-CSRF-Token": reader_token},
                )
                self.assertEqual(response.status_code, 403)

        # An existing browser session must lose write access when its account
        # is demoted. This also checks that roles are read from the database.
        reader_token = self.login(client=reader)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE users SET role = 'user' WHERE username = ?", ("classadmin",)
            )
        self.assertEqual(reader.get("/api/session").get_json()["user"]["role"], "user")
        self.assertEqual(self.tasks(reader)[0]["id"], task["id"])
        for method, url in (
            ("POST", "/api/tasks"),
            ("PATCH", f"/api/tasks/{task['id']}"),
            ("DELETE", f"/api/tasks/{task['id']}"),
        ):
            with self.subTest(method=method):
                response = reader.open(
                    url,
                    method=method,
                    json=self.task_payload(),
                    headers={"X-CSRF-Token": reader_token},
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.tasks()[0]["id"], task["id"])

    def test_login_entry_must_match_account_role(self):
        token = self.csrf()
        for username, password, role in (
            ("classadmin", "test-admin-password", "user"),
            ("student", "test-user-password", "admin"),
        ):
            with self.subTest(username=username, role=role):
                response = self.client.post(
                    "/api/login",
                    json={"username": username, "password": password, "role": role},
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.get_json()["error"], "账号或密码不正确。")
                self.assertIsNone(self.client.get("/api/session").get_json()["user"])
        for role in (None, [], "owner", True):
            with self.subTest(invalid_role=role):
                response = self.client.post(
                    "/api/login",
                    json={"username": "classadmin", "password": "test-admin-password", "role": role},
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 400)
        self.login(role="admin")
        self.assertEqual(self.client.get("/api/session").get_json()["user"]["role"], "admin")

    def test_registration_creates_reader_hashes_password_and_rotates_session(self):
        previous_token = self.csrf()
        response = self.client.post(
            "/api/register",
            json={"username": "  new-student  ", "password": "student-password", "passwordConfirm": "student-password", "role": "admin"},
            headers={"X-CSRF-Token": previous_token},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        result = response.get_json()
        self.assertEqual(result["user"]["username"], "new-student")
        self.assertEqual(result["user"]["role"], "user")
        self.assertNotIn("password_hash", result["user"])
        self.assertNotEqual(result["csrfToken"], previous_token)
        self.assertEqual(self.client.get("/api/session").get_json()["user"], result["user"])
        with sqlite3.connect(self.database) as connection:
            stored_hash, role = connection.execute(
                "SELECT password_hash, role FROM users WHERE username = ?", ("new-student",)
            ).fetchone()
        self.assertNotEqual(stored_hash, "student-password")
        self.assertTrue(check_password_hash(stored_hash, "student-password"))
        self.assertEqual(role, "user")
        rejected = self.client.post("/api/logout", json={}, headers={"X-CSRF-Token": previous_token})
        self.assertEqual(rejected.status_code, 403)
        for method, url in (("POST", "/api/tasks"), ("PATCH", "/api/tasks/1"), ("DELETE", "/api/tasks/1")):
            with self.subTest(method=method):
                rejected = self.client.open(
                    url, method=method, json=self.task_payload(), headers={"X-CSRF-Token": result["csrfToken"]}
                )
                self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.tasks(), [])
        logout = self.client.post("/api/logout", json={}, headers={"X-CSRF-Token": result["csrfToken"]})
        self.assertEqual(logout.status_code, 200)
        self.assertIsNone(self.client.get("/api/session").get_json()["user"])
        restarted_client = create_app(self.config).test_client()
        self.login(username="new-student", password="student-password", role="user", client=restarted_client)
        self.assertEqual(restarted_client.get("/api/session").get_json()["user"], result["user"])

    def test_registration_requires_csrf_from_same_session(self):
        payload = {"username": "new-student", "password": "student-password", "passwordConfirm": "student-password"}
        other_token = self.csrf(self.app.test_client())
        for headers in ({}, {"X-CSRF-Token": "invalid"}, {"X-CSRF-Token": other_token}):
            with self.subTest(headers=headers):
                response = self.client.post("/api/register", json=payload, headers=headers)
                self.assertEqual(response.status_code, 403)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_registration_validates_fields_before_creating_account(self):
        token = self.csrf()
        payload = {"username": "new-student", "password": "student-password", "passwordConfirm": "student-password"}
        for fields in (
            {"username": None}, {"username": " "}, {"username": "two words"},
            {"username": "a" * 65}, {"username": ["new-student"]},
            {"password": None}, {"password": "short"}, {"password": "a" * 1025},
            {"passwordConfirm": None}, {"passwordConfirm": "different-password"},
        ):
            with self.subTest(fields=str(fields)[:80]):
                response = self.client.post("/api/register", json={**payload, **fields}, headers={"X-CSRF-Token": token})
                self.assertEqual(response.status_code, 400, response.get_json())
        for body, content_type in (("{broken", "application/json"), ("[]", "application/json"), ("null", "application/json"), ("plain", "text/plain")):
            with self.subTest(body=body):
                response = self.client.post("/api/register", data=body, content_type=content_type, headers={"X-CSRF-Token": token})
                self.assertEqual(response.status_code, 400)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
        self.assertIsNone(self.client.get("/api/session").get_json()["user"])

    def test_duplicate_registration_preserves_existing_accounts(self):
        token = self.csrf()
        with sqlite3.connect(self.database) as connection:
            original_accounts = connection.execute("SELECT * FROM users ORDER BY id").fetchall()
        for username in ("classadmin", "student"):
            with self.subTest(username=username):
                response = self.client.post(
                    "/api/register",
                    json={"username": username, "password": "new-password", "passwordConfirm": "new-password"},
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 409)
                self.assertIsNone(self.client.get("/api/session").get_json()["user"])
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT * FROM users ORDER BY id").fetchall(), original_accounts)
        self.login()

    def test_registration_rate_limit_expires_and_ignores_forwarded_ips(self):
        token = self.csrf()
        payload = {"username": "student", "password": "student-password", "passwordConfirm": "student-password"}
        with patch("app.time.monotonic", return_value=1000):
            for attempt in range(10):
                response = self.client.post(
                    "/api/register", json=payload,
                    headers={"X-CSRF-Token": token, "X-Forwarded-For": f"192.0.2.{attempt + 1}"},
                )
                self.assertEqual(response.status_code, 409)
            blocked = self.client.post(
                "/api/register", json={**payload, "username": "blocked-student"},
                headers={"X-CSRF-Token": token, "X-Forwarded-For": "198.51.100.1"},
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.headers["Retry-After"], "900")
            # Registration limits are independent of login and public reading.
            self.assertEqual(self.tasks(), [])
            token = self.login()
        with patch("app.time.monotonic", return_value=1901):
            response = self.client.post(
                "/api/register", json={**payload, "username": "new-student"},
                headers={"X-CSRF-Token": token},
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.get_json()["user"]["role"], "user")

    def test_new_database_defaults_unprivileged_accounts_to_user(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                ("default-role", "isolated-test-placeholder"),
            )
            self.assertEqual(connection.execute("SELECT role FROM users WHERE username = 'default-role'").fetchone()[0], "user")

    def test_favorites_require_login_and_matching_csrf(self):
        guest_token = self.csrf()
        for method, url in (("GET", "/api/favorites"), ("PUT", "/api/favorites/1"), ("DELETE", "/api/favorites/1")):
            with self.subTest(guest_method=method):
                response = self.client.open(url, method=method, json={}, headers={"X-CSRF-Token": guest_token})
                self.assertEqual(response.status_code, 401)
        admin_token = self.login()
        task = self.create_task(admin_token)
        reader = self.app.test_client()
        reader_token = self.login(username="student", password="test-user-password", role="user", client=reader)
        for method in ("PUT", "DELETE"):
            for headers in ({}, {"X-CSRF-Token": "invalid"}, {"X-CSRF-Token": admin_token}):
                with self.subTest(method=method, headers=headers):
                    response = reader.open(f"/api/favorites/{task['id']}", method=method, json={}, headers=headers)
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(reader.get("/api/favorites").get_json()["taskIds"], [])
        response = reader.put(f"/api/favorites/{task['id']}", json={}, headers={"X-CSRF-Token": reader_token})
        self.assertEqual(response.status_code, 200)

    def test_favorites_are_isolated_between_ordinary_users_and_admin(self):
        admin_token = self.login()
        first = self.create_task(admin_token)
        second = self.create_task(admin_token, title="另一个任务")
        reader = self.app.test_client()
        reader_token = self.login(username="student", password="test-user-password", role="user", client=reader)
        second_reader = self.app.test_client()
        registration = second_reader.post(
            "/api/register", json={"username": "second-reader", "password": "reader-password", "passwordConfirm": "reader-password"},
            headers={"X-CSRF-Token": self.csrf(second_reader)},
        )
        self.assertEqual(registration.status_code, 201)
        second_token = registration.get_json()["csrfToken"]
        reader_id = reader.get("/api/session").get_json()["user"]["id"]
        second_id = registration.get_json()["user"]["id"]
        admin_id = self.client.get("/api/session").get_json()["user"]["id"]
        for client, token, task_id, other_id, own_id in (
            (reader, reader_token, first["id"], second_id, reader_id),
            (second_reader, second_token, second["id"], reader_id, second_id),
            (self.client, admin_token, first["id"], reader_id, admin_id),
        ):
            response = client.put(
                f"/api/favorites/{task_id}", json={"userId": other_id}, headers={"X-CSRF-Token": token}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"userId": own_id, "taskId": task_id, "favorited": True})
        for client, own_id, other_id, task_id in (
            (reader, reader_id, second_id, first["id"]),
            (second_reader, second_id, reader_id, second["id"]),
            (self.client, admin_id, reader_id, first["id"]),
        ):
            response = client.get(f"/api/favorites?userId={other_id}")
            self.assertEqual(response.get_json(), {"userId": own_id, "taskIds": [task_id]})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        removed = second_reader.delete(
            f"/api/favorites/{first['id']}", json={"userId": reader_id}, headers={"X-CSRF-Token": second_token}
        )
        self.assertEqual(removed.get_json(), {"userId": second_id, "taskId": first["id"], "favorited": False})
        self.assertEqual(reader.get("/api/favorites").get_json()["taskIds"], [first["id"]])
        self.assertEqual(self.client.get("/api/favorites").get_json()["taskIds"], [first["id"]])
        for method, url in (("POST", "/api/tasks"), ("PATCH", f"/api/tasks/{first['id']}"), ("DELETE", f"/api/tasks/{first['id']}")):
            response = reader.open(url, method=method, json=self.task_payload(), headers={"X-CSRF-Token": reader_token})
            self.assertEqual(response.status_code, 403)

    def test_favorites_are_idempotent_persist_and_allow_missing_delete(self):
        admin_token = self.login()
        task = self.create_task(admin_token)
        reader = self.app.test_client()
        token = self.login(username="student", password="test-user-password", role="user", client=reader)
        reader_id = reader.get("/api/session").get_json()["user"]["id"]
        url = f"/api/favorites/{task['id']}"
        for _ in range(2):
            response = reader.put(url, json={}, headers={"X-CSRF-Token": token})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"userId": reader_id, "taskId": task["id"], "favorited": True})
        with sqlite3.connect(self.database) as connection:
            records = connection.execute("SELECT user_id, task_id, created_at FROM favorites").fetchall()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][:2], (reader_id, task["id"]))
        self.assertTrue(records[0][2].endswith("Z"))
        reopened = create_app(self.config).test_client()
        reopened_token = self.login(username="student", password="test-user-password", role="user", client=reopened)
        self.assertEqual(reopened.get("/api/favorites").get_json(), {"userId": reader_id, "taskIds": [task["id"]]})
        for _ in range(2):
            response = reopened.delete(url, headers={"X-CSRF-Token": reopened_token})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"userId": reader_id, "taskId": task["id"], "favorited": False})
        self.assertEqual(reopened.get("/api/favorites").get_json()["taskIds"], [])
        missing_add = reopened.put("/api/favorites/999999", json={}, headers={"X-CSRF-Token": reopened_token})
        self.assertEqual(missing_add.status_code, 404)
        missing_delete = reopened.delete("/api/favorites/999999", json={}, headers={"X-CSRF-Token": reopened_token})
        self.assertEqual(missing_delete.status_code, 200)
        self.assertFalse(missing_delete.get_json()["favorited"])
        self.assertEqual(self.tasks(), [task])

    def test_deleting_task_cascades_favorites_for_every_account(self):
        token = self.login()
        task = self.create_task(token)
        reader = self.app.test_client()
        reader_token = self.login(username="student", password="test-user-password", role="user", client=reader)
        for client, csrf in ((self.client, token), (reader, reader_token)):
            response = client.put(f"/api/favorites/{task['id']}", json={}, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 200)
        deleted = self.client.delete(f"/api/tasks/{task['id']}", headers={"X-CSRF-Token": token})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(reader.get("/api/favorites").get_json()["taskIds"], [])
        self.assertEqual(self.client.get("/api/favorites").get_json()["taskIds"], [])
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM favorites").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_deleted_user_loses_favorites_and_authenticated_access(self):
        token = self.login()
        task = self.create_task(token)
        reader = self.app.test_client()
        reader_token = self.login(username="student", password="test-user-password", role="user", client=reader)
        response = reader.put(f"/api/favorites/{task['id']}", json={}, headers={"X-CSRF-Token": reader_token})
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM users WHERE username = 'student'")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM favorites").fetchone()[0], 0)
        for method, url in (("GET", "/api/favorites"), ("PUT", f"/api/favorites/{task['id']}"), ("DELETE", f"/api/favorites/{task['id']}")):
            response = reader.open(url, method=method, json={}, headers={"X-CSRF-Token": reader_token})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.tasks(reader), [task])

    def test_expired_session_cannot_read_or_change_favorites(self):
        token = self.login()
        task = self.create_task(token)
        saved = self.client.put(f"/api/favorites/{task['id']}", json={}, headers={"X-CSRF-Token": token})
        self.assertEqual(saved.status_code, 200)
        self.app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=-1)
        for method, url in (("GET", "/api/favorites"), ("PUT", f"/api/favorites/{task['id']}"), ("DELETE", f"/api/favorites/{task['id']}")):
            response = self.client.open(url, method=method, json={}, headers={"X-CSRF-Token": token})
            self.assertEqual(response.status_code, 401)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM favorites").fetchone()[0], 1)

    def test_favorite_mutations_reject_malformed_bodies(self):
        token = self.login()
        task = self.create_task(token)
        for method in ("PUT", "DELETE"):
            for body, content_type in (("{broken", "application/json"), ("[]", "application/json"), ("null", "application/json"), ("plain", "text/plain")):
                with self.subTest(method=method, body=body):
                    response = self.client.open(
                        f"/api/favorites/{task['id']}", method=method, data=body, content_type=content_type,
                        headers={"X-CSRF-Token": token},
                    )
                    self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/favorites").get_json()["taskIds"], [])

    def test_admin_crud_persists_across_application_restart(self):
        token = self.login()
        created = self.create_task(token)
        self.assertEqual(created["title"], self.task_payload()["title"])
        self.assertEqual(created["category"], "homework")
        self.assertEqual(created["deadline"], "2026-10-25T15:59:00Z")
        self.assertEqual(created["author"], "classadmin")
        self.assertFalse(created["pinned"])
        self.assertTrue(created["createdAt"])
        self.assertTrue(created["updatedAt"])
        restarted = create_app(self.config)
        guest = restarted.test_client()
        self.assertEqual(self.tasks(guest), [created])
        self.assertIsNone(guest.get("/api/session").get_json()["user"])

        response = self.client.patch(
            f"/api/tasks/{created['id']}",
            json={"title": "截止时间已延后", "deadline": None, "pinned": True},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        updated = self.tasks(guest)[0]
        self.assertEqual(updated["title"], "截止时间已延后")
        self.assertIsNone(updated["deadline"])
        self.assertTrue(updated["pinned"])
        self.assertEqual(updated["content"], created["content"])
        self.assertEqual(updated["createdAt"], created["createdAt"])

        response = self.client.delete(
            f"/api/tasks/{created['id']}", headers={"X-CSRF-Token": token}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.tasks(create_app(self.config).test_client()), [])

    def test_task_without_deadline_uses_defaults(self):
        token = self.login()
        response = self.client.post(
            "/api/tasks",
            json={"title": "  班级通知  ", "content": "  请阅读本周班会安排。  "},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        task = self.tasks()[0]
        self.assertEqual(task["title"], "班级通知")
        self.assertEqual(task["content"], "请阅读本周班会安排。")
        self.assertEqual(task["category"], "course")
        self.assertIsNone(task["deadline"])
        self.assertIs(task["pinned"], False)

    def test_all_categories_round_trip_on_create_and_edit(self):
        token = self.login()
        categories = ("homework", "exam", "course", "activity")
        for category in categories:
            with self.subTest(created_category=category):
                created = self.create_task(token, category=category)
                self.assertEqual(created["category"], category)
                for edited_category in categories:
                    with self.subTest(edited_category=edited_category):
                        response = self.client.patch(
                            f"/api/tasks/{created['id']}",
                            json={"category": edited_category},
                            headers={"X-CSRF-Token": token},
                        )
                        self.assertEqual(response.status_code, 200, response.get_json())
                        stored = next(task for task in self.tasks() if task["id"] == created["id"])
                        self.assertEqual(stored["category"], edited_category)
                        for field in ("id", "title", "content", "deadline", "pinned", "author", "createdAt"):
                            self.assertEqual(stored[field], created[field])
        self.assertEqual(len(self.tasks()), len(categories))

    def test_deadline_offsets_are_normalized_to_utc(self):
        token = self.login()
        for deadline, expected in (
            ("2026-10-25T23:59:00+08:00", "2026-10-25T15:59:00Z"),
            ("2026-10-25T12:00:00-04:00", "2026-10-25T16:00:00Z"),
            ("2026-10-25T15:00:00Z", "2026-10-25T15:00:00Z"),
        ):
            with self.subTest(deadline=deadline):
                task = self.create_task(token, deadline=deadline)
                self.assertEqual(task["deadline"], expected)

    def test_invalid_task_fields_do_not_create_records(self):
        token = self.login()
        invalid_fields = (
            {"title": " "},
            {"title": None},
            {"title": 123},
            {"title": "a" * 101},
            {"content": "\n\t"},
            {"content": None},
            {"content": ["text"]},
            {"content": "a" * 10001},
            {"category": "unknown-category"},
            {"category": "notice"},
            {"category": None},
            {"category": ["course"]},
            {"deadline": "2026-10-25T23:59:00"},
            {"deadline": "2026-10-25"},
            {"deadline": "2026-02-30T10:00:00+08:00"},
            {"deadline": "not-a-date"},
            {"deadline": 1792943940},
            {"deadline": True},
            {"pinned": "true"},
            {"pinned": 1},
            {"pinned": None},
            {"unexpected": "value"},
        )
        for fields in invalid_fields:
            with self.subTest(fields=str(fields)[:100]):
                response = self.client.post(
                    "/api/tasks",
                    json=self.task_payload(**fields),
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(self.tasks(), [])

    def test_invalid_edits_leave_existing_task_intact(self):
        token = self.login()
        task = self.create_task(token)
        for payload in ({}, {"title": ""}, {"pinned": 1}, {"id": 5}, {"category": "unknown-category"}, {"category": "notice"}):
            with self.subTest(payload=payload):
                response = self.client.patch(
                    f"/api/tasks/{task['id']}",
                    json=payload,
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.tasks(), [task])

    def test_missing_task_edit_and_delete_return_not_found(self):
        token = self.login()
        for method in ("PATCH", "DELETE"):
            with self.subTest(method=method):
                response = self.client.open(
                    "/api/tasks/999999",
                    method=method,
                    json={"title": "不存在的任务"},
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.tasks(), [])

    def test_html_and_sql_characters_round_trip_as_task_data(self):
        token = self.login()
        title = "<img src=x onerror=alert(1)>"
        content = "<script>alert('notice')</script>\n'; DROP TABLE tasks; --"
        task = self.create_task(token, title=title, content=content)
        guest = self.app.test_client()
        response = guest.get("/api/tasks")
        self.assertEqual(response.mimetype, "application/json")
        stored = response.get_json()["tasks"][0]
        self.assertEqual(stored["title"], title)
        self.assertEqual(stored["content"], content)
        self.assertEqual(stored["id"], task["id"])
        self.create_task(token, title="后续正常任务")
        self.assertEqual(len(self.tasks(guest)), 2)

    def test_invalid_json_body_is_rejected_without_server_error(self):
        token = self.login()
        for body, content_type in (
            ("{not valid json", "application/json"),
            ("[]", "application/json"),
            ("null", "application/json"),
            ("plain text", "text/plain"),
        ):
            with self.subTest(body=body):
                response = self.client.post(
                    "/api/tasks",
                    data=body,
                    content_type=content_type,
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.tasks(), [])

    def test_csrf_tokens_are_bound_to_each_browser_session(self):
        token = self.login()
        other_browser = self.app.test_client()
        other_token = self.login(client=other_browser)
        self.assertNotEqual(token, other_token)
        for wrong_token in (other_token, "非本站页面的令牌"):
            with self.subTest(token_kind="cross-session" if wrong_token == other_token else "unicode"):
                response = self.client.post(
                    "/api/tasks",
                    json=self.task_payload(),
                    headers={"X-CSRF-Token": wrong_token},
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.tasks(), [])
        self.create_task(token)

    def test_login_rate_limit_expires_and_ignores_untrusted_forwarded_ips(self):
        token = self.csrf()
        with patch("app.time.monotonic", return_value=1000):
            for attempt in range(10):
                response = self.client.post(
                    "/api/login",
                    json={"username": "classadmin", "password": "incorrect-password"},
                    headers={"X-CSRF-Token": token, "X-Forwarded-For": f"192.0.2.{attempt + 1}"},
                )
                self.assertEqual(response.status_code, 401)
            blocked = self.client.post(
                "/api/login",
                json={"username": "classadmin", "password": "test-admin-password"},
                headers={"X-CSRF-Token": token, "X-Forwarded-For": "198.51.100.1"},
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.headers["Retry-After"], "900")
            self.assertIsNone(self.client.get("/api/session").get_json()["user"])
        with patch("app.time.monotonic", return_value=1901):
            self.login()

    def test_invalid_deadline_boundaries_do_not_create_records(self):
        token = self.login()
        for deadline in (
            "2026-10-25T12:00:00+24:00",
            "2026-10-25T12:00:00+00:60",
            "2026-10-25T12:00:00-24:00",
            "2026-10-25T24:00:00Z",
            "2026-10-25T12:00:60Z",
            "1999-12-31T23:59:59Z",
            "2101-01-01T00:00:00Z",
            "0001-01-01T00:00:00+23:59",
            "9999-12-31T23:59:59-23:59",
        ):
            with self.subTest(deadline=deadline):
                response = self.client.post(
                    "/api/tasks",
                    json=self.task_payload(deadline=deadline),
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(self.tasks(), [])

    def test_valid_deadline_boundaries_and_minute_precision_are_supported(self):
        token = self.login()
        for deadline, expected in (
            ("2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z"),
            ("2100-12-31T23:59:59Z", "2100-12-31T23:59:59Z"),
            ("2028-02-29T08:00+08:00", "2028-02-29T00:00:00Z"),
        ):
            with self.subTest(deadline=deadline):
                task = self.create_task(token, deadline=deadline)
                self.assertEqual(task["deadline"], expected)

    def test_pin_and_creation_order_survive_restart(self):
        token = self.login()
        first = self.create_task(token, title="先发布")
        pinned = self.create_task(token, title="置顶通知", pinned=True)
        newest = self.create_task(token, title="后发布")
        guest = create_app(self.config).test_client()
        self.assertEqual(
            [task["id"] for task in self.tasks(guest)],
            [pinned["id"], newest["id"], first["id"]],
        )


class CategoryMigrationTestCase(unittest.TestCase):
    LEGACY_SCHEMA = """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('admin', 'user'))
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL CHECK (category IN ('notice', 'activity', 'homework')),
            deadline TEXT,
            pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
            author_id INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_tasks_deadline ON tasks(deadline);
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = str(Path(temporary.name) / "legacy.sqlite3")
        self.config = {
            "TESTING": True,
            "SECRET_KEY": "only-for-isolated-migration-tests",
            "DATABASE": self.database,
        }
        with sqlite3.connect(self.database) as connection:
            connection.executescript(self.LEGACY_SCHEMA)
            connection.execute(
                "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, ?)",
                (7, "migration-admin", "isolated-test-placeholder", "admin"),
            )
            self.legacy_rows = [
                (11, "已有班级通知", "保留内容及时间戳。", "notice", "2026-10-25T15:59:00Z", 1, 7, "2026-10-01T01:00:00Z", "2026-10-02T02:00:00Z"),
                (12, "已有活动", "没有截止时间。", "activity", None, 0, 7, "2026-10-03T03:00:00Z", "2026-10-03T03:00:00Z"),
                (13, "已有作业", "保持原来的作业分类。", "homework", "2026-10-26T15:59:00Z", 0, 7, "2026-10-04T04:00:00Z", "2026-10-05T05:00:00Z"),
            ]
            connection.executemany(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", self.legacy_rows
            )
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (99, *self.legacy_rows[0][1:]),
            )
            connection.execute("DELETE FROM tasks WHERE id = 99")

    def test_legacy_categories_migrate_without_data_loss_and_restart_is_idempotent(self):
        expected_rows = [
            tuple("course" if index == 3 and value == "notice" else value for index, value in enumerate(row))
            for row in self.legacy_rows
        ]
        for startup in range(2):
            with self.subTest(startup=startup):
                app = create_app(self.config)
                response = app.test_client().get("/api/tasks")
                self.assertEqual(response.status_code, 200)
                tasks = {task["id"]: task for task in response.get_json()["tasks"]}
                self.assertEqual(set(tasks), {11, 12, 13})
                self.assertEqual(tasks[11]["category"], "course")
                self.assertEqual(tasks[11]["author"], "migration-admin")
                self.assertIs(tasks[11]["pinned"], True)
                with sqlite3.connect(self.database) as connection:
                    self.assertEqual(
                        connection.execute("SELECT * FROM tasks ORDER BY id").fetchall(), expected_rows
                    )
                    self.assertEqual(
                        connection.execute("SELECT * FROM users").fetchall(),
                        [(7, "migration-admin", "isolated-test-placeholder", "admin")],
                    )
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertIn(
                        "idx_tasks_deadline",
                        {row[1] for row in connection.execute("PRAGMA index_list(tasks)")},
                    )
                    self.assertEqual(connection.execute("SELECT * FROM favorites").fetchall(), [])
                    self.assertIn(
                        "idx_favorites_task",
                        {row[1] for row in connection.execute("PRAGMA index_list(favorites)")},
                    )
                    self.assertEqual(
                        {(row[2], row[3], row[6]) for row in connection.execute("PRAGMA foreign_key_list(favorites)")},
                        {("users", "user_id", "CASCADE"), ("tasks", "task_id", "CASCADE")},
                    )
        with sqlite3.connect(self.database) as connection:
            for category in ("exam", "course"):
                row = list(expected_rows[0][1:])
                row[2] = category
                cursor = connection.execute(
                    "INSERT INTO tasks (title, content, category, deadline, pinned, author_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
                self.assertGreater(cursor.lastrowid, 99, "Migration must preserve deleted IDs in the AUTOINCREMENT sequence.")

    def test_empty_legacy_table_preserves_deleted_id_sequence(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("DELETE FROM tasks")
        create_app(self.config)
        create_app(self.config)
        with sqlite3.connect(self.database) as connection:
            row = list(self.legacy_rows[0][1:])
            row[2] = "exam"
            cursor = connection.execute(
                "INSERT INTO tasks (title, content, category, deadline, pinned, author_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            self.assertGreater(cursor.lastrowid, 99)

    def test_registration_on_legacy_database_cannot_inherit_admin_default(self):
        client = create_app(self.config).test_client()
        token = client.get("/api/session").get_json()["csrfToken"]
        response = client.post(
            "/api/register",
            json={"username": "legacy-reader", "password": "reader-password", "passwordConfirm": "reader-password", "role": "admin"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["user"]["role"], "user")
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute("SELECT id, username, role FROM users ORDER BY id").fetchall(),
                [(7, "migration-admin", "admin"), (8, "legacy-reader", "user")],
            )

    def test_favorites_on_migrated_database_preserve_tasks_and_accounts(self):
        app = create_app(self.config)
        client = app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["user_id"] = 7
            browser_session["csrf_token"] = "isolated-migration-csrf"
        response = client.put("/api/favorites/11", json={}, headers={"X-CSRF-Token": "isolated-migration-csrf"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"userId": 7, "taskId": 11, "favorited": True})
        with sqlite3.connect(self.database) as connection:
            previous_tasks = connection.execute("SELECT * FROM tasks ORDER BY id").fetchall()
            previous_users = connection.execute("SELECT * FROM users ORDER BY id").fetchall()
            previous_favorites = connection.execute("SELECT * FROM favorites").fetchall()
        create_app(self.config)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT * FROM tasks ORDER BY id").fetchall(), previous_tasks)
            self.assertEqual(connection.execute("SELECT * FROM users ORDER BY id").fetchall(), previous_users)
            self.assertEqual(connection.execute("SELECT * FROM favorites").fetchall(), previous_favorites)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
