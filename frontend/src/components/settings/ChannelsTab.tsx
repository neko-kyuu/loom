import { Plus, Trash2, X } from "lucide-react";
import type { ChannelsState } from "../../lib/channels";
import {
  newForumChannelId,
  updateBroadcastDescription,
  updateBroadcastGroup,
  updateForumChannelDescription,
  updateForumChannelGroup,
  updateForumChannelTitle
} from "../../lib/channels";

export default function ChannelsTab(props: {
  open: boolean;
  channels: ChannelsState;
  setChannels: (next: ChannelsState | ((prev: ChannelsState) => ChannelsState)) => void;
  onRequestClose: () => void;
}) {
  return (
    <>
      <div className="modalHeader">
        <div className="modalHeaderTitle">频道</div>
        <button className="iconBtn iconOnly" onClick={props.onRequestClose} aria-label="关闭" title="关闭">
          <X size={16} />
        </button>
      </div>

      <div className="modalContent">
        <div className="settingsSubTitle">固定频道</div>
        <div className="emptyNote">
          <div>
            <strong>#broadcast</strong> 为闲聊/广播频道：固定存在、不可删除。
          </div>
        </div>
        <label className="channelsDescBlock">
          <div className="channelsDescLabel">分组（可选，用于侧边栏聚合）</div>
          <input
            className="customInput"
            value={props.channels.broadcast.group || ""}
            placeholder="例如：rp专区"
            onChange={(e) => {
              props.setChannels((prev) => updateBroadcastGroup(prev, e.target.value));
            }}
          />
        </label>
        <label className="channelsDescBlock">
          <div className="channelsDescLabel">描述（给 DM 用，不在聊天界面显示）</div>
          <textarea
            className="customInput channelsTextarea"
            value={props.channels.broadcast.description}
            placeholder="例如：闲聊/广播频道，用于随手讨论与即时沟通"
            onChange={(e) => {
              props.setChannels((prev) => updateBroadcastDescription(prev, e.target.value));
            }}
          />
        </label>

        <div className="channelsHeader">
          <div className="settingsSubTitle">论坛频道（kind=&quot;forum&quot;）</div>
          <button
            className="iconBtn"
            onClick={() => {
              const id = newForumChannelId();
              props.setChannels((prev) => ({ broadcast: prev.broadcast, forums: [...prev.forums, { id, title: "#new", description: "" }] }));
            }}
            aria-label="新增论坛频道"
            title="新增论坛频道"
          >
            <Plus size={16} />
          </button>
        </div>

        {props.channels.forums.length ? (
          <div className="channelsList">
            {props.channels.forums.map((c) => (
              <div key={c.id} className="channelsRow">
                <div className="channelsId">{c.id}</div>
                <div className="channelsFields">
                  <input
                    className="customInput"
                    value={c.group || ""}
                    placeholder="分组（可选）例如：美食专区"
                    onChange={(e) => {
                      const v = e.target.value;
                      props.setChannels((prev) => updateForumChannelGroup(prev, c.id, v));
                    }}
                  />
                  <input
                    className="customInput"
                    value={c.title}
                    placeholder="#trade"
                    onChange={(e) => {
                      const v = e.target.value;
                      props.setChannels((prev) => updateForumChannelTitle(prev, c.id, v));
                    }}
                  />
                  <textarea
                    className="customInput channelsTextarea"
                    value={c.description}
                    placeholder="描述（给 DM 用，不在聊天界面显示）"
                    onChange={(e) => {
                      props.setChannels((prev) => updateForumChannelDescription(prev, c.id, e.target.value));
                    }}
                  />
                </div>
                <button
                  className="iconBtn iconOnly danger"
                  onClick={() => {
                    props.setChannels((prev) => ({ broadcast: prev.broadcast, forums: prev.forums.filter((x) => x.id !== c.id) }));
                  }}
                  aria-label="删除"
                  title="删除"
                >
                  <Trash2 size={16} />
                </button>
              </div>
            ))}
          </div>
        ) : (
          <div className="emptyNote">还没有论坛频道，点“新增”创建。</div>
        )}

        <div className="channelsHint">
          说明：论坛频道用于聚合主题内容；thread/发言路由由 DM 决定。当前仅管理频道列表，thread 仍为假数据。
        </div>
      </div>
    </>
  );
}
