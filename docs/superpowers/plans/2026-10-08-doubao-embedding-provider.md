# Doubao 嵌入模型切换实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:executing-plans` 逐任务实现此计划。

**目标：** 增加可由 env 切换的本地 BGE/Doubao 嵌入 provider，移除本地重排序依赖，并把 Doubao 向量导入独立的 Zilliz 集合。

**架构：** `EMBEDDING_PROVIDER=local|doubao` 统一控制文档入库和查询嵌入；Doubao 通过 Ark 兼容的 `/embeddings` API 批量请求，当前检索直接使用 Milvus 向量分数前 N 条，不再加载 reranker。Doubao 使用独立集合和 1024 维，保留旧 BGE 集合以便 env 切回。

**技术栈：** Pydantic Settings、httpx、Milvus/Zilliz、pytest、uv。

---

### 任务 1：配置 provider 和 Doubao 客户端

**文件：**
- 修改：`backend/app/config.py`
- 修改：`backend/app/rag/embeddings.py`
- 修改：`backend/.env.example`
- 测试：`backend/tests/unit/test_embeddings.py`

- [ ] 为 `Settings` 增加 `embedding_provider`、`doubao_api_key`、`doubao_embedding_model`、`doubao_embedding_dimension` 和超时配置，默认保持本地 BGE。
- [ ] 增加失败测试：Doubao provider 使用配置的 API key、model、base URL，批量请求 `/embeddings` 并按响应 index 返回向量。
- [ ] 运行 `cd backend && uv run pytest tests/unit/test_embeddings.py -q`，确认新测试因 provider 类不存在而失败。
- [ ] 实现 `DoubaoEmbedder`，复用 Ark base URL，限制单次请求不超过 256 条，校验响应数量、维度和错误码，不打印 key。
- [ ] 运行同一测试，确认通过；再运行 `uv run ruff check app/config.py app/rag/embeddings.py tests/unit/test_embeddings.py`。
- [ ] 更新 `.env.example`，只放变量名和示例占位值，不放真实密钥。

### 任务 2：统一 worker 使用嵌入 provider 并去掉重排序

**文件：**
- 修改：`backend/app/rag/retriever.py`
- 修改：`backend/app/workers/pipeline.py`
- 修改：`backend/app/services/cycle.py`
- 修改：`backend/app/rag/milvus.py`
- 测试：`backend/tests/integration/test_rag.py`

- [ ] 增加失败测试：Retriever 在有候选时按 vector score 返回前 `request.limit` 条，不调用 reranker。
- [ ] 运行目标测试确认失败。
- [ ] 修改 Retriever 使 reranker 可选并默认跳过，保留已有候选过滤、asset fallback 和日志；新增 provider factory，让 pipeline 和 cycle 查询使用同一个配置 provider。
- [ ] 将 Milvus collection 的 embedding dimension 从 Settings 读取，默认 1024；文档写入的 `embedding_model` 使用 provider 的标识，不再硬编码 BGE。
- [ ] 运行 RAG 单测、集成测试和 `ruff`，确认无重排序模型加载路径。

### 任务 3：Doubao 独立集合重建入口

**文件：**
- 修改：`backend/scripts/reindex_documents.py`
- 修改：`backend/app/config.py`
- 测试：`backend/tests/unit/test_reindex_documents.py`

- [ ] 增加失败测试：reindex 目标集合默认为 Doubao 集合名，且 target settings 使用 Doubao provider、1024 维和 v2 schema。
- [ ] 运行目标测试确认失败。
- [ ] 让 reindex 使用统一 provider factory 和 `EMBEDDING_MODEL_NAME`，保持旧集合只读、不 drop、不覆盖。
- [ ] 增加 dry-run 输出，能显示 source collection、target collection、provider、dimension、selected 文档数。
- [ ] 运行目标测试和 `ruff`。

### 任务 4：本地 env 配置、重新导入和验证

**文件：**
- 修改：本机未跟踪文件 `backend/.env`
- 修改：`backend/.env.example`

- [ ] 在本地 `backend/.env` 写入 Doubao provider、模型名、API key、1024 维、Zilliz 开关和独立集合名；确认 `git status` 不显示该文件。
- [ ] 运行 `cd backend && uv run python scripts/reindex_documents.py --dry-run --target-collection "$ZILLIZ_COLLECTION"`，确认数据库中存在可重建文档且配置指向 Doubao。
- [ ] 运行实际 reindex，把已 INDEXED 文档导入新的 Doubao 集合；记录 selected/indexed/rows/failed。
- [ ] 使用新集合运行一次最小检索 smoke check，验证向量维度、返回候选、无 reranker 日志和无本地模型加载错误。
- [ ] 运行 `uv run pytest -q`、`uv run ruff check .`，阅读完整输出后再提交。

