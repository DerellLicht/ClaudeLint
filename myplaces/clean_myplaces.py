#!/usr/bin/env python3
"""
clean_myplaces.py

Sanitizes an old Google Earth Pro / GE4W "myplaces.kml" file so it can be
imported into Google Earth on the Web, which supports a much smaller subset
of KML than desktop GE ever did.

Fixes applied (see REPORT at the end of the run for exact details):
  1. gx:Track / gx:MultiTrack geometries -> plain LineString / MultiGeometry.
     GE Web's importer does not support the gx: track extensions at all, so
     any Placemark using them is silently dropped on import.
  2. All altitudeMode values normalized to clampToGround, since GE Web
     handles "absolute"/sea-floor-relative altitudes from noisy GPS/barometric
     data by floating features above the terrain instead of draping them.
  3. Icon hrefs that are local file paths, bare filenames, or otherwise not
     a real http(s) URL are replaced with a working fallback icon (the
     Google-hosted yellow pushpin already used elsewhere in the file).
  4. The container tree (Folder/Document nesting) is flattened to a single
     level under the root Document, grouped by original "session" name, so
     every placemark ends up two containers deep at most.

Usage:
    python3 clean_myplaces.py myplaces_in.kml myplaces_cleaned.kml report.txt
"""

import sys
from lxml import etree

KML_NS = "http://www.opengis.net/kml/2.2"
GX_NS = "http://www.google.com/kml/ext/2.2"
NS = {"k": KML_NS, "gx": GX_NS}

# Fallback icon used to replace any href GE Web can't resolve (local paths,
# bare filenames, dead links). Picked because it's already used natively
# elsewhere in the file, so it's a known-good, currently-reachable URL.
FALLBACK_ICON_HREF = "http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png"

# Session-level folders to drop entirely rather than clean up -- content the
# user has confirmed is no longer wanted (outdated data, no longer relevant).
# Match is against the "session" name used as the flatten_structure() group
# key, i.e. the container name at ancestor_chain_names()[2].
DROP_SESSIONS = {
    "San Andreas & Bay Areas Faults",  # outdated fault-line data, superseded
}

# Container depth every placemark is flattened to (root Document + one
# session folder). Kept as a constant in case GE Web's real behavior turns
# out to need something different after a test import.
TARGET_MAX_DEPTH = 1


def qn(ns, tag):
    """Build a Clark-notation qualified tag name, e.g. qn(KML_NS, 'Folder')."""
    return f"{{{ns}}}{tag}"


def local_name(tag):
    """Strip the namespace off a Clark-notation tag, e.g. '{...}Folder' -> 'Folder'."""
    return tag.split("}")[-1] if "}" in tag else tag


def is_container(el):
    """A KML container we treat as a grouping node: Folder or Document."""
    return el.tag in (qn(KML_NS, "Folder"), qn(KML_NS, "Document"))


def get_name(el):
    """Return an element's <name> text, or None if absent."""
    nm = el.find("k:name", NS)
    return nm.text.strip() if nm is not None and nm.text else None


def gx_track_to_linestring(track_el):
    """
    Convert a single gx:Track element into a KML <LineString>.

    gx:Track stores coordinates as space-separated "lon lat alt" in
    <gx:coord> children (one per <when> timestamp, in the same order).
    LineString wants comma-separated "lon,lat,alt" triples in one
    <coordinates> block. This also captures a plain-text time summary
    for the report and for an ExtendedData field, since the animated
    time-track itself isn't something GE Web can play back anyway.
    """
    coords = track_el.findall("gx:coord", NS)
    whens = track_el.findall("k:when", NS)

    coord_strs = []
    for c in coords:
        lon, lat, alt = c.text.split()
        coord_strs.append(f"{lon},{lat},{alt}")

    linestring = etree.SubElement(track_el.getparent(), qn(KML_NS, "LineString"))
    # Placeholder position; caller replaces track_el with this element directly.
    etree.SubElement(linestring, qn(KML_NS, "altitudeMode")).text = "clampToGround"
    etree.SubElement(linestring, qn(KML_NS, "tessellate")).text = "1"
    etree.SubElement(linestring, qn(KML_NS, "coordinates")).text = " ".join(coord_strs)

    time_summary = None
    if whens:
        time_summary = f"{whens[0].text} to {whens[-1].text} ({len(whens)} points)"

    # Detach the new LineString from its temporary parent; caller repositions it.
    linestring.getparent().remove(linestring)
    return linestring, time_summary


