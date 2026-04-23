from __future__ import annotations

import configparser
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def load_config(filename: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser(allow_no_value=True)
    config.read(BASE_DIR / filename)
    return config


def env_or_config(
    env_name: str,
    config: configparser.ConfigParser,
    section: str,
    option: str,
    default: str,
) -> str:
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        return env_value
    if config.has_section(section):
        return config.get(section, option, fallback=default).strip()
    return default


def env_bool_or_config(
    env_name: str,
    config: configparser.ConfigParser,
    section: str,
    option: str,
    default: bool,
) -> bool:
    env_value = os.getenv(env_name, "").strip().lower()
    if env_value:
        return env_value == "true"
    if config.has_section(section):
        return config.getboolean(section, option, fallback=default)
    return default


def default_frontend_origin() -> str:
    return f"http://localhost:{os.getenv('FRONTEND_PORT', '8080').strip() or '8080'}"


def get_service_port(env_name: str, default: int) -> int:
    raw_value = os.getenv(env_name, str(default)).strip()
    try:
        return int(raw_value)
    except ValueError:
        return default


def _parse_neo4j_auth() -> tuple[str, str]:
    raw_auth = os.getenv("NEO4J_AUTH", "").strip()
    if "/" not in raw_auth:
        return "", ""
    username, password = raw_auth.split("/", 1)
    return username.strip(), password.strip()


def get_neo4j_settings() -> tuple[str, str, str]:
    config = load_config("neo4j_config.ini")
    auth_username, auth_password = _parse_neo4j_auth()
    config_username = (
        config.get("neo4j", "username", fallback="neo4j").strip()
        if config.has_section("neo4j")
        else "neo4j"
    )
    config_password = (
        config.get("neo4j", "password", fallback="password").strip()
        if config.has_section("neo4j")
        else "password"
    )
    uri = env_or_config(
        "NEO4J_URI",
        config,
        "neo4j",
        "uri",
        "bolt://datapact-neo4j-db:7687",
    )
    username = os.getenv("NEO4J_USERNAME", "").strip() or auth_username or config_username
    password = os.getenv("NEO4J_PASSWORD", "").strip() or auth_password or config_password
    return uri, username, password


def get_minio_settings() -> tuple[str, str, str, bool]:
    config = load_config("minio_config.ini")
    config_access_key = (
        config.get("minio", "access_key", fallback="minio-datapact").strip()
        if config.has_section("minio")
        else "minio-datapact"
    )
    config_secret_key = (
        config.get("minio", "secret_key", fallback="minio-datapact").strip()
        if config.has_section("minio")
        else "minio-datapact"
    )
    endpoint = env_or_config(
        "MINIO_ENDPOINT",
        config,
        "minio",
        "endpoint",
        "datapact-minio-db:9000",
    )
    access_key = (
        os.getenv("MINIO_ACCESS_KEY", "").strip()
        or os.getenv("MINIO_ROOT_USER", "").strip()
        or config_access_key
    )
    secret_key = (
        os.getenv("MINIO_SECRET_KEY", "").strip()
        or os.getenv("MINIO_ROOT_PASSWORD", "").strip()
        or config_secret_key
    )
    secure = env_bool_or_config("MINIO_SECURE", config, "minio", "secure", False)
    return endpoint, access_key, secret_key, secure
