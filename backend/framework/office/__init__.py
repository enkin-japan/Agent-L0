"""OfficeCLI 集成层（Word 生成波起点）。

外部依赖形态与 LibreOffice 同纪律：运行时探测二进制、缺失优雅降级（工具不注册），
不进 app bundle。出网红线：子进程环境强制 OFFICECLI_SKIP_UPDATE=1（装机时已
config autoUpdate false，此处纵深防御）；落盘红线：OFFICECLI_NO_AUTO_RESIDENT=1
禁常驻进程——每次调用一进程、退出即落盘，"其他工具读到旧文件"这一类事故在
结构上不存在（我们的构建走 batch 单趟，常驻的性能收益本就用不上）。
"""
from .officecli import (OfficeCliError, batch, find_officecli, run_cli,  # noqa: F401
                        validate)
from .md_docx import md_to_commands, write_docx  # noqa: F401
