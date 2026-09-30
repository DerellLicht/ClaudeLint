#!/usr/bin/env python3
"""
clean_gemini.py - clean up Gemini conversations saved as markdown by the
Firefox "Save as Markdown" extension.

Steps performed (see clean_gemini.md):
  1. Remove the YAML-ish title block at the top of the file (--- ... ---)
  2. Remove the "[Accessibility help](...)" line
  3. Remove embedded images (plain and link-wrapped data: images)
  4. Remove ad blocks ("Copied to clipboard..." / "## Shared" ... "Show all")
  5. Replace each "## You said: ..." heading line with "## Me  " (the
     second, normal-font copy of the post is kept; the timestamp line
     after it, e.g. "9:48 AM", is dropped)
  6. Replace each "### AI Mode reply for ..." line with "## Gemini  "
  7. Remove the trailing page noise at the end of the file (a final
     "## Shared" heading, "Skip to previous prompt", account info, etc.)
  8. Remove the "## AI Mode Conversation: <title>" line, which just repeats
     the thread title that is already shown above it

Usage:
  python clean_gemini.py file1.md [file2.md ...]      (wildcards OK)
  python clean_gemini.py --inplace file.md            (overwrite the original)

By default the cleaned text is written next to the original as
"<name> - clean.md", and the original is left untouched.
"""

import argparse
import glob
import re
import sys
from pathlib import Path

# Step 1: title block at the very top of the file, delimited by '---' lines
TITLE_BLOCK_RE = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*\n", re.DOTALL)

# Step 2: the accessibility link line (whole line, including its newline)
ACCESSIBILITY_RE = re.compile(r"^\[Accessibility help\]\([^)]*\)[ \t]*\n?", re.MULTILINE)

# Step 3: images.  Base64 data never contains ')' or ']', so these are safe.
#   linked image:  [![alt](data:image/...)](https://...)
#   plain image:   ![alt](data:image/...)
LINKED_IMAGE_RE = re.compile(r"\[!\[[^\]]*\]\(data:image[^)]*\)\]\([^)]*\)")
PLAIN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(data:image[^)]*\)")

# Step 4: ad block, from the clipboard-failure line through 'Show all'
AD_BLOCK_RE = re.compile(
    r"^Copied to clipboardFailed to copy to clipboard\. Try again later\.[ \t]*\n"
    r"\s*## Shared[ \t]*\n.*?^Show all[ \t]*\n?",
    re.DOTALL | re.MULTILINE,
)

# Step 8: the line repeating the thread title (whole line, with or without
# leading '#' heading marks)
CONVERSATION_TITLE_RE = re.compile(r"^#*[ \t]*AI Mode Conversation:.*\n?", re.MULTILINE)

# Steps 5 and 6 work line-by-line
YOU_SAID_PREFIX = "## You said:"
AI_REPLY_PREFIX = "### AI Mode reply for"
TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}\s*[AP]M\s*$")

ME_HEADING = "## Me  "
GEMINI_HEADING = "## Gemini  "


def clean_posts(text):
    """
    Replace the "You said" and "AI Mode reply" headings.

    Each of the user's posts is stored as a '## You said: [post]' heading
    followed by a second, normal-font copy of the post.  Only the heading
    line is replaced with '## Me  '; the normal-font copy is kept as the
    post text.

    The timestamp line (e.g. '9:48 AM') that follows a post is dropped.  It
    is only dropped while 'awaiting_timestamp' is set, i.e. between a
    '## You said:' heading and the next heading, so a stray time-like line
    elsewhere in a reply is never touched.
    """
    lines = text.split("\n")
    out = []
    awaiting_timestamp = False

    for line in lines:
        if line.startswith(YOU_SAID_PREFIX):
            out.append(ME_HEADING)
            awaiting_timestamp = True
        elif line.startswith(AI_REPLY_PREFIX):
            out.append(GEMINI_HEADING)
            awaiting_timestamp = False
        elif awaiting_timestamp and TIMESTAMP_RE.match(line):
            awaiting_timestamp = False  # drop this line
        elif line.startswith("## "):
            out.append(line)
            awaiting_timestamp = False
        else:
            out.append(line)

    return "\n".join(out)


def strip_footer(text):
    """
    Remove the trailing page noise (step 7).

    After the ad blocks are gone, the only '## Shared' heading left is the
    one in the page footer, followed by things like '0 files', 'Skip to
    previous prompt', 'Cart', and account info.  The text is cut at the LAST
    '## Shared' heading, but only if no post heading ('## Me', '## You said:',
    '## Gemini', '### AI Mode reply for') follows it, so real conversation
    content is never removed.
    """
    matches = list(re.finditer(r"^## Shared[ \t]*$", text, re.MULTILINE))
    if not matches:
        return text
    cut = matches[-1].start()
    tail = text[cut:]
    if re.search(r"^(## Me|## You said:|## Gemini|### AI Mode reply for)", tail, re.MULTILINE):
        return text
    return text[:cut]


def clean_text(text):
    """Apply all cleaning steps, in order, to the full file text."""
    text = TITLE_BLOCK_RE.sub("", text, count=1)
    text = ACCESSIBILITY_RE.sub("", text)
    text = LINKED_IMAGE_RE.sub("", text)
    text = PLAIN_IMAGE_RE.sub("", text)
    text = AD_BLOCK_RE.sub("", text)
    text = strip_footer(text)
    text = CONVERSATION_TITLE_RE.sub("", text)
    text = clean_posts(text)
    # Tidy: removals leave runs of blank lines behind
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + "\n"


def process_file(path, inplace):
    """
    Clean one file and write the result.  Line endings (LF or CRLF) of the
    original are preserved.  Returns the path written.
    """
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")

    cleaned = clean_text(text)
    if crlf:
        cleaned = cleaned.replace("\n", "\r\n")

    if inplace:
        dest = path
    else:
        dest = path.with_name(path.stem + " - clean" + path.suffix)
    dest.write_bytes(cleaned.encode("utf-8"))
    return dest, len(raw), len(cleaned)


def main():
    parser = argparse.ArgumentParser(description="Clean saved Gemini markdown files.")
    parser.add_argument("files", nargs="+", help="markdown files (wildcards allowed)")
    parser.add_argument("--inplace", action="store_true",
                        help="overwrite the original files instead of writing '<name> - clean.md'")
    args = parser.parse_args()

    # cmd.exe doesn't expand wildcards, so do it here
    paths = []
    for pattern in args.files:
        matches = glob.glob(pattern)
        paths.extend(Path(m) for m in (matches if matches else [pattern]))

    status = 0
    for path in paths:
        if not path.is_file():
            print(f"not found: {path}", file=sys.stderr)
            status = 1
            continue
        if path.stem.endswith(" - clean") and not args.inplace:
            continue  # don't re-clean our own output
        dest, before, after = process_file(path, args.inplace)
        print(f"{path.name} -> {dest.name}  ({before:,} -> {after:,} bytes)")
    return status


if __name__ == "__main__":
    sys.exit(main())
