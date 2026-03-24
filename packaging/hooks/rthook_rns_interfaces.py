import sys

import RNS.Interfaces.Interface          as _iface_Interface
import RNS.Interfaces.LocalInterface     as _iface_Local
import RNS.Interfaces.AutoInterface      as _iface_Auto
import RNS.Interfaces.BackboneInterface  as _iface_Backbone
import RNS.Interfaces.TCPInterface       as _iface_TCP
import RNS.Interfaces.UDPInterface       as _iface_UDP
import RNS.Interfaces.I2PInterface       as _iface_I2P
import RNS.Interfaces.RNodeInterface     as _iface_RNode
import RNS.Interfaces.RNodeMultiInterface as _iface_RNodeMulti
import RNS.Interfaces.WeaveInterface     as _iface_Weave
import RNS.Interfaces.SerialInterface    as _iface_Serial
import RNS.Interfaces.KISSInterface      as _iface_KISS
import RNS.Interfaces.AX25KISSInterface  as _iface_AX25KISS
import RNS.Interfaces.PipeInterface      as _iface_Pipe

_INTERFACE_MAP = {
    "Interface":            _iface_Interface,
    "LocalInterface":       _iface_Local,
    "AutoInterface":        _iface_Auto,
    "BackboneInterface":    _iface_Backbone,
    "TCPInterface":         _iface_TCP,
    "UDPInterface":         _iface_UDP,
    "I2PInterface":         _iface_I2P,
    "RNodeInterface":       _iface_RNode,
    "RNodeMultiInterface":  _iface_RNodeMulti,
    "WeaveInterface":       _iface_Weave,
    "SerialInterface":      _iface_Serial,
    "KISSInterface":        _iface_KISS,
    "AX25KISSInterface":    _iface_AX25KISS,
    "PipeInterface":        _iface_Pipe,
}

# Inject interface submodule objects directly into RNS.Reticulum's module globals.
# `from RNS.Interfaces import *` in Reticulum.py produces an empty set in frozen
# builds because RNS.Interfaces.__init__ uses glob.glob() to build __all__, which
# finds nothing when there are no .py files on disk. Injecting into the module dict
# ensures the names are present when _synthesize_interface() does a LOAD_GLOBAL.
_rns_reticulum = sys.modules["RNS.Reticulum"]
for _name, _mod in _INTERFACE_MAP.items():
    setattr(_rns_reticulum, _name, _mod)

