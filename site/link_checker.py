#!/usr/bin/env python3
"""
link_checker.py — static internal-link crawler for the HitungKerusi 222
Vercel site.

Walks every *.html file in the repo, extracts href="..."/src="..." targets,
resolves any internal (site-relative or root-relative) link against the
filesystem, and reports broken ones. External links (http(s)://, mailto:,
tel:, #fragments-only) are skipped. Vercel serves extensionless "clean URLs"
(vercel.json has cleanUrls or the linker relies on /path resolving to
/path.html) — this checker accepts both forms.

Usage:
    python3 link_checker.py            # exit 1 if any broken link found
    python3 link_checker.py --verbose  # print every internal link checked
"""
import os
import re
import sys
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.abspath(__file__))

HREF_RE = re.compile(r'''(?:href|src)\s*=\s*["']([^"']+)["']''', re.IGNORECASE)

SKIP_PREFIXES = ("http://", "https://", "//", "mailto:", "tel:", "javascript:", "data:")


def find_html_files(root):
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".vercel")]
        for f in files:
            if f.endswith(".html"):
                out.append(os.path.join(base, f))
    return out


def resolve_target(link, source_file):
    """Return the filesystem path a link should resolve to, or None if it's
    not a checkable internal link (external/anchor-only/etc.)."""
    # strip query + fragment
    parts = urlsplit(link)
    if parts.scheme or parts.netloc:
        return None  # external
    path = parts.path
    if not path:
        return None  # pure fragment (#foo) or empty — not a broken-link case

    if path.startswith("/"):
        base_dir = ROOT
        rel = path.lstrip("/")
    else:
        base_dir = os.path.dirname(source_file)
        rel = path

    candidate = os.path.normpath(os.path.join(base_dir, rel))
    return candidate


def target_exists(candidate):
    """Accept exact file, clean-URL .html match, or directory index.html."""
    if os.path.isfile(candidate):
        return True
    if os.path.isfile(candidate + ".html"):
        return True
    if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "index.html")):
        return True
    return False


def main():
    verbose = "--verbose" in sys.argv
    html_files = sorted(find_html_files(ROOT))
    broken = []
    checked = 0

    for f in html_files:
        text = open(f, encoding="utf-8", errors="replace").read()
        for m in HREF_RE.finditer(text):
            link = m.group(1)
            if link.startswith(SKIP_PREFIXES):
                continue
            candidate = resolve_target(link, f)
            if candidate is None:
                continue
            checked += 1
            ok = target_exists(candidate)
            if verbose:
                print(f"{'OK  ' if ok else 'FAIL'} {os.path.relpath(f, ROOT)} -> {link}")
            if not ok:
                broken.append((os.path.relpath(f, ROOT), link, os.path.relpath(candidate, ROOT)))

    print(f"\n=== link_checker.py — {len(html_files)} HTML files scanned, "
          f"{checked} internal links checked ===")
    if broken:
        print(f"\n{len(broken)} BROKEN internal link(s):\n")
        for src, link, resolved in broken:
            print(f"  {src}\n    -> {link}\n    (resolved: {resolved})\n")
        sys.exit(1)
    else:
        print("0 broken internal links.")
        sys.exit(0)


if __name__ == "__main__":
    main()
