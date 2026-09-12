from __future__ import annotations

from typing import Any

from pymilvus import DataType

from scripts.migrate_milvus_to_zilliz import (
    build_collection_schema,
    filter_missing_rows,
    migrate_collection,
)


def _collection_description(*, asset_max_length: int = 32) -> dict[str, Any]:
    return {
        "collection_name": "documents",
        "description": "document vectors",
        "auto_id": False,
        "enable_dynamic_field": False,
        "fields": [
            {
                "name": "id",
                "type": int(DataType.VARCHAR),
                "params": {"max_length": 160},
                "is_primary": True,
                "auto_id": False,
            },
            {
                "name": "asset",
                "type": int(DataType.VARCHAR),
                "params": {"max_length": asset_max_length},
            },
            {
                "name": "content",
                "type": int(DataType.VARCHAR),
                "params": {"max_length": 65535},
            },
            {
                "name": "embedding",
                "type": int(DataType.FLOAT_VECTOR),
                "params": {"dim": 3},
            },
        ],
    }


def test_build_collection_schema_expands_varchar_32_fields() -> None:
    schema = build_collection_schema(_collection_description(), varchar_32_max_length=128)

    fields = {field.name: field for field in schema.fields}
    assert fields["asset"].params["max_length"] == 128
    assert fields["content"].params["max_length"] == 65535
    assert fields["embedding"].params["dim"] == 3
    assert fields["id"].is_primary is True


def test_filter_missing_rows_skips_existing_and_duplicate_primary_keys() -> None:
    rows = [
        {"id": "already-there", "content": "old"},
        {"id": "new", "content": "first"},
        {"id": "new", "content": "duplicate-in-source"},
    ]

    missing, skipped_existing, skipped_source_duplicates = filter_missing_rows(
        rows,
        existing_ids={"already-there"},
        primary_field="id",
    )

    assert missing == [{"id": "new", "content": "first"}]
    assert skipped_existing == 1
    assert skipped_source_duplicates == 1


class _Iterator:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.closed = False

    def next(self) -> list[dict[str, Any]]:
        if self._rows:
            rows, self._rows = self._rows, []
            return rows
        return []

    def close(self) -> None:
        self.closed = True


class _FakeMilvusClient:
    def __init__(
        self,
        *,
        description: dict[str, Any],
        existing: bool,
        target_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.description = description
        self.existing = existing
        self.target_rows = target_rows or []
        self.created: dict[str, Any] | None = None
        self.inserted: list[dict[str, Any]] = []
        self.loaded: list[str] = []
        self.flushed: list[str] = []

    def describe_collection(self, collection_name: str) -> dict[str, Any]:
        return self.description

    def has_collection(self, collection_name: str) -> bool:
        return self.existing

    def load_collection(self, collection_name: str) -> None:
        self.loaded.append(collection_name)

    def query_iterator(self, **kwargs: Any) -> _Iterator:
        return _Iterator(self.target_rows)

    def list_indexes(self, collection_name: str) -> list[str]:
        return []

    def prepare_index_params(self) -> Any:
        raise AssertionError("no index should be created in this fixture")

    def create_collection(self, **kwargs: Any) -> None:
        self.created = kwargs
        self.existing = True

    def insert(self, *, collection_name: str, data: list[dict[str, Any]]) -> dict[str, int]:
        self.inserted.extend(data)
        return {"insert_count": len(data)}

    def flush(self, *, collection_name: str) -> None:
        self.flushed.append(collection_name)


def test_migrate_collection_creates_target_and_inserts_all_source_rows() -> None:
    source = _FakeMilvusClient(description=_collection_description(), existing=True)
    target = _FakeMilvusClient(description=_collection_description(), existing=False)
    source.query_iterator = lambda **kwargs: _Iterator(  # type: ignore[method-assign]
        [{"id": "one", "asset": "BTC", "content": "a", "embedding": [1.0, 0.0, 0.0]}]
    )

    result = migrate_collection(
        source,
        target,
        source_collection="documents",
        target_collection="documents",
        batch_size=100,
        varchar_32_max_length=128,
    )

    assert target.created is not None
    assert target.inserted == [
        {"id": "one", "asset": "BTC", "content": "a", "embedding": [1.0, 0.0, 0.0]}
    ]
    assert result.inserted == 1
    assert result.skipped_existing == 0
    assert target.flushed == ["documents"]


def test_migrate_collection_skips_primary_keys_already_in_target() -> None:
    source = _FakeMilvusClient(description=_collection_description(), existing=True)
    target = _FakeMilvusClient(
        description=_collection_description(asset_max_length=128),
        existing=True,
        target_rows=[{"id": "one"}],
    )
    source.query_iterator = lambda **kwargs: _Iterator(  # type: ignore[method-assign]
        [
            {"id": "one", "asset": "BTC", "content": "old", "embedding": [1.0, 0.0, 0.0]},
            {"id": "two", "asset": "ETH", "content": "new", "embedding": [0.0, 1.0, 0.0]},
        ]
    )

    result = migrate_collection(
        source,
        target,
        source_collection="documents",
        target_collection="documents",
        batch_size=100,
        varchar_32_max_length=128,
    )

    assert target.created is None
    assert target.inserted == [
        {"id": "two", "asset": "ETH", "content": "new", "embedding": [0.0, 1.0, 0.0]}
    ]
    assert result.inserted == 1
    assert result.skipped_existing == 1
    assert target.flushed == ["documents"]
