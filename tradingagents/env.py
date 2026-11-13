"""The shared, user-level dotenv file, and the project files read before it.

A `.env` belongs to one checkout: it is the right home for a run started inside
that checkout. A `tradingagents` installed as a tool runs from any directory
instead, so its keys and settings would be homeless — and a key typed at the
prompt would land in whatever directory the shell happened to be in. The shared
file at ``~/.tradingagents/.env`` gives them one home, beside the logs, cache,
memory and preferences the package already keeps in that directory.

The order is deliberate. The shell environment wins over every file; a
project's ``.env`` wins over the shared one, so a checkout can override a key
for the runs started inside it. The shared file is read last and only fills
values that are unset or blank, so a blank key in a copied ``.env.example``
does not shadow the shared value.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, find_dotenv


def user_env_file() -> Path:
    """The shared file: ``~/.tradingagents/.env``, or ``TRADINGAGENTS_ENV_FILE``.

    The override exists for files rendered elsewhere — a secrets manager's
    temporary output, a container mount, a password-store export — so the keys
    never have to be copied into a file by hand.
    """
    override = os.environ.get("TRADINGAGENTS_ENV_FILE")
    if override:
        return Path(override).expanduser()
    # The directory default_config.py keeps its logs, cache, memory and
    # preferences in; spelled out here because this module is read while the
    # package is still importing, before DEFAULT_CONFIG is built.
    return Path(os.path.expanduser("~")) / ".tradingagents" / ".env"


def project_env_file() -> Path | None:
    """The ``.env`` python-dotenv finds from the working directory upward."""
    found = find_dotenv(usecwd=True)
    return Path(found) if found else None


def enterprise_env_file() -> Path | None:
    """The project's ``.env.enterprise``, read for Azure credentials."""
    found = find_dotenv(".env.enterprise", usecwd=True)
    return Path(found) if found else None


def load_user_env() -> Path | None:
    """Fill unset values from the shared file; return it, or None if absent.

    Blank values count as unset, so a checkout that copied ``.env.example``
    leaves its blank keys to the shared file rather than shadowing them.
    A value already in the process environment — exported, or from the
    project's own ``.env`` — always wins and is never overwritten.
    """
    path = user_env_file()
    if not path.is_file():
        return None
    for name, value in dotenv_values(path).items():
        if value and not os.environ.get(name):
            os.environ[name] = value
    return path


def api_key_target(project_env: str | None = None) -> Path:
    """Where a key typed at the prompt is saved.

    A project's ``.env`` when one was found (``project_env``), else the shared
    file once it exists, else the working directory's own ``.env`` — upstream's
    behaviour, kept so an install that never set up a shared file keeps working
    as before. The caller checks the project file because it already calls
    ``find_dotenv`` with the working directory in mind.
    """
    if project_env:
        return Path(project_env)
    shared = user_env_file()
    if shared.is_file():
        return shared
    return Path.cwd() / ".env"


# Variables a run reads that are neither TRADINGAGENTS_* nor a *_API_KEY/_TOKEN.
_OTHER_SETTINGS = frozenset({
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_ENDPOINT",
    "OLLAMA_BASE_URL",
    "OPENCODE_GO_SESSION_ID",
    "SEC_EDGAR_USER_AGENT",
    "TYPESAFE_BASE_URL",
    "TYPESAFE_DEFAULT_MODEL",
})


def looks_like_setting(name: str) -> bool:
    """Whether an environment variable is one a run would read.

    Lets ``tradingagents env`` list the shell's own settings without printing
    PATH, DISPLAY and the rest of the environment.
    """
    if name == "TRADINGAGENTS_ENV_FILE":
        return False  # the pointer to the file, not a setting inside it
    return (
        name.startswith("TRADINGAGENTS_")
        or name.endswith(("_API_KEY", "_TOKEN"))
        or name in _OTHER_SETTINGS
    )