def transform_tracks(root, report):
    """
    Find every Placemark whose geometry is a gx:Track or gx:MultiTrack and
    replace that geometry in place with a plain LineString or MultiGeometry
    of LineStrings. Records one report line per Placemark converted.
    """
    converted = 0
    detail_lines = []
    for placemark in root.findall(".//k:Placemark", NS):
        gx_track = placemark.find("gx:Track", NS)
        gx_multitrack = placemark.find("gx:MultiTrack", NS)
        name = get_name(placemark) or "(unnamed)"

        if gx_track is not None:
            linestring, time_summary = gx_track_to_linestring(gx_track)
            placemark.replace(gx_track, linestring)
            _add_time_note(placemark, time_summary)
            detail_lines.append(f"  track->LineString: '{name}'" +
                                 (f"  [{time_summary}]" if time_summary else ""))
            converted += 1

        elif gx_multitrack is not None:
            multigeom = etree.Element(qn(KML_NS, "MultiGeometry"))
            sub_tracks = gx_multitrack.findall("gx:Track", NS)
            summaries = []
            for sub in sub_tracks:
                linestring, time_summary = gx_track_to_linestring(sub)
                multigeom.append(linestring)
                if time_summary:
                    summaries.append(time_summary)
            placemark.replace(gx_multitrack, multigeom)
            note = "; ".join(summaries) if summaries else None
            _add_time_note(placemark, note)
            detail_lines.append(f"  multitrack->MultiGeometry ({len(sub_tracks)} segments): '{name}'")
            converted += 1

    report.append(f"Converted {converted} gx:Track/gx:MultiTrack placemark(s) to LineString/MultiGeometry:")
    report.extend(detail_lines)
    report.append("")


def _add_time_note(placemark, time_summary):
    """Stash the original recording time range in ExtendedData for reference."""
    if not time_summary:
        return
    ext = placemark.find("k:ExtendedData", NS)
    if ext is None:
        ext = etree.SubElement(placemark, qn(KML_NS, "ExtendedData"))
    data = etree.SubElement(ext, qn(KML_NS, "Data"), name="original_recorded_time")
    etree.SubElement(data, qn(KML_NS, "value")).text = time_summary


def normalize_altitude_modes(root, report):
    """
    Force every altitudeMode / gx:altitudeMode value to clampToGround.
    Hiking tracks/waypoints should always drape onto terrain; anything else
    (absolute, relativeToSeaFloor, clampToSeaFloor, relativeToGround) is what
    was making tracks appear to float above the map.
    """
    changed = 0
    seen_values = {}
    for tag in (qn(KML_NS, "altitudeMode"), qn(GX_NS, "altitudeMode")):
        for el in root.iter(tag):
            if el.text and el.text.strip() != "clampToGround":
                seen_values[el.text.strip()] = seen_values.get(el.text.strip(), 0) + 1
                el.text = "clampToGround"
                changed += 1
    report.append(f"Normalized {changed} altitudeMode value(s) to clampToGround. Original values seen: {seen_values}")
    report.append("")


def fix_icon_hrefs(root, report):
    """
    Replace any Icon href that isn't a real http(s) URL (local file paths,
    bare filenames, empty schemes) with a working fallback icon. Logs every
    original href + the enclosing Placemark's name so nothing is silently lost.
    """
    fixed = 0
    detail_lines = []
    for icon in root.iter(qn(KML_NS, "Icon")):
        href = icon.find("k:href", NS)
        if href is None or not href.text:
            continue
        text = href.text.strip()
        if text.lower().startswith("http://") or text.lower().startswith("https://"):
            continue
        # Walk up to the nearest enclosing Placemark for a readable report line.
        anc = icon.getparent()
        while anc is not None and anc.tag != qn(KML_NS, "Placemark") and anc.tag not in (qn(KML_NS, "Style"), qn(KML_NS, "IconStyle")):
            anc = anc.getparent()
        owner_name = get_name(anc) if anc is not None else None
        label = owner_name or local_name(anc.tag) if anc is not None else "(unknown)"
        detail_lines.append(f"  icon fixed on '{label}': was '{text}'")
        href.text = FALLBACK_ICON_HREF
        fixed += 1
    report.append(f"Fixed {fixed} broken/local icon href(s), replaced with fallback pushpin icon.")
    report.extend(detail_lines)
    report.append("")


