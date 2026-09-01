import os
import sys
import subprocess
import textwrap
import config
from config import S, tw, _hr, _visible_len
from session import load_memory, list_sessions
from context import _estimate_tokens, _get_ctx_budget, _get_conv_pairs
from skills import list_skills, skill_dirs
import mcp_client
import providers
import toolspec


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


def _logo() -> str:
    art = _LOGO_WIDE if config.tw() >= 66 else _LOGO_NARROW
    return f"{S.ACCENT}{S.BOLD}\n{art}\n{S.R}"


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
    print()
    print(f"  {S.GRAY}Type {S.ACCENT}/help{S.GRAY} for commands · {S.ACCENT}/exit{S.GRAY} to quit{S.R}")
    if not config.PROMPT_TOOLKIT_AVAILABLE:
        print(f"  {S.WARN}⚠ Tip: pip install prompt_toolkit for history & autocompletion.{S.R}")
    print(f"  {_hr()}")
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
        ("/deepthink", "Show the plan-check-build-review-verify chain and whether it is on"),
        ("/deepthink <on/off>", "Turn that chain on or off"),
        ("/undo", "Take back the last file change the AI committed"),
        ("/autocommit", "Whether each AI edit gets its own git commit, and the recent ones"),
        ("/autocommit <on/off>", "Turn that on or off"),
    ]
    print()
    print(f"  {S.BOLD}{S.ACCENT}Commands{S.R}")
    print(f"  {_hr(width=44)}")
    for cmd, desc in commands:
        print(f"  {S.ACCENT}{cmd:22}{S.R} {S.GRAY}{desc}{S.R}")
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
    import permissions

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
    if "[Error]" in result or "[System] User denied" in result:
        print(f"  {S.ERR}╰─ error{S.R}")
    else:
        print(f"  {S.OK}╰─ done{S.R}")


def _approval_prompt(action_label: str, details: list[tuple[str, str]], rule: str = "") -> bool:
    from renderer import _disp_width
    import permissions

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


def _fmt_tokens(prompt_t: int, comp_t: int, total_dur: float, eval_dur: float):
    total = prompt_t + comp_t
    tps = (comp_t / eval_dur) if eval_dur > 0 else 0.0
    print(
        f"\n  {S.MUTED}─ tokens: "
        f"{S.GRAY}{prompt_t}{S.MUTED} in · "
        f"{S.GRAY}{comp_t}{S.MUTED} out · "
        f"{S.GRAY}{total}{S.MUTED} total · "
        f"{S.GRAY}{total_dur:.1f}s{S.MUTED} · "
        f"{S.GRAY}TPS: {tps:.1f}{S.R}\n"
    )


def display_usage_graph(messages: list[dict]):
    if not config.token_history:
        print(f"  {S.GRAY}No token usage data yet.{S.R}")
    else:
        totals = [t["prompt"] + t["completion"] for t in config.token_history]
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

        total_prompt = sum(t["prompt"] for t in config.token_history)
        total_comp = sum(t["completion"] for t in config.token_history)
        total_all = total_prompt + total_comp
        
        print(f"\n  {S.BOLD}Cumulative Token Usage{S.R}")
        print(f"  {S.GRAY}prompt{S.R} {S.WHITE}{total_prompt:,}{S.R}  "
              f"{S.GRAY}completion{S.R} {S.WHITE}{total_comp:,}{S.R}  "
              f"{S.GRAY}total{S.R} {S.BOLD}{S.WHITE}{total_all:,}{S.R}")

    est_tokens = _estimate_tokens(messages)
    budget = _get_ctx_budget()
    usage_pct = min(100, int(est_tokens / max(1, budget) * 100))
    usage_color = S.OK if usage_pct < 60 else (S.WARN if usage_pct < 85 else S.ERR)
    pairs = _get_conv_pairs(messages)
    
    print(f"\n  {S.BOLD}Session Context Usage{S.R}")
    print(f"  {S.GRAY}turns{S.R}  {S.WHITE}{len(pairs)}{S.R}")
    print(f"  {S.GRAY}ctx{S.R}    {S.WHITE}{len(messages)} messages{S.R}")
    print(f"  {S.GRAY}tokens{S.R} {usage_color}~{est_tokens:,}{S.R} {S.MUTED}/ {budget:,} ({usage_pct}%){S.R}\n")
