# 第三方组件

| 组件 | 版本 | 用途 | 上游 |
|---|---|---|---|
| PySide6 / Shiboken6 / Qt | 6.8.3 | 桌面界面与 WebEngine | https://doc.qt.io/qtforpython-6.8/licenses.html |
| Pandoc | 3.6.4 | Markdown AST 与 DOCX 写入 | https://github.com/jgm/pandoc/tree/3.6.4 |
| MathJax | 3.2.2 | 本地数学预览 | https://github.com/mathjax/MathJax/tree/3.2.2 |
| GSAP | 3.13.0 | 本地预览动效 | https://gsap.com/community/standard-license/ |
| python-docx | 1.2.0 | DOCX 后处理 | https://github.com/python-openxml/python-docx |
| Pydantic | 2.11.9 | 不可变模型与验证 | https://github.com/pydantic/pydantic |

Python 依赖的实际锁定版本见 requirements.lock；资源来源与完整性见 resources/vendor-manifest.json。GSAP 的原始文件头、README、许可页面快照和取得记录保留在 resources/preview/gsap，不能将其标为 MIT。

Pandoc 与 MathJax 由开发准备脚本取得，其上游附带版权和许可证保留在下载目录中。Qt WebEngine 还包含 Chromium 等组件；冻结运行时的完整依赖与许可清单需根据实际构建产物另行整理。

此仓库只提供应用源码和开发所需资源，不包含 CPython、Qt DLL、Pandoc 可执行文件或完整冻结程序。不以源码公开代替第三方二进制分发义务；应用自身未在本仓库指定开源许可证。
