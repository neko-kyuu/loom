import { useEffect, useMemo, useState } from "react";
import { Copy, Plus, RotateCcw, Trash2, X } from "lucide-react";
import type { Appearance, CustomTheme, CustomThemeColors } from "../../lib/appearance";
import {
  createNewCustomTheme,
  DEFAULT_CUSTOM,
  duplicateCustomTheme,
  isHexColor,
  PRESET_OPTIONS,
  safeHex
} from "../../lib/appearance";

export default function AppearanceTab(props: {
  open: boolean;
  appearance: Appearance;
  setAppearance: (next: Appearance | ((prev: Appearance) => Appearance)) => void;
  customThemes: CustomTheme[];
  setCustomThemes: (next: CustomTheme[] | ((prev: CustomTheme[]) => CustomTheme[])) => void;
  onRequestClose: () => void;
}) {
  const [selectedCustomId, setSelectedCustomId] = useState<string | null>(null);

  useEffect(() => {
    if (!props.open) return;
    if (props.appearance.mode === "custom") setSelectedCustomId(props.appearance.customId);
    else setSelectedCustomId(props.customThemes[0]?.id ?? null);
  }, [props.open, props.appearance, props.customThemes]);

  const selectedCustomTheme = useMemo(() => {
    if (!selectedCustomId) return null;
    return props.customThemes.find((t) => t.id === selectedCustomId) ?? null;
  }, [props.customThemes, selectedCustomId]);

  function updateCustomTheme(themeId: string, updater: (t: CustomTheme) => CustomTheme) {
    props.setCustomThemes((prev) => prev.map((t) => (t.id === themeId ? updater(t) : t)));
  }

  function createTheme(base?: CustomThemeColors) {
    const theme = createNewCustomTheme(props.customThemes, base);
    props.setCustomThemes((prev) => [...prev, theme]);
    setSelectedCustomId(theme.id);
    props.setAppearance({ mode: "custom", customId: theme.id });
  }

  function duplicateSelected() {
    if (!selectedCustomTheme) return;
    const theme = duplicateCustomTheme(selectedCustomTheme);
    props.setCustomThemes((prev) => [...prev, theme]);
    setSelectedCustomId(theme.id);
    props.setAppearance({ mode: "custom", customId: theme.id });
  }

  function deleteSelected() {
    if (!selectedCustomTheme) return;
    const deletingId = selectedCustomTheme.id;
    const remaining = props.customThemes.filter((t) => t.id !== deletingId);
    props.setCustomThemes(remaining);
    setSelectedCustomId((prev) => (prev === deletingId ? remaining[0]?.id ?? null : prev));
    props.setAppearance((prev) =>
      prev.mode === "custom" && prev.customId === deletingId ? { mode: "preset", preset: "darkgray" } : prev
    );
  }

  return (
    <>
      <div className="modalHeader">
        <div className="modalHeaderTitle">外观</div>
        <button className="iconBtn iconOnly" onClick={props.onRequestClose} aria-label="关闭" title="关闭">
          <X size={16} />
        </button>
      </div>

      <div className="modalContent">
        <div className="settingsSubTitle">默认主题</div>
        <div className="themeGrid">
          {PRESET_OPTIONS.map((opt) => {
            const active = props.appearance.mode === "preset" && props.appearance.preset === opt.id;
            return (
              <button
                key={opt.id}
                className={`themeCard ${active ? "active" : ""}`}
                onClick={() => props.setAppearance({ mode: "preset", preset: opt.id })}
              >
                <span className="themeSwatches" aria-hidden="true">
                  {opt.swatches.map((c) => (
                    <span key={c} className="dot" style={{ background: c }} />
                  ))}
                </span>
              </button>
            );
          })}
        </div>

        <div className="customThemeHeader">
          <div className="settingsSubTitle">自定义主题</div>
          <div className="customThemeActions">
            <button
              className="iconBtn iconOnly"
              onClick={() => createTheme(selectedCustomTheme?.colors ?? DEFAULT_CUSTOM)}
              aria-label="新增"
              title="新增"
            >
              <Plus size={16} />
            </button>
            <button
              className="iconBtn iconOnly"
              disabled={!selectedCustomTheme}
              onClick={duplicateSelected}
              aria-label="复制"
              title="复制"
            >
              <Copy size={16} />
            </button>
            <button
              className="iconBtn iconOnly danger"
              disabled={!selectedCustomTheme}
              onClick={deleteSelected}
              aria-label="删除"
              title="删除"
            >
              <Trash2 size={16} />
            </button>
          </div>
        </div>

        {props.customThemes.length ? (
          <div className="customThemeGrid">
            {props.customThemes.map((t) => {
              const inUse = props.appearance.mode === "custom" && props.appearance.customId === t.id;
              const selected = selectedCustomId === t.id;
              return (
                <button
                  key={t.id}
                  className={`customThemeCard ${inUse ? "active" : ""} ${selected ? "selected" : ""}`}
                  onClick={() => {
                    setSelectedCustomId(t.id);
                    props.setAppearance({ mode: "custom", customId: t.id });
                  }}
                >
                  <span className="themeSwatches" aria-hidden="true">
                    <span className="dot" style={{ background: safeHex(t.colors.bg, DEFAULT_CUSTOM.bg) }} />
                    <span className="dot" style={{ background: safeHex(t.colors.panel, DEFAULT_CUSTOM.panel) }} />
                    <span className="dot" style={{ background: safeHex(t.colors.accent, DEFAULT_CUSTOM.accent) }} />
                  </span>
                  {inUse ? <span className="tag">使用中</span> : null}
                </button>
              );
            })}
          </div>
        ) : (
          <div className="emptyNote">还没有自定义主题，点“新增”创建。</div>
        )}

        {selectedCustomTheme ? (
          <div className="customEditor">
            <div className="settingsSubTitle">编辑</div>
            <label className="nameRow">
              <span className="customLabel">名称</span>
              <input
                className="customInput"
                value={selectedCustomTheme.name}
                onChange={(e) => {
                  const next = e.target.value;
                  updateCustomTheme(selectedCustomTheme.id, (t) => ({ ...t, name: next }));
                }}
              />
            </label>
            <div className="customGrid">
              {(
                [
                  ["bg", "背景"],
                  ["panel", "侧栏/面板"],
                  ["text", "文字"],
                  ["accent", "强调色"]
                ] as Array<[keyof CustomThemeColors, string]>
              ).map(([key, label]) => {
                const value = selectedCustomTheme.colors[key];
                return (
                  <label key={key} className="customRow">
                    <span className="customLabel">{label}</span>
                    <input
                      className="customColor"
                      type="color"
                      value={isHexColor(value) ? value : DEFAULT_CUSTOM[key]}
                      onChange={(e) => {
                        const next = e.target.value;
                        updateCustomTheme(selectedCustomTheme.id, (t) => ({
                          ...t,
                          colors: { ...t.colors, [key]: next }
                        }));
                      }}
                    />
                    <input
                      className="customInput"
                      value={value}
                      onChange={(e) => {
                        const next = e.target.value;
                        updateCustomTheme(selectedCustomTheme.id, (t) => ({
                          ...t,
                          colors: { ...t.colors, [key]: next }
                        }));
                      }}
                    />
                  </label>
                );
              })}
              <div className="customActions">
                <button
                  className="iconBtn iconOnly"
                  onClick={() => {
                    updateCustomTheme(selectedCustomTheme.id, (t) => ({
                      ...t,
                      colors: { ...DEFAULT_CUSTOM }
                    }));
                  }}
                  aria-label="重置颜色"
                  title="重置颜色"
                >
                  <RotateCcw size={16} />
                </button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </>
  );
}
