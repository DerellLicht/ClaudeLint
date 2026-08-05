#!/usr/bin/env python3
# usage: python phase0_spike.py --file cmd_line.cpp
"""
Phase 0 feasibility spike (unused-symbol-linter-spec-V0.3, section 5).

Confirms libclang Python bindings load correctly and can parse a real
translation unit using the exact flags captured in compile_commands.json
(no hand-copied/guessed flags).

Usage:
    python phase0_spike.py [--file SUBSTRING] [--libclang-path PATH]
                            [--compile-commands PATH]

    --file SUBSTRING       Pick the entry whose "file" contains this text
                            (case-insensitive). Default: first entry.
    --libclang-path PATH   Explicit path to libclang.dll/.so, in case
                            auto-discovery doesn't find the pip-bundled one.
    --compile-commands PATH
                            Defaults to ./compile_commands.json
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    import clang.cindex
except ImportError:
    sys.exit("clang.cindex not found. Install with: pip install libclang")

# Driver-invocation args that make sense to `clang++` on the command line
# but are not what clang.cindex.Index.parse() wants in its `args` list.
DROP_FLAGS_NO_ARG = {"-c"}
DROP_FLAGS_WITH_ARG = {"-o"}


def load_compile_commands(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"compile_commands.json not found at: {path}")
    return json.loads(path.read_text())


def pick_entry(entries: list[dict], substring: str | None) -> dict:
    if substring is None:
        return entries[0]
    substring = substring.lower()
    for entry in entries:
        if substring in entry["file"].lower():
            return entry
    sys.exit(f"No compile_commands.json entry matched --file '{substring}'")


def clean_args(raw_args: list[str], source_file: str) -> list[str]:
    """Strip compiler executable, -c, -o <output>, and the bare source
    filename itself (it's passed separately as index.parse()'s path arg,
    so leaving it in `args` too makes libclang see it twice)."""
    cleaned = []
    skip_next = False
    for i, arg in enumerate(raw_args):
        if skip_next:
            skip_next = False
            continue
        if i == 0:
            continue  # the compiler executable itself
        if arg in DROP_FLAGS_NO_ARG:
            continue
        if arg in DROP_FLAGS_WITH_ARG:
            skip_next = True
            continue
        if arg == source_file:
            continue  # would duplicate the path already passed to parse()
        cleaned.append(arg)
    return cleaned


def defines_and_includes(raw_args: list[str]) -> list[str]:
    """Pull just the -D/-I flags out of a raw compile_commands.json argv,
    for use when querying the real compiler's own search paths below."""
    return [a for a in raw_args if a.startswith("-D") or a.startswith("-I")]


def find_engine_resource_dir(libclang_path: str) -> str | None:
    """Given the path to libclang.dll from a real LLVM archive (e.g.
    D:\\clang-22.1.8\\bin\\libclang.dll), locate that SAME build's own
    resource-dir (lib\\clang\\<version>\\include) sitting alongside it.
    Using the engine's own resource-dir, rather than a different
    installation's, guarantees the intrinsic/builtin headers match
    exactly what this specific libclang actually implements."""
    install_root = Path(libclang_path).parent.parent  # .../bin/libclang.dll -> ...
    clang_lib_dir = install_root / "lib" / "clang"
    if not clang_lib_dir.is_dir():
        return None
    version_dirs = sorted(clang_lib_dir.glob("*/include"))
    if not version_dirs:
        return None
    return str(version_dirs[-1])  # highest version dir if more than one


