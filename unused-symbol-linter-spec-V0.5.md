# Unused Symbol Linter — Design Spec (v0.5, v1 COMPLETE)

## 1. Problem Statement

PcLint could flag struct fields and variables that are declared but never
referenced anywhere in a codebase. No currently-maintained free tool does
this reliably:

- **clang-tidy** — `readability-*`, `cppcoreguidelines-*`, and full
  `clang-analyzer-*` were all tested. None flag unused struct members.
  clang-tidy's unused-* checks target local variables, RAII objects, and
  return values — not struct/class data members or file-scope globals.
- **cppcheck** — same gap; its `unusedFunction`/`unusedVariable` checks
  don't extend to struct members.
- **Compiler warnings** (`-Wall`, `-Wunused-variable`) — only cover local
  variables in a single translation unit, not cross-TU field usage.

The gap exists because this is fundamentally a **whole-program, cross-TU**
question: a struct declared in a header could theoretically be touched from
any `.cpp` file that includes it, so "is this field used" can't be answered
by looking at one file in isolation. That's exactly the kind of problem an
AST-based tool with real project knowledge is good at, and exactly what
none of the above tools attempt.

## 2. Goals

**v1 scope (this spec):**
1. Discover the project's source and header files from the Makefile.
2. Parse the whole project into ASTs with correct include paths/defines.
3. Build an index of "declared symbols" from headers: struct/class fields,
   global variables, (member functions are explicitly **out of scope** for
   v1 — see §7).
4. Build an index of file-local (`static`, or file-scope non-`extern`)
   variables declared directly in `.cpp`/`.c` files.
5. Search every translation unit's AST for references to each symbol.
6. Report: symbol name, declaring file:line, kind (field/global/local),
   "never referenced" — sorted by file.

**Explicit non-goals for v1** (noted so scope doesn't creep silently):
- Unused member *functions* (different reference-finding rules — virtual
  dispatch, overrides, function pointers taken).
- Unused `#define` macros.
- Dead code paths / unreachable code.
- Anything requiring control-flow or data-flow analysis (that's a much
  bigger project than "is this symbol name referenced anywhere").

Future rules (post-v1) will reuse the same infrastructure (§4) — the AST
index and cross-TU reference search are the reusable core; each new rule
is mostly a new "declared symbols of interest" query plus a new "what
counts as a reference" predicate.

## 3. Technology Stack

- **Python 3.14** (already installed).
- **libclang Python bindings** (`pip install libclang`, or the bindings
  bundled with an LLVM install — see §6.1 for the version-matching
  concern). This gives us a real, standards-compliant C++ front end for
  free instead of writing a parser.
- No dependency on CMake or `compile_commands.json` generators — see §4.1.

## 4. Architecture

```
Makefile ──► make -Bn dry run ──► compile command capture
                                        │
                                        ▼
                              compile_commands.json
                                        │
                                        ▼
                        libclang: parse every TU (per file)
                                        │
                    ┌───────────────────┴───────────────────┐
                    ▼                                        ▼
        Header symbol harvester                    Reference walker
   (structs/classes + fields,                 (visits every TU's AST,
    file-scope globals per file)               records every DeclRefExpr /
                    │                           MemberRefExpr, resolved
                    │                           by USR — see §4.4)
                    └───────────────────┬───────────────────┘
                                        ▼
                              Cross-reference & report
                        (declared symbols with zero
                         recorded references → flag)
```

### 4.1 Getting compile flags: `make -Bn`, not Makefile parsing

Rather than re-implementing Make's variable expansion (`$(VAR)`, pattern
rules, `include` of the generated `makedepend` block, etc.), the tool
invokes:

```
make -Bn
```

(`-B`/`--always-make` forces every rule to be considered "out of date" so
every compile command actually prints; `-n`/`--dry-run` means nothing is
actually built.) This prints the exact command line for every compile
step Make would run — same compiler, same `-I` paths, same `-D` defines,
same C++ standard flag — without the tool needing to understand Make
syntax at all. Bear and similar `compile_commands.json` generators use
the same trick (they typically intercept exec calls instead, but output
capture is simpler to implement and sufficient here since nothing is
actually being compiled).

The tool parses each compiler-invocation line into an entry of a
standard `compile_commands.json` (directory, file, arguments) — a format
libclang already knows how to consume via
`clang.cindex.CompilationDatabase`.

**Consequence:** we don't need `makedepend`'s header-dependency list to
figure out the *build graph*, since libclang resolves `#include`s itself
using the `-I` paths captured from the dry run, and will parse the real
header content. `makedepend`'s output could still be a useful secondary
check (§4.2) — e.g. to catch a header that's on disk but never actually
`#include`d by anything, which the AST pass alone wouldn't reveal.

