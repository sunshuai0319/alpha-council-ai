# PostgreSQL 迁移到服务器

脚本位置：`backend/scripts/migrate_postgres.py`。

脚本使用 PostgreSQL 官方的 `pg_dump` 和 `pg_restore`，会迁移当前
`DATABASE_URL` 对应数据库中的 schema、数据、索引、约束、序列和扩展。
源数据库只读，不会被修改。脚本不会把数据库密码放进命令行参数，而是通过
`PGPASSWORD` 传给 PostgreSQL 客户端。

## 前置条件

当前执行机器需要安装与 PATH 中可找到的 `pg_dump`、`pg_restore`，并且服务器
`118.145.245.39` 已放行 PostgreSQL 端口。目标数据库用户需要有连接、建表和
写入权限；使用 `--create-database` 时还需要创建数据库权限。

`pg_dump` 的主版本不能低于源 PostgreSQL 服务端主版本。例如源库是 18.x，
本机 17.x 的 `pg_dump` 会在真正导出前拒绝执行。脚本会先做版本预检，避免在
导出失败前创建目标数据库。

## 迁移步骤

在 `backend` 目录执行。脚本默认从 `DATABASE_URL` 读取源库，并保留源 URL 的
用户名、密码、端口、数据库名和连接参数，只将主机改为 `118.145.245.39`：

```bash
uv run python scripts/migrate_postgres.py --create-database
```

如果本机客户端版本低于源库，且 Docker Desktop 正在运行，可以直接使用
Docker Hub 的 PostgreSQL 18 客户端：

```bash
uv run python scripts/migrate_postgres.py \
  --create-database \
  --pg-client-image postgres:18
```

该模式会用同一个镜像执行 `pg_dump` 和 `pg_restore`，并将临时 dump 文件挂载
到容器中；数据库密码仍通过环境变量传递，不会进入命令行参数。也可以安装
PostgreSQL 18 客户端后，不需要 Docker，直接用 `--pg-dump` 和 `--pg-restore`
指定对应二进制路径。例如 Postgres.app：

```bash
uv run python scripts/migrate_postgres.py \
  --create-database \
  --pg-dump /Applications/Postgres.app/Contents/Versions/18/bin/pg_dump \
  --pg-restore /Applications/Postgres.app/Contents/Versions/18/bin/pg_restore
```

PostgreSQL 官方 macOS 下载页提供 18.x 安装包；安装后也可以把对应 `bin` 目录
加入 PATH，再直接运行不带这两个参数的命令。

默认要求目标数据库为空。如果目标库已存在且确认要清理其中与源库同名的对象，
才使用：

```bash
uv run python scripts/migrate_postgres.py --overwrite-target
```

也可以显式指定完整目标 URL（例如目标数据库名或端口不同）：

```bash
uv run python scripts/migrate_postgres.py \
  --target-url 'postgresql+psycopg://<user>:<password>@118.145.245.39:5432/<database>' \
  --create-database
```

迁移成功后，将 `backend/.env` 中 `DATABASE_URL` 的主机改为
`118.145.245.39`，其他配置保持不变，然后重启 API 和 worker。执行 Alembic
升级前先确认应用已指向服务器数据库：

```bash
uv run alembic upgrade head
```

不要把带真实密码的 URL 写入 Git、脚本或文档；`backend/.env.example` 只保留
占位值。
