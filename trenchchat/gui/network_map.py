"""
Network map dialog showing the currently recognised Reticulum topology.

Layout
------
A resizable dialog containing:
  - A canvas (NetworkMapWidget) that renders a force-directed graph via QPainter
  - A status bar showing node/path counts and interface info
  - A Refresh button and an auto-refresh toggle

Graph nodes
-----------
  ★  This device (yellow star)
  ◆  Interface / hub (orange diamond) — a connected network interface (e.g. TCP hub)
  ■  Transport / relay node (blue square) — a next-hop that is not a known peer
  ●  Known peer (green circle) — a destination whose identity is known via announce
  ○  Unknown destination (grey circle) — in path table but identity not recalled

Edges
-----
  Solid line  — direct path (1 hop) or interface connection
  Dashed line — multi-hop path; labelled with hop count

The graph uses a simple spring-layout (Fruchterman-Reingold) iterated on each
refresh so the layout settles over time.
"""

import math
import random
import time

import RNS

from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QPainterPath,
    QWheelEvent, QMouseEvent,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget,
    QPushButton, QLabel, QCheckBox, QSizePolicy,
)

# --- constants ---

_AUTO_REFRESH_MS   = 10_000   # 10 s
_LAYOUT_ITERATIONS = 80       # spring-layout steps per refresh
_REPULSION         = 8_000.0  # node-node repulsion constant
_ATTRACTION        = 0.04     # edge spring constant
_DAMPING           = 0.85     # velocity damping per step
_MIN_EDGE_LEN      = 120.0    # natural edge length (pixels)

# Node colours
_COL_SELF      = QColor("#f5c518")   # yellow — this device
_COL_INTERFACE = QColor("#ff8c42")   # orange — network interface / hub
_COL_TRANSPORT = QColor("#4a9eff")   # blue   — relay/transport node
_COL_PEER      = QColor("#4ec94e")   # green  — known peer
_COL_UNKNOWN   = QColor("#888888")   # grey   — unknown destination

_COL_EDGE_DIRECT    = QColor("#555555")
_COL_EDGE_MULTI     = QColor("#3a3a3a")
_COL_EDGE_INTERFACE = QColor("#664422")   # dim orange — interface link
_COL_LABEL          = QColor("#cccccc")
_COL_BG             = QColor("#1a1a1a")

_NODE_R_SELF      = 14
_NODE_R_INTERFACE = 12
_NODE_R_TRANSPORT = 11
_NODE_R_PEER      = 9
_NODE_R_UNKNOWN   = 7

_MAX_NODES = 120   # cap to keep the graph readable


# ---------------------------------------------------------------------------
# Data gathering (pure functions — testable without Qt)
# ---------------------------------------------------------------------------

