"""Fail-closed adapters for supported local command-line agent harnesses."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import tomllib
from typing import Callable, Iterator, Mapping, Sequence

from ..bounded_process import BoundedCompletedProcess, run_bounded
from ..context_guard import assert_content_allowed, safe_error_text
from ..errors import DispatchValidationError
from ..process_identity import identity_for_pid
from ..task_contract import TaskContract
from .base import AdapterResult


_COMMON_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
)
_BACKEND_ENV_ALLOWLIST = {
    "codex": frozenset(
        {
            "CODEX_HOME",
        }
    ),
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
        }
    ),
    "cursor-agent": frozenset(
        {"CURSOR_API_KEY", "CURSOR_API_ENDPOINT", "XDG_CONFIG_HOME"}
    ),
    "opencode": frozenset(
        {
            "OPENCODE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        }
    ),
    "grok": frozenset({"GROK_HOME", "XAI_API_KEY"}),
    "hermes": frozenset(
        {
            "HERMES_HOME",
            "HERMES_INFERENCE_MODEL",
        }
    ),
    "kimi": frozenset(
        {
            "KIMI_CODE_HOME",
        }
    ),
    "dsh": frozenset({"DEEPSEEK_API_KEY", "DSH_HOME"}),
    "pi": frozenset(
        {
            "PI_CODING_AGENT_DIR",
        }
    ),
}

_PROVIDER_CREDENTIAL_NAMES = {
    "anthropic": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ),
    "anthropiccompatible": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ),
    "deepinfra": ("DEEPINFRA_API_KEY", "DEEPINFRA_BASE_URL"),
    "deepinfraai": ("DEEPINFRA_API_KEY", "DEEPINFRA_BASE_URL"),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GEMINI_BASE_URL"),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GEMINI_BASE_URL"),
    "kimi": (
        "KIMI_API_KEY",
        "KIMI_CODING_API_KEY",
        "KIMI_BASE_URL",
    ),
    "kimicoding": ("KIMI_API_KEY", "KIMI_CODING_API_KEY", "KIMI_BASE_URL"),
    "kimicodingcn": ("KIMI_CN_API_KEY",),
    "moonshot": ("KIMI_API_KEY", "KIMI_CODING_API_KEY", "KIMI_BASE_URL"),
    "minimax": (
        "MINIMAX_API_KEY",
        "MINIMAX_BASE_URL",
    ),
    "minimaxcn": ("MINIMAX_CN_API_KEY", "MINIMAX_CN_BASE_URL"),
    "minimaxoauth": (),
    "nvidia": ("NVIDIA_API_KEY", "NVIDIA_BASE_URL"),
    "nvidianim": ("NVIDIA_API_KEY", "NVIDIA_BASE_URL"),
    "nousportal": (),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "openaiapi": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "openaicodex": (),
    "openaicompatible": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "qwenoauth": (),
    "qwen": (),
    "nous": (),
    "stepfun": ("STEPFUN_API_KEY", "STEPFUN_BASE_URL"),
    "stepfunstep": ("STEPFUN_API_KEY", "STEPFUN_BASE_URL"),
    "xai": ("XAI_API_KEY", "XAI_BASE_URL"),
    "xaioauth": (),
    "grok": ("XAI_API_KEY", "XAI_BASE_URL"),
    "zai": ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "GLM_BASE_URL"),
}

_DEFAULT_BACKEND_HOMES = {
    "cursor-agent": ("HOME", "."),
    "grok": ("GROK_HOME", ".grok"),
    "hermes": ("HERMES_HOME", ".hermes"),
    "kimi": ("KIMI_CODE_HOME", ".kimi-code"),
    "dsh": ("DSH_HOME", ".dsh"),
    "pi": ("PI_CODING_AGENT_DIR", ".pi/agent"),
}


def _backend_environment(backend: str) -> dict[str, str]:
    """Pass only backend login/runtime variables, never the full host environment."""
    allowed = _COMMON_ENV_ALLOWLIST | _BACKEND_ENV_ALLOWLIST.get(
        backend,
        frozenset(),
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in allowed and value
    }
    default_home = _DEFAULT_BACKEND_HOMES.get(backend)
    if default_home is not None:
        variable, relative = default_home
        default_path = Path.home() if relative == "." else Path.home() / relative
        environment.setdefault(variable, str(default_path))
    if backend == "opencode":
        environment.setdefault("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        environment.setdefault("XDG_DATA_HOME", str(Path.home() / ".local/share"))
    return environment


def _read_small_regular_bytes(
    path: Path,
    *,
    max_bytes: int = 1024 * 1024,
    require_private: bool = False,
) -> bytes | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > max_bytes
            or (require_private and opened.st_mode & 0o077)
        ):
            return None
        linked = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(linked.st_mode) or not os.path.samestat(opened, linked):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        return raw if len(raw) <= max_bytes else None
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _dotenv_credentials(path: Path, names: Sequence[str]) -> dict[str, str]:
    """Read only selected simple assignments from one Provider dotenv file."""
    wanted = set(names)
    values: dict[str, str] = {}
    raw = _read_small_regular_bytes(path)
    if raw is None:
        return values
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in wanted or key in values:
            continue
        value = value.strip()
        if not value:
            continue
        if value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                continue
            value = value[1:-1]
        elif value.startswith('"'):
            try:
                decoded = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(decoded, str):
                continue
            value = decoded
        else:
            value = value.split(" #", 1)[0].strip()
        if value and "\x00" not in value:
            values[key] = value
    return values


def _provider_credentials(
    provider: str | None,
    *,
    dotenv_path: Path | None = None,
) -> dict[str, str]:
    """Return only credentials belonging to the selected model Provider."""
    if not provider:
        return {}
    normalized = re.sub(r"[^a-z0-9]+", "", provider.lower())
    names = _PROVIDER_CREDENTIAL_NAMES.get(normalized, ())
    credentials = {
        name: os.environ[name]
        for name in names
        if os.environ.get(name)
    }
    if dotenv_path is not None:
        for name, value in _dotenv_credentials(dotenv_path, names).items():
            credentials.setdefault(name, value)
    return credentials


def _read_small_private_json(path: Path) -> dict[str, object] | None:
    raw = _read_small_regular_bytes(path)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_small_private_toml(path: Path) -> dict[str, object] | None:
    raw = _read_small_regular_bytes(path)
    if raw is None:
        return None
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True)
class _KimiRoute:
    provider: str
    model: str
    route_sha256: str
    config: dict[str, object]
    credential_files: tuple[tuple[str, bytes], ...] = ()


_KIMI_PROVIDER_FIELDS = frozenset(
    {
        "api_key",
        "base_url",
        "custom_headers",
        "default_model",
        "env",
        "model_source",
        "oauth",
        "type",
    }
)
_KIMI_MODEL_FIELDS = frozenset(
    {
        "adaptive_thinking",
        "beta_api",
        "capabilities",
        "default_effort",
        "display_name",
        "max_context_size",
        "max_input_size",
        "max_output_size",
        "model",
        "off_effort",
        "overrides",
        "provider",
        "reasoning_key",
        "support_efforts",
    }
)


def _kimi_route_digest(
    config: Mapping[str, object],
    credential_files: Sequence[tuple[str, bytes]] = (),
) -> str:
    digest_payload = {
        "config": dict(config),
        "credential_files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in credential_files
        },
    }
    try:
        encoded = json.dumps(
            digest_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DispatchValidationError(
            "selected Kimi provider configuration is not serializable"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _positive_kimi_integer(
    environment: Mapping[str, str],
    name: str,
    default: int | None = None,
) -> int | None:
    raw = environment.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) <= 0:
        raise DispatchValidationError(f"{name} must be a positive integer")
    return int(raw)


def _kimi_env_route(environment: Mapping[str, str]) -> _KimiRoute | None:
    model_name = environment.get("KIMI_MODEL_NAME", "").strip()
    if not model_name:
        return None
    api_key = environment.get("KIMI_MODEL_API_KEY", "").strip()
    if not api_key:
        raise DispatchValidationError(
            "KIMI_MODEL_NAME requires KIMI_MODEL_API_KEY"
        )
    provider_type = environment.get("KIMI_MODEL_PROVIDER_TYPE", "kimi").strip().lower()
    if provider_type not in {"kimi", "anthropic", "openai"}:
        raise DispatchValidationError("KIMI_MODEL_PROVIDER_TYPE is unsupported")
    default_base_urls = {
        "kimi": "https://api.moonshot.ai/v1",
        "openai": "https://api.openai.com/v1",
    }
    provider: dict[str, object] = {
        "type": provider_type,
        "api_key": api_key,
    }
    base_url = environment.get("KIMI_MODEL_BASE_URL", "").strip()
    if base_url or provider_type in default_base_urls:
        provider["base_url"] = base_url or default_base_urls[provider_type]
    alias: dict[str, object] = {
        "provider": "__kimi_env__",
        "model": model_name,
        "max_context_size": _positive_kimi_integer(
            environment,
            "KIMI_MODEL_MAX_CONTEXT_SIZE",
            262144,
        ),
    }
    capabilities = [
        item.strip().lower()
        for item in environment.get(
            "KIMI_MODEL_CAPABILITIES",
            "image_in,thinking",
        ).split(",")
        if item.strip()
    ]
    if capabilities:
        alias["capabilities"] = capabilities
    optional_strings = {
        "display_name": "KIMI_MODEL_DISPLAY_NAME",
        "reasoning_key": "KIMI_MODEL_REASONING_KEY",
    }
    for target, source in optional_strings.items():
        value = environment.get(source, "").strip()
        if value:
            alias[target] = value
    max_output_size = _positive_kimi_integer(
        environment,
        "KIMI_MODEL_MAX_OUTPUT_SIZE",
    )
    if max_output_size is not None:
        alias["max_output_size"] = max_output_size
    adaptive = environment.get("KIMI_MODEL_ADAPTIVE_THINKING", "").strip().lower()
    if adaptive:
        if adaptive not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise DispatchValidationError(
                "KIMI_MODEL_ADAPTIVE_THINKING must be boolean"
            )
        alias["adaptive_thinking"] = adaptive in {"1", "true", "yes", "on"}
    config: dict[str, object] = {
        "default_provider": "__kimi_env__",
        "default_model": "__kimi_env_model__",
        "providers": {"__kimi_env__": provider},
        "models": {"__kimi_env_model__": alias},
    }
    return _KimiRoute(
        provider="__kimi_env__",
        model="__kimi_env_model__",
        route_sha256=_kimi_route_digest(config),
        config=config,
    )


def _filtered_kimi_mapping(
    value: object,
    *,
    allowed: frozenset[str],
) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if key in allowed}


def _kimi_config_route(source_home: Path) -> _KimiRoute | None:
    payload = _read_small_private_toml(source_home / "config.toml") or {}
    model = payload.get("default_model")
    if not isinstance(model, str) or not model:
        return None
    models = payload.get("models")
    selected_model = models.get(model) if isinstance(models, dict) else None
    alias = _filtered_kimi_mapping(
        selected_model,
        allowed=_KIMI_MODEL_FIELDS,
    )
    if alias is None:
        raise DispatchValidationError(
            "Kimi default_model has no selectable model configuration"
        )
    alias_provider = alias.get("provider")
    default_provider = payload.get("default_provider")
    provider = (
        alias_provider
        if isinstance(alias_provider, str) and alias_provider
        else default_provider
    )
    if not isinstance(provider, str) or not provider:
        raise DispatchValidationError(
            "Kimi default model has no selected provider"
        )
    providers = payload.get("providers")
    selected_provider = providers.get(provider) if isinstance(providers, dict) else None
    provider_config = _filtered_kimi_mapping(
        selected_provider,
        allowed=_KIMI_PROVIDER_FIELDS,
    )
    if provider_config is None:
        raise DispatchValidationError(
            "Kimi selected provider has no configuration"
        )
    credential_files: tuple[tuple[str, bytes], ...] = ()
    oauth = selected_provider.get("oauth")
    if oauth is not None:
        if not isinstance(oauth, dict) or oauth.get("storage") != "file":
            raise DispatchValidationError(
                "Kimi dispatch supports only file-backed OAuth profiles"
            )
        oauth_key = oauth.get("key")
        if not isinstance(oauth_key, str) or not oauth_key:
            raise DispatchValidationError("Kimi OAuth profile has no token key")
        if oauth_key in {"kimi-code", "oauth/kimi-code"}:
            storage_name = "kimi-code"
        elif oauth_key.startswith("oauth/") and oauth_key[6:]:
            storage_name = oauth_key[6:]
        elif "/" not in oauth_key and not oauth_key.startswith("."):
            storage_name = oauth_key
        else:
            raise DispatchValidationError("Kimi OAuth token key is unsafe")
        if Path(storage_name).name != storage_name or not storage_name:
            raise DispatchValidationError("Kimi OAuth token key is unsafe")
        credential_name = f"{storage_name}.json"
        credential = _read_small_regular_bytes(
            source_home / "credentials" / credential_name,
            require_private=True,
        )
        if credential is None:
            raise DispatchValidationError(
                "Kimi selected OAuth credential is unavailable or not private"
            )
        credential_files = ((credential_name, credential),)
    api_key = provider_config.get("api_key")
    provider_env = provider_config.get("env")
    if oauth is None and not (isinstance(api_key, str) and api_key) and not (
        isinstance(provider_env, dict)
        and any(isinstance(value, str) and value for value in provider_env.values())
    ):
        raise DispatchValidationError(
            "Kimi selected provider has no scoped non-OAuth credential"
        )
    config = {
        "default_provider": provider,
        "default_model": model,
        "providers": {provider: provider_config},
        "models": {model: alias},
    }
    return _KimiRoute(
        provider=provider,
        model=model,
        route_sha256=_kimi_route_digest(config, credential_files),
        config=config,
        credential_files=credential_files,
    )


def _kimi_route(
    *,
    source_home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> _KimiRoute | None:
    source_environment = environment if environment is not None else os.environ
    env_route = _kimi_env_route(source_environment)
    if env_route is not None:
        return env_route
    root = source_home or Path(
        source_environment.get(
            "KIMI_CODE_HOME",
            str(Path.home() / ".kimi-code"),
        )
    )
    return _kimi_config_route(root)


def _configured_model(backend: str) -> tuple[str, str] | None:
    if os.environ.get("DYRO_DISPATCH_PROFILE_BACKEND") == backend:
        provider = os.environ.get("DYRO_DISPATCH_PROFILE_PROVIDER", "")
        model = os.environ.get("DYRO_DISPATCH_PROFILE_MODEL", "")
        if provider and model:
            return provider, model
    environment = _backend_environment(backend)
    if backend == "codex":
        root = Path(environment.get("CODEX_HOME", str(Path.home() / ".codex")))
        payload = _read_small_private_toml(root / "config.toml") or {}
        model = payload.get("model")
        return ("openai", model) if isinstance(model, str) and model else None
    if backend == "claude":
        root = Path(
            environment.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        )
        payload = _read_small_private_json(root / "settings.json") or {}
        model = payload.get("model")
        return ("anthropic", model) if isinstance(model, str) and model else None
    if backend == "cursor-agent":
        return ("cursor", os.environ.get("DYRO_CURSOR_MODEL", "gpt-5"))
    if backend == "opencode":
        root = Path(
            environment.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        )
        payload = _read_small_private_json(root / "opencode" / "opencode.json") or {}
        model = payload.get("model")
        if isinstance(model, str) and "/" in model:
            return model.partition("/")[0], model
        return None
    if backend == "grok":
        root = Path(environment.get("GROK_HOME", str(Path.home() / ".grok")))
        payload = _read_small_private_toml(root / "config.toml") or {}
        models = payload.get("models")
        model = models.get("default") if isinstance(models, dict) else None
        return ("xai", model) if isinstance(model, str) and model else None
    if backend == "kimi":
        route = _kimi_route()
        return (route.provider, route.model) if route is not None else None
    if backend == "dsh":
        return "deepseek-official", "deepseek-v4-flash"
    return None


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > 1024 * 1024:
        raise DispatchValidationError("isolated worker profile exceeds byte limit")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _write_private_bytes(path: Path, content: bytes) -> None:
    if len(content) > 1024 * 1024:
        raise DispatchValidationError("isolated worker profile exceeds byte limit")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _toml_key(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, list) and all(
        isinstance(item, (str, bool)) or type(item) is int for item in value
    ):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise DispatchValidationError(
        "selected Kimi provider configuration contains an unsupported value"
    )


def _toml_document(payload: Mapping[str, object]) -> bytes:
    lines: list[str] = []

    def emit_table(path: tuple[str, ...], table: Mapping[str, object]) -> None:
        scalars = {
            key: value for key, value in table.items() if not isinstance(value, dict)
        }
        children = {
            key: value for key, value in table.items() if isinstance(value, dict)
        }
        if path:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append("[" + ".".join(_toml_key(item) for item in path) + "]")
        for key in sorted(scalars):
            lines.append(f"{_toml_key(key)} = {_toml_value(scalars[key])}")
        for key in sorted(children):
            emit_table(path + (key,), children[key])

    emit_table((), payload)
    encoded = ("\n".join(lines) + "\n").encode("utf-8")
    if len(encoded) > 1024 * 1024:
        raise DispatchValidationError("isolated worker profile exceeds byte limit")
    return encoded


def _materialize_kimi_worker_home(
    *,
    source_home: Path,
    isolated_home: Path,
    provider: str,
    model: str,
    route_sha256: str,
) -> None:
    route = _kimi_route(source_home=source_home)
    if route is None or (
        route.provider != provider
        or route.model != model
        or route.route_sha256 != route_sha256
    ):
        raise DispatchValidationError(
            "Kimi execution route changed before worker profile materialization"
        )
    _write_private_bytes(
        isolated_home / "config.toml",
        _toml_document(route.config),
    )
    if route.credential_files:
        credentials = isolated_home / "credentials"
        credentials.mkdir(mode=0o700)
        credentials.chmod(0o700)
        for name, content in route.credential_files:
            _write_private_bytes(credentials / name, content)


def _copy_private_file(
    source: Path,
    destination: Path,
    *,
    require_private: bool = False,
) -> bool:
    content = _read_small_regular_bytes(
        source,
        require_private=require_private,
    )
    if content is None:
        return False
    _write_private_bytes(destination, content)
    return True


def _create_worker_home(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except OSError as exc:
        raise DispatchValidationError(
            "isolated async worker home cannot be created"
        ) from exc
    if path.is_symlink() or not path.is_dir():
        raise DispatchValidationError("isolated async worker home is unsafe")
    path.chmod(0o700)


def _materialize_basic_worker_home(
    *,
    backend: str,
    source_home: Path,
    isolated_home: Path,
    provider: str,
    model: str,
    route_sha256: str = "",
) -> None:
    _create_worker_home(isolated_home)
    if backend == "codex":
        _copy_private_file(source_home / "auth.json", isolated_home / "auth.json")
        return
    if backend == "claude":
        _copy_private_file(
            source_home / "config.json", isolated_home / "config.json"
        )
        return
    if backend == "cursor-agent":
        return
    if backend == "grok":
        _copy_private_file(source_home / "auth.json", isolated_home / "auth.json")
        config = (
            "[models]\n"
            f"default = {json.dumps(model)}\n"
            "default_reasoning_effort = \"high\"\n"
        ).encode("utf-8")
        _write_private_bytes(isolated_home / "config.toml", config)
        return
    if backend == "kimi":
        _materialize_kimi_worker_home(
            source_home=source_home,
            isolated_home=isolated_home,
            provider=provider,
            model=model,
            route_sha256=route_sha256,
        )
        return
    if backend == "dsh":
        _copy_private_file(
            source_home / ".credentials.yaml",
            isolated_home / ".credentials.yaml",
            require_private=True,
        )
        return
    raise DispatchValidationError(
        f"backend does not support an isolated async worker home: {backend}"
    )


def _materialize_opencode_worker_home(
    *,
    source_config_root: Path,
    source_data_root: Path,
    isolated_home: Path,
    provider: str,
    model: str,
) -> None:
    _create_worker_home(isolated_home)
    config_dir = isolated_home / "config" / "opencode"
    data_dir = isolated_home / "data" / "opencode"
    config_dir.mkdir(parents=True, mode=0o700)
    data_dir.mkdir(parents=True, mode=0o700)
    source_config = _read_small_private_json(
        source_config_root / "opencode" / "opencode.json"
    ) or {}
    providers = source_config.get("provider")
    selected_provider = (
        {provider: providers[provider]}
        if isinstance(providers, dict) and provider in providers
        else {}
    )
    _write_private_json(
        config_dir / "opencode.json",
        {"model": model, "provider": selected_provider},
    )
    source_auth = _read_small_private_json(
        source_data_root / "opencode" / "auth.json"
    ) or {}
    if provider in source_auth:
        _write_private_json(
            data_dir / "auth.json",
            {provider: source_auth[provider]},
        )


def _materialize_pi_worker_home(
    *,
    source_home: Path,
    isolated_home: Path,
    provider: str,
    model: str,
) -> None:
    _create_worker_home(isolated_home)
    _write_private_json(
        isolated_home / "settings.json",
        {"defaultProvider": provider, "defaultModel": model},
    )
    auth = _read_small_private_json(source_home / "auth.json")
    if auth is not None and provider in auth:
        _write_private_json(
            isolated_home / "auth.json",
            {provider: auth[provider]},
        )


def _materialize_hermes_worker_home(
    *,
    source_home: Path,
    isolated_home: Path,
    provider: str,
    model: str,
) -> None:
    _create_worker_home(isolated_home)
    config = f"model:\n  default: {json.dumps(model)}\n  provider: {json.dumps(provider)}\n"
    descriptor = os.open(
        isolated_home / "config.yaml",
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(config)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    auth = _read_small_private_json(source_home / "auth.json")
    if auth is None:
        return
    providers = auth.get("providers")
    pool = auth.get("credential_pool")
    selected_providers = (
        {provider: providers[provider]}
        if isinstance(providers, dict) and provider in providers
        else {}
    )
    selected_pool = (
        {provider: pool[provider]}
        if isinstance(pool, dict) and provider in pool
        else {}
    )
    if not selected_providers and not selected_pool:
        return
    _write_private_json(
        isolated_home / "auth.json",
        {
            "version": auth.get("version", 1),
            "active_provider": provider,
            "providers": selected_providers,
            "credential_pool": selected_pool,
        },
    )


def _build_prompt(contract: TaskContract, context_files: Mapping[str, str]) -> str:
    parts = [
        "# Task (self-contained; no prior conversation)",
        "",
        f"Execution mode: {contract.mode}",
        f"Strict context-only mode: {contract.strict}",
        f"## Briefing\n{contract.task.briefing}",
        f"## Locations\n{contract.task.locations}",
        f"## Objective\n{contract.task.objective}",
        f"## Constraints\n{contract.task.constraints}",
        f"## Output contract\n{contract.task.output_contract}",
        "",
        "Use only the context supplied below unless edit mode explicitly provides an",
        "isolated worktree. Never invoke Git network operations or production actions.",
        "Respond with one JSON object containing summary (string),",
        "confidence (high|medium|low), and evidence",
        "(array of {file, lines?, claim}). Do not include secrets or Markdown fences.",
        "",
        "## Context files",
    ]
    for relative, content in sorted(context_files.items()):
        parts.append(f"\n### {relative}\n```\n{content}\n```")
    return "\n".join(parts)


def _parse_model_json(text: str) -> dict[str, object]:
    raw = text.strip()
    if not raw:
        raise DispatchValidationError("backend returned an empty JSON result")
    candidates = [raw]
    if "```" in raw:
        for chunk in raw.split("```"):
            candidate = chunk.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate:
                candidates.append(candidate)
    payload: object | None = None
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            payload = decoded
            break
    if not isinstance(payload, dict):
        raise DispatchValidationError("backend result is not a JSON object")

    summary = payload.get("summary")
    confidence = payload.get("confidence")
    evidence = payload.get("evidence")
    if type(summary) is not str or not summary.strip() or len(summary) > 4000:
        raise DispatchValidationError("backend JSON summary is invalid")
    if confidence not in {"high", "medium", "low"}:
        raise DispatchValidationError("backend JSON confidence is invalid")
    if not isinstance(evidence, list) or len(evidence) > 100:
        raise DispatchValidationError("backend JSON evidence is invalid")
    if any(not isinstance(item, dict) for item in evidence):
        raise DispatchValidationError("backend JSON evidence entries must be objects")
    assert_content_allowed(summary, label="provider.summary")
    for index, item in enumerate(evidence):
        for name in ("file", "claim", "lines"):
            value = item.get(name)
            if isinstance(value, str):
                assert_content_allowed(value, label=f"provider.evidence[{index}].{name}")
    return {
        "summary": summary.strip(),
        "confidence": confidence,
        "evidence": evidence,
    }


def _message_content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _parse_wrapped_model_json(text: str, *, backend: str) -> dict[str, object]:
    """Extract the final assistant text from one backend's JSON protocol."""
    try:
        return _parse_model_json(text)
    except DispatchValidationError:
        pass

    decoded: list[dict[str, object]] = []
    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        decoded.append(whole)
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value not in decoded:
            decoded.append(value)
    if not decoded:
        raise DispatchValidationError(f"{backend} returned invalid JSON output")

    candidates: list[str] = []
    for value in decoded:
        if backend == "cursor-agent":
            result = value.get("result")
            if isinstance(result, str):
                candidates.append(result)
        elif backend == "grok":
            result = value.get("text")
            if isinstance(result, str):
                candidates.append(result)
        elif backend == "opencode":
            part = value.get("part")
            if isinstance(part, dict) and part.get("type") == "text":
                result = part.get("text")
                if isinstance(result, str):
                    candidates.append(result)
            elif value.get("type") == "text":
                result = value.get("text") or value.get("data")
                if isinstance(result, str):
                    candidates.append(result)
        elif backend == "kimi" and value.get("role") == "assistant":
            result = _message_content_text(value.get("content"))
            if result:
                candidates.append(result)
        elif backend == "pi" and value.get("type") == "message_end":
            message = value.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                result = _message_content_text(message.get("content"))
                if result:
                    candidates.append(result)

    for candidate in reversed(candidates):
        try:
            return _parse_model_json(candidate)
        except DispatchValidationError:
            continue
    raise DispatchValidationError(
        f"{backend} output did not contain a valid final assistant result"
    )


