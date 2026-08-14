# NovelForge Web

这是原有的 NovelForge Web 版本，完整保留：

- `apps/web`：Next.js 前端；
- `apps/api`：FastAPI 后端；
- `apps/worker`：Redis Worker 与 LangGraph；
- `docker-compose.yml`：PostgreSQL、Redis、MinIO；
- `docs`：产品、架构和开发文档。

在 Windows 上可以双击 `启动NovelForge.cmd`，也可以执行：

```powershell
cd D:\CodeProject\AgentProject\NovelForge\Web
docker compose up -d
```

各子应用的手动启动方式见：

- [`apps/web/README.md`](apps/web/README.md)
- [`apps/api/README.md`](apps/api/README.md)
- [`apps/worker/README.md`](apps/worker/README.md)

