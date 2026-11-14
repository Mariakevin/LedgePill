"""List/drawer cards and hardware LED / color helpers for Expanded."""

from __future__ import annotations

import time

from gi.repository import Gio, GLib, Gtk, Pango

from ledgepill.ui.panels import (
    _badge,
    _icon_btn,
    _section_card,
    _section_title,
    build_dock_tile,
)

_CMD_PREFIXES = (
    "$ ",
    "sudo ",
    "git ",
    "python",
    "npm ",
    "pnpm ",
    "cargo ",
    "docker ",
    "systemctl ",
    "yay ",
    "paru ",
    "pacman ",
)


def classify_clipboard_text(text: str) -> tuple[str, str]:
    """Return a short kind badge and size summary for a clipboard entry."""
    stripped = (text or "").strip()
    low = stripped.lower()
    if low.startswith(("https://", "http://")):
        kind = "URL"
    elif stripped.startswith(("/", "~/", "file://")):
        kind = "PATH"
    elif low.startswith(_CMD_PREFIXES):
        kind = "CMD"
    elif "\n" in stripped and any(
        tok in stripped for tok in ("{", "};", "def ", "class ", "import ", "=>")
    ):
        kind = "CODE"
    else:
        kind = "TEXT"
    chars = len(text or "")
    lines = stripped.count("\n") + 1 if stripped else 0
    meta = f"{lines} lines · {chars} chars" if lines > 1 else f"{chars} chars"
    return kind, meta


