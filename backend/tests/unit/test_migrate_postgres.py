from __future__ import annotations

from pathlib import Path

from scripts.migrate_postgres import (
    build_docker_pg_command,
    build_pg_command,
    parse_pg_major,
    replace_database_host,
)


def test_replace_database_host_preserves_all_other_database_settings() -> None:
    source = "postgresql+psycopg://db_user:p%40ssword@192.168.0.106:5432/alpha_council_ai?sslmode=require"

    target = replace_database_host(source, "118.145.245.39")

    assert target.host == "118.145.245.39"
    assert target.port == 5432
    assert target.username == "db_user"
    assert target.password == "p@ssword"
    assert target.database == "alpha_council_ai"
    assert target.query["sslmode"] == "require"


def test_build_pg_command_does_not_put_password_in_process_arguments() -> None:
    url = replace_database_host(
        "postgresql+psycopg://db_user:secret@192.168.0.106:5432/alpha_council_ai",
        "118.145.245.39",
    )

    command = build_pg_command(
        "pg_dump",
        url,
        "--format=custom",
        "--file",
        "/tmp/alpha.dump",
    )

    assert command[:2] == ["pg_dump", "--format=custom"]
    assert "--host" in command
    assert "118.145.245.39" in command
    assert "--dbname" in command
    assert "alpha_council_ai" in command
    assert "secret" not in command


def test_parse_pg_major_reads_client_version() -> None:
    assert parse_pg_major("pg_dump (PostgreSQL) 18.4") == 18
    assert parse_pg_major("pg_dump (PostgreSQL) 17.5") == 17


def test_build_docker_pg_command_maps_dump_path_and_keeps_password_out_of_argv() -> None:
    url = replace_database_host(
        "postgresql+psycopg://db_user:secret@192.168.0.106:5432/alpha_council_ai",
        "118.145.245.39",
    )
    command = build_pg_command(
        "pg_dump",
        url,
        "--format=custom",
        "--file",
        "/tmp/alpha-council-pg/database.dump",
    )

    docker_command = build_docker_pg_command(
        command,
        image="postgres:18",
        temporary_directory=Path("/tmp/alpha-council-pg"),
    )

    assert docker_command[:3] == ["docker", "run", "--rm"]
    assert "postgres:18" in docker_command
    assert "/migration/database.dump" in docker_command
    assert "secret" not in docker_command
