# PyInstaller hook for the RNS (Reticulum) package.
#
# RNS.Interfaces.__init__ builds __all__ via glob.glob() at import time,
# which returns nothing in a frozen build (no .py files on disk).
# This means `from RNS.Interfaces import *` imports an empty set and names
# like `Interface`, `TCPInterface`, etc. are never bound in Reticulum.py's
# namespace, causing NameError at runtime.
#
# collect_submodules ensures every submodule is included in the bundle.
# hiddenimports lists them explicitly so PyInstaller traces their dependencies.

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules("RNS")
datas = collect_data_files("RNS")
