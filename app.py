"""Class notices: a small Flask app with SQLite-backed administration.

Run ``python app.py init-admin --username admin`` before signing in, then
``python app.py run``. Ordinary visitors can read every task without an account.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps
import getpass
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import threading
import time

from flask import Flask, g, jsonify, render_template, request, send_file, session, url_for
from werkzeug.exceptions import BadRequest, HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from attachments import (
    ATTACHMENTS_SCHEMA, MAX_ATTACHMENTS, MAX_REQUEST_BYTES,
    AttachmentValidationError, stage_uploads, storage_path,
)
from document_preview import DocumentPreviewError, render_word_preview


TASK_FIELDS = {"title", "content", "category", "deadline", "pinned"}
CATEGORIES = {"homework", "exam", "course", "activity"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user'))
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('homework', 'exam', 'course', 'activity')),
    deadline TEXT,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    author_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);
"""
FAVORITES_SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_favorites_task ON favorites(task_id);
"""


class ValidationError(ValueError):
    """An invalid API or command-line value, safe to show to the user."""


def migrate_task_categories(database: sqlite3.Connection) -> None:
    """Upgrade the original CHECK constraint without losing existing records.

    SQLite cannot alter a CHECK constraint in place. Rebuild only the task
    table in one transaction, preserving record IDs and AUTOINCREMENT history.
    The original general notices become course notices in the new taxonomy.
    """
    database.execute("BEGIN IMMEDIATE")
    try:
        definition = database.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()[0]
        if "'exam'" in definition and "'course'" in definition and "'notice'" not in definition:
            database.commit()
            return
        # Preserve any indexes/triggers in addition to the built-in deadline index.
        auxiliary_sql = [
            row[0] for row in database.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name = 'tasks' "
                "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
            ).fetchall()
        ]
        sequence = database.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'tasks'"
        ).fetchone()
        previous_sequence = sequence[0] if sequence else 0
        database.execute("""
            CREATE TABLE tasks_category_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT NOT NULL CHECK (category IN ('homework', 'exam', 'course', 'activity')),
                deadline TEXT,
                pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
                author_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        database.execute("""
            INSERT INTO tasks_category_migration
                (id, title, content, category, deadline, pinned, author_id, created_at, updated_at)
            SELECT id, title, content,
                CASE WHEN category = 'notice' THEN 'course' ELSE category END,
                deadline, pinned, author_id, created_at, updated_at
            FROM tasks
        """)
        database.execute("DROP TABLE tasks")
        database.execute("ALTER TABLE tasks_category_migration RENAME TO tasks")
        for sql in auxiliary_sql:
            database.execute(sql)
        sequence = database.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'tasks'"
        ).fetchone()
        if sequence:
            database.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = 'tasks'",
                (max(previous_sequence, sequence[0]),),
            )
        elif previous_sequence:
            database.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('tasks', ?)",
                (previous_sequence,),
            )
        database.commit()
    except Exception:
        database.rollback()
        raise


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_secret(instance_path: str) -> str:
    """Persist a random signing secret; never replace it on each app restart."""
    secret_file = Path(instance_path) / "secret_key"
    secret_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not secret_file.exists():
        # Publish a complete file atomically, without replacing another worker's
        # secret when multiple application processes initialize at the same time.
        descriptor, temporary_name = tempfile.mkstemp(prefix=".secret-", dir=secret_file.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_hex(32))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_name, secret_file)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary_name)
    secret = secret_file.read_text(encoding="utf-8").strip()
    if len(secret) < 32:
        raise RuntimeError("instance/secret_key 无效，请检查文件；不要在运行中修改密钥。")
    return secret


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        from flask import current_app

        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 10000")
    return g.db


