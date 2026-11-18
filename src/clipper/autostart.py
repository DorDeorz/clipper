"""Start `clipper run` at login (ROADMAP Faz 8).

Windows: a Task Scheduler task (`schtasks /Create … /SC ONLOGON`) that runs a small
`.cmd` launcher (it sets the working directory). Linux: a systemd *user* unit file;
the user enables it with `systemctl --user enable --now clipper`.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from clipper.util.proc import no_window

TASK_NAME = "clipper"
UNIT_NAME = "clipper.service"


@dataclass(frozen=True)
class Plan:
    kind: str  # "windows" | "systemd"
    files: dict[Path, str]
    commands: list[list[str]]  # run by `install` on Windows
    hint: str  # Turkish: what the user does next


def run_args(config: Path, env: Path) -> list[str]:
    return [
        sys.executable, "-m", "clipper", "--config", str(config.resolve()),
        "--env", str(env.resolve()), "run", "--no-browser",
    ]  # fmt: skip


def _cmd_quote(arg: str) -> str:
    return f'"{arg}"' if any(c in arg for c in ' &()^|<>"') else arg


def windows_plan(workdir: Path, config: Path, env: Path) -> Plan:
    launcher = workdir.resolve() / "data" / "clipper-autostart.cmd"
    body = (
        "@echo off\r\n"
        f'cd /d "{workdir.resolve()}"\r\n'
        + " ".join(_cmd_quote(a) for a in run_args(config, env))
        + ' >> "data\\logs\\autostart.log" 2>&1\r\n'
    )
    create = [
        "schtasks", "/Create", "/TN", TASK_NAME, "/TR", _cmd_quote(str(launcher)),
        "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
    ]  # fmt: skip
    return Plan(
        "windows",
        {launcher: body},
        [create],
        f"Görev Zamanlayıcı'ya '{TASK_NAME}' görevi eklendi: oturum açınca clipper başlar. "
        f"Kaldırmak için: clipper uninstall-autostart",
    )


def systemd_plan(workdir: Path, config: Path, env: Path, home: Path | None = None) -> Plan:
    home = home or Path.home()
    unit = home / ".config" / "systemd" / "user" / UNIT_NAME
    exec_start = " ".join(_systemd_quote(a) for a in run_args(config, env))
    body = (
        "[Unit]\n"
        "Description=clipper: yayın klip otomasyonu\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"WorkingDirectory={workdir.resolve()}\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "Environment=PYTHONUNBUFFERED=1\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    return Plan(
        "systemd",
        {unit: body},
        [],
        f"Unit dosyası yazıldı: {unit}\n"
        "Etkinleştirmek için: systemctl --user daemon-reload && "
        "systemctl --user enable --now clipper\n"
        "Oturum kapalıyken de çalışsın istersen: loginctl enable-linger $USER",
    )


def _systemd_quote(arg: str) -> str:
    if any(c in arg for c in ' "\\'):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def plan(workdir: Path, config: Path, env: Path) -> Plan:
    if sys.platform == "win32":
        return windows_plan(workdir, config, env)
    return systemd_plan(workdir, config, env)


def install(p: Plan, *, dry_run: bool = False) -> None:
    if dry_run:
        return
    for path, body in p.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="")
    for cmd in p.commands:
        subprocess.run(cmd, check=True, capture_output=True, text=True, **no_window())


def uninstall(workdir: Path) -> str:
    if sys.platform == "win32":
        subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            check=True, capture_output=True, text=True, **no_window(),
        )  # fmt: skip
        (workdir.resolve() / "data" / "clipper-autostart.cmd").unlink(missing_ok=True)
        return f"'{TASK_NAME}' görevi silindi."
    unit = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    unit.unlink(missing_ok=True)
    return (
        f"{unit} silindi. Çalışıyorsa: systemctl --user disable --now clipper && "
        "systemctl --user daemon-reload"
    )


# ---- desktop shortcut ---------------------------------------------------------------------


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def gui_python() -> str:
    """pythonw.exe next to the venv's python on Windows (no console window)."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.is_file() else exe)


def shortcut_plan(workdir: Path, config: Path, env: Path, home: Path | None = None) -> Plan:
    """Double-click "Clipper": starts `clipper run` (or opens the panel if it already
    runs). Windows: a .lnk on the Desktop that runs pythonw (no terminal). Linux: a
    .desktop launcher."""
    wd = workdir.resolve()
    args = [
        "-m", "clipper", "--config", str(config.resolve()), "--env", str(env.resolve()), "run",
    ]  # fmt: skip
    if sys.platform == "win32":
        arg_line = " ".join(_cmd_quote(a) for a in args)
        script = (
            "$d=[Environment]::GetFolderPath('Desktop');"
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Clipper.lnk'));"
            f"$s.TargetPath={_ps_quote(gui_python())};"
            f"$s.Arguments={_ps_quote(arg_line)};"
            f"$s.WorkingDirectory={_ps_quote(str(wd))};"
            "$s.Description='clipper paneli';"
            "$s.Save()"
        )
        return Plan(
            "windows",
            {},
            [["powershell", "-NoProfile", "-NonInteractive", "-Command", script]],
            "Masaüstüne 'Clipper' kısayolu eklendi. Çift tıklayınca panel açılır; kapatmak "
            "için paneldeki 'Kapat' butonu.",
        )
    home = home or Path.home()
    exec_line = " ".join(_systemd_quote(a) for a in [sys.executable, *args])
    body = (
        "[Desktop Entry]\nType=Application\nName=Clipper\nComment=clipper paneli\n"
        f"Path={wd}\nExec={exec_line}\nTerminal=false\nCategories=AudioVideo;\n"
    )
    target = home / ".local" / "share" / "applications" / "clipper.desktop"
    return Plan(
        "desktop",
        {target: body},
        [],
        f"Uygulama menüsüne 'Clipper' eklendi ({target}).",
    )
