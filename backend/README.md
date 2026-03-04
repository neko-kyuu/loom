# loom-backend

```bash
uv run uvicorn app.main:app --reload --port 8080
```

配置：

- `backend/config.json`（参考 `backend/config.example.json`），或
- `backend/.env`（参考 `backend/.env.example`，环境变量前缀 `LOOM_`）

论坛频道的 demo 公告贴种子：

- 默认读取 `backend/demo_forum_seed.json`（可在 `config.json` 里用 `demo_forum_seed_path` 修改/关闭）
- 示例：`backend/demo_forum_seed.example.json`
- 若修改了公告内容但页面没变化：种子消息用固定 id 写入且默认不会覆盖已存在记录；可删除 `backend/loom.sqlite3` 或改 `key/thread_id` 生成新帖。

人设与提示词模板：

- `backend/pc_config/personas.py`
- `backend/pc_config/prompts.json`
