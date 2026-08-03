import {
  ButtonItem,
  Dropdown,
  DropdownOption,
  Field,
  PanelSection,
  PanelSectionRow,
  TextField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaHdd } from "react-icons/fa";

interface Config {
  label: string;
  mountpoint: string;
  fstype: string;
  options: string;
  apply_on_boot: boolean;
  manage_polkit: boolean;
}

interface State {
  root: boolean;
  config: Config;
  entry: string;
  fstab_ok: boolean;
  polkit_ok: boolean;
  mountpoint_ok: boolean;
  device: string | null;
  mounted: boolean;
  mount_source: string | null;
  has_password: boolean;
  password_saved: boolean;
}

interface Volume {
  name: string;
  label: string;
  fstype: string;
  size: string;
  mountpoint: string | null;
}

interface Result {
  ok: boolean;
  steps: string[];
  errors: string[];
}

const getState = callable<[], State>("get_state");
const listVolumes = callable<[], Volume[]>("list_volumes");
const setConfig = callable<[Config], { ok: boolean; config: Config }>("set_config");
const applyNow = callable<[], Result>("apply_now");
const mountNow = callable<[], Result>("mount_now");
const unmountNow = callable<[], Result>("unmount_now");
const removeAll = callable<[], Result>("remove_all");
const setPassword = callable<[string, boolean], Result>("set_password");

function report(title: string, result: Result) {
  const body = result.ok
    ? result.steps.length
      ? result.steps.join("；")
      : "已是目标状态，无需改动"
    : result.errors.join("；");
  toaster.toast({ title, body });
}

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [volumes, setVolumes] = useState<Volume[]>([]);
  const [busy, setBusy] = useState(false);
  const [password, setPasswordInput] = useState("");
  const [remember, setRemember] = useState(false);

  const refresh = async () => {
    setState(await getState());
    setVolumes(await listVolumes());
  };

  useEffect(() => {
    void refresh();
  }, []);

  if (!state) {
    return (
      <PanelSection title="NTFS Mount Keeper">
        <PanelSectionRow>读取状态中…</PanelSectionRow>
      </PanelSection>
    );
  }

  const cfg = state.config;

  const patch = async (changes: Partial<Config>) => {
    const next = { ...cfg, ...changes };
    setState({ ...state, config: next });
    await setConfig(next);
    setState(await getState());
  };

  const run = async (title: string, fn: () => Promise<Result>) => {
    setBusy(true);
    try {
      report(title, await fn());
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const ok = (v: boolean) => (v ? "✔ 正常" : "✘ 缺失");

  const volumeOptions: DropdownOption[] = [
    ...volumes.map((v) => ({
      data: v.label,
      label: `${v.label} (${v.fstype}, ${v.size})`,
    })),
    ...(cfg.label && !volumes.some((v) => v.label === cfg.label)
      ? [{ data: cfg.label, label: `${cfg.label} (当前未接入)` }]
      : []),
  ];

  return (
    <>
      <PanelSection title="状态">
        <PanelSectionRow>
          <Field label="插件权限" focusable>
            {state.root ? "✔ root" : "✘ 非 root，需在下方填密码"}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="/etc/fstab 条目" focusable>
            {ok(state.fstab_ok)}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="polkit 免密规则" focusable>
            {cfg.manage_polkit ? ok(state.polkit_ok) : "— 未启用"}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="设备" focusable>
            {state.device ?? `未检测到卷标 ${cfg.label}`}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="挂载" focusable>
            {state.mounted ? `✔ 已挂载 (${state.mount_source})` : "✘ 未挂载"}
          </Field>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="操作">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => void run("应用配置", applyNow)}
          >
            立即修复并挂载
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() =>
              void run(state.mounted ? "卸载" : "挂载", state.mounted ? unmountNow : mountNow)
            }
          >
            {state.mounted ? "卸载" : "仅挂载"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => void refresh()}>
            刷新状态
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="配置">
        <PanelSectionRow>
          <ToggleField
            label="开机自动修复"
            description="每次插件加载时检查并写回 fstab 与 polkit 规则，用于抵消系统更新的重置"
            checked={cfg.apply_on_boot}
            onChange={(v) => void patch({ apply_on_boot: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="管理 polkit 免密规则"
            description="让本地活动会话挂载分区时不再弹密码框"
            checked={cfg.manage_polkit}
            onChange={(v) => void patch({ manage_polkit: v })}
          />
        </PanelSectionRow>
        {volumeOptions.length > 0 && (
          <PanelSectionRow>
            <Field label="选择卷标" childrenLayout="below">
              <Dropdown
                rgOptions={volumeOptions}
                selectedOption={cfg.label}
                onChange={(o) => void patch({ label: o.data as string })}
              />
            </Field>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <TextField
            label="卷标 (LABEL)"
            value={cfg.label}
            onChange={(e) => void patch({ label: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="挂载点"
            value={cfg.mountpoint}
            onChange={(e) => void patch({ mountpoint: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="文件系统"
            value={cfg.fstype}
            onChange={(e) => void patch({ fstype: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="挂载选项"
            value={cfg.options}
            onChange={(e) => void patch({ options: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="fstab 行" focusable>
            {state.entry}
          </Field>
        </PanelSectionRow>
      </PanelSection>

      {!state.root && (
        <PanelSection title="sudo 密码">
          <PanelSectionRow>
            <Field focusable>
              插件未以 root 运行，写入系统文件需要 deck 用户的 sudo 密码。
            </Field>
          </PanelSectionRow>
          <PanelSectionRow>
            <TextField
              label="密码"
              bIsPassword
              value={password}
              onChange={(e) => setPasswordInput(e.target.value)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label="记住密码"
              description="以 0600 权限明文存放于插件设置目录，不勾选则仅在本次运行期间有效"
              checked={remember}
              onChange={setRemember}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={busy || !password}
              onClick={() =>
                void run("验证密码", async () => {
                  const r = await setPassword(password, remember);
                  if (r.ok) setPasswordInput("");
                  return r;
                })
              }
            >
              验证并保存
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="卸载配置">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => void run("移除配置", removeAll)}
          >
            从 fstab 与 polkit 中移除
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "NTFS Mount Keeper",
  titleView: <div className={staticClasses.Title}>NTFS Mount Keeper</div>,
  content: <Content />,
  icon: <FaHdd />,
}));