def query_compiler_isystem_dirs(compiler_exe: str, extra_args: list[str]) -> list[str]:
    """Ask the REAL compiler (the one from compile_commands.json, e.g.
    d:/llvm/bin/x86_64-w64-mingw32-clang++) what its own default system
    include search paths are, instead of hand-guessing -isystem dirs.

    This is the `-Bn`-style trick applied to header search: clang -E -v
    prints its resolved '#include <...> search starts here' list to
    stderr. Capturing that gives us the *actual* mingw sysroot paths
    (where its own windows.h etc. live) with zero guessing, and zero
    dependency on the pip-bundled libclang's own (irrelevant) defaults.
    """
    cmd = [compiler_exe, "-E", "-v", "-x", "c++"] + extra_args + ["-"]
    try:
        result = subprocess.run(
            cmd, input="", capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        sys.exit(f"Could not run compiler to query include dirs: {compiler_exe}")

    stderr = result.stderr
    start_marker = "#include <...> search starts here:"
    end_marker = "End of search list."
    if start_marker not in stderr:
        print(
            "  (warning: couldn't find search-path markers in compiler -v "
            "output; skipping auto -isystem discovery)",
            file=sys.stderr,
        )
        return []

    section = stderr.split(start_marker, 1)[1].split(end_marker, 1)[0]
    dirs = []
    for line in section.splitlines():
        line = line.strip()
        if not line:
            continue
        dirs.append(line.split(" (")[0])  # drop "(framework directory)" etc.
    return dirs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="substring to select a compile_commands.json entry")
    ap.add_argument(
        "--libclang-path",
        default=r"D:\clang-22.1.8\bin\libclang.dll",
        help="explicit path to libclang.dll/.so",
    )
    ap.add_argument("--compile-commands", default="compile_commands.json")
    ap.add_argument(
        "--target",
        default="x86_64-w64-mingw32",
        help="Target triple to force, so libclang doesn't auto-detect a "
             "host toolchain (e.g. an unrelated MSVC install) instead of "
             "the mingw one the project actually builds with. "
             "Pass '' to disable.",
    )
    ap.add_argument(
        "--no-query-include-dirs",
        action="store_true",
        help="Skip querying the real compiler for its -isystem dirs "
             "(on by default).",
    )
    args = ap.parse_args()

    if args.libclang_path:
        clang.cindex.Config.set_library_file(args.libclang_path)

    entries = load_compile_commands(Path(args.compile_commands))
    entry = pick_entry(entries, args.file)

    directory = Path(entry["directory"])
    source_file = directory / entry["file"]
    clang_args = clean_args(entry["arguments"], entry["file"])
    if args.target:
        clang_args = [f"--target={args.target}"] + clang_args

    if not args.no_query_include_dirs:
        compiler_exe = entry["arguments"][0]
        query_args = defines_and_includes(entry["arguments"])
        print(f"Querying real compiler for its -isystem dirs: {compiler_exe}")
        isystem_dirs = query_compiler_isystem_dirs(compiler_exe, query_args)

        # Drop the queried compiler's OWN resource-dir (lib/clang/<ver>/include)
        # — that's specific to d:/llvm's particular build. Use the parsing
        # engine's own resource-dir instead, so builtin/intrinsic headers
        # always match exactly what libclang itself implements.
        isystem_dirs = [d for d in isystem_dirs if "lib" + "/clang/" not in d.replace("\\", "/")]

        # Drop directories that are just echoed -I flags (e.g. der_libs),
        # already present elsewhere in clang_args as -I.
        project_include_dirs = {
            a[2:] for a in entry["arguments"] if a.startswith("-I")
        }
        isystem_dirs = [d for d in isystem_dirs if d not in project_include_dirs]

        if args.libclang_path:
            engine_resource_dir = find_engine_resource_dir(args.libclang_path)
            if engine_resource_dir:
                isystem_dirs.append(engine_resource_dir)
            else:
                print(
                    "  (warning: couldn't find a lib/clang/<ver>/include next "
                    "to --libclang-path; builtin headers may be missing)",
                    file=sys.stderr,
                )

        if isystem_dirs:
            print(f"  using {len(isystem_dirs)} search dir(s):")
            for d in isystem_dirs:
                print(f"    {d}")
            clang_args = [f"-isystem{d}" for d in isystem_dirs] + clang_args
        print()

    print(f"Selected file : {entry['file']}")
    print(f"Full path     : {source_file}")
    print(f"Parse args    : {clang_args}")
    print()

    index = clang.cindex.Index.create()
    print(f"Parsing engine (libclang): {args.libclang_path or '(auto-discovered)'}")
    print()

    tu = index.parse(str(source_file), args=clang_args)

    if not tu:
        sys.exit("Parse failed: no translation unit returned.")

    diags = list(tu.diagnostics)
    if not diags:
        print("No diagnostics. Parse succeeded cleanly.")
    else:
        print(f"{len(diags)} diagnostic(s):")
        for d in diags:
            print(f"  [{d.severity}] {d.location}: {d.spelling}")

    # Quick sanity check that the AST actually resolved real content,
    # not just an empty/failed parse that happened to return a TU object.
    top_level_kinds = {}
    for cursor in tu.cursor.get_children():
        top_level_kinds[cursor.kind.name] = top_level_kinds.get(cursor.kind.name, 0) + 1

    print()
    print("Top-level cursor kinds found (sanity check, from included headers too):")
    for kind, count in sorted(top_level_kinds.items(), key=lambda kv: -kv[1]):
        print(f"  {kind}: {count}")


if __name__ == "__main__":
    main()