def _completed_to_result(
    completed: BoundedCompletedProcess,
    *,
    backend: str,
    parser: Callable[[str], dict[str, object]] = _parse_model_json,
) -> AdapterResult:
    if completed.cancelled:
        return AdapterResult(
            status="cancelled",
            summary="",
            error_code="cancelled",
            warnings=["backend process group was cancelled and terminated"],
        )
    if completed.timed_out:
        return AdapterResult(
            status="timeout",
            summary="",
            error_code="timeout",
            warnings=["backend process group exceeded its deadline and was terminated"],
        )
    if completed.output_limited:
        return AdapterResult(
            status="error",
            summary="",
            error_code="output_limit",
            warnings=["backend output exceeded the byte limit and was terminated"],
        )
    if completed.returncode != 0:
        return AdapterResult(
            status="error",
            summary="",
            error_code=f"exit_{completed.returncode}",
            warnings=[f"{backend} process exited with code {completed.returncode}"],
        )
    try:
        parsed = parser(completed.stdout)
    except DispatchValidationError as exc:
        return AdapterResult(
            status="error",
            summary="",
            error_code="protocol_error",
            warnings=[safe_error_text(exc, fallback="backend result failed validation")],
        )
    return AdapterResult(
        status="ok",
        summary=str(parsed["summary"]),
        evidence=list(parsed["evidence"]),  # type: ignore[arg-type]
        confidence=str(parsed["confidence"]),
        usage={"exit_code": completed.returncode, "backend": backend},
    )


