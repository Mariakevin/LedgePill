"""Shared compact Bento panel helpers."""

from __future__ import annotations

from gi.repository import Gtk, Pango


def _section_title(text: str) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=0)
    lbl.add_css_class("section-title")
    return lbl


def _section_card(spacing: int = 8) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    box.add_css_class("section-card")
    return box


def _badge(text: str = "", accent: bool = False) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("section-badge")
    if accent:
        lbl.add_css_class("section-badge--accent")
    lbl.set_valign(Gtk.Align.CENTER)
    return lbl


def _icon_btn(icon_name: str, tooltip: str = "") -> Gtk.Button:
    btn = Gtk.Button()
    btn.set_icon_name(icon_name)
    btn.add_css_class("clip-act-btn")
    if tooltip:
        btn.set_tooltip_text(tooltip)
    return btn


def _scroll_box(child: Gtk.Widget, max_h: int = 150) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    sw.set_propagate_natural_height(True)
    sw.set_max_content_height(max_h)
    sw.set_child(child)
    return sw


def build_dock_tile(
    icon_name: str,
    title_text: str,
    count_badge: Gtk.Widget,
    sub_lbl: Gtk.Label,
    chevron: Gtk.Image | None = None,
    extra_badge: Gtk.Widget | None = None,
    tooltip: str = "",
) -> Gtk.Button:
    """Create a compact two-line Bento dock toggle tile."""
    btn = Gtk.Button()
    btn.add_css_class("dock-tile")
    if tooltip:
        btn.set_tooltip_text(tooltip)

    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    ico = Gtk.Image.new_from_icon_name(icon_name)
    ico.set_pixel_size(13)
    ico.add_css_class("dock-tile-icon")
    title = Gtk.Label(label=title_text, xalign=0, hexpand=True)
    title.add_css_class("list-title")
    title.set_ellipsize(Pango.EllipsizeMode.END)
    title.set_width_chars(1)
    top.append(ico)
    top.append(title)
    if extra_badge is not None:
        top.append(extra_badge)
    top.append(count_badge)

    bot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
    sub_lbl.set_hexpand(True)
    sub_lbl.set_xalign(0)
    sub_lbl.set_ellipsize(Pango.EllipsizeMode.END)
    sub_lbl.set_width_chars(1)
    sub_lbl.set_max_width_chars(10)
    bot.append(sub_lbl)
    if chevron is not None:
        chevron.set_pixel_size(11)
        bot.append(chevron)

    vbox.append(top)
    vbox.append(bot)
    btn.set_child(vbox)
    return btn
