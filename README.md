# NTFS Mount Keeper

Steam Deck (SteamOS 3) 上的 Decky Loader 插件，用来让一个 NTFS 分区（例如 Windows 分区或外接 SSD）开机自动挂载，并且**在每次 SteamOS 系统更新把配置抹掉之后自动写回去**。

## 为什么需要它

手工做法是改两个文件：

| 文件 | 改动 | 系统更新后 |
|---|---|---|
| `/etc/fstab` | 增加 `LABEL=SSD /run/media/deck/SSD ntfs defaults,nofail 0 0` | 被镜像里的版本顶掉，改动丢失 |
| `/usr/share/polkit-1/actions/org.freedesktop.UDisks2.policy` | 把两处 `allow_active` 改成 `yes` | `/usr` 整体替换，改动必然丢失 |

`/home` 是独立分区，系统更新完全不碰。Decky 插件装在 `/home/deck/homebrew/plugins/` 下，因此插件本身能存活，可以在每次加载时把上面两项配置重新写回系统。

另外，polkit 部分本插件**不再修改那个 3882 行的 policy 文件**，而是写一条独立规则：

```
/etc/polkit-1/rules.d/50-udisks-mount-nopasswd.rules
```

`rules.d` 优先级高于 `actions/*.policy` 里的 `<defaults>`，按 `action.id` 匹配，与行号无关。原做法依赖"第 170 行 / 第 250 行"，而 udisks2 每次版本更新增删多语言 `<message>` 条目都会让行号漂移（实际当前文件里已经变成 181 和 265 行）。

## 功能

- 开机（插件加载时）自动检查并修复 `/etc/fstab` 条目与 polkit 规则，幂等，不会重复写入
- 面板内查看状态：root 权限、fstab 条目、polkit 规则、设备是否接入、是否已挂载
- 从当前接入的卷中下拉选择卷标，也可手动填写卷标、挂载点、文件系统、挂载选项
- 手动"立即修复并挂载" / "仅挂载" / "卸载"
- 一键从系统中移除本插件写入的配置（卸载插件时也会自动执行）
- 首次运行时把原始 `/etc/fstab` 备份到插件设置目录
- 非 root 场景下可填写并（可选）保存 sudo 密码作为回退

## Decky 挂掉时的兜底

Decky Loader 自己的 systemd unit 在 `/etc/systemd/system/plugin_loader.service`，也就是说**它同样会被 SteamOS 更新抹掉**。Decky 起不来，插件就不会被加载，自愈机制也不会运行。

因此 `main.py` 可以脱离 Decky 独立运行 —— 检测不到 `decky` 模块时自动降级（日志打到 stdout，配置读固定路径）：

```bash
sudo python3 /home/deck/homebrew/plugins/ntfs-mount-keeper/main.py --apply
```

```bash
sudo python3 /home/deck/homebrew/plugins/ntfs-mount-keeper/main.py --status
```

```bash
sudo python3 /home/deck/homebrew/plugins/ntfs-mount-keeper/main.py --remove
```

同一份代码两种用法，逻辑不重复，不会出现"插件改了脚本没跟上"的漂移。文件在 `/home`，系统更新动不了它。

## 写入前的护栏

以下任何一条不通过，就不会碰 `/etc/fstab`：

- 卷标为空或含空格
- 挂载点不是绝对路径，或落在 `/`、`/usr`、`/etc`、`/home`、`/var`、`/boot` 等系统目录上
- 文件系统类型为空或含空格
- 挂载选项含空格
- 生成的 fstab 行字段数不等于 6

另外两条无条件生效：

- **`nofail` 会被强制补回**。删掉它意味着目标盘一旦缺失或变脏，开机就掉进 emergency mode，而那里需要 root 密码才能进去。
- **写入后立即读回校验**，目标行不在就从原内容回滚，并把 `findmnt --verify` 的结果记进日志（只记录不拦截 —— 盘没插时它也会报警告，不能拿来当写入门槛）。

## 权限

`plugin.json` 里声明了 `"flags": ["root"]`。Decky Loader 的 `plugin_loader.service` 本身以 root 运行，但插件后端默认降权到 `deck` 用户，加上这个 flag 后端才真正以 root 运行，从而无需 sudo 密码即可写 `/etc/fstab` 和 `/etc/polkit-1/rules.d/`。

> flag 名是 `root`，不是 `_root`。可以参照 PowerTools 等已知需要提权的插件的 `plugin.json` 来核对，用 `ps -eo user,pid,args | grep <插件名>` 能直接看到后端进程实际以谁的身份运行。

面板底部的密码区只在检测到插件**不是** root 时才显示，此时可填 `deck` 用户的 sudo 密码作为回退。勾选"记住密码"会把密码以 `0600` 权限明文写入插件设置目录 —— 明文存储始终有风险，能用 `root` flag 就不要走这条路径。插件以 root 加载时，会自动删除之前存下的密码文件。

## 构建

需要 Node 18+ 和 pnpm/npm：

```bash
npm install && npm run build
```

产物是 `dist/index.js`。

## 安装到 Steam Deck

把下列文件按原结构拷到 `/home/deck/homebrew/plugins/ntfs-mount-keeper/`：

```
plugin.json
package.json
main.py
dist/index.js
```

然后重启 Decky：

```bash
sudo systemctl restart plugin_loader
```

从 PC 用 SSH 推送（Deck 上先 `sudo systemctl enable --now sshd`，`<deck-ip>` 换成 `ip route get 1.1.1.1 | awk '{print $7; exit}'` 的输出）：

```bash
scp -r ntfs-mount-keeper deck@<deck-ip>:/home/deck/
```

