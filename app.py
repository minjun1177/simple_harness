import asyncio
import sys
import itertools
import ollama
import platform
import re
import json
import shlex
import subprocess
import os
import datetime
import shutil
import unicodedata
import time
from duckduckgo_search import DDGS
import requests
from bs4 import BeautifulSoup
import random
import psutil
import hashlib

try:
    from tree_sitter import Language, Parser, Query, QueryCursor
    import tree_sitter_python as _ts_python
    import tree_sitter_javascript as _ts_javascript
    import tree_sitter_typescript as _ts_typescript
    import tree_sitter_java as _ts_java
    import tree_sitter_c as _ts_c
    import tree_sitter_cpp as _ts_cpp
    import tree_sitter_go as _ts_go
    import tree_sitter_rust as _ts_rust
    import tree_sitter_c_sharp as _ts_csharp
    TREE_SITTER_AVAILABLE = True

    _TS_LANGUAGES = {
        "python":     Language(_ts_python.language()),
        "javascript": Language(_ts_javascript.language()),
        "typescript":  Language(_ts_typescript.language_typescript()),
        "tsx":         Language(_ts_typescript.language_tsx()),
        "java":       Language(_ts_java.language()),
        "c":          Language(_ts_c.language()),
        "cpp":        Language(_ts_cpp.language()),
        "go":         Language(_ts_go.language()),
        "rust":       Language(_ts_rust.language()),
        "csharp":     Language(_ts_csharp.language()),
    }

    _EXT_TO_LANG = {
        ".py": "python", ".pyw": "python",
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript", ".tsx": "tsx",
        ".java": "java",
        ".c": "c", ".h": "c",
        ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hxx": "cpp",
        ".go": "go",
        ".rs": "rust",
        ".cs": "csharp",
    }
except ImportError:
    TREE_SITTER_AVAILABLE = False
    _TS_LANGUAGES = {}
    _EXT_TO_LANG = {}

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI
    PROMPT_TOOLKIT_AVAILABLE = True

    class SlashCommandCompleter(Completer):
        def __init__(self, commands):
            self.commands = commands

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith('/') and ' ' not in text:
                for cmd in self.commands:
                    if cmd.startswith(text):
                        yield Completion(cmd, start_position=-len(text))
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False

from systemprompt import systemprompt as syp
from systemprompt import summarizeprompt as smrp


MODEL = "gemma4:e4b"

CURRENT_OS = platform.system()

ALLOWED_COMMANDS = {
    "cmd": ["cmd"],
    "dir": ["dir"],
    "ls": ["ls", "-l"],
    "pwd": ["pwd"],
    "whoami": ["whoami"],
    "echo": ["echo"],
}

SYSTEM_PROMPT = syp()

FILE_MAX_DISPLAY_LENGTH = 1000

MEMORY_FILE = "memory.json"
SESSION_DIR = "sessions"

SEARCH_MAX_RESULTS = 5

NUM_CTX = 32768*2
NUM_PREDICT = 2048

AUTO_ALLOW = False
RETURN_ALL_FILE_CONTENT = True
SAVE_CHAT_HISTORY = True
CUSTOM_PERSONA = ""


class S:
    """ANSI 스타일 상수."""
    R     = "\033[0m"
    BOLD  = "\033[1m"
    DIM   = "\033[2m"
    ITAL  = "\033[3m"

    ACCENT    = "\033[38;2;217;119;87m"
    USER_CLR  = "\033[38;2;184;187;38m"
    OK        = "\033[38;2;142;192;124m"
    WARN      = "\033[38;2;250;189;47m"
    ERR       = "\033[38;2;251;73;52m"
    INFO      = "\033[38;2;131;165;152m"
    GRAY      = "\033[38;2;146;131;116m"
    WHITE     = "\033[38;2;235;219;178m"
    MUTED     = "\033[38;2;102;92;84m"
    PURPLE    = "\033[38;2;211;134;155m"
    CYAN      = "\033[38;2;131;165;152m"


def tw() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _visible_len(text: str) -> int:
    return len(re.sub(r'\033\[[^m]*m', '', text))


def _hr(char: str = "─", width: int = 0, style: str = S.MUTED) -> str:
    w = width or max(1, tw() - 6)
    return f"{style}{char * w}{S.R}"



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

def _welcome():
    memory_count = len(load_memory())
    session_count = len(list_sessions())
    
    workspace = os.getcwd()
    git_info = _get_git_info()
    rules_info = _get_rules_info()
    python_info = _get_python_info()
    tools_count = SYSTEM_PROMPT.count('"name":')

    logo = f"""\
{S.ACCENT}{S.BOLD}
    ██╗      ██████╗  ██████╗ █████╗ ██╗          ██████╗██╗  ██╗ █████╗ ████████╗
    ██║     ██╔═══██╗██╔════╝██╔══██╗██║         ██╔════╝██║  ██║██╔══██╗╚══██╔══╝
    ██║     ██║   ██║██║     ███████║██║         ██║     ███████║███████║   ██║   
    ██║     ██║   ██║██║     ██╔══██║██║         ██║     ██╔══██║██╔══██║   ██║   
    ███████╗╚██████╔╝╚██████╗██║  ██║███████╗    ╚██████╗██║  ██║██║  ██║   ██║   
    ╚══════╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝╚══════╝     ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   
{S.R}"""
    print(logo)
    print(f"  {S.GRAY}model{S.R}      {S.WHITE}{MODEL}{S.R}")
    print(f"  {S.GRAY}os{S.R}         {S.WHITE}{CURRENT_OS}{S.R}")
    print(f"  {S.GRAY}python{S.R}     {S.WHITE}{python_info}{S.R}")
    print(f"  {S.GRAY}workspace{S.R}  {S.WHITE}{workspace}{S.R}")
    print(f"  {S.GRAY}git{S.R}        {S.WHITE}{git_info}{S.R}")
    print(f"  {S.GRAY}rules{S.R}      {S.WHITE}{rules_info}{S.R}")
    print(f"  {S.GRAY}tools{S.R}      {S.WHITE}{tools_count} active{S.R}")
    print(f"  {S.GRAY}memory{S.R}     {S.WHITE}{memory_count} item{'s' if memory_count != 1 else ''}{S.R}")
    print(f"  {S.GRAY}sessions{S.R}   {S.WHITE}{session_count} saved{S.R}")
    print()
    print(f"  {S.GRAY}Type {S.ACCENT}/help{S.GRAY} for commands · {S.ACCENT}/exit{S.GRAY} to quit{S.R}")
    if not PROMPT_TOOLKIT_AVAILABLE:
        print(f"  {S.WARN}⚠ Tip: pip install prompt_toolkit for history & autocompletion.{S.R}")
    print(f"  {_hr()}")
    print()


def _show_help():
    commands = [
        ("/help",  "Show this help message"),
        ("/usage", "Show token usage history graph"),
        ("/clear", "Clear conversation history"),
        ("/model", "Show model info / select model"),
        ("/models", "List available Ollama models"),
        ("/sessions", "List saved sessions"),
        ("/load <id>", "Load a specific session by ID"),
        ("/exit",  "Exit the chat"),
        ("/automode <on/off>", "Toggle allow modal"),
        ("/fullcontent <on/off>", "Toggle returning all file content"),
        ("/record <on/off>", "Toggle saving chat history to sessions"),
        ("/export [filename]", "Export conversation to a markdown file"),
        ("/system <prompt>", "Change the system prompt"),
    ]
    print()
    print(f"  {S.BOLD}{S.ACCENT}Commands{S.R}")
    print(f"  {_hr(width=44)}")
    for cmd, desc in commands:
        print(f"  {S.ACCENT}{cmd:12}{S.R} {S.GRAY}{desc}{S.R}")
    print()



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