### 4.1.1 Staleness detection is read-only — never auto-regenerate

`compile_commands.json` is not treated as a build artifact the tool is
free to (re)generate on its own. Once created, the tool will **detect**
staleness against the Makefile (e.g. a `.cpp` added to the Makefile
since the file was last generated, or a source file listed in the JSON
that no longer exists) but will **never silently overwrite or patch the
file itself**.

On detecting a mismatch, the tool must:
1. Stop before doing any parsing/analysis that depends on the stale file.
2. Print an explicit error identifying the mismatch — e.g. which
   Makefile-listed sources are missing from `compile_commands.json`,
   and/or which `compile_commands.json` entries no longer correspond to
   a file the Makefile builds.
3. Leave `compile_commands.json` untouched and exit, requiring the user
   to update it by hand before re-running.

**Why this matters more than the usual "don't clobber user files"
caution:** `compile_commands.json` is consumed by other tools besides
this linter — notably `clang-tidy`, which resolves its own toolchain
from the compiler entry in each command entry rather than necessarily
using the Makefile's actual compiler. If this tool silently regenerated
the file from a fresh `make -Bn` capture, it could overwrite a
deliberately hand-tuned toolchain pointer that `clang-tidy` depends on,
with no warning. So a stale-vs-current mismatch is surfaced as a
blocking error requiring manual edit, not something the tool resolves
on the user's behalf — the same posture applies even if `-Bn` capture
(§4.1) is later re-run interactively; it must not write over an existing
`compile_commands.json` without the user explicitly requesting that
specific action.

### 4.2 Header inventory

Two sources, cross-checked:
- Every header libclang actually includes while parsing a TU (from the
  AST — reliable, reflects real usage).
- The `makedepend`-generated block at the end of the Makefile (a second,
  independent listing) — used to flag headers that exist in `.` or
  `.\der_libs` but never appear in either list, as a sanity check that
  nothing was missed by the compile-command capture.

### 4.3 Declared-symbol harvesting

Walk each header's top-level AST cursor. For every `STRUCT_DECL` /
`CLASS_DECL`, recurse into its children and record every `FIELD_DECL`
(name, USR, declaring file:line, enclosing struct name). For every
`VAR_DECL` at file (translation-unit) scope in a header, record it as a
global. For `.cpp`/`.c` files specifically, record `VAR_DECL`s at file
scope as file-local candidates (whether or not marked `static` — an
un-`static` file-scope variable in a `.cpp` that's never referenced
elsewhere is just as much a candidate).

A symbol's **USR** (libclang's Unified Symbol Resolution string) is the
key used everywhere below — it's how the same declaration seen from two
different TUs (via a shared header) is recognized as "the same symbol."

### 4.4 Reference walking

For every TU, walk the full AST recursively (skip nothing — a reference
can appear anywhere: initializer, sizeof, address-of, template argument).
For each `MEMBER_REF_EXPR`, resolve `cursor.referenced` and record its
USR as "seen." For each `DECLREF_EXPR`, same. Cross off every USR seen
against the declared-symbol table from §4.3.

### 4.5 Report

Plain-text (v1) report grouped by file, each line:
```
<file>:<line>: unused <field|global|local> '<Struct>::<field>' (or '<name>')
```
Sorted by file then line, so it reads like a compiler warning list and
slots into an editor's "jump to error" workflow the same way clang-tidy
output does.

## 5. Phased Implementation Plan — v1 COMPLETE

All phases below are done. The implementation lives in `ClaudeLint.py`
(the tool grew past the original phase-script names; `phase0_spike.py`
and `phase2_harvest.py` are its predecessors, superseded), plus
`check_compile_commands_stale.py` as a standalone companion tool for
§4.1.1. See §6 for lessons learned along the way.

- **Phase 0 — Feasibility spike. ✅ DONE** — see §6.1 for the full
  story; resolved via a version-matched standalone Clang 22 install
  used purely as the `libclang.dll` engine, combined with `-isystem`
  paths queried live from the real project toolchain. Confirmed clean
  (zero diagnostics) against `cmd_line.cpp` and `Filelist.cpp`.
  *(Original phase goal, retained for reference: confirm libclang
  Python bindings load correctly against the project's llvm/mingw
  install, and can parse a real `.cpp` with flags captured from
  `compile_commands.json`. This turned out to be the phase most likely
  to surface environment friction — worth having isolated it before
  writing any analysis logic.)*
