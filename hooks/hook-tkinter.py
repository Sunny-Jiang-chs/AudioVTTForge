# -*- mode: python ; coding: utf-8 -*-
"""完整替代 PyInstaller 内置的 hook-_tkinter.py。

为什么需要它（2026-10 实测）：
1. 本机是 Anaconda（D:\\tools\\Anoconda3）布局，tcl86t.dll / tk86t.dll 位于
   <prefix>\\Library\\bin，该目录不在 PATH 里，PyInstaller 的 DLL 依赖分析找不到
   它们，打出的包一启动就报：
       ImportError: DLL load failed while importing _tkinter: 找不到指定的模块。
2. 自定义 hook 会整体覆盖内置 hook（ModuleHookCache 每个模块只保留最高优先级的一个），
   所以这里必须把内置 hook 的 Tcl/Tk 数据收集也一并做掉，否则 _tcl_data / _tk_data
   不会被收集，运行时报 "Can't find a usable init.tcl"。

注意：hook 文件名用 hook-tkinter.py（模块名 tkinter），避免与内置 hook-_tkinter.py
同名而互相覆盖；tkinter 包被收集时会连带触发本 hook。
"""

import os
import sys

from PyInstaller.utils.hooks import logger
from PyInstaller.utils.hooks.tcl_tk import tcltk_info


def _tcl_tk_runtime_dlls():
    """收集 Anaconda 布局下的 Tcl/Tk 运行库。"""
    found = []
    candidates = [
        os.path.join(sys.base_prefix, "Library", "bin"),
        os.path.join(sys.base_prefix, "DLLs"),
    ]
    for directory in candidates:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            low = name.lower()
            if low.startswith(("tcl", "tk")) and low.endswith(".dll"):
                found.append((os.path.join(directory, name), "."))
    return found


binaries = _tcl_tk_runtime_dlls()
_datas = list(tcltk_info.data_files)

if binaries:
    logger.info("hook-tkinter.py: collected Tcl/Tk runtime DLLs: %r", [item[0] for item in binaries])
else:
    logger.warning("hook-tkinter.py: no Tcl/Tk runtime DLLs found under %s", sys.base_prefix)

if not _datas:
    logger.warning("hook-tkinter.py: no Tcl/Tk data files collected; tkinter may fail to start")


def hook(hook_api):
    # 数据文件是 3 元 TOC 元组，必须走 add_datas（与内置 hook 一致），
    # 直接赋给模块级 datas 会因为 2/3 元元组不一致而报 unpack 错误。
    hook_api.add_datas(_datas)

