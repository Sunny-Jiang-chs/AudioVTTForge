"""Register bundled directories for Windows DLL lookup before tkinter loads."""

import os
import sys


_base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
for _directory in {_base, os.path.join(_base, "_internal")}:
    if os.path.isdir(_directory) and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(_directory)
        except OSError:
            pass