- **Phase 1 — Compile command capture. ✅ DONE** — `compile_commands.json`
  was already hand-maintained alongside the Makefile; rather than a
  `make -Bn`-capture tool, this phase's real deliverable ended up being
  `check_compile_commands_stale.py`, a read-only checker (never writes
  the file, per §4.1.1) comparing it against a fresh `make -B -n` dry
  run — catching missing files, stale entries, and drifted flags.
  Later extended (see §6.3) to also catch a wrong `directory` field.
- **Phase 2 — Symbol harvesting. ✅ DONE** — header + file-local
  declared-symbol index (§4.3), plus §4.2's header inventory bundled
  into the same pass (both need the same per-TU AST walk). The header-
  inventory side effect turned out to be a genuinely useful standalone
  feature (catching e.g. `keycodes32.h`, dead since the DOS era) and
  was kept as permanent output rather than a sanity-check throwaway.
- **Phase 3 — Reference walking + cross-reference. ✅ DONE** — full,
  unrestricted AST walk (§4.4, "skip nothing" — deliberately not
  container-restricted like §4.3's harvesting walk, since references
  live inside function bodies) resolving `MEMBER_REF_EXPR`/
  `DECL_REF_EXPR` via `cursor.referenced` to a USR. Regression-tested
  against the known-unused `ucase` field as planned — see §6.3 for how
  that regression check itself caught a serious unrelated bug.
- **Phase 4 — Report polish + false-positive triage. ✅ DONE** on the
  real project. First trustworthy real run found exactly 5 unused
  fields; 2 (`_SCSI_PASS_THROUGH_WITH_BUFFERS::Filler`/`SenseBuf`, in a
  vendored SDK header) were legitimate padding/alignment fields,
  suppressed via the suppression file rather than flagged as bugs —
  confirming §6.2's "external/vendored header" caveat was a real,
  not just theoretical, concern. The other 3 (`ucase`, `low_ascii`,
  `unused1`) were genuine dead fields, one of them (`low_ascii`)
  previously unknown.
- **Phase 5+ (future, not this spec)** — additional PcLint-style rules
  built on the same harvester/walker infrastructure. Unused *member
  functions* specifically (out of v1 scope per §2/§7) are already
  covered by existing tools (clang-tidy/cppcheck) elsewhere in the
  project's toolchain, once those tools are also told to exclude
  `der_libs` — not a gap ClaudeLint needs to fill.

## 6. Known Risks / Open Questions

### 6.1 libclang version matching — RESOLVED (Phase 0, confirmed working)

`libclang`'s Python bindings talk to a specific `libclang.dll`/`.so`
version via ABI, and mismatches between the pip package's expected
version and whatever your llvm/mingw install provides can cause anything
from import errors to silently wrong parses. This section originally
flagged the risk in the abstract; Phase 0 hit it in a concrete and
fairly severe form, and the fix below is confirmed working against two
real project files (`cmd_line.cpp`, `Filelist.cpp`) with zero
diagnostics.

**What went wrong, in order of discovery:**

1. **`pip install libclang` is not a thin binding — it bundles its own
   prebuilt `libclang.dll`.** This is the whole point of that package
   (no system LLVM required), but it means the parsing engine is
   whatever version the PyPI maintainer last built (18.1.1 at the time
   of this work) — completely independent of, and normally much older
   than, a hand-installed llvm/mingw toolchain.
2. **Without an explicit `--target`, libclang doesn't know to look like
   the project's cross-compiler.** On Windows it fell back to
   auto-detecting a host toolchain and picked up an unrelated Visual
   Studio installation's MSVC STL headers — nothing to do with the
   project's actual `d:/llvm` mingw toolchain at all. Fix: always pass
   `--target=<the same triple the project's compiler targets>` (here,
   `x86_64-w64-mingw32`) explicitly in `args`.
3. **Once targeted correctly, headers ARE at least the right family —
   but "file not found" for `windows.h`.** The pip libclang has no
   knowledge of where the real toolchain's own mingw sysroot lives
   (there's no reason it would; it's a separate installation). Fix:
   don't hand-maintain `-isystem` paths (this is the rabbit hole that
   burned time on this project before, via `compiledb`). Instead, query
   the *real* compiler for its own resolved header search paths and
   feed those to libclang verbatim:
   ```
   <real-compiler-exe> -E -v -x c++ <same -D/-I flags> -
   ```
   parsing the `#include <...> search starts here:` … `End of search
   list.` block out of stderr. This is the same principle as `make -Bn`
   (§4.1) applied to header resolution — ask the tool that actually
   knows the answer, rather than reimplementing its logic.
