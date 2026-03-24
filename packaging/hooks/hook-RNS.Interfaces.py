# PyInstaller hook for RNS.Interfaces.
#
# RNS.Interfaces.__init__ builds __all__ via glob.glob() at import time.
# In a frozen build there are no .py files on disk, so the glob returns
# nothing and `from RNS.Interfaces import *` (in Reticulum.py) imports an
# empty set — leaving names like `Interface` unbound in Reticulum's globals.
#
# collect_submodules() tells PyInstaller to include every submodule in the
# bundle. The runtime hook (rthook_rns_interfaces.py) patches __all__ and
# injects the submodule objects as attributes before Reticulum.py runs.

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("RNS.Interfaces")
