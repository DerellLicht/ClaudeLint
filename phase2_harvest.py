#!/usr/bin/env python3
"""
Phase 2 -- header inventory (spec §4.2) + declared-symbol harvesting
(spec §4.3), unused-symbol-linter-spec-V0.4.

This is a SANITY-CHECK dump only: it reports what the harvester finds
declared, not what's unused (that's Phase 3, reference walking). Two
things bundled into one pass over all of compile_commands.json, because
both need the same per-TU AST walk:

  §4.2 Header inventory: cross-checks headers libclang actually included
  while parsing every TU against headers listed in the Makefile's
  makedepend-generated block, and flags any project header that
  appears in neither -- i.e. a header sitting on disk that (as far as
  this tool can tell) nothing actually uses.

  §4.3 Declared-symbol harvesting: walks every TU's AST and records
  every FIELD_DECL (with its enclosing struct/class), every header-
  scope VAR_DECL ("global"), and every file-scope VAR_DECL declared
  directly in a .cpp/.c ("local" candidate, whether or not `static`).

Reuses the Phase 0 recipe for getting a clean parse (matched-version
libclang engine + querying the real compiler's own -isystem dirs) --
see phase0_spike.py / spec §6.1 for why that machinery exists.

Usage:
    python phase2_harvest.py [--compile-commands PATH]
                              [--libclang-path PATH] [--target TRIPLE]
                              [--makefile PATH] [--no-header-inventory]
"""

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import clang.cindex
    from clang.cindex import CursorKind
except ImportError:
    sys.exit("clang.cindex not found. Install with: pip install libclang")

DROP_FLAGS_NO_ARG = {"-c"}
DROP_FLAGS_WITH_ARG = {"-o"}
HEADER_EXTENSIONS = {".h", ".hpp", ".hh", ".hxx"}
SOURCE_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx"}

# Only recurse into these container kinds when harvesting §4.3 symbols --
# deliberately NOT recursing into FUNCTION_DECL bodies, so local variables
# inside functions never get mistaken for file-scope declarations.
CONTAINER_KINDS = {
    CursorKind.NAMESPACE,
    CursorKind.STRUCT_DECL,
    CursorKind.CLASS_DECL,
    CursorKind.UNION_DECL,
    CursorKind.LINKAGE_SPEC,  # extern "C" { ... } blocks
}
AGGREGATE_KINDS = {CursorKind.STRUCT_DECL, CursorKind.CLASS_DECL, CursorKind.UNION_DECL}


# ---------------------------------------------------------------------
# Phase 0 recipe, reused verbatim (see phase0_spike.py for commentary)
# ---------------------------------------------------------------------

def clean_args(raw_args: list[str], source_file: str) -> list[str]:
    cleaned = []
    skip_next = False
    for i, arg in enumerate(raw_args):
        if skip_next:
            skip_next = False
            continue
        if i == 0:
            continue
        if arg in DROP_FLAGS_NO_ARG:
            continue
        if arg in DROP_FLAGS_WITH_ARG:
            skip_next = True
            continue
        if arg == source_file:
            continue
        cleaned.append(arg)
    return cleaned


def defines_and_includes(raw_args: list[str]) -> list[str]:
    return [a for a in raw_args if a.startswith("-D") or a.startswith("-I")]


def find_engine_resource_dir(libclang_path: str) -> str | None:
    install_root = Path(libclang_path).parent.parent
    clang_lib_dir = install_root / "lib" / "clang"
    if not clang_lib_dir.is_dir():
        return None
    version_dirs = sorted(clang_lib_dir.glob("*/include"))
    return str(version_dirs[-1]) if version_dirs else None