def flatten_structure(root, report):
    """
    Rebuild the container tree so every Placemark ends up under exactly one
    named Folder beneath the root Document, instead of the original file's
    up-to-6-level-deep Document/Folder nesting. Grouping key is the second
    container name in each placemark's ancestor chain (the "session" name,
    e.g. 'coal mine canyon 04.27.22'); placemarks with no such ancestor go
    into a 'Misc' folder.
    """
    kml_root = root  # <kml> element
    old_doc = kml_root.find("k:Document", NS)
    top_name = get_name(old_doc) or "My Places"

    container_tags = (qn(KML_NS, "Folder"), qn(KML_NS, "Document"))

    def ancestor_chain_names(el):
        names = []
        anc = el.getparent()
        while anc is not None:
            if anc.tag in container_tags:
                names.append(get_name(anc) or "(unnamed)")
            anc = anc.getparent()
        return list(reversed(names))

    placemarks = root.findall(".//k:Placemark", NS)
    groups = {}  # group name -> list of placemark elements
    depth_before = {}
    for pm in placemarks:
        chain = ancestor_chain_names(pm)
        depth_before[len(chain)] = depth_before.get(len(chain), 0) + 1
        # chain[0] is the outermost root Document name, chain[1] is always the
        # single top-level "My Places" wrapper folder -- neither is a useful
        # grouping key. The actual per-hike/session name (when present) is
        # chain[2]; placemarks that sit directly under "My Places" with no
        # session wrapper (chain length 2) have no such name, so bucket them
        # into an explicit "Misc" folder instead of the meaningless wrapper name.
        group = chain[2] if len(chain) >= 3 else "Misc"
        groups.setdefault(group, []).append(pm)

    dropped = 0
    for session_name in DROP_SESSIONS:
        removed = groups.pop(session_name, None)
        if removed:
            dropped += len(removed)

    # Detach every placemark from wherever it currently lives.
    for pm in placemarks:
        pm.getparent().remove(pm)

    # Build the new, flat Document.
    new_doc = etree.Element(qn(KML_NS, "Document"))
    etree.SubElement(new_doc, qn(KML_NS, "name")).text = top_name

    # Carry over top-level Style/StyleMap definitions so styleUrl references
    # used by the (moved) placemarks still resolve.
    if old_doc is not None:
        for style_el in old_doc.findall("k:Style", NS) + old_doc.findall("k:StyleMap", NS):
            new_doc.append(style_el)

    for group_name in sorted(groups.keys()):
        folder = etree.SubElement(new_doc, qn(KML_NS, "Folder"))
        etree.SubElement(folder, qn(KML_NS, "name")).text = group_name
        for pm in groups[group_name]:
            folder.append(pm)

    # Replace the old Document with the new flat one.
    if old_doc is not None:
        kml_root.replace(old_doc, new_doc)
    else:
        kml_root.append(new_doc)

    report.append(f"Flattened container tree: {len(groups)} session folder(s) under one root Document.")
    if dropped:
        report.append(f"Dropped {dropped} placemark(s) from excluded session(s): {sorted(DROP_SESSIONS)}")
    report.append(f"Placemark depth (# container ancestors) BEFORE flattening: {dict(sorted(depth_before.items()))}")
    report.append("Depth AFTER flattening: every placemark is now exactly 1 folder deep.")
    report.append("")


def main():
    if len(sys.argv) != 4:
        print("Usage: python3 clean_myplaces.py <input.kml> <output.kml> <report.txt>")
        sys.exit(1)

    in_path, out_path, report_path = sys.argv[1], sys.argv[2], sys.argv[3]

    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.parse(in_path, parser)
    root = tree.getroot()

    report = []
    report.append(f"Cleanup report for {in_path}")
    report.append("=" * 60)
    report.append("")

    transform_tracks(root, report)
    normalize_altitude_modes(root, report)
    fix_icon_hrefs(root, report)
    flatten_structure(root, report)

    tree.write(out_path, xml_declaration=True, encoding="UTF-8", pretty_print=True)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    print(f"Wrote cleaned KML to {out_path}")
    print(f"Wrote report to {report_path}")


if __name__ == "__main__":
    main()