class SubprocessCliAdapter:
    strict_isolation = False
    supported_modes = frozenset({"read-only", "edit"})

    def __init__(self, *, backend_id: str, command: str) -> None:
        self.id = backend_id
        self.command = command
        self._process_observer: Callable[[int, int, str], None] | None = None
        self._lifetime_lock_path: Path | None = None
        self._cancel_check: Callable[[], bool] | None = None
        self._planned_execution_profile: dict[str, str] | None = None

    def configure_execution_profile(
        self,
        profile: Mapping[str, str],
    ) -> None:
        if profile.get("backend") != self.id:
            raise DispatchValidationError(
                "backend execution profile identity mismatch"
            )
        self._planned_execution_profile = dict(profile)

    def _execution_value(self, name: str) -> str:
        profile = self._planned_execution_profile or dict(
            self.execution_profile()
        )
        value = profile.get(name, "")
        if not value:
            raise DispatchValidationError(
                f"backend execution profile is missing {name}: {self.id}"
            )
        return value

    def configure_process_tracking(
        self,
        *,
        observer: Callable[[int, int, str], None],
        lifetime_lock_path: Path,
    ) -> None:
        self._process_observer = observer
        self._lifetime_lock_path = Path(lifetime_lock_path)

    def configure_cancellation(
        self,
        *,
        cancel_check: Callable[[], bool],
    ) -> None:
        self._cancel_check = cancel_check

    def worker_environment(
        self,
        *,
        isolated_home: Path | None = None,
    ) -> dict[str, str]:
        """Build the minimal environment needed by this adapter's async worker."""
        environment = _backend_environment(self.id)
        if isolated_home is not None and self.id in {
            "codex",
            "claude",
            "cursor-agent",
            "grok",
            "kimi",
            "dsh",
        }:
            source_spec = _DEFAULT_BACKEND_HOMES.get(self.id)
            if source_spec is None:
                source_spec = {
                    "codex": ("CODEX_HOME", ".codex"),
                    "claude": ("CLAUDE_CONFIG_DIR", ".claude"),
                }[self.id]
            variable, relative = source_spec
            source_home = Path(
                environment.get(variable, str(Path.home() / relative))
            )
            _materialize_basic_worker_home(
                backend=self.id,
                source_home=source_home,
                isolated_home=isolated_home,
                provider=self._execution_value("provider"),
                model=self._execution_value("model"),
                route_sha256=(
                    self._execution_value("route_sha256")
                    if self.id == "kimi"
                    else ""
                ),
            )
            environment[variable] = str(isolated_home)
            if self.id == "cursor-agent":
                environment["HOME"] = str(isolated_home)
                environment["XDG_CONFIG_HOME"] = str(isolated_home / "config")
            elif self.id == "claude":
                environment["XDG_CONFIG_HOME"] = str(isolated_home / "xdg")
        elif isolated_home is not None and self.id == "opencode":
            source_config_root = Path(
                environment.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
            )
            source_data_root = Path(
                environment.get(
                    "XDG_DATA_HOME", str(Path.home() / ".local/share")
                )
            )
            _materialize_opencode_worker_home(
                source_config_root=source_config_root,
                source_data_root=source_data_root,
                isolated_home=isolated_home,
                provider=self._execution_value("provider"),
                model=self._execution_value("model"),
            )
            environment["XDG_CONFIG_HOME"] = str(isolated_home / "config")
            environment["XDG_DATA_HOME"] = str(isolated_home / "data")
            environment["OPENCODE_CONFIG_DIR"] = str(
                isolated_home / "config" / "opencode"
            )
        if self.id == "hermes":
            provider = self._execution_value("provider")
            model = self._execution_value("model")
            if provider and model:
                source_home = Path(
                    environment.get("HERMES_HOME", str(Path.home() / ".hermes"))
                )
                environment.update(
                    _provider_credentials(
                        provider,
                        dotenv_path=source_home / ".env",
                    )
                )
                if isolated_home is not None:
                    _materialize_hermes_worker_home(
                        source_home=source_home,
                        isolated_home=isolated_home,
                        provider=provider,
                        model=model,
                    )
                    environment["HERMES_HOME"] = str(isolated_home)
        elif self.id == "pi":
            # Pi can route many Providers. Propagate only the current default's
            # credentials so an async worker cannot inherit lateral keys.
            provider = self._execution_value("provider")
            model = self._execution_value("model")
            environment.update(_provider_credentials(provider))
            if isolated_home is not None:
                source_home = Path(
                    environment.get(
                        "PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")
                    )
                )
                _materialize_pi_worker_home(
                    source_home=source_home,
                    isolated_home=isolated_home,
                    provider=provider,
                    model=model,
                )
                environment["PI_CODING_AGENT_DIR"] = str(isolated_home)
        return environment

    def execution_profile(self) -> Mapping[str, str]:
        resolved = shutil.which(self.command)
        profile = {
            "backend": self.id,
            "command_path": (
                str(Path(resolved).resolve()) if resolved else self.command
            ),
        }
        configured = _configured_model(self.id)
        if configured is not None:
            profile["provider"], profile["model"] = configured
        if self.id == "kimi":
            route = _kimi_route()
            if route is not None:
                profile["provider"] = route.provider
                profile["model"] = route.model
                profile["route_sha256"] = route.route_sha256
        if self.id == "hermes":
            selection = _hermes_model_selection()
            if selection is not None:
                profile["provider"], profile["model"] = selection
        elif self.id == "pi":
            selection = _pi_default_selection()
            if selection is not None:
                profile["provider"], profile["model"] = selection
        if self.id in {
            "codex",
            "claude",
            "cursor-agent",
            "opencode",
            "grok",
            "hermes",
            "kimi",
            "dsh",
            "pi",
        } and (not profile.get("provider") or not profile.get("model")):
            raise DispatchValidationError(
                f"backend has no statically selectable provider/model: {self.id}"
            )
        return profile

    def available(self) -> bool:
        return shutil.which(self.command) is not None

    def authenticated(self) -> bool:
        return self.available()

    def readiness_reason(self) -> str:
        return "authentication probe failed"

    def _probe(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float = 5.0,
        environment_overrides: Mapping[str, str] | None = None,
    ):
        if not self.available():
            return None
        try:
            environment = _backend_environment(self.id)
            if environment_overrides:
                environment.update(environment_overrides)
            completed = run_bounded(
                argv,
                cwd=Path.cwd(),
                timeout_seconds=timeout_seconds,
                env=environment,
                max_output_bytes=64 * 1024,
            )
        except OSError:
            return None
        if completed.returncode != 0 or completed.timed_out or completed.output_limited:
            return None
        return completed

    def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        prompt: str,
        timeout_seconds: float,
        parser: Callable[[str], dict[str, object]] = _parse_model_json,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> AdapterResult:
        on_spawn: Callable[[int], None] | None = None
        if self._process_observer is not None:
            observer = self._process_observer

            def observe(pid: int) -> None:
                identity = identity_for_pid(pid)
                if os.name != "posix":
                    raise DispatchValidationError(
                        "tracked subprocess execution requires POSIX"
                    )
                process_group_id = os.getpgid(identity.pid)
                if process_group_id != identity.pid:
                    raise DispatchValidationError(
                        "tracked backend must lead a dedicated process group"
                    )
                observer(
                    identity.pid,
                    process_group_id,
                    identity.started_at,
                )

            on_spawn = observe
        try:
            environment = _backend_environment(self.id)
            if environment_overrides:
                environment.update(environment_overrides)
            completed = run_bounded(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=environment,
                input_text=prompt,
                on_spawn=on_spawn,
                lifetime_lock_path=self._lifetime_lock_path,
                cancel_check=self._cancel_check,
            )
        except OSError as exc:
            return AdapterResult(
                status="error",
                summary="",
                error_code="spawn_failed",
                warnings=[safe_error_text(exc, fallback="backend process could not start")],
            )
        return _completed_to_result(completed, backend=self.id, parser=parser)