def gather_network_data(rns: RNS.Reticulum, self_hex: str,
                        storage=None) -> dict:
    """
    Query the RNS instance for the current network topology.

    storage — optional Storage instance; when provided, peer nodes are labelled
              with the display name stored in the members table (i.e. the name
              seen in channel member lists) in preference to the announce app_data.

    Returns a dict with keys:
      nodes  — list[dict]: id, label, kind ('self'|'transport'|'peer'|'unknown'), hops
      edges  — list[dict]: src, dst, hops, direct (bool)
      interfaces — list[dict]: name, type, status, rxb, txb
      stats  — dict: node_count, path_count, interface_count
    """
    nodes: dict[str, dict] = {}   # hash_hex -> node dict
    edges: list[dict] = []

    # --- self node ---
    nodes[self_hex] = {
        "id":    self_hex,
        "label": "This device",
        "kind":  "self",
        "hops":  0,
    }

    # --- path table ---
    try:
        path_table = rns.get_path_table()
    except Exception:
        path_table = []

    # Fetch interface stats once; reused both for routing multi-hop peers through
    # the correct interface diamond node and for drawing the interface nodes.
    try:
        _iface_stats: dict = rns.get_interface_stats()
    except Exception:
        _iface_stats = {}

    iface_name_to_id: dict[str, str] = {
        (iface.get("short_name") or iface.get("name", "")): (
            f"__iface__{iface.get('short_name') or iface.get('name', '')}"
        )
        for iface in _iface_stats.get("interfaces", [])
    }

    # Collect all transport (next-hop) hashes so we can classify them.
    # Only count a via-hash as a transport node when it differs from the
    # destination itself (i.e. it is a true relay, not a direct 1-hop peer).
    transport_hashes: set[str] = set()
    for entry in path_table:
        dest_h = entry.get("hash")
        via = entry.get("via")
        if via and dest_h:
            via_hex = via.hex() if isinstance(via, bytes) else str(via)
            dest_h_hex = dest_h.hex() if isinstance(dest_h, bytes) else str(dest_h)
            if via_hex != self_hex and via_hex != dest_h_hex:
                transport_hashes.add(via_hex)

    seen_pairs: set[tuple] = set()

    for entry in path_table[:_MAX_NODES]:
        dest_hash = entry.get("hash")
        if dest_hash is None:
            continue
        dest_hex = dest_hash.hex() if isinstance(dest_hash, bytes) else str(dest_hash)
        hops = entry.get("hops", 0)
        via = entry.get("via")
        via_hex = via.hex() if isinstance(via, bytes) else (str(via) if via else None)
        # The interface name this path was learned through (may be None/empty)
        path_iface: str = entry.get("interface") or ""

        # Classify the destination
        identity = RNS.Identity.recall(dest_hash if isinstance(dest_hash, bytes)
                                       else bytes.fromhex(dest_hex))
        if dest_hex == self_hex:
            kind = "self"
        elif dest_hex in transport_hashes:
            kind = "transport"
        elif identity is not None:
            kind = "peer"
        else:
            kind = "unknown"

        label = _make_label(dest_hex, identity, kind, storage)

        if dest_hex not in nodes:
            nodes[dest_hex] = {
                "id":    dest_hex,
                "label": label,
                "kind":  kind,
                "hops":  hops,
            }

        # Determine the relay node to route through for multi-hop paths.
        # Prefer the interface diamond node (if the path came through a known
        # interface) over creating a separate floating transport hash node.
        relay_id: str | None = None
        if via_hex and via_hex != self_hex and hops > 1:
            iface_node_id = iface_name_to_id.get(path_iface)
            if iface_node_id:
                relay_id = iface_node_id          # anchor to interface diamond
            else:
                relay_id = via_hex                # fall back to transport hash node
                if relay_id not in nodes:
                    via_identity = RNS.Identity.recall(bytes.fromhex(relay_id))
                    nodes[relay_id] = {
                        "id":    relay_id,
                        "label": _make_label(relay_id, via_identity, "transport", storage),
                        "kind":  "transport",
                        "hops":  1,
                    }

        if relay_id:
            # self → relay
            pair = (self_hex, relay_id)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                edges.append({"src": self_hex, "dst": relay_id, "hops": 1, "direct": True,
                               "kind": "interface" if relay_id.startswith("__iface__") else "path"})
            # relay → dest
            pair2 = (relay_id, dest_hex)
            if pair2 not in seen_pairs:
                seen_pairs.add(pair2)
                edges.append({"src": relay_id, "dst": dest_hex,
                               "hops": hops, "direct": False})
        else:
            pair = (self_hex, dest_hex)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                edges.append({"src": self_hex, "dst": dest_hex,
                               "hops": hops, "direct": hops <= 1})

    # --- interface stats + interface nodes ---
    interfaces: list[dict] = []
    try:
        stats = _iface_stats  # already fetched above; reuse to avoid a second RPC
        for iface in stats.get("interfaces", []):
            iface_name = iface.get("short_name") or iface.get("name", "?")
            iface_type = iface.get("type", "")
            iface_status = iface.get("status", False)
            rxb = iface.get("rxb", 0)
            txb = iface.get("txb", 0)
            interfaces.append({
                "name":   iface_name,
                "type":   iface_type,
                "status": iface_status,
                "rxb":    rxb,
                "txb":    txb,
            })

            # Add the interface as a graph node so it appears on the map.
            # Use a stable synthetic ID so the layout doesn't reset on refresh.
            iface_id = f"__iface__{iface_name}"
            status_dot = "●" if iface_status else "○"
            type_short = (iface_type
                          .replace("ClientInterface", "")
                          .replace("Interface", "")
                          .strip())
            label = f"{status_dot} {iface_name}"
            if type_short:
                label += f" ({type_short})"
            nodes[iface_id] = {
                "id":    iface_id,
                "label": label,
                "kind":  "interface",
                "hops":  0,
            }
            # Edge: self → interface
            pair = (self_hex, iface_id)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                edges.append({
                    "src":    self_hex,
                    "dst":    iface_id,
                    "hops":   0,
                    "direct": True,
                    "kind":   "interface",
                })
    except Exception:
        pass

    return {
        "nodes":      list(nodes.values()),
        "edges":      edges,
        "interfaces": interfaces,
        "stats": {
            "node_count":      len(nodes),
            "path_count":      len(path_table),
            "interface_count": len(interfaces),
        },
    }


