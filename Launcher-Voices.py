"""Paradox launcher entry: start the selected Voices profile and debug game."""
from pathlib import Path
import ctypes
from datetime import datetime
import json
import msvcrt
import os
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
from unified_mod import select_unified, CONFIGS, install_or_compile, characterization_source, configure_bases, BASES

ROOT = Path(__file__).resolve().parent
USERDIR = Path(os.environ.get("VOICES_USERDIR", Path.home() / "Documents/Paradox Interactive/Hearts of Iron IV"))
DATA = ROOT / "middleware/user-data"
PROFILES = {
    frozenset({"mod/HOI4_AI_Voices_ChatGPT.mod"}): "config.json",
    frozenset({"mod/MD_LOCAL_STANDARD.mod", "mod/HOI4_AI_Voices_MD.mod"}): "config-md.json",
    frozenset({"mod/Cold_War_Iron_Curtain.mod", "mod/HOI4_AI_Voices_CWIC_Dev.mod"}): "config-cwic-dev.json",
    frozenset({"mod/ugc_3350890356.mod", "mod/HOI4_AI_Voices_TFR.mod"}): "config-tfr.json",
}


def select_profile(enabled_mods):
    unified = select_unified(enabled_mods)
    if unified:
        return CONFIGS[unified]
    if "mod/HOI4_AI_Voices_MD.mod" in enabled_mods:
        bases = [entry for entry in enabled_mods if BASES.get(entry) == "millennium_dawn"]
        if len(enabled_mods) == 2 and len(set(enabled_mods)) == 2 and len(bases) == 1:
            return "config-md.json"
    profile = PROFILES.get(frozenset(enabled_mods))
    if not profile or len(enabled_mods) != len(set(enabled_mods)):
        raise RuntimeError("Activate a dedicated playset containing one Voices test entry and its matching overhaul.")
    return profile


def log(message):
    DATA.mkdir(exist_ok=True)
    with (DATA / "launcher-session.log").open("a", encoding="utf-8") as stream:
        stream.write(datetime.now().astimezone().isoformat() + " " + message + "\n")


def hidden_process_args():
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": info, "creationflags": subprocess.CREATE_NO_WINDOW}


def process_ids(name, command_contains=None):
    name = name.replace("'", "''")
    script = f"$p=@(Get-CimInstance Win32_Process -Filter \"name='{name}'\"); "
    if command_contains:
        needle = str(command_contains).replace("'", "''")
        script += f"$p=@($p | Where-Object {{$_.CommandLine -and $_.CommandLine.Contains('{needle}')}}); "
    script += "ConvertTo-Json -Compress -InputObject @($p | ForEach-Object {$_.ProcessId})"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, check=True, **hidden_process_args())
    return json.loads(result.stdout.strip() or "[]")


def main(forwarded):
    DATA.mkdir(exist_ok=True)
    selection = json.loads((USERDIR / "dlc_load.json").read_text(encoding="utf-8"))
    configure_bases(USERDIR, selection.get("enabled_mods", []))
    config_name = select_profile(selection.get("enabled_mods", []))
    config_path = ROOT / "middleware" / config_name
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    detected = select_unified(selection.get("enabled_mods", [])) or ("millennium_dawn" if config_name == "config-md.json" else None)
    if detected and cfg.get("game_profile") != detected:
        raise RuntimeError("The selected mod profile disagrees with its middleware configuration: " + config_name)
    original = json.loads((ROOT / "launcher-bridge.json").read_text(encoding="utf-8"))
    game_dir = Path(original["game_directory"])
    executable = (game_dir / original["exePath"]).resolve()
    assert executable.is_file() and config_path.is_file()
    source = characterization_source(selection.get("enabled_mods", []), USERDIR, game_dir)
    if forwarded == ["--check"]:
        print(json.dumps({"selected_config": config_name, "profile": cfg.get("game_profile", "vanilla"),
                          "characterization_source": str(source) if source else None,
                          "dry_run": cfg["injection"]["dry_run"], "game_launch_requested": False}))
        return 0

    with (DATA / "launcher-session.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("A Voices launcher session is already running.") from None
        if process_ids("hoi4.exe"):
            raise RuntimeError("Hearts of Iron IV is already running. Close it before starting another session.")
        if process_ids("pythonw.exe", ROOT / "middleware/main.py"):
            raise RuntimeError("A Voices middleware is already running. Close its window before starting this launcher session.")
        if config_name in CONFIGS.values():
            install_or_compile(USERDIR, cfg["game_profile"], telemetry=cfg.get("telemetry", {}).get("enabled", False))
        python = ROOT / ".venv/Scripts/pythonw.exe"
        if not python.is_file():
            python = Path(sys.executable)
        output = (DATA / (config_name + ".stdout.log")).open("a", encoding="utf-8")
        errors = (DATA / (config_name + ".stderr.log")).open("a", encoding="utf-8")
        middleware = None
        game = None
        try:
            middleware_args = [str(python), str(ROOT / "middleware/main.py"), "--config", str(config_path), "--userdir", str(USERDIR)]
            if source:
                middleware_args += ["--characterization-source", str(source)]
            middleware = subprocess.Popen(middleware_args, cwd=ROOT / "middleware",
                                          stdout=output, stderr=errors)
            time.sleep(1)
            if middleware.poll() is not None:
                raise RuntimeError("The Voices middleware could not start. See its profile error log.")
            visible = subprocess.STARTUPINFO()
            visible.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            visible.wShowWindow = 1  # Win32 SW_SHOWNORMAL; subprocess only exports SW_HIDE.
            arguments = [str(executable), *original.get("exeArgs", []), "-debug", "-userdir=" + str(USERDIR), *forwarded]
            game = subprocess.Popen(arguments, cwd=game_dir, startupinfo=visible)
            log(f"Started profile={cfg.get('game_profile', 'vanilla')} game_pid={game.pid} "
                f"middleware_pid={middleware.pid} dry_run={cfg['injection']['dry_run']}")
            return game.wait()
        finally:
            if middleware is not None and middleware.poll() is None:
                subprocess.run(["taskkill.exe", "/PID", str(middleware.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               **hidden_process_args())
            output.close()
            errors.close()
            log("Launcher session ended.")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        log("Launch failed: " + str(error))
        ctypes.windll.user32.MessageBoxW(None, str(error), "Voices of History", 0x10)
        raise SystemExit(1)
