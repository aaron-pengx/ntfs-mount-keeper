import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import decky
except ImportError:
    # Also runs standalone: sudo python3 main.py --apply. Decky's own systemd
    # unit lives in /etc and is wiped by SteamOS updates, so the recovery path
    # cannot depend on Decky being alive.
    import logging as _logging
    import types as _types

    _logging.basicConfig(format="%(levelname)s: %(message)s", level=_logging.INFO)
    decky = _types.SimpleNamespace(
        logger=_logging.getLogger("ntfs-mount-keeper"),
        DECKY_PLUGIN_SETTINGS_DIR="/home/deck/homebrew/settings/ntfs-mount-keeper",
    )

FSTAB = "/etc/fstab"
POLKIT_RULE = "/etc/polkit-1/rules.d/50-udisks-mount-nopasswd.rules"

# libmount does not understand trailing "#" comments, so the marker lives on its
# own line above the entry and the block is identified by that pair of lines.
MARKER = "# managed by ntfs-mount-keeper -- do not edit this block by hand"

POLKIT_RULE_BODY = """/* managed by ntfs-mount-keeper -- do not edit by hand */
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.udisks2.filesystem-mount-system" ||
        action.id == "org.freedesktop.udisks2.filesystem-mount-other-seat") {
        if (subject.local && subject.active) {
            return polkit.Result.YES;
        }
    }
});
"""

DEFAULTS: Dict[str, Any] = {
    "label": "SSD",
    "mountpoint": "/run/media/deck/SSD",
    # "ntfs" resolves to mount.ntfs-3g on SteamOS; that is the same path udisks2
    # already takes, so it is known to work on this hardware.
    "fstype": "ntfs",
    # Without uid/gid the deck user cannot write: ntfs-3g would otherwise hand
    # the whole volume to root, unlike the udisks2 automount. umask stays at 000
    # to keep the execute bit on everything -- the volume holds a Steam library,
    # and NTFS carries no real Unix mode bits to fall back on.
    "options": "nofail,uid=1000,gid=1000,umask=000",
    "apply_on_boot": True,
    "manage_polkit": True,
}

# Mounting over any of these would shadow a live system directory.
FORBIDDEN_MOUNTPOINTS = {
    "/", "/boot", "/dev", "/efi", "/esp", "/etc", "/home", "/proc",
    "/root", "/run", "/srv", "/sys", "/tmp", "/usr", "/var",
}

# Held in memory only; written to disk (0600) exclusively when the user asks.
_password: Optional[str] = None


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