def query_compiler_isystem_dirs(compiler_exe: str, extra_args: list[str]) -> list[str]:
    cmd = [compiler_exe, "-E", "-v", "-x", "c++"] + extra_args + ["-"]
    try:
        result = subprocess.run(cmd, input="", capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        sys.exit(f"Could not run compiler to query include dirs: {compiler_exe}")
    stderr = result.stderr
    start_marker = "#include <...> search starts here:"
    end_marker = "End of search list."
    if start_marker not in stderr:
        return []
    section = stderr.split(start_marker, 1)[1].split(end_marker, 1)[0]
    return [ln.strip().split(" (")[0] for ln in section.splitlines() if ln.strip()]


def build_parse_args(entry: dict, target: str, libclang_path: str | None,
                      isystem_cache: dict | None = None) -> list[str]:
    clang_args = clean_args(entry["arguments"], entry["file"])
    if target:
        clang_args = [f"--target={target}"] + clang_args

    compiler_exe = entry["arguments"][0]
    query_args = defines_and_includes(entry["arguments"])
    cache_key = (compiler_exe, tuple(query_args))

    if isystem_cache is not None and cache_key in isystem_cache:
        isystem_dirs = isystem_cache[cache_key]
    else:
        isystem_dirs = query_compiler_isystem_dirs(compiler_exe, query_args)
        isystem_dirs = [d for d in isystem_dirs if "lib" + "/clang/" not in d.replace("\\", "/")]
        project_include_dirs = {a[2:] for a in entry["arguments"] if a.startswith("-I")}
        isystem_dirs = [d for d in isystem_dirs if d not in project_include_dirs]
        if libclang_path:
            engine_resource_dir = find_engine_resource_dir(libclang_path)
            if engine_resource_dir:
                isystem_dirs.append(engine_resource_dir)
        if isystem_cache is not None:
            isystem_cache[cache_key] = isystem_dirs

    return [f"-isystem{d}" for d in isystem_dirs] + clang_args


# ---------------------------------------------------------------------
# §4.3 declared-symbol harvesting
# ---------------------------------------------------------------------

def is_under(path_str: str, project_dir: Path) -> bool:
    try:
        Path(path_str).resolve().relative_to(project_dir)
        return True
    except (ValueError, OSError):
        return False


def is_excluded(path_str: str, project_dir: Path, patterns: list[str]) -> bool:
    """A path is excluded if it matches an --exclude pattern, either as a
    glob (fnmatch-style, e.g. '*.legacy.h') or as a directory prefix
    (e.g. 'der_libs' or 'der_libs/*' both exclude everything under
    der_libs/, without requiring exact glob syntax for the common case)."""
    if not patterns:
        return False
    try:
        rel = Path(path_str).resolve().relative_to(project_dir).as_posix()
    except (ValueError, OSError):
        return False
    for pat in patterns:
        norm = pat.rstrip("/").removesuffix("/*").rstrip("/")
        if fnmatch.fnmatch(rel, pat):
            return True
        if rel == norm or rel.startswith(norm + "/"):
            return True
    return False


def harvest_tu(tu, main_file: Path, project_dir: Path, symbols: dict, headers_seen: set,
               exclude_patterns: list[str]):
    for inc in tu.get_includes():
        fname = str(inc.include)
        if fname and is_under(fname, project_dir) and not is_excluded(fname, project_dir, exclude_patterns):
            headers_seen.add(str(Path(fname).resolve()))

    def visit(cursor, enclosing_struct):
        for child in cursor.get_children():
            kind = child.kind
            loc_file = child.location.file
            file_str = str(loc_file) if loc_file else None
            in_project = bool(
                file_str
                and is_under(file_str, project_dir)
                and not is_excluded(file_str, project_dir, exclude_patterns)
            )

            if kind in AGGREGATE_KINDS:
                next_enclosing = enclosing_struct
                if child.is_definition() and in_project:
                    next_enclosing = child.spelling or "<anonymous>"
                visit(child, next_enclosing)
                continue

            if kind == CursorKind.FIELD_DECL:
                if in_project and enclosing_struct:
                    usr = child.get_usr()
                    symbols[usr] = {
                        "kind": "field",
                        "name": child.spelling,
                        "enclosing": enclosing_struct,
                        "file": file_str,
                        "line": child.location.line,
                    }
                continue

            if kind == CursorKind.VAR_DECL:
                if in_project:
                    usr = child.get_usr()
                    is_main_file = str(Path(file_str).resolve()) == str(main_file.resolve())
                    symbols[usr] = {
                        "kind": "local" if is_main_file else "global",
                        "name": child.spelling,
                        "enclosing": None,
                        "file": file_str,
                        "line": child.location.line,
                    }
                continue

            if kind in CONTAINER_KINDS:
                visit(child, enclosing_struct)

    visit(tu.cursor, None)


# ---------------------------------------------------------------------
# §4.2 header inventory: makedepend block + on-disk header scan
# ---------------------------------------------------------------------

def parse_makedepend_headers(makefile_path: Path, project_dir: Path) -> set[str]:
    """Best-effort parse of a makedepend-generated dependency block:
    lines of the form `target.o: dep1.h dep2.h \\` with backslash
    line-continuations. Returns resolved, project-relative header paths.
    This is a heuristic over an unspecified-format block -- if it
    doesn't match your Makefile's actual output, the header-inventory
    cross-check will just be conservative (report fewer matches than
    reality) rather than crash; treat mismatches here as a parser bug
    to report back, not as real orphan headers."""
    if not makefile_path.exists():
        return set()

    text = makefile_path.read_text(errors="replace")
    # Join backslash-continued lines into single logical lines.
    text = re.sub(r"\\\r?\n", " ", text)

    headers = set()
    dep_rule = re.compile(r"^\s*[\w./\\-]+\.o\s*:\s*(.+)$")
    for line in text.splitlines():
        m = dep_rule.match(line)
        if not m:
            continue
        for tok in m.group(1).split():
            if Path(tok).suffix.lower() in HEADER_EXTENSIONS:
                full = (project_dir / tok) if not Path(tok).is_absolute() else Path(tok)
                if is_under(str(full), project_dir):
                    headers.add(str(full.resolve()))
    return headers


def scan_disk_headers(project_dir: Path, include_dirs: list[str]) -> set[str]:
    """All project header files actually sitting on disk, under the
    project root and any project-local -I dirs (e.g. der_libs)."""
    roots = {project_dir}
    for d in include_dirs:
        p = (project_dir / d) if not Path(d).is_absolute() else Path(d)
        if p.is_dir():
            roots.add(p.resolve())

    found = set()
    for root in roots:
        for ext in HEADER_EXTENSIONS:
            for f in root.rglob(f"*{ext}"):
                if ".git" in f.parts:
                    continue
                found.add(str(f.resolve()))
    return found


# ---------------------------------------------------------------------
# Parallel worker -- each OS process gets its own libclang Index (libclang
# objects aren't shareable across processes/threads), computes its own
# harvest for one TU, and returns only plain, picklable Python data.
# ---------------------------------------------------------------------

def _parse_one(task: dict) -> dict:
    entry = task["entry"]
    parse_args = task["parse_args"]
    project_dir = Path(task["project_dir"])
    source_file = Path(entry["directory"]) / entry["file"]
    exclude_patterns = task["exclude"]

    if task["libclang_path"]:
        try:
            clang.cindex.Config.set_library_file(task["libclang_path"])
        except Exception:
            pass  # already configured in this worker process from a prior task

    index = clang.cindex.Index.create()
    tu = index.parse(str(source_file), args=parse_args)
    result = {"file": entry["file"], "ok": False, "diag_count": 0, "diag_sample": None,
              "symbols": {}, "headers_seen": []}
    if not tu:
        return result

    diags = [d for d in tu.diagnostics if d.severity >= 3]
    result["diag_count"] = len(diags)
    if diags:
        result["diag_sample"] = diags[0].spelling

    symbols: dict[str, dict] = {}
    headers_seen: set[str] = set()
    harvest_tu(tu, source_file, project_dir, symbols, headers_seen, exclude_patterns)
    result["ok"] = True
    result["symbols"] = symbols
    result["headers_seen"] = list(headers_seen)
    return result


# ---------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compile-commands", default="compile_commands.json")
    ap.add_argument("--libclang-path", default=r"D:\clang-22.1.8\bin\libclang.dll")
    ap.add_argument("--target", default="x86_64-w64-mingw32")
    ap.add_argument("--makefile", default="Makefile")
    ap.add_argument("--exclude", action="append", default=[],
                     help="path (dir prefix or fnmatch glob) to exclude from "
                          "symbol harvesting and header inventory entirely, "
                          "relative to the project directory (e.g. der_libs, "
                          "or der_libs/*, or *.legacy.h). Repeatable.")
    ap.add_argument("--no-header-inventory", action="store_true",
                     help="skip §4.2 (useful if the makedepend-block parser "
                          "doesn't match your Makefile's format)")
    ap.add_argument("--jobs", type=int, default=0,
                     help="parallel worker processes (default: one per CPU "
                          "core). Use --jobs 1 to force sequential parsing, "
                          "e.g. for debugging a parse failure in isolation.")
    args = ap.parse_args()

    if args.libclang_path:
        clang.cindex.Config.set_library_file(args.libclang_path)

    cc_path = Path(args.compile_commands)
    if not cc_path.exists():
        sys.exit(f"{cc_path} not found")
    entries = json.loads(cc_path.read_text())
    if not entries:
        sys.exit(f"{cc_path} has no entries")

    project_dir = Path(entries[0]["directory"]).resolve()

    symbols: dict[str, dict] = {}
    headers_seen: set[str] = set()
    project_include_dirs: set[str] = set()

    print(f"Parsing {len(entries)} translation unit(s)...")
    if args.exclude:
        print(f"  excluding: {args.exclude}")

    # Build each entry's parse_args up front, sequentially, in the main
    # process -- this is where the single (now cached) -E -v compiler
    # query happens, so it only runs once total regardless of --jobs.
    isystem_cache: dict = {}
    tasks = []
    for entry in entries:
        parse_args = build_parse_args(entry, args.target, args.libclang_path, isystem_cache)
        project_include_dirs |= {a for a in entry["arguments"] if a.startswith("-I")}
        tasks.append({
            "entry": entry,
            "parse_args": parse_args,
            "project_dir": str(project_dir),
            "exclude": args.exclude,
            "libclang_path": args.libclang_path,
        })

    import concurrent.futures
    import os as _os
    workers = args.jobs if args.jobs > 0 else (_os.cpu_count() or 1)

    def handle_result(r: dict) -> None:
        print(".", end="", flush=True)
        if not r["ok"]:
            print(f"\n  ! failed to parse {r['file']}", file=sys.stderr)
            return
        if r["diag_count"]:
            print(f"\n  ! {r['file']}: {r['diag_count']} diagnostic(s), "
                  f"e.g. {r['diag_sample']}", file=sys.stderr)
        symbols.update(r["symbols"])
        headers_seen.update(r["headers_seen"])

    if workers == 1:
        for t in tasks:
            handle_result(_parse_one(t))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_parse_one, t) for t in tasks]
            for future in concurrent.futures.as_completed(futures):
                handle_result(future.result())

    print()  # close out the line of dots

    # ---- §4.3 report ----
    fields = sorted((s for s in symbols.values() if s["kind"] == "field"),
                     key=lambda s: (s["file"], s["line"]))
    globals_ = sorted((s for s in symbols.values() if s["kind"] == "global"),
                       key=lambda s: (s["file"], s["line"]))
    locals_ = sorted((s for s in symbols.values() if s["kind"] == "local"),
                      key=lambda s: (s["file"], s["line"]))

    print()
    print(f"=== [4.3] Declared symbols ({len(symbols)} total) ===")
    print(f"  fields:  {len(fields)}")
    print(f"  globals: {len(globals_)}")
    print(f"  locals:  {len(locals_)}")
    print()
    for label, group in (("FIELD", fields), ("GLOBAL", globals_), ("LOCAL", locals_)):
        for s in group:
            name = f"{s['enclosing']}::{s['name']}" if s["enclosing"] else s["name"]
            print(f"{s['file']}:{s['line']}: {label} '{name}'")

    # ---- §4.2 report ----
    if not args.no_header_inventory:
        include_dirs_relative = [a[2:] for a in project_include_dirs]
        makefile_path = Path(args.makefile)
        if not makefile_path.is_absolute():
            makefile_path = project_dir / makefile_path
        makedepend_headers = parse_makedepend_headers(makefile_path, project_dir)
        makedepend_headers = {h for h in makedepend_headers
                               if not is_excluded(h, project_dir, args.exclude)}
        disk_headers = scan_disk_headers(project_dir, include_dirs_relative)
        disk_headers = {h for h in disk_headers
                         if not is_excluded(h, project_dir, args.exclude)}

        orphans = sorted(disk_headers - headers_seen - makedepend_headers)

        print()
        print("=== [4.2] Header inventory ===")
        print(f"  headers seen via AST includes: {len(headers_seen)}")
        print(f"  headers seen via makedepend block ({makefile_path.name}): "
              f"{len(makedepend_headers)}"
              + ("" if makefile_path.exists() else "  (file not found)"))
        print(f"  headers found on disk:         {len(disk_headers)}")
        if orphans:
            print(f"  {len(orphans)} header(s) on disk but referenced by NEITHER source:")
            for o in orphans:
                print(f"    {o}")
        else:
            print("  no orphan headers found.")


if __name__ == "__main__":
    main()
