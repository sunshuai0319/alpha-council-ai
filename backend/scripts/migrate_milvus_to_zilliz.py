"""Copy the project's Milvus collection to Zilliz Cloud.

The script deliberately handles one collection only.  It is safe to re-run:
rows whose primary key already exists in the target are skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from pymilvus import DataType, MilvusClient

logger = logging.getLogger("migrate_milvus_to_zilliz")

DEFAULT_COLLECTION = "alpha_council_documents_bge_m3_v1"
DEFAULT_BATCH_SIZE = 500
DEFAULT_VARCHAR_32_MAX_LENGTH = 128


class MigrationError(RuntimeError):
    """Raised when the source or target collection cannot be migrated safely."""


@dataclass
class MigrationStats:
    source_rows: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    skipped_source_duplicates: int = 0


def _as_data_type(value: Any) -> DataType:
    if isinstance(value, DataType):
        return value
    try:
        return DataType(value)
    except (TypeError, ValueError):
        aliases = {
            "BOOL": DataType.BOOL,
            "INT8": DataType.INT8,
            "INT16": DataType.INT16,
            "INT32": DataType.INT32,
            "INT64": DataType.INT64,
            "FLOAT": DataType.FLOAT,
            "DOUBLE": DataType.DOUBLE,
            "VARCHAR": DataType.VARCHAR,
            "STRING": DataType.VARCHAR,
            "JSON": DataType.JSON,
            "ARRAY": DataType.ARRAY,
            "FLOAT_VECTOR": DataType.FLOAT_VECTOR,
            "BINARY_VECTOR": DataType.BINARY_VECTOR,
            "FLOAT16_VECTOR": DataType.FLOAT16_VECTOR,
            "BFLOAT16_VECTOR": DataType.BFLOAT16_VECTOR,
            "SPARSE_FLOAT_VECTOR": DataType.SPARSE_FLOAT_VECTOR,
        }
        try:
            return aliases[str(value).upper().replace(" ", "_")]
        except KeyError as exc:
            raise MigrationError(f"Unsupported Milvus field type: {value!r}") from exc


def _field_params(field: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key in ("type_params", "params"):
        value = field.get(key)
        if isinstance(value, dict):
            params.update(value)
    if "max_length" in params:
        params["max_length"] = int(params["max_length"])
    if "dim" in params:
        params["dim"] = int(params["dim"])
    if "element_type" in params:
        params["element_type"] = _as_data_type(params["element_type"])
    return params


def build_collection_schema(
    description: dict[str, Any],
    *,
    varchar_32_max_length: int = DEFAULT_VARCHAR_32_MAX_LENGTH,
) -> Any:
    """Build a target schema from ``describe_collection`` output.

    Only fields with the exact source ``max_length=32`` are expanded.  Other
    VarChar limits are kept unchanged, so the target remains compatible with
    the source data and existing application expectations.
    """

    if varchar_32_max_length < 32:
        raise ValueError("varchar_32_max_length must be at least 32")

    schema_kwargs: dict[str, Any] = {
        "auto_id": bool(description.get("auto_id", False)),
        "enable_dynamic_fields": bool(description.get("enable_dynamic_field", False)),
    }
    if description.get("description"):
        schema_kwargs["description"] = description["description"]
    if "enable_namespace" in description:
        schema_kwargs["enable_namespace"] = bool(description["enable_namespace"])
    schema = MilvusClient.create_schema(**schema_kwargs)

    for raw_field in description.get("fields", []):
        field = dict(raw_field)
        datatype = _as_data_type(field.get("type"))
        params = _field_params(field)
        if datatype == DataType.VARCHAR and params.get("max_length") == 32:
            params["max_length"] = varchar_32_max_length

        for key in (
            "is_primary",
            "auto_id",
            "is_partition_key",
            "is_clustering_key",
            "nullable",
            "default_value",
            "is_dynamic",
        ):
            if key in field and field[key] is not None:
                params[key] = field[key]
        if field.get("description"):
            params["description"] = field["description"]
        schema.add_field(field_name=field["name"], datatype=datatype, **params)
    return schema


def _primary_field(description: dict[str, Any]) -> str:
    for field in description.get("fields", []):
        if field.get("is_primary"):
            return str(field["name"])
    primary_name = description.get("primary_field_name")
    if primary_name:
        return str(primary_name)
    raise MigrationError("Source collection has no primary key field")


def filter_missing_rows(
    rows: Iterable[dict[str, Any]],
    *,
    existing_ids: set[Any],
    primary_field: str,
    seen_source_ids: set[Any] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Return rows absent from target, plus duplicate counters.

    ``seen_source_ids`` can be shared across batches so duplicates split over
    multiple query pages are skipped too.
    """

    seen = seen_source_ids if seen_source_ids is not None else set()
    missing: list[dict[str, Any]] = []
    skipped_existing = 0
    skipped_source_duplicates = 0
    for row in rows:
        if primary_field not in row:
            raise MigrationError(f"Source row does not contain primary key {primary_field!r}")
        primary_key = row[primary_field]
        if primary_key in existing_ids:
            skipped_existing += 1
        elif primary_key in seen:
            skipped_source_duplicates += 1
        else:
            missing.append(row)
        seen.add(primary_key)
    return missing, skipped_existing, skipped_source_duplicates


