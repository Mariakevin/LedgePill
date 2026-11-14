Name:           ledge-pill
Version:        1.0.0
Release:        1%{?dist}
Summary:        A Dynamic Island for your Linux desktop
License:        MIT
URL:            https://github.com/Mariakevin/ledge-pill-next
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz
BuildArch:      noarch
Requires:       python3 >= 3.11, python3-gobject, python3-cairo, gtk4, gtk4-layer-shell, libadwaita, pulseaudio-utils
Recommends:     alsa-utils, NetworkManager, upower, cava

%description
A fluid, always-on-top island at the top of your screen: music with full
controls and album art, volume OSD, battery, notifications and a pomodoro
timer. Native GTK4, event-driven, light on your battery.

%prep
%autosetup -n %{name}-%{version}

%install
install -d %{buildroot}/usr/lib/%{name}
cp -r ledgepill %{buildroot}/usr/lib/%{name}/
install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/ledge-pill <<'EOF'
#!/bin/sh
PYTHONPATH=/usr/lib/ledge-pill exec python3 -m ledgepill "$@"
EOF
chmod 755 %{buildroot}%{_bindir}/ledge-pill
install -Dm644 data/ledge-pill.desktop %{buildroot}%{_datadir}/applications/ledge-pill.desktop
install -Dm644 data/ledge-pill.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/ledge-pill.svg
install -Dm644 data/ledge-pill.service %{buildroot}/usr/lib/systemd/user/ledge-pill.service
install -Dm644 data/ledge-pill-mic-led.conf %{buildroot}/usr/lib/modules-load.d/90-ledge-pill-mic-led.conf
install -Dm644 data/99-ledge-pill-mic-led.rules %{buildroot}/usr/lib/udev/rules.d/99-ledge-pill-mic-led.rules

%post
if [ -S /run/udev/control ] && command -v udevadm >/dev/null 2>&1; then
  if command -v modprobe >/dev/null 2>&1; then
    modprobe snd_ctl_led || echo "ledge-pill: snd_ctl_led will be retried at boot" >&2
  fi
  udevadm control --reload-rules
  udevadm trigger --action=add --subsystem-match=leds --sysname-match='hda::micmute'
fi

%files
/usr/lib/%{name}
%{_bindir}/ledge-pill
%{_datadir}/applications/ledge-pill.desktop
%{_datadir}/icons/hicolor/scalable/apps/ledge-pill.svg
/usr/lib/systemd/user/ledge-pill.service
/usr/lib/modules-load.d/90-ledge-pill-mic-led.conf
/usr/lib/udev/rules.d/99-ledge-pill-mic-led.rules
%license LICENSE

%changelog
* Tue Sep 02 2026 Marco Zorn <m@zorn.it> - 1.7.0-1
- Claude usage pill face: time + 5h session limit via a statusLine hook
* Fri Aug 15 2026 Marco Zorn <m@zorn.it> - 1.6.1-1
- Android: fix island invisible on notched phones (cutout centering feedback loop)

* Fri Aug 15 2026 Marco Zorn <m@zorn.it> - 1.5.0-1
- Android: camera-safe pill slots, notification mirroring, full settings, accents

* Thu Aug 14 2026 Marco Zorn <m@zorn.it> - 1.4.0-1
- Android overhaul: cutout overlap, signed apk, faces, lyrics, settings, icon

* Thu Aug 14 2026 Marco Zorn <m@zorn.it> - 1.3.0-1
- Pill face deck with click/swipe cycling, synced lyrics, opacity, cava mono fix

* Thu Aug 14 2026 Marco Zorn <m@zorn.it> - 1.2.0-1
- Audio-reactive EQ via cava, pill faces, click-to-close, browser demo

* Thu Aug 14 2026 Marco Zorn <m@zorn.it> - 1.1.0-1
- Settings window, panel overlap, quick toggles, bluetooth, weather, system stats

* Thu Aug 14 2026 Marco Zorn <m@zorn.it> - 1.0.0-1
- Initial release
