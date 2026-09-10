# 部署（Docker Compose）

```
浏览器 ──▶ gateway (nginx :80 → 宿主 ${GATEWAY_PORT:-8888})
                ├── /      ▶ web  (Next.js :3000)
                └── /api/  ▶ api  (uvicorn :8000) ──▶ Postgres / Milvus（外部已有）
                                 worker (workers.scheduler) ──▶ 同上 + WEEX
```

前端与后端同源，因此**不需要任何 CORS 配置**；前端用相对路径 `NEXT_PUBLIC_API_BASE_URL=/api` 调用。

## 前置条件

1. **Postgres 与 Milvus 已在运行**（本方案不启动它们），在 `backend/.env` 里配好 `DATABASE_URL` / `MILVUS_URI`。
2. **BGE 模型已在部署主机上**（约 12.8G，不会打进镜像）。在根 `.env` 里设 `MODEL_DIR`
   指向同时包含 `bge-m3` 与 `bge-reranker-v2-m3` 的目录。注意这是**部署主机**的路径。
3. 两份 env：`cp .env.example .env`、确认 `backend/.env` 存在。

## 启动

```bash
docker compose up -d --build
docker compose logs -f worker      # 交易周期在 worker 里跑
```

访问 `http://<主机>:${GATEWAY_PORT:-8888}`。

## 两个必须知道的坑

### 1. `NEXT_PUBLIC_*` 改了必须重建前端

这些变量由 Next 在**构建时**内联进客户端 bundle，运行时设同名环境变量**无效**。

之前镜像里因此硬编码了 `http://localhost:8000/api` —— 别的机器访问时，浏览器会去请求
**它自己的** localhost，整个控制台拿不到数据。现在通过 build args 传入，代价是：

```bash
docker compose build web && docker compose up -d web
```

改 Clerk 密钥、网关地址后都要这样重建。

### 2. 端口占用

`192.168.0.106` 上 **80 / 3000 / 9000 已被其他服务占用**（3000 是一个无关的 NestJS 应用）。
所以网关默认用 **8888**，可在根 `.env` 里改 `GATEWAY_PORT`。

## Clerk

内网裸 IP **用不了生产实例** —— Clerk 生产实例要求「拥有一个域名 + 能配 DNS 记录」。
内网演示用**开发实例**即可（已在 `192.168.0.107` 的局域网 IP 上实测：中间件握手与登录页
渲染均正常）。

开发实例的限制：**最多 100 个用户**、数据无法迁移到生产实例、Account Portal 显示
`accounts.dev`、官方定位为不适用于生产负载。

注意 Clerk 的资源走其 CDN，本网络实测偶发 `ERR_CONNECTION_CLOSED` 与 6 秒级重定向，
演示前建议先预热一次。