class _ExpandedListsMixin:
    def _build_notifications(self, parent: Gtk.Box):
        if self.notifier is None:
            return
        self.notif_group = _section_card(6)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = _section_title("Notifications")
        title.set_hexpand(True)
        self.notif_badge = _badge("0")
        clear = Gtk.Button(label="Clear")
        clear.add_css_class("list-clear-btn")
        clear.connect("clicked", self._on_clear_notifications)
        head.append(title)
        head.append(self.notif_badge)
        head.append(clear)
        self.notif_group.append(head)

        self.notif_list = Gtk.ListBox()
        self.notif_list.add_css_class("island-list")
        self.notif_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.notif_group.append(self.notif_list)
        parent.append(self.notif_group)

        self._refresh_notifications()
        self.notifier.connect("notified", lambda *_: self._refresh_notifications())
        if hasattr(self.notifier, "connect"):
            try:
                self.notifier.connect(
                    "history-changed", lambda *_: self._refresh_notifications()
                )
            except TypeError:
                pass

    def _on_clear_notifications(self, *_):
        if self.notifier is not None:
            self.notifier.clear()
        self._refresh_notifications()

    def _refresh_notifications(self):
        if not hasattr(self, "notif_list"):
            return
        history = getattr(self.notifier, "history", []) if self.notifier else []
        self.notif_group.set_visible(bool(history))
        self._sync_overview_empty()
        self.notif_badge.set_label(str(len(history)))
        shown = list(history[:12])
        snap = tuple(
            (
                idx,
                e.get("app", ""),
                e.get("category", ""),
                e.get("time", 0),
                e.get("summary", ""),
                e.get("body", "")[:120],
            )
            for idx, e in enumerate(shown)
        )
        if snap == self._notif_snap:
            return
        self._notif_snap = snap
        while (row := self.notif_list.get_row_at_index(0)) is not None:
            self.notif_list.remove(row)
        for idx, entry in enumerate(shown):
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            cat = entry.get("category", "")
            app_txt = entry.get("app", "")
            if cat and cat != "general":
                app_txt = f"{app_txt} · {cat}" if app_txt else cat
            app_lbl = Gtk.Label(label=app_txt, xalign=0, hexpand=True)
            app_lbl.add_css_class("tile-sub")
            app_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            ts = entry.get("time", 0)
            time_lbl = Gtk.Label(
                label=time.strftime("%H:%M", time.localtime(ts)) if ts else ""
            )
            time_lbl.add_css_class("tile-sub")
            top.append(app_lbl)
            top.append(time_lbl)

            sum_lbl = Gtk.Label(label=entry.get("summary", ""), xalign=0)
            sum_lbl.add_css_class("list-title")
            sum_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(top)
            box.append(sum_lbl)
            body = entry.get("body", "")
            if body:
                body_lbl = Gtk.Label(label=" ".join(body.splitlines()), xalign=0)
                body_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                body_lbl.add_css_class("list-sub")
                box.append(body_lbl)

            rm = _icon_btn("window-close-symbolic", "Dismiss notification")
            rm.set_valign(Gtk.Align.CENTER)
            rm.connect(
                "clicked",
                lambda _b, i=idx: (
                    hasattr(self.notifier, "dismiss") and self.notifier.dismiss(i)
                ),
            )
            outer.append(box)
            outer.append(rm)
            row.set_child(outer)
            self.notif_list.append(row)

    def _build_clipboard(self, parent: Gtk.Box, drawer_parent: Gtk.Box | None = None):
        if self.clipboard is None:
            return
        self.clip_preview_lbl = Gtk.Label(label="Empty", xalign=0, hexpand=True)
        self.clip_preview_lbl.add_css_class("tile-sub")
        self.clip_copied_badge = Gtk.Label(label="Copied ✓")
        self.clip_copied_badge.add_css_class("clip-copied")
        self.clip_copied_badge.set_visible(False)
        self.clip_count_badge = _badge("0")
        self.clip_chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")

        self.clip_toggle_btn = build_dock_tile(
            "edit-paste-symbolic",
            "Clipboard",
            self.clip_count_badge,
            self.clip_preview_lbl,
            chevron=self.clip_chevron,
            extra_badge=self.clip_copied_badge,
            tooltip="Click to open or close clipboard history",
        )
        self.clip_toggle_btn.connect("clicked", self._toggle_clipboard)
        self.clip_group = self.clip_toggle_btn
        parent.append(self.clip_group)

        self.clip_revealer = Gtk.Revealer()
        self.clip_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.clip_revealer.set_transition_duration(180)
        self.clip_revealer.set_reveal_child(False)

        drawer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.clip_search = Gtk.SearchEntry(placeholder_text="Search clipboard…")
        self.clip_search.add_css_class("island-search")
        self.clip_search.set_hexpand(True)
        self.clip_search.connect("search-changed", lambda *_: self._refresh_clipboard())
        self.clip_clear_btn = Gtk.Button(label="Clear")
        self.clip_clear_btn.add_css_class("list-clear-btn")
        self.clip_clear_btn.connect("clicked", self._on_clear_clipboard)
        toolbar.append(self.clip_search)
        toolbar.append(self.clip_clear_btn)
        drawer.append(toolbar)

        self.clip_list = Gtk.ListBox()
        self.clip_list.add_css_class("island-list")
        self.clip_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.clip_list.set_activate_on_single_click(True)
        self.clip_list.connect("row-activated", self._on_clip_activate)
        self._clip_visible_indices: list[int] = []
        drawer.append(self.clip_list)

        self.clip_empty = Gtk.Label(label="Clipboard is empty", xalign=0)
        self.clip_empty.add_css_class("empty-hint")
        self.clip_empty.set_visible(False)
        drawer.append(self.clip_empty)

        self.clip_more_btn = Gtk.Button(label="Show all")
        self.clip_more_btn.add_css_class("panel-row-btn")
        self.clip_more_btn.connect("clicked", self._toggle_clip_show_all)
        self.clip_more_btn.set_visible(False)
        drawer.append(self.clip_more_btn)

        self.clip_revealer.set_child(drawer)
        (drawer_parent if drawer_parent is not None else parent).append(
            self.clip_revealer
        )

        self._refresh_clipboard()
        self.clipboard.connect("changed", lambda *_: self._refresh_clipboard())

    def _set_clipboard_open(self, opened: bool):
        self._clip_open = bool(opened)
        if hasattr(self, "clip_revealer"):
            self.clip_revealer.set_reveal_child(self._clip_open)
        if hasattr(self, "clip_toggle_btn"):
            if self._clip_open:
                self.clip_toggle_btn.add_css_class("dock-tile--active")
            else:
                self.clip_toggle_btn.remove_css_class("dock-tile--active")
        if hasattr(self, "clip_chevron"):
            self.clip_chevron.set_from_icon_name(
                "pan-up-symbolic" if self._clip_open else "pan-down-symbolic"
            )
        self._on_dock_toggle("clipboard", self._clip_open)
        if self._clip_open and self.clipboard is not None:
            if hasattr(self.clipboard, "sync_external"):
                self.clipboard.sync_external()
            self._refresh_clipboard()

    def _toggle_clipboard(self, *_):
        self._set_clipboard_open(not self._clip_open)

    def _toggle_clip_show_all(self, *_):
        self._clip_show_all = not self._clip_show_all
        self._refresh_clipboard()

    def _on_clear_clipboard(self, *_):
        self._clip_show_all = False
        if hasattr(self, "clip_search") and self.clip_search.get_text():
            self.clip_search.set_text("")
        if self.clipboard is not None:
            history = getattr(self.clipboard, "history", [])
            pinned_list = getattr(self.clipboard, "pinned", [])
            only_pinned = (
                bool(history)
                and len(history) <= len(pinned_list)
                and all(item in pinned_list for item in history)
            )
            self.clipboard.clear(include_pinned=only_pinned)
        self._refresh_clipboard()

    def _refresh_clipboard(self):
        if not hasattr(self, "clip_list"):
            return
        history = getattr(self.clipboard, "history", []) if self.clipboard else []
        pinned_list = getattr(self.clipboard, "pinned", []) if self.clipboard else []
        self.clip_group.set_visible(self.clipboard is not None)
        self._sync_overview_empty()
        if hasattr(self, "clip_preview_lbl"):
            if history:
                first_line = " ".join(history[0][:60].split()) or "(whitespace)"
                self.clip_preview_lbl.set_label(first_line)
            else:
                self.clip_preview_lbl.set_label("Empty")
        pin_count = sum(1 for t in history if t in pinned_list)
        if hasattr(self, "clip_count_badge"):
            self.clip_count_badge.set_label(
                f"{len(history)}★" if pin_count else str(len(history))
            )
            self.clip_count_badge.set_tooltip_text(
                f"{len(history)} items ({pin_count} pinned)"
                if pin_count
                else f"{len(history)} clipboard items"
            )
        if hasattr(self, "clip_clear_btn"):
            has_unpinned = any(t not in pinned_list for t in history)
            self.clip_clear_btn.set_label("Clear" if has_unpinned else "Clear all")
            self.clip_clear_btn.set_tooltip_text(
                "Clear unpinned items" if has_unpinned else "Clear all items"
            )
            self.clip_clear_btn.set_sensitive(bool(history))

        query = (
            self.clip_search.get_text().strip().casefold()
            if hasattr(self, "clip_search")
            else ""
        )
        matches = (
            self.clipboard.search(query)
            if self.clipboard is not None and hasattr(self.clipboard, "search")
            else list(enumerate(history))
        )
        limit = len(matches) if self._clip_show_all else 12
        shown = matches[:limit]
        self.clip_empty.set_label(
            "Clipboard is empty" if not history else "No matching clipboard items"
        )
        self.clip_empty.set_visible(not matches)
        self.clip_list.set_visible(bool(shown))
        if hasattr(self, "clip_more_btn"):
            has_more = len(matches) > 12
            self.clip_more_btn.set_visible(has_more)
            if has_more:
                self.clip_more_btn.set_label(
                    "Show fewer"
                    if self._clip_show_all
                    else f"Show all ({len(matches)})"
                )

        snap = (
            tuple((idx, txt[:160], txt in pinned_list) for idx, txt in shown),
            self._clip_show_all,
            len(matches),
        )
        if snap == self._clip_snap:
            return
        self._clip_snap = snap
        while (row := self.clip_list.get_row_at_index(0)) is not None:
            self.clip_list.remove(row)
        self._clip_visible_indices = []
        for idx, text in shown:
            self._clip_visible_indices.append(idx)
            is_pin = text in pinned_list
            kind, meta = classify_clipboard_text(text)

            row = Gtk.ListBoxRow()
            card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            card.add_css_class("clip-row")
            if is_pin:
                card.add_css_class("clip-row--pinned")

            body = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True
            )
            preview_txt = " ".join(text[:240].split())[:140] or "(whitespace)"
            lbl = Gtk.Label(label=preview_txt, xalign=0)
            lbl.add_css_class("clip-preview")
            lbl.set_lines(2)
            lbl.set_wrap(True)
            lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_max_width_chars(42)

            meta_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            if is_pin:
                p_badge = Gtk.Label(label="PINNED")
                p_badge.add_css_class("clip-badge")
                p_badge.add_css_class("clip-badge--pinned")
                meta_row.append(p_badge)
            k_badge = Gtk.Label(label=kind)
            k_badge.add_css_class("clip-badge")
            sz_lbl = Gtk.Label(label=meta, xalign=0)
            sz_lbl.add_css_class("tile-sub")
            meta_row.append(k_badge)
            meta_row.append(sz_lbl)

            body.append(lbl)
            body.append(meta_row)

            acts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            acts.set_valign(Gtk.Align.CENTER)
            cp_b = _icon_btn("edit-copy-symbolic", "Copy to clipboard")
            cp_b.connect("clicked", lambda _b, i=idx: self._copy_clip_index(i))

            pin_b = _icon_btn(
                "starred-symbolic" if is_pin else "non-starred-symbolic",
                "Unpin" if is_pin else "Pin to top",
            )
            if is_pin:
                pin_b.add_css_class("clip-act-btn--active")
            pin_b.connect(
                "clicked",
                lambda _b, i=idx: (
                    hasattr(self.clipboard, "toggle_pin")
                    and self.clipboard.toggle_pin(i)
                ),
            )

            rm = _icon_btn("window-close-symbolic", "Remove from history")
            rm.connect(
                "clicked",
                lambda _b, i=idx: (
                    hasattr(self.clipboard, "remove") and self.clipboard.remove(i)
                ),
            )
            acts.append(cp_b)
            acts.append(pin_b)
            acts.append(rm)

            card.append(body)
            card.append(acts)
            row.set_tooltip_text(text[:300])
            row.set_child(card)
            self.clip_list.append(row)

    def _copy_clip_index(self, idx: int):
        if self.clipboard is None:
            return
        history = getattr(self.clipboard, "history", [])
        if not (0 <= idx < len(history)):
            return
        if hasattr(self.clipboard, "copy_item"):
            self.clipboard.copy_item(idx)
        else:
            text = history[idx]
            cb = self.get_clipboard() or (
                self.get_display().get_clipboard() if self.get_display() else None
            )
            if cb is not None:
                cb.set(text)
        if hasattr(self, "clip_copied_badge"):
            self.clip_copied_badge.set_visible(True)
            if hasattr(self, "clip_count_badge"):
                self.clip_count_badge.set_visible(False)
        if self._clip_feedback_timer:
            GLib.source_remove(self._clip_feedback_timer)
        self._clip_feedback_timer = GLib.timeout_add(1200, self._reset_clip_title)

    def _on_clip_activate(self, _list, row):
        if self.clipboard is None:
            return
        vis_idx = row.get_index()
        idx = (
            self._clip_visible_indices[vis_idx]
            if 0 <= vis_idx < len(self._clip_visible_indices)
            else vis_idx
        )
        self._copy_clip_index(idx)

    def _reset_clip_title(self):
        self._clip_feedback_timer = 0
        if hasattr(self, "clip_copied_badge"):
            self.clip_copied_badge.set_visible(False)
        if hasattr(self, "clip_count_badge"):
            self.clip_count_badge.set_visible(True)
        return False

    def _build_feeds(self, parent: Gtk.Box):
        if self.feeds is None:
            return
        self.feed_group = _section_card(6)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        heading = _section_title("Information feeds")
        heading.set_hexpand(True)
        self.feed_badge = _badge("0")
        head.append(heading)
        head.append(self.feed_badge)
        self.feed_group.append(head)
        self.feed_list = Gtk.ListBox()
        self.feed_list.add_css_class("island-list")
        self.feed_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.feed_list.set_activate_on_single_click(True)
        self.feed_list.connect("row-activated", self._on_feed_activate)
        self.feed_group.append(self.feed_list)
        self._feed_urls = []
        parent.append(self.feed_group)
        self.feed_group.set_visible(False)
        self.feeds.connect("changed", lambda *_: self._refresh_feeds())

    def _refresh_feeds(self):
        if not hasattr(self, "feed_list"):
            return
        while (row := self.feed_list.get_row_at_index(0)) is not None:
            self.feed_list.remove(row)
        entries = self.feeds.items if self.feeds is not None else []
        shown = list(entries[:12])
        self._feed_urls = [item.get("url", "") for item in shown]
        self.feed_group.set_visible(bool(entries))
        self._sync_overview_empty()
        if hasattr(self, "feed_badge"):
            self.feed_badge.set_label(str(len(entries)))
        for item in shown:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            source = Gtk.Label(label=item.get("source", "Feed"), xalign=0)
            source.add_css_class("tile-sub")
            title = Gtk.Label(label=item.get("title", ""), xalign=0)
            title.add_css_class("list-title")
            title.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(source)
            box.append(title)
            summary = item.get("summary", "").strip()
            if summary:
                preview = Gtk.Label(label=" ".join(summary.splitlines()), xalign=0)
                preview.add_css_class("list-sub")
                preview.set_ellipsize(Pango.EllipsizeMode.END)
                preview.set_max_width_chars(46)
                box.append(preview)
            row = Gtk.ListBoxRow()
            row.set_child(box)
            row.set_activatable(bool(item.get("url")))
            self.feed_list.append(row)

    def _on_feed_activate(self, _list, row):
        index = row.get_index()
        url = self._feed_urls[index] if 0 <= index < len(self._feed_urls) else ""
        if url.startswith(("https://", "http://")):
            try:
                Gio.AppInfo.launch_default_for_uri(url, None)
            except GLib.Error:
                pass


