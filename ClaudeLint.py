#!/usr/bin/env python3
"""
ClaudeLint -- unused struct-field/global/local-variable linter for C/C++
projects, per unused-symbol-linter-spec-V0.4. Grew out of, and now
supersedes, phase2_harvest.py.

§0 Staleness gate (formerly the standalone check_compile_commands_stale.py,
now folded in as an unconditional first step -- see spec §4.1.1): before
touching libclang at all, runs `make -B -n` and diffs it against
compile_commands.json (missing entries, stale entries, drifted flags),
same as the directory-field check that already lived here. On any
mismatch, prints the problem(s) and exits before any parsing begins --
compile_commands.json is still never written/regenerated automatically
either way. check_compile_commands_stale.py itself is unaffected and
still works standalone; this is a superset, not a replacement of it.
Use --skip-stale-check to bypass (e.g. re-running against a JSON you
just hand-verified, or a project with no Makefile-based capture at all).

Full pipeline, all in one pass over compile_commands.json:

  §4.2 Header inventory: cross-checks headers libclang actually included
  while parsing every TU against headers listed in the Makefile's
  makedepend-generated block, flagging any project header referenced
  by neither (a header nothing includes at all).

  §4.3 Declared-symbol harvesting: every FIELD_DECL (with enclosing
  struct/class), every header-scope VAR_DECL ("global"), and every
  file-scope VAR_DECL declared directly in a .cpp/.c ("local").

  §4.4 Reference walking: every TU's full AST (this time NOT skipping
  function bodies -- that's where symbols actually get used) is walked
  for MEMBER_REF_EXPR / DECL_REF_EXPR nodes; each resolves via
  cursor.referenced to a USR, marking that declared symbol "seen".

  §4.5 Report: declared symbols with zero recorded references, minus
  anything in the suppression file, printed as
  `<file>:<line>: unused <kind> '<name>'`, sorted by file then line.

Reuses the Phase 0 recipe for getting a clean parse (matched-version
libclang engine + querying the real compiler's own -isystem dirs) --
see phase0_spike.py / spec §6.1 for why that machinery exists.

Suppression file (default: .claudelint-suppress in the project dir):
    # comment lines and trailing '# ...' comments are ignored
    path/relative/to/project:line
    der_libs/common.h:101

Usage:
    python ClaudeLint.py [--compile-commands PATH] [--libclang-path PATH]
                          [--target TRIPLE] [--exclude PATTERN ...]
                          [--suppressions PATH] [--generate-suppressions PATH]
                          [--makefile PATH] [--no-header-inventory]
                          [--dump-declared] [--jobs N]
                          [--skip-stale-check] [--make-cmd CMD]
"""

import argparse
import fnmatch
import json
import re
import shlex
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


