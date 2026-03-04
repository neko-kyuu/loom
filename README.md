# loom (demo v0)

Demo v0 目标见 `plan/demo.md`。

## 结构

- `backend/`：FastAPI + WebSocket + SQLite（先用 fake 引擎跑通消息流）
- `frontend/`：React + Vite（Discord 风格消息 UI 的最小骨架）

## 本地启动

### 后端

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8080
```

配置可用 `backend/config.json`（参考 `backend/config.example.json`）或 `backend/.env`（参考 `backend/.env.example`）。
论坛频道的 demo 公告贴可用 `backend/demo_forum_seed.json` 控制（参考 `backend/demo_forum_seed.example.json`）。

### 前端

```bash
cd frontend
npm install
npm run dev
```

默认前端会连 `ws://localhost:8080/ws`，后端会允许 `http://localhost:5173` 的 CORS。

说明：`npm install` / `uv` 安装依赖会联网下载包（我这边不会自动执行）。
