import type { Actor, ForumPost, ForumThread } from "../types";

export type ForumFakeData = {
  threads: ForumThread[];
  postsByThread: Record<string, ForumPost[]>;
};

function pcActor(pcs: Array<{ id: string; name: string }>, index: number): Actor | null {
  const pc = pcs[index];
  if (!pc) return null;
  return { kind: "pc", id: pc.id, name: pc.name };
}

export function buildForumFakeData(params: {
  channelId: string;
  channelTitle: string;
  pcs: Array<{ id: string; name: string }>;
  nowMs: number;
}): ForumFakeData {
  const { channelId, channelTitle, pcs, nowMs } = params;
  const dm: Actor = { kind: "dm", id: "dm", name: "DM" };
  const p1 = pcActor(pcs, 0);
  const p2 = pcActor(pcs, 1);
  const p3 = pcActor(pcs, 2);

  const postsByThread: Record<string, ForumPost[]> = {
    [`${channelId}:t1`]: [
      {
        id: `${channelId}:t1:p1`,
        thread_id: `${channelId}:t1`,
        timestamp: new Date(nowMs - 1000 * 60 * 60 * 36).toISOString(),
        from_actor: dm,
        content: `这里是 ${channelTitle}（论坛频道）。每个 thread 是一个可追溯的剧情片段；当前先用假数据展示 UI。`
      },
      ...(p1
        ? [
            {
              id: `${channelId}:t1:p2`,
              thread_id: `${channelId}:t1`,
              timestamp: new Date(nowMs - 1000 * 60 * 60 * 20).toISOString(),
              from_actor: p1,
              content: "收到，我会把每次行动的总结放在对应 thread 里。"
            }
          ]
        : []),
      ...(p2
        ? [
            {
              id: `${channelId}:t1:p3`,
              thread_id: `${channelId}:t1`,
              timestamp: new Date(nowMs - 1000 * 60 * 60 * 9).toISOString(),
              from_actor: p2,
              content: "这样回溯会方便很多，尤其是线索和结果整理。"
            }
          ]
        : []),
      ...(p3
        ? [
            {
              id: `${channelId}:t1:p4`,
              thread_id: `${channelId}:t1`,
              timestamp: new Date(nowMs - 1000 * 60 * 8).toISOString(),
              from_actor: p3,
              content: "建议 thread 列表默认按最近活跃排序。"
            }
          ]
        : [])
    ],
    [`${channelId}:t2`]: [
      {
        id: `${channelId}:t2:p1`,
        thread_id: `${channelId}:t2`,
        timestamp: new Date(nowMs - 1000 * 60 * 60 * 18).toISOString(),
        from_actor: dm,
        content: "你们在酒馆里听到有人低声提起“北门的封印”。谁去搭话？"
      },
      ...(p1
        ? [
            {
              id: `${channelId}:t2:p2`,
              thread_id: `${channelId}:t2`,
              timestamp: new Date(nowMs - 1000 * 60 * 60 * 12).toISOString(),
              from_actor: p1,
              content: "我会先观察对方的反应，再以买酒为由接近。"
            }
          ]
        : []),
      ...(p2
        ? [
            {
              id: `${channelId}:t2:p3`,
              thread_id: `${channelId}:t2`,
              timestamp: new Date(nowMs - 1000 * 60 * 60 * 8).toISOString(),
              from_actor: p2,
              content: "我在旁边装作没兴趣，听关键词并记住细节。"
            }
          ]
        : []),
      ...(p3
        ? [
            {
              id: `${channelId}:t2:p4`,
              thread_id: `${channelId}:t2`,
              timestamp: new Date(nowMs - 1000 * 60 * 60 * 3).toISOString(),
              from_actor: p3,
              content: "我会试着问一句：封印什么时候开始松动的？"
            }
          ]
        : []),
      {
        id: `${channelId}:t2:p5`,
        thread_id: `${channelId}:t2`,
        timestamp: new Date(nowMs - 1000 * 60 * 42).toISOString(),
        from_actor: dm,
        content: "陌生人看了你一眼，低声说：‘昨夜。’ 他把一枚刻着纹路的铜币推了过来。"
      }
    ],
    [`${channelId}:t3`]: [
      {
        id: `${channelId}:t3:p1`,
        thread_id: `${channelId}:t3`,
        timestamp: new Date(nowMs - 1000 * 60 * 60 * 8).toISOString(),
        from_actor: dm,
        content: "这里会放一些公开可见的世界观/角色档案，便于新人快速补课。"
      },
      ...(p1
        ? [
            {
              id: `${channelId}:t3:p2`,
              thread_id: `${channelId}:t3`,
              timestamp: new Date(nowMs - 1000 * 60 * 60 * 2).toISOString(),
              from_actor: p1,
              content: `${p1.name}：来自南方港口城市，擅长交涉。`
            }
          ]
        : [])
    ]
  };

  const threadSpecs: Array<{ id: string; title: string }> = [
    { id: `${channelId}:t1`, title: "【公告】先用 thread 来组织剧情片段" },
    { id: `${channelId}:t2`, title: "酒馆里出现了一个陌生人…（线索收集）" },
    { id: `${channelId}:t3`, title: "世界观碎片：各 PC 的公开档案" }
  ];

  const threads: ForumThread[] = threadSpecs
    .map((spec) => {
      const posts = postsByThread[spec.id] || [];
      const created_at = posts[0]?.timestamp || new Date(nowMs).toISOString();
      const last_activity_at = posts.length ? posts[posts.length - 1].timestamp : created_at;
      const reply_count = Math.max(0, posts.length - 1);
      return {
        id: spec.id,
        channel_id: channelId,
        title: spec.title,
        created_at,
        created_by: dm,
        last_activity_at,
        reply_count
      };
    })
    .sort((a, b) => b.last_activity_at.localeCompare(a.last_activity_at));

  return { threads, postsByThread };
}

