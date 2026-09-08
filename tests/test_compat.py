"""The public surface, written down here, so it cannot shrink by accident.

A version number is a promise about what will still be there tomorrow, and a
promise nobody wrote down is one the code cannot keep. This file is the writing
down: the commands, the settings, the tool names and the files on disk that
other people's habits, scripts and saved sessions are built on.

Adding is free. **Removing or renaming is what this catches** - the lists below
have to be edited by hand for the suite to go green again, and that edit is the
deliberate act. It is also the diff a reviewer sees, which is the whole point:
`- "/undo",` in a changed file is a conversation, and a quietly deleted command
is not.

Four of the five surfaces are less obvious than they look:

- **Setting names are public the moment they exist.** `/set` derives its list
  from the UPPER_CASE names in `config.py` rather than a table (invariant 5.1
  applied to settings), which is the right design and means a rename there is a
  rename of somebody's `~/.localchat/settings.json` key.
- **Tool names are written into saved sessions**, as `<tool_call>` text, whatever
  protocol produced them. Renaming one does not just change a prompt; it stops an
  old session from replaying.
- **The state layout is one directory** and `LOCALCHAT_HOME` moves all of it
  together. Moving one file out on its own splits a memory or a session list in
  two, which is why those paths are not settings.
- **Project files are read from the working directory first.** That precedence
  is the feature - a repository can carry its own permissions and its own MCP
  servers - so it is pinned, not just the filenames.

While the major version is 0 this is a record rather than a guarantee. It is
here from 0.6.0 so that 1.0.0 can be a promise this project has already been
keeping, instead of one it makes on the day.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from simple_harness import config
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

import simple_harness
from simple_harness import mcp_client
from simple_harness import paths
from simple_harness import permissions
from simple_harness import skills
from simple_harness import toolspec

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def missing(promised, present):
    return sorted(set(promised) - set(present))


def report_new(present, promised):
    """Say what has appeared since. Not a failure - adding is always allowed."""
    added = sorted(set(present) - set(promised))
    if added:
        print(f"         new since this list was written: {', '.join(added)}")


# ---------------------------------------------------------------------------
# 1. slash commands
# ---------------------------------------------------------------------------

COMMANDS = (
    "/agents", "/autocommit", "/automode", "/autotitle", "/autoverify",
    "/clear", "/connect", "/deepthink", "/export", "/fullcontent", "/help",
    "/load", "/mcp", "/model", "/models", "/perms", "/planmode", "/record",
    "/sessions", "/set", "/skill", "/skills", "/system", "/tdd", "/think",
    "/title", "/undo", "/usage", "/vm",
)

with open(os.path.join(ROOT, "simple_harness", "app.py"), encoding="utf-8") as f:
    APP = f.read()
defined = (set(re.findall(r'cmd == "(/[a-z]+)"', APP))
           | set(re.findall(r'cmd\.startswith\("(/[a-z]+)', APP)))

print("--- every command that was ever promised still answers ---")
gone = missing(COMMANDS, defined)
check("none of them has been dropped or renamed", not gone, str(gone))
report_new(defined, COMMANDS)

# ---------------------------------------------------------------------------
# 2. settings `/set` may change
# ---------------------------------------------------------------------------

SETTINGS = (
    "AUTO_ALLOW", "AUTO_TITLE", "AUTO_VERIFY", "CHANNEL_CLAIMS",
    "CHANNEL_CLAIM_TTL", "CHANNEL_ENABLED", "CHANNEL_POLL_SECONDS",
    "CHANNEL_STALE", "CHANNEL_WRITE_TTL", "CMD_IDLE_GRACE", "CMD_IDLE_TIMEOUT",
    "CMD_MAX_SESSIONS", "CMD_OUTPUT_CHARS", "CMD_SESSION_LIFETIME",
    "CMD_TIMEOUT", "CMD_WAIT_TIMEOUT", "DEEPTHINK", "DEEPTHINK_MAX_PASSES",
    "FILE_MAX_DISPLAY_LENGTH", "GIT_AUTO_COMMIT", "MAX_TOOL_CALLS",
    "MCP_AUTO_APPROVE_READONLY", "MCP_CALL_TIMEOUT", "MCP_ENABLED",
    "MCP_HTTP_TIMEOUT", "MCP_LAZY_MIN_TOOLS", "MCP_LAZY_TOOLS",
    "MCP_MAX_TOOLS_PER_SERVER", "MCP_RESULT_CHARS", "MCP_STARTUP_TIMEOUT",
    "MCP_TRUSTED_SERVERS", "MENTION_MAX_CHARS", "NATIVE_TOOLS", "NUM_CTX",
    "NUM_PREDICT", "PERMISSIONS_ENABLED", "PLANMODE", "RETURN_ALL_FILE_CONTENT",
    "SAVE_CHAT_HISTORY", "SEARCH_CANDIDATES", "SEARCH_FETCH_PAGES",
    "SEARCH_FETCH_TIMEOUT", "SEARCH_MAX_RESULTS", "SEARCH_PAGE_CHARS",
    "SEARCH_PASSAGE_CHARS", "SEARCH_RESULT_CHARS", "SEARCH_SOURCE_TIMEOUT",
    "SEARCH_TOTAL_TIMEOUT", "SEARXNG_URL", "SESSION_SLUG_MAX_LEN",
    "SESSION_TITLE_MAX_LEN", "SHOW_THINKING", "STORE_THINKING",
    "SUBAGENT_MAX_DEPTH", "SUBAGENT_MAX_TURNS", "TDD_VERIFY_FAILURES",
    "VERIFY_OUTPUT_CHARS", "VERIFY_TIMEOUT", "VM_FILE_MB", "VM_MEMORY_MB",
    "VM_OUTPUT_CHARS", "VM_TIMEOUT",
)

print("\n--- every setting name somebody may have saved still resolves ---")
settable = config.settable()
gone = missing(SETTINGS, settable)
check("none of them has been renamed out from under a settings.json", not gone,
      str(gone))
# A setting that stops being one is the same break as a rename: `/set` stops
# knowing the name, and the saved value stops being applied.
check("and each is still the kind of value it was",
      all(isinstance(settable.get(name), (bool, int, float, str, list))
          for name in SETTINGS if name in settable))
report_new(settable, SETTINGS)

# ---------------------------------------------------------------------------
# 3. tool names
# ---------------------------------------------------------------------------

TOOLS = (
    "call_api", "claim_files", "copy_file", "create_dir", "delete_file",
    "delete_memory", "edit_file", "edit_memory", "end_process",
    "get_code_skeleton", "get_memory_list", "get_system_info", "get_url",
    "get_user_input", "git_diff", "git_status", "list_agents", "list_dir",
    "query_ast_node", "read_file", "read_memory", "release_files", "run_cmd",
    "run_python", "search_in_file", "search_web", "send_agent_message",
    "send_input", "spawn_agent", "submit_plan_for_approval", "use_mcp_server",
    "use_skill", "write_file", "write_memory",
)

print("\n--- every tool name a saved session may contain still exists ---")
names = [t.name for t in toolspec.TOOLS]
gone = missing(TOOLS, names)
check("none of them has been renamed", not gone, str(gone))
check("the table has no duplicates", len(names) == len(set(names)))
report_new(names, TOOLS)

# ---------------------------------------------------------------------------
# 4. what is on disk, and the one variable that moves it
# ---------------------------------------------------------------------------

STATE = {
    "memory.json":      config.MEMORY_FILE,
    "sessions":         config.SESSION_DIR,
    "history":          config.HISTORY_FILE,
    "settings.json":    config.SETTINGS_FILE,
    "permissions.json": permissions.USER_CONFIG_FILE,
    "mcp.json":         mcp_client.USER_CONFIG_FILE,
    "skills":           skills.USER_SKILL_DIR,
}

print("\n--- the state layout is the one people's files are already in ---")
wrong = sorted(name for name, path in STATE.items()
               if os.path.basename(path) != name)
check("each is still under the name it was written as", not wrong, str(wrong))
under_home = sorted(name for name, path in STATE.items()
                    if os.path.dirname(path) != paths.home())
check("and all of them in one directory, not scattered", not under_home,
      str(under_home))
check("the directory is still ~/.localchat", paths.DIR_NAME == ".localchat")

# The whole reason the paths above are not settings: one variable moves the lot,
# so two profiles stay two profiles instead of half of each.
os.environ[paths.ENV_VAR] = os.path.join(ROOT, "nowhere")
try:
    moved = paths.state("sessions")
finally:
    del os.environ[paths.ENV_VAR]
check(f"{paths.ENV_VAR} still moves all of it together",
      moved == os.path.join(ROOT, "nowhere", "sessions"), moved)

# ---------------------------------------------------------------------------
# 5. project files, and that the project still wins
# ---------------------------------------------------------------------------

print("\n--- a project's own files are still read, and still read first ---")
check("`.permissions.json` is the project's permission file",
      permissions.PROJECT_CONFIG_FILES[0] == ".permissions.json",
      str(permissions.PROJECT_CONFIG_FILES))
check("`.mcp.json` is the project's server list",
      mcp_client.PROJECT_CONFIG_FILES[0] == ".mcp.json",
      str(mcp_client.PROJECT_CONFIG_FILES))
check("`skills/<name>/SKILL.md` is still what a skill is",
      skills.SKILL_FILENAME == "SKILL.md")
check("and the project's skills still beat the user's",
      [label for label, _ in skills.skill_dirs()] == ["project", "user"],
      str([label for label, _ in skills.skill_dirs()]))

# ---------------------------------------------------------------------------
# 6. the version says which promise this is
# ---------------------------------------------------------------------------

print("\n--- the changelog accounts for the version being shipped ---")
with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
    LOG = f.read()
version = simple_harness.__version__
check("the version is a release number", re.fullmatch(r"\d+\.\d+\.\d+", version),
      version)
check("the changelog has a section for it", f"## {version}" in LOG, version)
check("and it names the surface this file pins", "## Compatibility" in LOG)
# The 0.x escape clause is load-bearing while it is true, and misleading the
# moment it stops being. 1.0.0 has to remove it in the same commit.
zero_x = version.startswith("0.")
check("the 0.x caveat is there while the major version is 0"
      if zero_x else "the 0.x caveat is gone now that 1.0 has shipped",
      ("while the major version is 0" in LOG) == zero_x)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("compatibility checks passed")
