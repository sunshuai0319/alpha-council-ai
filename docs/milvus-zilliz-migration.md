# Milvus 迁移到 Zilliz

迁移脚本位于 `backend/scripts/migrate_milvus_to_zilliz.py`，只处理当前项目配置的一个集合，不会枚举或迁移其他集合。

## 行为

- 源集合默认读取 `MILVUS_COLLECTION`，未设置时使用 `alpha_council_documents_bge_m3_v1`。
- RAG v2 的 `asset_scope` / `schema_version` 需要先通过
  `backend/scripts/reindex_documents.py` 写入新 collection，再切换 `MILVUS_COLLECTION`；不要
  直接在现有 v1 collection 上改 schema。
- 目标集合默认使用相同名称，也可以通过 `ZILLIZ_COLLECTION` 或 `--target-collection` 指定。
- 目标集合不存在时，复制源集合的字段和索引配置后创建。
- 使用主键判断重复记录。目标中已存在的记录跳过，不会覆盖目标已有内容。
- 源端同一主键重复出现时只插入第一次出现的记录。
- 源端 `VarChar` 的 `max_length` 恰好为 `32` 时，目标默认扩大为 `128`；可通过 `--varchar-32-max-length` 调整。
- 迁移会复制向量字段和标量字段，数据插入目标集合的默认分区。

如果目标集合已经存在，但字段、类型、向量维度或主键不匹配，脚本会直接中止。特别是目标仍为 `VarChar(32)` 时，脚本不会尝试删除或修改目标集合，而是提示使用新的目标集合名称，避免破坏已有数据。

## 使用方式

在 `backend/` 目录执行。凭据只通过环境变量传入，不要写入脚本、文档或提交记录：

```bash
export MILVUS_URI='http://127.0.0.1:19530'
export MILVUS_USER='root'
export MILVUS_PASSWORD='<本地 Milvus 密码>'
export MILVUS_DB_NAME='default'
export MILVUS_COLLECTION='alpha_council_documents_bge_m3_v1'

export ZILLIZ_URI='https://<你的 Zilliz Public Endpoint>'
export ZILLIZ_TOKEN='<你的 Zilliz Token>'
# ZILLIZ_DB_NAME 通常留空即可；只有使用了特定数据库时才设置。

uv run python scripts/migrate_milvus_to_zilliz.py
```

脚本成功后会输出 JSON 统计，例如：

```json
{
  "source_rows": 1200,
  "inserted": 1200,
  "skipped_existing": 0,
  "skipped_source_duplicates": 0
}
```

可以安全地重复执行；再次执行时，已有主键会进入 `skipped_existing`，不会重复插入。

如果需要更大的字符串长度或更小的批次：

```bash
uv run python scripts/migrate_milvus_to_zilliz.py \
  --varchar-32-max-length 256 \
  --batch-size 200
```

如果目标集合使用不同名称：

```bash
uv run python scripts/migrate_milvus_to_zilliz.py \
  --target-collection alpha_council_documents_bge_m3_v1_migrated
```

迁移并校验完成后，如果要让应用改用 Zilliz，只需在 `backend/.env` 设置 `USE_ZILLIZ=true`。应用会自动读取现有的 `ZILLIZ_*` 配置；`ZILLIZ_COLLECTION` 为空时会回退到 `MILVUS_COLLECTION`。设置为 `false` 即切回本地 Milvus。

你在消息中贴出的 Zilliz 凭据已经属于公开暴露状态，建议迁移完成后在 Zilliz 控制台轮换 Token 和密码，并只使用新凭据运行脚本。
