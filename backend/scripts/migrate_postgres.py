"""Migrate the application's PostgreSQL database to another PostgreSQL host.

The migration uses PostgreSQL's native ``pg_dump``/``pg_restore`` tools so
that schema, data, indexes, constraints, sequences, and extensions are copied
as one consistent dump.  The source database is never modified.

By default the target database must already exist and be empty.  Use
``--create-database`` when the target database does not exist, or
``--overwrite-target`` when an existing target should be replaced during
restore.  The latter is intentionally explicit because ``pg_restore --clean``
can remove objects from the target database.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url

DEFAULT_TARGET_HOST = "118.145.245.39"
DEFAULT_CONNECT_TIMEOUT = 10


class MigrationError(RuntimeError):
    """Raised when PostgreSQL migration preconditions are not satisfied."""


def _load_env_file(path: Path = Path(".env")) -> None:
    """Load simple KEY=value entries without overriding shell environment values."""

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def parse_pg_major(version_output: str) -> int:
    """Extract a PostgreSQL major version from client or server version text."""

    matches = re.findall(r"(?:^|[^\d])(\d+)(?:\.\d+)+", version_output)
    if not matches:
        raise MigrationError(f"Cannot parse PostgreSQL version from {version_output!r}")
    return int(matches[-1])


def replace_database_host(source_url: str, target_host: str) -> URL:
    """Return a target URL with only the hostname changed."""

    if not target_host.strip():
        raise ValueError("target_host must not be empty")
    return make_url(source_url).set(host=target_host.strip())


def build_pg_command(program: str, database_url: URL, *arguments: str) -> list[str]:
    """Build a pg_dump/pg_restore command without putting its password in argv."""

    command = [program, *arguments]
    if database_url.host:
        command.extend(["--host", database_url.host])
    if database_url.port:
        command.extend(["--port", str(database_url.port)])
    if database_url.username:
        command.extend(["--username", database_url.username])
    if database_url.database:
        command.extend(["--dbname", database_url.database])
    return command


def build_docker_pg_command(
    command: Sequence[str],
    *,
    image: str,
    temporary_directory: Path,
    environment_names: Sequence[str] = (),
) -> list[str]:
    """Run a PostgreSQL client image while mapping the temporary dump path."""

    temporary_directory = temporary_directory.resolve()
    mapped_command: list[str] = []
    for argument in command:
        path = Path(argument)
        if path.is_absolute():
            try:
                relative_path = path.resolve().relative_to(temporary_directory)
            except ValueError:
                pass
            else:
                mapped_command.append(f"/migration/{relative_path.as_posix()}")
                continue
        mapped_command.append(argument)

    docker_command = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{temporary_directory}:/migration",
    ]
    for name in environment_names:
        docker_command.extend(["--env", name])
    docker_command.extend([image, *mapped_command])
    return docker_command


def _query_value(database_url: URL, name: str) -> str | None:
    value = database_url.query.get(name)
    if isinstance(value, tuple):
        return value[0] if value else None
    return str(value) if value is not None else None


def _pg_environment(database_url: URL) -> dict[str, str]:
    """Build libpq environment variables, keeping credentials out of argv."""

    environment = os.environ.copy()
    if database_url.password is not None:
        environment["PGPASSWORD"] = database_url.password
    if sslmode := _query_value(database_url, "sslmode"):
        environment["PGSSLMODE"] = sslmode
    if connect_timeout := _query_value(database_url, "connect_timeout"):
        environment["PGCONNECT_TIMEOUT"] = connect_timeout
    return environment


def _psycopg_kwargs(database_url: URL, *, database: str | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "host": database_url.host,
        "port": database_url.port,
        "user": database_url.username,
        "password": database_url.password,
        "dbname": database or database_url.database,
        "connect_timeout": DEFAULT_CONNECT_TIMEOUT,
    }
    for option in ("sslmode", "sslrootcert", "sslcert", "sslkey", "application_name"):
        if value := _query_value(database_url, option):
            kwargs[option] = value
    return {key: value for key, value in kwargs.items() if value is not None}


def _database_exists(database_url: URL) -> bool:
    maintenance_kwargs = _psycopg_kwargs(database_url, database="postgres")
    with psycopg.connect(**maintenance_kwargs) as connection:
        row = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database_url.database,),
        ).fetchone()
    return row is not None


def _create_database(database_url: URL) -> None:
    if not database_url.database:
        raise MigrationError("Target URL must include a database name")
    maintenance_kwargs = _psycopg_kwargs(database_url, database="postgres")
    with psycopg.connect(**maintenance_kwargs, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} WITH OWNER {}")
            .format(
                sql.Identifier(database_url.database),
                sql.Identifier(database_url.username or "postgres"),
            )
        )


def _user_object_count(database_url: URL) -> int:
    if not database_url.database:
        raise MigrationError("Target URL must include a database name")
    with psycopg.connect(**_psycopg_kwargs(database_url)) as connection:
        row = connection.execute(
            """
            SELECT count(*)
            FROM pg_class AS class
            JOIN pg_namespace AS namespace ON namespace.oid = class.relnamespace
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND class.relkind IN ('r', 'p', 'v', 'm', 'f')
            """
        ).fetchone()
    return int(row[0]) if row else 0


def _require_pg_tools(
    *,
    pg_dump_path: str,
    pg_restore_path: str,
    pg_client_image: str | None,
) -> None:
    if pg_client_image:
        if shutil.which("docker") is None:
            raise MigrationError(
                "--pg-client-image requires Docker CLI; install/start Docker Desktop "
                "or use PostgreSQL 18 client binaries"
            )
        return

    missing = [
        tool
        for tool in (pg_dump_path, pg_restore_path)
        if shutil.which(tool) is None
    ]
    if missing:
        joined = ", ".join(missing)
        raise MigrationError(f"Missing PostgreSQL client tools: {joined}")


def _source_server_major(database_url: URL) -> int:
    with psycopg.connect(**_psycopg_kwargs(database_url)) as connection:
        version = connection.execute("SHOW server_version").fetchone()
    if not version:
        raise MigrationError("Could not read source PostgreSQL server version")
    return parse_pg_major(str(version[0]))


def _local_pg_dump_major(pg_dump_path: str) -> int:
    result = subprocess.run(
        [pg_dump_path, "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_pg_major(result.stdout or result.stderr)


def _validate_pg_dump_version(source: URL, pg_dump_path: str) -> None:
    server_major = _source_server_major(source)
    client_major = _local_pg_dump_major(pg_dump_path)
    if client_major < server_major:
        raise MigrationError(
            f"Source PostgreSQL server is {server_major}.x but pg_dump is {client_major}.x. "
            "Use a PostgreSQL client with the same/newer major version, or rerun with "
            f"--pg-client-image postgres:{server_major}"
        )


def _run_pg_command(
    command: Sequence[str],
    database_url: URL,
    *,
    pg_client_image: str | None = None,
    temporary_directory: Path | None = None,
) -> None:
    environment = _pg_environment(database_url)
    if pg_client_image:
        if temporary_directory is None:
            raise MigrationError("Docker PostgreSQL client requires a temporary directory")
        environment_names = tuple(
            name
            for name in ("PGPASSWORD", "PGSSLMODE", "PGCONNECT_TIMEOUT")
            if name in environment
        )
        command = build_docker_pg_command(
            command,
            image=pg_client_image,
            temporary_directory=temporary_directory,
            environment_names=environment_names,
        )
    subprocess.run(command, check=True, env=environment)


def migrate(
    source_url: str,
    target_url: str,
    *,
    create_database: bool = False,
    overwrite_target: bool = False,
    pg_dump_path: str = "pg_dump",
    pg_restore_path: str = "pg_restore",
    pg_client_image: str | None = None,
) -> dict[str, object]:
    """Dump ``source_url`` and restore it into ``target_url``."""

    source = make_url(source_url)
    target = make_url(target_url)
    if not source.database or not target.database:
        raise MigrationError("Both source and target URLs must include a database name")

    _require_pg_tools(
        pg_dump_path=pg_dump_path,
        pg_restore_path=pg_restore_path,
        pg_client_image=pg_client_image,
    )
    if not pg_client_image:
        _validate_pg_dump_version(source, pg_dump_path)
    if not _database_exists(target):
        if not create_database:
            raise MigrationError(
                f"Target database {target.database!r} does not exist; "
                "rerun with --create-database"
            )
        _create_database(target)

    existing_objects = _user_object_count(target)
    if existing_objects and not overwrite_target:
        raise MigrationError(
            f"Target database is not empty ({existing_objects} user objects); "
            "rerun with --overwrite-target only when replacement is intended"
        )

    with tempfile.TemporaryDirectory(prefix="alpha-council-pg-") as temporary_directory:
        dump_path = Path(temporary_directory) / "database.dump"
        _run_pg_command(
            build_pg_command(
                pg_dump_path,
                source,
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file",
                str(dump_path),
            ),
            source,
            pg_client_image=pg_client_image,
            temporary_directory=Path(temporary_directory),
        )

        restore_arguments = ["--no-owner", "--no-acl", "--exit-on-error"]
        if overwrite_target:
            restore_arguments.extend(["--clean", "--if-exists"])
        restore_arguments.append(str(dump_path))
        _run_pg_command(
            build_pg_command(pg_restore_path, target, *restore_arguments),
            target,
            pg_client_image=pg_client_image,
            temporary_directory=Path(temporary_directory),
        )

    return {
        "source_host": source.host,
        "source_database": source.database,
        "target_host": target.host,
        "target_database": target.database,
        "target_objects_before_restore": existing_objects,
        "overwrite_target": overwrite_target,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-url",
        default=os.getenv("DATABASE_URL", ""),
        help="Source PostgreSQL URL; defaults to DATABASE_URL",
    )
    parser.add_argument(
        "--target-url",
        default=os.getenv("TARGET_DATABASE_URL", ""),
        help="Complete target PostgreSQL URL; defaults to source URL with target host",
    )
    parser.add_argument(
        "--target-host",
        default=os.getenv("POSTGRES_TARGET_HOST", DEFAULT_TARGET_HOST),
        help=f"Target host when --target-url is omitted (default: {DEFAULT_TARGET_HOST})",
    )
    parser.add_argument(
        "--target-port",
        type=int,
        default=int(os.getenv("POSTGRES_TARGET_PORT", "0")) or None,
        help="Optional target port; otherwise preserves the source port",
    )
    parser.add_argument(
        "--target-database",
        default=os.getenv("POSTGRES_TARGET_DATABASE", ""),
        help="Optional target database; otherwise preserves the source database",
    )
    parser.add_argument(
        "--create-database",
        action="store_true",
        help="Create the target database when it does not exist",
    )
    parser.add_argument(
        "--overwrite-target",
        action="store_true",
        help="Allow restoring into a non-empty target and clean existing objects",
    )
    parser.add_argument(
        "--pg-dump",
        default=os.getenv("PG_DUMP_PATH", "pg_dump"),
        help="Path to pg_dump when not using --pg-client-image",
    )
    parser.add_argument(
        "--pg-restore",
        default=os.getenv("PG_RESTORE_PATH", "pg_restore"),
        help="Path to pg_restore when not using --pg-client-image",
    )
    parser.add_argument(
        "--pg-client-image",
        default=os.getenv("POSTGRES_CLIENT_IMAGE", ""),
        help="Docker Hub PostgreSQL image for both clients, e.g. postgres:18",
    )
    return parser


def main() -> int:
    _load_env_file()
    args = _parser().parse_args()
    if not args.source_url:
        raise SystemExit("Missing --source-url or DATABASE_URL")

    if args.target_url:
        target = make_url(args.target_url)
    else:
        target = replace_database_host(args.source_url, args.target_host)
        if args.target_port:
            target = target.set(port=args.target_port)
        if args.target_database:
            target = target.set(database=args.target_database)

    try:
        result = migrate(
            args.source_url,
            target.render_as_string(hide_password=False),
            create_database=args.create_database,
            overwrite_target=args.overwrite_target,
            pg_dump_path=args.pg_dump,
            pg_restore_path=args.pg_restore,
            pg_client_image=args.pg_client_image or None,
        )
    except (MigrationError, OSError, psycopg.Error, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"PostgreSQL migration failed: {exc}") from exc

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
