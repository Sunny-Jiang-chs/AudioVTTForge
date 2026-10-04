# -*- mode: python ; coding: utf-8 -*-
"""运行时钩子：把 _tkinter.pyd 所在目录加进 DLL 搜索路径。

conda/Anaconda 布局下 tcl86t.dll / tk86t.dll 可能和 _tkinter.pyd 不在同一目录，
或 PATH 被清理过，导致 import tkinter 报 DLL load failed。这里在加载前显式注册。
"""

import os
import sys

_base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
for _dir in {_base, os.path.join(_base, "_internal")}:
    if os.path.isdir(_dir) and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(_dir)
        except OSError:
            pass
