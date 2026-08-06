# ClaudeLint

A python-based whole-program, cross-translation-unit linter for one specific, stubborn
gap: **unused struct/class fields and unused global/file-scope variables
in C/C++ projects.**

## The gap this fills

No currently-maintained free tool catches this reliably:

- **clang-tidy**'s `readability-*`, `cppcoreguidelines-*`, and
  `clang-analyzer-*` checks target local variables, RAII objects, and
  return values — not struct/class data members or file-scope globals.
- **cppcheck**'s `unusedFunction`/`unusedVariable` checks don't extend
  to struct members either.
- **Compiler warnings** (`-Wall`, `-Wunused-variable`) only see one
  translation unit at a time, so they can't tell whether a field
  declared in a shared header is *ever* touched from *any* `.cpp` that
  includes it.

That last point is the real reason this is hard: "is this field used"
is a **whole-program** question. A field in a header could be touched
from any `.cpp` that includes it, so no single-file tool can answer it
correctly. ClaudeLint parses the whole project into ASTs (via
`libclang`) and checks every translation unit before calling a symbol
unused.

## What it does, and doesn't, check

**Checks:** struct/class fields, header-scope global variables, and
file-scope variables declared directly in a `.cpp`/`.c` (`static` or
not). A symbol is "unused" if it's declared somewhere in the project
and never appears in a `MEMBER_REF_EXPR` or `DECL_REF_EXPR` anywhere in
any translation unit.

**Deliberately out of scope:** unused member *functions* (different
reference rules — virtual dispatch, overrides, function pointers),
unused `#define` macros, dead code paths, and anything needing real
data-flow analysis. It also doesn't currently distinguish a field
that's *written* but never *read* from one that's genuinely used —
any reference counts, read or write.

**Bonus, for free:** since building the project's real AST requires
knowing which headers actually get `#include`d, ClaudeLint also reports
project headers that are never included by anything at all — dead
headers, not just dead fields.

## How it works, briefly

1. Reads `compile_commands.json` for the real compiler flags per file
   (no flag-guessing).
2. Parses every translation unit with `libclang`, using a
   version-matched parsing engine (see the `pip install libclang` note
   at the bottom — this matters more than it sounds like it should).
3. Walks every header/TU for declared fields and globals.
4. Walks every TU's *entire* AST (including inside function bodies —
   that's where symbols actually get used) recording every reference.
5. Reports declared symbols with zero recorded references, minus
   anything in your suppression file.

## Requirements

- Python 3.10+
- `pip install libclang` (see the note at the bottom — you may need a
  second, separately-installed Clang for this to work cleanly)
- A `compile_commands.json` for your project, with correct, absolute
  `directory` paths (see `--libclang-path`/directory notes below)

## Example run

```
python ClaudeLint.py --exclude der_libs
```

```
Parsing 17 translation unit(s)...
  excluding: ['der_libs']
.................
=== [4.5] Unused symbols ===
  (2 suppressed via .claudelint-suppress, 3 shown)
D:\SourceCode\Git\ndir64\ndir32.h:58: unused field 'ndir_data::ucase'
D:\SourceCode\Git\ndir64\ndir32.h:74: unused field 'ndir_data::low_ascii'
D:\SourceCode\Git\ndir64\ndir32.h:75: unused field 'ndir_data::unused1'
  3 unused symbol(s) found.

=== [4.2] Header inventory ===
  headers seen via AST includes: 4
  headers seen via makedepend block (Makefile): 4
  headers found on disk:         5
  1 header(s) on disk but referenced by NEITHER source:
    D:\SourceCode\Git\ndir64\keycodes32.h
```

`--exclude der_libs` here excludes a personal shared library folder
that's reused across multiple projects — fields "unused" *within this
one project* don't mean unused everywhere it's included, so it's kept
out of consideration entirely rather than generating false positives.

## Command-line options

