# 参与开发

欢迎通过 Issue 描述问题、提出改进建议，或通过 Pull Request 提交代码。修改前可先检查已有 Issue，较大的改动建议先讨论目标和兼容方案。

## 本地开发

1. 在 GitHub 上 Fork 本仓库，将自己的 Fork 克隆到本地，并为本次修改创建分支。
2. 按 [README.md](README.md#快速开始) 创建独立 Python 环境、安装依赖并创建自己的管理员账号。
3. 使用本地生成的数据进行开发。不要把环境变量 `CLASS_NOTICE_INSTANCE_PATH` 指向正式网站的数据目录。

项目主要使用 Python 标准库、Flask 和原生 JavaScript，无需前端构建步骤。建议使用 Python 3.12 和 Node.js 22 或更新版本。生产部署额外需要 `requirements-production.txt` 中的依赖；Ubuntu 附件预览依赖见 [部署说明](deploy/README.md)。

## 提交前检查

在项目根目录、已激活 Python 环境中执行：

```bash
python database/export_schema.py --check
python -m unittest discover -s tests -v
node --test tests/test_frontend.js
git diff --check
```

后端测试使用临时数据库，前端单元测试使用 Node.js 内置测试工具，不需要浏览器或额外 npm 包。没有启用真实 Office 集成测试时，相关可选用例显示 `skipped` 属于预期结果。

### 可选：浏览器测试

需要 Node.js、npm 和已安装 Python 依赖的 `.venv`：

```bash
npm install --no-save --package-lock=false playwright
npx playwright install chromium
node tests/test_attachments_browser.js
```

该脚本自动创建临时测试数据，检查上传、预览、下载、移除附件以及手机布局。截图输出到已忽略的 `tests/artifacts/`。默认 Python 路径为 `.venv/bin/python`；Windows 或自定义环境可通过 `TEST_PYTHON` 指定 Python 可执行文件。

### 可选：真实 Word 转换

安装本机 LibreOffice 及部署说明中对应系统的转换依赖后，在 macOS / Linux 运行：

```bash
CLASS_NOTICE_RUN_OFFICE_TESTS=1 python -m unittest discover -s tests -p 'test_document_preview.py' -v
```

Linux 需要允许隔离转换；以测试结果确认转换能力，不要为了通过测试移除隔离。

## 数据库变更

数据库源结构在 `app.py` 和 `attachments.py`。修改结构时应考虑现有数据的迁移，补充相应测试，再运行 `python database/export_schema.py` 更新 `database/schema.sql`。该文件只有建表语句，不是生产数据副本，也不是升级旧数据库的迁移脚本。更多说明见 [数据库文档](database/README.md)。

## 提交内容

- 一次 Pull Request 尽量围绕一个明确问题，说明修改目的、可见行为变化和验证方法。涉及界面时可附使用虚构数据的截图。
- 新增功能或修复问题时，按影响补充有意义的测试；保持管理员权限、CSRF 校验、文件类型与大小校验、数据持久化约定。
- 提交源码、测试、配置示例和文档；数据库、真实通知、账号、密码哈希、会话密钥、上传附件、备份、虚拟环境均不进入仓库。测试中的虚构账号仅用于隔离的临时环境。
- 提交前查看 `git status` 和 `git diff --cached`，确认没有实际运行数据或个人服务器配置。

仓库不自动发布到服务器。合并代码后，由部署维护者按照部署文档备份、发布和验证，协作者不需要共享正式网站账号或数据库。
