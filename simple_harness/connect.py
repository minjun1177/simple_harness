"""`/connect` - point the harness at a model provider.

Interactive by default, because the useful part is picking a model from the
list the provider actually offers rather than typing an id from memory. It also
takes arguments for the times you already know what you want:

    /connect                      pick a provider, then a model
    /connect anthropic            pick a model from Anthropic
    /connect openai gpt-4o        connect straight to a model
    /connect status               what is connected, and what could be
"""

import os

from simple_harness import config
from simple_harness import providers
from simple_harness.config import S, _hr


def _ask(prompt: str) -> str:
    try:
        return config.safe_text(input(prompt).strip())
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _choose(title: str, rows: list, formatter) -> int:
    """Show a numbered list and return the chosen index, or -1."""
    print(f"\n  {S.BOLD}{S.ACCENT}{title}{S.R}")
    for i, row in enumerate(rows, 1):
        print(f"  {S.ACCENT}{i:3}.{S.R} {formatter(row)}")
    print(f"  {S.MUTED}  0.{S.R} {S.GRAY}Cancel{S.R}\n")
    raw = _ask(f"  {S.INFO}Select{S.R} {S.MUTED}(0~{len(rows)}){S.R} {S.INFO}›{S.R} ")
    if not raw or raw == "0":
        return -1
    try:
        index = int(raw) - 1
    except ValueError:
        return -1
    return index if 0 <= index < len(rows) else -1


def show_status() -> None:
    print(f"\n  {S.BOLD}{S.ACCENT}Providers{S.R}")
    print(f"  {_hr(width=60)}")
    active = providers.current().name
    for name, factory in providers.PROVIDERS.items():
        provider = providers.build(name)
        marker = f" {S.OK}◀ connected{S.R}" if name == active else ""
        problem = provider.ready()
        state = f"{S.ERR}{problem}{S.R}" if problem else f"{S.OK}ready{S.R}"
        print(f"  {S.ACCENT}{provider.label}{S.R} {S.MUTED}({name}){S.R}  {state}{marker}")
        if provider.model:
            print(f"  {S.MUTED}│{S.R}  {S.GRAY}model{S.R}  {provider.model}")
        how = ("native tool calling" if provider.supports_native_tools
               and getattr(config, "NATIVE_TOOLS", True)
               else "text <tool_call> protocol")
        print(f"  {S.MUTED}│{S.R}  {S.GRAY}tools{S.R}  {S.MUTED}{how}{S.R}")
        if provider.key_env:
            source = provider.key_source or f"{S.MUTED}not set{S.R}"
            print(f"  {S.MUTED}│{S.R}  {S.GRAY}key{S.R}    {source}"
                  f"   {S.MUTED}{provider.key_help}{S.R}")
        print(f"  {S.MUTED}╰─{S.R}")
    print(f"\n  {S.GRAY}Keys are read from the environment first, then "
          f"{S.MUTED}{providers.CONFIG_PATH}{S.GRAY}.{S.R}")
    print(f"  {S.GRAY}Connect with {S.ACCENT}/connect <provider> [model]{S.GRAY}.{S.R}\n")


def _pick_provider() -> str:
    rows = [providers.build(name) for name in providers.PROVIDERS]
    active = providers.current().name

    def render(provider):
        mark = f" {S.OK}◀ connected{S.R}" if provider.name == active else ""
        problem = provider.ready()
        note = f"{S.MUTED}{problem}{S.R}" if problem else f"{S.GRAY}{provider.model}{S.R}"
        return f"{S.WHITE}{provider.label}{S.R}  {note}{mark}"

    index = _choose("Connect to", rows, render)
    return rows[index].name if index >= 0 else ""


def _ensure_key(name: str) -> bool:
    """Ask for an API key if the provider needs one and has none."""
    provider = providers.build(name)
    if not provider.needs_key or provider.api_key:
        return True

    print(f"\n  {S.WARN}{provider.label} needs an API key.{S.R}")
    print(f"  {S.MUTED}{provider.key_help}{S.R}")
    print(f"  {S.MUTED}Set {' or '.join(provider.key_env)} in the environment to keep it "
          f"out of a file, or paste it here to save it to{S.R}")
    print(f"  {S.MUTED}{providers.CONFIG_PATH} (owner-only).{S.R}\n")
    key = _ask(f"  {S.INFO}API key{S.R} {S.MUTED}(blank to cancel){S.R} {S.INFO}›{S.R} ")
    if not key:
        print(f"  {S.GRAY}Cancelled.{S.R}\n")
        return False
    providers.settings_for(name)["api_key"] = key
    return True


def _pick_model(name: str) -> str:
    provider = providers.build(name)
    print(f"  {S.MUTED}⟳ asking {provider.label} what it offers…{S.R}", end="", flush=True)
    try:
        models = provider.list_models()
    except Exception as error:
        print(f"\r\033[K  {S.ERR}✗ Could not list models: {str(error)[:160]}{S.R}\n")
        return _ask(f"  {S.INFO}Model id{S.R} {S.MUTED}(blank to cancel){S.R} {S.INFO}›{S.R} ")
    print("\r\033[K", end="")

    if not models:
        return _ask(f"  {S.INFO}Model id{S.R} {S.MUTED}(blank to cancel){S.R} {S.INFO}›{S.R} ")

    def render(entry):
        detail = f"  {S.GRAY}{entry['detail']}{S.R}" if entry.get("detail") else ""
        return f"{S.WHITE}{entry['name']}{S.R}{detail}"

    index = _choose(f"{provider.label} models", models, render)
    return models[index]["name"] if index >= 0 else ""


def run(argument: str) -> None:
    """Handle `/connect ...`. Everything it prints, it prints itself."""
    parts = argument.split()
    if parts and parts[0].lower() in ("status", "list", "-l"):
        show_status()
        return

    name = parts[0].lower() if parts else ""
    model = parts[1] if len(parts) > 1 else ""

    if name and name not in providers.PROVIDERS:
        print(f"  {S.ERR}✗ Unknown provider '{name}'. "
              f"Choose from: {', '.join(providers.PROVIDERS)}{S.R}\n")
        return

    if not name:
        name = _pick_provider()
        if not name:
            print(f"  {S.GRAY}Cancelled.{S.R}\n")
            return

    if not _ensure_key(name):
        return

    if not model:
        model = _pick_model(name)
        if not model:
            print(f"  {S.GRAY}Cancelled.{S.R}\n")
            return

    provider, problem = providers.connect(name, model=model)
    if problem:
        print(f"  {S.WARN}⚠ Connected to {provider.label}, but {problem}.{S.R}\n")
        return
    where = f" {S.MUTED}(key {provider.key_source}){S.R}" if provider.key_source else ""
    print(f"  {S.OK}✓ Connected: {provider.label} · {provider.model}{S.R}{where}\n")


if __name__ == "__main__":
    print("This file can not run directly.")
