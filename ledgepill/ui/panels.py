"""Secondary panels for the expanded card (re-export facade)."""

from __future__ import annotations

from ledgepill.ui.panel_shared import (
    _badge,
    _icon_btn,
    _scroll_box,
    _section_card,
    _section_title,
    build_dock_tile,
)
from ledgepill.ui.panels_system import ConnectivityPanel
from ledgepill.ui.panels_workspace import (
    AiDevPanel,
    CameraMirror,
    DesktopPanel,
)

__all__ = [
    "AiDevPanel",
    "CameraMirror",
    "ConnectivityPanel",
    "DesktopPanel",
    "_badge",
    "_icon_btn",
    "_scroll_box",
    "_section_card",
    "_section_title",
    "build_dock_tile",
]