@contextmanager
def _temporary_text_file(
    cwd: Path,
    *,
    suffix: str,
    content: str,
) -> Iterator[Path]:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".dyro-dispatch-",
        suffix=suffix,
        dir=cwd,
        text=True,
    )
    path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise DispatchValidationError(
                "temporary dispatch input could not be removed"
            ) from exc


class CodexAdapter(SubprocessCliAdapter):
    strict_isolation = False

    def authenticated(self) -> bool:
        if not self.available():
            return False
        try:
            completed = run_bounded(
                ["codex", "login", "status"],
                cwd=Path.cwd(),
                timeout_seconds=3.0,
                env=_backend_environment("codex"),
                max_output_bytes=32 * 1024,
            )
        except OSError:
            return False
        return completed.returncode == 0 and not completed.timed_out

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return AdapterResult(
                status="error",
                summary="",
                error_code="backend_not_installed",
                warnings=["command not found: codex"],
            )
        sandbox = "workspace-write" if contract.mode == "edit" else "read-only"
        argv = [
            "codex",
            "exec",
            "--model",
            self._execution_value("model"),
            "--sandbox",
            sandbox,
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "-c",
            'approval_policy="never"',
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-",
        ]
        return self._run(
            argv,
            cwd=cwd,
            prompt=_build_prompt(contract, context_files),
            timeout_seconds=timeout_seconds,
        )


