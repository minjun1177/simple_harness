"""State belongs to the person, not to whatever directory they started in.

`sessions/`, `memory.json` and `.chat_history` used to be written into the
working directory. That was survivable while the harness was `python app.py`
inside its own checkout. As an installed command it meant starting in a home
directory left files there, two projects gave you two unrelated memories, and
`/sessions` only ever listed the ones belonging to wherever you were standing.

The checks below are the ones that would have caught that: nothing personal
resolves to a relative path, every module agrees on one home, and the override
the tests themselves rely on actually works.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from simple_harness import paths

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


# The override has to be set before `config` is imported: it reads the paths at
# import time, and this must not touch the real ~/.localchat.
HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".paths-test-home")
os.environ[paths.ENV_VAR] = HOME

from simple_harness import config          # noqa: E402
from simple_harness import mcp_client, permissions, providers, skills   # noqa: E402

print("--- the override decides where everything goes ---")
check("home() follows LOCALCHAT_HOME", paths.home() == os.path.abspath(HOME),
      paths.home())
check("state() builds inside it",
      paths.state("x.json") == os.path.join(os.path.abspath(HOME), "x.json"))

print("\n--- nothing personal is written where the user happens to stand ---")
personal = {
    "config.MEMORY_FILE": config.MEMORY_FILE,
    "config.SESSION_DIR": config.SESSION_DIR,
    "config.HISTORY_FILE": config.HISTORY_FILE,
    "providers.CONFIG_PATH": providers.CONFIG_PATH,
    "skills.USER_SKILL_DIR": skills.USER_SKILL_DIR,
    "mcp_client.USER_CONFIG_FILE": mcp_client.USER_CONFIG_FILE,
    "permissions.USER_CONFIG_FILE": permissions.USER_CONFIG_FILE,
}
for name, value in personal.items():
    check(f"{name} is absolute", os.path.isabs(value), value)
    check(f"{name} is under the home directory",
          os.path.commonpath([value, paths.home()]) == paths.home())

print("\n--- what is about a project stays with the project ---")
# These are read from the working directory *first* and from the home directory
# second. A project's own rules, servers and skills have to be able to win.
check("permissions still reads a project file",
      any(source == "project" for source, _ in permissions.config_paths()))
check("mcp still reads a project file",
      any(source == "project" for source, _ in mcp_client.config_paths()))
check("skills still reads a project directory",
      skills.skill_dirs()[0] == ("project", os.path.abspath("skills")))

print("\n--- state left by an older version is named, never moved ---")
work = os.path.join(HOME, "work")
os.makedirs(work, exist_ok=True)
origin = os.getcwd()
os.chdir(work)
try:
    check("a clean directory reports nothing", paths.strays_in_cwd() == [])

    open("memory.json", "w").close()
    os.makedirs("sessions", exist_ok=True)
    found = paths.strays_in_cwd()
    check("an older version's files are found", set(found) == {"memory.json", "sessions"},
          str(found))
    check("and they are still there afterwards",
          os.path.exists("memory.json") and os.path.isdir("sessions"))
finally:
    os.chdir(origin)

shutil.rmtree(HOME, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("path checks passed")