def _make_label(hex_id: str, identity, kind: str, storage=None) -> str:
    """Build a short human-readable label for a node.

    Preference order:
      1. Display name from the members table (name seen in channel member lists)
      2. Display name from LXMF announce app_data
      3. Identity hash prefix (matches what the rest of TrenchChat shows)
      4. Destination hash prefix (fallback when identity is unknown)
    """
    if kind == "self":
        return "This device"
    identity_hex: str | None = None
    if identity is not None:
        identity_hex = identity.hash.hex()

    # 1. Storage lookup — name from any channel's member list
    if storage is not None and identity_hex is not None:
        try:
            stored_name = storage.get_display_name_for_identity(identity_hex)
            if stored_name:
                return stored_name
        except Exception:
            pass

    # 2. LXMF announce app_data
    if identity is not None:
        try:
            raw = RNS.Identity.recall_app_data(
                RNS.Destination.hash(identity.hash, "lxmf", "delivery")
            )
            if raw:
                import msgpack
                parsed = msgpack.unpackb(raw, raw=False)
                app_data = parsed.get("display_name") or parsed.get("name")
                if isinstance(app_data, bytes):
                    app_data = app_data.decode(errors="replace")
                if app_data:
                    return str(app_data)
        except Exception:
            pass

    # 3 & 4. Hash prefix fallback
    fallback = identity_hex if identity_hex else hex_id
    return fallback[:12] + "…"


# ---------------------------------------------------------------------------
# Spring layout
# ---------------------------------------------------------------------------

class _SpringLayout:
    """Fruchterman-Reingold spring layout for a set of nodes."""

    def __init__(self, node_ids: list[str], width: float, height: float):
        self._ids = node_ids
        self._pos: dict[str, list[float]] = {}
        self._vel: dict[str, list[float]] = {}
        cx, cy = width / 2, height / 2
        for nid in node_ids:
            angle = random.uniform(0, 2 * math.pi)
            r = random.uniform(50, min(width, height) * 0.35)
            self._pos[nid] = [cx + r * math.cos(angle), cy + r * math.sin(angle)]
            self._vel[nid] = [0.0, 0.0]

    def pin(self, node_id: str, x: float, y: float) -> None:
        """Pin a node to a fixed position."""
        self._pos[node_id] = [x, y]
        self._vel[node_id] = [0.0, 0.0]

    def step(self, edges: list[dict], width: float, height: float,
             iterations: int = 1) -> None:
        for _ in range(iterations):
            forces: dict[str, list[float]] = {nid: [0.0, 0.0] for nid in self._ids}

            # Repulsion between all pairs
            ids = self._ids
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, b = ids[i], ids[j]
                    dx = self._pos[a][0] - self._pos[b][0]
                    dy = self._pos[a][1] - self._pos[b][1]
                    dist2 = dx * dx + dy * dy + 0.01
                    dist = math.sqrt(dist2)
                    f = _REPULSION / dist2
                    fx, fy = f * dx / dist, f * dy / dist
                    forces[a][0] += fx
                    forces[a][1] += fy
                    forces[b][0] -= fx
                    forces[b][1] -= fy

            # Attraction along edges
            for edge in edges:
                src, dst = edge["src"], edge["dst"]
                if src not in self._pos or dst not in self._pos:
                    continue
                dx = self._pos[dst][0] - self._pos[src][0]
                dy = self._pos[dst][1] - self._pos[src][1]
                dist = math.sqrt(dx * dx + dy * dy) + 0.01
                f = _ATTRACTION * (dist - _MIN_EDGE_LEN)
                fx, fy = f * dx / dist, f * dy / dist
                forces[src][0] += fx
                forces[src][1] += fy
                forces[dst][0] -= fx
                forces[dst][1] -= fy

            # Integrate
            for nid in self._ids:
                vx = (self._vel[nid][0] + forces[nid][0]) * _DAMPING
                vy = (self._vel[nid][1] + forces[nid][1]) * _DAMPING
                self._vel[nid] = [vx, vy]
                self._pos[nid][0] = max(30, min(width - 30,
                                                self._pos[nid][0] + vx))
                self._pos[nid][1] = max(30, min(height - 30,
                                                self._pos[nid][1] + vy))

    def positions(self) -> dict[str, tuple[float, float]]:
        return {nid: (p[0], p[1]) for nid, p in self._pos.items()}