def query_compiler_target(compiler_exe: str) -> str | None:
    """Ask the REAL compiler for its own target triple via -dumpmachine,
    instead of assuming one global --target for every entry in
    compile_commands.json. Same "ask the tool that actually knows"
    principle as query_compiler_isystem_dirs above -- a project can mix
    32-bit and 64-bit (or otherwise differently-targeted) compilers
    across its compile_commands.json entries, and a single hardcoded
    triple silently mis-parses whichever ones don't match it (e.g. a
    32-bit TU parsed as 64-bit loses/renames Windows API symbols that
    are gated on _WIN64, producing spurious "undeclared identifier"
    diagnostics that have nothing to do with the actual source)."""
    try:
        result = subprocess.run(
            [compiler_exe, "-dumpmachine"], capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        return None
    triple = result.stdout.strip()
    return triple or None


def build_parse_args(entry: dict, target: str | None, libclang_path: str | None,
                      isystem_cache: dict | None = None,
                      target_cache: dict | None = None) -> list[str]:
    clang_args = clean_args(entry["arguments"], entry["file"])
    compiler_exe = entry["arguments"][0]

    # Explicit --target always wins (escape hatch for anything the
    # -dumpmachine query gets wrong). Otherwise, resolve per-compiler,
    # since different entries may legitimately use different compilers
    # (e.g. a 32-bit tdm32 g++ and a 64-bit mingw clang++ in the same
    # compile_commands.json).
    resolved_target = target
    if not resolved_target:
        if target_cache is not None and compiler_exe in target_cache:
            resolved_target = target_cache[compiler_exe]
        else:
            resolved_target = query_compiler_target(compiler_exe)
            if target_cache is not None:
                target_cache[compiler_exe] = resolved_target

    if resolved_target:
        clang_args = [f"--target={resolved_target}"] + clang_args

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
# §4.4 reference walking
# ---------------------------------------------------------------------

REFERENCE_KINDS = {CursorKind.MEMBER_REF_EXPR, CursorKind.DECL_REF_EXPR}


def find_references(tu) -> dict[str, list[tuple[str, int]]]:
    """Walk the ENTIRE AST -- deliberately not restricted to any
    container kind, unlike harvest_tu's walk, because references live
    inside function bodies, initializers, sizeof, address-of, etc (spec
    §4.4: "skip nothing"). Returns {usr: [(file, line), ...]} for every
    USR any reference resolved to -- keeping locations (not just a
    boolean set) so --why can show exactly what was counted as a use."""
    seen: dict[str, list[tuple[str, int]]] = {}
    for c in tu.cursor.walk_preorder():
        if c.kind in REFERENCE_KINDS:
            ref = c.referenced
            if ref is None:
                continue
            usr = ref.get_usr()
            if not usr:
                continue
            loc = c.location
            if loc.file:
                seen.setdefault(usr, []).append((str(loc.file), loc.line))
    return seen


# ---------------------------------------------------------------------
# Suppression file (cppcheck .suppress-style: path:line per entry)
# ---------------------------------------------------------------------

def load_suppressions(path: Path, project_dir: Path) -> set[tuple[str, int]]:
    if not path.exists():
        return set()
    out = set()
    for raw_line in path.read_text(errors="replace").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        file_part, _, line_part = line.rpartition(":")
        try:
            line_no = int(line_part.strip())
        except ValueError:
            continue
        full = (project_dir / file_part) if not Path(file_part).is_absolute() else Path(file_part)
        out.add((str(full.resolve()), line_no))
    return out


def is_suppressed(sym: dict, suppressions: set[tuple[str, int]]) -> bool:
    return (str(Path(sym["file"]).resolve()), sym["line"]) in suppressions


def write_suppressions(path: Path, unused: list[dict], project_dir: Path) -> None:
    """--generate-suppressions: dump the CURRENT unused list as a ready-
    to-use suppression file -- the "yes, I know, leave it" baseline
    workflow from spec §7, so existing findings don't need one-by-one
    manual transcription."""
    lines = [
        "# ClaudeLint suppression file",
        "# One entry per line: path/relative/to/project:line",
        "# Generated from a --generate-suppressions run; edit/prune as needed.",
        "",
    ]
    for s in sorted(unused, key=lambda s: (s["file"], s["line"])):
        rel = Path(s["file"]).resolve().relative_to(project_dir).as_posix()
        name = f"{s['enclosing']}::{s['name']}" if s["enclosing"] else s["name"]
        lines.append(f"{rel}:{s['line']}  # {name}")
    path.write_text("\n".join(lines) + "\n")


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
              "symbols": {}, "headers_seen": [], "refs_seen": []}
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
    result["refs_seen"] = find_references(tu)
    return result


# ---------------------------------------------------------------------

