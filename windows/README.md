# Lost Island for Windows

Windows companion to Lost Island, the Dynamic Island for desktop OSes.
A small always-on-top capsule at the top of the screen: a clock when idle,
"Artist — Title" with an accent dot while music plays. Clicking it morphs
the pill into a card with album art and previous / play-pause / next
controls; with no media session it shows a large clock and date instead.

Media comes from the Global System Media Transport Controls — the same
source as the Windows volume flyout — so Spotify, browsers and most players
work out of the box. Scope is deliberately smaller than the Linux flagship:
no peek notifications, tiles or pomodoro here.

## Build

    dotnet publish windows/LostIsland.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true

The release executable bundles the .NET runtime and runs without a separate
runtime installation. The .NET 8 SDK is needed only to build from source.
Release binaries target x64 and are unsigned, so Windows SmartScreen may show
a warning. Download them only from the project's official Releases page.

## Run at startup

Press Win+R, type `shell:startup`, and drop a shortcut to `LostIsland.exe`
(found under `windows/bin/Release/net8.0-windows10.0.19041.0/win-x64/publish/`)
into the folder that opens.
