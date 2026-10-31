#!/usr/bin/env python3
"""manor - reach your Manor files from inside the sandbox.

Installed on PATH in the sandbox image. Standard library only: it must run
in any sandbox image without installing anything.

It works only inside a ``bash`` tool command: that command's environment
carries MANOR_API_URL and a short-lived MANOR_CLI_TOKEN scoped to the agent,
user, conversation and sandbox the command runs for. Every request is
checked on the server with the same permissions as the agent's file tools.

$MANOR_WORKSPACE (the command's cwd) is this agent's working copy of the
entity's files, in its own area of the user's shared sandbox. Files a bash
command names are copied in before it runs; files created, changed or
deleted in it are saved back when the command finishes (approval may be
required). Use /tmp for scratch output.

Exit codes: 0 ok, 1 the server refused or the file was not found, 2 usage
error, 3 no or expired token / not configured, 4 the API was unreachable.

This module is imported by the server for ``COMMANDS`` (the prompt text is
generated from it), so importing it must have no side effects.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_UNREACHABLE = 4

_DEFAULT_WORKSPACE = "/workspace"
_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    summary: str
    # Named in the bash tool's always-loaded description. A command only some
    # users have stays out of it (``manor --help`` still lists it).
    in_tool_summary: bool = True


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("ls", "manor ls [-l] [-R] [PATH]", "list entity files on the server without copying them"),
    CommandSpec("tree", "manor tree [PATH]", "recursive listing on the server"),
    CommandSpec("stat", "manor stat PATH", "type, size and mtime of one path"),
    CommandSpec("cat", "manor cat PATH [--offset N] [--limit N]", "read a file's text (PDF/DOCX/XLSX extracted)"),
    CommandSpec("grep", "manor grep PATTERN [PATH] [--glob G] [--max N]", "case-insensitive regex search of every file you may read"),
    CommandSpec("glob", "manor glob PATTERN", "find paths by name, e.g. '**/*.pdf'"),
    CommandSpec("search", "manor search QUESTION", "semantic search of Knowledge document contents"),
    CommandSpec("pull", "manor pull PATH [DEST]", "copy a file into the sandbox (default: the working copy's PATH)"),
    CommandSpec("push", "manor push SRC DEST", "save a sandbox file as entity file DEST"),
    CommandSpec("mv", "manor mv SRC... DEST", "move or rename entity files without copying them"),
    CommandSpec("cp", "manor cp SRC... DEST", "copy entity files without copying them into the sandbox"),
    CommandSpec("rm", "manor rm [-r] PATH...", "delete entity files"),
    CommandSpec("mkdir", "manor mkdir PATH...", "create entity folders"),
    CommandSpec("touch", "manor touch PATH...", "create empty entity files"),
    CommandSpec("whoami", "manor whoami", "show who this command runs as"),
)


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _workspace_root() -> str:
    return os.environ.get("MANOR_WORKSPACE") or _DEFAULT_WORKSPACE


def _entity_path(raw: str) -> str:
    """Entity-relative path for an argument given relative to /workspace or absolute in it."""

    text = raw.replace("\\", "/")
    root = _workspace_root().rstrip("/")
    for prefix in (root + "/", _DEFAULT_WORKSPACE + "/"):
        if text.startswith(prefix):
            stripped = os.path.normpath(text[len(prefix):] or ".")
            return "" if stripped == "." else stripped
    if text in {root, _DEFAULT_WORKSPACE}:
        return ""
    if text.startswith("/"):
        raise CliError(f"{raw}: not under {_DEFAULT_WORKSPACE}; entity paths are relative to it", EXIT_USAGE)
    relative_cwd = ""
    cwd = os.getcwd().replace("\\", "/")
    for base in (root, _DEFAULT_WORKSPACE, os.path.realpath(root)):
        if cwd == base or cwd.startswith(base.rstrip("/") + "/"):
            relative_cwd = cwd[len(base):].strip("/")
            break
    joined = os.path.normpath(os.path.join(relative_cwd, text) if relative_cwd else text)
    return "" if joined == "." else joined


def _local_path(rel_path: str) -> str:
    return os.path.join(_workspace_root(), rel_path)


def _request(endpoint: str, body: dict, *, raw: bool = False):
    base = os.environ.get("MANOR_API_URL", "").rstrip("/")
    token = os.environ.get("MANOR_CLI_TOKEN", "")
    if not base or not token:
        raise CliError(
            "manor: MANOR_API_URL/MANOR_CLI_TOKEN are not set. The manor CLI works only "
            "inside a bash tool command (and needs a runner that supports per-command env).",
            EXIT_AUTH,
        )
    request = urllib.request.Request(
        f"{base}/api/v1/sandbox-cli/{endpoint}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "manor-sandbox/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = response.read()
            if raw:
                return payload, dict(response.headers)
            return json.loads(payload or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}")
        except ValueError:
            detail = {}
        message = detail.get("detail") or detail.get("error") or exc.reason
        code = EXIT_AUTH if exc.code == 401 else EXIT_REFUSED
        raise CliError(f"manor: {message}", code) from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CliError(f"manor: cannot reach the Manor API at {base}: {reason}", EXIT_UNREACHABLE) from None


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_ls(args) -> int:
    payload = _request("ls", {
        "path": _entity_path(args.path or "."),
        "recursive": bool(args.recursive),
        "limit": args.limit,
        "offset": args.offset,
    })
    if args.json:
        _print_json(payload)
        return EXIT_OK
    base = payload.get("path") or "."
    for entry in payload.get("entries") or []:
        path = entry.get("path") or ""
        shown = path if args.recursive or base == "." else path[len(base):].lstrip("/") or path
        if entry.get("type") == "dir":
            shown += "/"
        if args.long:
            size = entry.get("size")
            print(f"{entry.get('type', 'file'):4} {'' if size is None else size:>12} {shown}")
        else:
            print(shown)
    if payload.get("has_more"):
        print(f"... more entries: rerun with --offset {payload.get('next_offset')}", file=sys.stderr)
    return EXIT_OK


def _cmd_tree(args) -> int:
    args.recursive = True
    args.long = False
    return _cmd_ls(args)


def _cmd_stat(args) -> int:
    payload = _request("stat", {"path": _entity_path(args.path)})
    if args.json:
        _print_json(payload)
    else:
        print(f"{payload.get('path')}\t{payload.get('type')}\t{payload.get('size')}\t{payload.get('mtime')}")
    return EXIT_OK


def _cmd_cat(args) -> int:
    body = {"path": _entity_path(args.path)}
    for name in ("offset", "limit"):
        value = getattr(args, name)
        if value is not None:
            body[name] = value
    payload = _request("read", body)
    if args.json:
        _print_json(payload)
        return EXIT_OK
    content = payload.get("content")
    if content is None:
        _print_json(payload)
        return EXIT_OK
    sys.stdout.write(content)
    if content and not content.endswith("\n"):
        sys.stdout.write("\n")
    if payload.get("truncated") and payload.get("next_offset") is not None:
        print(f"... truncated: rerun with --offset {payload['next_offset']}", file=sys.stderr)
    return EXIT_OK


def _cmd_grep(args) -> int:
    payload = _request("grep", {
        "pattern": args.pattern,
        "path": _entity_path(args.path or "."),
        "glob": args.glob or "",
        "max_matches": args.max,
        "offset": args.offset,
    })
    if args.json:
        _print_json(payload)
        return EXIT_OK
    matches = payload.get("matches") or []
    for match in matches:
        print(f"{match.get('file')}:{match.get('line')}:{match.get('text')}")
    if payload.get("has_more"):
        print(f"... more matches: rerun with --offset {payload.get('next_offset')}", file=sys.stderr)
    if payload.get("truncated"):
        print("... scan budget exhausted before every file was searched; narrow PATH or --glob", file=sys.stderr)
    return EXIT_OK if matches else EXIT_REFUSED


def _cmd_glob(args) -> int:
    payload = _request("glob", {"pattern": args.pattern, "limit": args.limit, "offset": args.offset})
    if args.json:
        _print_json(payload)
        return EXIT_OK
    for path in payload.get("files") or []:
        print(path)
    if payload.get("has_more"):
        print(f"... more files: rerun with --offset {payload.get('next_offset')}", file=sys.stderr)
    return EXIT_OK


def _cmd_search(args) -> int:
    body = {"query": " ".join(args.query)}
    if args.limit is not None:
        body["limit"] = args.limit
    _print_json(_request("search", body))
    return EXIT_OK


def _cmd_pull(args) -> int:
    rel_path = _entity_path(args.path)
    data, headers = _request("pull", {"path": rel_path}, raw=True)
    destination = args.dest or _local_path(headers.get("X-Manor-Path") or rel_path)
    parent = os.path.dirname(destination)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = destination + ".manor-pull"
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, destination)
    print(f"{destination}\t{len(data)} bytes")
    return EXIT_OK


def _cmd_push(args) -> int:
    if not os.path.isfile(args.src):
        raise CliError(f"manor push: {args.src}: not a file", EXIT_USAGE)
    destination = _local_path(_entity_path(args.dest))
    if os.path.isdir(destination):
        destination = os.path.join(destination, os.path.basename(args.src))
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    shutil.copyfile(args.src, destination)
    print(f"{destination} (saved to Manor when this command finishes)")
    return EXIT_OK


def _queue(op: str, paths: list[str], recursive: bool = False) -> int:
    payload = _request("ops", {
        "op": op,
        "paths": [_entity_path(path) for path in paths],
        "recursive": recursive,
    })
    print(f"queued {op} {' '.join(payload.get('paths') or [])}: {payload.get('note', '')}")
    return EXIT_OK


def _cmd_mv(args) -> int:
    return _queue("mv", args.paths)


def _cmd_cp(args) -> int:
    return _queue("cp", args.paths)


def _cmd_rm(args) -> int:
    return _queue("rm", args.paths, recursive=args.recursive)


def _cmd_mkdir(args) -> int:
    for path in args.paths:
        os.makedirs(_local_path(_entity_path(path)), exist_ok=True)
    return EXIT_OK


def _cmd_touch(args) -> int:
    for path in args.paths:
        local = _local_path(_entity_path(path))
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        with open(local, "a", encoding="utf-8"):
            pass
    return EXIT_OK


def _cmd_whoami(args) -> int:
    _print_json(_request("whoami", {}))
    return EXIT_OK


HANDLERS = {
    "ls": _cmd_ls,
    "tree": _cmd_tree,
    "stat": _cmd_stat,
    "cat": _cmd_cat,
    "grep": _cmd_grep,
    "glob": _cmd_glob,
    "search": _cmd_search,
    "pull": _cmd_pull,
    "push": _cmd_push,
    "mv": _cmd_mv,
    "cp": _cmd_cp,
    "rm": _cmd_rm,
    "mkdir": _cmd_mkdir,
    "touch": _cmd_touch,
    "whoami": _cmd_whoami,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manor",
        description="Reach your Manor files from inside the sandbox.",
        epilog="\n".join(f"  {spec.usage:<48} {spec.summary}" for spec in COMMANDS),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    specs = {spec.name: spec for spec in COMMANDS}

    def command(name: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name, help=specs[name].summary, description=specs[name].summary)
        child.add_argument("--json", action="store_true", help="print the raw JSON response")
        return child

    ls = command("ls")
    ls.add_argument("-l", dest="long", action="store_true")
    ls.add_argument("-R", dest="recursive", action="store_true")
    ls.add_argument("--limit", type=int, default=500)
    ls.add_argument("--offset", type=int, default=0)
    ls.add_argument("path", nargs="?")
    tree = command("tree")
    tree.add_argument("--limit", type=int, default=500)
    tree.add_argument("--offset", type=int, default=0)
    tree.add_argument("path", nargs="?")
    command("stat").add_argument("path")
    cat = command("cat")
    cat.add_argument("path")
    cat.add_argument("--offset", type=int)
    cat.add_argument("--limit", type=int)
    grep = command("grep")
    grep.add_argument("pattern")
    grep.add_argument("path", nargs="?")
    grep.add_argument("--glob")
    grep.add_argument("--max", type=int)
    grep.add_argument("--offset", type=int)
    glob = command("glob")
    glob.add_argument("pattern")
    glob.add_argument("--limit", type=int)
    glob.add_argument("--offset", type=int)
    search = command("search")
    search.add_argument("query", nargs="+")
    search.add_argument("--limit", type=int)
    pull = command("pull")
    pull.add_argument("path")
    pull.add_argument("dest", nargs="?")
    push = command("push")
    push.add_argument("src")
    push.add_argument("dest")
    for name in ("mv", "cp"):
        command(name).add_argument("paths", nargs="+", metavar="PATH")
    rm = command("rm")
    rm.add_argument("-r", "-R", "--recursive", dest="recursive", action="store_true")
    rm.add_argument("-f", dest="force", action="store_true", help="accepted for compatibility")
    rm.add_argument("paths", nargs="+", metavar="PATH")
    for name in ("mkdir", "touch"):
        child = command(name)
        child.add_argument("-p", action="store_true", help="accepted for compatibility")
        child.add_argument("paths", nargs="+", metavar="PATH")
    command("whoami")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    if args.command in {"mv", "cp"} and len(args.paths) < 2:
        print(f"manor {args.command}: needs a source and a destination", file=sys.stderr)
        return EXIT_USAGE
    try:
        return HANDLERS[args.command](args)
    except CliError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"manor {args.command}: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
