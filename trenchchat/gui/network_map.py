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

import RNS

from trenchchat.core.link_quality import LinkQuality
from trenchchat.core.network_data import gather_network_data

from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QFontMetrics, QPainterPath,
    QWheelEvent, QMouseEvent,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget,
    QPushButton, QLabel, QCheckBox, QSizePolicy,
)

# --- constants ---

_AUTO_REFRESH_MS   = 10_000   # 10 s
_LAYOUT_ITERATIONS    = 80       # spring-layout steps per refresh
_REPULSION_BASE       = 8_000.0  # base node-node repulsion constant (scaled by node count)
_ATTRACTION           = 0.04     # edge spring constant
_DAMPING              = 0.85     # velocity damping per step
_MIN_EDGE_LEN_BASE    = 120.0    # base natural edge length (pixels, scaled by node count)
_MIN_EDGE_LEN_MAX     = 350.0    # cap on scaled edge length
_LAYOUT_MARGIN        = 60       # initial placement margin (px); no boundary clamp during layout

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

# Edge / ring colours by link quality tier — bright enough to read on dark bg
_COL_QUALITY = {
    LinkQuality.EXCELLENT: QColor("#3ddc3d"),   # vivid green
    LinkQuality.GOOD:      QColor("#e8e83a"),   # vivid yellow
    LinkQuality.FAIR:      QColor("#e8963a"),   # vivid orange
    LinkQuality.POOR:      QColor("#e83a3a"),   # vivid red
    LinkQuality.UNKNOWN:   QColor("#666666"),   # mid grey
}

_NODE_R_SELF      = 14
_NODE_R_INTERFACE = 12
_NODE_R_TRANSPORT = 11
_NODE_R_PEER      = 9
_NODE_R_UNKNOWN   = 7


# ---------------------------------------------------------------------------
# Spring layout
# ---------------------------------------------------------------------------

