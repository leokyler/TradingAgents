"""``tradingagents env``: which dotenv files a run reads, and what they set.

Values are never printed — the command answers "why is my key ignored?" without
putting a credential on the terminal or in a scrollback buffer.
"""

from __future__ import annotations

import os
import stat

from dotenv import dotenv_values
from rich.markup import escape

from cli.display import console
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.env import (
    enterprise_env_file,
    looks_like_setting,
    project_env_file,
    user_env_file,
)


def show_env() -> None:
    """Show which dotenv files are read and which keys they set; values stay hidden."""
    sources = [
        ("project", project_env_file()),
        ("enterprise", enterprise_env_file()),
        ("shared", user_env_file()),
    ]

    # soft_wrap keeps a long path on one line, so it can be copied whole.
    console.print("Dotenv files, highest priority first:", soft_wrap=True)
    for label, path in sources:
        if path is None:
            console.print(f"  {label:<11} (none found)", soft_wrap=True)
            continue
        status = "found" if path.is_file() else "missing"
        console.print(
            f"  {label:<11} {escape(str(path))} ({status})", soft_wrap=True
        )

    shared = user_env_file()
    if os.name != "nt" and shared.is_file():
        mode = stat.S_IMODE(shared.stat().st_mode)
        if mode & 0o077:
            console.print(
                f"\n[yellow]{escape(str(shared))} is readable by other users "
                f"(mode {mode:o}); run `chmod 600 {shared}`.[/yellow]",
                soft_wrap=True,
            )

    # Which file each key in effect came from. A key the shell set to a value
    # that differs from the file's is the shell's, not the file's.
    effective: dict[str, list[str]] = {label: [] for label, _ in sources}
    effective["shell"] = []
    seen: set[str] = set()
    for label, path in sources:
        if path is None or not path.is_file():
            continue
        for name, value in dotenv_values(path).items():
            if not value or name in seen:
                continue
            seen.add(name)
            current = os.environ.get(name)
            if current == value:
                effective[label].append(name)
            elif current:
                effective["shell"].append(name)
    for name, value in os.environ.items():
        if value and name not in seen and looks_like_setting(name):
            effective["shell"].append(name)

    console.print("\nKeys in effect (values hidden):", soft_wrap=True)
    printed = False
    for label in ("shell", "project", "enterprise", "shared"):
        names = sorted(effective[label])
        if names:
            printed = True
            console.print(f"  {label:<11} {', '.join(names)}", soft_wrap=True)
    if not printed:
        console.print("  (none)", soft_wrap=True)

    console.print(
        "\nSettings: "
        f"provider={DEFAULT_CONFIG['llm_provider']} "
        f"deep={DEFAULT_CONFIG['deep_think_llm']} "
        f"quick={DEFAULT_CONFIG['quick_think_llm']} "
        f"language={DEFAULT_CONFIG['output_language']}",
        soft_wrap=True,
    )
