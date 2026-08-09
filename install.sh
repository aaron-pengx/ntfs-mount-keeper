#!/usr/bin/env bash
# NTFS Mount Keeper installer
#
#   curl -sL https://raw.githubusercontent.com/aaron-pengx/ntfs-mount-keeper/main/install.sh | sudo bash
#
# Uninstall (also reverts the fstab entry and the polkit rule):
#
#   curl -sL https://raw.githubusercontent.com/aaron-pengx/ntfs-mount-keeper/main/install.sh | sudo bash -s -- --uninstall

set -euo pipefail

REPO="aaron-pengx/ntfs-mount-keeper"
NAME="ntfs-mount-keeper"
ZIP_URL="https://github.com/${REPO}/releases/latest/download/${NAME}.zip"

HOMEBREW="/home/deck/homebrew"
PLUGIN_DIR="${HOMEBREW}/plugins/${NAME}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "需要 root 权限，请用 sudo 运行。"

# --------------------------------------------------------------------------- #
# uninstall
# --------------------------------------------------------------------------- #

if [ "${1:-}" = "--uninstall" ]; then
    [ -d "$PLUGIN_DIR" ] || die "没有找到已安装的插件：${PLUGIN_DIR}"

    say "正在撤销对系统的改动…"
    # Let the plugin revert its own fstab entry and polkit rule before the
    # code that knows how to do that is deleted.
    python3 "${PLUGIN_DIR}/main.py" --remove || \
        printf '撤销时报错，请稍后手动检查 /etc/fstab\n' >&2

    say "正在删除插件文件…"
    rm -rf "$PLUGIN_DIR"

    systemctl restart plugin_loader 2>/dev/null || true
    say "已卸载。原始 /etc/fstab 的备份仍保留在 ${HOMEBREW}/settings/ 下。"
    exit 0
fi

# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #

[ -d "$HOMEBREW" ] || die "没有找到 ${HOMEBREW}，请先安装 Decky Loader：https://decky.xyz/"

command -v python3 >/dev/null || die "没有找到 python3，插件后端需要它。"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

say "正在下载最新版本…"
if command -v curl >/dev/null; then
    curl -fsSL "$ZIP_URL" -o "${tmp}/plugin.zip" || die "下载失败：${ZIP_URL}"
elif command -v wget >/dev/null; then
    wget -qO "${tmp}/plugin.zip" "$ZIP_URL" || die "下载失败：${ZIP_URL}"
else
    die "既没有 curl 也没有 wget。"
fi

say "正在解压…"
# Not using unzip: python3 is guaranteed to be present, unzip is not.
python3 - "$tmp" <<'PY' || die "解压失败，下载的文件可能不完整。"
import sys, zipfile, pathlib
tmp = pathlib.Path(sys.argv[1])
with zipfile.ZipFile(tmp / "plugin.zip") as z:
    z.extractall(tmp / "extracted")
PY

src="${tmp}/extracted/${NAME}"
[ -f "${src}/main.py" ] || die "压缩包内容不是预期的结构。"

# Settings live outside the plugin directory, so an upgrade keeps the user's
# label, mountpoint and mount options.
if [ -d "$PLUGIN_DIR" ]; then
    say "检测到已安装，正在升级（配置会保留）…"
    rm -rf "$PLUGIN_DIR"
else
    say "正在安装…"
fi

mkdir -p "$(dirname "$PLUGIN_DIR")"
cp -r "$src" "$PLUGIN_DIR"
chown -R deck:deck "$PLUGIN_DIR"
find "$PLUGIN_DIR" -type f -exec chmod 644 {} +
find "$PLUGIN_DIR" -type d -exec chmod 755 {} +

say "正在重启 Decky…"
systemctl restart plugin_loader || die "重启 plugin_loader 失败，请手动执行：sudo systemctl restart plugin_loader"

cat <<'DONE'

安装完成。

打开快捷菜单（手柄上的 ... 键）→ 插件图标 → NTFS Mount Keeper。

默认针对卷标为 SSD 的分区。如果你的盘不叫这个名字，在面板的
「选择卷标」里改，然后点「立即修复并挂载」。

不依赖 Decky 的手动修复命令（Decky 挂掉时用）：

  sudo python3 /home/deck/homebrew/plugins/ntfs-mount-keeper/main.py --apply
  sudo python3 /home/deck/homebrew/plugins/ntfs-mount-keeper/main.py --status

DONE
