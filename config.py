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
from systemprompt import titleprompt as ttlp


MODEL = "gemma4:e4b"

CURRENT_OS = platform.system()

SYSTEM_PROMPT = syp()

FILE_MAX_DISPLAY_LENGTH = 1000

MEMORY_FILE = "memory.json"
SESSION_DIR = "sessions"

# Sessions are filed under a human title instead of a timestamp. AUTO_TITLE lets
# the model name a new session after its first exchange; /title overrides it.
AUTO_TITLE = True
SESSION_TITLE = ""
SESSION_TITLE_MAX_LEN = 60
SESSION_SLUG_MAX_LEN = 48

SEARCH_MAX_RESULTS = 5

# Point this at a self-hosted SearXNG (e.g. "http://localhost:8080") to make
# candidate generation fully local. Empty = use the keyless public sources only.
SEARXNG_URL = ""

SEARCH_CANDIDATES = 10          # results requested per source
SEARCH_FETCH_PAGES = 8          # pages actually downloaded and read
SEARCH_PAGE_CHARS = 60000       # per-page text kept for ranking
SEARCH_PASSAGE_CHARS = 900      # passage size, and the snippet size returned
SEARCH_RESULT_CHARS = 6000      # ceiling on the whole tool result
SEARCH_SOURCE_TIMEOUT = 12
SEARCH_FETCH_TIMEOUT = 10
SEARCH_TOTAL_TIMEOUT = 25

NUM_CTX = 32768*2
NUM_PREDICT = 4096*1.5      # 2048 truncated file writes mid-way

AUTO_ALLOW = False
RETURN_ALL_FILE_CONTENT = True
SAVE_CHAT_HISTORY = True
CUSTOM_PERSONA = ""

# run_cmd stays connected to the command instead of waiting for it to finish.
# When the output goes quiet for CMD_IDLE_TIMEOUT the process is most likely
# sitting at a prompt, so what it printed is handed to the model, which answers
# with send_input. It never gets the user's terminal: its prompts are captured,
# so holding the terminal only froze the app with nothing on screen.
CMD_IDLE_TIMEOUT = 0.6      # silence this long is worth looking at
CMD_WAIT_TIMEOUT = 8        # ...but silence only counts as a prompt after this,
                            # unless the output looks like one or /proc confirms it
CMD_IDLE_GRACE = 2.5        # Windows/macOS, where /proc cannot be asked: how long
                            # silence with no CPU burned has to last instead
CMD_TIMEOUT = 120           # ceiling on any single read from a command
CMD_OUTPUT_CHARS = 6000     # per-read output ceiling; the newest is kept
CMD_MAX_SESSIONS = 3        # live commands kept at once; the oldest is dropped
CMD_SESSION_LIFETIME = 900  # seconds a live command may sit idle before it is killed

MAX_TOOL_CALLS = 10

PLANMODE = False

LOADED_SKILLS = []

# --- tool permissions --------------------------------------------------------
# Rules live in .permissions.json / ~/.localchat/permissions.json. Empty rules
# behave exactly as before: the approval prompt decides.
PERMISSIONS_ENABLED = True
POLICY_AUTO_ALLOW = False       # set per call by dispatch_tool; not a user setting

# --- reasoning ("thinking") models -------------------------------------------
# Reasoning models wrap their scratch work in <think> tags, or return it in
# Ollama's separate `thinking` field. It is never the answer, so by default it
# is neither shown nor kept in the conversation history.
SHOW_THINKING = False
STORE_THINKING = False

# --- MCP (Model Context Protocol) -------------------------------------------
# Servers are declared in ./.mcp.json (project) or ~/.localchat/mcp.json (user).
MCP_ENABLED = True
MCP_STARTUP_TIMEOUT = 30        # seconds to wait for a server's initialize
MCP_CALL_TIMEOUT = 120          # seconds to wait for a tools/call result
MCP_HTTP_TIMEOUT = 60           # per-request timeout for http/sse transports
MCP_RESULT_CHARS = 8000         # ceiling on a single MCP result handed to the model
MCP_MAX_TOOLS_PER_SERVER = 40   # keeps one chatty server from flooding the prompt
MCP_TRUSTED_SERVERS = []        # server names whose calls skip the approval prompt
MCP_AUTO_APPROVE_READONLY = False  # trust a tool's own readOnlyHint annotation

token_history = []


class S:
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


_SURROGATE_PATTERN = re.compile(r'[\ud800-\udfff]')


def _console_encodings() -> list[str]:
    """What the console might actually be handing over, most likely first.

    UTF-8 is tried first on purpose: a legacy code page will happily decode
    almost any byte into mojibake, so guessing it before UTF-8 would quietly
    corrupt text that was fine.
    """
    candidates = ["utf-8"]
    if CURRENT_OS == "Windows":
        try:
            import ctypes
            for code_page in (ctypes.windll.kernel32.GetConsoleCP(),
                              ctypes.windll.kernel32.GetACP()):
                if code_page:
                    candidates.append(f"cp{code_page}")
        except Exception:
            pass
    try:
        import locale
        preferred = locale.getpreferredencoding(False)
        if preferred:
            candidates.append(preferred)
    except Exception:
        pass

    seen, ordered = set(), []
    for encoding in candidates:
        key = encoding.lower().replace("-", "")
        if key not in seen:
            seen.add(key)
            ordered.append(encoding)
    return ordered


def safe_text(text):
    """Repair console input that arrived as surrogate escapes.

    In Python's UTF-8 mode `sys.stdin` is read as UTF-8 with the
    `surrogateescape` error handler. On a Windows console that hands over cp949
    (or any other legacy code page) bytes, Korean input therefore comes back as
    lone surrogates. Those cannot be encoded again, so the moment such a string
    enters the conversation both `json.dump` and the request to Ollama raise
    `UnicodeEncodeError` - and every later turn fails too, because the bad
    string is still sitting in the history.

    The escaped bytes are not lost: encoding them back recovers the original
    bytes, which then decode properly.
    """
    if not isinstance(text, str) or not _SURROGATE_PATTERN.search(text):
        return text
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return _SURROGATE_PATTERN.sub(" ", text)

    for encoding in _console_encodings():
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return _SURROGATE_PATTERN.sub(" ", text)


def repair_messages(messages: list[dict]) -> list[dict]:
    """Repair the conversation in place so it can always be encoded.

    Repairing rather than copying is deliberate: a broken string left in the
    history would fail on every later turn as well, and the conversation would
    stay stuck until it was cleared.
    """
    for message in messages:
        content = message.get("content", "")
        repaired = safe_text(content)
        if repaired is not content:
            message["content"] = repaired
    return messages


def _hr(char: str = "─", width: int = 0, style: str = S.MUTED) -> str:
    w = width or max(1, tw() - 6)
    return f"{style}{char * w}{S.R}"