def check_directory_field(entries: list[dict], cc_path: Path) -> list[str]:
    """Guards against compile_commands.json (or its whole containing
    folder) being copied from one checkout to another without updating
    the "directory" field. If the old checkout is still intact on disk,
    nothing fails loudly -- ClaudeLint would just silently analyze the
    OLD tree instead of the one you're actually sitting in. See the
    ndir32/ndir64 incident that motivated this check."""
    problems = []
    dirs_in_json = {e["directory"] for e in entries}
    if len(dirs_in_json) > 1:
        problems.append(
            "INCONSISTENT DIRECTORY: compile_commands.json entries don't "
            f"all agree on 'directory' -- found {len(dirs_in_json)} distinct "
            f"values: {sorted(dirs_in_json)}"
        )
        return problems

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
            f"updating this field -- every finding below would silently "
            f"describe the OLD location, not this one."
        )
    return problems


# ---------------------------------------------------------------------
# §0 / §4.1.1 staleness gate -- ported from check_compile_commands_stale.py.
# Read-only with respect to compile_commands.json, same as its standalone
# ancestor: this only ever detects and reports a mismatch, never patches
# or regenerates the file. Kept as a straight port rather than a shared
# import so this file has no dependency on that one (they're deliberately
# independent -- see conversation history for why).
# ---------------------------------------------------------------------

def run_make_dry_run(directory: Path, make_cmd: str) -> str:
    """`make -B -n`: -B forces every rule to be considered out of date so
    every compile command actually prints; -n means nothing is actually
    built. Also surfaces recipe lines even if silenced with a leading
    '@' in the Makefile."""
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


def extract_make_compile_commands(dry_run_output: str) -> dict[str, list[str]]:
    """Scan `make -Bn` output for compile-command lines and pull out
    {source_file: raw_tokens}.

    Deliberately does NOT filter by "does tokens[0] match a compiler
    path already in compile_commands.json" -- that assumption breaks
    whenever compile_commands.json is intentionally pointed at a
    DIFFERENT compiler than the Makefile actually uses (e.g. a
    clang-tidy-friendly compiler substituted in for header-resolution
    reasons, per §4.1.1's "deliberately hand-tuned compiler-path entry"
    carve-out -- this is Derell's actual setup: compile_commands.json
    hardcodes a clang++ path for clang-tidy while the Makefile itself
    may build with a different toolchain, e.g. tdm32 g++). Instead, a
    line is recognized as a compile command purely structurally: it
    contains -c, and exactly one token ends in a recognized source
    extension. A link line has zero such tokens (only .o/.exe), so
    this discriminates cleanly without needing to know the compiler's
    identity up front."""
    found: dict[str, list[str]] = {}
    for line in dry_run_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            tokens = shlex.split(stripped, posix=False)
            tokens = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t
                      for t in tokens]
        except ValueError:
            continue  # unbalanced quotes etc -- not a compile line we can parse
        if not tokens or "-c" not in tokens:
            continue
        source_tokens = [
            t for t in tokens[1:]
            if Path(t).suffix.lower() in SOURCE_EXTENSIONS
        ]
        if len(source_tokens) != 1:
            continue  # 0: not a compile line (e.g. a link step). >1: ambiguous.
        found[source_tokens[0]] = tokens
    return found


def normalize_make_flags(tokens: list[str], source_file: str) -> set[str]:
    """Strip compiler exe, -c, -o <out>, and the source filename, leaving
    just the real build-configuration flags for comparison. Order-
    insensitive on purpose -- Makefile variable expansion can reorder
    flags harmlessly."""
    cleaned = []
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if i == 0:
            continue  # compiler exe -- exempted from drift-checking
        if tok in DROP_FLAGS_NO_ARG:
            continue
        if tok in DROP_FLAGS_WITH_ARG:
            skip_next = True
            continue
        if tok == source_file:
            continue
        cleaned.append(tok)
    return set(cleaned)