class ClaudeAdapter(SubprocessCliAdapter):
    # Tool-less read-only mode reduces capability, but it is not an OS sandbox.
    strict_isolation = False

    def authenticated(self) -> bool:
        if not self.available():
            return False
        try:
            completed = run_bounded(
                ["claude", "auth", "status", "--json"],
                cwd=Path.cwd(),
                timeout_seconds=3.0,
                env=_backend_environment("claude"),
                max_output_bytes=32 * 1024,
            )
        except OSError:
            return False
        if completed.returncode != 0 or completed.timed_out:
            return False
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and payload.get("loggedIn") is True

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return AdapterResult(
                status="error",
                summary="",
                error_code="backend_not_installed",
                warnings=["command not found: claude"],
            )
        edit_mode = contract.mode == "edit"
        argv = [
            "claude",
            "-p",
            "--output-format",
            "text",
            "--model",
            self._execution_value("model"),
            "--permission-mode",
            "acceptEdits" if edit_mode else "plan",
            "--safe-mode",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--mcp-config",
            "{}",
            "--no-session-persistence",
            "--tools",
            "Read,Edit" if edit_mode else "",
        ]
        return self._run(
            argv,
            cwd=cwd,
            prompt=_build_prompt(contract, context_files),
            timeout_seconds=timeout_seconds,
        )


