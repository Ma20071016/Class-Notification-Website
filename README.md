# 班级通知网站（Class Notification Website）

集中查看班级通知、活动和作业截止时间的基础网站。后端使用 **Python + Flask**，数据库使用 **SQLite**，前端使用原生 HTML、CSS 和 JavaScript。

欢迎大家参与完善与优化！安装步骤见下文，提交改进请先阅读 [贡献指南](CONTRIBUTING.md)，数据库说明见 [database/README.md](database/README.md)。

## 已实现

- 访客无需登录即可查看通知列表、详情和截止时间。
- 提供“普通用户登录”和“管理员登录”两个入口；普通用户可以注册，注册成功后自动登录。
- 左侧“我的收藏”保存个人关注的通知；登录后可收藏、取消收藏，记录按账号独立保存。
- 管理员登录后可以发布、编辑、删除通知，并置顶重要通知。
- 管理员可以为通知上传 PDF、Word（`.doc`、`.docx`）附件；每个文件最多 20 MiB，每条通知最多 5 个。访客和用户可在通知详情中在线预览或下载原件。
- 通知分为作业通知、考试通知、课程通知、活动通知；支持搜索、分类、截止状态筛选及排序。
- 截止时间可选；填写和显示统一使用北京时间，数据库存储 UTC 时间。
- 页面显示剩余时间、24 小时内即将截止的任务和已截止状态；定时刷新列表。
- “最近截止”旁的“最新发布”展示最近 24 小时发布的全部通知，按发布时间倒序排列，点击可查看详情；编辑旧通知不会将其算作新发布。
- “本周一览”默认展示今天的截止任务；点击本周日期即可在日历下方查看当天任务的标题和类型，无需弹窗。日期按北京时间归属，列表按截止时间排序，无截止时间的通知不计入。
- 通知保存到 SQLite，刷新网页或重启服务不会丢失。

当前提醒是**页面内提示**。邮件、微信、短信和关闭网页后的推送尚未实现。

## 快速开始

需要 Python 3.10 或更新版本，建议使用 Python 3.12。macOS / Linux 在终端执行：

```bash
git clone https://github.com/Ma20071016/Class-Notification-Website.git
cd Class-Notification-Website
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py init-admin --username admin
python app.py run --host 127.0.0.1 --port 5050
```

Windows PowerShell 创建和激活环境时改用 `py -m venv .venv` 和 `.venv\Scripts\Activate.ps1`，其余 `python` 命令相同。

创建管理员时按提示设置自己的密码，没有默认账号密码。首次运行会自动创建空数据库和所需表，无需导入他人的数据库。保持终端窗口打开，在浏览器访问 <http://127.0.0.1:5050>；按 `Control + C` 停止服务。

右上角选择“普通用户登录”或“管理员登录”，使用对应身份的账号登录。

### 登录与注册

- 普通用户：点击“普通用户登录” → “注册普通用户”，填写用户名、密码和确认密码。用户名为 1–64 个字符，不含空白字符；密码为 8–1024 个字符。
- 注册成功后会自动登录；以后可直接从普通用户入口登录。普通用户可以查看通知、详情和截止提醒。
- 管理员：从“管理员登录”入口使用已有管理账号登录，登录后可发布、编辑、删除和置顶通知。
- 公开注册只创建普通用户，管理员账号仍由网站维护者通过命令行创建，不能通过注册页面取得管理权限。
- 登录后右上角显示用户名和身份，点击“退出登录”可返回访客状态；访客仍能查看公开通知。

### 我的收藏

- 在通知卡片或详情中点击“收藏”，再从左侧“我的收藏”集中查看；点击“取消收藏”即可移除，不会删除原通知。
- 未登录时仍能查看全部公开通知；点击“我的收藏”会提示登录，登录后才会显示当前账号的收藏。
- 普通用户和管理员均可管理自己的收藏，其他账号无法查看或更改你的收藏记录。
- 收藏支持关键词搜索、截止状态筛选和排序。记录保存在 SQLite，退出登录、刷新或重启服务后不会丢失；同一账号在其他设备登录可查看相同收藏。
- 管理员删除原通知时，对应收藏会一并清理。手机端导航为两行布局，收藏按钮支持触屏操作。

### 通知附件

- 管理员在发布、编辑通知时选择附件，编辑时可以删除已有附件；保存后读者即可在详情中预览或下载。允许的格式为 `.pdf`、`.doc`、`.docx`，扩展名和文件内容都会校验。单个原件最多 20 MiB，一条通知最多保留 5 个附件；整次请求上限为 101 MiB（含表单开销）。
- PDF 使用浏览器内置阅读器预览；浏览器不支持嵌入预览时仍可下载原件。Word 优先由服务器本机的 LibreOffice 转为 PDF 预览，文档不会发送到第三方在线转换服务。复杂格式或缺失字体可能影响预览，下载内容始终为上传的原件。
- Ubuntu 服务器需安装 LibreOffice Writer、中文字体、`bubblewrap` 和 `antiword`，具体步骤见 [deploy/README.md](deploy/README.md)。没有转换器或隔离转换不可用时，`.docx` 可显示简化预览，`.doc` 可通过 `antiword`（macOS 使用系统 `textutil`）显示文字预览；无法预览时页面保留下载入口并说明原因。DOCX 简化预览支持段落、基础格式、表格和部分内嵌图片，不保证还原分页、表格样式或复杂排版。
- 通知附件与通知一样公开可读。删除附件或通知会清理对应原件；备份包含附件，已创建的备份不会随通知删除自动更新。

## 手机打开