def _approval_prompt(action_label: str, details: list[tuple[str, str]]) -> bool:
    print()
    print(f"  {S.WARN}⚠  {S.BOLD}Approval Required{S.R}")

    w = max(40, tw() - 8)
    print(f"  {S.WARN}╭{'─' * w}╮{S.R}")
    for label, value in details:
        max_val = w - _visible_len(label) - 5
        val_display = value if len(value) <= max_val else value[:max_val - 3] + "..."
        val_lines = val_display.split("\n")
        first = True
        for vl in val_lines:
            if first:
                print(f"  {S.WARN}│{S.R}  {S.GRAY}{label}:{S.R} {vl}")
                first = False
            else:
                pad = " " * (_visible_len(label) + 4)
                print(f"  {S.WARN}│{S.R}  {pad}{vl}")
    print(f"  {S.WARN}╰{'─' * w}╯{S.R}")

    try:
        if AUTO_ALLOW is True: return True
        answer = input(f"  {S.WARN}Allow? {S.MUTED}[{S.OK}y{S.MUTED}/{S.ERR}n{S.MUTED}]{S.R} {S.WARN}›{S.R} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == "y"


_LATEX_SYMBOLS: list[tuple[str, str]] = [
    (r'\Gamma', 'Γ'), (r'\Delta', 'Δ'), (r'\Theta', 'Θ'), (r'\Lambda', 'Λ'),
    (r'\Xi', 'Ξ'), (r'\Pi', 'Π'), (r'\Sigma', 'Σ'), (r'\Upsilon', 'Υ'),
    (r'\Phi', 'Φ'), (r'\Psi', 'Ψ'), (r'\Omega', 'Ω'),
    (r'\alpha', 'α'), (r'\beta', 'β'), (r'\gamma', 'γ'), (r'\delta', 'δ'),
    (r'\epsilon', 'ε'), (r'\varepsilon', 'ε'), (r'\zeta', 'ζ'), (r'\eta', 'η'),
    (r'\theta', 'θ'), (r'\vartheta', 'ϑ'), (r'\iota', 'ι'), (r'\kappa', 'κ'),
    (r'\lambda', 'λ'), (r'\mu', 'μ'), (r'\nu', 'ν'), (r'\xi', 'ξ'),
    (r'\pi', 'π'), (r'\varpi', 'ϖ'), (r'\rho', 'ρ'), (r'\varrho', 'ϱ'),
    (r'\sigma', 'σ'), (r'\varsigma', 'ς'), (r'\tau', 'τ'), (r'\upsilon', 'υ'),
    (r'\phi', 'φ'), (r'\varphi', 'φ'), (r'\chi', 'χ'), (r'\psi', 'ψ'),
    (r'\omega', 'ω'),
    (r'\cdot', '·'), (r'\times', '×'), (r'\div', '÷'), (r'\pm', '±'),
    (r'\mp', '∓'), (r'\leq', '≤'), (r'\geq', '≥'), (r'\neq', '≠'),
    (r'\approx', '≈'), (r'\equiv', '≡'), (r'\sim', '∼'), (r'\propto', '∝'),
    (r'\infty', '∞'), (r'\partial', '∂'), (r'\nabla', '∇'), (r'\forall', '∀'),
    (r'\exists', '∃'), (r'\in', '∈'), (r'\notin', '∉'), (r'\subset', '⊂'),
    (r'\supset', '⊃'), (r'\subseteq', '⊆'), (r'\supseteq', '⊇'),
    (r'\cup', '∪'), (r'\cap', '∩'), (r'\emptyset', '∅'), (r'\varnothing', '∅'),
    (r'\sum', 'Σ'), (r'\prod', 'Π'), (r'\int', '∫'), (r'\oint', '∮'),
    (r'\sqrt', '√'), (r'\ldots', '…'), (r'\cdots', '⋯'), (r'\vdots', '⋮'),
    (r'\ddots', '⋱'), (r'\to', '→'), (r'\rightarrow', '→'), (r'\leftarrow', '←'),
    (r'\Rightarrow', '⇒'), (r'\Leftarrow', '⇐'), (r'\Leftrightarrow', '⇔'),
    (r'\leftrightarrow', '↔'), (r'\uparrow', '↑'), (r'\downarrow', '↓'),
    (r'\langle', '⟨'), (r'\rangle', '⟩'),
    (r'\quad', '  '), (r'\qquad', '    '), (r'\,', ' '), (r'\;', ' '),
    (r'\!', ''), (r'\ ', ' '),
    (r'\log', 'log'), (r'\ln', 'ln'), (r'\exp', 'exp'), (r'\sin', 'sin'),
    (r'\cos', 'cos'), (r'\tan', 'tan'), (r'\arcsin', 'arcsin'),
    (r'\arccos', 'arccos'), (r'\arctan', 'arctan'), (r'\lim', 'lim'),
    (r'\max', 'max'), (r'\min', 'min'), (r'\sup', 'sup'), (r'\inf', 'inf'),
    (r'\det', 'det'), (r'\dim', 'dim'), (r'\ker', 'ker'),
    (r'\mathbb{R}', 'ℝ'), (r'\mathbb{N}', 'ℕ'), (r'\mathbb{Z}', 'ℤ'),
    (r'\mathbb{Q}', 'ℚ'), (r'\mathbb{C}', 'ℂ'),
    (r'\left(', '('), (r'\right)', ')'), (r'\left[', '['), (r'\right]', ']'),
    (r'\left\{', '{'), (r'\right\}', '}'), (r'\left|', '|'), (r'\right|', '|'),
    (r'\text{', ''), (r'\mathrm{', ''), (r'\mathbf{', ''), (r'\mathit{', ''),
    (r'\boldsymbol{', ''), (r'\hat{', ''), (r'\bar{', ''), (r'\tilde{', ''),
    (r'\vec{', ''), (r'\dot{', ''), (r'\ddot{', ''), (r'\textbf{', ''), (r'\textit{', ''),
]


_SUP_MAP = str.maketrans('0123456789+-=()', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾')
_SUB_MAP = str.maketrans('0123456789+-=()', '₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎')


def _render_latex(expr: str) -> str:
    s = expr.strip()

    s = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'(\1/\2)', s)
    s = re.sub(r'\\sqrt\{([^}]*)\}', r'√(\1)', s)
    s = re.sub(r'\^\{([^}]*)\}', lambda m: m.group(1).translate(_SUP_MAP), s)
    s = re.sub(r'_\{([^}]*)\}', lambda m: m.group(1).translate(_SUB_MAP), s)
    s = re.sub(r'\^([0-9a-zA-Z])', lambda m: m.group(1).translate(_SUP_MAP), s)
    s = re.sub(r'_([0-9a-zA-Z])', lambda m: m.group(1).translate(_SUB_MAP), s)

    for latex_sym, unicode_sym in sorted(_LATEX_SYMBOLS, key=lambda x: -len(x[0])):
        s = s.replace(latex_sym, unicode_sym)

    s = re.sub(r'\\[a-zA-Z]+', '', s)
    s = re.sub(r'[{}]', '', s)

    return s.strip()


def _apply_inline_md(line: str) -> str:
    def replace_inline_latex(m: re.Match) -> str:
        rendered = _render_latex(m.group(1))
        return f"{S.PURPLE}{rendered}{S.R}"

    line = re.sub(r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)', replace_inline_latex, line)

    line = re.sub(r'\*\*\*(.+?)\*\*\*', f'{S.BOLD}{S.ITAL}\\1{S.R}', line)
    line = re.sub(r'\*\*(.+?)\*\*', f'{S.BOLD}\\1{S.R}', line)
    line = re.sub(r'(?<![^\W_])__(.+?)__(?![^\W_])', f'{S.BOLD}\\1{S.R}', line)
    line = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', f'{S.ITAL}\\1{S.R}', line)
    line = re.sub(r'(?<![^\W_])_([^_]+?)_(?![^\W_])', f'{S.ITAL}\\1{S.R}', line)
    line = re.sub(r'~~(.+?)~~', f'{S.DIM}{S.MUTED}\\1{S.R}', line)
    line = re.sub(r'`([^`]+)`', f'{S.ACCENT}\\1{S.R}', line)

    return line


def _wrap_box_line(text: str, max_w: int) -> list[str]:
    lines = []
    curr_line = ""
    curr_w = 0
    for char in text:
        cw = 2 if unicodedata.east_asian_width(char) in 'WF' else 1
        if curr_w + cw > max_w:
            lines.append(curr_line)
            curr_line = char
            curr_w = cw
        else:
            curr_line += char
            curr_w += cw
    if curr_line or not lines:
        lines.append(curr_line)
    return lines

def _format_box_lines(text: str, border_color: str, text_color: str = "") -> str:
    max_w = max(1, tw() - 6)
    wrapped = _wrap_box_line(text, max_w)
    out = []
    for wl in wrapped:
        w = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in wl)
        pad = " " * (max_w - w)
        out.append(f"  {border_color}│{S.R} {text_color}{wl}{S.R}{pad} {border_color}│{S.R}")
    return "\n".join(out)

def _render_line(line: str, in_code: bool, in_latex_block: bool = False) -> tuple[str, bool, bool]:
    stripped = line.strip()

    if stripped.startswith("```"):
        in_code = not in_code
        if in_code:
            lang = stripped[3:].strip() or "code"
            w = max(1, tw() - len(lang) - 7)
            return f"  {S.MUTED}╭─ {lang} {'─' * w}╮{S.R}", in_code, in_latex_block
        else:
            w = max(1, tw() - 4)
            return f"  {S.MUTED}╰{'─' * w}╯{S.R}", in_code, in_latex_block

    if in_code:
        return _format_box_lines(line, S.MUTED), in_code, in_latex_block

    inline_display = re.match(r'^(\$\$|\\\[)(.+?)(\$\$|\\\])$', stripped)
    if inline_display and not in_latex_block:
        rendered_math = _render_latex(inline_display.group(2))
        w = max(1, tw() - 11)
        middle = _format_box_lines(rendered_math, S.PURPLE, S.PURPLE)
        return (
            f"  {S.PURPLE}╭─ math {'─' * w}╮{S.R}\n"
            f"{middle}\n"
            f"  {S.PURPLE}╰{'─' * (w + 7)}╯{S.R}"
        ), in_code, in_latex_block

    is_latex_open = re.match(r'^(\$\$|\\\[|\\begin\{[a-zA-Z*]+\})', stripped)
    is_latex_close = re.match(r'^(\$\$|\\\]|\\end\{[a-zA-Z*]+\})', stripped)

    if not in_latex_block and is_latex_open:
        in_latex_block = True
        w = max(1, tw() - 11)
        return f"  {S.PURPLE}╭─ math {'─' * w}╮{S.R}", in_code, in_latex_block

    if in_latex_block and is_latex_close:
        in_latex_block = False
        w = max(1, tw() - 4)
        return f"  {S.PURPLE}╰{'─' * w}╯{S.R}", in_code, in_latex_block


    if in_latex_block:
        if stripped.startswith(r'\item'):
            stripped = "* " + stripped[5:].strip()
        rendered_math = _render_latex(stripped)
        return _format_box_lines(rendered_math, S.PURPLE, S.PURPLE), in_code, in_latex_block

    if stripped.startswith("###### "):
        return f"  {S.DIM}{S.GRAY}{stripped[7:]}{S.R}", in_code, in_latex_block
    if stripped.startswith("##### "):
        return f"  {S.GRAY}{stripped[6:]}{S.R}", in_code, in_latex_block
    if stripped.startswith("#### "):
        return f"  {S.BOLD}{S.MUTED}{stripped[5:]}{S.R}", in_code, in_latex_block
    if stripped.startswith("### "):
        return f"  {S.BOLD}{S.INFO}{stripped[4:]}{S.R}", in_code, in_latex_block
    if stripped.startswith("## "):
        return f"  {S.BOLD}{S.ACCENT}{stripped[3:]}{S.R}", in_code, in_latex_block
    if stripped.startswith("# "):
        return f"  {S.BOLD}{S.WHITE}{stripped[2:]}{S.R}", in_code, in_latex_block

    if re.match(r'^(\-{3,}|\*{3,}|_{3,})$', stripped):
        w = max(1, tw() - 4)
        return f"  {S.MUTED}{'─' * w}{S.R}", in_code, in_latex_block

    if stripped.startswith("> ") or stripped == ">":
        inner = stripped[2:] if stripped.startswith("> ") else ""
        inner = _apply_inline_md(inner)
        return f"  {S.MUTED}┃{S.R} {S.ITAL}{S.GRAY}{inner}{S.R}", in_code, in_latex_block

    line = _apply_inline_md(line)

    line = re.sub(r'^(\s*)[-*]\s', f'\\1{S.ACCENT}•{S.R} ', line)
    line = re.sub(r'^(\s*)(\d+\.)\s', f'\\1{S.ACCENT}\\2{S.R} ', line)

    return f"  {line}", in_code, in_latex_block


