# AGENTS.md (Repo: loom)

本文件面向在此仓库中工作的编码代理/协作者，描述默认约束、目录约定与工作方式。

## 默认约束

- **小步提交**：优先可运行/可测试的增量改动，避免大重构。
- **尊重既有模式**：先阅读现有代码与目录约定，再实现功能。
- **清晰可读**：代码直白优先，避免“聪明但难维护”的写法。
- **依赖安装需用户执行**：由于代理可能无网络权限，任何可能联网/下载依赖的操作（如 `npm install`、`uv sync`/`pip install`）由用户执行。

## 目录约定

- `plan/`：需求/设计草案（不纳入版本控制）。当前开发方向见 `demo-direction-forum-queue.md`与`forum-technique.md`
- `backend/`：Python/FastAPI + WebSocket + SQLite。
- `frontend/`：React + Vite。

## 工作方式

- 搜索优先用 `rg`，列文件优先用 `fd`（若不可用再用 `find/grep`）。
- 修改文件一律用补丁方式（`apply_patch`），避免临时脚本批量改写。
- 变更完成后尽量做**最小验证**：Python 用 `python3 -m compileall`；前端用 `npm run build`（均需依赖已安装）。
- 不修与当前任务无关的测试/格式问题；必要时在说明里标注风险点与下一步建议。
- 当你进行重构任务，或需要修改大量行数时，尽量小块`apply_patch`，减少patch失败或SSE超时风险。

## UI

- 常用按钮无需文字，使用lucide-react图标。