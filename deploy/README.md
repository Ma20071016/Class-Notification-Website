# Ubuntu 服务器部署

本文适用于使用 systemd 的 Ubuntu 服务器。示例先通过 HTTP 验证连通性，再配置域名和 HTTPS。文中的 `203.0.113.10` 是文档示例地址，部署时必须替换为自己的服务器 IP 或域名；SSH 用户名也需按实际环境修改。

默认架构使用公网 8080 端口和本机 8000 端口，可与其他端口上的站点共存。启动前检查系统版本、端口占用、现有 Nginx 配置和目录权限；如需改端口，应同步修改 Gunicorn、Nginx 和防火墙规则，不覆盖其他站点。

这些文件是可复用的部署模板，不代表任何特定服务器的运行状态。先替换示例值并核对每一步结果；不要把带占位符的命令原样执行。

## 目录与运行方式

| 内容 | 服务器位置 | 更新时处理 |
| --- | --- | --- |
| 每次发布的代码和独立虚拟环境 | `/opt/class-notice/releases/<版本号>/` | 每次新增一个版本 |
| 当前版本符号链接 | `/opt/class-notice/current` | 验证后切换 |
| SQLite、附件原件 `uploads/`、稳定的签名密钥和备份 | `/var/lib/class-notice/` | 保留，不用空目录覆盖 |
| 环境配置 | `/etc/class-notice.env` | 保留，按需修改 |
| systemd 服务 | `/etc/systemd/system/class-notice.service` | 配置变更时更新 |
| Nginx 配置 | `/etc/nginx/sites-available/class-notice` | 配置变更时更新 |

公网请求 → Nginx 8080 端口 → 本机 Gunicorn 8000 端口 → Flask → SQLite。Gunicorn 使用一个 worker、四个线程，与当前进程内登录/注册限流匹配；暂不启用多实例或多 worker。

附件保存在数据库同级的 `uploads/`，只通过应用接口访问，不把该目录作为 Nginx 静态目录暴露。生产默认位置为 `/var/lib/class-notice/uploads/`，服务用户需能读写该目录。代码版本目录不存原件。

## 1. 本地打包

在项目目录生成源码程序包：

```bash
.venv/bin/python deploy/build_release.py
```

命令生成 `dist/class-notice-<时间>-<唯一标识>.tar.gz`，只包含白名单代码（含 `attachments.py` 和 `document_preview.py`）、静态文件、测试、贡献指南、数据库结构源码和部署说明；没有真实账号、通知、收藏、上传原件、密钥或虚拟环境。`database/` 只提供表结构与结构导出工具，不是运行数据备份。依赖在 Ubuntu 上重新安装，不能上传 Mac 的 `.venv` 直接使用。

如果本机已有需要保留的数据，再执行备份；刚克隆项目、尚无数据库时无需执行此步：

```bash
.venv/bin/python deploy/backup_data.py
```

备份工具用 SQLite 在线备份接口生成一致性快照，不会停掉本机网站，不会把密码或密钥打印出来。工具先取得数据库写锁，再从另一个只读连接生成快照，并复制快照引用的 `uploads/` 原件；读者仍可查看通知，写入可能短暂等待。生成的 `backup-info.json` 记录时间、表行数、附件清单、数量和字节数，方便迁移前后核对。备份数据库、被引用的附件和存在的 `secret_key`，不带 `admin-credentials.txt`、未引用的孤立文件或可重新生成的预览文件。附件缺失、大小不符、文件或 uploads 目录为符号链接、存储名包含路径时，备份失败并清理半成品。若通过环境变量提供 `CLASS_NOTICE_SECRET_KEY`，需单独安全保管该值。

如果希望带上本机现有数据，实际迁移前停止本机写入并重新备份一次；开发期间继续产生的新记录不会自动同步到已经生成的备份。数据只能通过 SSH 等受控渠道传输。

## 2. 首次登录与检查

使用服务器控制台，或从本机终端通过 SSH 登录。以下以用户名 `ubuntu` 为例：

```bash
ssh ubuntu@203.0.113.10
```

凭据仅在受信任的 SSH 或云服务商登录界面输入，不写入仓库、示例文件或公开问题中。登录后先检查：

```bash
cat /etc/os-release
python3 --version
sudo ss -ltnp
df -h
```

需要 Python 3.10 或更高版本。若 8080/8000 端口被占用，先调整本项目配置，不覆盖已有服务。安装 Nginx、Python 环境、Word 预览转换器和中文字体：

```bash
sudo apt update
sudo apt install nginx python3-venv python3-pip libreoffice-writer fonts-noto-cjk bubblewrap antiword
libreoffice --version
```

