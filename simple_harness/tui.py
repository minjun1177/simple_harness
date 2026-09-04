import os
import sys
import subprocess
import textwrap
from simple_harness import config
from simple_harness.config import S, tw, th, _hr, _visible_len
from simple_harness.session import load_memory, list_sessions
from simple_harness.context import (_estimate_tokens, _get_ctx_budget, _get_conv_pairs,
                                    token_turns)
from simple_harness.skills import list_skills, skill_dirs
from simple_harness import mcp_client
from simple_harness import providers
from simple_harness import toolspec


def _get_git_info() -> str:
    try:
        branch = subprocess.check_output(["git", "branch", "--show-current"], stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", shell=False).strip()
        if not branch: return "none"
        status = subprocess.check_output(["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", shell=False)
        modified = len([line for line in status.splitlines() if line.strip()])
        return f"{branch} (clean)" if modified == 0 else f"{branch} ({modified} modified)"
    except Exception:
        return "none"

def _get_rules_info() -> str:
    for filename in ["CONVENTIONS.md", ".clauderc"]:
        if os.path.exists(filename):
            return f"{filename} (active)"
    return "none"

def _get_python_info() -> str:
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    in_venv = hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)
    return f"{ver} (venv)" if in_venv else ver

def _get_skills_info() -> str:
    items = list_skills()
    if not items:
        return "none"
    sources = {s["source"] for s in items}
    return f"{len(items)} available ({', '.join(sorted(sources))})"

def _get_mcp_info() -> str:
    if not config.MCP_ENABLED:
        return "disabled"
    return mcp_client.status_summary()


# "SIMPLE HARNESS" does not fit on one line in this face - it comes to about
# 109 columns - so the two words are stacked. On a terminal too narrow even for
# that, a three-row face carries the same name rather than wrapping into
# nonsense.
_LOGO_WIDE = """\
    ███████╗██╗███╗   ███╗██████╗ ██╗     ███████╗
    ██╔════╝██║████╗ ████║██╔══██╗██║     ██╔════╝
    ███████╗██║██╔████╔██║██████╔╝██║     █████╗  
    ╚════██║██║██║╚██╔╝██║██╔═══╝ ██║     ██╔══╝  
    ███████║██║██║ ╚═╝ ██║██║     ███████╗███████╗
    ╚══════╝╚═╝╚═╝     ╚═╝╚═╝     ╚══════╝╚══════╝
    ██╗  ██╗ █████╗ ██████╗ ███╗   ██╗███████╗███████╗███████╗
    ██║  ██║██╔══██╗██╔══██╗████╗  ██║██╔════╝██╔════╝██╔════╝
    ███████║███████║██████╔╝██╔██╗ ██║█████╗  ███████╗███████╗
    ██╔══██║██╔══██║██╔══██╗██║╚██╗██║██╔══╝  ╚════██║╚════██║
    ██║  ██║██║  ██║██║  ██║██║ ╚████║███████╗███████║███████║
    ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚══════╝"""

_LOGO_NARROW = """\
    ╔═╗╦╔╦╗╔═╗╦  ╔═╗  ╦ ╦╔═╗╦═╗╔╗╔╔═╗╔═╗╔═╗
    ╚═╗║║║║╠═╝║  ║╣   ╠═╣╠═╣╠╦╝║║║║╣ ╚═╗╚═╗
    ╚═╝╩╩ ╩╩  ╩═╝╚═╝  ╩ ╩╩ ╩╩╚═╝╚╝╚═╝╚═╝╚═╝"""


# What `_welcome` prints besides the logo: the eleven fact lines, the hint, the
# rule under it, one blank line, and the prompt the person then types at.
_WELCOME_CHROME = 15


def _logo() -> str:
    """The face, sized to the terminal it is about to be printed into.

    Both directions matter and only one of them used to. The wide face is
    twelve rows and the rest of the welcome is fifteen more; on a terminal
    shorter than that the whole banner scrolled off before the person could
    read it - the art went first, being at the top, so the one thing they
    were meant to see was the one thing they never did.
    """
    rows = len(_LOGO_WIDE.splitlines()) + 2        # a blank line above and below
    fits = config.tw() >= 66 and th() >= rows + _WELCOME_CHROME
    return f"{S.ACCENT}{S.BOLD}\n{_LOGO_WIDE if fits else _LOGO_NARROW}\n{S.R}"


def _welcome():
    memory_count = len(load_memory())
    session_count = len(list_sessions())
    
    workspace = os.getcwd()
    git_info = _get_git_info()
    rules_info = _get_rules_info()
    python_info = _get_python_info()
    # Counted from the tool table, which is the only tool list (5.1). Scraping
    # the system prompt for it used to report "0 tools" on any model that
    # supports native tool calling, because the schemas travel with the request
    # there and never reach the prompt at all.
    tools_count = len(toolspec.TOOLS) + mcp_client.tool_count()

    print(_logo())
    print(f"  {S.GRAY}model{S.R}      {S.WHITE}{providers.status_line()}{S.R}")
    print(f"  {S.GRAY}os{S.R}         {S.WHITE}{config.CURRENT_OS}{S.R}")
    print(f"  {S.GRAY}python{S.R}     {S.WHITE}{python_info}{S.R}")
    print(f"  {S.GRAY}workspace{S.R}  {S.WHITE}{workspace}{S.R}")
    print(f"  {S.GRAY}git{S.R}        {S.WHITE}{git_info}{S.R}")
    print(f"  {S.GRAY}rules{S.R}      {S.WHITE}{rules_info}{S.R}")
    print(f"  {S.GRAY}tools{S.R}      {S.WHITE}{tools_count} active{S.R}")
    print(f"  {S.GRAY}skills{S.R}     {S.WHITE}{_get_skills_info()}{S.R}")
    print(f"  {S.GRAY}mcp{S.R}        {S.WHITE}{_get_mcp_info()}{S.R}")
    print(f"  {S.GRAY}memory{S.R}     {S.WHITE}{memory_count} item{'s' if memory_count != 1 else ''}{S.R}")
    print(f"  {S.GRAY}sessions{S.R}   {S.WHITE}{session_count} saved{S.R}")
    print(f"  {S.GRAY}Type {S.ACCENT}/help{S.GRAY} for commands · {S.ACCENT}/exit{S.GRAY} to quit{S.R}")
    if not config.PROMPT_TOOLKIT_AVAILABLE:
        print(f"  {S.WARN}⚠ Tip: pip install prompt_toolkit for history & autocompletion.{S.R}")
    print(f"  {_hr()}")
    # Exactly one blank line under it, and nothing else: every row spent here is
    # a row of the banner that scrolls off the top before it is read.
    print()


def _show_help():
    commands = [
        ("/help",  "Show this help message"),
        ("/usage", "Show token usage history graph"),
        ("/clear", "Clear conversation history"),
        ("/connect", "Connect a provider (Ollama, Anthropic, OpenAI, Gemini)"),
        ("/connect status", "Show every provider and whether it is usable"),
        ("/model", "Show the current provider and pick a model"),
        ("/models", "List models from the connected provider"),
        ("/sessions", "List saved sessions"),
        ("/load <id|title>", "Load a past session by ID or title"),
        ("/title [name]", "Show or set the current session's title"),
        ("/autotitle <on/off>", "Toggle letting the model name new sessions"),
        ("/exit",  "Exit the chat"),
        ("/automode <on/off>", "Toggle allow modal"),
        ("/fullcontent <on/off>", "Toggle returning all file content"),
        ("/record <on/off>", "Toggle saving chat history to sessions"),
        ("/export [filename]", "Export conversation to a markdown file"),
        ("/system <prompt>", "Change the system prompt"),
        ("/planmode <on/off>", "Toggle plan mode (forces AI to write a plan first)"),
        ("/skills", "List available skills"),
        ("/skills reload", "Rescan skill directories and refresh the system prompt"),
        ("/skill <name>", "Load a skill into the current conversation manually"),
        ("/mcp", "Show attached MCP servers and their tools"),
        ("/mcp reload", "Re-read .mcp.json and reconnect every server"),
        ("/mcp connect <name>", "Reconnect a single MCP server"),
        ("/mcp tools [name]", "List the tools an MCP server exposes"),
        ("/mcp resources [name]", "List MCP resources"),
        ("/mcp prompt <server> <name>", "Insert an MCP prompt into the conversation"),
        ("/perms", "Show the tool permission rules"),
        ("/perms reload", "Re-read the permission rule files"),
        ("/perms allow|deny <rule>", "Add a rule, e.g. /perms allow run_cmd(git *)"),
        ("/think <on/off>", "Show or hide a reasoning model's thinking"),
        ("/deepthink", "Show the plan-check-build-review-revise-verify chain and whether it is on"),
        ("/deepthink <on/off>", "Turn that chain on or off"),
        ("/agents", "Show the other AI agents working in this project"),
        ("/agents say <text>", "Say something to all of them yourself"),
        ("/agents release <path>", "Take a file back from the agent holding it"),
        ("/agents <on/off>", "Whether this session appears on the agent board"),
        ("/vm", "Show the Python scratch process run_python uses, and where it runs"),
        ("/vm reset", "Throw away every variable the model left in it"),
        ("/vm stop", "End the process; the next run_python starts a new one"),
        ("/set", "Show every setting, and which ones you have changed"),
        ("/set <NAME> <value>", "Change one, e.g. /set NUM_CTX 32768. Saved for next time"),
        ("/set <NAME> default", "Put it back to what config.py says"),
        ("/connect forget <provider>", "Delete the API key saved for a provider"),
        ("/undo", "Take back the last file change the AI committed"),
        ("/autocommit", "Whether each AI edit gets its own git commit, and the recent ones"),
        ("/autocommit <on/off>", "Turn that on or off"),
    ]
    # Not slash commands: these two act on the line itself, so they are listed
    # apart from the table rather than pretending to belong to it.
    prefixes = [
        ("@<path>", "Attach a file or directory to the message. Typing @ opens a "
                    "list of what is here - arrows to move, Tab to insert"),
        ("!<command>", "Run a shell command yourself. Its output joins the "
                       "conversation, so the next question can be about it"),
    ]
    print()
    print(f"  {S.BOLD}{S.ACCENT}Commands{S.R}")
    print(f"  {_hr(width=44)}")
    for cmd, desc in commands:
        print(f"  {S.ACCENT}{cmd:22}{S.R} {S.GRAY}{desc}{S.R}")
    print()
    print(f"  {S.BOLD}{S.ACCENT}In a message{S.R}")
    print(f"  {_hr(width=44)}")
    for cmd, desc in prefixes:
        wrapped = textwrap.wrap(desc, max(30, tw() - 32)) or [""]
        print(f"  {S.ACCENT}{cmd:22}{S.R} {S.GRAY}{wrapped[0]}{S.R}")
        for line in wrapped[1:]:
            print(f"  {' ' * 22} {S.GRAY}{line}{S.R}")
    print()


def _show_skills():
    items = list_skills()
    print()
    print(f"  {S.BOLD}{S.ACCENT}Skills{S.R}")
    print(f"  {_hr(width=60)}")

    if not items:
        print(f"  {S.GRAY}No skills found. Create one at:{S.R}")
        for source, directory in skill_dirs():
            print(f"  {S.GRAY}•{S.R} {os.path.join(directory, '<skill-name>', 'SKILL.md')} {S.MUTED}({source}){S.R}")
        print()
        return

    wrap_width = max(30, tw() - 12)
    for skill in items:
        marker = f"  {S.OK}◀ loaded{S.R}" if skill["name"] in config.LOADED_SKILLS else ""
        print(f"  {S.ACCENT}{skill['name']}{S.R} {S.MUTED}[{skill['source']}]{S.R}{marker}")
        for line in textwrap.wrap(skill["description"], width=wrap_width) or [""]:
            print(f"  {S.MUTED}│{S.R}  {S.GRAY}{line}{S.R}")
        if skill["allowed_tools"]:
            print(f"  {S.MUTED}│{S.R}  {S.MUTED}tools: {', '.join(skill['allowed_tools'])}{S.R}")
        print(f"  {S.MUTED}╰─ {skill['path']}{S.R}")
    print(f"\n  {S.GRAY}Load one with {S.ACCENT}/skill <name>{S.GRAY}, or let the model call {S.ACCENT}use_skill{S.GRAY} on its own.{S.R}\n")


def _show_perms():
    """`/perms` - the rules that decide what runs without asking."""
    from simple_harness import permissions

    permissions.load_rules()
    print()
    print(f"  {S.BOLD}{S.ACCENT}Tool Permissions{S.R}")
    print(f"  {_hr(width=60)}")

    if not config.PERMISSIONS_ENABLED:
        print(f"  {S.WARN}Permission rules are off (config.PERMISSIONS_ENABLED = False).{S.R}\n")
        return

    for problem in permissions.errors:
        print(f"  {S.ERR}✗ {problem}{S.R}")

    denied = permissions.rules_for("deny")
    allowed = permissions.rules_for("allow")

    if denied:
        print(f"  {S.ERR}deny{S.R} {S.MUTED}(never runs, never asks){S.R}")
        for rule, source in denied:
            print(f"  {S.MUTED}│{S.R}  {S.WHITE}{rule}{S.R}")
    if allowed:
        if denied:
            print(f"  {S.MUTED}│{S.R}")
        print(f"  {S.OK}allow{S.R} {S.MUTED}(runs without asking){S.R}")
        for rule, source in allowed:
            print(f"  {S.MUTED}│{S.R}  {S.WHITE}{rule}{S.R}")

    if not denied and not allowed:
        print(f"  {S.GRAY}No rules. Every guarded tool asks for approval.{S.R}")
        print(f"  {S.GRAY}Answer {S.ACCENT}a{S.GRAY} at an approval prompt to add one, or write:{S.R}")
        for source, path in permissions.config_paths()[::2]:
            print(f"  {S.GRAY}•{S.R} {path} {S.MUTED}({source}){S.R}")
    else:
        print(f"  {S.MUTED}╰─{S.R}")
        for path in permissions.rule_sources():
            print(f"  {S.MUTED}   {path}{S.R}")

    state = "OFF" if config.AUTO_ALLOW else "ON"
    print(f"\n  {S.GRAY}Approval prompts are {state}"
          f"{S.MUTED} (/automode toggles them for everything at once){S.R}\n")


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


def _show_mcp(detail: str = "", only: str = ""):
    """`/mcp` - the state of every configured server, and what it exposes."""
    servers = mcp_client.all_servers()
    if only:
        wanted = mcp_client.get_server(only)
        if wanted is None:
            print(f"\n  {S.ERR}✗ No MCP server named '{only}'.{S.R}\n")
            return
        servers = [wanted]
    print()
    print(f"  {S.BOLD}{S.ACCENT}MCP Servers{S.R}")
    print(f"  {_hr(width=60)}")

    if not config.MCP_ENABLED:
        print(f"  {S.WARN}MCP is disabled (config.MCP_ENABLED = False).{S.R}\n")
        return

    for problem in getattr(mcp_client.load_servers, "errors", []):
        print(f"  {S.ERR}✗ {problem}{S.R}")

    if not servers:
        print(f"  {S.GRAY}No servers configured. Declare one in:{S.R}")
        for source, path in mcp_client.config_paths():
            print(f"  {S.GRAY}•{S.R} {path} {S.MUTED}({source}){S.R}")
        print(f"\n  {S.GRAY}Example:{S.R}")
        print(f'  {S.MUTED}{{"mcpServers": {{"files": {{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}}}}}}{S.R}')
        print()
        return

    state_style = {
        "connected": (S.OK, "●"),
        "failed":    (S.ERR, "✗"),
        "disabled":  (S.MUTED, "○"),
        "idle":      (S.GRAY, "○"),
    }
    wrap_width = max(30, tw() - 12)

    for server in servers:
        colour, glyph = state_style.get(server.state, (S.GRAY, "○"))
        version = server.info.get("version", "")
        title = server.info.get("name", "") or server.name
        print(f"  {colour}{glyph}{S.R} {S.ACCENT}{server.name}{S.R} "
              f"{S.MUTED}[{server.source}]{S.R} {colour}{server.state}{S.R}"
              f"{f'  {S.MUTED}{title} {version}{S.R}' if server.state == 'connected' else ''}")
        transport = server.transport.kind if server.transport else (
            "stdio" if server.spec.get("command") else "http")
        print(f"  {S.MUTED}│{S.R}  {S.GRAY}{transport}:{S.R} {S.MUTED}{server.target[:wrap_width]}{S.R}")

        if server.state == "connected":
            counts = [_plural(len(server.tools), "tool")]
            if server.resources:
                counts.append(_plural(len(server.resources), "resource"))
            if server.prompts:
                counts.append(_plural(len(server.prompts), "prompt"))
            print(f"  {S.MUTED}│{S.R}  {S.GRAY}{' · '.join(counts)}{S.R}")
            if detail in ("tools", "all") and server.tools:
                for tool in server.tools:
                    description = " ".join(str(tool.get("description") or "").split())
                    print(f"  {S.MUTED}│{S.R}    {S.WHITE}{mcp_client.qualified_name(server, tool.get('name', ''))}{S.R}")
                    if description:
                        for line in textwrap.wrap(description, width=wrap_width - 6)[:2]:
                            print(f"  {S.MUTED}│{S.R}      {S.GRAY}{line}{S.R}")
            if detail in ("prompts", "all") and server.prompts:
                for prompt in server.prompts:
                    print(f"  {S.MUTED}│{S.R}    {S.PURPLE}prompt{S.R} {S.WHITE}{prompt.get('name', '')}{S.R}"
                          f"  {S.GRAY}{' '.join(str(prompt.get('description') or '').split())[:70]}{S.R}")
        elif server.state == "failed":
            for line in textwrap.wrap(server.error, width=wrap_width)[:4]:
                print(f"  {S.MUTED}│{S.R}  {S.ERR}{line}{S.R}")
        print(f"  {S.MUTED}╰─{S.R}")

    print(f"\n  {S.GRAY}Reload with {S.ACCENT}/mcp reload{S.GRAY} · see every tool with {S.ACCENT}/mcp tools{S.GRAY}.{S.R}\n")


def _fmt_tool_call(name: str, arguments: dict):
    print()
    print(f"  {S.INFO}▸{S.R} {S.BOLD}{name}{S.R}")
    if arguments:
        items = list(arguments.items())
        for i, (k, v) in enumerate(items):
            is_last = (i == len(items) - 1)
            connector = "╰" if is_last else "├"
            val = str(v)
            if len(val) > 120:
                val = val[:117] + "..."
            print(f"  {S.MUTED}{connector}─{S.R} {S.GRAY}{k}:{S.R} {val}")


def _fmt_tool_result(name: str, result: str):
    max_preview = 600
    if len(result) > max_preview:
        preview = result[:max_preview]
        footer = f"{S.GRAY}… {len(result)} chars total{S.R}"
    else:
        preview = result
        footer = ""

    lines = preview.split("\n")
    print(f"  {S.MUTED}│{S.R}")
    for line in lines:
        print(f"  {S.MUTED}│{S.R}  {line}")
    if footer:
        print(f"  {S.MUTED}│{S.R}  {footer}")
    print(f"  {S.MUTED}│{S.R}")
    # Anchored, not searched. Every failure this harness produces puts its
    # marker at the front - `tools._name_the_failure`, `_commit_if_changed` and
    # `deepthink` all decide the same way - so a result that merely *contains*
    # "[Error]" is a result, not a failure: a page `get_url` fetched that
    # discusses one, a file whose source raises one, a `run_cmd` grep that
    # matched the word. Searching the whole body put a red "error" under output
    # that was perfectly good, and did it more often the better the tool worked.
    if result.startswith(config.TOOL_ERROR_PREFIX):
        print(f"  {S.ERR}╰─ error{S.R}")
    elif result.startswith(config.TOOL_REFUSAL_PREFIX):
        # A refusal is not a failure and not a success: a deny rule, a read-only
        # stage or a declined prompt each stopped the tool before it ran. Only
        # "User denied" was recognised here before, so the rest reported "done"
        # under output saying plainly that nothing had been done.
        print(f"  {S.WARN}╰─ refused{S.R}")
    else:
        print(f"  {S.OK}╰─ done{S.R}")


def _fmt_setting(value) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "(none)"
    if isinstance(value, str):
        return value or '""'
    return f"{value:,}" if isinstance(value, int) else str(value)


def _show_settings(only: str = "") -> None:
    """`/set`: what can be changed, what it is, and what it started as.

    Grouped by the prefix the names already carry, because sixty settings in
    one alphabetical column is a list nobody reads to the end of. A value that
    is no longer the default is marked and shows what it was - that line is the
    whole reason a person opens this rather than reading `config.py`.
    """
    settings = config.settable()
    if only:
        only = only.strip().upper()
        if only not in settings:
            print(f"  {S.ERR}✗ '{only}' is not a setting.{S.R} "
                  f"{S.GRAY}/set on its own lists them.{S.R}\n")
            return
        settings = {only: settings[only]}

    changed = config.saved_settings()
    defaults = config.defaults()
    groups: dict = {}
    for name, value in settings.items():
        prefix = name.split("_")[0]
        # A prefix shared by one setting is not a group, it is a name.
        key = prefix if sum(1 for n in settings if n.startswith(prefix + "_")) > 1 else ""
        groups.setdefault(key, []).append((name, value))

    print(f"\n  {S.BOLD}{S.ACCENT}Settings{S.R}  "
          f"{S.GRAY}/set <NAME> <value>  ·  /set <NAME> default{S.R}")
    print(f"  {_hr(width=60)}")
    width = max((len(n) for n in settings), default=10)
    for key in sorted(groups, key=lambda k: (k == "", k)):
        rows = groups[key]
        if key and not only:
            print(f"  {S.MUTED}{key.lower()}{S.R}")
        for name, value in rows:
            mark = f"{S.OK}•{S.R}" if name in changed else " "
            was = (f"  {S.MUTED}(was {_fmt_setting(defaults.get(name))}){S.R}"
                   if name in changed else "")
            print(f"  {mark} {S.GRAY}{name:<{width}}{S.R}  "
                  f"{S.WHITE}{_fmt_setting(value)}{S.R}{was}")
    if changed:
        print(f"\n  {S.MUTED}{len(changed)} changed, saved in "
              f"{config.SETTINGS_FILE}{S.R}")
    else:
        print(f"\n  {S.MUTED}All at their defaults. Changes are saved in "
              f"{config.SETTINGS_FILE}{S.R}")
    print()


def _approval_prompt(action_label: str, details: list[tuple[str, str]], rule: str = "") -> bool:
    from simple_harness.renderer import _disp_width
    from simple_harness import permissions

    # A permission rule already said yes, or /automode is on.
    if config.AUTO_ALLOW or config.POLICY_AUTO_ALLOW:
        return True

    print()
    print(f"  {S.WARN}⚠  {S.BOLD}Approval Required{S.R}")

    w = max(40, tw() - 8)
    print(f"  {S.WARN}╭{'─' * w}╮{S.R}")
    for label, value in details:
        label_w = _disp_width(label)
        max_val = w - label_w - 5
        val_display = value if len(value) <= max_val else value[:max_val - 3] + "..."
        val_lines = val_display.split("\n")
        first = True
        for vl in val_lines:
            vl_w = _disp_width(vl)
            if first:
                content_w = 2 + label_w + 2 + vl_w
                pad = " " * max(0, w - content_w)
                print(f"  {S.WARN}│{S.R}  {S.GRAY}{label}:{S.R} {vl}{pad}{S.WARN}│{S.R}")
                first = False
            else:
                content_w = 2 + label_w + 2 + vl_w
                pad = " " * max(0, w - content_w)
                pad_left = " " * (label_w + 2)
                print(f"  {S.WARN}│{S.R}  {pad_left}{vl}{pad}{S.WARN}│{S.R}")
    print(f"  {S.WARN}╰{'─' * w}╯{S.R}")

    if rule:
        print(f"  {S.MUTED}a = always allow {S.GRAY}{rule}{S.MUTED} (saved to .permissions.json){S.R}")
        choices = f"{S.MUTED}[{S.OK}y{S.MUTED}/{S.ERR}n{S.MUTED}/{S.INFO}a{S.MUTED}]{S.R}"
    else:
        choices = f"{S.MUTED}[{S.OK}y{S.MUTED}/{S.ERR}n{S.MUTED}]{S.R}"

    try:
        answer = input(f"  {S.WARN}Allow? {choices} {S.WARN}›{S.R} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if answer == "a" and rule:
        saved, where = permissions.add_rule(rule, "allow")
        if saved:
            print(f"  {S.OK}✓ Always allowing {rule}{S.R} {S.MUTED}({where}){S.R}")
        else:
            print(f"  {S.ERR}✗ Could not save the rule: {where}{S.R}")
        return True
    return answer == "y"


def _fmt_tokens(prompt_t: int, comp_t: int, total_dur: float, eval_dur: float,
                turn: dict | None = None):
    """The cost of the request that just finished, and of the turn it ended.

    The first line is one request: its TPS and its duration only mean anything
    per request. The second is what the person actually spent on the thing they
    asked for - the request above plus every follow-up the tool loop made to
    get there - and it appears only when those are different numbers, so an
    ordinary answer looks exactly as it always did.
    """
    total = prompt_t + comp_t
    tps = (comp_t / eval_dur) if eval_dur > 0 else 0.0
    print(
        f"\n  {S.MUTED}─ tokens: "
        f"{S.GRAY}{prompt_t}{S.MUTED} in · "
        f"{S.GRAY}{comp_t}{S.MUTED} out · "
        f"{S.GRAY}{total}{S.MUTED} total · "
        f"{S.GRAY}{total_dur:.1f}s{S.MUTED} · "
        f"{S.GRAY}TPS: {tps:.1f}{S.R}"
    )
    if turn and turn.get("requests", 0) > 1:
        print(f"  {S.MUTED}─ this turn: "
              f"{S.GRAY}{turn['requests']}{S.MUTED} requests · "
              f"{S.GRAY}{turn['prompt'] + turn['completion']:,}{S.MUTED} tokens{S.R}")
    print()


def _fixed_overhead(messages: list[dict]) -> int:
    """What every request pays before the conversation is even counted.

    Almost all of it is the tool list, and *where* the tool list travels is not
    the point: over the text protocol it is in the system prompt, over a native
    interface it is in the request's own `tools` field instead - and it is
    tokenised either way. Measured against gemma4:e4b the two come to 6,013 and
    5,958 tokens. Counting only `messages[0]` would report 1,866 for the native
    one and quietly hide two thirds of the bill.
    """
    if not messages:
        return 0
    total = _estimate_tokens(messages[:1])
    try:
        from simple_harness import llm_client
        if llm_client.native_enabled():
            import json as _json
            total += _estimate_tokens([{"role": "user", "content": _json.dumps(
                llm_client.native_tools(), ensure_ascii=False)}])
    except Exception:
        pass            # a display must not fail because a provider is down
    return total


def display_usage_graph(messages: list[dict]):
    if not config.token_history:
        print(f"  {S.GRAY}No token usage data yet.{S.R}")
    else:
        # One bar per turn, not per request to the model. Answering one thing
        # the person asked takes another request after every tool result, and
        # counting those separately drew a conversation five bars long that the
        # person had spoken in once.
        turns = token_turns()
        totals = [t["prompt"] + t["completion"] for t in turns]
        max_val = max(totals) if totals else 0

        print(f"\n  {S.BOLD}{S.ACCENT}Token Usage History{S.R}")
        print(f"  {_hr(width=max(len(totals) * 4 + 10, 30))}")

        rows = 8
        if max_val == 0:
            print(f"  {S.GRAY}No tokens used yet.{S.R}")
        else:
            bar_chars = ["░", "▒", "▓", "█"]
            for row in range(rows, 0, -1):
                threshold = (max_val / rows) * row
                label = f"{int(threshold):>7}"
                line = f"  {S.MUTED}{label} │{S.R} "
                for val in totals:
                    ratio = val / max_val if max_val else 0
                    bar_level = int(ratio * rows)
                    if bar_level >= row:
                        intensity = min(3, int((val / max_val) * 4))
                        line += f"{S.ACCENT}{bar_chars[intensity]}{bar_chars[intensity]} {S.R}"
                    else:
                        line += "   "
                print(line)

            x_border = "  " + " " * 8 + f"{S.MUTED}╰" + "─" * (len(totals) * 3) + f"{S.R}"
            print(x_border)
            x_labels = "  " + " " * 9
            for i in range(1, len(totals) + 1):
                x_labels += f"{S.GRAY}{i:02d}{S.R} "
            print(x_labels)

        total_prompt = sum(t["prompt"] for t in turns)
        total_comp = sum(t["completion"] for t in turns)
        total_all = total_prompt + total_comp
        requests = sum(t["requests"] for t in turns)

        print(f"\n  {S.BOLD}Cumulative Token Usage{S.R}")
        print(f"  {S.GRAY}prompt{S.R} {S.WHITE}{total_prompt:,}{S.R}  "
              f"{S.GRAY}completion{S.R} {S.WHITE}{total_comp:,}{S.R}  "
              f"{S.GRAY}total{S.R} {S.BOLD}{S.WHITE}{total_all:,}{S.R}")
        # The count the bars no longer show. A turn that took nine requests is
        # a turn worth looking at, and it is the tool calls that make it nine.
        print(f"  {S.GRAY}turns{S.R} {S.WHITE}{len(turns)}{S.R}  "
              f"{S.GRAY}model requests{S.R} {S.WHITE}{requests:,}{S.R}  "
              f"{S.GRAY}per turn{S.R} {S.WHITE}{requests / max(1, len(turns)):.1f}{S.R}")
        busiest = max(turns, key=lambda t: t["requests"])
        if busiest["requests"] > 1:
            print(f"  {S.MUTED}busiest turn: {busiest['requests']} requests, "
                  f"{busiest['prompt'] + busiest['completion']:,} tokens{S.R}")

    est_tokens = _estimate_tokens(messages)
    budget = _get_ctx_budget()
    usage_pct = min(100, int(est_tokens / max(1, budget) * 100))
    usage_color = S.OK if usage_pct < 60 else (S.WARN if usage_pct < 85 else S.ERR)
    pairs = _get_conv_pairs(messages)
    overhead = _fixed_overhead(messages)

    print(f"\n  {S.BOLD}Session Context Usage{S.R}")
    # NOT "turns". `_get_conv_pairs` starts a new block at every user message
    # that is not a tool result, and the harness writes several of those
    # itself: each of deepthink's six stage instructions, the nudge after an
    # empty reply, the one after an unparseable call, a channel note, a `!`
    # command. So one question can be seven blocks, and calling both numbers
    # "turns" on one screen said the conversation was seven times what it was.
    print(f"  {S.GRAY}blocks{S.R} {S.WHITE}{len(pairs)}{S.R} "
          f"{S.MUTED}(what compression keeps or drops){S.R}")
    print(f"  {S.GRAY}ctx{S.R}    {S.WHITE}{len(messages)} messages{S.R}")
    print(f"  {S.GRAY}tokens{S.R} {usage_color}~{est_tokens:,}{S.R} {S.MUTED}/ {budget:,} ({usage_pct}%){S.R}")
    if overhead:
        # Of the whole request, not of `messages`: over a native interface the
        # tool schemas are counted in `overhead` but travel beside the message
        # list, so dividing by the message list alone can exceed 100%.
        beside = overhead - (_estimate_tokens(messages[:1]) if messages else 0)
        share = min(100, int(overhead / max(1, est_tokens + beside) * 100))
        print(f"  {S.GRAY}fixed{S.R}  {S.WHITE}~{overhead:,}{S.R} "
              f"{S.MUTED}of that is the system prompt and tool list - "
              f"{share}% of each request, and re-sent with every one{S.R}")
    print()