def public_user(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def current_user() -> sqlite3.Row | None:
    # Permissions always come from the database, never from a browser role flag.
    user_id = session.get("user_id")
    if not isinstance(user_id, int):
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str):
        session.permanent = True
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def require_csrf(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        expected = session.get("csrf_token")
        received = request.headers.get("X-CSRF-Token", "")
        if not isinstance(expected, str) or not hmac.compare_digest(expected.encode("utf-8"), received.encode("utf-8")):
            return jsonify(error="页面验证已失效，请刷新页面后重试。"), 403
        return view(*args, **kwargs)

    return wrapped


def require_admin(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify(error="请先登录管理员账号。"), 401
        if user["role"] != "admin":
            return jsonify(error="只有管理员可以修改任务。"), 403
        return view(*args, **kwargs)

    return wrapped


def require_user(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify(error="请先登录后使用收藏功能。"), 401
        g.authenticated_user = user
        return view(*args, **kwargs)

    return wrapped


def json_object() -> dict:
    if not request.is_json:
        raise ValidationError("请使用 JSON 格式提交数据。")
    try:
        payload = request.get_json()
    except BadRequest as exc:
        raise ValidationError("JSON 数据格式不正确。") from exc
    if not isinstance(payload, dict):
        raise ValidationError("提交的数据必须是 JSON 对象。")
    return payload


def clean_username(value) -> str:
    if not isinstance(value, str):
        raise ValidationError("请输入账号。")
    value = value.strip()
    if not 1 <= len(value) <= 64 or any(char.isspace() for char in value):
        raise ValidationError("账号应为 1–64 个字符，且不能包含空白字符。")
    return value


def clean_deadline(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 50:
        raise ValidationError("截止时间格式不正确。")
    # Keep a predictable Internet timestamp format; reject ambiguous local times.
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise ValidationError("截止时间必须包含日期、时间和时区。")
    try:
        if not value.endswith("Z") and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            raise ValueError("invalid timezone offset")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("missing timezone")
        parsed = parsed.astimezone(timezone.utc)
        if not 2000 <= parsed.year <= 2100:
            raise ValueError("unsupported year")
    except (ValueError, OverflowError) as exc:
        raise ValidationError("截止时间无效，请使用 2000–2100 年之间的日期。") from exc
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_task(payload: dict, *, partial: bool = False) -> dict:
    if set(payload) - TASK_FIELDS:
        raise ValidationError("提交的数据包含未知字段。")
    if partial and not payload:
        raise ValidationError("请提交需要修改的任务内容。")
    result = {}
    for field, label, maximum in (("title", "标题", 100), ("content", "内容", 10000)):
        if partial and field not in payload:
            continue
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"请填写任务{label}。")
        value = value.strip()
        if len(value) > maximum:
            raise ValidationError(f"任务{label}不能超过 {maximum} 个字符。")
        result[field] = value
    if not partial or "category" in payload:
        category = payload.get("category", "course")
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ValidationError("请选择有效的任务分类。")
        result["category"] = category
    if not partial or "deadline" in payload:
        result["deadline"] = clean_deadline(payload.get("deadline"))
    if not partial or "pinned" in payload:
        pinned = payload.get("pinned", False)
        if not isinstance(pinned, bool):
            raise ValidationError("置顶选项必须为 true 或 false。")
        result["pinned"] = int(pinned)
    return result


def task_submission(*, partial: bool = False) -> tuple[dict, list, list[int]]:
    """Use one mutation endpoint for old JSON clients and attachment forms."""
    files = []
    if request.mimetype == "multipart/form-data":
        if set(request.form) != {"payload"} or len(request.form.getlist("payload")) != 1 or set(request.files) - {"attachments"}:
            raise ValidationError("附件表单格式不正确，请刷新页面后重试。")
        try:
            payload = json.loads(request.form["payload"])
        except (ValueError, TypeError) as exc:
            raise ValidationError("任务数据格式不正确。") from exc
        if not isinstance(payload, dict):
            raise ValidationError("提交的数据必须是 JSON 对象。")
        files = request.files.getlist("attachments")
    else:
        payload = json_object()
    removed = payload.pop("removedAttachmentIds", []) if partial else []
    if (not isinstance(removed, list) or len(removed) > MAX_ATTACHMENTS
            or any(type(value) is not int or value <= 0 for value in removed)
            or len(set(removed)) != len(removed)):
        raise ValidationError("需要移除的附件列表无效。")
    if partial and not payload and (files or removed):
        values = {}
    else:
        values = clean_task(payload, partial=partial)
    return values, files, removed


def attachment_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "size": row["size"],
        "extension": Path(row["stored_name"]).suffix.lstrip("."),
        "previewUrl": url_for("preview_attachment", attachment_id=row["id"]),
        "downloadUrl": url_for("download_attachment", attachment_id=row["id"]),
    }


def task_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "category": row["category"],
        "deadline": row["deadline"],
        "pinned": bool(row["pinned"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "author": row["author"],
        "attachments": [attachment_dict(item) for item in get_db().execute(
            "SELECT * FROM attachments WHERE task_id = ? ORDER BY id", (row["id"],)
        ).fetchall()],
    }


TASK_SELECT = "SELECT tasks.*, users.username AS author FROM tasks JOIN users ON users.id = tasks.author_id"


def create_app(test_config: dict | None = None) -> Flask:
    instance_path = os.environ.get("CLASS_NOTICE_INSTANCE_PATH")
    if instance_path is not None and (not instance_path or not Path(instance_path).is_absolute()):
        raise RuntimeError("CLASS_NOTICE_INSTANCE_PATH 必须是绝对路径。")
    proxy_hops_value = os.environ.get("CLASS_NOTICE_PROXY_HOPS", "0")
    if not re.fullmatch(r"[0-9]+", proxy_hops_value):
        raise RuntimeError("CLASS_NOTICE_PROXY_HOPS 必须是非负整数。")
    proxy_hops = int(proxy_hops_value)
    app = Flask(__name__, instance_relative_config=True, instance_path=instance_path)
    if proxy_hops:
        # Enable only behind the specified trusted proxies, with direct access
        # to the WSGI server blocked. Never trust forwarded host/port/prefix.
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=proxy_hops, x_proto=proxy_hops,
            x_host=0, x_port=0, x_prefix=0,
        )
    app.config.from_mapping(
        DATABASE=str(Path(app.instance_path) / "class_notice.sqlite3"),
        SECRET_KEY=os.environ.get("CLASS_NOTICE_SECRET_KEY"),
        SESSION_COOKIE_NAME="class_notice_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("CLASS_NOTICE_SECURE_COOKIE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        MAX_FORM_MEMORY_SIZE=128 * 1024,
        MAX_FORM_PARTS=16,
        UPLOAD_FOLDER=None,
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = load_secret(app.instance_path)
    elif len(app.config["SECRET_KEY"]) < 32 and not app.testing:
        raise RuntimeError("CLASS_NOTICE_SECRET_KEY 至少需要 32 个字符。")
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not app.config["UPLOAD_FOLDER"]:
        app.config["UPLOAD_FOLDER"] = str(Path(app.config["DATABASE"]).parent / "uploads")
    upload_folder = Path(app.config["UPLOAD_FOLDER"]).resolve()

    @app.teardown_appcontext
    def close_db(_exception=None):
        database = g.pop("db", None)
        if database is not None:
            database.close()

    with app.app_context():
        database = get_db()
        database.executescript(SCHEMA)
        database.commit()
        migrate_task_categories(database)
        # Install referencing tables after the legacy task-table rebuild.
        database.executescript(FAVORITES_SCHEMA)
        database.executescript(ATTACHMENTS_SCHEMA)
        database.commit()

    # Small, per-process rate limiter. Only trust request.remote_addr: production
    # proxies must be configured explicitly; never trust arbitrary forwarded IPs.
    failures: dict[str, deque] = defaultdict(deque)
    failure_lock = threading.Lock()
    failure_window = 15 * 60
    failure_limit = 10
    registration_attempts: dict[str, deque] = defaultdict(deque)
    registration_lock = threading.Lock()
    registration_window = 15 * 60
    registration_limit = 10
    # Make nonexistent usernames cost about the same as a wrong password.
    dummy_hash = generate_password_hash(secrets.token_urlsafe(32))

    @app.before_request
    def limit_json_payloads():
        # Attachment forms need room for five documents. Preserve the original
        # small limit for every other endpoint and for JSON-only mutations.
        attachment_form = request.endpoint in {"create_task", "update_task"} and request.mimetype == "multipart/form-data"
        if not attachment_form:
            request.max_content_length = min(app.config["MAX_CONTENT_LENGTH"], 128 * 1024)

    @app.errorhandler(ValidationError)
    @app.errorhandler(AttachmentValidationError)
    def validation_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            messages = {400: "请求格式不正确。", 404: "请求的内容不存在。", 405: "不支持此操作。", 413: "提交的内容过大；每条通知最多 5 个附件，单个附件不超过 20 MB。"}
            return jsonify(error=messages.get(error.code, "请求失败，请稍后重试。")), error.code
        return error

    @app.errorhandler(sqlite3.OperationalError)
    def database_error(error):
        app.logger.exception("Database operation failed")
        return jsonify(error="数据库暂时不可用，请稍后重试。"), 503

    @app.errorhandler(OSError)
    def storage_error(error):
        app.logger.exception("Attachment storage operation failed")
        return jsonify(error="服务器暂时无法读写附件，请稍后重试。"), 503

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if request.endpoint == "preview_attachment" else "DENY"
        if request.endpoint == "preview_attachment":
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'; sandbox"
                if response.mimetype == "text/html" else "frame-ancestors 'self'"
            )
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/session")
    def get_session():
        return jsonify(user=public_user(current_user()), csrfToken=csrf_token())

    @app.post("/api/login")
    @require_csrf
    def login():
        payload = json_object()
        username = clean_username(payload.get("username"))
        password = payload.get("password")
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise ValidationError("请输入有效的密码。")
        role = payload.get("role", "admin")
        if not isinstance(role, str) or role not in {"admin", "user"}:
            raise ValidationError("请选择普通用户登录或管理员登录。")
        address = request.remote_addr or "unknown"
        now = time.monotonic()
        with failure_lock:
            # Bound memory for this deliberately small local/MVP service.
            for key in list(failures):
                while failures[key] and failures[key][0] <= now - failure_window:
                    failures[key].popleft()
                if not failures[key]:
                    del failures[key]
            if len(failures.get(address, ())) >= failure_limit:
                response = jsonify(error="登录失败次数过多，请 15 分钟后重试。")
                response.headers["Retry-After"] = str(failure_window)
                return response, 429
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        password_ok = check_password_hash(user["password_hash"] if user else dummy_hash, password)
        if user is None or not password_ok or user["role"] != role:
            with failure_lock:
                if len(failures) >= 10000 and address not in failures:
                    failures.pop(next(iter(failures)))
                failures[address].append(time.monotonic())
            return jsonify(error="账号或密码不正确。"), 401
        with failure_lock:
            failures.pop(address, None)
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        return jsonify(user=public_user(user), csrfToken=csrf_token())

    @app.post("/api/register")
    @require_csrf
    def register():
        payload = json_object()
        username = clean_username(payload.get("username"))
        password = payload.get("password")
        if not isinstance(password, str) or not 8 <= len(password) <= 1024:
            raise ValidationError("密码应为 8–1024 个字符。")
        confirmation = payload.get("passwordConfirm")
        if not isinstance(confirmation, str) or confirmation != password:
            raise ValidationError("两次输入的密码不一致。")
        address = request.remote_addr or "unknown"
        now = time.monotonic()
        with registration_lock:
            for key in list(registration_attempts):
                while registration_attempts[key] and registration_attempts[key][0] <= now - registration_window:
                    registration_attempts[key].popleft()
                if not registration_attempts[key]:
                    del registration_attempts[key]
            if len(registration_attempts.get(address, ())) >= registration_limit:
                response = jsonify(error="注册操作过于频繁，请 15 分钟后重试。")
                response.headers["Retry-After"] = str(registration_window)
                return response, 429
            if len(registration_attempts) >= 10000 and address not in registration_attempts:
                registration_attempts.pop(next(iter(registration_attempts)))
            registration_attempts[address].append(now)
        database = get_db()
        try:
            with database:
                # Public registration always creates a reader. Never accept a
                # privilege level from the submitted form or a schema default.
                cursor = database.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
                    (username, generate_password_hash(password)),
                )
                user = database.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError:
            return jsonify(error="该账号已被注册，请更换账号或直接登录。"), 409
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        return jsonify(user=public_user(user), csrfToken=csrf_token()), 201

    @app.post("/api/logout")
    @require_csrf
    def logout():
        json_object()
        session.clear()
        return jsonify(user=None, csrfToken=csrf_token())

    @app.get("/api/tasks")
    def list_tasks():
        rows = get_db().execute(TASK_SELECT + " ORDER BY tasks.pinned DESC, tasks.created_at DESC, tasks.id DESC").fetchall()
        return jsonify(tasks=[task_dict(row) for row in rows])

    @app.get("/api/favorites")
    @require_user
    def list_favorites():
        user_id = g.authenticated_user["id"]
        rows = get_db().execute(
            "SELECT task_id FROM favorites WHERE user_id = ? ORDER BY task_id", (user_id,)
        ).fetchall()
        return jsonify(userId=user_id, taskIds=[row["task_id"] for row in rows])

    @app.put("/api/favorites/<int:task_id>")
    @require_user
    @require_csrf
    def add_favorite(task_id):
        if request.get_data():
            json_object()
        user_id = g.authenticated_user["id"]
        database = get_db()
        with database:
            # The insertion and existence check share a write transaction, so
            # a simultaneous task deletion cannot leave a dangling favorite.
            database.execute(
                "INSERT INTO favorites (user_id, task_id, created_at) "
                "SELECT ?, id, ? FROM tasks WHERE id = ? "
                "ON CONFLICT (user_id, task_id) DO NOTHING",
                (user_id, utc_now(), task_id),
            )
            if database.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
                return jsonify(error="这条任务不存在或已被删除。"), 404
        return jsonify(userId=user_id, taskId=task_id, favorited=True)

    @app.delete("/api/favorites/<int:task_id>")
    @require_user
    @require_csrf
    def remove_favorite(task_id):
        if request.get_data():
            json_object()
        user_id = g.authenticated_user["id"]
        database = get_db()
        with database:
            database.execute("DELETE FROM favorites WHERE user_id = ? AND task_id = ?", (user_id, task_id))
        return jsonify(userId=user_id, taskId=task_id, favorited=False)

    @app.post("/api/tasks")
    @require_admin
    @require_csrf
    def create_task():
        values, files, _removed = task_submission()
        now = utc_now()
        database = get_db()
        with stage_uploads(files, upload_folder) as staged:
            with database:
                database.execute("BEGIN IMMEDIATE")
                cursor = database.execute(
                    "INSERT INTO tasks (title, content, category, deadline, pinned, author_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (values["title"], values["content"], values["category"], values["deadline"], values["pinned"], current_user()["id"], now, now),
                )
                staged.publish(database, cursor.lastrowid, now, upload_folder)
                row = database.execute(TASK_SELECT + " WHERE tasks.id = ?", (cursor.lastrowid,)).fetchone()
            staged.committed = True
        return jsonify(task=task_dict(row)), 201

    @app.patch("/api/tasks/<int:task_id>")
    @require_admin
    @require_csrf
    def update_task(task_id):
        values, files, removed = task_submission(partial=True)
        database = get_db()
        values["updated_at"] = utc_now()
        # Keys were explicitly whitelisted above; user values remain bound parameters.
        assignments = ", ".join(f"{field} = ?" for field in values)
        with stage_uploads(files, upload_folder) as staged:
            with database:
                database.execute("BEGIN IMMEDIATE")
                if database.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
                    return jsonify(error="这条任务不存在或已被删除。"), 404
                existing = database.execute("SELECT * FROM attachments WHERE task_id = ?", (task_id,)).fetchall()
                if not set(removed).issubset({item["id"] for item in existing}):
                    raise ValidationError("需要移除的附件不属于此通知或已被删除，请刷新后重试。")
                if len(existing) - len(removed) + len(staged.files) > MAX_ATTACHMENTS:
                    raise ValidationError(f"每条通知最多保留 {MAX_ATTACHMENTS} 个附件。")
                old_files = [item["stored_name"] for item in existing if item["id"] in removed]
                database.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*values.values(), task_id))
                if removed:
                    database.executemany("DELETE FROM attachments WHERE id = ? AND task_id = ?", [(item_id, task_id) for item_id in removed])
                staged.publish(database, task_id, values["updated_at"], upload_folder)
                row = database.execute(TASK_SELECT + " WHERE tasks.id = ?", (task_id,)).fetchone()
            staged.committed = True
        remove_attachment_files(old_files)
        return jsonify(task=task_dict(row))

    @app.delete("/api/tasks/<int:task_id>")
    @require_admin
    @require_csrf
    def delete_task(task_id):
        database = get_db()
        with database:
            database.execute("BEGIN IMMEDIATE")
            old_files = [row["stored_name"] for row in database.execute(
                "SELECT stored_name FROM attachments WHERE task_id = ?", (task_id,)
            ).fetchall()]
            cursor = database.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cursor.rowcount == 0:
            return jsonify(error="这条任务不存在或已被删除。"), 404
        remove_attachment_files(old_files)
        return jsonify(ok=True)

    def remove_attachment_files(stored_names):
        # Delete only after the database commits. A failed mutation must retain
        # every original file; a failed cleanup may safely be retried later.
        for stored_name in stored_names:
            try:
                storage_path(upload_folder, stored_name).unlink(missing_ok=True)
            except (OSError, AttachmentValidationError):
                app.logger.exception("Could not remove unreferenced attachment")

    def find_attachment(attachment_id):
        row = get_db().execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        if row is None:
            return None, None
        source = storage_path(upload_folder, row["stored_name"])
        if not source.is_file():
            return None, None
        return row, source

    @app.get("/api/attachments/<int:attachment_id>/download")
    def download_attachment(attachment_id):
        row, source = find_attachment(attachment_id)
        if row is None:
            return jsonify(error="附件不存在或已被删除。"), 404
        return send_file(source, mimetype=row["media_type"], as_attachment=True, download_name=row["name"], conditional=True)

    @app.get("/api/attachments/<int:attachment_id>/preview")
    def preview_attachment(attachment_id):
        row, source = find_attachment(attachment_id)
        if row is None:
            return jsonify(error="附件不存在或已被删除。"), 404
        extension = source.suffix.lstrip(".")
        if extension == "pdf":
            return send_file(source, mimetype="application/pdf", as_attachment=False, download_name=row["name"], conditional=True)
        try:
            with tempfile.TemporaryDirectory(prefix="notice-preview-") as temporary:
                output_dir = Path(temporary)
                rendered = render_word_preview(source, extension, output_dir)
                if (rendered.is_symlink() or not rendered.is_file()
                        or rendered.resolve().parent != output_dir.resolve()
                        or rendered.suffix not in {".pdf", ".html"}
                        or rendered.stat().st_size > 80 * 1024 * 1024):
                    raise DocumentPreviewError("文档预览暂时不可用，请下载原文件查看。")
                data = rendered.read_bytes()
                media_type = "application/pdf" if rendered.suffix == ".pdf" else "text/html"
                name = Path(row["name"]).stem + rendered.suffix
            return send_file(io.BytesIO(data), mimetype=media_type, as_attachment=False, download_name=name, conditional=True)
        except DocumentPreviewError as error:
            return jsonify(error=str(error)), 503
        except OSError:
            app.logger.exception("Attachment preview failed")
            return jsonify(error="文档预览暂时不可用，请下载原文件查看。"), 503

    return app


