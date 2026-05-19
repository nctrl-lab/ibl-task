#!/usr/bin/env python3
"""Install a Linux application-menu entry for the IBL task.

Drops a .desktop file in ~/.local/share/applications, copies the icon
into the user icon theme, and places a clickable launcher in the
Desktop folder so the task GUI can be started from a desktop icon.
Run once after `pip install -e .`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if not sys.platform.startswith("linux"):
        print("This installer is for Linux. On Windows and macOS, `pip install`")
        print("creates an `ibl` command on PATH; pin or shortcut it from your")
        print("shell or the Start menu / Dock.")
        return 0

    ibl_bin = shutil.which("ibl")
    if not ibl_bin:
        print("error: ibl not found on PATH. Run `pip install -e .` first.",
              file=sys.stderr)
        return 1

    home = Path.home()
    apps_dir = home / ".local/share/applications"
    icons_dir = home / ".local/share/icons/hicolor/scalable/apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    icons_dir.mkdir(parents=True, exist_ok=True)

    src_icon = Path(__file__).resolve().parent.parent / "ibl" / "icon.svg"
    if not src_icon.exists():
        print(f"error: icon not found at {src_icon}", file=sys.stderr)
        return 1
    dst_icon = icons_dir / "ibl-task.svg"
    shutil.copy(src_icon, dst_icon)

    # Activate the conda env so the launcher sees the same libs as the terminal.
    p = Path(ibl_bin).resolve()
    conda_sh = p.parents[3] / "etc/profile.d/conda.sh"
    if conda_sh.exists():
        exec_field = f'bash -c "source {conda_sh} && conda activate {p.parents[1].name} && exec ibl"'
    else:
        exec_field = ibl_bin

    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=IBL task\n"
        "GenericName=trainingChoiceWorld 2AFC\n"
        "Comment=IBL trainingChoiceWorld visual 2AFC task for head-fixed mice\n"
        f"Exec={exec_field}\n"
        "Icon=ibl-task\n"
        "Terminal=false\n"
        "Categories=Science;Education;\n"
        "StartupNotify=true\n"
        "Keywords=ibl;neuroscience;psychophysics;2afc;choice;mouse;\n"
    )
    dst_desktop = apps_dir / "ibl-task.desktop"
    dst_desktop.write_text(desktop)
    dst_desktop.chmod(0o755)

    # Best-effort cache refresh; missing tools are fine.
    for cmd in (
        ["update-desktop-database", str(apps_dir)],
        ["gtk-update-icon-cache", str(home / ".local/share/icons/hicolor")],
    ):
        try:
            subprocess.run(cmd, check=False,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass

    # Also place a clickable launcher on the user's Desktop. GNOME/Nautilus
    # requires the file to be executable AND marked "trusted" before it will
    # render as a launcher icon instead of a plain text file.
    desktop_dir = _xdg_desktop_dir(home)
    dst_launcher = None
    if desktop_dir and desktop_dir.is_dir():
        dst_launcher = desktop_dir / "ibl-task.desktop"
        shutil.copy(dst_desktop, dst_launcher)
        dst_launcher.chmod(0o755)
        try:
            subprocess.run(
                ["gio", "set", str(dst_launcher), "metadata::trusted", "true"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

    print(f"Installed: {dst_desktop}")
    print(f"Icon:      {dst_icon}")
    if dst_launcher:
        print(f"Launcher:  {dst_launcher}")
        print("If the desktop icon shows as a text file, right-click it and")
        print("choose 'Allow Launching' (GNOME) or mark it executable.")
    print("The IBL task should now appear in your application menu.")
    return 0


def _xdg_desktop_dir(home: Path) -> Path | None:
    """Resolve the user's Desktop directory via xdg-user-dir, with fallback."""
    try:
        out = subprocess.run(
            ["xdg-user-dir", "DESKTOP"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if out:
            return Path(out)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    fallback = home / "Desktop"
    return fallback if fallback.is_dir() else None


if __name__ == "__main__":
    raise SystemExit(main())