class CursorAdapter(SubprocessCliAdapter):
    supported_modes = frozenset({"read-only"})

    def readiness_reason(self) -> str:
        return (
            "CURSOR_API_KEY is required so dispatch can isolate Cursor from "
            "user MCP and plugin processes"
        )

    def authenticated(self) -> bool:
        # Browser OAuth shares the user's full Cursor home, including MCP and
        # plugin startup. Dyro only routes Cursor when an API key lets the run
        # use an empty, ephemeral HOME.
        if not os.environ.get("CURSOR_API_KEY"):
            return False
        completed = self._probe(
            ["cursor-agent", "status"], timeout_seconds=15.0
        )
        if completed is None:
            return False
        plain = re.sub(r"\x1b\[[0-9;]*m", "", completed.stdout)
        return "Logged in" in plain

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return _not_installed(self.id, self.command)
        if contract.mode == "edit":
            return AdapterResult(
                status="error",
                summary="",
                error_code="backend_mode_unsupported",
                warnings=[
                    "Cursor edit dispatch is disabled because its sandbox "
                    "lifecycle cannot yet be proven"
                ],
                usage={"backend": self.id},
            )
        prompt = _build_prompt(contract, context_files)
        with (
            _temporary_text_file(cwd, suffix=".md", content=prompt) as task_file,
            tempfile.TemporaryDirectory(prefix="dyro-cursor-home-") as cursor_home,
        ):
            argv = [
                "cursor-agent",
                "--print",
                "--output-format",
                "json",
                # Cursor's sandbox helper currently daemonizes after the CLI
                # exits, which breaks Dyro's prove-before-release process-group
                # contract. The task still runs only after the caller accepts
                # best-effort unconfined read-only execution in a projection.
                "--sandbox",
                "disabled",
                "--workspace",
                str(cwd),
                "--model",
                self._execution_value("model"),
                "--trust",
            ]
            argv.extend(["--mode", "ask"])
            argv.append(
                f"Read and follow the complete task in @{task_file.name}."
            )
            return self._run(
                argv,
                cwd=cwd,
                prompt="",
                timeout_seconds=timeout_seconds,
                parser=lambda text: _parse_wrapped_model_json(
                    text, backend=self.id
                ),
                environment_overrides={"HOME": cursor_home},
            )


def _opencode_permissions(*, edit_mode: bool) -> str:
    permission = {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "edit": "allow" if edit_mode else "deny",
        "bash": "deny",
        "task": "deny",
        "external_directory": "deny",
        "todowrite": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "lsp": "deny",
        "skill": "deny",
        "question": "deny",
    }
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "permission": permission,
            "share": "disabled",
        },
        separators=(",", ":"),
    )


class OpenCodeAdapter(SubprocessCliAdapter):
    def _authenticated_provider_ids(self) -> list[str]:
        completed = self._probe(
            ["opencode", "auth", "list"], timeout_seconds=15.0
        )
        if completed is None:
            return []
        match = re.search(r"\b([1-9][0-9]*) credentials?\b", completed.stdout)
        if match is None:
            return []
        plain = re.sub(r"\x1b\[[0-9;]*m", "", completed.stdout)
        aliases = {
            "anthropic": "anthropic",
            "google": "google",
            "openai": "openai",
            "openrouter": "openrouter",
            "xai": "xai",
        }
        providers: list[str] = []
        for line in plain.splitlines():
            provider_match = re.search(r"^[●*]\s+(.+?)\s+(?:oauth|api)", line.strip())
            if provider_match is None:
                continue
            normalized = re.sub(
                r"[^a-z0-9]+", "", provider_match.group(1).lower()
            )
            for label, provider_id in aliases.items():
                if normalized.startswith(label):
                    providers.append(provider_id)
                    break
        return providers

    def authenticated(self) -> bool:
        selection = _configured_model("opencode")
        if selection is None:
            return False
        provider = selection[0]
        if provider in self._authenticated_provider_ids():
            return True
        config_root = Path(
            _backend_environment("opencode").get(
                "XDG_CONFIG_HOME", str(Path.home() / ".config")
            )
        )
        config = _read_small_private_json(
            config_root / "opencode" / "opencode.json"
        ) or {}
        providers = config.get("provider")
        selected = providers.get(provider) if isinstance(providers, dict) else None
        options = selected.get("options") if isinstance(selected, dict) else None
        api_key = options.get("apiKey") if isinstance(options, dict) else None
        return isinstance(api_key, str) and bool(api_key)

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return _not_installed(self.id, self.command)
        prompt = _build_prompt(contract, context_files)
        selected_model = self._execution_value("model")
        provider = self._execution_value("provider")
        source_environment = _backend_environment(self.id)
        source_config_root = Path(
            source_environment.get(
                "XDG_CONFIG_HOME", str(Path.home() / ".config")
            )
        )
        source_data_root = Path(
            source_environment.get(
                "XDG_DATA_HOME", str(Path.home() / ".local/share")
            )
        )
        with (
            tempfile.TemporaryDirectory(prefix="dyro-opencode-") as temporary_root,
            _temporary_text_file(cwd, suffix=".md", content=prompt) as task_file,
        ):
            isolated_home = Path(temporary_root) / "home"
            _materialize_opencode_worker_home(
                source_config_root=source_config_root,
                source_data_root=source_data_root,
                isolated_home=isolated_home,
                provider=provider,
                model=selected_model,
            )
            argv = [
                "opencode",
                "--pure",
                "run",
                "Follow the attached Dyro dispatch task exactly.",
                "--format",
                "json",
                "--dir",
                str(cwd),
                "--file",
                str(task_file),
                "--model",
                selected_model,
            ]
            return self._run(
                argv,
                cwd=cwd,
                prompt="",
                timeout_seconds=timeout_seconds,
                parser=lambda text: _parse_wrapped_model_json(
                    text, backend=self.id
                ),
                environment_overrides={
                    **_provider_credentials(provider),
                    "XDG_CONFIG_HOME": str(isolated_home / "config"),
                    "XDG_DATA_HOME": str(isolated_home / "data"),
                    "OPENCODE_CONFIG_DIR": str(
                        isolated_home / "config" / "opencode"
                    ),
                    "OPENCODE_CONFIG_CONTENT": _opencode_permissions(
                        edit_mode=contract.mode == "edit"
                    ),
                    "OPENCODE_DISABLE_AUTOUPDATE": "1",
                    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
                },
            )


class GrokAdapter(SubprocessCliAdapter):
    def authenticated(self) -> bool:
        completed = self._probe(["grok", "models"], timeout_seconds=10.0)
        if completed is None:
            return False
        plain = re.sub(r"\x1b\[[0-9;]*m", "", completed.stdout)
        return "You are logged in with" in plain

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return _not_installed(self.id, self.command)
        prompt = _build_prompt(contract, context_files)
        with _temporary_text_file(cwd, suffix=".md", content=prompt) as task_file:
            edit_mode = contract.mode == "edit"
            argv = [
                "grok",
                "--model",
                self._execution_value("model"),
                "--prompt-file",
                str(task_file),
                "--output-format",
                "json",
                "--verbatim",
                "--no-memory",
                "--no-subagents",
                "--disable-web-search",
                "--cwd",
                str(cwd),
                "--sandbox",
                "workspace-write" if edit_mode else "read-only",
                "--permission-mode",
                "acceptEdits" if edit_mode else "plan",
            ]
            if not edit_mode:
                argv.extend(["--agent", "explore"])
            return self._run(
                argv,
                cwd=cwd,
                prompt="",
                timeout_seconds=timeout_seconds,
                parser=lambda text: _parse_wrapped_model_json(
                    text, backend=self.id
                ),
            )


def _hermes_model_selection() -> tuple[str, str] | None:
    home = Path(
        _backend_environment("hermes").get(
            "HERMES_HOME", str(Path.home() / ".hermes")
        )
    )
    config = home / "config.yaml"
    raw = _read_small_regular_bytes(config)
    if raw is None:
        return None
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    in_model = False
    model = ""
    provider = ""
    for line in lines:
        if line == "model:":
            in_model = True
            continue
        if in_model and line and not line.startswith((" ", "\t")):
            break
        if not in_model:
            continue
        stripped = line.strip()
        if stripped.startswith("default:"):
            model = stripped.partition(":")[2].strip().strip("'\"")
        elif stripped.startswith("provider:"):
            provider = stripped.partition(":")[2].strip().strip("'\"")
    if model and provider:
        return provider, model
    return None


def _hermes_python_runtime() -> Path | None:
    command = shutil.which("hermes")
    if not command:
        return None
    command_path = Path(command).resolve()
    for name in ("python3", "python"):
        candidate = command_path.parent / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