class _SpringLayout:
    """Fruchterman-Reingold spring layout for a set of nodes.

    Repulsion and minimum edge length are scaled by the caller based on node
    count so the layout spreads proportionally for dense graphs.  There is no
    hard boundary clamp — the auto-fit zoom in NetworkMapWidget handles viewport
    fitting, so nodes are free to occupy whatever space the forces require.
    """

    def __init__(self, node_ids: list[str], width: float, height: float,
                 repulsion: float = _REPULSION_BASE,
                 min_edge_len: float = _MIN_EDGE_LEN_BASE):
        self._ids = node_ids
        self._repulsion = repulsion
        self._min_edge_len = min_edge_len
        self._pos: dict[str, list[float]] = {}
        self._vel: dict[str, list[float]] = {}
        cx, cy = width / 2, height / 2
        spread = max(min(width, height) * 0.4, min_edge_len * len(node_ids) ** 0.5 / 4)
        for nid in node_ids:
            angle = random.uniform(0, 2 * math.pi)
            r = random.uniform(_LAYOUT_MARGIN, spread)
            self._pos[nid] = [cx + r * math.cos(angle), cy + r * math.sin(angle)]
            self._vel[nid] = [0.0, 0.0]

    def pin(self, node_id: str, x: float, y: float) -> None:
        """Pin a node to a fixed position."""
        self._pos[node_id] = [x, y]
        self._vel[node_id] = [0.0, 0.0]

    def step(self, edges: list[dict], width: float, height: float,
             iterations: int = 1) -> None:
        """Advance the layout by the given number of integration steps."""
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
                    f = self._repulsion / dist2
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
                f = _ATTRACTION * (dist - self._min_edge_len)
                fx, fy = f * dx / dist, f * dy / dist
                forces[src][0] += fx
                forces[src][1] += fy
                forces[dst][0] -= fx
                forces[dst][1] -= fy

            # Integrate — no boundary clamp; auto-fit zoom handles viewport
            for nid in self._ids:
                vx = (self._vel[nid][0] + forces[nid][0]) * _DAMPING
                vy = (self._vel[nid][1] + forces[nid][1]) * _DAMPING
                self._vel[nid] = [vx, vy]
                self._pos[nid][0] += vx
                self._pos[nid][1] += vy

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
        # When not None, only peer/unknown nodes whose ID is in this set are shown.
        self._peer_filter: set[str] | None = None

        # Pan / zoom state
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self._drag_start: QPointF | None = None
        self._drag_offset_start: QPointF | None = None
        # True once the first auto-fit has been applied; subsequent refreshes
        # leave the user's zoom/pan untouched.
        self._fitted = False

    def set_data(self, nodes: list[dict], edges: list[dict]) -> None:
        """Update topology data and rebuild the layout."""
        node_ids = [n["id"] for n in nodes]
        w, h = float(self.width() or 600), float(self.height() or 400)

        n = max(len(node_ids), 1)
        repulsion = _REPULSION_BASE * (1.0 + n / 15.0)
        min_edge_len = min(_MIN_EDGE_LEN_BASE + n * 4.0, _MIN_EDGE_LEN_MAX)

        is_new_layout = self._layout is None or set(node_ids) != set(self._positions.keys())
        if is_new_layout:
            self._layout = _SpringLayout(node_ids, w, h,
                                         repulsion=repulsion,
                                         min_edge_len=min_edge_len)
            if self._self_hex in node_ids:
                self._layout.pin(self._self_hex, w / 2, h / 2)
            self._fitted = False
        else:
            # Update scaling if node count changed significantly
            self._layout._repulsion = repulsion
            self._layout._min_edge_len = min_edge_len

        self._layout.step(edges, w, h, iterations=_LAYOUT_ITERATIONS)
        self._positions = self._layout.positions()
        self._nodes = nodes
        self._edges = edges
        if not self._fitted:
            self._auto_fit()
            self._fitted = True
        self.update()

    def load_data(self, data: dict, self_hex: str) -> None:
        """Load new topology data from a raw data dict (used by NetworkMapDialog)."""
        self._self_hex = self_hex
        self.set_data(data.get("nodes", []), data.get("edges", []))

    def set_peer_filter(self, peer_identity_hexes: set[str] | None) -> None:
        """Restrict visible nodes to TrenchChat peers only.

        When peer_identity_hexes is a set of identity hashes, only nodes whose
        kind is 'self', 'interface', or 'transport' — plus peer/unknown nodes
        whose identity_hex appears in peer_identity_hexes — are drawn.
        Pass None to disable the filter and show all nodes.
        """
        self._peer_filter = peer_identity_hexes
        self.update()

    def _auto_fit(self) -> None:
        """Adjust zoom and pan so all nodes fit within the visible viewport.

        Called automatically after each layout step in set_data.  The user can
        still pan and zoom freely after the fit is applied.
        """
        if not self._positions:
            return
        xs = [p[0] for p in self._positions.values()]
        ys = [p[1] for p in self._positions.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        graph_w = max(max_x - min_x, 1.0)
        graph_h = max(max_y - min_y, 1.0)
        pad = 80
        w = self.width() or 600
        h = self.height() or 400
        zoom_x = (w - pad * 2) / graph_w
        zoom_y = (h - pad * 2) / graph_h
        self._zoom = max(0.2, min(2.0, min(zoom_x, zoom_y)))
        cx = (min_x + max_x) / 2
        cy = (min_y + max_y) / 2
        self._offset = QPointF(
            w / 2 - cx * self._zoom,
            h / 2 - cy * self._zoom,
        )

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

        # When a peer filter is active, build the set of visible node IDs.
        # Pass 1: always include self and interface nodes, plus peer/unknown
        #         nodes whose identity_hex is in the filter set.
        # Pass 2: include transport nodes only when they lie on a path to a
        #         visible peer (i.e. they have an edge whose dst is already
        #         visible). This keeps relay hops that connect our device to
        #         a TrenchChat peer while hiding unrelated transport nodes.
        if self._peer_filter is not None:
            node_kind: dict[str, str] = {n["id"]: n.get("kind", "unknown")
                                         for n in self._nodes}
            visible_ids: set[str] | None = set()
            # Pass 1: self, interfaces, and TrenchChat peers.
            for node in self._nodes:
                nid = node["id"]
                kind = node.get("kind", "unknown")
                if kind in ("self", "interface"):
                    visible_ids.add(nid)
                elif kind != "transport" and node.get("identity_hex") in self._peer_filter:
                    visible_ids.add(nid)
            # Pass 2: transport nodes that lie on a path to a visible node.
            for edge in self._edges:
                if (edge["dst"] in visible_ids
                        and edge["src"] not in visible_ids
                        and node_kind.get(edge["src"]) == "transport"):
                    visible_ids.add(edge["src"])
        else:
            visible_ids = None  # no filter — all nodes visible

        # Draw edges — coloured by link quality tier
        for edge in self._edges:
            src, dst = edge["src"], edge["dst"]
            if src not in pos or dst not in pos:
                continue
            # Skip edges where either endpoint is filtered out
            if visible_ids is not None and (src not in visible_ids or dst not in visible_ids):
                continue
            sx, sy = pos[src]
            dx, dy = pos[dst]
            is_iface_edge = edge.get("kind") == "interface"
            quality = LinkQuality(edge.get("quality", int(LinkQuality.UNKNOWN)))
            q_col = _COL_QUALITY[quality]

            # Glow pass — wider, semi-transparent line underneath
            glow_col = QColor(q_col)
            glow_col.setAlpha(60)
            glow_pen = QPen(glow_col, 6.0)
            painter.setPen(glow_pen)
            painter.drawLine(int(sx), int(sy), int(dx), int(dy))

            # Main line
            if is_iface_edge:
                pen = QPen(q_col, 1.5)
                pen.setStyle(Qt.PenStyle.DotLine)
            elif edge.get("direct"):
                pen = QPen(q_col, 2.5)
            else:
                pen = QPen(q_col, 2.0)
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
        fm = QFontMetrics(font_label)
        _label_max_w = 160   # hard cap to prevent very long labels from overlapping
        _label_h = 28
        for node in self._nodes:
            nid = node["id"]
            if nid not in pos:
                continue
            if visible_ids is not None and nid not in visible_ids:
                continue
            nx, ny = pos[nid]
            kind = node.get("kind", "unknown")
            col, r = _node_style(kind)

            # Quality ring around peer/transport/interface nodes
            if kind not in ("self",):
                quality = LinkQuality(node.get("quality", int(LinkQuality.UNKNOWN)))
                ring_col = _COL_QUALITY[quality]
                ring_pen = QPen(ring_col, 2.0)
                painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                painter.setPen(ring_pen)
                rr = r + 4
                painter.drawEllipse(QRectF(nx - rr, ny - rr, rr * 2, rr * 2))

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

            # Label below node — width fitted to the actual text, capped at _label_max_w
            painter.setFont(font_label)
            painter.setPen(QPen(_COL_LABEL))
            label = node.get("label", nid[:8])
            text_w = min(fm.horizontalAdvance(label) + 8, _label_max_w)
            label_w = max(text_w, r * 2)
            painter.drawText(
                QRectF(nx - label_w / 2, ny + r + 3, label_w, _label_h),
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
            "      "
            "<span style='color:#3ddc3d'>━</span> Excellent  "
            "<span style='color:#e8e83a'>━</span> Good  "
            "<span style='color:#e8963a'>━</span> Fair  "
            "<span style='color:#e83a3a'>━</span> Poor"
        )
        self._legend.setTextFormat(Qt.TextFormat.RichText)
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
