#!/usr/bin/env python
"""One-shot database backup runner.

Creates a compressed SQL dump of the configured reactor_ctrl database. The
script is designed for systemd timer execution and reads configuration from
.env or environment variables. It deliberately does not start the Flask app,
so no background reconcilers or hardware workers are launched during backup.

Key settings:
    DATABASE_URL                    mysql+pymysql://user:pass@host:3306/db
    DB_BACKUP_DIR                   target directory for .sql.gz files
    DB_BACKUP_RETENTION_DAYS        delete older dumps after this many days
    DB_BACKUP_DUMP_BINARY           optional mariadb-dump/mysqldump override
    DB_BACKUP_TIMEOUT_SECONDS       maximum dump runtime

Exit codes:
    0 - backup completed
    1 - backup failed
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_BACKUP_DIR = Path("/home/pthuerlemann/backups/reactor_ctrl/sql")
DEFAULT_RETENTION_DAYS = 30
DEFAULT_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class DatabaseConfig:
    username: str
    password: str
    host: str
    port: int
    database: str
    unix_socket: str | None = None


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value.strip())
    except ValueError:
        return default


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-") or "database"


def _parse_database_url(database_url: str) -> DatabaseConfig:
    if not database_url:
        raise ValueError("DATABASE_URL is not configured.")

    parsed = urlparse(database_url)
    if not parsed.scheme.startswith(("mysql", "mariadb")):
        raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")

    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise ValueError("DATABASE_URL does not contain a database name.")

    query = parse_qs(parsed.query)
    unix_socket = query.get("unix_socket", query.get("socket", [None]))[0]

    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username:
        raise ValueError("DATABASE_URL does not contain a database user.")

    return DatabaseConfig(
        username=username,
        password=password,
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        database=database,
        unix_socket=unquote(unix_socket) if unix_socket else None,
    )


def _quote_option_file_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("Database option values must not contain line breaks.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_defaults_file(path: Path, config: DatabaseConfig) -> None:
    lines = [
        "[client]",
        f"user={_quote_option_file_value(config.username)}",
        f"password={_quote_option_file_value(config.password)}",
    ]
    if config.unix_socket:
        lines.append(f"socket={_quote_option_file_value(config.unix_socket)}")
    else:
        lines.extend(
            [
                f"host={_quote_option_file_value(config.host)}",
                f"port={config.port}",
                "protocol=tcp",
            ]
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _find_dump_binary(explicit_binary: str | None) -> str:
    candidates = [explicit_binary] if explicit_binary else ["mariadb-dump", "mysqldump"]
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        candidate_path = Path(candidate)
        if candidate_path.is_file():
            return str(candidate_path)
    raise FileNotFoundError("Neither mariadb-dump nor mysqldump was found.")


def _dump_command(dump_binary: str, defaults_file: Path, database: str) -> list[str]:
    return [
        dump_binary,
        f"--defaults-extra-file={defaults_file}",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--routines",
        "--triggers",
        "--events",
        "--no-tablespaces",
        database,
    ]


def _compress_sql_file(source: Path, target: Path) -> None:
    with source.open("rb") as input_file:
        with target.open("wb") as raw_output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, compresslevel=6) as gzip_output:
                shutil.copyfileobj(input_file, gzip_output)
    target.chmod(0o600)


def _update_latest_symlink(final_path: Path, latest_path: Path) -> None:
    tmp_link = latest_path.with_name(f".{latest_path.name}.{os.getpid()}.tmp")
    try:
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(final_path.name)
        tmp_link.replace(latest_path)
    except OSError as exc:
        print(f"Warning: latest symlink could not be updated: {exc}", file=sys.stderr)
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink(missing_ok=True)


def _prune_old_backups(backup_dir: Path, prefix: str, retention_days: int, now: datetime) -> list[Path]:
    if retention_days <= 0:
        return []

    cutoff = now - timedelta(days=retention_days)
    deleted: list[Path] = []
    for candidate in backup_dir.glob(f"{prefix}.*.sql.gz"):
        if candidate.name.endswith(".latest.sql.gz") or candidate.is_symlink():
            continue
        try:
            modified_at = datetime.fromtimestamp(candidate.stat().st_mtime).astimezone(now.tzinfo)
        except OSError:
            continue
        if modified_at < cutoff:
            candidate.unlink()
            deleted.append(candidate)
    return deleted


def create_database_backup(
    *,
    database_url: str,
    backup_dir: Path,
    retention_days: int,
    dump_binary: str | None,
    timeout_seconds: int,
    dry_run: bool = False,
    now: datetime | None = None,
) -> Path | None:
    config = _parse_database_url(database_url)
    resolved_dump_binary = _find_dump_binary(dump_binary)
    now = now or datetime.now().astimezone()
    prefix = _safe_name(config.database)

    backup_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = backup_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        backup_dir.chmod(0o700)
        tmp_dir.chmod(0o700)
    except OSError as exc:
        print(f"Warning: backup directory permissions could not be tightened: {exc}", file=sys.stderr)

    timestamp = now.strftime("%Y-%m-%d_%H%M%S")
    final_path = backup_dir / f"{prefix}.{timestamp}.sql.gz"
    latest_path = backup_dir / f"{prefix}.latest.sql.gz"

    if dry_run:
        print(f"Dry run: would create {final_path} using {resolved_dump_binary}")
        return None

    old_umask = os.umask(0o077)
    defaults_path: Path | None = None
    tmp_sql_path: Path | None = None
    tmp_gzip_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".mysql-client.", suffix=".cnf", dir=tmp_dir, delete=False) as defaults:
            defaults_path = Path(defaults.name)
        _write_defaults_file(defaults_path, config)

        tmp_sql_path = tmp_dir / f".{prefix}.{timestamp}.{os.getpid()}.sql"
        tmp_gzip_path = tmp_dir / f".{prefix}.{timestamp}.{os.getpid()}.sql.gz"

        command = _dump_command(resolved_dump_binary, defaults_path, config.database)
        print(f"Starting database backup for '{config.database}' into {final_path}")
        with tmp_sql_path.open("wb") as dump_output:
            result = subprocess.run(
                command,
                stdout=dump_output,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout_seconds,
            )

        if result.returncode != 0:
            stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Database dump failed with exit code {result.returncode}: {stderr_text}")

        _compress_sql_file(tmp_sql_path, tmp_gzip_path)
        tmp_gzip_path.replace(final_path)
        final_path.chmod(0o600)
        _update_latest_symlink(final_path, latest_path)
        deleted = _prune_old_backups(backup_dir, prefix, retention_days, now)

        size_bytes = final_path.stat().st_size
        print(f"Database backup completed: {final_path} ({size_bytes} bytes)")
        if deleted:
            print(f"Deleted {len(deleted)} old backup file(s).")
        return final_path
    finally:
        os.umask(old_umask)
        for path in (defaults_path, tmp_sql_path, tmp_gzip_path):
            if path and (path.exists() or path.is_symlink()):
                path.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a compressed reactor_ctrl database dump.")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration without creating a dump.")
    parser.add_argument(
        "--backup-dir",
        default=os.getenv("DB_BACKUP_DIR", str(DEFAULT_BACKUP_DIR)),
        help=f"Target directory for dumps. Default: {DEFAULT_BACKUP_DIR}",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=_env_int("DB_BACKUP_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
        help=f"Delete backup files older than this many days. Default: {DEFAULT_RETENTION_DAYS}",
    )
    parser.add_argument(
        "--dump-binary",
        default=os.getenv("DB_BACKUP_DUMP_BINARY"),
        help="Optional path/name for mariadb-dump or mysqldump.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=_env_int("DB_BACKUP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
        help=f"Maximum dump runtime. Default: {DEFAULT_TIMEOUT_SECONDS}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(BASE_DIR / ".env")
    args = _build_parser().parse_args(argv)
    try:
        create_database_backup(
            database_url=os.getenv("DATABASE_URL", ""),
            backup_dir=Path(args.backup_dir),
            retention_days=args.retention_days,
            dump_binary=args.dump_binary,
            timeout_seconds=args.timeout_seconds,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"Database backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
