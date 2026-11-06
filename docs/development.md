# 开发与验证

## 环境

使用 Windows x64 与 Python 3.12。按 README 建立虚拟环境、安装 requirements.lock、以 editable 模式安装项目，再执行 tools/fetch_resources.py。资源下载脚本校验锁定哈希和 npm integrity，不由 GUI 调用。

通过源码运行与打包程序均需完整的 resources 目录。editable 安装适用于本仓库；本项目尚未配置自包含 Python wheel 的资源分发。

## 测试

```powershell
..venvScriptspython.exe -m pytest -q --junitxml=artifacts	ests.xml
..venvScriptspython.exe -m md2word smoke --output-dir artifactssmoke
```

`tests/fixtures` 均为固定输入。回归覆盖解析、公式降级、Word 结构、格式锚点、安全输出、锁竞争、坏 JSON、草稿恢复和界面调度。Qt WebEngine 相关检查需要可用的 Windows 桌面会话；不要以系统管理员身份运行浏览器沙箱。

`tools/verify_stage2.py` 至 `verify_stage5.py` 是固定样例验证入口；使用新的输出目录，避免覆盖已有验证结果。artifacts、build、dist、下载缓存和虚拟环境不纳入 Git。

## 构建

```powershell
..venvScriptspython.exe -m PyInstaller --clean --noconfirm packagingmd2word.spec
```

构建配置包含本地资源、图标及 Windows 版本信息，排除不使用的 Qt 模块和虚拟键盘插件。构建后应验证启动、预览、导出、帮助、中文空格路径与普通用户权限。构建成功不等于独立机器及文档软件兼容性验收通过。

## 验证证据的范围

OOXML 与内容清单检查、真实 Word 对象检查、完整页面视觉检查及打印检查分别记录。网络请求拦截不能代替物理断网；短时间合成负载不能代替数小时真实编辑。
