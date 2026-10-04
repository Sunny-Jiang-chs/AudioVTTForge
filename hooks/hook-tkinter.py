"""Collect Tcl/Tk runtimes from the common Anaconda Windows layout."""

import os
import sys

from PyInstaller.utils.hooks import logger
from PyInstaller.utils.hooks.tcl_tk import tcltk_info


def _tcl_tk_runtime_dlls():
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
    logger.info("Collected Tcl/Tk runtime DLLs: %r", [item[0] for item in binaries])
else:
    logger.warning("No Tcl/Tk runtime DLLs found under %s", sys.base_prefix)

if not _datas:
    logger.warning("No Tcl/Tk data files collected; tkinter may fail to start")


def hook(hook_api):
    hook_api.add_datas(_datas)