def _query_batches(
    client: Any,
    *,
    collection_name: str,
    output_fields: list[str],
    batch_size: int,
) -> Iterable[list[dict[str, Any]]]:
    iterator = client.query_iterator(
        collection_name=collection_name,
        batch_size=batch_size,
        limit=-1,
        filter="",
        output_fields=output_fields,
    )
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            yield batch
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


def _copy_index_params(source: Any, target: Any, collection_name: str) -> Any | None:
    index_names = source.list_indexes(collection_name=collection_name)
    if not index_names:
        return None

    index_params = target.prepare_index_params()
    for index_name in index_names:
        details = source.describe_index(collection_name, index_name)
        field_name = details.get("field_name")
        if not field_name:
            raise MigrationError(f"Index {index_name!r} has no field_name")
        kwargs: dict[str, Any] = {
            "index_type": details.get("index_type", ""),
            "index_name": details.get("index_name", index_name),
        }
        for key in ("metric_type", "params"):
            if details.get(key) is not None:
                kwargs[key] = details[key]
        index_params.add_index(field_name=field_name, **kwargs)
    return index_params


def _validate_target_schema(
    source_description: dict[str, Any],
    target_description: dict[str, Any],
    *,
    varchar_32_max_length: int,
) -> None:
    source_fields = {field["name"]: field for field in source_description.get("fields", [])}
    target_fields = {field["name"]: field for field in target_description.get("fields", [])}
    if set(source_fields) != set(target_fields):
        raise MigrationError(
            "Target collection schema fields differ from source; "
            "use a new target collection name instead of mixing schemas"
        )

    for name, source_field in source_fields.items():
        target_field = target_fields[name]
        if _as_data_type(source_field.get("type")) != _as_data_type(target_field.get("type")):
            raise MigrationError(f"Target field {name!r} has a different data type")
        source_params = _field_params(source_field)
        target_params = _field_params(target_field)
        if _as_data_type(source_field.get("type")) == DataType.VARCHAR:
            required_length = source_params.get("max_length")
            if required_length == 32:
                required_length = varchar_32_max_length
            if target_params.get("max_length", 0) < required_length:
                raise MigrationError(
                    f"Target VarChar field {name!r} is too small ({target_params.get('max_length')}); "
                    f"need at least {required_length}"
                )
        elif "dim" in source_params and target_params.get("dim") != source_params["dim"]:
            raise MigrationError(f"Target vector field {name!r} has a different dimension")

    if _primary_field(source_description) != _primary_field(target_description):
        raise MigrationError("Target collection has a different primary key field")