def _label(css: str, width: int) -> Gtk.Label:
    lbl = Gtk.Label(xalign=0)
    lbl.add_css_class(css)
    lbl.set_ellipsize(Pango.EllipsizeMode.END)
    lbl.set_max_width_chars(width)
    return lbl


def _media_btn(icon: str, main: bool = False) -> Gtk.Button:
    btn = Gtk.Button()
    btn.set_icon_name(icon)
    btn.add_css_class("media-btn")
    if main:
        btn.add_css_class("media-btn--main")
    return btn


def _chip(icon: str) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
    box.add_css_class("chip")
    img = Gtk.Image.new_from_icon_name(icon)
    img.set_pixel_size(13)
    lbl = Gtk.Label()
    lbl.add_css_class("net-chip")
    lbl.set_ellipsize(Pango.EllipsizeMode.END)
    lbl.set_max_width_chars(16)
    box.append(img)
    box.append(lbl)
    return box


def _set_chip(chip: Gtk.Box, icon: str, text: str):
    img = chip.get_first_child()
    lbl = chip.get_last_child()
    img.set_from_icon_name(icon)
    lbl.set_label(text)


def _fmt_us(us: int) -> str:
    s = max(0, us // 1_000_000)
    return f"{s // 60}:{s % 60:02d}"


def _hex_rgb(hex_color: str):
    cleaned = (hex_color or "").strip().lstrip("#")
    if len(cleaned) == 3:
        cleaned = "".join(c * 2 for c in cleaned)
    if len(cleaned) != 6:
        return (1.0, 0.62, 0.04)
    try:
        return tuple(int(cleaned[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (1.0, 0.62, 0.04)
