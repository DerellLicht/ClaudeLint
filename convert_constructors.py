#!/usr/bin/env python3
"""
convert_constructors.py

One-time cleanup for the gtstuff graph_object hierarchy: strips the unused
`std::string title_text` constructor parameter (and the matching
`graph_object(std::move(title_text))` base-class init) out of every derived
class's .cpp file in the current directory.

Usage:
    python convert_constructors.py [--dry-run]

--dry-run   Report what would change without writing any files.
"""

import glob
import re
import sys


# ---------------------------------------------------------------------------
# strip_comments
#
# Returns a copy of `text` with // line comments and /* */ block comments
# blanked out (replaced with spaces, preserving line structure/offsets).
# This is ONLY used to locate matches safely -- we never write this stripped
# text back to disk. Blanking rather than deleting keeps character offsets
# aligned with the original text, and preserving newlines keeps line numbers
# correct if we ever need them for diagnostics.
# ---------------------------------------------------------------------------
def strip_comments(text):
    result = []
    i = 0
    n = len(text)
    in_line_comment = False
    in_block_comment = False

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ''

        if in_line_comment:
            if c == '\n':
                in_line_comment = False
                result.append(c)
            else:
                result.append(' ')
            i += 1
            continue

        if in_block_comment:
            if c == '*' and nxt == '/':
                result.append('  ')
                i += 2
                in_block_comment = False
            else:
                result.append(c if c == '\n' else ' ')
                i += 1
            continue

        if c == '/' and nxt == '/':
            in_line_comment = True
            result.append('  ')
            i += 2
            continue

        if c == '/' and nxt == '*':
            in_block_comment = True
            result.append('  ')
            i += 2
            continue

        result.append(c)
        i += 1

    return ''.join(result)


# Matches "ClassName::ClassName ( std::string title_text )" with the class
# name captured so we know what to look for in the initializer list, and so
# we can report which class was converted.
CTOR_PATTERN = re.compile(
    r'\b(\w+)::\1\s*\(\s*std::string\s+title_text\s*\)'
)

# Matches "graph_object ( std::move ( title_text ) )" -- may be followed by
# more initializers on the same or later lines, which we leave untouched.
BASE_INIT_PATTERN = re.compile(
    r'graph_object\s*\(\s*std::move\s*\(\s*title_text\s*\)\s*\)'
)


# ---------------------------------------------------------------------------
# process_file
#
# Reads one .cpp file, looks for the title_text constructor pattern (search
# is done against a comment-stripped copy so commented-out code can't cause
# a false match), and if found, rewrites both the ctor signature and the
# graph_object base-class initializer in the ORIGINAL text. Returns True if
# the file was changed, False otherwise. Prints a one-line status message
# either way.
# ---------------------------------------------------------------------------
def process_file(path, dry_run=False):
    with open(path, 'r', encoding='utf-8') as f:
        original = f.read()

    searchable = strip_comments(original)
    match = CTOR_PATTERN.search(searchable)

    if not match:
        print(f"{path}: template not found")
        return False

    class_name = match.group(1)

    # Replace the constructor signature (only the first real occurrence).
    updated, n1 = CTOR_PATTERN.subn(
        f'{class_name}::{class_name}()', original, count=1
    )

    # Replace the base-class initializer, independently -- it may be on a
    # different line and may be followed by other member initializers.
    updated, n2 = BASE_INIT_PATTERN.subn('graph_object()', updated, count=1)

    if n1 == 0 or n2 == 0:
        # Found the ctor signature in the comment-stripped scan but couldn't
        # cleanly apply both substitutions to the real text -- don't risk a
        # partial/garbled edit. Flag it for a manual look instead of guessing.
        print(f"{path}: {class_name} - pattern matched but replace failed, skipped")
        return False

    if not dry_run:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(updated)

    print(f"{path}: {class_name} - constructor converted")
    return True


def main():
    dry_run = '--dry-run' in sys.argv

    cpp_files = sorted(glob.glob('*.cpp'))
    if not cpp_files:
        print("No .cpp files found in current directory.")
        return

    converted = 0
    for path in cpp_files:
        if process_file(path, dry_run=dry_run):
            converted += 1

    print(f"\n{converted}/{len(cpp_files)} file(s) converted"
          + (" (dry run, no files written)" if dry_run else ""))


if __name__ == '__main__':
    main()
