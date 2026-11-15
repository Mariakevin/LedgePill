"""Fail CI when native platform version metadata drifts from pyproject.toml."""

from __future__ import annotations

import os
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    release_tag = os.environ.get("GITHUB_REF_NAME", "")
    expected = {
        "Android versionName": (
            ROOT / "android/app/build.gradle.kts",
            r'^\s*versionName\s*=\s*"([^"]+)"',
        ),
    }

    errors: list[str] = []
    for name, (path, pattern) in expected.items():
        match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
        actual = match.group(1) if match else "missing"
        if actual != version:
            errors.append(f"{name}: expected {version}, got {actual}")

    windows = ET.parse(ROOT / "windows/LedgePill.csproj").getroot()
    windows_versions = {
        "Windows Version": "Version",
        "Windows assembly version": "AssemblyVersion",
        "Windows file version": "FileVersion",
        "Windows informational version": "InformationalVersion",
    }
    for name, element in windows_versions.items():
        node = windows.find(f".//{element}")
        actual = node.text.strip() if node is not None and node.text else "missing"
        target = (
            f"{version}.0" if element in {"AssemblyVersion", "FileVersion"} else version
        )
        if actual != target:
            errors.append(f"{name}: expected {target}, got {actual}")

    android = (ROOT / "android/app/build.gradle.kts").read_text(encoding="utf-8")
    code_match = re.search(r"^\s*versionCode\s*=\s*(\d+)", android, re.MULTILINE)
    if code_match is None or int(code_match.group(1)) < 1:
        errors.append("Android versionCode must be a positive integer")
    if release_tag.startswith("v") and release_tag[1:] != version:
        errors.append(
            f"release tag {release_tag} does not match project version {version}"
        )

    if errors:
        print("Platform version check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Platform versions agree with pyproject.toml: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
