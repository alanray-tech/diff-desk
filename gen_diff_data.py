"""Collect a git range as reviewable data: whole branch and per commit, for any repository and any base."""

import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import threading
import time
import tokenize


def github():
    """The command that talks to GitHub. Overridable so a test can answer for it without a network or a login.

    Split as the platform's own shell would: POSIX splitting reads the backslashes of a Windows path as escapes and
    drops them, so there the words are split as they stand and only their quotes taken off.
    """
    said = os.environ.get("DIFF_DESK_GH", "gh")
    if os.name != "nt":
        return shlex.split(said)
    words = shlex.split(said, posix=False)
    return [word[1:-1] if len(word) > 1 and word[0] == word[-1] == '"' else word for word in words]


def home():
    """Where the desk keeps its payload, its page and the comments it has collected."""
    where = pathlib.Path(os.environ.get("DIFF_DESK_HOME", pathlib.Path.home() / ".claude" / "diff-desk"))
    where.mkdir(parents=True, exist_ok=True)
    return where


def run(root, *args):
    """Ask git something. A command that fails answers with nothing, which every caller reads as absence."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=root, check=False
    ).stdout


# What gh says when the call never left this machine: nothing resolved, nothing connected, no handshake made. GitHub
# cannot have acted on any of these, so repeating them costs nothing whatever the call was.
UNREACHED = ("error connecting", "no such host", "connection refused", "tls handshake")
# What gh says when the answer went missing on the way back, which says nothing about what GitHub did with the call
# first: it may have received it, acted on it, and lost only the answer.
UNANSWERED = ("connection reset", "timeout", "timed out", "deadline exceeded")
# What GitHub itself answered, which says the same however often it is asked.
ANSWERED = ("http 401", "http 403", "http 404", "http 422", "not found", "bad credentials")


def worth_asking_again(told, *, repeatable):
    """Whether a failed gh call is worth making again - and safe to.

    Judged on what gh said, since its exit status is the same whatever went wrong. A call that never left the machine is
    always worth repeating. A call whose answer went missing is repeated only when the caller says asking twice cannot
    be told from asking once: GitHub may have acted on it already, and a second reply or a second review landing on a
    pull request is a mess no failure justifies. A refusal is an answer, and an answer does not change for being asked
    again.
    """
    said = " ".join(told.lower().split())
    if any(phrase in said for phrase in ANSWERED):
        return False
    if any(phrase in said for phrase in UNREACHED):
        return True
    return repeatable and any(phrase in said for phrase in UNANSWERED)


def gh(*args, repeatable, given=None, cwd=None, budget=120):
    """Ask GitHub something, and ask again when what came back was a stumble rather than an answer.

    `repeatable` is the caller's word on whether asking twice can be told from asking once. A read, a resolution and a
    deletion can be repeated whatever went wrong, since asking again is told the same thing. Anything that posts - a
    reply, a review - can only be repeated when the call provably never left the machine.

    Three attempts a fraction of a second apart, all of them inside one budget: a reader is waiting in front of the
    page, so the whole call answers within it however many attempts it takes, and the last attempt is the one that
    reports. Nothing is said to the reader while an earlier attempt is being made good.

    A call that never answers within its budget is an answer too, a failed one saying so, rather than an exception
    thrown at whoever is collecting a page.
    """
    words = [*github(), *args]
    told = None
    ends = time.monotonic() + budget
    for pause in (0.4, 1.2, None):
        # Never nothing, so a budget already spent still leaves the attempt that reports a moment to answer in.
        left = max(1.0, ends - time.monotonic())
        try:
            told = subprocess.run(
                words,
                input=given,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
                timeout=left,
                check=False,
            )
        except subprocess.TimeoutExpired:
            told = subprocess.CompletedProcess(words, 1, "", f"no answer from GitHub within {budget:g}s")
            print(f"gh gave no answer within {budget:g}s", flush=True)
            break
        except OSError as error:
            told = subprocess.CompletedProcess(words, 127, "", f"cannot run {words[0]}: {error}")
            print(f"gh could not be run: {error}", flush=True)
            break
        if told.returncode == 0 or pause is None:
            break
        if not worth_asking_again(told.stderr or told.stdout, repeatable=repeatable):
            break
        blip = " ".join((told.stderr or told.stdout).split())[:120]
        print(f"gh stumbled ({blip}); asking again", flush=True)
        time.sleep(pause)
    return told


# What GitHub last answered, filed by question, for the life of the desk: the slug a remote's name resolves to, the
# open pull requests of a repository, who the reader is. Everything that decorates a diff is read from here and asked
# again in the background (see `recalled`), so collecting a page never waits on GitHub. The pull request each branch
# is opened as outlives the desk (see `remembered_pulls` in serve_diff), the rest is asked again by the next one.
REMEMBERED = {}
# The questions out right now, and the ones asked again while they were out, which are asked once more as they land:
# a question is never out twice, and nobody who asked is answered with older news than their asking. Both are changed
# under the one lock, so a question landing and one arriving at the same moment never lose each other.
ASKING = set()
WANTED = set()
ASKING_LOCK = threading.Lock()
# The local branch each pull request served by number is read from, filed by number (see `local_head`), so the stamp a
# page polls follows the commits made there as well as the head fetched from GitHub.
LOCAL_HEADS = {}
# Called with the key of every answer that changed what is remembered, which is how a desk learns to decorate what it
# serves again (see serve_diff).
LISTENERS = []


def answered(key, ask, args):
    """Ask GitHub one question, file the answer when there is one and it is news, and ask again while it is wanted."""
    while True:
        told = ask(*args)
        if told is not None and told != REMEMBERED.get(key):
            REMEMBERED[key] = told
            for listener in LISTENERS:
                listener(key)
        with ASKING_LOCK:
            if key not in WANTED:
                ASKING.discard(key)
                return
            WANTED.discard(key)


def recalled(key, ask, *args):
    """What GitHub last answered to `ask(*args)`, filed under `key`, while it is asked again in the background.

    The question is asked over, once it has landed when it is out already, and what is remembered is returned at once:
    the last answer, or None where GitHub has never answered. `ask` answers None for no answer, which leaves what is
    remembered standing. Nothing here waits on GitHub: an answer lands in its own time and the listeners are told (see
    LISTENERS).
    """
    with ASKING_LOCK:
        if key in ASKING:
            WANTED.add(key)
        else:
            ASKING.add(key)
            threading.Thread(target=answered, args=(key, ask, args), daemon=True).start()
    return REMEMBERED.get(key)


def slug_of(named):
    """The slug GitHub files a repository under, followed through renames, or None when it does not answer."""
    told = gh("api", f"repos/{named}", "--jq", ".full_name", repeatable=True, budget=10)
    return (told.stdout.strip() or None) if told.returncode == 0 else None


def canonical_repo(root):
    """The slug pull requests live under, followed through renames so the API does not answer with a redirect.

    The rename is followed in the background and remembered (see `recalled`), and a name GitHub has never answered for
    is taken as it stands, since a page must not lose its links to the pull requests over a failed request. Which
    remotes a repository has is read from disk each time, so one added or removed under the desk is noticed.
    """
    for remote in ("upstream", "origin"):
        url = run(root, "remote", "get-url", remote).strip()
        if not url:
            continue
        match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
        if not match:
            continue
        named = match.group(1)
        return recalled(f"slug:{named}", slug_of, named) or named
    return ""


PULL = re.compile(r"^(?:#|pr[/-])?(\d+)$", re.IGNORECASE)


def pull_number(ref):
    """The pull request a ref names, when it names one rather than a branch."""
    match = PULL.match(str(ref).strip())
    return int(match.group(1)) if match else None


def fetch_pull(root, upstream, number):
    """Bring a pull request's head into a local ref, and describe it.

    Fetched by number through the upstream repository, so neither the fork it lives on nor the branch name it uses has
    to be known, and a head force-pushed since the last look is picked up.

    A head fetched earlier stays reviewable while GitHub is unreachable: whatever cannot be read or fetched falls back
    to what is already on disk, and only a pull request with nothing local behind it is refused.
    """
    local = f"refs/diffdesk/pull/{number}"
    held = run(root, "rev-parse", "--verify", "--quiet", local).strip()
    fetched = {"number": number, "title": f"#{number}", "url": "", "headRefName": local}
    if not upstream:
        if held:
            return local, fetched
        raise RuntimeError(f"#{number} needs a GitHub remote to resolve against, and this repository has none")
    wanted = "number,title,url,headRefName,baseRefName"
    seen = gh("pr", "view", str(number), "--repo", upstream, "--json", wanted, repeatable=True, cwd=root, budget=60)
    read = None
    if seen.returncode == 0:
        try:
            read = json.loads(seen.stdout)
        except json.JSONDecodeError:
            # An answer that is not one is no answer: treated as a failed read rather than crashing the collection.
            read = None
    if read is not None:
        request = read
    elif held:
        print(f"#{number} could not be read from {upstream}; showing the head fetched earlier", flush=True)
        request = fetched
    else:
        told = " ".join((seen.stderr or seen.stdout or "no answer").split())[:200]
        raise RuntimeError(f"#{number} could not be read from {upstream}: {told}")
    brought = subprocess.run(
        ["git", "fetch", "--quiet", f"https://github.com/{upstream}.git", f"+refs/pull/{number}/head:{local}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=root,
        timeout=300,
        check=False,
    )
    if brought.returncode != 0:
        if not held:
            raise RuntimeError(f"#{number} could not be fetched: {' '.join(brought.stderr.split())[:200]}")
        print(f"#{number} could not be fetched; showing the head fetched earlier", flush=True)
    return local, request


def local_head(root, ref, request):
    """The ref a fetched pull request is read from: the local branch it is worked on, when that builds on the fetch.

    A pull request is fetched as GitHub holds it, so the commits made on its branch and not pushed yet would stay out of
    the review until they are. The local branch named after the head of the pull request holds everything the fetch
    does and the work waiting to be pushed whenever the fetched head is one of its ancestors, and it is read instead.
    """
    name = request.get("headRefName") or ""
    if not name or not run(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").strip():
        return ref
    builds = subprocess.run(["git", "merge-base", "--is-ancestor", ref, f"refs/heads/{name}"], cwd=root, check=False)
    return name if builds.returncode == 0 else ref


def desk_version():
    """What the tool on disk is, as a digest of the code it is made of, so a page can tell it is behind the tool.

    A page holds the script it was rendered with, and serving fast-forwards the desk and restarts into whatever has
    been published since. Nothing about the review changes when that happens, so the branch stamp says nothing, and
    the reader is left pressing a page that was fixed hours ago.
    """
    here = pathlib.Path(__file__).parent
    said = hashlib.sha1()
    for name in ("diff_desk_template.html", "gen_diff_data.py", "serve_diff.py", "desk.py", "semantic.py"):
        held = here / name
        said.update(held.read_bytes() if held.exists() else b"")
    return said.hexdigest()[:12]


# What the desk answering is running, taken once as it starts. The files it is made of move on under it - the tool is
# updated while a desk is up - and the page it renders comes from those newer files while the endpoints answering that
# page are the ones this process started with. A page asking for something this desk has never had is the shape of it,
# so the two are kept apart and compared rather than both read off the disk, where they always agree.
RUNNING = desk_version()


def render_page(template, payload):
    """The page as served: the payload and the grammar inlined, stamped with the moment it was built.

    The grammar is the vendored highlight.js, written into the page rather than fetched by it: the desk serves one file
    and a review reads the same with the network gone.
    """
    body = json.dumps(payload, separators=(",", ":")).replace("</script", "<\\/script")
    stamp = time.strftime("built %H:%M:%S")
    grammar = (pathlib.Path(__file__).parent / "vendor" / "highlight.min.js").read_text(encoding="utf-8")
    return template.replace("__DIFF_DATA__", body).replace("__HIGHLIGHT__", grammar).replace("__BUILD__", stamp)


# The strings of every text tokenized lately, filed by a digest of the text (see 'string_marks'). A branch reads the
# same file at many revisions, and a page load collects them all again, so most texts asked about have been read.
MARKED = {}
MARKED_KEPT = 4096
MARKED_LOCK = threading.Lock()


def string_marks(text):
    """The tokens that open, close or are strings in a text, from one tokenizing of all of it (see 'string_opening').

    In the order the tokenizer gives them: ("s", start, end, delimiter) for a string, ("f", start, 0, opening) and
    ("e", start, 0, "") for where an f-string opens and ends. Beside them, the line of the last token read when the
    tokenizer gave up, or None when it read the text through.
    """
    key = hashlib.sha1(text.encode("utf-8", "surrogatepass")).digest()
    with MARKED_LOCK:
        held = MARKED.pop(key, None)
        if held is not None:
            MARKED[key] = held
            return held
    marks = []
    last = 0
    broken = None
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            last = token.start[0]
            if token.type == tokenize.STRING:
                said = re.match(r"[rRbBuUfF]*(\"\"\"|'''|\"|')", token.string).group(0)
                marks.append(("s", last, token.end[0], said))
            elif token.type == tokenize.FSTRING_START:
                marks.append(("f", last, 0, token.string))
            elif token.type == tokenize.FSTRING_END:
                marks.append(("e", last, 0, ""))
    except (tokenize.TokenError, SyntaxError):
        broken = last
    held = (marks, broken)
    with MARKED_LOCK:
        MARKED[key] = held
        while len(MARKED) > MARKED_KEPT:
            MARKED.pop(next(iter(MARKED)))
    return held


def string_opening(text, line_no):
    """The delimiter opening the Python string a line stands inside, or "" for a line standing in code.

    A hunk is painted on its own and starts wherever the diff cut it, so a line inside a docstring opened above the hunk
    reads as code. Handing the painter the delimiter puts the line back inside its string.

    Read as the tokenizer reads the text up to that line: a text it gives up on before reaching the line says nothing.
    """
    if line_no < 1:
        return ""
    marks, broken = string_marks(text)
    opened = None
    for kind, start, end, said in marks:
        if start >= line_no:
            break
        if kind == "s" and line_no <= end:
            return said
        if kind == "f":
            opened = said
        elif kind == "e":
            opened = None
    if broken is not None and broken < line_no:
        return ""
    return opened or ""


def as_text(raw):
    """Bytes git gave for a file, read as `run` reads what git says: UTF-8, and every line ending a newline."""
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def file_text(root, rev, path):
    """What a file reads as at a revision, the working tree standing for an empty revision, or "" where it has none."""
    if rev:
        return run(root, "show", f"{rev}:{path}")
    file = pathlib.Path(root) / path
    return file.read_text(encoding="utf-8", errors="replace") if file.is_file() else ""


def file_texts(root, wanted):
    """What each (rev, path) reads as (see 'file_text'), every committed one read by a single git process.

    Starting git costs more than reading a file through it, the more so on Windows, and a branch names hundreds.
    """
    named = sorted({(rev, path) for rev, path in wanted if rev})
    texts = {(rev, path): file_text(root, rev, path) for rev, path in wanted if not rev}
    if not named:
        return texts
    asked = "".join(f"{rev}:{path}\n" for rev, path in named).encode("utf-8")
    told = subprocess.run(
        ["git", "cat-file", "--batch"], input=asked, capture_output=True, cwd=root, check=False
    ).stdout
    at = 0
    for name in named:
        texts[name] = ""
        end = told.find(b"\n", at)
        if end < 0:
            continue
        # "<oid> <type> <size>", or the name asked for followed by "missing" or "ambiguous".
        header = told[at:end].rsplit(b" ", 2)
        at = end + 1
        if not header[-1].isdigit():
            continue
        size = int(header[-1])
        if header[-2] == b"blob":
            texts[name] = as_text(told[at : at + size])
        at += size + 1
    return texts


def mark_strings(root, jobs):
    """Note on each hunk of a Python file the string delimiter its old and its new side start inside.

    `jobs` holds (files, old_rev, new_rev) for every diff collected, so the texts all of them read are fetched at once.
    The note is a fifth member of the hunk header row (see 'string_opening' for what it holds and why).
    """
    marked = [
        (entry, revs)
        for files, *revs in jobs
        for entry in files
        if not entry["binary"] and entry["path"].endswith(".py") and any(row[0] == "h" for row in entry["lines"])
    ]
    texts = file_texts(root, {(rev, entry["path"]) for entry, revs in marked for rev in revs})
    for entry, revs in marked:
        sides = [texts[(rev, entry["path"])] for rev in revs]
        for row in entry["lines"]:
            if row[0] == "h":
                row.append([string_opening(text, row[1 + side]) for side, text in enumerate(sides)])


def parse(diff):
    files = []
    current = None
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.*) b/(.*)", line)
            path = match.group(2) if match else line
            current = {"path": path, "added": 0, "removed": 0, "lines": [], "binary": False}
            files.append(current)
            continue
        if current is None:
            continue
        if line.startswith("Binary files"):
            current["binary"] = True
            continue
        if line.startswith(("index ", "--- ", "+++ ", "old mode", "new mode", "similarity index", "rename ")):
            continue
        if line.startswith("new file mode"):
            current["state"] = "added"
            continue
        if line.startswith("deleted file mode"):
            current["state"] = "deleted"
            continue
        if line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", line)
            old_no = int(match.group(1)) if match else 0
            new_no = int(match.group(2)) if match else 0
            current["lines"].append(["h", old_no, new_no, match.group(3).strip() if match else line])
            current["_old"], current["_new"] = old_no, new_no
            continue
        kind = {"+": "a", "-": "d"}.get(line[:1], "c")
        if kind == "a":
            current["lines"].append(["a", 0, current.get("_new", 0), line[1:]])
            current["_new"] = current.get("_new", 0) + 1
            current["added"] += 1
        elif kind == "d":
            current["lines"].append(["d", current.get("_old", 0), 0, line[1:]])
            current["_old"] = current.get("_old", 0) + 1
            current["removed"] += 1
        else:
            current["lines"].append(["c", current.get("_old", 0), current.get("_new", 0), line[1:]])
            current["_old"] = current.get("_old", 0) + 1
            current["_new"] = current.get("_new", 0) + 1
    for entry in files:
        entry.pop("_old", None)
        entry.pop("_new", None)
        # A digest of the hunks, so a page can tell a file it already reviewed from one that moved under it.
        body = "\n".join("".join(str(part) for part in line[:4]) for line in entry["lines"])
        entry["digest"] = hashlib.sha1(body.encode()).hexdigest()[:12]
    return files


def open_pulls(root, upstream):
    """Every open pull request of a repository, keyed by the ref it is opened from, or None when GitHub does not answer.

    One listing, matched locally: asking per ref costs a request each and a single hiccup silently drops that branch's
    link.
    """
    listing = ("pr", "list", "--repo", upstream, "--state", "open", "--limit", "200")
    told = gh(*listing, "--json", "number,url,title,headRefName", repeatable=True, cwd=root, budget=15)
    if told.returncode != 0:
        return None
    try:
        rows = json.loads(told.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return {row["headRefName"]: row for row in rows}


def pull_requests(root, upstream):
    """Every open pull request as GitHub last listed them, keyed by the ref it is opened from (see `recalled`)."""
    return recalled(f"pulls:{upstream}", open_pulls, root, upstream) or {}


def ahead_refs(root, base):
    """Every local branch holding at least one commit the base does not, newest tip first."""
    rows = []
    for name in run(root, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads").split():
        count = run(root, "rev-list", "--count", f"{base}..{name}").strip()
        if count not in ("", "0"):
            rows.append({"ref": name, "ahead": int(count)})
    return rows


def stamp(root, base, refs):
    """What the diffs of these refs are built from, in one string: their tips, the base, and the work on disk.

    The work on disk is taken as the content of the change, not as the list of files carrying it: an edit inside a file
    that was already modified - a rename swept through it, a comment rewritten - leaves `git status` saying exactly what
    it said before, and a page comparing that would believe it was up to date while showing the previous diff.

    Cheap enough to ask for every few seconds, which is what lets a page notice the branch has moved on without
    collecting the diffs and rebuilding itself around them. The branch checked out is part of it too: the one checked
    out is read from disk and the others from their refs, so a checkout changes the diffs while every tip stands.
    """
    root = str(pathlib.Path(root).expanduser())
    heads = [LOCAL_HEADS.get(pull_number(ref), ref) for ref in refs]
    marks = [run(root, "rev-parse", ref).strip() for ref in (base, *refs, *heads)]
    marks.append(run(root, "rev-parse", "--abbrev-ref", "HEAD").strip())
    marks.append(run(root, "status", "--porcelain").strip())
    marks.append(run(root, "diff", "HEAD").strip())
    return hashlib.sha1("\x1f".join(marks).encode()).hexdigest()[:16]


def login():
    """The login GitHub takes the reader for, or None when it does not answer."""
    done = gh("api", "user", "--jq", ".login", repeatable=True, budget=10)
    return (done.stdout.strip() or None) if done.returncode == 0 else None


def viewer():
    """Who GitHub takes the reader for, so their own words on a pull request read as theirs rather than as a login.

    Nothing is owed if there is nobody to ask: a desk served without `gh` reads every login as a login. Asked in the
    background and remembered (see `recalled`).
    """
    return recalled("viewer", login) or ""


def decorate(payload):
    """Read what GitHub adds to a payload off what it last answered, and say whether any of it changed.

    The slug pull requests live under, who the reader is, and the pull request each branch is opened as. A pull request
    served by number keeps what it was fetched as. The whole of it is stamped into `decor`, which is what a page
    compares to learn that GitHub has answered since the page was built.
    """
    root = payload["root"]
    upstream = canonical_repo(root)
    requests = pull_requests(root, upstream) if upstream else {}
    payload["upstream"] = upstream
    payload["viewer"] = viewer()
    for branch in payload["branches"]:
        if not branch["ref"].startswith("refs/diffdesk/pull/"):
            branch["pr"] = requests.get(branch["ref"])
    before = payload.get("decor")
    said = [upstream, payload["viewer"], *[(branch["ref"], branch["pr"]) for branch in payload["branches"]]]
    payload["decor"] = hashlib.sha1(json.dumps(said, sort_keys=True).encode()).hexdigest()[:16]
    return payload["decor"] != before


def collect(root, base, refs):
    """The whole reviewable payload: one entry per ref, each with its per-commit breakdown.

    Read from git, and decorated with what GitHub last answered (see `decorate`), so it never waits on GitHub.
    """
    root = str(pathlib.Path(root).expanduser())
    upstream = canonical_repo(root)
    current = run(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    refs = list(refs) or [row["ref"] for row in ahead_refs(root, base)]
    data = {
        "root": root,
        "name": pathlib.Path(run(root, "rev-parse", "--show-toplevel").strip() or root).name,
        "baseRef": base,
        "base": run(root, "rev-parse", "--short", base).strip(),
        # The refs the stamp is taken over, which is what tells a desk the payload it holds answers what it serves.
        "stampedRefs": refs,
        "branches": [],
    }
    data["stamp"] = stamp(root, base, refs)
    # The process collecting, which is what the state a page polls answers with: the tool on disk may have moved on from
    # it, and a page carrying that would read as behind the very desk that served it, reloading at every refresh.
    data["desk"] = RUNNING
    marking = []
    for wanted in refs:
        number = pull_number(wanted)
        request = None
        ref = head = wanted
        if number is not None:
            ref, request = fetch_pull(root, upstream, number)
            head = LOCAL_HEADS[number] = local_head(root, ref, request)
        commits = []
        log = run(root, "log", "--format=%h%x1f%s", f"{base}..{head}").strip().split("\n")
        for row in reversed([line for line in log if line]):
            sha, subject = row.split("\x1f")
            files = parse(run(root, "show", "--format=", "--unified=3", sha))
            commits.append({"sha": sha, "subject": subject, "files": files})
        fork = run(root, "merge-base", base, head).strip()
        # What the ref has committed, read from the ref rather than from disk, so work saved there says nothing about
        # where the base stands.
        touched = [row for row in run(root, "diff", "--name-only", fork, head).split("\n") if row]
        carried = bool(touched) and not run(root, "diff", "--name-only", base, head, "--", *touched).strip()
        if carried:
            # The base stands where this ref stands everywhere it touched, so it has taken the work in, squashed or
            # rebased or not. What is left to review is what no commit carries, and reading from the fork point would
            # hand the base its own work back as though it were new.
            whole = run(root, "diff", "--unified=3", "HEAD") if head == current else ""
            revs = ("HEAD", "")
        else:
            # A ref is read from where it forked, so a base that moved on since does not appear in it backwards. One
            # carrying no commit of its own has only the base to be read against, which is the difference it has.
            start = fork if commits else base
            whole = (
                run(root, "diff", "--unified=3", start)
                if head == current
                else run(root, "diff", "--unified=3", start, head)
            )
            revs = (start, "" if head == current else head)
        files = parse(whole)
        # A ref that differs from the base nowhere has nothing to review, whatever it carries in commits.
        if not files:
            continue
        marking.extend([(files, *revs), *[(commit["files"], f"{commit['sha']}^", commit["sha"]) for commit in commits]])
        data["branches"].append(
            {
                "ref": ref,
                "blurb": f"#{number}" if number else ref.split("/")[-1].replace("_", " "),
                "pr": request,
                "tip": run(root, "rev-parse", "--short", head).strip(),
                # Empty means the working tree, which is what the checked-out branch is shown as.
                "rev": "" if head == current else head,
                # What the whole branch is read against: where it forked, or the base when it has nothing of its own.
                "start": revs[0],
                "commits": commits,
                "files": files,
                "dirty": head == current,
            }
        )
    mark_strings(root, marking)
    decorate(data)
    return data


if __name__ == "__main__":
    payload = collect(os.environ.get("DIFF_ROOT", "."), os.environ.get("DIFF_BASE", "upstream/main"), sys.argv[1:])
    out = home() / "diff_data.json"
    out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    template = pathlib.Path(__file__).parent / "diff_desk_template.html"
    (home() / "diff_desk.html").write_text(render_page(template.read_text(encoding="utf-8"), payload), encoding="utf-8")
    print(f"{sum(len(b['files']) for b in payload['branches'])} file diffs, ", end="")
    print(f"{sum(len(b['commits']) for b in payload['branches'])} commits, upstream {payload['upstream'] or '(none)'}")
    print("bytes:", out.stat().st_size)
