"""Two harnesses in one project, and what stops them destroying each other's work.

The failure this exists to prevent is silent: agent A reads a file, agent B
reads the same file, both write their own idea of it, and B's write throws A's
away with nothing on either screen to say so. So the checks that matter most
here are the ones about a *refusal* - that a file somebody else is holding
cannot be written from another session, that the refusal names who to ask, and
that it stops being a refusal the moment the holder goes away.

The rest is about not creating a new way to lose work: nothing half-claimed,
nothing claimed forever by a terminal that crashed, no message reported as
delivered to an agent that was never there, and no lost write when several
processes touch the board at the same moment.

Two agents are simulated in one process by swapping `channel._id`, which is the
only thing that distinguishes them; the concurrency check uses real subprocesses
because a lock is not worth testing inside one interpreter.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from simple_harness import paths

# Before `config` is imported: it resolves the state paths at import time, and
# none of this may touch the real ~/.localchat.
HOME = tempfile.mkdtemp(prefix="channel-home-")
os.environ[paths.ENV_VAR] = HOME

from simple_harness import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True
config.GIT_AUTO_COMMIT = False             # git is tested in test_git_ops.py

from simple_harness import channel         # noqa: E402
from simple_harness import tools           # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


WORK = tempfile.mkdtemp(prefix="channel-work-")


def fresh_board():
    """An empty workspace with nobody on it."""
    channel.reset()
    channel._workspace = os.path.realpath(WORK)
    for path in (channel.board_path(), channel.board_path() + ".lock"):
        try:
            os.unlink(path)
        except OSError:
            pass


def as_agent(agent_id):
    """Act as an agent that is already registered. The id is all that differs."""
    channel._id = agent_id
    channel._last_beat, channel._last_label = 0.0, ""


def join(label):
    """Register another agent, and leave this process acting as it."""
    channel._id = ""
    return channel.join(label)


def board():
    return channel.read_board()


def drain(*agent_ids):
    """Read past the arrival notices, which are messages like any other.

    Joining is announced to everybody already here, so an agent that has just
    watched somebody else arrive has something waiting for it. That is checked
    on its own further down; the checks about what was *said* start from quiet.
    """
    for agent_id in agent_ids:
        as_agent(agent_id)
        channel.take_for_model()
        channel.take_for_screen()


def path_in_work(name):
    full = os.path.join(WORK, name)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


print("--- the board is state about a workspace, not a file in it ---")
fresh_board()
a1 = join("model-one")
check("joining gives this session an id", a1 == "a1", a1)
check("the board is under the home directory",
      os.path.commonpath([channel.board_path(), paths.home()]) == paths.home(),
      channel.board_path())
check("and nothing was written into the project",
      os.listdir(WORK) == [], str(os.listdir(WORK)))
check("its name says which project it is for",
      os.path.basename(channel.board_path()).startswith(
          os.path.basename(os.path.realpath(WORK))[:32]))

print("\n--- one workspace, whichever directory of it you started in ---")
repo = tempfile.mkdtemp(prefix="channel-repo-")
subprocess.run(("git", "init", "-q", "-b", "main", repo), capture_output=True)
deep = os.path.join(repo, "src", "inner")
os.makedirs(deep, exist_ok=True)
origin = os.getcwd()
try:
    from simple_harness import git_ops
    os.chdir(deep)
    channel.reset()
    git_ops._repo_root_cache.clear()
    check("a terminal in a subdirectory shares the top of the repository",
          channel.workspace() == os.path.realpath(repo),
          f"{channel.workspace()} != {os.path.realpath(repo)}")
    deep_board = channel.board_path()
    os.chdir(repo)
    channel.reset()
    git_ops._repo_root_cache.clear()
    check("so both of them read the same board", channel.board_path() == deep_board)
finally:
    os.chdir(origin)
    shutil.rmtree(repo, ignore_errors=True)

print("\n--- two agents can see each other ---")
fresh_board()
a1 = join("model-one")
a2 = join("model-two")
check("the second gets its own id", (a1, a2) == ("a1", "a2"), f"{a1}, {a2}")
as_agent(a1)
check("each sees the other as a peer", [p["id"] for p in channel.peers()] == ["a2"])
check("and itself in the full list",
      sorted(r["id"] for r in channel.agents()) == ["a1", "a2"])
as_agent(a2)
check("from the other side too", [p["id"] for p in channel.peers()] == ["a1"])

print("\n--- a message reaches the other agent, and only them ---")
drain(a1, a2)
as_agent(a1)
ok, said = channel.send("who has app.py?")
check("a broadcast is accepted when somebody is here", ok, said)
check("the sender is not handed back their own message",
      channel.take_for_model() == [])
as_agent(a2)
mine = channel.take_for_model()
check("the other agent receives it", [m["text"] for m in mine] == ["who has app.py?"],
      str(mine))
check("it is described as coming from a1",
      channel.describe(mine[0]).startswith("a1 → everyone:"), channel.describe(mine[0]))
check("and it is not delivered to the model twice", channel.take_for_model() == [])
check("but the screen has its own cursor, so it is still printed once",
      [m["text"] for m in channel.take_for_screen()] == ["who has app.py?"])
check("and only once", channel.take_for_screen() == [])

as_agent(a1)
ok, _ = channel.send("just you", to="a2")
as_agent(a2)
direct = channel.take_for_model()
check("a direct message arrives", [m["text"] for m in direct] == ["just you"])
check("addressed to you rather than the room",
      channel.describe(direct[0]) == "a1 → you: just you")

print("\n--- a message with nobody to read it is refused, not swallowed ---")
as_agent(a1)
ok, said = channel.send("hello?", to="a9")
check("a message to an agent that is not here fails", not ok, said)
check("and says who is actually here", "a2" in said, said)
fresh_board()
only = join("alone")
ok, said = channel.send("anyone?")
check("a broadcast into an empty workspace fails", not ok, said)
check("nothing was posted", board()["messages"] == [])

print("\n--- a claimed file cannot be written from another session ---")
fresh_board()
a1, a2 = join("one"), join("two")
shared = path_in_work("shared.py")
with open(shared, "w") as f:
    f.write("original\n")

as_agent(a1)
taken, refused = channel.claim(shared, "rewriting the parser")
check("the first agent takes it", taken == ["shared.py"], str(taken))
check("with nothing refused", refused == [])

as_agent(a2)
conflict = channel.holder(shared)
check("the second agent sees the claim", conflict.get("agent") == "a1", str(conflict))
result = tools.dispatch_tool("write_file", {"filepath": shared, "content": "clobbered\n"})
check("and its write is refused", result.startswith("[System]"), result[:80])
check("the refusal names the holder", "a1" in result, result[:120])
check("and says what they are doing", "rewriting the parser" in result)
check("the file on disk is untouched", open(shared).read() == "original\n")
check("edit_file is refused the same way",
      tools.dispatch_tool("edit_file", {"filepath": shared, "old_content": "original",
                                        "new_content": "x"}).startswith("[System]"))
check("delete_file too",
      tools.dispatch_tool("delete_file", {"filepath": shared}).startswith("[System]"))
check("and copy_file, which is refused on where it would land",
      tools.dispatch_tool("copy_file", {"src": path_in_work("other.py"),
                                        "dst": shared}).startswith("[System]"))

as_agent(a1)
check("the holder is not refused its own claim", channel.holder(shared) == {})
check("and can write it", tools.dispatch_tool(
    "write_file", {"filepath": shared, "content": "mine\n"}).startswith("[Success"))

print("\n--- nothing is half-claimed ---")
one, two = path_in_work("one.py"), path_in_work("two.py")
as_agent(a2)
taken, refused = channel.claim(f"{one}, {shared}, {two}", "a batch")
check("a batch containing somebody else's file takes nothing", taken == [], str(taken))
check("and reports the conflict", [c["path"] for c in refused] == ["shared.py"],
      str(refused))
check("so the others are still free", channel.holder(one) == {} and
      board()["claims"].get("one.py") is None)

print("\n--- a claim is given back ---")
as_agent(a1)
check("release drops it", channel.release(shared) == ["shared.py"])
as_agent(a2)
check("and the other agent may now write", tools.dispatch_tool(
    "write_file", {"filepath": shared, "content": "theirs\n"}).startswith("[Success"))
as_agent(a1)
check("but only the holder can release one",
      channel.release("two.py") == [])

print("\n--- a claim dies with the agent that took it ---")
fresh_board()
a1, a2 = join("one"), join("two")
as_agent(a1)
channel.claim(shared, "mid-edit")
as_agent(a2)
check("held while a1 is alive", channel.holder(shared).get("agent") == "a1")
# a1's terminal is killed: no `leave`, no heartbeat, and a pid that is not there.
data = board()
data["agents"]["a1"].update({"pid": 999999999, "host": "somewhere-else",
                             "seen": time.time() - 10000})
channel._update(lambda b: b.update(data))
check("a crashed agent stops holding anything", channel.holder(shared) == {},
      str(channel.holder(shared)))
check("and the write goes through", tools.dispatch_tool(
    "write_file", {"filepath": shared, "content": "b\n"}).startswith("[Success"))
check("the dead agent is off the list", [r["id"] for r in channel.agents()] == ["a2"])

print("\n--- and it expires on its own ---")
fresh_board()
a1, a2 = join("one"), join("two")
as_agent(a1)
channel.claim(shared, "briefly", seconds=0.4)
as_agent(a2)
check("held at first", channel.holder(shared).get("agent") == "a1")
time.sleep(0.5)
check("gone once it has expired", channel.holder(shared) == {})

print("\n--- writing a file claims it, without being asked to ---")
fresh_board()
a1, a2 = join("one"), join("two")
untouched = path_in_work("auto.py")
as_agent(a1)
check("a successful write takes a claim", tools.dispatch_tool(
    "write_file", {"filepath": untouched, "content": "one\n"}).startswith("[Success"))
as_agent(a2)
held = channel.holder(untouched)
check("so the other agent is stopped", held.get("agent") == "a1", str(held))
check("even though a1 never called claim_files",
      held.get("reason") == "changed it just now", str(held))

fresh_board()
a1, a2 = join("one"), join("two")
as_agent(a1)
missing = os.path.join(WORK, "no-such-dir", "x.py")
failed = tools.dispatch_tool("edit_file", {"filepath": missing, "old_content": "a",
                                           "new_content": "b"})
check("a write that failed claims nothing", not failed.startswith("[Success"), failed[:60])
as_agent(a2)
check("so nothing is held", channel.holder(missing) == {})

print("\n--- the person can overrule a claim, and the model cannot ---")
fresh_board()
a1, a2 = join("one"), join("two")
as_agent(a1)
channel.claim(shared, "gone to lunch")
as_agent(a2)
check("release does not touch somebody else's claim", channel.release(shared) == [])
check("still held", channel.holder(shared).get("agent") == "a1")
check("force_release - what /agents release calls - takes it back",
      channel.force_release(shared) == ["shared.py"])
check("and the write goes through", tools.dispatch_tool(
    "write_file", {"filepath": shared, "content": "c\n"}).startswith("[Success"))

print("\n--- turned off, none of it happens ---")
fresh_board()
a1, a2 = join("one"), join("two")
as_agent(a1)
channel.claim(shared, "holding")
as_agent(a2)
config.CHANNEL_CLAIMS = False
check("claims can be stopped from being enforced", channel.holder(shared) == {})
config.CHANNEL_CLAIMS = True
config.CHANNEL_ENABLED = False
check("with the channel off there are no agents at all", channel.agents() == [])
check("no claim is enforced", channel.holder(shared) == {})
check("nothing is delivered", channel.turn_note() == "")
ok, said = channel.send("hello")
check("and a message says so rather than pretending", not ok, said)
check("the tool says so too, without an [Error] the model would retry",
      tools.dispatch_tool("list_agents", {}).startswith("[System]"))
check("and so does a message with nowhere to go",
      tools.dispatch_tool("send_agent_message",
                          {"message": "anyone?"}).startswith("[System]"))
config.CHANNEL_ENABLED = True

print("\n--- what the model is told, and when ---")
fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
as_agent(a1)
check("a quiet turn adds nothing to the conversation", channel.turn_note() == "")
as_agent(a2)
channel.send("I am taking the tests directory", to="a1")
as_agent(a1)
note = channel.turn_note()
check("a turn with a message does", note.startswith("[Channel]"), note[:40])
check("and carries what was said", "taking the tests directory" in note)
check("and names the tool to answer with", "send_agent_message" in note)
check("the next turn is quiet again", channel.turn_note() == "")

print("\n--- joining and leaving are announced ---")
fresh_board()
a1 = join("one")
as_agent(a1)
check("nobody is told about the first agent to arrive", channel.turn_note() == "")
a2 = join("two")
check("and an agent is not told about its own arrival", channel.turn_note() == "",
      channel.turn_note()[:60])
as_agent(a1)
note = channel.turn_note()
check("but an arrival reaches the agent already here", "a2 joined" in note, note[:60])
as_agent(a2)
channel.take_for_model()
as_agent(a2)
channel.leave()
as_agent(a1)
check("and so does a departure", "a2 left" in channel.turn_note())
check("with their claims gone too", board()["claims"] == {})

print("\n--- several processes writing at once lose nothing ---")
fresh_board()
here = join("host")
WRITERS = 6
script = (
    "import os, sys\n"
    f"os.environ['{paths.ENV_VAR}'] = {HOME!r}\n"
    f"sys.path.insert(0, {ROOT!r})\n"
    "from simple_harness import config\n"
    "config.MCP_ENABLED = False\n"
    "from simple_harness import channel\n"
    f"channel._workspace = {os.path.realpath(WORK)!r}\n"
    "me = channel.join('writer ' + sys.argv[1])\n"
    "channel.send('message from ' + sys.argv[1] + ' as ' + me)\n"
    # Held open so that all of them are on the board at once. Without this each
    # one leaves before the next joins, and every one of them is legitimately
    # handed the same free id - which would prove nothing about the race.
    "import time; time.sleep(2.5)\n"
)
runners = [subprocess.Popen((sys.executable, "-c", script, str(n)),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
           for n in range(WRITERS)]
for runner in runners:
    runner.wait(timeout=60)
sent = [m for m in board()["messages"] if m["text"].startswith("message from ")]
check(f"all {WRITERS} messages survived the concurrent writes",
      len(sent) == WRITERS, f"{len(sent)} of {WRITERS}")
check("no two of them were handed the same agent id",
      len({m["from"] for m in sent}) == WRITERS,
      str(sorted(m["from"] for m in sent)))
check("and each one left the board on its way out",
      [r["id"] for r in channel.agents()] == [here],
      str([r["id"] for r in channel.agents()]))
check("the board is still valid JSON", isinstance(
    json.load(open(channel.board_path(), encoding="utf-8")), dict))
check("and no lock was left behind",
      not os.path.exists(channel.board_path() + ".lock"))

print("\n--- a damaged board is not a broken harness ---")
with open(channel.board_path(), "w") as f:
    f.write("{ this is not json")
check("it reads as an empty one", channel.read_board()["agents"] == {})
check("a claim check on it refuses nothing", channel.holder(shared) == {})
os.unlink(channel.board_path())
check("a missing one reads as empty too", channel.read_board()["messages"] == [])

channel.reset()
shutil.rmtree(HOME, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("agent channel checks passed")