def initialize_admin(app: Flask, username: str, password: str, *, reset: bool = False) -> int:
    username = clean_username(username)
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
        raise ValidationError("管理员密码应为 8–1024 个字符。")
    with app.app_context():
        database = get_db()
        existing = database.execute("SELECT id, role FROM users WHERE username = ?", (username,)).fetchone()
        if existing is not None and reset:
            if existing["role"] != "admin":
                raise ValidationError("该账号不是管理员，不能通过重置密码更改权限。")
            with database:
                database.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(password), existing["id"]),
                )
            return existing["id"]
        try:
            with database:
                cursor = database.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                    (username, generate_password_hash(password)),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError("该账号已存在，未修改现有账号。") from exc
        return cursor.lastrowid


def main():
    parser = argparse.ArgumentParser(description="班级通知网站")
    commands = parser.add_subparsers(dest="command")
    admin = commands.add_parser("init-admin", help="创建管理员（不会覆盖现有账号）")
    admin.add_argument("--username", default="admin", help="管理员账号，默认 admin")
    admin.add_argument("--reset", action="store_true", help="显式重置已存在的管理员密码")
    run = commands.add_parser("run", help="启动本地开发服务")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    app = create_app()
    if args.command == "init-admin":
        password = os.environ.get("CLASS_NOTICE_ADMIN_PASSWORD")
        if password is None:
            password = getpass.getpass("设置管理员密码（至少 8 位）: ")
            confirmation = getpass.getpass("再次输入密码: ")
            if password != confirmation:
                parser.error("两次输入的密码不一致。")
        try:
            initialize_admin(app, args.username, password, reset=args.reset)
        except ValidationError as exc:
            parser.error(str(exc))
        print(f"管理员 {args.username} 已设置。现在可以运行 python app.py run。")
        return
    # Debug is intentionally off. For public deployment use a WSGI server + HTTPS.
    app.run(host=getattr(args, "host", "127.0.0.1"), port=getattr(args, "port", 5000), debug=False)


if __name__ == "__main__":
    main()
