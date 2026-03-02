import { useMemo, useState } from "react";
import { Plus, X } from "lucide-react";
import type { Actor } from "../../types";
import type { MoodStatus, NameFont, NameStyle, Profile, ProfilesState } from "../../lib/profiles";
import { defaultProfileForActor, getProfile } from "../../lib/profiles";
import { uploadAssetDataUrl } from "../../lib/api";
import ImageCropModal from "../ImageCropModal";
import ProfileModal from "../ProfileModal";

export default function ProfileTab(props: {
  open: boolean;
  actors: Actor[];
  profiles: ProfilesState;
  setProfiles: (next: ProfilesState | ((prev: ProfilesState) => ProfilesState)) => void;
  selectedId: string;
  onRequestClose: () => void;
}) {
  const [cropReq, setCropReq] = useState<{ kind: "avatar" | "cover"; file: File } | null>(null);
  const [cropBusy, setCropBusy] = useState(false);

  const selectedActor = useMemo(() => {
    const pcs = props.actors.filter((a) => a.kind === "pc" && a.id);
    const items: Actor[] = [
      props.actors.find((a) => a.kind === "user") || { kind: "user", id: "user", name: "You" },
      props.actors.find((a) => a.kind === "dm") || { kind: "dm", id: "dm", name: "DM" },
      ...pcs
    ];
    for (const a of items) {
      const id = a.kind === "user" ? "user" : a.kind === "dm" ? "dm" : a.id || "";
      if (id === props.selectedId) return a;
    }
    return items[0] || null;
  }, [props.actors, props.selectedId]);

  const selectedProfile = useMemo(() => {
    if (!selectedActor) return null;
    return getProfile(props.profiles, selectedActor) || defaultProfileForActor(selectedActor);
  }, [props.profiles, selectedActor]);

  function updateProfile(updater: (p: Profile) => Profile) {
    if (!selectedActor || !selectedProfile) return;
    const id = selectedActor.kind === "user" ? "user" : selectedActor.kind === "dm" ? "dm" : selectedActor.id || "";
    if (!id) return;
    props.setProfiles((prev) => ({ byId: { ...prev.byId, [id]: updater(prev.byId[id] || selectedProfile) } }));
  }

  function updateNameStyle(partial: Partial<NameStyle>) {
    updateProfile((p) => ({ ...p, nameStyle: { ...p.nameStyle, ...partial } }));
  }

  async function uploadCropped(kind: "avatar" | "cover", croppedDataUrl: string) {
    setCropBusy(true);
    try {
      const res = await uploadAssetDataUrl(croppedDataUrl);
      if (kind === "avatar") updateProfile((p) => ({ ...p, avatarUrl: res.url }));
      else updateProfile((p) => ({ ...p, panelCoverUrl: res.url }));
    } finally {
      setCropBusy(false);
    }
  }

  const previewTitle = selectedProfile?.nickname || "";
  const previewSub = selectedProfile?.displayName || "";

  return (
    <>
      <div className="modalHeader">
        <div className="modalHeaderTitle">个人资料</div>
        <button className="iconBtn iconOnly" onClick={props.onRequestClose} aria-label="关闭" title="关闭">
          <X size={16} />
        </button>
      </div>

      <div className="modalContent profileTabContent">
        {selectedProfile && selectedActor ? (
          <div className="profileTabCanvas">
            <div className="profileTabForm">
              <div className="settingsSubTitle">基础信息</div>
              <div className="formGrid">
                <label className="formRow">
                  <span className="formLabel">头像</span>
                  <div className="uploadRow">
                    <input
                      className="customInput"
                      value={selectedProfile.avatarUrl}
                      placeholder="URL 或上传（留空使用首字母）"
                      onChange={(e) => updateProfile((p) => ({ ...p, avatarUrl: e.target.value }))}
                    />
                    <div className="uploadActions">
                      <input
                        className="hiddenFileInput"
                        type="file"
                        accept="image/*"
                        onChange={(e) => {
                          const f = e.target.files?.[0];
                          if (f) setCropReq({ kind: "avatar", file: f });
                          e.currentTarget.value = "";
                        }}
                        title="上传头像"
                      />
                      <button
                        type="button"
                        className="iconBtn iconOnly"
                        aria-label="选择头像文件"
                        title="选择头像文件"
                        onClick={(e) => {
                          const root = (e.currentTarget as HTMLButtonElement).parentElement;
                          const input = root?.querySelector("input[type=file]") as HTMLInputElement | null;
                          input?.click();
                        }}
                      >
                        <Plus size={16} />
                      </button>
                    </div>
                  </div>
                </label>
                <label className="formRow">
                  <span className="formLabel">名称（聊天）</span>
                  <input
                    className="customInput"
                    value={selectedProfile.displayName}
                    onChange={(e) => updateProfile((p) => ({ ...p, displayName: e.target.value }))}
                  />
                </label>
                <label className="formRow">
                  <span className="formLabel">昵称（面板）</span>
                  <input
                    className="customInput"
                    value={selectedProfile.nickname}
                    onChange={(e) => updateProfile((p) => ({ ...p, nickname: e.target.value }))}
                  />
                </label>
                <label className="formRow">
                  <span className="formLabel">标签</span>
                  <input
                    className="customInput"
                    value={selectedProfile.tags.join(", ")}
                    placeholder="逗号分隔，例如：勇敢, 话痨"
                    onChange={(e) =>
                      updateProfile((p) => ({
                        ...p,
                        tags: e.target.value
                          .split(",")
                          .map((x) => x.trim())
                          .filter(Boolean)
                      }))
                    }
                  />
                </label>
              </div>

              <div className="settingsSubTitle">名称样式</div>
              <div className="formGrid">
                <label className="formRow">
                  <span className="formLabel">字体</span>
                  <select
                    className="customSelect"
                    value={selectedProfile.nameStyle.font}
                    onChange={(e) => updateNameStyle({ font: e.target.value as NameFont })}
                  >
                    <option value="system">System</option>
                    <option value="serif">Serif</option>
                    <option value="mono">Mono</option>
                  </select>
                </label>
                <label className="formRow">
                  <span className="formLabel">颜色模式</span>
                  <select
                    className="customSelect"
                    value={selectedProfile.nameStyle.colorMode}
                    onChange={(e) => updateNameStyle({ colorMode: e.target.value as any })}
                  >
                    <option value="solid">纯色</option>
                    <option value="gradient">渐变</option>
                  </select>
                </label>
                {selectedProfile.nameStyle.colorMode === "solid" ? (
                  <label className="formRow">
                    <span className="formLabel">纯色</span>
                    <div className="colorRow">
                      <input
                        className="customColor"
                        type="color"
                        value={selectedProfile.nameStyle.solid}
                        onChange={(e) => updateNameStyle({ solid: e.target.value })}
                      />
                      <input
                        className="customInput"
                        value={selectedProfile.nameStyle.solid}
                        onChange={(e) => updateNameStyle({ solid: e.target.value })}
                      />
                    </div>
                  </label>
                ) : (
                  <>
                    <label className="formRow">
                      <span className="formLabel">渐变起</span>
                      <div className="colorRow">
                        <input
                          className="customColor"
                          type="color"
                          value={selectedProfile.nameStyle.gradientFrom}
                          onChange={(e) => updateNameStyle({ gradientFrom: e.target.value })}
                        />
                        <input
                          className="customInput"
                          value={selectedProfile.nameStyle.gradientFrom}
                          onChange={(e) => updateNameStyle({ gradientFrom: e.target.value })}
                        />
                      </div>
                    </label>
                    <label className="formRow">
                      <span className="formLabel">渐变止</span>
                      <div className="colorRow">
                        <input
                          className="customColor"
                          type="color"
                          value={selectedProfile.nameStyle.gradientTo}
                          onChange={(e) => updateNameStyle({ gradientTo: e.target.value })}
                        />
                        <input
                          className="customInput"
                          value={selectedProfile.nameStyle.gradientTo}
                          onChange={(e) => updateNameStyle({ gradientTo: e.target.value })}
                        />
                      </div>
                    </label>
                  </>
                )}
              </div>

	              <div className="settingsSubTitle">面板外观</div>
	              <div className="formGrid">
	                <label className="formRow">
	                  <span className="formLabel">背景图</span>
	                  <div className="uploadRow">
	                    <input
	                      className="customInput"
	                      value={selectedProfile.panelCoverUrl}
	                      placeholder="URL 或上传（留空使用占位）"
	                      onChange={(e) => updateProfile((p) => ({ ...p, panelCoverUrl: e.target.value }))}
	                    />
	                    <div className="uploadActions">
	                      <input
	                        className="hiddenFileInput"
	                        type="file"
	                        accept="image/*"
	                        onChange={(e) => {
	                          const f = e.target.files?.[0];
	                          if (f) setCropReq({ kind: "cover", file: f });
	                          e.currentTarget.value = "";
	                        }}
	                        title="上传背景图"
	                      />
	                      <button
	                        type="button"
	                        className="iconBtn iconOnly"
	                        aria-label="选择背景图文件"
	                        title="选择背景图文件"
	                        onClick={(e) => {
	                          const root = (e.currentTarget as HTMLButtonElement).parentElement;
	                          const input = root?.querySelector("input[type=file]") as HTMLInputElement | null;
	                          input?.click();
	                        }}
	                      >
	                        <Plus size={16} />
	                      </button>
	                    </div>
	                  </div>
	                </label>
	                <label className="formRow">
	                  <span className="formLabel">背景纯色</span>
	                  <div className="colorRow">
	                    <input
	                      className="customColor"
	                      type="color"
	                      value={selectedProfile.panelCoverColor}
	                      onChange={(e) => updateProfile((p) => ({ ...p, panelCoverColor: e.target.value }))}
	                    />
	                    <input
	                      className="customInput"
	                      value={selectedProfile.panelCoverColor}
	                      placeholder="#rrggbb"
	                      onChange={(e) => updateProfile((p) => ({ ...p, panelCoverColor: e.target.value }))}
	                    />
	                  </div>
	                </label>
	                <label className="formRow">
	                  <span className="formLabel">面板背景色</span>
	                  <div className="colorRow">
	                    <input
	                      className="customColor"
	                      type="color"
	                      value={selectedProfile.panelBgColor}
	                      onChange={(e) => updateProfile((p) => ({ ...p, panelBgColor: e.target.value }))}
	                    />
	                    <input
	                      className="customInput"
	                      value={selectedProfile.panelBgColor}
	                      onChange={(e) => updateProfile((p) => ({ ...p, panelBgColor: e.target.value }))}
	                    />
	                  </div>
	                </label>
	                <label className="formRow">
	                  <span className="formLabel">面板字体色</span>
	                  <div className="colorRow">
	                    <input
	                      className="customColor"
	                      type="color"
	                      value={selectedProfile.panelTextColor}
	                      onChange={(e) => updateProfile((p) => ({ ...p, panelTextColor: e.target.value }))}
	                    />
	                    <input
	                      className="customInput"
	                      value={selectedProfile.panelTextColor}
	                      onChange={(e) => updateProfile((p) => ({ ...p, panelTextColor: e.target.value }))}
	                    />
	                  </div>
	                </label>
	              </div>

              <div className="settingsSubTitle">心情状态</div>
              <div className="formGrid">
                <label className="formRow">
                  <span className="formLabel">小球</span>
                  <select
                    className="customSelect"
                    value={selectedProfile.status}
                    onChange={(e) => updateProfile((p) => ({ ...p, status: e.target.value as MoodStatus }))}
                  >
                    <option value="none">无</option>
                    <option value="green">绿色</option>
                    <option value="yellow">黄色</option>
                    <option value="red">红色</option>
                    <option value="gray">灰色</option>
                    <option value="custom">自定义</option>
                  </select>
                </label>
                {selectedProfile.status === "custom" ? (
                  <label className="formRow">
                    <span className="formLabel">自定义色</span>
                    <div className="colorRow">
                      <input
                        className="customColor"
                        type="color"
                        value={selectedProfile.statusColor}
                        onChange={(e) => updateProfile((p) => ({ ...p, statusColor: e.target.value }))}
                      />
                      <input
                        className="customInput"
                        value={selectedProfile.statusColor}
                        onChange={(e) => updateProfile((p) => ({ ...p, statusColor: e.target.value }))}
                      />
                    </div>
                  </label>
                ) : null}
              </div>

              <div className="profileTabHint">
                上传文件会保存到后端 DB（`/api/assets`），并在资料里存为 URL（默认 `http://localhost:8080`）。
              </div>
            </div>

            <div className="profileTabPreviewFixed">
              <div className="profilePreviewShell">
                <ProfileModal
                  className="preview"
                  profile={selectedProfile}
                  title={previewTitle}
                  subtitle={previewSub}
                  style={{ color: selectedProfile.panelTextColor }}
                  dm={selectedActor.kind === "pc" ? { kind: "preview", placeholder: "私信 PC…" } : { kind: "none" }}
                />
              </div>
            </div>
          </div>
        ) : (
          <div className="emptyNote">未选择。</div>
        )}
      </div>

      <ImageCropModal
        open={Boolean(cropReq)}
        file={cropReq?.file ?? null}
        title={cropReq?.kind === "avatar" ? "裁剪头像" : "裁剪背景图"}
        aspect={cropReq?.kind === "avatar" ? 1 : 300 / 105}
        outWidth={cropReq?.kind === "avatar" ? 256 : 1200}
        outHeight={cropReq?.kind === "avatar" ? 256 : 420}
        outMime={cropReq?.kind === "avatar" ? "image/png" : "image/jpeg"}
        outQuality={0.9}
        onCancel={() => setCropReq(null)}
        onConfirm={(dataUrl) => {
          const kind = cropReq?.kind;
          if (!kind) return;
          setCropReq(null);
          void uploadCropped(kind, dataUrl).catch(() => {});
        }}
      />

      {cropBusy ? <div className="savingToast">正在上传图片…</div> : null}
    </>
  );
}
