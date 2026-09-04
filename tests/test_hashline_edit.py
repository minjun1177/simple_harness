"""Editing a file by naming its lines instead of quoting them.

`read_file` has always returned `50:1f|    print(answer)`, and `edit_file` has
always thrown that prefix away - so to change one line the model had to
reproduce it exactly: every space of indentation, every quote, every backslash.
That is the thing a small model gets wrong most often, and when the line
appeared twice in the file it could not be done at all, because the snippet was
ambiguous and the edit was refused.

An `old_content` made of those prefixes names the lines directly. The checks
here are in two halves, and the second half matters more: that the anchors
reach the right lines, and that an anchor which no longer describes the file
is *refused* rather than applied a few lines off - which would be a silent
wrong edit, the worst thing this tool can do.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from simple_harness import paths

HOME = tempfile.mkdtemp(prefix="hashline-home-")
os.environ[paths.ENV_VAR] = HOME

from simple_harness import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True
config.GIT_AUTO_COMMIT = False
config.CHANNEL_ENABLED = False             # the board is tested in test_channel.py

from simple_harness import tools           # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


WORK = tempfile.mkdtemp(prefix="hashline-work-")

# Lines 3 and 8 are identical on purpose: that is the case the old text match
# could not do at all.
SAMPLE = ('def greet(name):\n'
          '    answer = "hi " + name\n'
          '    print(answer)\n'
          '    return answer\n'
          '\n'
          'def farewell(name):\n'
          '    answer = "bye " + name\n'
          '    print(answer)\n'
          '    return answer\n')


def sample(name="demo.py", text=SAMPLE):
    path = os.path.join(WORK, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def edit(path, old, new):
    return tools.handle_edit_file(path, old, new)


def line_of(path, number):
    return read(path).split("\n")[number - 1]


print("--- the whole edit is one row: the anchor, and what the line becomes ---")
path = sample()
result = edit(path, "", '3:9b|    print("said:", answer)')
check("new_content alone, with no old_content at all, edits the line",
      result.startswith("[Success"), result)
check("and it is the right line", line_of(path, 3) == '    print("said:", answer)',
      line_of(path, 3))
check("the identical line further down is untouched",
      line_of(path, 8) == "    print(answer)", line_of(path, 8))
check("nothing moved, and the result says so",
      len(read(path).split("\n")) == 10 and "nothing below moved" in result, result)

path = sample()
result = edit(path, "", '2:cd|    answer = f"hi {name}"\n8:9b|    pass')
check("several rows change several lines", result.startswith("[Success"), result)
check("which need not be next to each other",
      line_of(path, 2) == '    answer = f"hi {name}"' and line_of(path, 8) == "    pass",
      f"{line_of(path, 2)!r} {line_of(path, 8)!r}")
check("and the lines between them are untouched",
      line_of(path, 3) == "    print(answer)" and line_of(path, 5) == "")
check("the result names every line it changed", "lines 2, 8 replaced" in result, result)

path = sample()
same_line = edit(path, "", "3:9b|a\n3:9b|b")
check("naming one line twice is refused", same_line.startswith("[Error]"), same_line)
check("because a line can only become one thing", "twice" in same_line, same_line)
check("and nothing is written", read(path) == SAMPLE)

print("\n--- the hash is the only check this form has, so it is not waived ---")
path = sample()
stale = edit(path, "", "3:0e|    pass")
check("a hash that does not match refuses the edit", stale.startswith("[Error]"), stale)
check("the file is untouched", read(path) == SAMPLE)
check("and the refusal says what is on that line now",
      "3:9b|    print(answer)" in stale, stale)
check("saying why nothing was written",
      "overwrite something you have not read" in stale, stale)

path = sample()
missing = edit(path, "", "99:9b|    pass")
check("a line the file does not have is refused", missing.startswith("[Error]"), missing)
check("and it says how many lines there are", "10 lines" in missing, missing)

path = sample()
clash = edit(path, "1:eb", "3:9b|    pass")
check("an old_content naming other lines is refused rather than half-obeyed",
      clash.startswith("[Error]"), clash)
check("and says which lines new_content meant", "line(s) 3" in clash, clash)
check("nothing is written", read(path) == SAMPLE)
check("the same anchors in both is accepted",
      edit(path, "3:9b", "3:9b|    pass").startswith("[Success"))
check("and edits that line", line_of(path, 3) == "    pass")

path = sample()
check("an indented row is still read as an anchor, not written literally",
      edit(path, "", "    3:9b|    pass").startswith("[Success"))
check("so the file never gets an anchor written into it",
      "3:9b|" not in read(path) and line_of(path, 3) == "    pass", line_of(path, 3))

print("\n--- the longer form is for what the short one cannot do ---")
path = sample()
listing = tools.handle_read_file(path).split("\n")
check("read_file still prints the anchor it is asked for",
      listing[2] == "3:9b|    print(answer)", listing[2])

result = edit(path, "3:9b", '    print("said:", answer)')
check("an anchor in old_content edits that line", result.startswith("[Success"), result)
check("and it is the right line", line_of(path, 3) == '    print("said:", answer)',
      line_of(path, 3))
check("the identical line further down is untouched",
      line_of(path, 8) == "    print(answer)", line_of(path, 8))
check("nothing else moved", len(read(path).split("\n")) == 10)

path = sample()
result = edit(path, "3:9b|    print(answer)", '    print("said:", answer)')
check("the whole row copied out of the listing works too",
      result.startswith("[Success") and line_of(path, 3) == '    print("said:", answer)',
      result)

print("\n--- which is the case the text match could never do ---")
path = sample()
duplicate = edit(path, "    print(answer)", "    pass")
check("quoting a line that appears twice is still refused",
      duplicate.startswith("[Error]") and "2 times" in duplicate, duplicate)
check("and the refusal now says what to do instead", "50:1f" in duplicate, duplicate)
check("the file was not touched", read(path) == SAMPLE)
check("by anchor the same edit lands", edit(path, "8:9b", "    pass").startswith("[Success"))
check("on the second one", line_of(path, 8) == "    pass" and
      line_of(path, 3) == "    print(answer)")

print("\n--- an anchor that no longer describes the file is refused ---")
path = sample()
stale = edit(path, "3:0e", "    pass")
check("a hash that does not match refuses the edit", stale.startswith("[Error]"), stale)
check("the file is untouched", read(path) == SAMPLE)
check("and the refusal says what is actually on that line",
      "3:9b|    print(answer)" in stale, stale)
check("and what to do about it", "read_file" in stale)

# The real thing this protects against: the file moved under the model.
path = sample()
edit(path, "1:eb", "def greet(name):\n    # a new line, everything below shifts")
after = edit(path, "3:9b", "    pass")
check("an anchor taken before an edit does not fire after it",
      after.startswith("[Error]"), after)
check("nothing was written at the wrong place",
      "print(answer)" in read(path) and "pass" not in read(path))

print("\n--- but a mistyped hash beside the right line is forgiven ---")
path = sample()
# Two hand-copied hex characters are far easier to get wrong than the line
# itself, so the line is the stronger evidence when the two disagree.
typo = edit(path, "3:zz|    print(answer)", "    pass")
check("a malformed hash is not an anchor at all - it falls through to text",
      typo.startswith("[Error]"), typo[:60])
typo = edit(path, "3:ab|    print(answer)", "    pass")
check("a wrong-but-well-formed hash beside the exact line is accepted",
      typo.startswith("[Success"), typo)
check("and it edited that line", line_of(path, 3) == "    pass")

path = sample()
both_wrong = edit(path, "3:ab|    print(nothing)", "    pass")
check("a wrong hash beside the wrong text is refused",
      both_wrong.startswith("[Error]"), both_wrong[:70])
check("the file is untouched", read(path) == SAMPLE)

print("\n--- several lines at once ---")
path = sample()
result = edit(path, "6:ca-9:96", 'def farewell(name):\n    return "bye " + name')
check("a span replaces every line in it", result.startswith("[Success"), result)
check("with the new text", read(path).split("\n")[5:7] ==
      ["def farewell(name):", '    return "bye " + name'], str(read(path).split("\n")))
check("and says the lines below have moved", "moved" in result, result)

path = sample()
result = edit(path, "3:9b\n4:96", "    return None")
check("a run of anchors, one per line, does the same",
      result.startswith("[Success"), result)
check("replacing both of them", read(path).split("\n")[2] == "    return None" and
      read(path).split("\n")[3] == "")

path = sample()
result = edit(path, "3:9b\n4:96", "    print(answer)\n    return answer")
check("a replacement of the same length does not claim anything moved",
      result.startswith("[Success") and "moved" not in result, result)

print("\n--- anchors that do not describe a run of lines are refused ---")
path = sample()
gap = edit(path, "3:9b\n5:d4", "    pass")
check("a gap between anchors is refused", gap.startswith("[Error]"), gap)
check("and the span form is offered instead", "3:9b-5:d4" in gap, gap)
check("the file is untouched", read(path) == SAMPLE)

backwards = edit(path, "9:96-3:9b", "    pass")
check("a span written backwards is refused", backwards.startswith("[Error]"), backwards)

missing = edit(path, "99:9b", "    pass")
check("a line the file does not have is refused", missing.startswith("[Error]"), missing)
check("and the refusal says how many lines there are", "10 lines" in missing, missing)
check("the file is still untouched", read(path) == SAMPLE)

print("\n--- deleting, and the shape of the file afterwards ---")
path = sample()
result = edit(path, "4:96\n5:d4", "")
check("an empty replacement removes the lines", result.startswith("[Success"), result)
check("without leaving a blank one behind",
      read(path).split("\n")[3] == "def farewell(name):", str(read(path).split("\n")[:5]))
check("and the rest of the file is intact",
      read(path) == ('def greet(name):\n'
                     '    answer = "hi " + name\n'
                     '    print(answer)\n'
                     'def farewell(name):\n'
                     '    answer = "bye " + name\n'
                     '    print(answer)\n'
                     '    return answer\n'), repr(read(path)))

path = sample()
edit(path, "1:eb", "def greet(name):")
check("a file that ended in a newline still does", read(path).endswith("answer\n"))
check("and has not grown a line", len(read(path).split("\n")) == 10)

print("\n--- everything that is not an anchor still behaves as it did ---")
path = sample()
result = edit(path, '    answer = "hi " + name', '    answer = f"hi {name}"')
check("an ordinary snippet is still matched as text", result.startswith("[Success"), result)
check("and replaced", line_of(path, 2) == '    answer = f"hi {name}"')

path = sample()
# One ordinary line makes the whole snippet text - anchors have to be certain
# before they take over. `_strip_hashlines` then declines to strip a snippet
# that is only half-prefixed (its 0.8 ratio, unchanged), so this is refused
# rather than half-understood. It was refused before this feature too; what is
# new is that the refusal says what to do instead.
mixed = edit(path, '3:9b|    print(answer)\n    return answer', "    pass")
check("a snippet that is only partly anchors is not treated as anchors",
      mixed.startswith("[Error]"), mixed[:60])
check("and it is refused rather than half-stripped", read(path) == SAMPLE)
check("with the refusal pointing at the anchor form", "anchor" in mixed, mixed)
check("while every row being an anchor does take over",
      edit(path, '3:9b|    print(answer)\n4:96|    return answer', "    pass"
           ).startswith("[Success"))
check("replacing exactly those lines",
      read(path).split("\n")[2:4] == ["    pass", ""], str(read(path).split("\n")[:5]))

path = sample()
gone = edit(path, "    print(nothing_like_this)", "    pass")
check("a snippet that is not there is still refused", gone.startswith("[Error]"), gone)
check("and now says the anchor is the way round it", "anchor" in gone, gone)

path = sample()
result = edit(path, "3:9b", "3:9b|    print(answer, flush=True)")
check("hashline prefixes in new_content are still stripped",
      line_of(path, 3) == "    print(answer, flush=True)", line_of(path, 3))

path = sample()
empty = edit(path, "", "    pass")
check("replacement text with no anchors and no old_content is refused",
      empty.startswith("[Error]"), empty)
check("rather than matched against the whole file", read(path) == SAMPLE)
check("and it says both ways of naming the lines", "50:1f|" in empty, empty)

print("\n--- the user still decides ---")
path = sample()
approvals = []
real_prompt = tools._approval_prompt


def refuse(title, details, rule=""):
    approvals.append(dict(details))
    return False


tools._approval_prompt = refuse
try:
    denied = edit(path, "3:9b", "    pass")
finally:
    tools._approval_prompt = real_prompt
check("an anchored edit is still put to the user", len(approvals) == 1)
check("and shows the real lines, not the anchor",
      approvals[0].get("from") == "    print(answer)", str(approvals[0]))
check("saying which lines they are", approvals[0].get("replacing") == "line 3",
      str(approvals[0]))
check("declining refuses it", denied.startswith("[System]"), denied)
check("and writes nothing", read(path) == SAMPLE)

approvals.clear()
tools._approval_prompt = refuse
try:
    denied = edit(path, "", "3:9b|    pass")
finally:
    tools._approval_prompt = real_prompt
check("a one-row edit is put to the user too", len(approvals) == 1)
check("showing the line before and after",
      approvals[0].get("line 3") == "    print(answer)  →      pass", str(approvals[0]))
check("declining refuses it", denied.startswith("[System]") and read(path) == SAMPLE)

print("\n--- a missing file is a missing file, whichever form is used ---")
absent = os.path.join(WORK, "not-here.py")
check("by one-row anchor", edit(absent, "", "1:aa|x").startswith("[Error]"))
check("by anchor", edit(absent, "1:aa", "x").startswith("[Error]"))
check("by text", edit(absent, "x", "y").startswith("[Error]"))

shutil.rmtree(HOME, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("hashline edit checks passed")
