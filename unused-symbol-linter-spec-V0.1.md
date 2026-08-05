# Unused Symbol Linter — Design Spec (v0.1)

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

## 5. Phased Implementation Plan

- **Phase 0 — Feasibility spike.** Confirm libclang Python bindings load
  correctly against your specific llvm/mingw install, and can parse a
  trivial `.cpp` with `-I` flags captured from a real `make -Bn` run.
  This is the phase most likely to surface environment friction (see
  §6.1) — worth isolating before writing any analysis logic.
- **Phase 1 — Compile command capture.** `make -Bn` → parsed
  `compile_commands.json`. Verified by hand against a known project.
- **Phase 2 — Symbol harvesting.** Header + file-local declared-symbol
  index, dumped as a sanity-check report (no usage analysis yet) —
  confirms the AST walk finds every struct/field/global you'd expect.
- **Phase 3 — Reference walking + cross-reference.** The actual
  unused-symbol detection, run first against the known-unused `ucase`
  field as a regression check.
- **Phase 4 — Report polish + false-positive triage** on a real project
  (the DOS-era program), since real code will surface edge cases
  (§6.2) that a synthetic test file won't.
- **Phase 5+ (future, not this spec)** — additional PcLint-style rules
  built on the same harvester/walker infrastructure.

## 6. Known Risks / Open Questions

### 6.1 libclang version matching
`libclang`'s Python bindings talk to a specific `libclang.dll`/`.so`
version via ABI, and mismatches between the pip package's expected
version and whatever your llvm/mingw install provides can cause anything
from import errors to silently wrong parses. The Phase 0 spike exists
specifically to catch this early. If needed, `clang.cindex.Config.set_library_file()`
can point the bindings at the LLVM install's `libclang.dll` explicitly
instead of relying on whatever the pip package bundles.

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

## 7. Explicitly Deferred Design Decisions

These are flagged rather than decided now, since they're worth your input
before Phase 2 locks in the data model:
- Output format beyond plain text (e.g. a `.clang-tidy`-style suppression
  file so a known "yes, I know, leave it" field doesn't re-report every
  run)?
- Should file-local variables that are only *written* but never *read*
  count as "used" (PcLint-style dead-store detection is a different,
  harder rule — worth keeping separate from pure unused-symbol detection)?
- Whether struct fields that are only ever memset/memcpy'd as part of the
  whole struct (common in old C-style state structs) should count as
  "used" — arguably they're not *meaningfully* used, but a naive
  reference walker will see the struct-level access and may need a
  judgment call here.