def migrate_collection(
    source: Any,
    target: Any,
    *,
    source_collection: str,
    target_collection: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    varchar_32_max_length: int = DEFAULT_VARCHAR_32_MAX_LENGTH,
) -> MigrationStats:
    """Migrate one collection, inserting only primary keys missing in target."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not source.has_collection(collection_name=source_collection):
        raise MigrationError(f"Source collection does not exist: {source_collection}")

    source_description = source.describe_collection(collection_name=source_collection)
    primary_field = _primary_field(source_description)
    target_exists = target.has_collection(collection_name=target_collection)

    target_schema = build_collection_schema(
        source_description,
        varchar_32_max_length=varchar_32_max_length,
    )
    existing_ids: set[Any] = set()
    if target_exists:
        target_description = target.describe_collection(collection_name=target_collection)
        _validate_target_schema(
            source_description,
            target_description,
            varchar_32_max_length=varchar_32_max_length,
        )
        target.load_collection(collection_name=target_collection)
        for batch in _query_batches(
            target,
            collection_name=target_collection,
            output_fields=[primary_field],
            batch_size=batch_size,
        ):
            existing_ids.update(row[primary_field] for row in batch)
    else:
        target.create_collection(
            collection_name=target_collection,
            schema=target_schema,
            index_params=_copy_index_params(source, target, source_collection),
        )

    source.load_collection(collection_name=source_collection)
    field_names = [str(field["name"]) for field in source_description.get("fields", [])]
    if source_description.get("enable_dynamic_field"):
        field_names = ["*"]

    stats = MigrationStats()
    seen_source_ids: set[Any] = set()
    for batch in _query_batches(
        source,
        collection_name=source_collection,
        output_fields=field_names,
        batch_size=batch_size,
    ):
        stats.source_rows += len(batch)
        missing, skipped_existing, skipped_source_duplicates = filter_missing_rows(
            batch,
            existing_ids=existing_ids,
            primary_field=primary_field,
            seen_source_ids=seen_source_ids,
        )
        stats.skipped_existing += skipped_existing
        stats.skipped_source_duplicates += skipped_source_duplicates
        if missing:
            result = target.insert(collection_name=target_collection, data=missing)
            stats.inserted += int(result.get("insert_count", len(missing)))
    if stats.inserted:
        target.flush(collection_name=target_collection)
    return stats


def _client_kwargs(
    *,
    uri: str,
    token: str = "",
    user: str = "",
    password: str = "",
    db_name: str = "",
) -> dict[str, str]:
    kwargs = {"uri": uri}
    if token:
        kwargs["token"] = token
    elif user:
        kwargs["user"] = user
        kwargs["password"] = password
    if db_name:
        kwargs["db_name"] = db_name
    return kwargs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-uri", default=os.getenv("MILVUS_URI", ""))
    parser.add_argument("--source-user", default=os.getenv("MILVUS_USER", ""))
    parser.add_argument("--source-password", default=os.getenv("MILVUS_PASSWORD", ""))
    parser.add_argument("--source-token", default=os.getenv("MILVUS_TOKEN", ""))
    parser.add_argument("--source-db-name", default=os.getenv("MILVUS_DB_NAME", ""))
    parser.add_argument(
        "--source-collection",
        default=os.getenv("MILVUS_COLLECTION", DEFAULT_COLLECTION),
    )
    parser.add_argument("--target-uri", default=os.getenv("ZILLIZ_URI", ""))
    parser.add_argument("--target-user", default=os.getenv("ZILLIZ_USER", ""))
    parser.add_argument("--target-password", default=os.getenv("ZILLIZ_PASSWORD", ""))
    parser.add_argument("--target-token", default=os.getenv("ZILLIZ_TOKEN", ""))
    parser.add_argument("--target-db-name", default=os.getenv("ZILLIZ_DB_NAME", ""))
    parser.add_argument("--target-collection", default=os.getenv("ZILLIZ_COLLECTION", ""))
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("MILVUS_MIGRATION_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))),
    )
    parser.add_argument(
        "--varchar-32-max-length",
        type=int,
        default=int(
            os.getenv("MILVUS_MIGRATION_VARCHAR_32_MAX_LENGTH", str(DEFAULT_VARCHAR_32_MAX_LENGTH))
        ),
        help="New max_length for source VarChar fields whose max_length is exactly 32.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    target_collection = args.target_collection or args.source_collection
    if not args.source_uri:
        raise SystemExit("Missing --source-uri or MILVUS_URI")
    if not args.target_uri:
        raise SystemExit("Missing --target-uri or ZILLIZ_URI")

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    source = MilvusClient(
        **_client_kwargs(
            uri=args.source_uri,
            token=args.source_token,
            user=args.source_user,
            password=args.source_password,
            db_name=args.source_db_name,
        )
    )
    target = MilvusClient(
        **_client_kwargs(
            uri=args.target_uri,
            token=args.target_token,
            user=args.target_user,
            password=args.target_password,
            db_name=args.target_db_name,
        )
    )
    stats = migrate_collection(
        source,
        target,
        source_collection=args.source_collection,
        target_collection=target_collection,
        batch_size=args.batch_size,
        varchar_32_max_length=args.varchar_32_max_length,
    )
    print(json.dumps(asdict(stats), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
