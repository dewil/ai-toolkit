#!/usr/bin/env python3
"""Create one durable memory fact and add its link to MEMORY.md safely."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
NO_SNAPSHOT = object()


class Invalid(Exception):
    """Input or filesystem state cannot be handled without risk."""


def regular(path: Path, *, required: bool = True) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if required:
            raise Invalid(f"required path is missing: {path.name}")
        return False
    if not stat.S_ISREG(mode):
        raise Invalid(f"path must be a regular file: {path.name}")
    return True


def directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise Invalid(f"required directory is missing: {path.name}") from None
    if not stat.S_ISDIR(mode):
        raise Invalid(f"path must be a directory: {path.name}")


def read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise Invalid(f"cannot safely read {path.name}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Invalid(f"path must be a regular file: {path.name}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def assert_snapshot(path: Path, expected: bytes | None, message: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if expected is None:
            return
        raise Invalid(message) from None
    if expected is None or not stat.S_ISREG(mode) or read_regular(path) != expected:
        raise Invalid(message)


def atomic_replace(path: Path, data: bytes, mode: int,
                   expected: bytes | None | object = NO_SNAPSHOT,
                   changed_message: str = "target changed during write",
                   guards: tuple[tuple[Path, bytes | None, str], ...] = ()) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".memory-write-", suffix=".tmp",
                                     dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for guarded_path, snapshot, message in guards:
            assert_snapshot(guarded_path, snapshot, message)
        if expected is not NO_SNAPSHOT:
            assert_snapshot(path, expected, changed_message)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def markdown_label(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>])", r"\\\1", text)


def link_pattern(name: str) -> re.Pattern[bytes]:
    return re.compile(rb"\]\((?:\./)?" + re.escape(name.encode("ascii")) + rb"\.md\)")


def append_link(index: bytes, name: str, description: str) -> bytes:
    separator = b"" if not index or index.endswith(b"\n") else b"\n"
    if index and not index.endswith(b"\n\n"):
        separator += b"\n"
    entry = (f"- [{markdown_label(description)}]({name}.md)\n").encode("utf-8")
    return index + separator + entry


def validate_root(root_arg: str) -> tuple[Path, Path, Path, Path]:
    try:
        root = Path(root_arg).resolve(strict=True)
    except OSError:
        raise Invalid("selected root must already exist") from None
    if not root.is_dir():
        raise Invalid("selected root must be a directory")
    ai = root / ".AI"
    memory = ai / "memory"
    directory(ai)
    directory(memory)
    config = ai / "project.json"
    regular(config)
    try:
        project = json.loads(read_regular(config).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Invalid(".AI/project.json must contain valid project metadata") from None
    project_id = project.get("project_id") if isinstance(project, dict) else None
    if not isinstance(project_id, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            project_id):
        raise Invalid(".AI/project.json must contain a valid project_id")
    index = memory / "MEMORY.md"
    regular(index)
    return root, memory, index, memory / ".write.lock"


def write_fact(memory: Path, index_path: Path, lock_path: Path,
               name: str, description: str, body: bytes) -> str:
    try:
        import fcntl
    except ImportError:
        raise Invalid("memory writing requires Linux flock support") from None

    fact_path = memory / f"{name}.md"
    regular(lock_path, required=False)
    existing_fact = regular(fact_path, required=False)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
    except OSError:
        raise Invalid("cannot safely open memory write lock") from None
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise Invalid("memory write lock must be a regular file")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        current_index = read_regular(index_path)
        existing_fact = regular(fact_path, required=False)
        fact = ("---\ndescription: " + json.dumps(description, ensure_ascii=False) +
                "\n---\n").encode("utf-8") + body
        if existing_fact:
            if read_regular(fact_path) != fact:
                raise Invalid("fact ID already exists with different content")
        else:
            atomic_replace(fact_path, fact, 0o600, expected=None,
                           changed_message="fact ID appeared during write; refusing to overwrite it")

        if link_pattern(name).search(current_index):
            return "unchanged" if existing_fact else "created"

        updated_index = append_link(current_index, name, description)
        index_mode = stat.S_IMODE(index_path.lstat().st_mode)
        atomic_replace(index_path, updated_index, index_mode, expected=current_index,
                       changed_message="MEMORY.md changed during write; fact is complete and index was preserved",
                       guards=((fact_path, fact,
                                "fact changed during write; MEMORY.md was preserved"),))
        return "index-repaired" if existing_fact else "created"
    finally:
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="explicit .AI project root")
    parser.add_argument("--name", required=True, help="safe short fact ID")
    parser.add_argument("--description", required=True, help="one-line search description")
    args = parser.parse_args(argv)
    if not NAME.fullmatch(args.name):
        print("--name must match [a-z0-9][a-z0-9_-]{0,99}", file=sys.stderr)
        return 2
    if not args.description.strip() or "\n" in args.description or "\r" in args.description:
        print("--description must be nonempty and fit on one line", file=sys.stderr)
        return 2
    try:
        body = sys.stdin.buffer.read()
        body.decode("utf-8")
        if not body.strip():
            raise Invalid("stdin body must be nonempty UTF-8 text")
        _, memory, index, lock = validate_root(args.root)
        status = write_fact(memory, index, lock, args.name, args.description, body)
    except UnicodeDecodeError:
        print("stdin body must be valid UTF-8", file=sys.stderr)
        return 2
    except (Invalid, OSError) as error:
        message = str(error) if isinstance(error, Invalid) else "memory write failed safely"
        print(message, file=sys.stderr)
        return 2
    print(json.dumps({"status": status, "filename": f"{args.name}.md"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