```bash
sudo cp -r /home/deck/ntfs-mount-keeper /home/deck/homebrew/plugins/ && sudo chown -R deck:deck /home/deck/homebrew/plugins/ntfs-mount-keeper && rm -rf /home/deck/ntfs-mount-keeper
```

先推到家目录再转移，是因为 `homebrew/plugins/` 可能属 root，`scp` 以 `deck` 身份直接写会被拒。

## 默认配置

```json
{
  "label": "SSD",
  "mountpoint": "/run/media/deck/SSD",
  "fstype": "ntfs",
  "options": "nofail,uid=1000,gid=1000,umask=000",
  "apply_on_boot": true,
  "manage_polkit": true
}
```

配置存放于 `DECKY_PLUGIN_SETTINGS_DIR/config.json`（位于 `/home`，系统更新不会丢）。

## 注意事项

- `nofail` 必须保留，否则目标盘没接入时会拖慢甚至卡住开机。
- 类型写 `ntfs` 时，SteamOS 会解析到 `mount.ntfs-3g`（FUSE 用户态驱动，`findmnt` 里显示为 `fuseblk`），与 udisks2 自动挂载走的是同一条路径。
- `uid=1000,gid=1000` 不能省。udisks2 自动挂载时会把卷交给登录用户，而 fstab 里只写 `defaults` 的话 ntfs-3g 会把整卷判给 root，deck 用户和 Steam 都写不进去。
- `umask=000` 是刻意保留的。NTFS 不带真实的 Unix 权限位，全靠挂载选项统一赋值；收紧成 `umask=022` 会抹掉所有文件的执行位，卷上的 Steam 库和 Proton 会因此起不来。单用户设备上收紧权限没有实际安全收益。
- 不要加 `windows_names`。它会拒绝创建含 Windows 非法字符的文件名，而 Proton 的 wine prefix（`compatdata`）确实会用到这类名字。
- 如果 Windows 那侧开着"快速启动"或处于休眠状态，分区会被判定为脏，挂载后变成只读 —— 需要到 Windows 里关闭快速启动并完整关机。
- 首次应用（或改动 fstab 后）建议重启一次 Steam Deck，确认开机自动挂载生效。
- 插件写入 fstab 前，会剥掉自己上次写的那一段（以注释行 `# managed by ntfs-mount-keeper ...` 标识），**并吸收掉任何指向同一挂载点或同一卷标的手工条目**。同一个挂载点存在两条记录会让 systemd 生成两个互相打架的 mount unit，所以手工加过的行会被合并而不是并存。系统自己的条目不受影响 —— 写入前的校验已经排除了系统目录作为挂载点的可能。

## 实测环境与验证状态

| 项 | 值 |
|---|---|
| 设备 | Steam Deck，2TB NVMe（双系统，Windows 11 + SteamOS） |
| polkit | 126（`rules.d` 的 JS 规则引擎需要 ≥ 0.106） |
| NTFS 驱动 | ntfs-3g 2022.10.3（`findmnt` 显示为 `fuseblk`） |
| 目标卷 | 1.7T NTFS，与 Windows 共享，含 Steam 库 |

跨越 2026-08 的三次 SteamOS 更新验证：

- **自愈已实测触发。** 2026-08-09 的更新清空了 fstab 条目、polkit 规则以及挂载点目录，插件在加载时一次补齐并完成挂载：

  ```
  boot-time apply: {'ok': True, 'steps': ['created /run/media/deck/SSD',
    'restored the /etc/fstab entry', 'restored the udisks2 polkit rule',
    'mounted SSD'], 'errors': []}
  ```

  链路是自洽的：fstab 条目没了，systemd 开机时无从挂载，`/run` 又是 tmpfs 所以挂载点目录同样不存在，四个步骤依次补回。

- **并非每次更新都会覆盖 `/etc`。** 同期另外几次更新后 `steps` 均为空 —— 配置还在，插件检查完就收手，不做任何写入。幂等按预期工作。

- 配置未被破坏时，开机由 fstab 挂载 —— `systemctl status run-media-deck-SSD.mount` 显示 `loaded (/etc/fstab; generated)`，journal 里 ntfs-3g 收到的是 `rw,uid=1000,gid=1000,umask=000`
- 卷上文件属主为 `deck`、权限 0777，执行位完整保留
- 写入去重、危险挂载点拒绝、`nofail` 强制补回、写入后回滚，均以真实 `fstab` 文件测试过
- Decky Loader 本体在这几次更新中均存活。若某次它没能挺过来，`main.py --apply` 这条不依赖 Decky 的路径仍然可用。

> 判据提示：`findmnt` 输出里的 `user_id=`/`group_id=` 是 FUSE 记录的**挂载执行者身份**，udisks2 和 systemd 都以 root 执行，因此该字段无法区分卷是被谁挂上的。要判断是否由 fstab 挂载，看 mount unit 的 `Loaded:` 行。

## 已知限制

- 前端依赖 `@decky/ui`，而它的组件是运行时从 Steam 客户端的 webpack 模块里按特征字符串挖出来的。**Steam 客户端更新（独立于 SteamOS 更新，且更频繁）可能打断 UI**。真出问题一般是升级 `@decky/ui` 重新构建即可，且后端的自愈逻辑不依赖 UI —— 面板打不开时开机挂载照常工作。
- Steam 官方不支持把 NTFS 卷作为 SteamOS 侧的 Steam 库（缺少符号链接、大小写敏感和 Unix 权限）。共享 Windows 游戏的文件没问题，当成 SteamOS 的正式库用会踩坑。本插件只负责把卷挂上去。

## 许可

MIT，见 [LICENSE](LICENSE)。