# ---------------------------------------------------------------------------
# Canvas widget
# ---------------------------------------------------------------------------

class NetworkMapWidget(QWidget):
    """QPainter-based network graph canvas with pan and zoom."""

    def __init__(self, self_hex: str = "", parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.WheelFocus)

        self._self_hex = self_hex
        self._nodes: list[dict] = []
        self._edges: list[dict] = []
        self._layout: _SpringLayout | None = None
        self._positions: dict[str, tuple[float, float]] = {}

        # Pan / zoom state
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self._drag_start: QPointF | None = None
        self._drag_offset_start: QPointF | None = None

    def set_data(self, nodes: list[dict], edges: list[dict]) -> None:
        """Update topology data and rebuild the layout."""
        node_ids = [n["id"] for n in nodes]
        w, h = float(self.width() or 600), float(self.height() or 400)

        if self._layout is None or set(node_ids) != set(self._positions.keys()):
            self._layout = _SpringLayout(node_ids, w, h)
            if self._self_hex in node_ids:
                self._layout.pin(self._self_hex, w / 2, h / 2)

        self._layout.step(edges, w, h, iterations=_LAYOUT_ITERATIONS)
        self._positions = self._layout.positions()
        self._nodes = nodes
        self._edges = edges
        self.update()

    def load_data(self, data: dict, self_hex: str) -> None:
        """Load new topology data from a raw data dict (used by NetworkMapDialog)."""
        self._self_hex = self_hex
        self.set_data(data.get("nodes", []), data.get("edges", []))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), _COL_BG)

        if not self._nodes:
            painter.setPen(QPen(QColor("#555")))
            painter.setFont(QFont("monospace", 12))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "No network data — click Refresh")
            return

        painter.translate(self._offset)
        painter.scale(self._zoom, self._zoom)

        pos = self._positions

        # Draw edges
        for edge in self._edges:
            src, dst = edge["src"], edge["dst"]
            if src not in pos or dst not in pos:
                continue
            sx, sy = pos[src]
            dx, dy = pos[dst]
            is_iface_edge = edge.get("kind") == "interface"
            if is_iface_edge:
                col = _COL_EDGE_INTERFACE
                pen = QPen(col, 1.5)
                pen.setStyle(Qt.PenStyle.DotLine)
            elif edge.get("direct"):
                col = _COL_EDGE_DIRECT
                pen = QPen(col, 1.2)
            else:
                col = _COL_EDGE_MULTI
                pen = QPen(col, 1.2)
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(int(sx), int(sy), int(dx), int(dy))

            # Hop count label on multi-hop edges
            hops = edge.get("hops", 0)
            if hops > 1:
                mx, my = (sx + dx) / 2, (sy + dy) / 2
                painter.setFont(QFont("monospace", 7))
                painter.setPen(QPen(_COL_LABEL))
                painter.drawText(QRectF(mx - 12, my - 8, 24, 14),
                                 Qt.AlignmentFlag.AlignCenter, str(hops))

        # Draw nodes
        font_label = QFont("monospace", 8)
        for node in self._nodes:
            nid = node["id"]
            if nid not in pos:
                continue
            nx, ny = pos[nid]
            kind = node.get("kind", "unknown")
            col, r = _node_style(kind)

            painter.setBrush(QBrush(col))
            painter.setPen(QPen(col.darker(140), 1.5))

            if kind == "self":
                _draw_star(painter, nx, ny, r)
            elif kind == "interface":
                _draw_diamond(painter, nx, ny, r)
            elif kind == "transport":
                painter.drawRect(int(nx - r), int(ny - r), r * 2, r * 2)
            else:
                painter.drawEllipse(QRectF(nx - r, ny - r, r * 2, r * 2))

            # Label below node — wide enough for long interface names
            painter.setFont(font_label)
            painter.setPen(QPen(_COL_LABEL))
            label = node.get("label", nid[:8])
            label_w = 180
            label_h = 28
            painter.drawText(
                QRectF(nx - label_w / 2, ny + r + 3, label_w, label_h),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
                | Qt.TextFlag.TextWordWrap,
                label,
            )

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        factor = 1.12 if delta > 0 else 1 / 1.12
        self._zoom = max(0.2, min(5.0, self._zoom * factor))
        self.update()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()
            self._drag_offset_start = QPointF(self._offset)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_start is not None:
            delta = event.position() - self._drag_start
            self._offset = self._drag_offset_start + delta
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = None


