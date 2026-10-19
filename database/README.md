# 数据库结构与本地初始化

项目使用 SQLite。此目录只共享建表 SQL 和生成工具；不包含真实数据库、账号、密码、通知、收藏或附件。每位协作者使用自己的本地数据库，并创建自己的管理员账号。

## 初始化开发环境

在项目根目录执行（已安装依赖时可跳过前三步）：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py init-admin --username your-admin-name
python app.py run
```

Windows PowerShell 可用 `py -m venv .venv` 创建虚拟环境，用 `.venv\Scripts\Activate.ps1` 激活，然后继续执行后面的 `python` 命令。

`init-admin` 会自动创建数据库及表，再提示输入和确认管理员密码（至少 8 位）。请将 `your-admin-name` 替换为自己的账号名。项目不提供默认密码或多人共用的账号；普通用户可在网页自行注册。已存在的账号不会被覆盖，确需重置本地管理员密码时，显式运行 `python app.py init-admin --username your-admin-name --reset`。

默认数据库为 `instance/class_notice.sqlite3`，会话密钥为 `instance/secret_key`，上传附件保存在 `instance/uploads/`。若环境中设置了 `CLASS_NOTICE_INSTANCE_PATH`，应用会使用该绝对路径作为运行数据目录；开发时请确认它指向自己的目录。实际运行数据不应提交到 Git；协作时共享代码和此目录的结构文件即可。

启动时应用自动检查并初始化表结构，因此无需手动导入 `schema.sql`。该 SQL 可用于查看结构或创建全新的空数据库，不是旧数据库的升级脚本。已有数据库应由应用现有的迁移逻辑处理，升级前先备份。

## 表与关系

| 表 | 用途与主要关系 |
| --- | --- |
| `users` | 本地账号、密码哈希及 `admin` / `user` 角色；账号名唯一。 |
| `tasks` | 通知正文、分类、截止时间、置顶状态和时间戳；`author_id` 引用 `users.id`。 |
| `favorites` | 用户收藏；`user_id` 与 `task_id` 为联合主键，用户或通知删除时级联删除收藏。 |
| `attachments` | 附件元数据；`task_id` 引用通知，通知删除时级联删除元数据；文件内容单独存储于上传目录。 |

SQLite 还会为自增主键建立内部表 `sqlite_sequence`。应用在每次数据库连接时启用外键约束。删除附件原件由应用负责，仅在数据库提交成功后清理磁盘文件；请通过网站或 API 管理通知与附件。

## 同步结构文件

结构的源头是 `app.py` 的 `SCHEMA` / `FAVORITES_SCHEMA` 和 `attachments.py` 的 `ATTACHMENTS_SCHEMA`。修改这些常量后，在项目根目录执行：

```bash
python database/export_schema.py
python database/export_schema.py --check
python -m unittest discover -s tests -p 'test_database_setup.py' -v
```

生成工具只使用 Python 标准库，通过 AST 读取三个 SQL 字符串常量；不会导入或启动应用，也不会读取 `instance/`、创建管理员或打开实际数据库。`--check` 只检查快照是否过期，过期时返回非零退出码，适合放入持续集成检查。请一并提交源代码与重新生成的 `database/schema.sql`。