| Flag | Default | Purpose |
|---|---|---|
| `--compile-commands PATH` | `compile_commands.json` | Path to the compile database. |
| `--libclang-path PATH` | *(set to your matched engine)* | Path to the `libclang.dll`/`.so` to parse with. **Must be version-matched to your real toolchain's own resource-dir headers** — see the note below. |
| `--target TRIPLE` | *(your project's target)* | Forces the target triple so libclang doesn't auto-detect an unrelated host toolchain. |
| `--makefile PATH` | `Makefile` | Used for the header-inventory cross-check (§4.2) against a `makedepend`-generated dependency block. |
| `--exclude PATTERN` | *(none)* | Exclude a path (directory prefix, e.g. `der_libs`) or glob (e.g. `*.legacy.h`) from **all** harvesting and header inventory — repeatable. Use for vendored/shared code you don't own. |
| `--no-header-inventory` | off | Skip the §4.2 header-inventory report entirely. |
| `--suppressions PATH` | `.claudelint-suppress` | Suppression file: `path:line` per entry (relative to the project dir), `#` comments allowed (full-line or trailing). Missing file = no suppressions, not an error. |
| `--generate-suppressions PATH` | *(none)* | Instead of reporting, write the *current* unused list to `PATH` in suppression-file format and exit — a ready-to-edit "yes, I know, leave it" baseline. |
| `--dump-declared` | off | Also print every declared symbol (used or not) — for debugging the harvester itself, not for normal use. |
| `--why NAME` | *(none)* | Debug: instead of the normal report, show every reference recorded for symbol `NAME` (bare name or `Struct::field`) and where. The tool showing its work, not just its verdict. |
| `--jobs N` | one per CPU core | Parallel worker processes. `--jobs 1` forces sequential parsing — useful for isolating whether a failure is real or a parallelism artifact. |

## The suppression file

A `.claudelint-suppress` file (or wherever `--suppressions` points) is a
plain text list of known-fine findings, cppcheck-`.suppress`-style:

```
# comment lines and trailing '# ...' comments are both fine
scsi_defs.h:288  # _SCSI_PASS_THROUGH_WITH_BUFFERS::Filler
scsi_defs.h:289  # _SCSI_PASS_THROUGH_WITH_BUFFERS::SenseBuf
```

Use this instead of `--exclude` when a *specific* finding is fine
(e.g. an intentional padding/alignment field in a vendored SDK header)
but you still want the rest of that file checked normally — `--exclude`
removes a file/directory from consideration forever; suppression just
acknowledges specific lines.

One caveat inherited from cppcheck's own `.suppress` format: entries
are `file:line`, so they can drift if the file is heavily edited later
and lines shift. Re-run and re-check suppressed entries occasionally.

## A note on `compile_commands.json`'s `directory` field

It must be an **absolute** path, and it must match where
`compile_commands.json` itself actually lives — ClaudeLint checks this
before doing anything else and refuses to run if it doesn't match,
because a stale `directory` (e.g. left over from copying the whole
project folder to a new location without regenerating the compile
database) causes every tool that trusts it to silently analyze the
*old* code without any error at all. If you ever copy/relocate the
whole project, regenerate `compile_commands.json` fresh in the new
location rather than assuming a copied one still applies.

## A note on `pip install libclang`

The `libclang` PyPI package bundles its own prebuilt `libclang.dll`/
`.so` — convenient, but it's frequently an *older* Clang version than
whatever real toolchain your project actually builds with. If your
project uses a recent/bleeding-edge Clang, you may need to separately
download an official LLVM release
(`clang+llvm-<version>-<platform>.tar.xz` from
[github.com/llvm/llvm-project/releases](https://github.com/llvm/llvm-project/releases),
matching your real toolchain's version) and point `--libclang-path` at
*its* `libclang.dll` instead — the parsing engine and its own
resource-dir headers need to come from the same build, or you'll see
spurious errors about unsupported compiler versions or unknown
compiler builtins that have nothing to do with your actual code.