1. 在已激活的环境中运行 `python app.py run --host 0.0.0.0 --port 5050`，手机与电脑连接**同一 Wi-Fi**。
2. 在电脑的网络设置中找到局域网 IP，然后在手机打开 `http://电脑的局域网IP:5050`。手机上不能使用 `localhost` 或 `127.0.0.1`，那会指向手机自己。
3. 电脑保持开机、服务保持运行；如 macOS 防火墙询问 Python 是否允许接入，允许局域网连接。

手机布局支持窄屏，菜单、列表、两个登录入口和注册表单会自动调整；表单内容较长时可在窗口内滚动。部分校园网或访客 Wi-Fi 会隔离设备，如果同一 Wi-Fi 仍无法访问，可尝试让电脑连接手机热点。Wi-Fi 改变后 IP 可能变化，需要重新查看网络设置。

## 管理员账号

按照“快速开始”创建首个管理员。之后使用新的用户名执行 `python app.py init-admin --username 新用户名` 可创建其他管理员；每位协作者在自己的开发数据库中创建账号。

修改已有管理员的密码：

```bash
.venv/bin/python app.py init-admin --username admin --reset
```

该命令只修改指定管理员的密码，不会把普通用户提升为管理员。

## 项目结构

```text
app.py                   Flask 应用、接口、登录、权限与数据库
attachments.py           附件格式校验、上传暂存与原件持久化
document_preview.py      本地 Word 转换与简化在线预览
database/                可共享的 SQL 表结构、生成工具与初始化说明
requirements.txt         Python 依赖
requirements-production.txt  生产依赖（含 Gunicorn）
templates/index.html     页面结构
static/app.js            列表交互、表单、倒计时
static/style.css         桌面和手机样式
instance/                本机数据库、uploads/ 附件与密钥，自动创建且不提交
tests/                   后端、前端、附件、数据库与部署测试
deploy/                  部署模板、备份与源码打包工具
CONTRIBUTING.md           开发流程、测试与提交说明
```

SQLite 数据文件位于 `instance/class_notice.sqlite3`，附件原件位于同级 `instance/uploads/`。停止服务后备份整个 `instance/` 目录可保留通知、账号、附件和会话签名密钥；也可使用下文的备份工具生成可核验的一致快照。不要只复制数据库后遗漏附件，也不要把数据目录放进公开仓库。

## 验证

在已激活并安装依赖的 Python 环境中运行：

```bash
python database/export_schema.py --check
python -m unittest discover -s tests -v
```

前端单元测试使用 Node.js（建议 22 或更新版本），无需安装 npm 依赖：

```bash
node --test tests/test_frontend.js
```

可选的真实浏览器与 LibreOffice 集成测试见 [贡献指南](CONTRIBUTING.md)。

## 当前运行范围

开发命令默认只监听本机；添加 `--host 0.0.0.0` 可用于同一局域网的设备联调。其他人克隆仓库后得到的是独立开发环境，不会自动连接现有网站或同步数据。互联网部署需要单独的 Python 服务环境，SQLite 适合当前单实例的小规模班级应用；不要将 Flask 开发服务器直接作为生产服务。

管理员权限在每个写入接口由后端检查，密码以哈希保存，写入请求校验 CSRF token。部署到互联网时应使用生产 WSGI 服务与 HTTPS，并设置 `CLASS_NOTICE_SECURE_COOKIE=1`。可通过 `CLASS_NOTICE_SECRET_KEY` 配置至少 32 字符的持久签名密钥；未配置时自动使用本机 `instance/secret_key`。

## 云服务器部署与后续更新

仓库提供 Ubuntu 云服务器所需的 Gunicorn、Nginx 和 systemd 配置，具体步骤见 [deploy/README.md](deploy/README.md)。示例使用 Nginx 8080 端口，后端 8000 端口仅供本机访问；部署者需按自己的域名、IP、端口与已有服务调整配置。

- 服务器代码放在 `/opt/class-notice/current`，通知、账号、收藏、附件原件（`uploads/`）和签名密钥放在独立的 `/var/lib/class-notice`；以后更新程序时保留这个数据目录。
- `CLASS_NOTICE_INSTANCE_PATH` 可设置数据目录，必须使用绝对路径；不设置时仍使用本机原有的 `instance/`，原启动脚本不变。
- `CLASS_NOTICE_PROXY_HOPS` 默认 `0`，不信任转发头；本项目随附的单层 Nginx 配置使用 `1`，Gunicorn 仅监听本机回环地址。
- `CLASS_NOTICE_SECURE_COOKIE=0` 用于第一阶段 IP + HTTP 访问。配置并验证 HTTPS 后改成 `1`；不要在普通 HTTP 上直接设为 `1`，否则浏览器无法正常携带登录 Cookie。HTTP 没有传输加密，仅适合初期连通性验证；正式登录使用前应配置 HTTPS。
- 更新页面或业务逻辑通常只需备份、上传代码和重启；新增数据库字段时，还需要与旧数据兼容的迁移。

本地生成独立的数据备份和不含数据的程序包：

```bash
.venv/bin/python deploy/backup_data.py
.venv/bin/python deploy/build_release.py
```

备份默认保存在 `instance/backups/`，包含数据库、该快照引用的附件原件及存在时的签名密钥，不含明文初始密码文件和临时预览文件。备份期间会短暂阻止写入，保证数据库记录与附件匹配；发现缺失、链接或大小不符的原件会失败并删除不完整备份。`backup-info.json` 记录附件清单、总大小和各表行数。程序包默认保存在 `dist/`，不包含数据库、上传附件、密钥、初始密码、虚拟环境或托管平台元数据。数据备份含账号信息、文档与密钥，只用于受控备份和迁移，不可公开。
