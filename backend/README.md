# loom-backend

```bash
uv run uvicorn app.main:app --reload --port 8080
```

配置：

- `backend/config.json`（参考 `backend/config.example.json`），或
- `backend/.env`（参考 `backend/.env.example`，环境变量前缀 `LOOM_`）

人设与提示词模板：

- `backend/pc_config/personas.py`
- `backend/pc_config/prompts.json`