4. **Fixed the missing-header problem, but surfaced the real one: an
   engine/headers *version* mismatch.** The real toolchain turned out
   to be a very new Clang (major version 22 — a trunk/nightly-class
   build, not a numbered public release at the time of this work), and
   its own libc++ headers `static_assert`'d that they require "Clang 20
   or later." Feeding those headers to the pip package's Clang-18
   engine tripped that assert — a real check, correctly firing, just
   against the wrong engine. Downgrading only the *headers* (or only
   the *engine*) doesn't fix this: **the parsing engine and the
   resource-dir/standard-library headers it reads must come from the
   version-matched build, full stop.** No amount of `-isystem`
   substitution bridges an engine/header version gap — that trick only
   works for filesystem-location mismatches (step 3 above), not
   version mismatches.
5. **First attempt at a matched pair (installing a standalone Clang
   18.1.8) fixed the symptom but not the disease.** It resolved the
   intrinsic-header errors (`avx512intrin.h` etc., which do match
   engine version) but that specific official Windows release doesn't
   bundle its own libc++ — meaning without an override it would have
   fallen back to the host's MSVC STL, which *also* now requires Clang
   20+. Both realistic standard-library options on a modern Windows
   dev box had already moved their minimum supported Clang version
   past 18. Lesson: matching the pip package's version is the wrong
   target to aim for; matching **the real toolchain's** version is what
   actually matters, since that's whose headers you need to read.
6. **Final fix: install a standalone Clang matching the real
   toolchain's major version** (`clang+llvm-22.1.8-x86_64-pc-windows-msvc.tar.xz`
   from the official LLVM GitHub releases — the `clang+llvm-` archive,
   not the `LLVM-*.exe` installer, since the archive is documented as a
   superset that includes libclang/library artifacts the plain
   installer may omit) and point
   `clang.cindex.Config.set_library_file()` at *that* build's
   `libclang.dll`.

**Resulting rule, confirmed working:** two independent header sets are
in play, and they have two independent correctness requirements —
1. **Resource-dir / builtin / standard-library headers**
   (`lib/clang/<ver>/include`, and libc++'s own headers if the engine
   bundles them) — these are gated by `__clang_major__` checks baked
   into the headers themselves, and MUST come from the exact same build
   as the parsing engine (`libclang.dll`). Locate this directory
   relative to wherever `libclang.dll` was installed
   (`<install-root>/lib/clang/<ver>/include`), not relative to the
   project's own toolchain.
