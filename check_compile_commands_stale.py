#!/usr/bin/env python3
"""
compile_commands.json staleness checker (unused-symbol-linter-spec-V0.4,
section 4.1.1).

Compares the *committed* compile_commands.json against what `make -Bn`
would produce right now, and reports any mismatch as a blocking error.
This tool is READ-ONLY with respect to compile_commands.json: it never
writes, patches, or regenerates it, no matter what it finds. See §4.1.1
for why — compile_commands.json may contain a deliberately hand-tuned
compiler-path entry (e.g. for clang-tidy) that a naive regeneration
would silently clobber.

What it checks:
  1. Source files `make -Bn` would compile, but that are missing from
     compile_commands.json entirely (e.g. a .cpp added to the Makefile
     that compile_commands.json was never updated to include).
  2. Entries in compile_commands.json whose source file `make -Bn` no
     longer builds (removed from the Makefile) -- these are further
     split into "file doesn't exist on disk at all" vs "file exists but
     the Makefile doesn't build it anymore".
  3. For files present in both: whether the *build flags* (-D/-I/-W/etc,
     everything except the compiler executable itself, which is exempt
     per §4.1.1) have drifted between the Makefile and the committed
     JSON.

Usage:
    python check_compile_commands_stale.py [--compile-commands PATH]
                                            [--directory DIR]
                                            [--make-cmd CMD]

    --compile-commands PATH   Defaults to ./compile_commands.json
    --directory DIR           Where to run `make -Bn`. Defaults to the
                               "directory" field of the first entry in
                               compile_commands.json.
    --make-cmd CMD            Defaults to "make" (use "mingw32-make" etc.
                               if that's what your toolchain provides).

Exit code is 0 if everything matches, 1 if any mismatch was found (so
this can be wired into a pre-flight check before running the rest of
the linter, or into CI).
"""

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

SOURCE_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx"}

# Recipe-line mechanics that don't represent real build configuration --
# excluded when diffing flag sets between the Makefile and the JSON, in
# addition to the source filename itself and (per §4.1.1) the compiler
# executable path.
DROP_FLAGS_NO_ARG = {"-c"}
DROP_FLAGS_WITH_ARG = {"-o"}


def load_compile_commands(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"compile_commands.json not found at: {path}")
    return json.loads(path.read_text())


def run_make_dry_run(directory: Path, make_cmd: str) -> str:
    """Run `make -Bn` (see spec §4.1: -B forces every rule to be
    considered out of date so every compile command actually prints;
    -n means nothing is actually built) and return its stdout.

    -n also has the useful side effect of printing recipe lines even if
    they're normally silenced with a leading '@' in the Makefile, which
    is exactly what we want here."""
    cmd = [make_cmd, "-B", "-n"]
    try:
        result = subprocess.run(
            cmd, cwd=str(directory), capture_output=True, text=True, timeout=120
        )
    except FileNotFoundError:
        sys.exit(
            f"Could not run '{make_cmd}'. Is it on PATH? "
            f"(try --make-cmd mingw32-make or similar)"
        )
    if result.returncode != 0:
        print(f"warning: '{make_cmd} -B -n' exited {result.returncode}; "
              f"proceeding with whatever it printed to stdout", file=sys.stderr)
    return result.stdout


def extract_compile_commands(dry_run_output: str, known_compilers: set[str]) -> dict[str, list[str]]:
    """Scan `make -Bn` output for lines invoking one of the project's
    known compiler executables, and pull out {source_file: raw_tokens}.

    The source file is identified as the token ending in a recognized
    source extension -- this naturally excludes -o's output file (which
    ends in .o/.obj, not .c/.cpp), without needing to track flag/value
    pairing across the whole line."""
    found: dict[str, list[str]] = {}
    for line in dry_run_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            continue  # unbalanced quotes etc -- not a compile line we can parse
        if not tokens or tokens[0] not in known_compilers:
            continue
        source_tokens = [
            t for t in tokens[1:]
            if Path(t).suffix.lower() in SOURCE_EXTENSIONS
        ]
        if len(source_tokens) != 1:
            # 0 found: not actually a compile line (e.g. a link step).
            # >1 found: ambiguous, skip rather than guess wrong.
            continue
        found[source_tokens[0]] = tokens
    return found


def normalize_flags(tokens: list[str], source_file: str) -> set[str]:
    """Strip compiler exe, -c, -o <out>, and the source filename itself,
    leaving just the set of real build-configuration flags for
    comparison. Order-insensitive on purpose -- Makefile variable
    expansion can reorder flags harmlessly."""
    cleaned = []
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if i == 0:
            continue  # compiler exe -- exempted from drift-checking, §4.1.1
        if tok in DROP_FLAGS_NO_ARG:
            continue
        if tok in DROP_FLAGS_WITH_ARG:
            skip_next = True
            continue
        if tok == source_file:
            continue
        cleaned.append(tok)
    return set(cleaned)