def _clean_md(text: str) -> str:
    t = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    t = re.sub(r'(?<![^\W_])__(.+?)__(?![^\W_])', r'\1', t)
    t = re.sub(r'`([^`]+)`', r'\1', t)
    return t

def _disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)

def _wrap_plain_text(text: str, max_w: int) -> list[str]:
    words = text.split(' ')
    lines = []
    curr_line = ""
    curr_w = 0
    for word in words:
        ww = _disp_width(word)
        space_w = 1 if curr_w > 0 else 0
        if curr_w + space_w + ww <= max_w:
            if curr_w > 0:
                curr_line += " "
                curr_w += 1
            curr_line += word
            curr_w += ww
        else:
            if curr_line: lines.append(curr_line)
            curr_line = ""
            curr_w = 0
            if ww > max_w:
                for char in word:
                    cw = 2 if unicodedata.east_asian_width(char) in 'WF' else 1
                    if curr_w + cw > max_w:
                        lines.append(curr_line)
                        curr_line = char
                        curr_w = cw
                    else:
                        curr_line += char
                        curr_w += cw
            else:
                curr_line = word
                curr_w = ww
    if curr_line: lines.append(curr_line)
    return lines if lines else [""]

def _format_table(lines: list[str]) -> list[str]:
    if not lines: return []
    parsed = []
    for line in lines:
        cells = line.strip().strip('|').split('|')
        parsed.append([c.strip() for c in cells])
        
    num_cols = max(len(row) for row in parsed)
    for row in parsed:
        while len(row) < num_cols: row.append("")
            
    col_widths = [0] * num_cols
    for row in parsed:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], _disp_width(_clean_md(cell)))
            
    available_w = max(10, tw() - 6 - (num_cols * 3))
    total_w = sum(col_widths)
    
    if total_w > available_w:
        new_widths = [0] * num_cols
        remaining = available_w
        for i in range(num_cols):
            new_widths[i] = min(col_widths[i], max(3, available_w // num_cols))
            remaining -= new_widths[i]
        needs_more = [i for i in range(num_cols) if col_widths[i] > new_widths[i]]
        while remaining > 0 and needs_more:
            for i in list(needs_more):
                if remaining <= 0: break
                new_widths[i] += 1
                remaining -= 1
                if new_widths[i] == col_widths[i]: needs_more.remove(i)
        col_widths = new_widths
            
    out = []
    for r_idx, row in enumerate(parsed):
        is_sep = all(re.match(r'^:?-+:?$', c) for c in row if c)
        if r_idx == 0:
            seps = ["─" * (w + 2) for w in col_widths]
            out.append(f"  {S.MUTED}╭{'┬'.join(seps)}╮{S.R}")
        if is_sep:
            seps = ["─" * (w + 2) for w in col_widths]
            out.append(f"  {S.MUTED}├{'┼'.join(seps)}┤{S.R}")
        else:
            wrapped_cells = []
            for i, c in enumerate(row):
                tgt_w = col_widths[i]
                c_clean = _clean_md(c)
                if _disp_width(c_clean) > tgt_w:
                    wrapped_cells.append(_wrap_plain_text(c_clean, tgt_w))
                else:
                    c_styled = re.sub(r'\*\*(.+?)\*\*', f'{S.BOLD}\\1{S.R}', c)
                    c_styled = re.sub(r'`([^`]+)`', f'{S.ACCENT}\\1{S.R}', c_styled)
                    wrapped_cells.append([c_styled])
                    
            max_lines = max((len(c) for c in wrapped_cells), default=1)
            for line_idx in range(max_lines):
                fmt_cells = []
                for i in range(num_cols):
                    tgt_w = col_widths[i]
                    cell_lines = wrapped_cells[i]
                    if line_idx < len(cell_lines):
                        line_text = cell_lines[line_idx]
                        line_clean = re.sub(r'\033\[[^m]*m', '', line_text)
                        w = _disp_width(line_clean)
                        pad = " " * max(0, tgt_w - w)
                        fmt_cells.append(f" {line_text}{pad} ")
                    else:
                        pad = " " * tgt_w
                        fmt_cells.append(f" {pad} ")
                border = f"{S.MUTED}│{S.R}"
                out.append(f"  {border}{border.join(fmt_cells)}{border}")
            
    if parsed:
        seps = ["─" * (w + 2) for w in col_widths]
        out.append(f"  {S.MUTED}╰{'┴'.join(seps)}╯{S.R}")
    return out

def _render_full(text: str) -> str:
    lines = text.split('\n')
    out = []
    in_c = False
    in_lb = False
    table_buf = []

    def flush_t():
        if table_buf:
            out.extend(_format_table(table_buf))
            table_buf.clear()

    for line in lines:
        stripped = line.strip()
        if not in_c and not in_lb and stripped.startswith('|') and stripped.endswith('|'):
            table_buf.append(line)
        else:
            flush_t()
            rendered, in_c, in_lb = _render_line(line, in_c, in_lb)
            out.append(rendered)
    flush_t()
    return '\n'.join(out)



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



def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def save_memory(memory: dict) -> None:
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

def handle_write_memory(memory_id: str, content: str) -> str:
    if not memory_id:
        return "[Error] Memory ID is required."
    memory = load_memory()
    memory[memory_id] = {
        "content": content,
        "created_at": datetime.datetime.now().isoformat()
    }
    save_memory(memory)
    return f"[Success] Memory saved: '{memory_id}'"

def handle_get_memory_list() -> str:
    memory = load_memory()
    if not memory:
        return "[Memory] No memories stored."
    lines = []
    for i, (mid, data) in enumerate(memory.items(), 1):
        created = data.get("created_at", "unknown")
        preview = data.get("content", "")[:50]
        lines.append(f"{i}. {mid} ({created}) - {preview}")
    return "\n".join(lines)

def handle_read_memory(memory_id: str) -> str:
    if not memory_id:
        return "[Error] Memory ID is required."
    memory = load_memory()
    if memory_id not in memory:
        return f"[Error] Memory '{memory_id}' not found."
    data = memory[memory_id]
    return f"[Memory: {memory_id}]\nContent: {data.get('content', '')}\nCreated: {data.get('created_at', 'unknown')}"

def handle_delete_memory(memory_id: str) -> str:
    if not memory_id:
        return "[Error] Memory ID is required."
    memory = load_memory()
    if memory_id not in memory:
        return f"[Error] Memory '{memory_id}' not found."
    del memory[memory_id]
    save_memory(memory)
    return f"[Success] Memory deleted: '{memory_id}'"

def handle_edit_memory(memory_id: str, new_content: str) -> str:
    if not memory_id:
        return "[Error] Memory ID is required."
    memory = load_memory()
    if memory_id not in memory:
        return f"[Error] Memory '{memory_id}' not found."
    memory[memory_id]["content"] = new_content
    save_memory(memory)
    return f"[Success] Memory edited: '{memory_id}'"


def save_session(messages: list[dict], session_id: str) -> str:
    if not SAVE_CHAT_HISTORY:
        return session_id
    if not os.path.exists(SESSION_DIR):
        os.makedirs(SESSION_DIR)
    if not session_id:
        session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(SESSION_DIR, f"{session_id}.json")
    
    data = {
        "version": 2,
        "model": MODEL,
        "persona": CUSTOM_PERSONA,
        "token_history": token_history,
        "messages": messages,
        "updated_at": datetime.datetime.now().isoformat()
    }
    
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  {S.ERR}✗ Failed to save session: {e}{S.R}")
    return session_id

def load_session(session_id: str) -> dict | list:
    filepath = os.path.join(SESSION_DIR, f"{session_id}.json")
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def list_sessions() -> list[tuple[str, str]]:
    if not os.path.exists(SESSION_DIR):
        return []
    sessions = []
    for f in os.listdir(SESSION_DIR):
        if f.endswith(".json"):
            sid = f.replace(".json", "")
            filepath = os.path.join(SESSION_DIR, f)
            meta_str = ""
            try:
                with open(filepath, "r", encoding="utf-8") as file:
                    data = json.load(file)
                    if isinstance(data, dict) and data.get("version") == 2:
                        m = data.get("model", "unknown")
                        msgs = len(data.get("messages", []))
                        tokens = sum(t.get("prompt", 0) + t.get("completion", 0) for t in data.get("token_history", []))
                        meta_str = f"[{m}] {msgs} msgs, {tokens} tokens"
                    elif isinstance(data, list):
                        meta_str = f"[legacy] {len(data)} msgs"
            except Exception:
                meta_str = "[error loading info]"
            sessions.append((sid, meta_str))
    sessions.sort(key=lambda x: x[0], reverse=True)
    return sessions


def handle_search_web(query: str) -> str:
    if not query: return "[Error] Empty search query."
    try:
        results = DDGS().text(query, region="kr-kr", safesearch="moderate", max_results=SEARCH_MAX_RESULTS)
        if not results: return "[Search] No results found."
        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "(no title)")
            href  = r.get("href",  "(no url)")
            body  = r.get("body",  "(no summary)")
            lines.append(f"{i}. {title}\n   URL: {href}\n   {body}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"[Error] Search failed: {e}"

def safe_run_cmd(command_string: str) -> str:
    try:
        args = shlex.split(command_string)
    except ValueError:
        return "[Error] Invalid command format."
    if not args: return "[Error] Empty command."

    base_cmd = args[0]
    if base_cmd in ALLOWED_COMMANDS:
        safe_executable_list = list(ALLOWED_COMMANDS[base_cmd])
        if len(args) > 1:
            safe_executable_list.extend(args[1:])
    else:
        safe_executable_list = args

    cmd_display = ' '.join(safe_executable_list)
    approved = _approval_prompt("Run Command", [("command", cmd_display)])
    if not approved: return "[System] User denied command execution."

    try:
        result = subprocess.run(safe_executable_list, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, shell=True)
        output = result.stdout if result.returncode == 0 else result.stderr
        return output.strip()
    except Exception as e:
        return f"[Error] Failed to execute command: {e}"


_HASHLINE_PATTERN = re.compile(r'^\d+:[0-9a-f]{2}\|')

def _line_hash(line: str) -> str:
    return hashlib.md5(line.encode("utf-8")).hexdigest()[:2]

def _encode_hashlines(content: str) -> str:
    lines = content.split("\n")
    result = []
    for i, line in enumerate(lines, 1):
        h = _line_hash(line)
        result.append(f"{i}:{h}|{line}")
    return "\n".join(result)

def _strip_hashlines(content: str) -> str:
    lines = content.split("\n")
    if not lines:
        return content

    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return content
    matched = sum(1 for l in non_empty if _HASHLINE_PATTERN.match(l))
    ratio = matched / len(non_empty)

    if ratio < 0.8:
        return content

    stripped = []
    for line in lines:
        m = _HASHLINE_PATTERN.match(line)
        if m:
            stripped.append(line[m.end():])
        else:
            stripped.append(line)
    return "\n".join(stripped)



def handle_read_file(filepath: str) -> str:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        if not RETURN_ALL_FILE_CONTENT and len(content) > FILE_MAX_DISPLAY_LENGTH:
            content = content[:FILE_MAX_DISPLAY_LENGTH] + "\n...[Too long]..."
        return _encode_hashlines(content)
    except Exception as e:
        return f"[Error] Cannot read file: {e}"

def handle_write_file(filepath: str, content: str) -> str:
    content = _strip_hashlines(content)
    preview = content if len(content) <= 200 else content[:200] + "...[truncated]"
    approved = _approval_prompt("Write File", [("path", filepath), ("preview", preview)])
    if not approved: return "[System] User denied file write."
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"[Success] File written: {filepath}"
    except Exception as e:
        return f"[Error] Cannot write file: {e}"

def handle_edit_file(filepath: str, old_content: str, new_content: str) -> str:
    old_content = _strip_hashlines(old_content)
    new_content = _strip_hashlines(new_content)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            file_content = f.read()
    except Exception as e:
        return f"[Error] Cannot read file: {e}"

    if old_content not in file_content:
        return "[Error] The specified old_content was not found in the file."
    count = file_content.count(old_content)
    if count > 1:
        return f"[Error] old_content found {count} times. Please provide a more specific snippet."

    old_preview = old_content[:150] + ('...' if len(old_content) > 150 else '')
    new_preview = new_content[:150] + ('...' if len(new_content) > 150 else '')
    approved = _approval_prompt("Edit File", [("path", filepath), ("from", old_preview), ("to", new_preview)])
    if not approved: return "[System] User denied file edit."

    new_file_content = file_content.replace(old_content, new_content, 1)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(new_file_content)
        return f"[Success] File edited: {filepath}"
    except Exception as e:
        return f"[Error] Cannot write file: {e}"

def handle_delete_file(filepath: str) -> str:
    approved = _approval_prompt("Delete File", [("path", filepath)])
    if not approved: return "[System] User denied file deletion."
    try:
        os.remove(filepath)
        return f"[Success] File deleted: {filepath}"
    except Exception as e:
        return f"[Error] Cannot delete file: {e}"

def handle_copy_file(src: str, dst: str) -> str:
    approved = _approval_prompt("Copy File", [("from", src), ("to", dst)])
    if not approved: return "[System] User denied file copy."
    try:
        shutil.copy2(src, dst)
        return f"[Success] File copied to: {dst}"
    except Exception as e:
        return f"[Error] Cannot copy file: {e}"

def handle_create_dir(dirpath: str) -> str:
    approved = _approval_prompt("Create Directory", [("path", dirpath)])
    if not approved: return "[System] User denied directory creation."
    try:
        os.makedirs(dirpath, exist_ok=True)
        return f"[Success] Directory created: {dirpath}"
    except Exception as e:
        return f"[Error] Cannot create directory: {e}"

def handle_get_url(url: str) -> str:
    try:
        response = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "text/html" in content_type:
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "iframe"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            content = "\n".join(lines)
        else:
            content = response.text
        if not RETURN_ALL_FILE_CONTENT and len(content) > FILE_MAX_DISPLAY_LENGTH:
            content = content[:FILE_MAX_DISPLAY_LENGTH] + "\n...[Too long]..."
        return content
    except Exception as e:
        return f"[Error] Cannot fetch URL: {e}"

def handle_get_input(what_do: str, prompts: list) -> str:
    print(f"\n  {S.INFO}?{S.R} {S.BOLD}Input Required{S.R}")
    print(f"  {S.MUTED}│{S.R}  {what_do}\n  {S.MUTED}│{S.R}")
    prompts = prompts if isinstance(prompts, list) else ([prompts] if prompts else [])
    for i, p in enumerate(prompts):
        print(f"  {S.MUTED}│{S.R}  {S.ACCENT}{i+1}.{S.R} {p}")
    custom_idx = len(prompts) + 1
    print(f"  {S.MUTED}│{S.R}  {S.GRAY}{custom_idx}.  Custom Input{S.R}\n  {S.MUTED}│{S.R}")
    while True:
        try:
            user_input_str = input(f"  {S.MUTED}╰─{S.R} {S.INFO}Chosen{S.R} {S.MUTED}(1~{custom_idx}){S.R} {S.INFO}›{S.R} ").strip()
            if not user_input_str: continue
            user_input = int(user_input_str)
            if 1 <= user_input <= len(prompts): return str(prompts[user_input - 1])
            elif user_input == custom_idx: return input(f"  {S.INFO}  ›{S.R} ").strip()
            else: print(f"  {S.ERR}    Input 1 to {custom_idx} number.{S.R}")
        except ValueError: print(f"  {S.ERR}    Input correct number.{S.R}")

def handle_list_dir(dirpath: str) -> str:
    if not os.path.exists(dirpath):
        return f"[Error] Directory not found: {dirpath}"
    try:
        items = os.listdir(dirpath)
        return "\n".join(items) if items else "[Empty Directory]"
    except Exception as e:
        return f"[Error] list_dir failed: {e}"

def handle_git_status() -> str:
    try:
        res = subprocess.run(["git", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        return res.stdout or res.stderr
    except Exception as e:
        return f"[Error] git status failed: {e}"

def handle_git_diff() -> str:
    try:
        res = subprocess.run(["git", "diff"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        return res.stdout or res.stderr
    except Exception as e:
        return f"[Error] git diff failed: {e}"

def handle_get_system_info() -> str:
    try:
        cpu_percent = psutil.cpu_percent(interval=0.5)
        cpu_count = psutil.cpu_count(logical=True)

        mem = psutil.virtual_memory()
        mem_total_gb = mem.total / (1024 ** 3)
        mem_used_gb = mem.used / (1024 ** 3)

        disk = psutil.disk_usage(os.getcwd())
        disk_total_gb = disk.total / (1024 ** 3)
        disk_used_gb = disk.used / (1024 ** 3)

        processes = []
        for p in psutil.process_iter(['pid', 'name', 'memory_percent', 'cpu_percent']):
            try:
                processes.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        top_processes = sorted(processes, key=lambda x: x['memory_percent'] or 0, reverse=True)[:5]
        proc_lines = [
            f"  - PID {p['pid']}: {p['name']} (RAM: {p['memory_percent']:.1f}%)"
            for p in top_processes
        ]

        info = (
            f"[System Status]\n"
            f"• CPU Usage: {cpu_percent}% ({cpu_count} cores)\n"
            f"• RAM Usage: {mem.percent}% ({mem_used_gb:.1f}GB / {mem_total_gb:.1f}GB)\n"
            f"• Disk Usage: {disk.percent}% ({disk_used_gb:.1f}GB / {disk_total_gb:.1f}GB)\n\n"
            f"[Top Memory Processes]\n" + "\n".join(proc_lines)
        )
        return info

    except Exception as e:
        return f"[Error] Failed to fetch system info: {e}"


def handle_search_in_file(query: str, is_regex: bool = False) -> str:
    if not query:
        return "[Error] Search query is required."
    try:
        if is_regex:
            try:
                pattern = re.compile(query)
            except re.error as e:
                return f"[Error] Invalid regex pattern: {e}"
        else:
            pattern = re.compile(re.escape(query))

        matches = []
        max_results = 100
        search_root = os.getcwd()

        for root, dirs, files in os.walk(search_root):
            # 숨김 폴더, venv, __pycache__, node_modules, .git 제외
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('venv', '__pycache__', 'node_modules', '.git')]
            for filename in files:
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, search_root)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        for line_num, line in enumerate(f, 1):
                            if pattern.search(line):
                                line_preview = line.rstrip()
                                if len(line_preview) > 200:
                                    line_preview = line_preview[:200] + "..."
                                matches.append(f"{rel_path}:{line_num}: {line_preview}")
                                if len(matches) >= max_results:
                                    break
                except (PermissionError, OSError):
                    continue
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        if not matches:
            return f"[Search] No matches found for: {query}"
        header = f"[Search] Found {len(matches)} match(es) for '{query}':\n"
        if len(matches) >= max_results:
            header = f"[Search] Showing first {max_results} matches for '{query}' (more may exist):\n"
        return header + "\n".join(matches)
    except Exception as e:
        return f"[Error] Search failed: {e}"


def handle_call_api(url: str, method: str, headers: str = "", payload: str = "") -> str:
    if not url:
        return "[Error] URL is required."
    if not method:
        return "[Error] HTTP method is required."

    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return f"[Error] Unsupported HTTP method: {method}"

    approved = _approval_prompt("API Call", [("URL", url), ("Method", method)])
    if not approved:
        return "[System] User denied API call."

    try:
        req_headers = {"User-Agent": "LocalChat/1.0"}
        if headers:
            if isinstance(headers, str):
                try:
                    parsed = json.loads(headers)
                    if isinstance(parsed, dict):
                        req_headers.update(parsed)
                except json.JSONDecodeError:
                    return "[Error] Invalid headers JSON format. Expected a JSON object like {\"Key\": \"Value\"}."
            elif isinstance(headers, dict):
                req_headers.update(headers)

        req_body = None
        if payload:
            if isinstance(payload, str):
                try:
                    req_body = json.loads(payload)
                except json.JSONDecodeError:
                    req_body = payload
            elif isinstance(payload, dict):
                req_body = payload

        if method == "GET":
            resp = requests.get(url, headers=req_headers, timeout=30)
        elif method == "POST":
            resp = requests.post(url, headers=req_headers, json=req_body if isinstance(req_body, dict) else None, data=req_body if isinstance(req_body, str) else None, timeout=30)
        elif method == "PUT":
            resp = requests.put(url, headers=req_headers, json=req_body if isinstance(req_body, dict) else None, data=req_body if isinstance(req_body, str) else None, timeout=30)
        elif method == "PATCH":
            resp = requests.patch(url, headers=req_headers, json=req_body if isinstance(req_body, dict) else None, data=req_body if isinstance(req_body, str) else None, timeout=30)
        elif method == "DELETE":
            resp = requests.delete(url, headers=req_headers, timeout=30)

        content = resp.text
        if not RETURN_ALL_FILE_CONTENT and len(content) > FILE_MAX_DISPLAY_LENGTH:
            content = content[:FILE_MAX_DISPLAY_LENGTH] + "\n...[Too long]..."

        result = (
            f"[API Response]\n"
            f"• Status: {resp.status_code} {resp.reason}\n"
            f"• Content-Type: {resp.headers.get('Content-Type', 'unknown')}\n\n"
            f"{content}"
        )
        return result
    except requests.exceptions.Timeout:
        return "[Error] API request timed out (30s)."
    except requests.exceptions.ConnectionError:
        return f"[Error] Could not connect to: {url}"
    except Exception as e:
        return f"[Error] API call failed: {e}"



def _get_ts_parser(lang_name: str):
    lang = _TS_LANGUAGES.get(lang_name)
    if lang is None:
        return None, None
    parser = Parser(lang)
    return parser, lang


def _detect_language(filepath: str) -> str | None:
    _, ext = os.path.splitext(filepath)
    return _EXT_TO_LANG.get(ext.lower())


def _extract_params_python(node, src: bytes) -> list[dict]:
    params = []
    param_node = None
    for child in node.children:
        if child.type == "parameters":
            param_node = child
            break
    if param_node is None:
        return params
    for child in param_node.children:
        if child.type in ("identifier",):
            params.append({"name": child.text.decode(), "type": None, "default": None})
        elif child.type == "typed_parameter":
            name = default = ptype = None
            for sub in child.children:
                if sub.type == "identifier" and name is None:
                    name = sub.text.decode()
                elif sub.type == "type":
                    ptype = sub.text.decode()
            params.append({"name": name, "type": ptype, "default": None})
        elif child.type == "default_parameter":
            name = default = None
            for sub in child.children:
                if sub.type == "identifier" and name is None:
                    name = sub.text.decode()
                elif sub.type not in ("=",):
                    default = sub.text.decode()
            params.append({"name": name, "type": None, "default": default})
        elif child.type == "typed_default_parameter":
            name = default = ptype = None
            for sub in child.children:
                if sub.type == "identifier" and name is None:
                    name = sub.text.decode()
                elif sub.type == "type":
                    ptype = sub.text.decode()
                elif sub.type not in ("=", ":"):
                    default = sub.text.decode()
            params.append({"name": name, "type": ptype, "default": default})
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            prefix = "*" if child.type == "list_splat_pattern" else "**"
            for sub in child.children:
                if sub.type == "identifier":
                    params.append({"name": prefix + sub.text.decode(), "type": None, "default": None})
    return params


def _extract_params_generic(node, src: bytes) -> list[dict]:
    params = []
    for child in node.children:
        if child.type in ("formal_parameters", "parameter_list", "parameters",
                          "formal_parameter_list"):
            for p in child.named_children:
                params.append({"name": p.text.decode(), "type": None, "default": None})
            break
    return params


def _extract_return_type(node, src: bytes) -> str | None:
    for child in node.children:
        if child.type == "type":
            return child.text.decode()
        if child.type == "return_type":
            return child.text.decode()
    return None


def _extract_decorators(node, src: bytes) -> list[str]:
    decorators = []
    sibling = node.prev_named_sibling
    while sibling and sibling.type == "decorator":
        decorators.insert(0, sibling.text.decode())
        sibling = sibling.prev_named_sibling
    for child in node.children:
        if child.type == "decorator":
            decorators.append(child.text.decode())
    return decorators


_FUNC_TYPES = {
    "function_definition", "function_declaration",
    "method_definition", "method_declaration",
    "arrow_function", "generator_function_declaration",
    "function_item",
}
_CLASS_TYPES = {
    "class_definition", "class_declaration",
    "struct_item", "enum_item", "impl_item",
    "interface_declaration", "enum_declaration",
    "type_declaration",
}


def _walk_skeleton(node, src: bytes, lang: str) -> list[dict]:
    results = []
    for child in node.children:
        entry = None
        if child.type in _FUNC_TYPES:
            name_node = child.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "<anonymous>"
            if lang == "python":
                params = _extract_params_python(child, src)
            else:
                params = _extract_params_generic(child, src)
            ret = _extract_return_type(child, src)
            decorators = _extract_decorators(child, src) if lang == "python" else []
            entry = {
                "type": "function",
                "name": name,
                "line": child.start_point[0] + 1,
                "end_line": child.end_point[0] + 1,
                "parameters": params,
                "return_type": ret,
            }
            if decorators:
                entry["decorators"] = decorators
            body = child.child_by_field_name("body")
            if body:
                inner = _walk_skeleton(body, src, lang)
                if inner:
                    entry["children"] = inner

        elif child.type in _CLASS_TYPES:
            name_node = child.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "<anonymous>"
            bases = []
            for sub in child.children:
                if sub.type in ("argument_list", "superclass", "type_list",
                                "class_heritage"):
                    bases.append(sub.text.decode())
            entry = {
                "type": "class",
                "name": name,
                "line": child.start_point[0] + 1,
                "end_line": child.end_point[0] + 1,
                "bases": bases,
            }
            body = child.child_by_field_name("body")
            if body:
                inner = _walk_skeleton(body, src, lang)
                if inner:
                    entry["methods"] = inner
            else:
                inner = _walk_skeleton(child, src, lang)
                if inner:
                    entry["methods"] = inner

        if entry:
            results.append(entry)
        else:
            deeper = _walk_skeleton(child, src, lang)
            results.extend(deeper)
    return results


def handle_get_code_skeleton(file_path: str) -> str:
    if not TREE_SITTER_AVAILABLE:
        return "[Error] tree-sitter is not installed. Run: pip install tree-sitter tree-sitter-python (and other language grammars)"

    if not file_path:
        return "[Error] file_path is required."

    if not os.path.isfile(file_path):
        return f"[Error] File not found: {file_path}"

    lang_name = _detect_language(file_path)
    if lang_name is None:
        return (f"[Error] Unsupported file extension. "
                f"Supported: {', '.join(sorted(set(_EXT_TO_LANG.values())))}")

    parser, lang = _get_ts_parser(lang_name)
    if parser is None:
        return f"[Error] Language grammar not available: {lang_name}"

    try:
        with open(file_path, "rb") as f:
            src = f.read()
    except Exception as e:
        return f"[Error] Failed to read file: {e}"

    tree = parser.parse(src)
    skeleton = _walk_skeleton(tree.root_node, src, lang_name)

    result = {
        "file": file_path,
        "language": lang_name,
        "skeleton": skeleton,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


def handle_query_ast_node(file_path: str, pattern: str, language: str = "") -> str:
    if not TREE_SITTER_AVAILABLE:
        return "[Error] tree-sitter is not installed."

    if not file_path:
        return "[Error] file_path is required."
    if not pattern:
        return "[Error] pattern is required."

    if not os.path.isfile(file_path):
        return f"[Error] File not found: {file_path}"

    lang_name = language.strip().lower() if language else _detect_language(file_path)
    if not lang_name or lang_name not in _TS_LANGUAGES:
        return (f"[Error] Could not determine language. "
                f"Supported: {', '.join(sorted(_TS_LANGUAGES.keys()))}")

    parser, lang = _get_ts_parser(lang_name)
    if parser is None:
        return f"[Error] Language grammar not available: {lang_name}"

    try:
        with open(file_path, "rb") as f:
            src = f.read()
    except Exception as e:
        return f"[Error] Failed to read file: {e}"

    tree = parser.parse(src)

    try:
        query = Query(lang, pattern)
    except Exception as e:
        return f"[Error] Invalid Tree-sitter query pattern: {e}"

    cursor = QueryCursor(query)
    matches_raw = list(cursor.matches(tree.root_node))

    if not matches_raw:
        return json.dumps({"file": file_path, "pattern": pattern, "matches": []},
                          indent=2, ensure_ascii=False)

    lines = src.split(b"\n")
    results = []
    max_results = 50

    for pattern_idx, captures in matches_raw:
        if len(results) >= max_results:
            break
        for cap_name, nodes in captures.items():
            for node in nodes:
                if len(results) >= max_results:
                    break
                start_line = node.start_point[0]
                end_line = node.end_point[0]
                ctx_start = max(0, start_line - 1)
                ctx_end = min(len(lines) - 1, end_line + 1)
                if ctx_end - ctx_start > 10:
                    ctx_end = ctx_start + 10
                snippet_lines = []
                for i in range(ctx_start, ctx_end + 1):
                    prefix = ">>>" if start_line <= i <= end_line else "   "
                    snippet_lines.append(f"{prefix} {i + 1}: {lines[i].decode(errors='replace')}")
                results.append({
                    "capture": cap_name,
                    "node_type": node.type,
                    "text": node.text.decode(errors="replace"),
                    "start_line": start_line + 1,
                    "end_line": end_line + 1,
                    "start_col": node.start_point[1],
                    "end_col": node.end_point[1],
                    "snippet": "\n".join(snippet_lines),
                })

    output = {
        "file": file_path,
        "language": lang_name,
        "pattern": pattern,
        "total_matches": len(results),
        "matches": results,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


def dispatch_tool(function_name: str, arguments: dict) -> str | None:
    _fmt_tool_call(function_name, arguments)
    if function_name == "search_web": return handle_search_web(arguments.get("query", ""))
    if function_name == "run_cmd": return safe_run_cmd(arguments.get("command", ""))
    if function_name == "read_file": return handle_read_file(arguments.get("filepath", ""))
    if function_name == "get_url": return handle_get_url(arguments.get("url", ""))
    if function_name == "write_file": return handle_write_file(arguments.get("filepath", ""), arguments.get("content", ""))
    if function_name == "edit_file": return handle_edit_file(arguments.get("filepath", ""), arguments.get("old_content", ""), arguments.get("new_content", ""))
    if function_name == "delete_file": return handle_delete_file(arguments.get("filepath", ""))
    if function_name == "copy_file": return handle_copy_file(arguments.get("src", ""), arguments.get("dst", ""))
    if function_name == "list_dir": return handle_list_dir(arguments.get("dirpath", ""))
    if function_name == "create_dir": return handle_create_dir(arguments.get("dirpath", ""))
    if function_name == "git_status": return handle_git_status()
    if function_name == "git_diff": return handle_git_diff()
    if function_name == "write_memory": return handle_write_memory(arguments.get("id", ""), arguments.get("content", ""))
    if function_name == "get_memory_list": return handle_get_memory_list()
    if function_name == "read_memory": return handle_read_memory(arguments.get("id", ""))
    if function_name == "delete_memory": return handle_delete_memory(arguments.get("id", ""))
    if function_name == "edit_memory": return handle_edit_memory(arguments.get("id", ""), arguments.get("new_content", ""))
    if function_name == "get_user_input": return handle_get_input(arguments.get("what_do", ""), arguments.get("prompt", []))
    if function_name == "search_in_file": return handle_search_in_file(arguments.get("query", ""), arguments.get("is_regex", False)) # like a grep
    if function_name == "get_system_info": return handle_get_system_info()
    if function_name == "call_api": return handle_call_api(arguments.get("url", ""), arguments.get("method", ""), arguments.get("headers",""), arguments.get("payload",""))
    if function_name == "get_code_skeleton": return handle_get_code_skeleton(arguments.get("file_path", ""))
    if function_name == "query_ast_node": return handle_query_ast_node(arguments.get("file_path", ""), arguments.get("pattern", ""), arguments.get("language", ""))

    print(f"  {S.WARN}⚠ Unknown tool: {function_name}{S.R}")
    return None



token_history = []

def display_usage_graph(messages: list[dict]):
    if not token_history:
        print(f"  {S.GRAY}No token usage data yet.{S.R}")
    else:
        totals = [t["prompt"] + t["completion"] for t in token_history]
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

        total_prompt = sum(t["prompt"] for t in token_history)
        total_comp = sum(t["completion"] for t in token_history)
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


async def call_ollama(client: ollama.AsyncClient, messages: list[dict]) -> str:

    async def spinner():
        # frames = [
        #     f"{S.ACCENT}      ·{S.R}",
        #     f"{S.ACCENT}     · {S.R}",
        #     f"{S.ACCENT}    · ·{S.R}",
        #     f"{S.ACCENT}   · · {S.R}",
        #     f"{S.ACCENT}  · · ·{S.R}",
        #     f"{S.ACCENT} · · · {S.R}",
        #     f"{S.ACCENT}· · ·  {S.R}",
        #     f"{S.ACCENT} · ·   {S.R}",
        #     f"{S.ACCENT}· ·    {S.R}",
        #     f"{S.ACCENT} ·     {S.R}",
        #     f"{S.ACCENT}·      {S.R}"
        # ]
        # frames = [
        #     f"{S.ACCENT}· · ·   {S.R}",
        #     f"{S.ACCENT} · · ·  {S.R}",
        #     f"{S.ACCENT}  · · · {S.R}",
        #     f"{S.ACCENT}   · · ·{S.R}",
        #     f"{S.ACCENT}·   · · {S.R}",
        #     f"{S.ACCENT}· ·   · {S.R}",
        #     f"{S.ACCENT}· · ·   {S.R}",
        # ]
        """· ✢ ✳ ✶ ✻ ✽"""
        frames = [
            f"{S.ACCENT}·{S.R}",
            f"{S.ACCENT}✢\uFE0E{S.R}",
            f"{S.ACCENT}*\uFE0E{S.R}",
            f"{S.ACCENT}✶\uFE0E{S.R}",
            f"{S.ACCENT}✻\uFE0E{S.R}",
            f"{S.ACCENT}✽\uFE0E{S.R}",
        ]
        # frames = [
        #     f"{S.ACCENT}·{S.R}", 
        #     f"{S.ACCENT}✻{S.R}",
        #     f"{S.ACCENT}✽{S.R}",
        #     f"{S.ACCENT}✶{S.R}",
        #     f"{S.ACCENT}✳{S.R}",
        #     f"{S.ACCENT}✢{S.R}"
        # ]
        cycle = itertools.cycle(frames)
        try:
            while True:
                frame = next(cycle)
                sys.stdout.write(f'\r  {frame} {S.GRAY}thinking…{S.R}  ')
                sys.stdout.flush()
                await asyncio.sleep(random.randint(2, 4) * 0.1)
        except asyncio.CancelledError:
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()

    spin_task = asyncio.create_task(spinner())
    
    full_text = ""
    is_tool_call_check_done = False
    is_tool_call = False
    
    line_buffer = ""
    in_code = False
    
    table_buffer = []
    in_latex_block = False

    def flush_table():
        if table_buffer:
            for t_line in _format_table(table_buffer): print(t_line)
            table_buffer.clear()

    def process_line(line: str, in_c: bool) -> bool:
        nonlocal in_latex_block
        stripped = line.strip()
        if not in_c and not in_latex_block and stripped.startswith('|') and stripped.endswith('|'):
            table_buffer.append(line)
            return in_c
        else:
            flush_table()
            rendered, out_c, in_latex_block = _render_line(line, in_c, in_latex_block)
            for r_line in rendered.split('\n'):
                print(r_line)
            return out_c

    prompt_tokens = 0
    completion_tokens = 0
    total_duration = 0.0
    eval_duration = 0.0

    start_time = None
    chunk_count = 0

    try:
        response_stream = await client.chat(model=MODEL, messages=messages, stream=True, options={"num_ctx": NUM_CTX, "num_predict": NUM_PREDICT})

        async for chunk in response_stream:
            if start_time is None:
                start_time = time.time()

            chunk_count += 1

            content = chunk['message'].get('content', '')
            full_text += content

            if chunk.get('done'):
                prompt_tokens = chunk.get("prompt_eval_count", 0)
                completion_tokens = chunk.get("eval_count", 0)
                total_duration = chunk.get("total_duration", 0) / 1e9
                eval_duration = chunk.get("eval_duration", 0) / 1e9

            if not is_tool_call_check_done:
                if "<tool_call>" in full_text:
                    is_tool_call = True
                    is_tool_call_check_done = True
                elif len(full_text) > 15 or "\n" in full_text:
                    is_tool_call = False
                    is_tool_call_check_done = True
                    if not spin_task.done():
                        spin_task.cancel()
                        sys.stdout.write('\r\033[K')
                    for char in full_text:
                        if char == '\n':
                            in_code = process_line(line_buffer, in_code)
                            line_buffer = ""
                        else:
                            line_buffer += char
                continue

            if not is_tool_call:
                for char in content:
                    if char == '\n':
                        sys.stdout.write('\r\033[K')
                        sys.stdout.flush()
                        in_code = process_line(line_buffer, in_code)
                        line_buffer = ""
                    else:
                        line_buffer += char

                elapsed = time.time() - start_time
                if elapsed > 0.1:
                    tps = chunk_count / elapsed
                    sys.stdout.write(f"\r\033[K  {S.MUTED}TPS: {tps:.1f}{S.R}")
                    sys.stdout.flush()

        if not is_tool_call:
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()
            if line_buffer:
                in_code = process_line(line_buffer, in_code)
            flush_table()
            
    finally:
        if not spin_task.done():
            spin_task.cancel()
            try: await spin_task
            except asyncio.CancelledError: pass
            
    token_history.append({"prompt": prompt_tokens, "completion": completion_tokens})
    
    if not is_tool_call:
        _fmt_tokens(prompt_tokens, completion_tokens, total_duration, eval_duration)
        
    return full_text


_CHARS_PER_TOKEN = 3.5

def _estimate_tokens(messages: list[dict]) -> int:
    total_chars = sum(len(m.get("content", "")) for m in messages)
    return int(total_chars / _CHARS_PER_TOKEN)


def _get_ctx_budget() -> int:
    return int(NUM_CTX * 0.85) - NUM_PREDICT


def _get_summary_predict_tokens() -> int:
    try:
        model_list = ollama.list()
        m_list = model_list.get("models", []) if isinstance(model_list, dict) else getattr(model_list, 'models', [])
        for m in m_list:
            name = m.get("model", m.get("name", "")) if isinstance(m, dict) else getattr(m, 'model', getattr(m, 'name', ''))
            if name == MODEL:
                size_gb = (m.get("size", 0) if isinstance(m, dict) else getattr(m, 'size', 0)) / (1024 ** 3)
                if size_gb >= 15.0: return 600
                if size_gb >= 7.0:  return 400
                return 250
    except Exception:
        pass
    return 300


def _trim_tool_results(messages: list[dict], max_chars: int = 3000) -> list[dict]:
    result = []
    for m in messages:
        content = m.get("content", "")
        if m["role"] == "user" and content.startswith("[Tool Result") and len(content) > max_chars:
            trimmed = content[:max_chars] + f"\n...[Tool result truncated – {len(content) - max_chars} chars omitted]"
            result.append({**m, "content": trimmed})
        else:
            result.append(m)
    return result


def _get_conv_pairs(messages: list[dict]) -> list[list[dict]]:
    pairs: list[list[dict]] = []
    current: list[dict] = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "user" and not m["content"].startswith("[Tool Result"):
            if current:
                pairs.append(current)
            current = [m]
        else:
            current.append(m)
    if current:
        pairs.append(current)
    return pairs


async def _compress_context(client: ollama.AsyncClient, messages: list[dict]) -> bool:
    conv_msgs = [m for m in messages if m["role"] != "system"]
    if not conv_msgs:
        return False

    predict_tokens = _get_summary_predict_tokens()
    prompt = smrp()
    for m in conv_msgs:
        role = "User" if m["role"] == "user" else "Assistant"
        content = re.sub(r'<tool_call>.*?</tool_call>', '', m['content'], flags=re.DOTALL).strip()
        if content:
            if m["role"] == "user" and m["content"].startswith("[Tool Result"):
                content = content[:400] + ("..." if len(content) > 400 else "")
            prompt += f"[{role}]: {content}\n\n"

    summary_msg = [{"role": "user", "content": prompt}]

    async def spinner():
        frames = [
            f"{S.PURPLE}    ·  {S.R}",
            f"{S.PURPLE}   · · {S.R}",
            f"{S.PURPLE}  · · ·{S.R}",
            f"{S.PURPLE} · · · {S.R}",
            f"{S.PURPLE}· · ·  {S.R}",
            f"{S.PURPLE} · ·   {S.R}",
        ]
        cycle = itertools.cycle(frames)
        try:
            while True:
                frame = next(cycle)
                sys.stdout.write(f'\r  {frame} {S.GRAY}Compressing context…{S.R}  ')
                sys.stdout.flush()
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()

    spin_task = asyncio.create_task(spinner())
    try:
        response = await client.chat(
            model=MODEL, messages=summary_msg, stream=False,
            options={"num_predict": predict_tokens, "num_ctx": NUM_CTX}
        )
        summary = response['message']['content'].strip()
    except Exception as e:
        summary = ""
    finally:
        if not spin_task.done():
            spin_task.cancel()
            try: await spin_task
            except asyncio.CancelledError: pass

    if not summary or summary.startswith("(Summary failed"):
        return False

    sys_content = messages[0]["content"]
    sys_content = re.sub(r'\n\n<SUMMARY>.*?</SUMMARY>', '', sys_content, flags=re.DOTALL).strip()
    new_sys = f"{sys_content}\n\n<SUMMARY>\n{summary}\n</SUMMARY>"
    messages[0]["content"] = new_sys
    return True


async def manage_context(client: ollama.AsyncClient, messages: list[dict]) -> None:
    budget = _get_ctx_budget()

    trimmed = _trim_tool_results(messages)
    if _estimate_tokens(trimmed) <= budget:
        if trimmed != messages:
            messages[:] = trimmed
        return

    messages[:] = trimmed

    pairs = _get_conv_pairs(messages)
    n_pairs = len(pairs)

    if n_pairs <= 2:
        if _estimate_tokens(messages) > budget:
            print(f"\n  {S.WARN}⚠ Context limit approaching. Compressing…{S.R}")
            ok = await _compress_context(client, messages)
            if ok:
                latest = pairs[-1] if pairs else []
                messages[:] = [messages[0]] + latest
                print(f"  {S.OK}✓ Context compressed.{S.R}\n")
        return

    print(f"\n  {S.WARN}⚠ Context limit approaching. Compressing…{S.R}")
    ok = await _compress_context(client, messages)
    if ok:
        keep = pairs[-2:]
        keep_msgs = [msg for pair in keep for msg in pair]
        messages[:] = [messages[0]] + keep_msgs
        print(f"  {S.OK}✓ Context compressed ({n_pairs} → {len(keep)} pairs kept).{S.R}\n")
        return

    print(f"  {S.WARN}⚠ Summary failed. Dropping oldest turns…{S.R}")
    while _estimate_tokens(messages) > budget and len(_get_conv_pairs(messages)) > 2:
        cur_pairs = _get_conv_pairs(messages)
        keep_pairs = cur_pairs[1:]
        keep_msgs = [msg for pair in keep_pairs for msg in pair]
        messages[:] = [messages[0]] + keep_msgs

    dropped = n_pairs - len(_get_conv_pairs(messages))
    if dropped > 0:
        print(f"  {S.OK}✓ Dropped {dropped} oldest turn(s).{S.R}\n")



def parse_tool_call(response_text: str) -> tuple[str, dict] | None:
    match = re.search(r"<tool_call>(.*?)</tool_call>", response_text, re.DOTALL)
    if not match: return None
    try:
        tool_data = json.loads(match.group(1))
        name = tool_data.get("name")
        arguments = tool_data.get("arguments", {})
        if name: return name, arguments
    except json.JSONDecodeError:
        print(f"  {S.ERR}✗ AI generated invalid JSON for the tool call.{S.R}")
    return None

MAX_TOOL_CALLS = 10

async def chat_turn(client: ollama.AsyncClient, messages: list[dict]) -> str:
    call_count = 0
    while True:
        if call_count > 0 and call_count % MAX_TOOL_CALLS == 0:
            print(f"\n  {S.WARN}⚠  Tool call limit ({MAX_TOOL_CALLS}) reached.{S.R}")
            try:
                user_choice = input(f"  {S.WARN}Continue? {S.MUTED}[{S.OK}y{S.MUTED}/{S.ERR}n{S.MUTED}]{S.R} {S.WARN}›{S.R} ").strip().lower()
            except:
                user_choice = "n"
            if user_choice != 'y':
                return messages[-2]["content"] if len(messages) >= 2 else "Tool usage stopped."
                
        response_text = await call_ollama(client, messages)
        messages.append({"role": "assistant", "content": response_text})

        parsed = parse_tool_call(response_text)
        if parsed is None:
            return response_text

        function_name, arguments = parsed
        tool_result = dispatch_tool(function_name, arguments)

        if tool_result is None:
            return response_text

        _fmt_tool_result(function_name, tool_result)

        messages.append({
            "role": "user",
            "content": f"[Tool Result for '{function_name}']:\n{tool_result}",
        })
        call_count += 1



async def main() -> None:
    global MODEL, AUTO_ALLOW, RETURN_ALL_FILE_CONTENT, SAVE_CHAT_HISTORY, CUSTOM_PERSONA
    client = ollama.AsyncClient()
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    if CURRENT_OS == "Windows":
        os.system("")
    
    print("\033[2J\033[H", end="")
    _welcome()

    current_session_id = None

    if PROMPT_TOOLKIT_AVAILABLE:
        completer = SlashCommandCompleter(['/help', '/clear', '/usage', '/model', '/models', '/exit', '/quit', '/sessions', '/load', '/automode', '/fullcontent', '/record', '/export', '/system'])
        session_pt = PromptSession(
            history=FileHistory('.chat_history'),
            completer=completer,
        )

    while True:
        try:
            if PROMPT_TOOLKIT_AVAILABLE:
                user_input = await session_pt.prompt_async(ANSI(f"  {S.USER_CLR}{S.BOLD}❯{S.R} "))
                user_input = user_input.strip()
            else:
                user_input = input(f"  {S.USER_CLR}{S.BOLD}❯{S.R} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n\n  {S.GRAY}Goodbye!{S.R}\n")
            break

        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("/exit", "/quit"):
            print(f"\n  {S.GRAY}Goodbye!{S.R}\n")
            break
        if cmd == "/usage":
            display_usage_graph(messages)
            continue
        if cmd == "/help":
            _show_help()
            continue
        if cmd == "/clear":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            current_session_id = None
            token_history.clear()
            print("\033[2J\033[H", end="")
            _welcome()
            print(f"  {S.OK}✓ Conversation and usage cleared.{S.R}\n")
            continue
        if cmd == "/models":
            print(f"\n  {S.BOLD}{S.ACCENT}Available Ollama Models{S.R}")
            print(f"  {_hr(width=50)}")
            try:
                model_list = ollama.list()
                models_available = model_list.get("models", []) if isinstance(model_list, dict) else (model_list.models if hasattr(model_list, 'models') else [])
                if not models_available:
                    print(f"  {S.WARN}⚠ No models found. Pull a model with: ollama pull <model>{S.R}")
                else:
                    for i, m in enumerate(models_available, 1):
                        name = m.get("model", m.get("name", "unknown")) if isinstance(m, dict) else (m.model if hasattr(m, 'model') else str(m))
                        size_bytes = m.get("size", 0) if isinstance(m, dict) else (m.size if hasattr(m, 'size') else 0)
                        size_gb = size_bytes / (1024**3)
                        marker = f" {S.OK}◀ current{S.R}" if name == MODEL else ""
                        if size_gb >= 1:
                            print(f"  {S.ACCENT}{i:3}.{S.R} {S.WHITE}{name}{S.R}  {S.GRAY}({size_gb:.1f}GB){S.R}{marker}")
                        else:
                            size_mb = size_bytes / (1024**2)
                            print(f"  {S.ACCENT}{i:3}.{S.R} {S.WHITE}{name}{S.R}  {S.GRAY}({size_mb:.0f}MB){S.R}{marker}")
            except Exception as e:
                print(f"  {S.ERR}✗ Failed to list models: {e}{S.R}")
            print()
            continue
        if cmd == "/model":
            print(f"\n  {S.GRAY}model{S.R}  {S.WHITE}{MODEL}{S.R}\n")
            try:
                model_list = ollama.list()
                models_available = model_list.get("models", []) if isinstance(model_list, dict) else (model_list.models if hasattr(model_list, 'models') else [])
                if not models_available:
                    print(f"  {S.WARN}⚠ No models found.{S.R}\n")
                    continue
                model_names = []
                for m in models_available:
                    name = m.get("model", m.get("name", "unknown")) if isinstance(m, dict) else (m.model if hasattr(m, 'model') else str(m))
                    model_names.append(name)
                print(f"  {S.BOLD}{S.ACCENT}Select a model:{S.R}")
                for i, name in enumerate(model_names, 1):
                    marker = f" {S.OK}◀ current{S.R}" if name == MODEL else ""
                    print(f"  {S.ACCENT}{i:3}.{S.R} {S.WHITE}{name}{S.R}{marker}")
                print(f"  {S.MUTED}  0.{S.R} {S.GRAY}Cancel{S.R}")
                print()
                try:
                    choice = input(f"  {S.INFO}Select{S.R} {S.MUTED}(0~{len(model_names)}){S.R} {S.INFO}›{S.R} ").strip()
                    if not choice or choice == "0":
                        print(f"  {S.GRAY}Cancelled.{S.R}\n")
                        continue
                    idx = int(choice) - 1
                    if 0 <= idx < len(model_names):
                        old_model = MODEL
                        MODEL = model_names[idx]
                        if old_model == MODEL:
                            print(f"  {S.GRAY}Already using {MODEL}.{S.R}\n")
                        else:
                            print(f"  {S.OK}✓ Model changed: {old_model} → {MODEL}{S.R}\n")
                    else:
                        print(f"  {S.ERR}✗ Invalid selection.{S.R}\n")
                except (ValueError, EOFError, KeyboardInterrupt):
                    print(f"\n  {S.GRAY}Cancelled.{S.R}\n")
            except Exception as e:
                print(f"  {S.ERR}✗ Failed to list models: {e}{S.R}\n")
            continue
        if cmd == "/sessions":
            sessions = list_sessions()
            print(f"\n  {S.BOLD}{S.ACCENT}Saved Sessions{S.R}")
            if not sessions:
                print(f"  {S.GRAY}  No saved sessions yet.{S.R}")
            for sid, meta in sessions:
                print(f"  {S.GRAY}•{S.R} {sid:<18} {S.GRAY}{meta}{S.R}")
            print()
            continue
        if cmd.startswith("/load"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2:
                print(f"  {S.ERR}✗ Usage: /load <session_id>{S.R}\n")
                continue
            sid = parts[1].strip()
            loaded = load_session(sid)
            if loaded:
                if isinstance(loaded, dict) and loaded.get("version") == 2:
                    messages = loaded.get("messages", [])
                    token_history.clear()
                    token_history.extend(loaded.get("token_history", []))
                    MODEL = loaded.get("model", MODEL)
                    CUSTOM_PERSONA = loaded.get("persona", CUSTOM_PERSONA)
                else:
                    messages = loaded
                    token_history.clear()
                current_session_id = sid
                print(f"  {S.OK}✓ Loaded session: {sid} (Model: {MODEL}){S.R}\n")
                for msg in messages:
                    if msg["role"] == "system": 
                        continue
                    elif msg["role"] == "user":
                        if msg["content"].startswith("[Tool Result for '"):
                            m = re.match(r"\[Tool Result for '([^']+)'\]:\n(.*)", msg["content"], re.DOTALL)
                            if m:
                                _fmt_tool_result(m.group(1), m.group(2))
                            continue
                        print(f"  {S.USER_CLR}{S.BOLD}❯{S.R} {msg['content']}")
                    elif msg["role"] == "assistant":
                        parsed = parse_tool_call(msg["content"])
                        if parsed:
                            _fmt_tool_call(parsed[0], parsed[1])
                            
                        c = re.sub(r'<tool_call>.*?</tool_call>', '', msg["content"], flags=re.DOTALL).strip()
                        if c:
                            print(_render_full(c))
                            print()
            else:
                print(f"  {S.ERR}✗ Session not found: {sid}{S.R}\n")
            continue
        if cmd.startswith("/automode"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2 or not parts[1] in ("on", "off"):
                print(f"  {S.ERR}✗ Usage: /automode <on/off>{S.R}\n")
                continue
            if parts[1] == "on":
                AUTO_ALLOW = True
            if parts[1] == "off":
                AUTO_ALLOW = False
            print(f"  {S.INFO}✓ Automode has been on.{S.R}" if parts[1] == "on" else f"  {S.INFO}✓ Automode has been off.{S.R}")
            continue
        if cmd.startswith("/fullcontent"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2 or not parts[1] in ("on", "off"):
                print(f"  {S.ERR}✗ Usage: /fullcontent <on/off>{S.R}\n")
                continue
            if parts[1] == "on":
                RETURN_ALL_FILE_CONTENT = True
            if parts[1] == "off":
                RETURN_ALL_FILE_CONTENT = False
            print(f"  {S.INFO}✓ Full content mode has been turned on.{S.R}" if parts[1] == "on" else f"  {S.INFO}✓ Full content mode has been turned off.{S.R}")
            continue
        if cmd.startswith("/record"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2 or not parts[1] in ("on", "off"):
                print(f"  {S.ERR}✗ Usage: /record <on/off>{S.R}\n")
                continue
            if parts[1] == "on":
                SAVE_CHAT_HISTORY = True
            if parts[1] == "off":
                SAVE_CHAT_HISTORY = False
            print(f"  {S.INFO}✓ Chat history recording is ON.{S.R}" if parts[1] == "on" else f"  {S.INFO}✓ Chat history recording is OFF.{S.R}")
            continue
        if cmd.startswith("/export"):
            parts = cmd.split(" ", 1)
            filename = parts[1].strip() if len(parts) > 1 else f"export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    for m in messages:
                        if m["role"] == "system": continue
                        role_name = "User" if m["role"] == "user" else "Assistant"
                        c = re.sub(r'<tool_call>.*?</tool_call>', '', m["content"], flags=re.DOTALL).strip()
                        if c: f.write(f"### {role_name}\n\n{c}\n\n")
                print(f"  {S.OK}✓ Conversation exported to {filename}{S.R}\n")
            except Exception as e:
                print(f"  {S.ERR}✗ Export failed: {e}{S.R}\n")
            continue
        if cmd.startswith("/system"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2:
                print(f"  {S.ERR}✗ Usage: /system <new prompt> or /system reset{S.R}\n")
                continue
            new_prompt = parts[1].strip()
            current_sys = messages[0]["content"]
            summary_match = re.search(r'\n\n<SUMMARY>(.*?)</SUMMARY>', current_sys, re.DOTALL)
            summary_text = f"\n\n<SUMMARY>{summary_match.group(1)}</SUMMARY>" if summary_match else ""

            if new_prompt.lower() == "reset":
                CUSTOM_PERSONA = ""
                messages[0]["content"] = SYSTEM_PROMPT + summary_text
                print(f"  {S.INFO}✓ System prompt reset to default.{S.R}")
                print(f"  {S.WARN}⚠ If the persona context from the previous conversation remains, please clear the conversation history with /clear.{S.R}\n")
            else:
                CUSTOM_PERSONA = new_prompt
                messages[0]["content"] = CUSTOM_PERSONA + "\n\n" + SYSTEM_PROMPT + summary_text
                print(f"  {S.INFO}✓ System prompt updated.{S.R}")
                print(f"  {S.WARN}⚠ To ensure the persona is applied correctly, please clear the previous conversation with /clear.{S.R}\n")
            continue

        messages.append({"role": "user", "content": user_input})
        current_session_id = save_session(messages, current_session_id)

        try:
            await manage_context(client, messages)

            result = await chat_turn(client, messages)

            current_session_id = save_session(messages, current_session_id)

        except Exception as e:
            print(f"\n  {S.ERR}✗ Error: {e}{S.R}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n\n  {S.GRAY}Goodbye!{S.R}\n")
    except Exception as e:
        print(f"\n  {S.ERR}✗ Unexpected error: {e}{S.R}")
