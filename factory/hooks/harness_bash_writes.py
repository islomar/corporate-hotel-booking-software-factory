"""Find the harness paths a Bash command may write, for deny_harness_edits.py (CHB-165).

`write_targets("cd factory && echo x > a", "/repo")` gives `[("a", "/repo/factory")]`: each path is
as written, with the working directory it resolves against. Only paths that name a harness folder
(`.claude`, `factory`, `.github`) in some segment are returned; the caller decides by branch.

Forms read: output redirections (`>`, `>>`, `>|`, `&>`, `2>`), `tee`, `sed -i` and `perl -i`,
`cp`/`mv`/`install`/`ln`/`rsync` (destination, and the sources of `mv`), `rm`, `rmdir`, `touch`,
`mkdir`, `truncate`, `dd of=`, `cd` before any of them, `sh -c '...'`, shell here-documents, and
python, node, ruby, perl or php code that calls a write function and names a harness path.
A command that only reads a path (`cat`, `grep`, `git diff`, `git show`, `cp` from it) gives nothing.
The body of a here-document read by another program (`cat > f <<EOF`) is data, not commands.

Not caught: paths built from variables or command substitution (`$DIR/x`, `$(pwd)/x`), writes by
scripts or tools that the command only starts (`python3 script.py`, `make`, `npm run`, `curl -o`),
`git` commands that change the tree (`git checkout`, `git apply`, `git restore`, `git mv`),
`eval`, `xargs`, `find -exec`, `awk` and `ed` scripts, and interpreter code that builds a path from
pieces. Code-owner review of `.claude/`, `factory/` and `.github/` is the backstop.
"""

from __future__ import annotations

import posixpath
import re

from shell_words import is_operator, name, positionals, redirection, strip_launchers, tokens

HARNESS = {".claude", "factory", ".github"}
SEPARATOR_CHARS = ";|()\n"

SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
INTERPRETER = re.compile(r"^(python[0-9.]*|node|nodejs|deno|bun|ruby|perl|php)$")
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
CODE_WRITE = re.compile(
    r"""open\([^)]*?,\s*(?:mode\s*=\s*)?['"][rwxabt+]*[wax+][rwxabt+]*['"]"""  # python open(p, 'w')
    r"|\.(?:write|write_text|write_bytes|touch|unlink|rename|replace|mkdir|rmdir|symlink_to)\("
    r"|\b(?:shutil\.(?:copy|copy2|copyfile|copytree|move|rmtree)|os\.(?:rename|replace|remove|unlink|rmdir|makedirs|mkdir|symlink|link|truncate))\("
    r"|\b(?:writeFile|writeFileSync|appendFile|appendFileSync|copyFile|copyFileSync|rename|renameSync|unlink|unlinkSync|rmSync|mkdirSync|cpSync)\("
    r"|\bFile\.(?:write|open|rename|delete|binwrite)\b|\bfile_put_contents\(|\bFileUtils\.",
)
CODE_WORD = re.compile(r"""[^\s"'()<>,;|&=:\[\]{}]+""")

SED_IN_PLACE = re.compile(r"^(--in-place(=.*)?|-[nEerszu]*i.*)$")
PERL_IN_PLACE = re.compile(r"^-[pnlaw0-9]*i.*$")
SED_VALUE_OPTIONS = {"-e", "-f", "--expression", "--file", "-l", "--line-length"}
COPY_VALUE_OPTIONS = {"-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group", "-e", "--rsh"}
WRITES_ALL_POSITIONALS = {"rm", "rmdir", "touch", "mkdir", "tee", "truncate", "unlink", "shred"}
WRITES_LAST_POSITIONAL = {"cp", "install", "ln", "rsync"}
WRITES_ALL_VALUE_OPTIONS = {"truncate": {"-s", "--size", "-r", "--reference"}, "touch": {"-d", "-t", "-r", "--date", "--reference"}, "mkdir": {"-m", "--mode"}}


def mentions_harness(text: str) -> bool:
    return any(folder in text for folder in HARNESS)


def has_harness_segment(path: str, cwd: str) -> bool:
    """Some folder of the path, resolved against cwd, is named like a harness folder (deny_harness_edits decides)."""
    return bool(HARNESS & set(posixpath.join(cwd, path).split("/")))


def write_targets(command: str, cwd: str) -> list[tuple[str, str]]:
    """Paths the command may write that name a harness folder, as (path, working directory)."""
    if not (mentions_harness(command) or mentions_harness(cwd)):  # a shell that sits in a harness folder writes there
        return []
    found: list[tuple[str, str]] = []
    _scan(command, cwd, found)
    return found