Word 预览使用本机 LibreOffice 的 headless 转换，不依赖外部文档服务。转换在独立临时目录及独立 LibreOffice 配置中运行，禁用宏，并通过 Linux `bubblewrap`（macOS 使用 `sandbox-exec`）隔离网络。若缺少转换器或系统不允许隔离，DOCX 回退到简化预览，旧式 `.doc` 使用 `antiword` 提取文字；无法预览时仍可下载原件。含外部引用的 DOCX 直接使用简化预览。安装中文字体可以减少缺字，复杂版式仍需结合原件核对。单次转换超时为 35 秒，随附代理配置为此预留了时间。

程序会自动寻找 `soffice`/`libreoffice`；特殊安装位置可在 `/etc/class-notice.env` 中设置 `CLASS_NOTICE_SOFFICE=/绝对路径/soffice` 后重启。必须实际测试服务用户能否运行转换及 `bubblewrap`；不能仅凭命令已安装就判定转换正常，也不要通过移除隔离来处理失败。在云防火墙或安全组中为本站增加 TCP 8080 入站规则，保留已有站点和 SSH 所需规则；配置 HTTPS 时再开放 443，8000 不对公网开放。

## 3. 安装一个代码版本

以下是首次部署模板，`<版本号>`、`<程序包文件名>` 和 `<解包目录>` 都需要替换；实际部署时会结合服务器检查结果逐步执行。

1. 用 `scp` 把代码包传到服务器的用户家目录。
2. 在用户家目录新建解包目录，解压程序包，得到 `class-notice/`。
3. 创建独立服务用户 `class-notice`（系统用户、无登录 shell），创建 `/opt/class-notice/releases/`。
4. 将解包后的代码复制到 `/opt/class-notice/releases/<版本号>/`；代码归 `root`，服务用户只需读取权限。
5. 在该版本目录创建 Linux 虚拟环境，安装生产依赖：

```bash
sudo python3 -m venv /opt/class-notice/releases/<版本号>/.venv
sudo /opt/class-notice/releases/<版本号>/.venv/bin/python -m pip install -r /opt/class-notice/releases/<版本号>/requirements-production.txt
```

6. 创建服务用户专用数据目录：

```bash
sudo install -d -m 700 -o class-notice -g class-notice /var/lib/class-notice
```

7. **迁移本机数据或从空站开始二选一。** 若迁移，只在服务未启动且目标数据库不存在时，把同一份备份中的 `class_notice.sqlite3`、`uploads/`（存在时）和 `secret_key` 放入 `/var/lib/class-notice/`；文件权限设为 `600`，数据目录和 uploads 目录权限为 `700`、属主均为 `class-notice`。核对 `backup-info.json` 中的附件数量、文件名和大小，不要只还原数据库。不要传明文初始密码文件。若空站开始，不复制数据库，应用首次启动自动建表，随后通过命令行交互创建管理员。已有数据库绝不直接覆盖。
8. 将 `/opt/class-notice/current` 指向这一版本，首次安装环境文件（已经存在时先检查，不覆盖）：

```bash
sudo install -m 600 /opt/class-notice/current/deploy/class-notice.env.example /etc/class-notice.env
```

默认是 IP + HTTP、单层本机 Nginx、独立数据目录；以后启用 HTTPS 时再改 Cookie 设置。没有配置外部签名密钥时，会在数据目录生成并保留 `secret_key`。

## 4. 校验并启用服务

安装 systemd 文件后先验证：

```bash
sudo install -m 644 /opt/class-notice/current/deploy/class-notice.service /etc/systemd/system/class-notice.service
sudo systemd-analyze verify /etc/systemd/system/class-notice.service
sudo systemctl daemon-reload
sudo systemctl enable --now class-notice
sudo systemctl status class-notice --no-pager
curl --fail http://127.0.0.1:8000/api/tasks
```

创建新管理员（仅空站或确实需要新增时执行；输入密码不回显）：

```bash
sudo -u class-notice env CLASS_NOTICE_INSTANCE_PATH=/var/lib/class-notice /opt/class-notice/current/.venv/bin/python /opt/class-notice/current/app.py init-admin --username admin
```

随后编辑本版本的 `deploy/nginx.conf`，将 `server_name 203.0.113.10` 替换为实际 IP 或域名，再安装独立的 Nginx 站点配置。先检查是否存在同名配置/链接，不覆盖已有站点；同时创建模板所需的访问日志目录：

```bash
sudo install -d -m 750 -o root -g adm /var/log/nginx/class-notice
sudo install -m 644 /opt/class-notice/current/deploy/nginx.conf /etc/nginx/sites-available/class-notice
sudo ln -s /etc/nginx/sites-available/class-notice /etc/nginx/sites-enabled/class-notice
sudo nginx -t
sudo systemctl reload nginx
curl --fail -H 'Host: 203.0.113.10' http://127.0.0.1:8080/api/tasks
```