def check_directory_field(entries: list[dict], cc_path: Path) -> list[str]:
    """Guards against the exact failure mode that motivated this check:
    compile_commands.json (or its whole containing folder) copied from
    one checkout to another without updating the "directory" field. If
    the old checkout is still intact on disk, nothing fails loudly --
    every downstream tool just silently analyzes the OLD tree instead
    of the one you're actually sitting in."""
    problems = []
    dirs_in_json = {e["directory"] for e in entries}
    if len(dirs_in_json) > 1:
        problems.append(
            "INCONSISTENT DIRECTORY: compile_commands.json entries don't "
            f"all agree on 'directory' -- found {len(dirs_in_json)} distinct "
            f"values: {sorted(dirs_in_json)}"
        )
        return problems  # can't meaningfully compare further

    claimed_dir = Path(next(iter(dirs_in_json)))
    actual_dir = cc_path.resolve().parent
    try:
        matches = claimed_dir.resolve() == actual_dir
    except OSError:
        matches = False
    if not matches:
        problems.append(
            f"DIRECTORY MISMATCH: compile_commands.json claims directory "
            f"'{claimed_dir}', but the file itself is sitting in "
            f"'{actual_dir}'. This usually means the file (or its whole "
            f"containing folder) was copied from another checkout without "
            f"updating this field -- everything downstream would silently "
            f"analyze the OLD location instead of this one."
        )
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compile-commands", default="compile_commands.json")
    ap.add_argument("--directory", help="where to run `make -Bn` (default: "
                     "the \"directory\" field of the first JSON entry)")
    ap.add_argument("--make-cmd", default="make")
    args = ap.parse_args()

    cc_path = Path(args.compile_commands)
    entries = load_compile_commands(cc_path)
    if not entries:
        sys.exit(f"{cc_path} contains no entries.")

    dir_problems = check_directory_field(entries, cc_path)
    if dir_problems:
        print(f"{len(dir_problems)} problem(s) found -- {cc_path.name} is STALE:")
        for p in dir_problems:
            print(f"  - {p}")
        print()
        print(f"{cc_path.name} was NOT modified. Update it by hand, then re-run.")
        print("(stopped before running make -- the directory itself is "
              "suspect, so a make -Bn result wouldn't be trustworthy anyway.)")
        sys.exit(1)

    directory = Path(args.directory) if args.directory else Path(entries[0]["directory"])
    json_by_file = {e["file"]: e for e in entries}
    known_compilers = {e["arguments"][0] for e in entries}

    print(f"Running '{args.make_cmd} -B -n' in {directory} ...")
    dry_run_output = run_make_dry_run(directory, args.make_cmd)
    makefile_by_file = extract_compile_commands(dry_run_output, known_compilers)

    if not makefile_by_file:
        sys.exit(
            "No compile commands recognized in `make -Bn` output. "
            "Check --make-cmd, or that known compiler paths in "
            "compile_commands.json still match the Makefile."
        )

    problems: list[str] = []

    missing_from_json = sorted(set(makefile_by_file) - set(json_by_file))
    for f in missing_from_json:
        problems.append(f"MISSING FROM JSON: '{f}' is built by the Makefile "
                         f"but has no entry in {cc_path.name}")

    stale_in_json = sorted(set(json_by_file) - set(makefile_by_file))
    for f in stale_in_json:
        entry = json_by_file[f]
        full_path = Path(entry["directory"]) / f
        if not full_path.exists():
            problems.append(f"STALE ENTRY: '{f}' is in {cc_path.name} but the "
                             f"file no longer exists on disk ({full_path})")
        else:
            problems.append(f"STALE ENTRY: '{f}' is in {cc_path.name} but the "
                             f"Makefile no longer builds it")

    for f in sorted(set(makefile_by_file) & set(json_by_file)):
        makefile_flags = normalize_flags(makefile_by_file[f], f)
        json_flags = normalize_flags(json_by_file[f]["arguments"], f)
        added = makefile_flags - json_flags
        removed = json_flags - makefile_flags
        if added or removed:
            detail = []
            if added:
                detail.append(f"Makefile has but JSON lacks: {sorted(added)}")
            if removed:
                detail.append(f"JSON has but Makefile lacks: {sorted(removed)}")
            problems.append(f"FLAGS DIFFER for '{f}': " + "; ".join(detail))

    print()
    if not problems:
        print(f"OK -- {cc_path.name} matches the Makefile "
              f"({len(json_by_file)} file(s) checked).")
        sys.exit(0)

    print(f"{len(problems)} problem(s) found -- {cc_path.name} is STALE:")
    for p in problems:
        print(f"  - {p}")
    print()
    print(f"{cc_path.name} was NOT modified. Update it by hand, then re-run.")
    sys.exit(1)


if __name__ == "__main__":
    main()