_HERMES_ONESHOT_BOOTSTRAP = """
import os
import sys

os.environ["HERMES_SAFE_MODE"] = "1"
os.environ["HERMES_IGNORE_USER_CONFIG"] = "1"
os.environ["HERMES_IGNORE_RULES"] = "1"

prompt = sys.stdin.read()
model = sys.argv[1] or None
provider = sys.argv[2] or None
toolset = sys.argv[3]
source_hermes_home = sys.argv[4]
isolated_hermes_home = sys.argv[5]

def _dyro_safe_config(*args, **kwargs):
    return {
        "model": {"default": model or "", "provider": provider or ""},
        "context": {"engine": "compressor"},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
    }

from hermes_cli import config as _hermes_config
from hermes_cli import env_loader as _hermes_env_loader

_hermes_config.load_config = _dyro_safe_config
_hermes_config.load_config_readonly = _dyro_safe_config
_hermes_env_loader.load_hermes_dotenv = lambda *args, **kwargs: []

os.environ["HERMES_HOME"] = source_hermes_home
from hermes_cli.runtime_provider import resolve_runtime_provider

runtime = resolve_runtime_provider(requested=provider, target_model=model)
if runtime.get("api_mode") not in {
    "anthropic_messages",
    "chat_completions",
    "codex_responses",
}:
    raise RuntimeError("Hermes selected an unsupported runtime mode")

os.environ["HERMES_HOME"] = isolated_hermes_home
agent = None
try:
    from run_agent import AIAgent

    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        requested_provider=runtime.get("requested_provider"),
        api_mode=runtime.get("api_mode"),
        model=model or "",
        enabled_toolsets=[toolset],
        quiet_mode=True,
        platform="cli",
        session_db=None,
        credential_pool=None,
        fallback_model=None,
        clarify_callback=lambda *args, **kwargs: (
            "Non-interactive dispatch: make a bounded assumption and continue."
        ),
        skip_context_files=True,
        load_soul_identity=False,
        skip_memory=True,
        skip_background_review=True,
        checkpoints_enabled=False,
    )
    agent.suppress_status_output = True
    agent.stream_delta_callback = None
    agent.tool_gen_callback = None
    result = agent.run_conversation(prompt)
    response = result.get("final_response") or ""
    if not response.strip():
        raise RuntimeError("Hermes returned an empty response")
    print(response)
    raise SystemExit(0)
finally:
    if agent is not None:
        agent.close()
"""


def _hermes_provider_is_ready(status: str, provider: str) -> bool:
    canonical = provider.strip().lower().replace("_", "-")
    labels = {
        "anthropic": "Anthropic",
        "deepinfra": "DeepInfra",
        "deep-infra": "DeepInfra",
        "deepinfra-ai": "DeepInfra",
        "deepseek": "DeepSeek",
        "gemini": "Google / Gemini",
        "google": "Google / Gemini",
        "kimi": "Kimi / Moonshot",
        "kimi-coding": "Kimi / Moonshot",
        "kimi-coding-cn": "Kimi / Moonshot (China)",
        "moonshot": "Kimi / Moonshot",
        "minimax": "MiniMax",
        "minimax-cn": "MiniMax (China)",
        "minimax-oauth": "MiniMax OAuth",
        "nvidia": "NVIDIA NIM",
        "nvidia-nim": "NVIDIA NIM",
        "nous": "Nous Portal",
        "nous-portal": "Nous Portal",
        "openai": "OpenAI",
        "openai-api": "OpenAI",
        "openai-codex": "OpenAI Codex",
        "openrouter": "OpenRouter",
        "qwen": "Qwen OAuth",
        "qwen-oauth": "Qwen OAuth",
        "stepfun": "StepFun Step Plan",
        "stepfun-step": "StepFun Step Plan",
        "xai": "xAI / Grok",
        "grok": "xAI / Grok",
        "xai-oauth": "xAI OAuth",
        "z-ai": "Z.AI / GLM",
        "zai": "Z.AI / GLM",
    }
    label = labels.get(canonical)
    if label is None:
        return False
    plain = re.sub(r"\x1b\[[0-9;]*m", "", status)
    return re.search(
        rf"^\s*{re.escape(label)}\s+✓(?:\s|$)",
        plain,
        flags=re.MULTILINE,
    ) is not None


class HermesAdapter(SubprocessCliAdapter):
    def authenticated(self) -> bool:
        if _hermes_python_runtime() is None:
            return False
        selection = _hermes_model_selection()
        if selection is None:
            return False
        provider = selection[0]
        completed = self._probe(
            ["hermes", "status"],
            environment_overrides=_provider_credentials(provider),
        )
        if completed is None:
            return False
        return _hermes_provider_is_ready(completed.stdout, provider)

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return _not_installed(self.id, self.command)
        prompt = _build_prompt(contract, context_files)
        runtime = _hermes_python_runtime()
        if runtime is None:
            return AdapterResult(
                status="error",
                summary="",
                error_code="backend_runtime_unavailable",
                warnings=["Hermes isolated Python runtime was not found"],
                usage={"backend": self.id},
            )
        provider = self._execution_value("provider")
        model = self._execution_value("model")
        toolset = "file" if contract.mode == "edit" else "clarify"
        hermes_home = Path(
            _backend_environment("hermes").get(
                "HERMES_HOME", str(Path.home() / ".hermes")
            )
        )
        with tempfile.TemporaryDirectory(prefix="dyro-hermes-") as raw_home:
            isolated_home = Path(raw_home)
            isolated_home.chmod(0o700)
            argv = [
                str(runtime),
                "-I",
                "-c",
                _HERMES_ONESHOT_BOOTSTRAP,
                model,
                provider,
                toolset,
                str(hermes_home),
                str(isolated_home),
            ]
            return self._run(
                argv,
                cwd=cwd,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                environment_overrides={
                    **_provider_credentials(
                        provider,
                        dotenv_path=hermes_home / ".env",
                    ),
                    "HERMES_HOME": str(isolated_home),
                    "HERMES_SAFE_MODE": "1",
                    "HERMES_IGNORE_USER_CONFIG": "1",
                    "HERMES_IGNORE_RULES": "1",
                },
            )


def _kimi_agent_markdown(*, edit_mode: bool, prompt: str) -> str:
    tools = ["Read", "Grep", "Glob"]
    if edit_mode:
        tools.extend(["Write", "Edit"])
    tool_lines = "\n".join(f"  - {name}" for name in tools)
    return (
        "---\n"
        "name: dyro-dispatch\n"
        "description: Execute one bounded Dyro dispatch task\n"
        "tools:\n"
        f"{tool_lines}\n"
        "disallowedTools:\n"
        "  - Bash\n"
        "  - Agent\n"
        "  - AgentSwarm\n"
        "  - Skill\n"
        "  - WebSearch\n"
        "  - WebFetch\n"
        "subagents: []\n"
        "---\n\n"
        "Complete this self-contained task and emit only its required JSON.\n\n"
        f"{prompt}\n"
    )


class KimiAdapter(SubprocessCliAdapter):
    def readiness_reason(self) -> str:
        return "no configured Kimi provider passed the local readiness probe"

    def authenticated(self) -> bool:
        try:
            if _kimi_env_route(os.environ) is not None:
                return self.available()
        except DispatchValidationError:
            return False
        completed = self._probe(["kimi", "provider", "list"])
        if completed is None:
            return False
        return "No providers configured" not in completed.stdout

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return _not_installed(self.id, self.command)
        provider = self._execution_value("provider")
        model = self._execution_value("model")
        route_sha256 = self._execution_value("route_sha256")
        agent = _kimi_agent_markdown(
            edit_mode=contract.mode == "edit",
            prompt=_build_prompt(contract, context_files),
        )
        source_environment = _backend_environment(self.id)
        source_home = Path(
            source_environment.get(
                "KIMI_CODE_HOME",
                str(Path.home() / ".kimi-code"),
            )
        )
        with (
            tempfile.TemporaryDirectory(prefix="dyro-kimi-") as temporary_root,
            _temporary_text_file(cwd, suffix=".md", content=agent) as agent_file,
        ):
            isolated_home = Path(temporary_root) / "home"
            _create_worker_home(isolated_home)
            _materialize_kimi_worker_home(
                source_home=source_home,
                isolated_home=isolated_home,
                provider=provider,
                model=model,
                route_sha256=route_sha256,
            )
            argv = [
                "kimi",
                "--model",
                model,
                "--prompt",
                "Execute the complete Dyro dispatch task in your agent instructions.",
                "--output-format",
                "stream-json",
                "--agent-file",
                str(agent_file),
            ]
            return self._run(
                argv,
                cwd=cwd,
                prompt="",
                timeout_seconds=timeout_seconds,
                parser=lambda text: _parse_wrapped_model_json(
                    text, backend=self.id
                ),
                environment_overrides={
                    "KIMI_CODE_HOME": str(isolated_home),
                    "KIMI_CODE_NO_AUTO_UPDATE": "1",
                    "KIMI_DISABLE_TELEMETRY": "1",
                    "KIMI_DISABLE_CRON": "1",
                    "KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY": "1",
                },
            )


