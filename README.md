# Markdown 转 Word

将 AI 输出的 Markdown 内容、已有 Markdown 文稿或纯文本，在本机整理为可继续编辑的 Word 文档。支持预览、整篇与选区排版、数学公式检查、后台导出和草稿恢复。

版本 **1.0.0**。目标平台为 **Windows 11 x64 / Python 3.12**。运行时使用本地资源，不调用 AI 服务，不上传正文，不依赖云端账户。

## 功能

- UTF-8 Markdown / TXT 导入与直接粘贴，原始输入保持不变。
- 六级标题、列表、简单表格、引用、代码和超链接。
- 支持范围内的公式转换为可编辑的 Word 原生公式；不能可靠转换时提示并保留可见源码，不确定时阻断导出。
- 本地 MathJax 预览；整篇样式、个人样式、选中文字及段落格式。
- 后台快照导出、取消和超时处理；保护已有输出文件，必要时要求另存。
- 自动草稿、备份恢复、坏文件隔离、设置持久化、单实例和本地脱敏诊断。

## 开发环境

在仓库根目录的 PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe tools\fetch_resources.py
.\.venv\Scripts\python.exe -m md2word gui
```

首次准备环境需要联网；资源下载脚本只在开发时显式运行。Pandoc 3.6.4、MathJax 3.2.2、GSAP 3.13.0 的版本及完整性信息记录在 `resources/vendor-manifest.json`。Pandoc 和 MathJax 的下载内容不纳入 Git。

## 命令行

```powershell
# 只检查输入，不修改源文件。
.\.venv\Scripts\python.exe -m md2word inspect "input.md" --mode markdown

# 导出到新的 DOCX 文件。
.\.venv\Scripts\python.exe -m md2word export "input.md" --mode markdown --output "output.docx"

# 纯文本模式。
.\.venv\Scripts\python.exe -m md2word export "input.txt" --mode plain --output "plain.docx"
```

## 测试与构建

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm packaging\md2word.spec
```

测试使用固定样例，包含真实 Pandoc、Qt WebEngine、文件锁、恢复和内容校验。部分测试依赖 Windows 原生行为，需要在 Windows 普通用户桌面会话中运行。构建输出为 `dist/md2word`；保留 EXE 旁的 `_internal` 目录。

## 项目结构

```text
src/md2word/
  domain/       输入模型、来源映射、修复及局部格式
  conversion/   公式检查、DOCX 写入与 OOXML 验证
  exporting/    后台调度、安全输出和临时工作区
  storage/      草稿、设置、单实例和诊断
  templates/    样式解析、字体与 Word 样式
  ui/           PySide6 桌面界面与本地预览
resources/      模板、示例、预览脚本与资源锁
tests/          回归测试和固定输入
tools/          开发资源准备与验证工具
packaging/      PyInstaller 构建配置
docs/           架构、开发、使用及支持范围
```

## 技术文档

- [架构与数据流](docs/architecture.md)
- [使用方法](docs/usage.md)
- [开发与验证](docs/development.md)
- [支持范围与限制](docs/limitations.md)
- [第三方组件](docs/third-party.md)

预览不模拟 Word 的精确分页；当前不嵌入图片，不提供 PDF 导出、批量转换或 AI 内容生成。独立干净系统、物理断网、完整 Word/WPS 页面及打印兼容性仍需单独验证，不能用自动测试代替。

作者：暮玥。第三方组件保留各自的版权和许可证。本仓库未为应用自身指定开源许可证。