def _node_style(kind: str) -> tuple[QColor, int]:
    if kind == "self":
        return _COL_SELF, _NODE_R_SELF
    if kind == "interface":
        return _COL_INTERFACE, _NODE_R_INTERFACE
    if kind == "transport":
        return _COL_TRANSPORT, _NODE_R_TRANSPORT
    if kind == "peer":
        return _COL_PEER, _NODE_R_PEER
    return _COL_UNKNOWN, _NODE_R_UNKNOWN


def _draw_diamond(painter: QPainter, cx: float, cy: float, r: int) -> None:
    """Draw a diamond (rotated square) centred at (cx, cy) with half-width r."""
    path = QPainterPath()
    path.moveTo(cx, cy - r)
    path.lineTo(cx + r, cy)
    path.lineTo(cx, cy + r)
    path.lineTo(cx - r, cy)
    path.closeSubpath()
    painter.drawPath(path)


def _draw_star(painter: QPainter, cx: float, cy: float, r: int) -> None:
    """Draw a 5-pointed star centred at (cx, cy) with outer radius r."""
    path = QPainterPath()
    inner = r * 0.45
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        radius = r if i % 2 == 0 else inner
        x = cx + radius * math.cos(angle)
        y = cy - radius * math.sin(angle)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    painter.drawPath(path)


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class NetworkMapDialog(QDialog):
    """Resizable dialog hosting the network map canvas."""

    def __init__(self, rns: RNS.Reticulum, self_hex: str, parent=None):
        super().__init__(parent)
        self._rns = rns
        self._self_hex = self_hex
        self._last_data: dict = {}

        self.setWindowTitle("Network Map")
        self.setMinimumSize(700, 500)
        self.resize(900, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Toolbar row
        toolbar = QHBoxLayout()

        self._refresh_btn = QPushButton("↻ Refresh")
        self._refresh_btn.setFixedWidth(90)
        self._refresh_btn.clicked.connect(self._refresh)
        toolbar.addWidget(self._refresh_btn)

        self._auto_cb = QCheckBox("Auto-refresh (10 s)")
        self._auto_cb.setChecked(True)
        self._auto_cb.toggled.connect(self._on_auto_toggled)
        toolbar.addWidget(self._auto_cb)

        toolbar.addStretch()

        self._legend = QLabel(
            "  ★ This device   ◆ Interface/Hub   ■ Transport node   ● Known peer   ○ Unknown"
        )
        self._legend.setStyleSheet("color: #888; font-size: 11px;")
        toolbar.addWidget(self._legend)

        layout.addLayout(toolbar)

        # Canvas
        self._canvas = NetworkMapWidget(self)
        layout.addWidget(self._canvas, 1)

        # Status bar
        self._status = QLabel("Loading…")
        self._status.setStyleSheet("color: #666; font-size: 11px; padding: 2px 4px;")
        layout.addWidget(self._status)

        # Auto-refresh timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(_AUTO_REFRESH_MS)

        # Initial load
        self._refresh()

    def _refresh(self) -> None:
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("…")
        try:
            data = gather_network_data(self._rns, self._self_hex)
            self._last_data = data
            self._canvas.load_data(data, self._self_hex)
            self._update_status(data)
        except Exception as e:
            RNS.log(f"TrenchChat [network map]: refresh error: {e}", RNS.LOG_WARNING)
            self._status.setText(f"Error: {e}")
        finally:
            self._refresh_btn.setEnabled(True)
            self._refresh_btn.setText("↻ Refresh")

    def _update_status(self, data: dict) -> None:
        stats = data.get("stats", {})
        ifaces = data.get("interfaces", [])
        iface_str = "  |  ".join(
            f"{i['name']} {'●' if i['status'] else '○'}"
            f"  ↓{_fmt_bytes(i['rxb'])}  ↑{_fmt_bytes(i['txb'])}"
            for i in ifaces
        )
        self._status.setText(
            f"Nodes: {stats.get('node_count', 0)}   "
            f"Paths: {stats.get('path_count', 0)}   "
            f"Interfaces: {stats.get('interface_count', 0)}"
            + (f"   |   {iface_str}" if iface_str else "")
        )

    def _on_auto_toggled(self, checked: bool) -> None:
        if checked:
            self._timer.start(_AUTO_REFRESH_MS)
        else:
            self._timer.stop()


def _fmt_bytes(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"