def _dsh_patch(*, edit_mode: bool, provider: str, model: str) -> str:
    disabled = (
        "tool-bash",
        "tool-pwsh",
        "tool-jobs",
        "tool-skill",
        "tool-subagent-control",
        "tool-subagent-list-agents",
        "tool-subagent",
        "tool-subagent-fork",
        "tool-subagent-report",
        "tool-workflow",
        "tool-ralph",
        "tool-web",
    )
    lines = [f"- id: {name}\n  disabled: true" for name in disabled]
    lines.append(
        "- id: agent-default-model\n"
        "  config:\n"
        f"    provider: {json.dumps(provider)}\n"
        f"    model: {json.dumps(model)}"
    )
    if edit_mode:
        lines.append("- id: approval\n  config:\n    policy: never")
    return "\n".join(lines) + "\n"


def _dsh_has_default_credential() -> bool:
    environment = _backend_environment("dsh")
    if environment.get("DEEPSEEK_API_KEY"):
        return True
    home = Path(environment.get("DSH_HOME", str(Path.home() / ".dsh")))
    credentials = home / ".credentials.yaml"
    raw = _read_small_regular_bytes(credentials, require_private=True)
    if raw is None:
        return False
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    return any(
        re.match(r"^DEEPSEEK_API_KEY\s*:\s*\S+", line) is not None
        for line in lines
    )


class DshAdapter(SubprocessCliAdapter):
    def readiness_reason(self) -> str:
        return "DEEPSEEK_API_KEY is not configured for the headless DSH profile"

    def authenticated(self) -> bool:
        return self.available() and _dsh_has_default_credential()

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return _not_installed(self.id, self.command)
        if (
            self._execution_value("provider") != "deepseek-official"
            or self._execution_value("model") != "deepseek-v4-flash"
        ):
            raise DispatchValidationError(
                "DSH execution profile must use the reviewed pinned model"
            )
        provider = self._execution_value("provider")
        model = self._execution_value("model")
        prompt = _build_prompt(contract, context_files)
        source_environment = _backend_environment(self.id)
        source_home = Path(
            source_environment.get("DSH_HOME", str(Path.home() / ".dsh"))
        )
        with (
            tempfile.TemporaryDirectory(prefix="dyro-dsh-") as temporary_root,
            _temporary_text_file(cwd, suffix=".md", content=prompt) as task_file,
            _temporary_text_file(
                cwd,
                suffix=".yml",
                content=_dsh_patch(
                    edit_mode=contract.mode == "edit",
                    provider=provider,
                    model=model,
                ),
            ) as patch_file,
        ):
            isolated_home = Path(temporary_root) / "home"
            _materialize_basic_worker_home(
                backend=self.id,
                source_home=source_home,
                isolated_home=isolated_home,
                provider=provider,
                model=model,
            )
            argv = [
                "dsh",
                "--profile",
                "headless",
                "--patch",
                str(patch_file),
                f"Read {task_file.name} and execute that complete task.",
            ]
            return self._run(
                argv,
                cwd=cwd,
                prompt="",
                timeout_seconds=timeout_seconds,
                environment_overrides={
                    "DSH_HOME": str(isolated_home),
                    "DSH_PERMISSION_MODE": (
                        "workspace-write"
                        if contract.mode == "edit"
                        else "read-only"
                    ),
                    "DSH_TELEMETRY_MODE": "DISABLED",
                },
            )


def _pi_default_selection() -> tuple[str, str] | None:
    environment = _backend_environment("pi")
    root = Path(
        environment.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent"))
    )
    payload = _read_small_private_json(root / "settings.json")
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return None
    provider = payload.get("defaultProvider")
    model = payload.get("defaultModel")
    if not isinstance(provider, str) or not provider:
        return None
    return provider, model if isinstance(model, str) else ""


def _pi_default_provider() -> str | None:
    selection = _pi_default_selection()
    return selection[0] if selection is not None else None


class PiAdapter(SubprocessCliAdapter):
    def authenticated(self) -> bool:
        if not self.available():
            return False
        try:
            provider = self._execution_value("provider")
        except DispatchValidationError:
            return False
        if not provider:
            return False
        completed = self._probe(
            [
                "pi",
                "auth",
                "check",
                "--provider",
                provider,
                "--json",
                "--no-refresh",
            ],
            environment_overrides=_provider_credentials(provider),
        )
        if completed is None:
            return False
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and payload.get("status") == "ready"

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return _not_installed(self.id, self.command)
        prompt = _build_prompt(contract, context_files)
        provider = self._execution_value("provider")
        model = self._execution_value("model")
        source_environment = _backend_environment(self.id)
        source_home = Path(
            source_environment.get(
                "PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")
            )
        )
        with (
            tempfile.TemporaryDirectory(prefix="dyro-pi-") as temporary_root,
            _temporary_text_file(cwd, suffix=".md", content=prompt) as task_file,
        ):
            isolated_home = Path(temporary_root) / "home"
            _materialize_pi_worker_home(
                source_home=source_home,
                isolated_home=isolated_home,
                provider=provider,
                model=model,
            )
            tools = (
                "read,grep,find,ls,edit,write"
                if contract.mode == "edit"
                else "read,grep,find,ls"
            )
            argv = [
                "pi",
                "--mode",
                "json",
                "--print",
                "--no-session",
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-context-files",
                "--no-approve",
                "--provider",
                provider,
                "--model",
                model,
                "--tools",
                tools,
                f"@{task_file.name}",
                "Follow the attached Dyro dispatch task exactly.",
            ]
            return self._run(
                argv,
                cwd=cwd,
                prompt="",
                timeout_seconds=timeout_seconds,
                parser=lambda text: _parse_wrapped_model_json(
                    text, backend=self.id
                ),
                environment_overrides={
                    **_provider_credentials(provider),
                    "PI_CODING_AGENT_DIR": str(isolated_home),
                    "PI_TELEMETRY": "0",
                },
            )


def _not_installed(backend: str, command: str) -> AdapterResult:
    return AdapterResult(
        status="error",
        summary="",
        error_code="backend_not_installed",
        warnings=[f"command not found: {command}"],
        usage={"backend": backend},
    )


def codex_adapter() -> SubprocessCliAdapter:
    return CodexAdapter(backend_id="codex", command="codex")


def claude_adapter() -> SubprocessCliAdapter:
    return ClaudeAdapter(backend_id="claude", command="claude")


def cursor_adapter() -> SubprocessCliAdapter:
    return CursorAdapter(backend_id="cursor-agent", command="cursor-agent")


def opencode_adapter() -> SubprocessCliAdapter:
    return OpenCodeAdapter(backend_id="opencode", command="opencode")


def grok_adapter() -> SubprocessCliAdapter:
    return GrokAdapter(backend_id="grok", command="grok")


def hermes_adapter() -> SubprocessCliAdapter:
    return HermesAdapter(backend_id="hermes", command="hermes")


def kimi_adapter() -> SubprocessCliAdapter:
    return KimiAdapter(backend_id="kimi", command="kimi")


def dsh_adapter() -> SubprocessCliAdapter:
    return DshAdapter(backend_id="dsh", command="dsh")


def pi_adapter() -> SubprocessCliAdapter:
    return PiAdapter(backend_id="pi", command="pi")