上例中的 `Host` 也需替换为配置中的实际 IP 或域名。`nginx -t` 必须成功后再 reload。示例配置只在 8080 端口声明本站，不删除或修改其他站点。测试页面用 `http://你的服务器IP:8080` 打开；若外网打不开，检查云防火墙、安全组和 Ubuntu 防火墙是否允许 TCP 8080，以及 Nginx 状态。不要开放 Gunicorn 的 8000 端口。访问日志及轮转说明见 [TRAFFIC.md](TRAFFIC.md)。

随附 Nginx 配置允许最大 `102m` 的请求（高于应用的 101 MiB 上限），上传体读取超时为 120 秒，代理读写超时为 90 秒。应用逐个限制附件为 20 MiB、每通知最多 5 个，额外空间用于 multipart 表单开销。上传提示 413 时同时检查反向代理和应用限制；更新时若只替换 Python 代码而保留旧 `128k` Nginx 配置，附件仍会被代理拦截。

初期 HTTP 没有传输加密。第一轮先验证公开页面连通性；正式登录使用前应配置 HTTPS。启用 HTTPS 后，改 `CLASS_NOTICE_SECURE_COOKIE=1` 并重启服务；不应在纯 HTTP 阶段提前开启。域名和 HTTPS 的具体操作在确定域名及服务器环境后进行。

日志查看：

```bash
sudo journalctl -u class-notice -n 80 --no-pager
sudo tail -n 80 /var/log/nginx/error.log
```

## 5. 以后更新功能

1. 在本地修改、测试并打包。先判断是否需要数据库迁移；不要用删除数据库来完成升级。
2. 上传到新的版本目录，并安装该版本的虚拟环境；旧版本和正式数据保留。
3. 在临时数据目录验证新版本。不要在正式数据库上运行开发测试。
4. 准备切换时停止服务，备份正式数据：

```bash
sudo systemctl stop class-notice
sudo -u class-notice /opt/class-notice/current/.venv/bin/python /opt/class-notice/current/deploy/backup_data.py --instance /var/lib/class-notice
```

备份失败就不要继续切换，应恢复旧服务。在线也可执行备份工具，但需要同步数据库与代码版本的发布流程应先停写。

5. 记录原 `current` 指向，再切换到新版本，执行该版本明确要求的兼容迁移并启动。当前应用启动时处理已有分类迁移、收藏表及附件表创建，保留已有通知、账号和收藏；不代表能自动处理未来所有数据库改动。
6. 按新版本要求安装或更新依赖。附件预览需要上一节的 LibreOffice、字体、`bubblewrap` 和 `antiword`；Nginx 配置变更时先 `nginx -t` 再 reload，systemd 单元变更时执行 `systemctl daemon-reload`，最后重启 `class-notice`。不要仅热替换页面文件。代码包必须同时包含 `attachments.py`、`document_preview.py` 和相应前端文件。
7. 确认服务正常后检查页面、普通用户/管理员登录、通知发布与收藏，再分别上传 PDF、DOCX 和 DOC 验证预览及下载，核对下载原件内容，重启后再次检查附件。实际 Word 转换、Nginx 上传上限与服务权限必须在 Ubuntu 上验证。纯代码更新通常只需短暂重启；数据库升级耗时取决于实际迁移。

数据库结构没有变化时，可把 `current` 切回旧版本并重启。涉及结构变化时，先确认旧代码兼容；确需恢复数据库时必须停服务并备份当前状态，再按明确的恢复方案恢复对应快照。旧快照不包含升级后新增或修改的数据，这些记录需要单独核对并迁回，回滚不会自动保留它们。

恢复附件时必须将数据库与同一快照的 `uploads/` 一并恢复。通知删除后，旧备份仍可能保留其附件，按自己的保留周期清理备份；不能用清空正式 uploads 目录代替清理备份。定期核对磁盘空间，附件与备份均占用数据分区。备份工具按 `<instance>/class_notice.sqlite3` 和 `<instance>/uploads/` 的标准布局运行；如在代码中覆盖测试用 `DATABASE`/`UPLOAD_FOLDER`，必须先确保备份实际对应的数据库和附件目录。

## 本地与服务器验证的边界

本地可验证：Python/前端测试、Gunicorn 启动和 HTTP 请求、数据目录重启持久化、代理头信任、备份恢复与包内敏感文件排除。

仍需在 Ubuntu 上验证：systemd 单元、Nginx 配置语法和端口冲突、云防火墙、服务器依赖安装、域名/HTTPS及外网访问。本机通过测试不等于线上已经可访问。