def _scan(command: str, cwd: str, found: list[tuple[str, str]]) -> None:
    flat, bodies = _split_heredocs(command)
    interpreter_seen = False
    for words, redirects, here in _segments(flat):
        found.extend((target, cwd) for target in redirects if has_harness_segment(target, cwd))
        words = strip_launchers(words)
        if not words:
            continue
        program, args = name(words[0]), words[1:]
        if program in ("cd", "pushd"):
            cwd = _cd(cwd, args)
            continue
        if program in SHELLS:
            for script in _shell_scripts(args):
                _scan(script, cwd, found)
            if here:
                for body in bodies:
                    _scan(body, cwd, found)
        elif INTERPRETER.match(program):
            interpreter_seen = True
        found.extend((target, cwd) for target in _command_targets(program, args) if has_harness_segment(target, cwd))
    if interpreter_seen and CODE_WRITE.search(command):
        found.extend((word, cwd) for word in CODE_WORD.findall(command) if has_harness_segment(word, cwd))


def _cd(cwd: str, args: list[str]) -> str:
    dirs = positionals(args, set())
    if not dirs or "$" in dirs[0] or "`" in dirs[0]:
        return cwd
    return posixpath.normpath(posixpath.join(cwd, posixpath.expanduser(dirs[0])))


def _shell_scripts(args: list[str]) -> list[str]:
    """The string after `-c` (also `-lc`, `-ec`) of a shell command."""
    for i, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:] and i + 1 < len(args):
            return [args[i + 1]]
    return []


def _command_targets(program: str, args: list[str]) -> list[str]:
    if program == "sed":
        if any(SED_IN_PLACE.match(a) for a in args if a.startswith("-")):
            return _files_after_script(args)
        return []
    if program == "perl":
        if any(PERL_IN_PLACE.match(a) for a in args if a.startswith("-")):
            return _files_after_script(args)
        return []
    if program == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if program in WRITES_LAST_POSITIONAL:
        return _destination(args)
    if program == "mv":
        return positionals(args, COPY_VALUE_OPTIONS) + _target_directory(args)
    if program in WRITES_ALL_POSITIONALS:
        return positionals(args, WRITES_ALL_VALUE_OPTIONS.get(program, set()))
    return []


def _files_after_script(args: list[str]) -> list[str]:
    """Operands of `sed -i` / `perl -i`: all positionals, minus the script when no -e or -f gave one."""
    given = any(a in ("-e", "-f", "--expression", "--file") or a.startswith("--expression=") for a in args)
    operands = positionals(args, SED_VALUE_OPTIONS)
    return operands if given else operands[1:]


def _target_directory(args: list[str]) -> list[str]:
    out = []
    for i, arg in enumerate(args):
        if arg in ("-t", "--target-directory") and i + 1 < len(args):
            out.append(args[i + 1])
        elif arg.startswith("--target-directory="):
            out.append(arg.split("=", 1)[1])
    return out


def _destination(args: list[str]) -> list[str]:
    targeted = _target_directory(args)
    if targeted:
        return targeted
    operands = positionals(args, COPY_VALUE_OPTIONS)
    return operands[-1:]


def _split_heredocs(command: str) -> tuple[str, list[str]]:
    """The command without here-document bodies, and the bodies in order."""
    kept: list[str] = []
    bodies: list[str] = []
    pending: list[str] = []
    body: list[str] = []
    for line in command.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                bodies.append("\n".join(body))
                body = []
                pending.pop(0)
            else:
                body.append(line)
            continue
        kept.append(line)
        pending = [match.group(2) for match in HEREDOC.finditer(line)]
    return "\n".join(kept), bodies


def _segments(command: str):
    """Yield (words, redirect targets, has here-document) for each simple command of the command."""
    words: list[str] = []
    redirects: list[str] = []
    here = False
    items = [*tokens(command), ";"]
    i = 0
    while i < len(items):
        item = items[i]
        if not is_operator(item):
            words.append(item)
            i += 1
            continue
        if redirection(item):
            if "<<" in item:
                here = True
            if i + 1 < len(items) and not is_operator(items[i + 1]):
                if ">" in item:
                    redirects.append(items[i + 1])
                i += 1
            if words and words[-1].isdigit():
                words.pop()
            i += 1
            continue
        yield words, redirects, here
        words, redirects, here = [], [], False
        i += 1