2. **Target sysroot headers** (mingw's own `windows.h`, CRT headers,
   and — in this project's case — the real toolchain's own libc++,
   since that's what the project actually links against) — these are
   NOT engine-version-gated in the same way, and should be borrowed
   directly from the real project toolchain via the `-E -v` query in
   step 3 above. Explicitly exclude that query's own
   `lib/clang/<ver>/include` entry (that's the real toolchain's
   resource-dir, which must NOT be mixed with a different build's
   engine) and any bare directory that duplicates an existing `-I`
   flag already in the compile command.

Two separate Clang 22 installations were required — not because the
version needs to be one specific value, but because the parsing engine
(`libclang.dll`) and its own resource-dir must be self-consistent
(same build), while the sysroot/libc++ headers must match the real
project toolchain (a *different* build, also happening to be Clang 22
in this case). If the real toolchain had been an older, stable numbered
release instead of a trunk build, the pip package's bundled engine
might have matched it well enough to need no second install at all —
this two-install setup is a consequence of the real toolchain being
unusually new, not an inherent requirement of the approach.

The Phase 0 spike script (`phase0_spike.py`, not part of this repo)
implements the full recipe: `-Bn`-style querying of the real compiler's
search paths, filtering out its resource-dir entry, and appending the
matched-engine's own resource-dir in its place.

### 6.2 Expected false positives / negatives to watch for
- **Aggregate/designated initializers** (`Foo f = { .x = 1 };` or
  positional `{1, 2, 3}`) may not always produce a `MemberRefExpr` the
  same way a normal `f.x = 1;` does — worth verifying explicitly in
  Phase 3, since this is exactly the kind of member-access pattern an
  old C-style state struct is likely to use heavily.
- **Macros** — libclang works on the post-preprocessor token stream for
  most reference-resolution purposes, so straightforward macro use should
  be transparent, but this is worth a targeted test case rather than an
  assumption.
- **Unions / anonymous structs and unions** need explicit handling in the
  harvester (§4.3), since anonymous members don't have a normal enclosing
  name path.
- **Bitfields** are still `FIELD_DECL`s and should just work, but worth a
  test case since PcLint had known quirks there.
- **External use** — if any header in this project is ever consumed by
  code outside what the Makefile builds (unlikely given the current
  single-project setup, but worth confirming), a field could show as
  "unused" while actually being part of a public API used elsewhere. Not
  a v1 concern unless you tell me otherwise.

### 6.3 The `directory` field silently surviving a folder copy —
    RESOLVED (guard added to both tools)

Discovered *after* Phase 3 appeared to be working: `compile_commands.json`
carries an **absolute** `directory` path (required — see below for why
it can't be relative). When the whole project folder was copied
wholesale to set up a new working copy (`ndir32` → `ndir64`), the copied
`compile_commands.json` kept pointing at the OLD absolute path. The old
folder was still intact on disk, so nothing failed loudly — every tool
that trusted `directory` (ClaudeLint, the Phase 0 spike, and the
staleness checker's own default `--directory`) silently parsed and
reported on the stale `ndir32` tree while the person was confidently
working in, and looking at, `ndir64`.

This is what actually caused the `ucase` regression check (§5, Phase 3)
to appear to fail: `ucase` genuinely was still referenced — just in the
stale copy, not the current one. The regression check did its job
correctly; the input was wrong. Root-caused via a `--why <symbol>` debug
flag added to ClaudeLint specifically to make "what did the tool think
justified this verdict" answerable directly, rather than guessed at —
worth keeping as a standing feature, not a one-off debugging hack.

**Why `directory` can't just be relative (e.g. `"."`) to sidestep this:**
`clang-tidy`, `cppcheck`, and `clangd` are routinely invoked from
somewhere other than the project root (CI runners, editor background
processes, build wrappers), and they resolve every relative path in
`arguments`/`file` against `directory`, not against their own process's
cwd — that indirection is the whole point of the field. Making it
relative would trade one rare, loud bug (a stale absolute path,
one-time, catchable) for a common, silent one (every invocation from a
non-project-root directory misresolving paths, invocation-context-
dependent, much harder to notice).

**Fix:** both `ClaudeLint.py` and `check_compile_commands_stale.py` now
check, before doing anything else, that (a) all entries agree on
`directory`, and (b) that directory matches where `compile_commands.json`
itself actually resides on disk (`cc_path.resolve().parent`) — not the
invoking shell's cwd, since that's a more fundamental, invocation-
independent invariant. `check_compile_commands_stale.py` refuses to even
run `make -Bn` on mismatch (a dry-run result against a suspect directory
isn't trustworthy anyway); `ClaudeLint.py` refuses to parse anything.

**Standing workflow lesson:** treat `compile_commands.json` as tied to
one specific checkout location, not something that travels with a
folder copy — regenerate it fresh (§4.1, `make -Bn` capture) whenever
the project directory is copied or relocated, the same way you would
for any other build-system cache/config file with absolute paths baked
in (this class of bug isn't unique to this file).

## 7. Explicitly Deferred Design Decisions

These were flagged during initial design; resolved status noted inline.
- ~~Output format beyond plain text (e.g. a suppression file so a known
  "yes, I know, leave it" field doesn't re-report every run)?~~
  **RESOLVED**: `.claudelint-suppress`, cppcheck-`.suppress`-style
  `path:line` entries (`#` comments supported, full-line or trailing).
  `--generate-suppressions PATH` dumps the current unused list as a
  ready-to-edit baseline file. In real use: suppressed 2 legitimate
  padding fields in a vendored SDK header (`scsi_defs.h`) without
  hiding them from future re-review the way a blanket `--exclude` would.
- Should file-local variables that are only *written* but never *read*
  count as "used" (PcLint-style dead-store detection is a different,
  harder rule — worth keeping separate from pure unused-symbol
  detection)? **Still open** — v1's reference walker deliberately
  doesn't distinguish read from write (any `MEMBER_REF_EXPR`/
  `DECL_REF_EXPR` counts as "used"). Flagged as a possible
  `--detect-dead-stores`-style opt-in for a future version, not
  needed for v1's real findings so far.
- Whether struct fields that are only ever memset/memcpy'd as part of the
  whole struct (common in old C-style state structs) should count as
  "used" — arguably they're not *meaningfully* used, but a naive
  reference walker will see the struct-level access and may need a
  judgment call here. **Still open** — no case hit yet in real usage;
  revisit if a future run's findings look suspicious in this specific way.
- Unused member *functions* (§2 v1 non-goal). **RESOLVED as staying
  out of scope**: already covered by existing tools elsewhere in the
  project's toolchain (clang-tidy/cppcheck), once those are also
  pointed away from `der_libs` the same way ClaudeLint's `--exclude`
  is. No gap for ClaudeLint to fill here.