def _settings_dir() -> Path:
    d = Path(decky.DECKY_PLUGIN_SETTINGS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_file() -> Path:
    return _settings_dir() / "config.json"


def _password_file() -> Path:
    return _settings_dir() / "sudo_password"


def _backup_file() -> Path:
    return _settings_dir() / "fstab.orig"


# --------------------------------------------------------------------------- #
# privileged execution
# --------------------------------------------------------------------------- #

def _is_root() -> bool:
    return os.geteuid() == 0


def _run(argv: List[str], input_text: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out: {' '.join(argv)}"


def _run_priv(argv: List[str]) -> Tuple[int, str, str]:
    """Run argv as root, falling back to sudo -S when the plugin was not
    granted the _root flag."""
    if _is_root():
        return _run(argv)
    if not _password:
        return 1, "", "plugin is not running as root and no sudo password is set"
    return _run(["sudo", "-S", "-k", "--"] + argv, input_text=_password + "\n")


def _write_priv(path: str, content: str, mode: str = "0644") -> Tuple[int, str, str]:
    if _is_root():
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            os.chmod(path, int(mode, 8))
            return 0, "", ""
        except OSError as e:
            return 1, "", str(e)

    tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".nmk")
    try:
        tmp.write(content)
        tmp.close()
        return _run_priv(["install", "-D", "-m", mode, tmp.name, path])
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def _load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(_config_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def _save_config(cfg: Dict[str, Any]) -> None:
    _config_file().write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _sanitize(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in cfg and cfg[key] is not None:
            out[key] = cfg[key]
    out["label"] = str(out["label"]).strip()
    # Keep "/" intact instead of letting rstrip collapse it to "", otherwise the
    # fallback silently rewrites it and _validate never gets to reject it.
    mountpoint = str(out["mountpoint"]).strip()
    out["mountpoint"] = mountpoint.rstrip("/") or mountpoint or DEFAULTS["mountpoint"]
    out["fstype"] = str(out["fstype"]).strip() or "ntfs"
    out["options"] = str(out["options"]).strip() or DEFAULTS["options"]
    # nofail is not negotiable: without it a missing or dirty volume drops the
    # Deck into emergency mode at boot, which needs a root password to escape.
    opts = [o for o in out["options"].split(",") if o]
    if "nofail" not in opts:
        opts.insert(0, "nofail")
    out["options"] = ",".join(opts)
    out["apply_on_boot"] = bool(out["apply_on_boot"])
    out["manage_polkit"] = bool(out["manage_polkit"])
    # An fstab field cannot contain whitespace; a space in a mountpoint has to be
    # escaped the way util-linux expects.
    out["mountpoint"] = out["mountpoint"].replace(" ", "\\040")
    return out


def _fstab_entry(cfg: Dict[str, Any]) -> str:
    return f"LABEL={cfg['label']} {cfg['mountpoint']} {cfg['fstype']} {cfg['options']} 0 0"


def _validate(cfg: Dict[str, Any]) -> List[str]:
    """Everything that must hold before a line is allowed near /etc/fstab."""
    errors: List[str] = []

    if not cfg["label"]:
        errors.append("卷标为空")
    elif any(c.isspace() for c in cfg["label"]):
        errors.append(f"卷标不能含空格：{cfg['label']}")

    mountpoint = cfg["mountpoint"].replace("\\040", " ")
    if not mountpoint.startswith("/"):
        errors.append(f"挂载点必须是绝对路径：{mountpoint}")
    elif mountpoint.rstrip("/") in FORBIDDEN_MOUNTPOINTS or mountpoint == "/":
        errors.append(f"拒绝挂载到系统目录：{mountpoint}")

    if any(c.isspace() for c in cfg["fstype"]) or not cfg["fstype"]:
        errors.append(f"文件系统类型非法：{cfg['fstype']}")

    if any(c.isspace() for c in cfg["options"]):
        errors.append("挂载选项不能含空格")

    if len(_fstab_entry(cfg).split()) != 6:
        errors.append("生成的 fstab 行字段数不是 6")

    return errors


def _verify_fstab_file(path: str) -> str:
    """findmnt's own parser, for the log only -- it also warns about volumes
    that merely happen to be unplugged, so it must not gate the write."""
    rc, out, err = _run(["findmnt", "--verify", "--tab-file", path])
    return f"rc={rc} {out} {err}".strip()


# --------------------------------------------------------------------------- #
# fstab
# --------------------------------------------------------------------------- #

def _read_fstab() -> str:
    try:
        return Path(FSTAB).read_text(encoding="utf-8")
    except OSError:
        return ""


def _strip_managed_block(text: str) -> str:
    """Drop the marker line and the entry directly beneath it."""
    lines = text.splitlines()
    out: List[str] = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if line.strip() == MARKER:
            skip_next = True
            continue
        out.append(line)
    return "\n".join(out)


def _conflicts(line: str, cfg: Dict[str, Any]) -> bool:
    """True for any live entry aiming at the same mountpoint or the same volume.
    Two entries for one mountpoint hand systemd two competing mount units, so a
    hand-written line has to be absorbed rather than left alongside ours."""
    text = line.strip()
    if not text or text.startswith("#"):
        return False
    fields = text.split()
    if len(fields) < 2:
        return False
    return fields[0] == f"LABEL={cfg['label']}" or fields[1] == cfg["mountpoint"]


def _backup_fstab_once() -> None:
    backup = _backup_file()
    if backup.exists():
        return
    try:
        shutil.copyfile(FSTAB, backup)
    except OSError as e:
        decky.logger.warning(f"could not back up fstab: {e}")


def _ensure_fstab(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """Returns (changed, error)."""
    errors = _validate(cfg)
    if errors:
        return False, "；".join(errors)

    current = _read_fstab()
    if not current:
        return False, f"could not read {FSTAB}"

    _backup_fstab_once()

    entry = _fstab_entry(cfg)
    desired_block = f"{MARKER}\n{entry}"

    stripped = _strip_managed_block(current)
    # Absorb any hand-written entry for the same volume or mountpoint instead of
    # leaving it next to ours; _validate already rules out system mountpoints,
    # so this cannot swallow one of SteamOS's own lines.
    kept = [ln for ln in stripped.splitlines() if not _conflicts(ln, cfg)]

    body = "\n".join(kept).rstrip("\n")
    new_text = f"{body}\n{desired_block}\n"

    if new_text == current:
        return False, ""

    rc, _, err = _write_priv(FSTAB, new_text, "0644")
    if rc != 0:
        return False, err or "failed to write fstab"

    if not _fstab_ok(cfg):
        _write_priv(FSTAB, current, "0644")
        return False, "写入后校验失败，已回滚 /etc/fstab"

    decky.logger.info(f"findmnt --verify: {_verify_fstab_file(FSTAB)}")
    return True, ""


def _remove_fstab(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    current = _read_fstab()
    if not current:
        return False, f"could not read {FSTAB}"
    entry = _fstab_entry(cfg)
    stripped = _strip_managed_block(current)
    kept = [ln for ln in stripped.splitlines() if ln.strip() != entry]
    new_text = "\n".join(kept).rstrip("\n") + "\n"
    if new_text == current:
        return False, ""
    rc, _, err = _write_priv(FSTAB, new_text, "0644")
    if rc != 0:
        return False, err or "failed to write fstab"
    return True, ""


def _fstab_ok(cfg: Dict[str, Any]) -> bool:
    entry = _fstab_entry(cfg)
    return any(ln.strip() == entry for ln in _read_fstab().splitlines())


# --------------------------------------------------------------------------- #
# polkit
# --------------------------------------------------------------------------- #

def _polkit_ok() -> bool:
    try:
        return Path(POLKIT_RULE).read_text(encoding="utf-8") == POLKIT_RULE_BODY
    except OSError:
        return False


def _ensure_polkit() -> Tuple[bool, str]:
    if _polkit_ok():
        return False, ""
    rc, _, err = _write_priv(POLKIT_RULE, POLKIT_RULE_BODY, "0644")
    if rc != 0:
        return False, err or "failed to write polkit rule"
    return True, ""


def _remove_polkit() -> Tuple[bool, str]:
    if not Path(POLKIT_RULE).exists():
        return False, ""
    rc, _, err = _run_priv(["rm", "-f", POLKIT_RULE])
    if rc != 0:
        return False, err or "failed to remove polkit rule"
    return True, ""


# --------------------------------------------------------------------------- #
# mounting
# --------------------------------------------------------------------------- #

def _mountpoint_exists(cfg: Dict[str, Any]) -> bool:
    return Path(cfg["mountpoint"].replace("\\040", " ")).is_dir()


def _ensure_mountpoint(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    path = cfg["mountpoint"].replace("\\040", " ")
    if Path(path).is_dir():
        return False, ""
    rc, _, err = _run_priv(["mkdir", "-p", path])
    if rc != 0:
        return False, err or "failed to create mountpoint"
    # udisks2 owns /run/media/deck; keep the directory usable by the deck user.
    _run_priv(["chown", "deck:deck", path])
    return True, ""


def _device_path(cfg: Dict[str, Any]) -> Optional[str]:
    p = Path("/dev/disk/by-label") / cfg["label"]
    if p.exists():
        try:
            return os.path.realpath(p)
        except OSError:
            return str(p)
    return None


def _is_mounted(cfg: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    target = cfg["mountpoint"].replace("\\040", " ")
    try:
        for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].replace("\\040", " ") == target:
                return True, parts[0]
    except OSError:
        pass
    return False, None


def _mount(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    mounted, _ = _is_mounted(cfg)
    if mounted:
        return False, ""
    if not _device_path(cfg):
        return False, f"no volume with label '{cfg['label']}' is attached"
    _run_priv(["systemctl", "daemon-reload"])
    rc, _, err = _run_priv(["mount", cfg["mountpoint"].replace("\\040", " ")])
    if rc != 0:
        return False, err or "mount failed"
    return True, ""


def _unmount(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    mounted, _ = _is_mounted(cfg)
    if not mounted:
        return False, ""
    rc, _, err = _run_priv(["umount", cfg["mountpoint"].replace("\\040", " ")])
    if rc != 0:
        return False, err or "umount failed"
    return True, ""


# --------------------------------------------------------------------------- #
# plugin
# --------------------------------------------------------------------------- #

class Plugin:
    async def _main(self) -> None:
        global _password
        decky.logger.info("ntfs-mount-keeper starting")

        pw_file = _password_file()
        if pw_file.exists():
            if _is_root():
                # Dead weight now -- do not leave a plaintext credential behind
                # just because the plugin once ran unprivileged.
                try:
                    pw_file.unlink()
                    decky.logger.info("running as root, removed the stored sudo password")
                except OSError:
                    pass
            else:
                try:
                    _password = pw_file.read_text(encoding="utf-8").strip() or None
                except OSError:
                    _password = None

        cfg = _sanitize(_load_config())
        if cfg["apply_on_boot"]:
            result = await self.apply_now()
            decky.logger.info(f"boot-time apply: {result}")

    async def _unload(self) -> None:
        decky.logger.info("ntfs-mount-keeper unloading")

    async def _uninstall(self) -> None:
        cfg = _sanitize(_load_config())
        _remove_fstab(cfg)
        _remove_polkit()
        decky.logger.info("ntfs-mount-keeper uninstalled, system files reverted")

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #

    async def get_state(self) -> Dict[str, Any]:
        cfg = _sanitize(_load_config())
        mounted, source = _is_mounted(cfg)
        return {
            "root": _is_root(),
            "config": cfg,
            "entry": _fstab_entry(cfg),
            "fstab_ok": _fstab_ok(cfg),
            "polkit_ok": _polkit_ok(),
            "mountpoint_ok": _mountpoint_exists(cfg),
            "device": _device_path(cfg),
            "mounted": mounted,
            "mount_source": source,
            "has_password": _password is not None,
            "password_saved": _password_file().exists(),
        }

    async def list_volumes(self) -> List[Dict[str, Any]]:
        """Every labelled volume currently attached, so the user can pick one
        instead of typing a label by hand."""
        rc, out, _ = _run(["lsblk", "-J", "-o", "NAME,LABEL,FSTYPE,SIZE,MOUNTPOINT"])
        if rc != 0 or not out:
            return []

        found: List[Dict[str, Any]] = []

        def walk(nodes: List[Dict[str, Any]]) -> None:
            for node in nodes:
                if node.get("label") and node.get("fstype"):
                    found.append({
                        "name": node.get("name"),
                        "label": node.get("label"),
                        "fstype": node.get("fstype"),
                        "size": node.get("size"),
                        "mountpoint": node.get("mountpoint"),
                    })
                walk(node.get("children") or [])

        try:
            walk(json.loads(out).get("blockdevices", []))
        except ValueError:
            return []
        return found

    # ------------------------------------------------------------------ #
    # mutations
    # ------------------------------------------------------------------ #

    async def set_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        cfg = _sanitize(config)
        _save_config(cfg)
        return {"ok": True, "config": cfg}

    async def apply_now(self) -> Dict[str, Any]:
        cfg = _sanitize(_load_config())
        steps: List[str] = []
        errors: List[str] = []

        problems = _validate(cfg)
        if problems:
            return {"ok": False, "steps": [], "errors": problems}

        changed, err = _ensure_mountpoint(cfg)
        if err:
            errors.append(err)
        elif changed:
            steps.append(f"created {cfg['mountpoint']}")

        changed, err = _ensure_fstab(cfg)
        if err:
            errors.append(err)
        elif changed:
            steps.append("restored the /etc/fstab entry")

        if cfg["manage_polkit"]:
            changed, err = _ensure_polkit()
            if err:
                errors.append(err)
            elif changed:
                steps.append("restored the udisks2 polkit rule")

        changed, err = _mount(cfg)
        if err:
            errors.append(err)
        elif changed:
            steps.append(f"mounted {cfg['label']}")

        return {"ok": not errors, "steps": steps, "errors": errors}

    async def mount_now(self) -> Dict[str, Any]:
        cfg = _sanitize(_load_config())
        _ensure_mountpoint(cfg)
        changed, err = _mount(cfg)
        if err:
            return {"ok": False, "steps": [], "errors": [err]}
        return {"ok": True, "steps": ["mounted" if changed else "already mounted"], "errors": []}

    async def unmount_now(self) -> Dict[str, Any]:
        cfg = _sanitize(_load_config())
        changed, err = _unmount(cfg)
        if err:
            return {"ok": False, "steps": [], "errors": [err]}
        return {"ok": True, "steps": ["unmounted" if changed else "not mounted"], "errors": []}

    async def remove_all(self) -> Dict[str, Any]:
        cfg = _sanitize(_load_config())
        steps: List[str] = []
        errors: List[str] = []

        changed, err = _remove_fstab(cfg)
        if err:
            errors.append(err)
        elif changed:
            steps.append("removed the /etc/fstab entry")

        changed, err = _remove_polkit()
        if err:
            errors.append(err)
        elif changed:
            steps.append("removed the polkit rule")

        return {"ok": not errors, "steps": steps, "errors": errors}

    # ------------------------------------------------------------------ #
    # sudo fallback, only relevant when the _root flag is not honoured
    # ------------------------------------------------------------------ #

    async def set_password(self, password: str, remember: bool) -> Dict[str, Any]:
        global _password
        _password = password or None

        if not _password:
            try:
                _password_file().unlink()
            except OSError:
                pass
            return {"ok": True, "steps": ["password cleared"], "errors": []}

        rc, _, err = _run(["sudo", "-S", "-k", "--", "true"], input_text=_password + "\n")
        if rc != 0:
            _password = None
            return {"ok": False, "steps": [], "errors": [err or "password rejected by sudo"]}

        if remember:
            pw_file = _password_file()
            pw_file.write_text(_password, encoding="utf-8")
            os.chmod(pw_file, 0o600)
            return {"ok": True, "steps": ["password verified and saved"], "errors": []}

        try:
            _password_file().unlink()
        except OSError:
            pass
        return {"ok": True, "steps": ["password verified for this session"], "errors": []}


# --------------------------------------------------------------------------- #
# standalone entry point -- the recovery path when Decky itself is gone
# --------------------------------------------------------------------------- #

def _cli() -> int:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        prog="ntfs-mount-keeper",
        description="在 Decky 不可用时手动修复 NTFS 挂载配置",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true", help="写回 fstab 与 polkit 规则并挂载")
    group.add_argument("--status", action="store_true", help="打印当前状态")
    group.add_argument("--remove", action="store_true", help="移除本工具写入的所有配置")
    args = parser.parse_args()

    plugin = Plugin()

    if args.status:
        print(json.dumps(asyncio.run(plugin.get_state()), indent=2, ensure_ascii=False))
        return 0

    result = asyncio.run(plugin.apply_now() if args.apply else plugin.remove_all())
    for step in result["steps"]:
        print(f"  {step}")
    for error in result["errors"]:
        print(f"  错误: {error}", file=sys.stderr)
    if not result["steps"] and not result["errors"]:
        print("  已是目标状态，无需改动")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if not _is_root():
        print("需要 root 权限，请用 sudo 运行", file=sys.stderr)
        sys.exit(1)
    sys.exit(_cli())