def check_compile_commands_stale(entries: list[dict], directory: Path, make_cmd: str) -> list[str]:
    """The former check_compile_commands_stale.py's main comparison,
    minus the directory-field check (that's handled separately by
    check_directory_field, which runs first and gates this). Returns a
    list of human-readable problem strings; empty means "matches"."""
    json_by_file = {e["file"]: e for e in entries}

    print(f"Checking compile_commands.json against '{make_cmd} -B -n' in {directory} ...")
    dry_run_output = run_make_dry_run(directory, make_cmd)
    makefile_by_file = extract_make_compile_commands(dry_run_output)

    if not makefile_by_file:
        dry_lines = [ln for ln in dry_run_output.splitlines() if ln.strip()]
        print("No compile commands recognized in `make -B -n` output.", file=sys.stderr)
        print("A line is recognized as a compile command if it contains "
              "-c and exactly one token ends in a recognized source "
              "extension (.c/.cpp/.cc/.cxx) -- unrelated to the compiler "
              "path recorded in compile_commands.json. Diagnostics:",
              file=sys.stderr)
        print(f"  first {min(10, len(dry_lines))} non-blank line(s) of "
              f"'{make_cmd} -B -n' output ({len(dry_lines)} total):", file=sys.stderr)
        for ln in dry_lines[:10]:
            print(f"    {ln!r}", file=sys.stderr)
        if not dry_lines:
            print("  (dry-run output was completely empty -- check that "
                  f"'{make_cmd} -B -n' run manually in {directory} actually "
                  "produces recipe lines for the default target)", file=sys.stderr)
        sys.exit(1)

    problems: list[str] = []

    missing_from_json = sorted(set(makefile_by_file) - set(json_by_file))
    for f in missing_from_json:
        problems.append(f"MISSING FROM JSON: '{f}' is built by the Makefile "
                         f"but has no entry in compile_commands.json")

    stale_in_json = sorted(set(json_by_file) - set(makefile_by_file))
    for f in stale_in_json:
        entry = json_by_file[f]
        full_path = Path(entry["directory"]) / f
        if not full_path.exists():
            problems.append(f"STALE ENTRY: '{f}' is in compile_commands.json but the "
                             f"file no longer exists on disk ({full_path})")
        else:
            problems.append(f"STALE ENTRY: '{f}' is in compile_commands.json but the "
                             f"Makefile no longer builds it")

    for f in sorted(set(makefile_by_file) & set(json_by_file)):
        makefile_flags = normalize_make_flags(makefile_by_file[f], f)
        json_flags = normalize_make_flags(json_by_file[f]["arguments"], f)
        added = makefile_flags - json_flags
        removed = json_flags - makefile_flags
        if added or removed:
            detail = []
            if added:
                detail.append(f"Makefile has but JSON lacks: {sorted(added)}")
            if removed:
                detail.append(f"JSON has but Makefile lacks: {sorted(removed)}")
            problems.append(f"FLAGS DIFFER for '{f}': " + "; ".join(detail))

    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compile-commands", default="compile_commands.json")
    ap.add_argument("--libclang-path", default=r"D:\clang-22.1.8\bin\libclang.dll")
    ap.add_argument("--target", default=None,
                     help="force this target triple for EVERY compile_commands.json "
                          "entry, overriding auto-detection. Default: auto-detect "
                          "per entry by running that entry's own compiler with "
                          "-dumpmachine, so mixed 32-/64-bit (or otherwise mixed) "
                          "toolchains across entries resolve independently and "
                          "correctly. Only pass this if auto-detection is wrong "
                          "for your setup.")
    ap.add_argument("--makefile", default="Makefile")
    ap.add_argument("--exclude", action="append", default=[],
                     help="path (dir prefix or fnmatch glob) to exclude from "
                          "symbol harvesting and header inventory entirely, "
                          "relative to the project directory (e.g. der_libs, "
                          "or der_libs/*, or *.legacy.h). Repeatable.")
    ap.add_argument("--no-header-inventory", action="store_true",
                     help="skip §4.2 (useful if the makedepend-block parser "
                          "doesn't match your Makefile's format)")
    ap.add_argument("--suppressions", default=".claudelint-suppress",
                     help="path to the suppression file (path:line per "
                          "entry), relative to the project dir unless "
                          "absolute. Missing file = no suppressions.")
    ap.add_argument("--generate-suppressions", metavar="PATH",
                     help="instead of reporting, write the CURRENT unused "
                          "list to PATH in suppression-file format and "
                          "exit -- a baseline 'yes I know, leave it' file "
                          "you can then hand-edit.")
    ap.add_argument("--dump-declared", action="store_true",
                     help="also print the full §4.3 declared-symbol dump "
                          "(all fields/globals/locals, used or not) -- "
                          "useful for debugging the harvester itself.")
    ap.add_argument("--why", metavar="NAME",
                     help="debug: show every reference the walker recorded "
                          "for symbol NAME (bare name or Struct::field), "
                          "instead of the normal report. Ground truth for "
                          "'why did/didn't this get flagged?'")
    ap.add_argument("--jobs", type=int, default=0,
                     help="parallel worker processes (default: one per CPU "
                          "core). Use --jobs 1 to force sequential parsing, "
                          "e.g. for debugging a parse failure in isolation.")
    ap.add_argument("--skip-stale-check", action="store_true",
                     help="skip the §4.1.1 staleness gate (make -B -n vs "
                          "compile_commands.json) that otherwise runs "
                          "unconditionally before any parsing. Use for a "
                          "JSON you've already hand-verified, or a project "
                          "with no Makefile-based capture at all.")
    ap.add_argument("--make-cmd", default="make",
                     help="make executable to use for the staleness gate "
                          "(default: make). Try mingw32-make etc. if "
                          "that's what your toolchain provides. Ignored "
                          "if --skip-stale-check is passed.")
    args = ap.parse_args()

    if args.libclang_path:
        clang.cindex.Config.set_library_file(args.libclang_path)

    cc_path = Path(args.compile_commands)
    if not cc_path.exists():
        sys.exit(f"{cc_path} not found")
    entries = json.loads(cc_path.read_text())
    if not entries:
        sys.exit(f"{cc_path} has no entries")

    dir_problems = check_directory_field(entries, cc_path)
    if dir_problems:
        print(f"{len(dir_problems)} problem(s) found -- refusing to run:")
        for p in dir_problems:
            print(f"  - {p}")
        print()
        print("Fix compile_commands.json's \"directory\" field(s) by hand, then re-run.")
        sys.exit(1)

    project_dir = Path(entries[0]["directory"]).resolve()

    if not args.skip_stale_check:
        stale_problems = check_compile_commands_stale(entries, project_dir, args.make_cmd)
        if stale_problems:
            print(f"{len(stale_problems)} problem(s) found -- compile_commands.json is STALE:")
            for p in stale_problems:
                print(f"  - {p}")
            print()
            print("compile_commands.json was NOT modified. Update it by hand, then re-run.")
            print("(or pass --skip-stale-check to bypass this gate)")
            sys.exit(1)
        print("compile_commands.json matches the Makefile -- proceeding.")
        print()

    symbols: dict[str, dict] = {}
    headers_seen: set[str] = set()
    refs_seen: dict[str, list[tuple[str, int]]] = {}
    project_include_dirs: set[str] = set()

    print(f"Parsing {len(entries)} translation unit(s)...")
    if args.exclude:
        print(f"  excluding: {args.exclude}")

    # Build each entry's parse_args up front, sequentially, in the main
    # process -- this is where the single (now cached) -E -v compiler
    # query happens, so it only runs once total regardless of --jobs.
    isystem_cache: dict = {}
    target_cache: dict = {}
    tasks = []
    for entry in entries:
        parse_args = build_parse_args(entry, args.target, args.libclang_path,
                                       isystem_cache, target_cache)
        project_include_dirs |= {a for a in entry["arguments"] if a.startswith("-I")}
        tasks.append({
            "entry": entry,
            "parse_args": parse_args,
            "project_dir": str(project_dir),
            "exclude": args.exclude,
            "libclang_path": args.libclang_path,
        })

    if args.target:
        print(f"  target (forced via --target): {args.target}")
    elif target_cache:
        print("  target (auto-detected via -dumpmachine):")
        for compiler_exe, triple in target_cache.items():
            print(f"    {compiler_exe} -> {triple or '(unknown -- no --target applied)'}")

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
        for usr, locs in r["refs_seen"].items():
            refs_seen.setdefault(usr, []).extend(locs)

    if workers == 1:
        for t in tasks:
            handle_result(_parse_one(t))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_parse_one, t) for t in tasks]
            for future in concurrent.futures.as_completed(futures):
                handle_result(future.result())

    print()  # close out the line of dots

    if args.why:
        target = args.why.strip()
        matches = []
        for usr, s in symbols.items():
            disp = f"{s['enclosing']}::{s['name']}" if s["enclosing"] else s["name"]
            if disp == target or s["name"] == target:
                matches.append((usr, s, disp))
        if not matches:
            print(f"No declared symbol matching '{target}' found.")
            return
        for usr, s, disp in matches:
            print(f"=== {disp} ===")
            print(f"  declared: {s['file']}:{s['line']}  (kind={s['kind']})")
            print(f"  usr:      {usr}")
            locs = refs_seen.get(usr, [])
            if not locs:
                print(f"  references: none found -- would be reported as unused.")
            else:
                print(f"  references: {len(locs)} found:")
                for f, ln in sorted(set(locs)):
                    print(f"    {f}:{ln}")
        return

    # ---- §4.3 declared-symbol dump (optional, --dump-declared) ----
    if args.dump_declared:
        fields = sorted((s for s in symbols.values() if s["kind"] == "field"),
                         key=lambda s: (s["file"], s["line"]))
        globals_ = sorted((s for s in symbols.values() if s["kind"] == "global"),
                           key=lambda s: (s["file"], s["line"]))
        locals_ = sorted((s for s in symbols.values() if s["kind"] == "local"),
                          key=lambda s: (s["file"], s["line"]))
        print(f"=== [4.3] Declared symbols ({len(symbols)} total) ===")
        print(f"  fields:  {len(fields)}")
        print(f"  globals: {len(globals_)}")
        print(f"  locals:  {len(locals_)}")
        print()
        for label, group in (("FIELD", fields), ("GLOBAL", globals_), ("LOCAL", locals_)):
            for s in group:
                name = f"{s['enclosing']}::{s['name']}" if s["enclosing"] else s["name"]
                print(f"{s['file']}:{s['line']}: {label} '{name}'")
        print()

    # ---- §4.4/§4.5: cross-reference and report ----
    unused = [s for usr, s in symbols.items() if usr not in refs_seen]
    unused.sort(key=lambda s: (s["file"], s["line"]))

    if args.generate_suppressions:
        out_path = Path(args.generate_suppressions)
        write_suppressions(out_path, unused, project_dir)
        print(f"Wrote {len(unused)} suppression entry(ies) to {out_path}")
        return

    suppressions_path = Path(args.suppressions)
    if not suppressions_path.is_absolute():
        suppressions_path = project_dir / suppressions_path
    suppressions = load_suppressions(suppressions_path, project_dir)
    suppressed_count = sum(1 for s in unused if is_suppressed(s, suppressions))
    reported = [s for s in unused if not is_suppressed(s, suppressions)]

    print(f"=== [4.5] Unused symbols ===")
    if suppressions:
        print(f"  ({suppressed_count} suppressed via {suppressions_path.name}, "
              f"{len(reported)} shown)")
    for s in reported:
        name = f"{s['enclosing']}::{s['name']}" if s["enclosing"] else s["name"]
        print(f"{s['file']}:{s['line']}  # unused {s['kind']} '{name}'")
    if not reported:
        print("  none found.")
    else:
        print(f"  {len(reported)} unused symbol(s) found.")

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
