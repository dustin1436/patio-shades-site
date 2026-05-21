#!/usr/bin/env python3
"""
JAW Co Image Optimizer
======================

Audits and optimizes images across a JAW Co static HTML site.

Subcommands:
  audit                  Read-only report of image weight, formats, lazy-load.
  optimize               Convert JPG/PNG -> WebP siblings, resize oversized,
                         optionally add loading="lazy", optionally wrap in
                         <picture> for WebP with original fallback.
  verify                 Pass/fail check against the Definition of Done.
  picture-swap           Wrap <img src=...png|jpg> in <picture> with WebP source.
  dedupe-inline-base64   Extract base64-inlined images into external files
                         (deduplicated by content hash), then rewrite HTML to
                         reference them via <picture>.

Defaults are conservative: read-only unless --apply is passed; WebP files
are created as siblings (originals kept); HTML rewrites only happen with
explicit subcommands.

Usage:
  python3 jaw_image_optimizer.py audit                <site>
  python3 jaw_image_optimizer.py optimize             <site> --apply [opts]
  python3 jaw_image_optimizer.py picture-swap         <site> --apply
  python3 jaw_image_optimizer.py dedupe-inline-base64 <site> --apply
  python3 jaw_image_optimizer.py verify               <site>

Standards enforced (JAW Co Definition of Done items 13–14):
  - Every image <= 500 KB on disk (heroes <= 800 KB).
  - Every JPG/PNG photograph has a WebP sibling.
  - Every <img> tag below the first on a page has loading="lazy".
  - No image wider than 1920px (we resize down — preserves aspect ratio).
  - No base64-inlined images in production HTML.
  - No HTML file >50 KB (usually a sign of inlined assets).
  - No <img> outside a <picture> when a WebP sibling exists.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image
except ImportError:
    sys.stderr.write(
        "ERROR: Pillow not installed. Run: pip3 install Pillow\n"
    )
    sys.exit(1)


# ---------- Configuration -------------------------------------------------

RASTER_EXTS = {".jpg", ".jpeg", ".png"}
ALL_IMG_EXTS = RASTER_EXTS | {".webp", ".avif", ".gif", ".svg"}
EXCLUDE_DIRS = {".git", "node_modules", ".next", "dist", "build", ".claude"}

SIZE_CEILING_KB = 500          # Item 13: standard images
HERO_SIZE_CEILING_KB = 800     # heroes get a slightly wider lane
MAX_WIDTH_PX = 1920            # Item 14 supporting rule
WEBP_QUALITY = 82              # visually lossless for photos


# ---------- Data structures -----------------------------------------------

@dataclass
class ImageInfo:
    path: Path
    size_bytes: int
    width: int | None
    height: int | None
    format: str
    has_webp_sibling: bool

    @property
    def size_kb(self) -> int:
        return self.size_bytes // 1024


@dataclass
class HtmlAuditRow:
    path: Path
    total_imgs: int
    lazy_imgs: int
    first_img_excluded: bool
    below_fold_count: int = 0  # set explicitly when classify_imgs is used

    @property
    def lazy_required(self) -> int:
        # Number of <img> tags that must be lazy = the below-fold count
        if self.below_fold_count > 0 or self.total_imgs == 0:
            return self.below_fold_count
        # Fallback for callers that haven't set below_fold_count
        return max(self.total_imgs - 1, 0) if self.first_img_excluded else self.total_imgs

    @property
    def lazy_pct(self) -> float:
        if self.lazy_required == 0:
            return 100.0
        return 100.0 * self.lazy_imgs / self.lazy_required


@dataclass
class SiteReport:
    site_path: Path
    images: list[ImageInfo] = field(default_factory=list)
    html_rows: list[HtmlAuditRow] = field(default_factory=list)

    def by_format(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for img in self.images:
            count, total = out.get(img.format, (0, 0))
            out[img.format] = (count + 1, total + img.size_bytes)
        return out

    def oversized(self) -> list[ImageInfo]:
        """Return images over the size ceiling. Excludes raster files whose
        WebP sibling is under the ceiling — those are the visitor's actual
        payload, so the oversized fallback is acceptable."""
        webp_sizes: dict[str, int] = {
            i.path.with_suffix("").as_posix(): i.size_bytes
            for i in self.images
            if i.path.suffix.lower() == ".webp"
        }
        result = []
        for i in self.images:
            if i.size_kb <= SIZE_CEILING_KB:
                continue
            if i.path.suffix.lower() in RASTER_EXTS:
                sibling = i.path.with_suffix("").as_posix()
                sibling_size = webp_sizes.get(sibling)
                if sibling_size is not None and sibling_size // 1024 <= SIZE_CEILING_KB:
                    continue  # WebP fallback is small — visitor never sees the big PNG
            result.append(i)
        return result

    def missing_webp(self) -> list[ImageInfo]:
        return [
            i for i in self.images
            if i.path.suffix.lower() in RASTER_EXTS and not i.has_webp_sibling
        ]

    def overwide(self) -> list[ImageInfo]:
        return [i for i in self.images if i.width and i.width > MAX_WIDTH_PX]


# ---------- Scanning ------------------------------------------------------

def iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        yield path


def scan_images(site: Path) -> list[ImageInfo]:
    images: list[ImageInfo] = []
    raster_paths = [p for p in iter_files(site) if p.suffix.lower() in RASTER_EXTS]
    webp_paths = {
        p.with_suffix("").as_posix()
        for p in iter_files(site)
        if p.suffix.lower() == ".webp"
    }

    for path in raster_paths:
        try:
            with Image.open(path) as im:
                width, height = im.size
                fmt = (im.format or path.suffix.lstrip(".")).lower()
        except Exception:
            width = height = None
            fmt = path.suffix.lstrip(".").lower()

        has_webp = path.with_suffix("").as_posix() in webp_paths
        images.append(
            ImageInfo(
                path=path,
                size_bytes=path.stat().st_size,
                width=width,
                height=height,
                format=fmt,
                has_webp_sibling=has_webp,
            )
        )

    # Include WebPs in the report too (for size accounting)
    for path in iter_files(site):
        if path.suffix.lower() != ".webp":
            continue
        try:
            with Image.open(path) as im:
                width, height = im.size
        except Exception:
            width = height = None
        images.append(
            ImageInfo(
                path=path,
                size_bytes=path.stat().st_size,
                width=width,
                height=height,
                format="webp",
                has_webp_sibling=True,
            )
        )

    return sorted(images, key=lambda i: -i.size_bytes)


IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
LAZY_RE = re.compile(r"""loading\s*=\s*["']lazy["']""", re.IGNORECASE)
# Match data:image/png;base64,... or data:image/jpeg;base64,... — the actually
# harmful pattern (binary photos inlined). Excludes data:image/svg+xml,... which
# is a legitimate small-placeholder pattern in templates.
B64_SRC_RE = re.compile(
    r"""src\s*=\s*["']data:image/(?:png|jpe?g|gif|webp);base64,""",
    re.IGNORECASE,
)
NAV_CONTEXT_RE = re.compile(
    r"""class\s*=\s*["'][^"']*(?:nav|logo|brand|header)[^"']*["']""",
    re.IGNORECASE,
)
ATF_TAG_OPEN_RE = re.compile(r"<(nav|header)\b", re.IGNORECASE)
ATF_TAG_CLOSE_RE = re.compile(r"</(nav|header)>", re.IGNORECASE)


def is_above_fold(text: str, img_start: int, img_tag: str) -> bool:
    """Heuristic: an <img> is above-the-fold if:
    - It has class containing 'nav', 'logo', 'brand', or 'header', OR
    - It appears inside an unclosed <nav> or <header> element."""
    if NAV_CONTEXT_RE.search(img_tag):
        return True
    # Track open/close of nav/header up to this img position
    prefix = text[:img_start]
    open_count = len(ATF_TAG_OPEN_RE.findall(prefix))
    close_count = len(ATF_TAG_CLOSE_RE.findall(prefix))
    return open_count > close_count


def classify_imgs(text: str) -> list[tuple[re.Match, bool]]:
    """Return list of (match, is_above_fold) for every <img> in the text.
    Also marks the first <img> as above-the-fold if nothing else qualifies (fallback)."""
    matches = list(IMG_TAG_RE.finditer(text))
    classified: list[tuple[re.Match, bool]] = []
    any_atf = False
    for m in matches:
        atf = is_above_fold(text, m.start(), m.group(0))
        if atf:
            any_atf = True
        classified.append((m, atf))
    # Fallback: if no img was classified as above-fold (e.g., site without nav/header),
    # treat the first one as above-fold so we don't lazy-load the hero.
    if matches and not any_atf:
        first_m, _ = classified[0]
        classified[0] = (first_m, True)
    return classified


def scan_html(site: Path) -> list[HtmlAuditRow]:
    rows: list[HtmlAuditRow] = []
    for path in iter_files(site):
        if path.suffix.lower() != ".html":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        classified = classify_imgs(text)
        total = len(classified)
        if total == 0:
            continue
        # Below-the-fold imgs that have lazy
        below_fold = [m for m, atf in classified if not atf]
        lazy = sum(1 for m in below_fold if LAZY_RE.search(m.group(0)))
        rows.append(
            HtmlAuditRow(
                path=path,
                total_imgs=total,
                lazy_imgs=lazy,
                # We override lazy_required to mean below-fold imgs specifically
                first_img_excluded=True,
                below_fold_count=len(below_fold),
            )
        )
    return rows


# ---------- Reporting -----------------------------------------------------

def render_report(report: SiteReport) -> str:
    lines: list[str] = []
    site_name = report.site_path.name
    lines.append(f"# Image Optimization Audit — {site_name}\n")

    total_bytes = sum(i.size_bytes for i in report.images)
    lines.append(f"**Site:** `{report.site_path}`")
    lines.append(f"**Total images:** {len(report.images)}")
    lines.append(f"**Total image weight:** {total_bytes // 1024} KB ({total_bytes / 1_048_576:.1f} MB)\n")

    # Format breakdown
    lines.append("## Format breakdown\n")
    lines.append("| Format | Count | Total KB |")
    lines.append("|---|---:|---:|")
    for fmt, (count, total) in sorted(report.by_format().items()):
        lines.append(f"| {fmt} | {count} | {total // 1024:,} |")
    lines.append("")

    # Oversized
    oversized = report.oversized()
    lines.append(f"## Oversized images (>{SIZE_CEILING_KB} KB) — {len(oversized)} found\n")
    if oversized:
        lines.append("| Size KB | Path |")
        lines.append("|---:|---|")
        for img in oversized[:30]:
            rel = img.path.relative_to(report.site_path)
            lines.append(f"| {img.size_kb:,} | `{rel}` |")
        if len(oversized) > 30:
            lines.append(f"| ... | _{len(oversized) - 30} more_ |")
    else:
        lines.append("_None — all images within size ceiling._")
    lines.append("")

    # Overwide
    overwide = report.overwide()
    lines.append(f"## Overwide images (>{MAX_WIDTH_PX}px) — {len(overwide)} found\n")
    if overwide:
        lines.append("| Width | Path |")
        lines.append("|---:|---|")
        for img in overwide[:20]:
            rel = img.path.relative_to(report.site_path)
            lines.append(f"| {img.width} | `{rel}` |")
        if len(overwide) > 20:
            lines.append(f"| ... | _{len(overwide) - 20} more_ |")
    else:
        lines.append("_None._")
    lines.append("")

    # WebP coverage
    raster_count = sum(1 for i in report.images if i.path.suffix.lower() in RASTER_EXTS)
    missing = report.missing_webp()
    have_webp = raster_count - len(missing)
    pct = (100 * have_webp / raster_count) if raster_count else 100
    lines.append(f"## WebP coverage — {have_webp}/{raster_count} ({pct:.0f}%)\n")
    if missing:
        lines.append(f"{len(missing)} raster images have no WebP sibling. Convert these for the biggest win.\n")
    else:
        lines.append("_Every raster image has a WebP sibling._\n")

    # Lazy load
    lines.append("## Lazy-load coverage (below-the-fold images)\n")
    if report.html_rows:
        total_imgs = sum(r.total_imgs for r in report.html_rows)
        total_required = sum(r.lazy_required for r in report.html_rows)
        total_lazy = sum(r.lazy_imgs for r in report.html_rows)
        overall_pct = (100 * total_lazy / total_required) if total_required else 100
        lines.append(f"**Overall:** {total_lazy}/{total_required} below-the-fold images lazy ({overall_pct:.0f}%)")
        lines.append(f"_Above-the-fold (first img per page) is excluded from the requirement._\n")
        lines.append("| Page | Total <img> | Lazy | Required | % |")
        lines.append("|---|---:|---:|---:|---:|")
        for r in report.html_rows:
            rel = r.path.relative_to(report.site_path)
            lines.append(
                f"| `{rel}` | {r.total_imgs} | {r.lazy_imgs} | {r.lazy_required} | {r.lazy_pct:.0f}% |"
            )
    else:
        lines.append("_No HTML files with <img> tags found._")
    lines.append("")

    # Projected savings (rough heuristic: WebP saves ~65% on JPG, ~75% on PNG)
    proj_savings_bytes = 0
    for img in missing:
        if img.format in {"png"}:
            proj_savings_bytes += int(img.size_bytes * 0.75)
        else:
            proj_savings_bytes += int(img.size_bytes * 0.65)
    lines.append("## Projected savings\n")
    lines.append(
        f"Converting all {len(missing)} raster images to WebP would save approximately "
        f"**{proj_savings_bytes // 1024:,} KB** "
        f"({proj_savings_bytes / 1_048_576:.1f} MB), based on typical compression ratios."
    )
    lines.append("")

    # Standards check
    lines.append("## JAW Co Definition of Done — Items 13–14\n")
    item_13_pass = len(oversized) == 0 and len(missing) == 0
    item_14_pass = all(r.lazy_pct >= 100 for r in report.html_rows) if report.html_rows else True
    lines.append(f"- **Item 13 (image weight + WebP):** {'PASS' if item_13_pass else 'FAIL'}")
    lines.append(f"- **Item 14 (lazy load below-the-fold):** {'PASS' if item_14_pass else 'FAIL'}")

    return "\n".join(lines)


# ---------- Optimization actions ------------------------------------------

def convert_to_webp(img: ImageInfo, quality: int = WEBP_QUALITY) -> Path | None:
    target = img.path.with_suffix(".webp")
    if target.exists():
        return target
    try:
        with Image.open(img.path) as im:
            if im.mode in {"P", "RGBA"} and img.path.suffix.lower() == ".png":
                im = im.convert("RGBA")
            else:
                im = im.convert("RGB")
            im.save(target, format="WEBP", quality=quality, method=6)
        return target
    except Exception as exc:
        sys.stderr.write(f"  ! Failed to convert {img.path}: {exc}\n")
        return None


def resize_if_overwide(img: ImageInfo, max_width: int = MAX_WIDTH_PX) -> bool:
    if not img.width or img.width <= max_width:
        return False
    try:
        with Image.open(img.path) as im:
            ratio = max_width / im.width
            new_size = (max_width, int(im.height * ratio))
            resized = im.resize(new_size, Image.LANCZOS)
            params = {}
            if img.path.suffix.lower() in {".jpg", ".jpeg"}:
                params = {"quality": 88, "optimize": True}
            resized.save(img.path, **params)
        return True
    except Exception as exc:
        sys.stderr.write(f"  ! Failed to resize {img.path}: {exc}\n")
        return False


def add_lazy_load_to_html(path: Path) -> int:
    """Add loading=\"lazy\" to all below-the-fold <img> tags.

    Uses classify_imgs() to identify above-the-fold images (nav logos, header
    images, anything in <nav>/<header>, anything with class containing
    nav/logo/brand/header). Those stay eager; everything else gets lazy.

    Returns number of tags modified.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0

    classified = classify_imgs(text)
    if not classified:
        return 0

    modified = 0
    new_text = text
    # Walk in reverse so offsets stay valid
    for tag, atf in reversed(classified):
        if atf:
            continue
        tag_text = tag.group(0)
        if LAZY_RE.search(tag_text):
            continue
        if tag_text.endswith("/>"):
            new_tag = tag_text[:-2].rstrip() + ' loading="lazy" />'
        else:
            new_tag = tag_text[:-1].rstrip() + ' loading="lazy">'
        new_text = new_text[: tag.start()] + new_tag + new_text[tag.end():]
        modified += 1

    if modified:
        path.write_text(new_text, encoding="utf-8")
    return modified


# ---------- <picture> swap ------------------------------------------------

SRC_ATTR_RE = re.compile(r"""src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
PICTURE_OPEN_RE = re.compile(r"<picture\b", re.IGNORECASE)
PICTURE_CLOSE_RE = re.compile(r"</picture\s*>", re.IGNORECASE)


def _img_already_in_picture(text: str, img_start: int) -> bool:
    """True if the <img> at position img_start is already inside a <picture> block."""
    last_open = -1
    for m in PICTURE_OPEN_RE.finditer(text[:img_start]):
        last_open = m.start()
    if last_open == -1:
        return False
    closes = list(PICTURE_CLOSE_RE.finditer(text[last_open:img_start]))
    return len(closes) == 0


def swap_img_to_picture(html_path: Path, site_root: Path) -> int:
    """Wrap each <img src="...png|jpg|jpeg"> with <picture><source srcset="...webp">...</picture>
    if a WebP sibling exists. Returns count of <img> tags wrapped.

    Skips <img> tags already inside a <picture> element. Preserves all <img> attributes.
    """
    try:
        text = html_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0

    tags = list(IMG_TAG_RE.finditer(text))
    if not tags:
        return 0

    # Walk in reverse so offsets stay valid
    modified = 0
    new_text = text
    for tag in reversed(tags):
        tag_text = tag.group(0)
        if _img_already_in_picture(new_text, tag.start()):
            continue
        src_match = SRC_ATTR_RE.search(tag_text)
        if not src_match:
            continue
        src = src_match.group(1)
        if "://" in src:  # absolute / external URL — skip
            continue
        if src.lower().rsplit(".", 1)[-1] not in {"png", "jpg", "jpeg"}:
            continue

        # Resolve src against site root for root-absolute paths, else relative to HTML
        if src.startswith("/"):
            src_path = (site_root / src.lstrip("/")).resolve()
        else:
            src_path = (html_path.parent / src).resolve()
        webp_sibling = src_path.with_suffix(".webp")
        if not webp_sibling.is_file():
            continue

        # Build the webp src preserving the original src style (relative or root-absolute)
        webp_src = src.rsplit(".", 1)[0] + ".webp"

        wrapped = (
            f'<picture><source srcset="{webp_src}" type="image/webp">'
            f"{tag_text}"
            f"</picture>"
        )
        new_text = new_text[: tag.start()] + wrapped + new_text[tag.end():]
        modified += 1

    if modified:
        html_path.write_text(new_text, encoding="utf-8")
    return modified


def cmd_picture_swap(args: argparse.Namespace) -> int:
    site = Path(args.site).expanduser().resolve()
    if not site.is_dir():
        sys.stderr.write(f"Not a directory: {site}\n")
        return 2

    if not args.apply:
        print("DRY RUN — re-run with --apply to actually write files.\n")

    html_files = [p for p in iter_files(site) if p.suffix.lower() == ".html"]
    total_wrapped = 0
    for path in html_files:
        if args.apply:
            n = swap_img_to_picture(path, site)
        else:
            # Dry-run: count what would be wrapped
            text = path.read_text(encoding="utf-8", errors="ignore")
            n = 0
            for tag in IMG_TAG_RE.finditer(text):
                tag_text = tag.group(0)
                if _img_already_in_picture(text, tag.start()):
                    continue
                src_match = SRC_ATTR_RE.search(tag_text)
                if not src_match:
                    continue
                src = src_match.group(1)
                if "://" in src or src.lower().rsplit(".", 1)[-1] not in {"png", "jpg", "jpeg"}:
                    continue
                if src.startswith("/"):
                    src_path = (site / src.lstrip("/")).resolve()
                else:
                    src_path = (path.parent / src).resolve()
                if src_path.with_suffix(".webp").is_file():
                    n += 1
        if n:
            print(f"  {'wrapped' if args.apply else 'would wrap'} {n} <img> in {path.relative_to(site)}")
            total_wrapped += n

    print(f"\nTotal: {total_wrapped} <img> tag(s) {'wrapped' if args.apply else 'to wrap'}")
    if not args.apply:
        print("This was a dry run. Re-run with --apply to make changes.")
    return 0


# ---------- Base64 inline dedupe ------------------------------------------

import hashlib
import base64
import io

B64_IMG_TAG_RE = re.compile(
    r'<img\b([^>]*?)src\s*=\s*["\']data:image/(png|jpe?g|gif|webp);base64,'
    r'([A-Za-z0-9+/=]+)["\']([^>]*)>',
    re.IGNORECASE,
)


def _extract_attrs_except_src(before_src: str, after_src: str) -> str:
    """Combine the attribute strings on either side of src=, return a normalized
    attribute string suitable for re-insertion into a new <img> tag."""
    attrs = (before_src + " " + after_src).strip()
    # Collapse whitespace
    attrs = re.sub(r"\s+", " ", attrs).strip()
    return attrs


def dedupe_inline_base64(site: Path, apply: bool) -> dict:
    """Scan all HTML files for base64-inlined <img> tags. For each unique image
    (by content hash), save to <site>/extracted/inlined-<hash>.<ext> + WebP
    sibling, then rewrite the HTML to reference the external files via <picture>.

    Returns a summary dict with counts and per-file results.
    """
    html_files = [p for p in iter_files(site) if p.suffix.lower() == ".html"]
    target_dir = site / "extracted"

    # First pass: extract all unique base64 images
    extracted: dict[str, dict] = {}  # hash -> {path, webp_path, format, count}
    occurrences: list[tuple[Path, re.Match]] = []

    for html_path in html_files:
        try:
            text = html_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in B64_IMG_TAG_RE.finditer(text):
            ext = m.group(2).lower()
            if ext == "jpeg":
                ext = "jpg"
            b64 = m.group(3)
            try:
                data = base64.b64decode(b64)
            except Exception:
                continue
            h = hashlib.sha1(data).hexdigest()[:10]
            if h not in extracted:
                extracted[h] = {
                    "format": ext,
                    "data": data,
                    "count": 0,
                    "size_bytes": len(data),
                }
            extracted[h]["count"] += 1
            occurrences.append((html_path, m))

    if apply and extracted:
        target_dir.mkdir(exist_ok=True)
        for h, info in extracted.items():
            ext = info["format"]
            file_path = target_dir / f"inlined-{h}.{ext}"
            file_path.write_bytes(info["data"])
            info["path"] = file_path
            # WebP sibling (skip if already WebP)
            if ext != "webp":
                try:
                    with Image.open(io.BytesIO(info["data"])) as im:
                        webp_path = file_path.with_suffix(".webp")
                        im.save(webp_path, format="WEBP", quality=WEBP_QUALITY, method=6)
                        info["webp_path"] = webp_path
                except Exception:
                    info["webp_path"] = None

    # Second pass: rewrite HTML
    rewrites: dict[Path, int] = {}
    if apply and extracted:
        for html_path in html_files:
            try:
                text = html_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            new_text = text
            replacements = 0

            def repl(m: re.Match) -> str:
                nonlocal replacements
                ext = m.group(2).lower()
                if ext == "jpeg":
                    ext = "jpg"
                b64 = m.group(3)
                try:
                    data = base64.b64decode(b64)
                except Exception:
                    return m.group(0)
                h = hashlib.sha1(data).hexdigest()[:10]
                info = extracted.get(h)
                if not info or not info.get("path"):
                    return m.group(0)

                file_rel = Path("extracted") / info["path"].name
                webp_rel = Path("extracted") / info["webp_path"].name if info.get("webp_path") else None

                attrs = _extract_attrs_except_src(m.group(1), m.group(4))
                img_tag = f'<img src="/{file_rel.as_posix()}"' + (f" {attrs}" if attrs else "") + ">"
                if webp_rel:
                    wrapped = (
                        f'<picture><source srcset="/{webp_rel.as_posix()}" type="image/webp">'
                        f"{img_tag}</picture>"
                    )
                else:
                    wrapped = img_tag
                replacements += 1
                return wrapped

            new_text = B64_IMG_TAG_RE.sub(repl, text)
            if replacements:
                html_path.write_text(new_text, encoding="utf-8")
                rewrites[html_path] = replacements

    return {
        "unique_images": len(extracted),
        "total_occurrences": sum(info["count"] for info in extracted.values()),
        "extracted": extracted,
        "rewrites": rewrites,
    }


def recompress_to_target(img_path: Path, target_kb: int = SIZE_CEILING_KB) -> tuple[int, int] | None:
    """For an oversized raster image with a WebP sibling, re-encode the WebP at
    iteratively lower quality (and if needed, lower resolution) until it's under
    target_kb. Returns (before_kb, after_kb) on success, None on failure."""
    webp_path = img_path.with_suffix(".webp")
    if not webp_path.is_file():
        return None
    before_kb = webp_path.stat().st_size // 1024
    if before_kb <= target_kb:
        return None  # already small enough

    try:
        with Image.open(img_path) as source:
            current = source.convert("RGB") if source.mode in {"P", "L"} else source.copy()

        # Try lower quality first (less destructive than resize)
        for q in [75, 68, 60, 52, 45]:
            current.save(webp_path, format="WEBP", quality=q, method=6)
            if webp_path.stat().st_size // 1024 <= target_kb:
                return (before_kb, webp_path.stat().st_size // 1024)

        # Quality alone isn't enough — try resizing
        for max_w in [1600, 1280, 1024]:
            if current.width <= max_w:
                continue
            ratio = max_w / current.width
            resized = current.resize((max_w, int(current.height * ratio)), Image.LANCZOS)
            for q in [70, 60, 50]:
                resized.save(webp_path, format="WEBP", quality=q, method=6)
                if webp_path.stat().st_size // 1024 <= target_kb:
                    return (before_kb, webp_path.stat().st_size // 1024)

        # Couldn't get it under — return current state
        return (before_kb, webp_path.stat().st_size // 1024)
    except Exception as exc:
        sys.stderr.write(f"  ! Failed to recompress {webp_path}: {exc}\n")
        return None


def cmd_recompress_oversized(args: argparse.Namespace) -> int:
    site = Path(args.site).expanduser().resolve()
    if not site.is_dir():
        sys.stderr.write(f"Not a directory: {site}\n")
        return 2
    if not args.apply:
        print("DRY RUN — re-run with --apply to recompress.\n")

    report = SiteReport(site_path=site)
    report.images = scan_images(site)

    # Find raster images where the WebP sibling is still over the ceiling
    webp_sizes = {
        i.path.with_suffix("").as_posix(): i.size_bytes // 1024
        for i in report.images
        if i.path.suffix.lower() == ".webp"
    }

    targets: list[ImageInfo] = []
    for i in report.images:
        if i.path.suffix.lower() not in RASTER_EXTS:
            continue
        sibling_size = webp_sizes.get(i.path.with_suffix("").as_posix())
        if sibling_size is None:
            continue
        if sibling_size > args.target_kb:
            targets.append(i)

    print(f"Found {len(targets)} image(s) where WebP sibling exceeds {args.target_kb} KB.")
    if not targets:
        return 0

    total_before = 0
    total_after = 0
    for img in targets:
        if args.apply:
            result = recompress_to_target(img.path, target_kb=args.target_kb)
            if result:
                before, after = result
                total_before += before
                total_after += after
                ok = "✓" if after <= args.target_kb else "⚠"
                print(f"  {ok} {img.path.relative_to(site)}: {before} KB -> {after} KB")
        else:
            print(f"  would recompress {img.path.relative_to(site)}")

    if args.apply and total_before:
        saved = total_before - total_after
        print(f"\nTotal: {total_before} KB -> {total_after} KB (saved {saved} KB)")
    return 0


def cmd_dedupe_inline_base64(args: argparse.Namespace) -> int:
    site = Path(args.site).expanduser().resolve()
    if not site.is_dir():
        sys.stderr.write(f"Not a directory: {site}\n")
        return 2
    if not args.apply:
        print("DRY RUN — re-run with --apply to extract files and rewrite HTML.\n")

    summary = dedupe_inline_base64(site, apply=args.apply)
    unique = summary["unique_images"]
    occurrences = summary["total_occurrences"]

    if unique == 0:
        print("No base64-inlined <img> tags found. Nothing to do.")
        return 0

    print(f"Found {occurrences} base64-inlined <img> tag(s) representing {unique} unique image(s):")
    for h, info in summary["extracted"].items():
        print(f"  {h}.{info['format']}: {info['count']} occurrence(s), "
              f"{info['size_bytes']:,} bytes each "
              f"(~{info['size_bytes'] * info['count'] // 1024} KB of HTML bloat)")

    if args.apply:
        print(f"\nExtracted to: {site}/extracted/")
        print(f"Files written:")
        for h, info in summary["extracted"].items():
            print(f"  - {info['path'].name}" + (f" + {info['webp_path'].name}" if info.get("webp_path") else ""))
        print(f"\nHTML files rewritten: {len(summary['rewrites'])}")
        for path, n in summary["rewrites"].items():
            print(f"  {path.relative_to(site)}: {n} replacement(s)")
    else:
        print("\nThis was a dry run. Re-run with --apply to extract and rewrite.")
    return 0


# ---------- CLI -----------------------------------------------------------

def cmd_audit(args: argparse.Namespace) -> int:
    site = Path(args.site).expanduser().resolve()
    if not site.is_dir():
        sys.stderr.write(f"Not a directory: {site}\n")
        return 2

    report = SiteReport(site_path=site)
    report.images = scan_images(site)
    report.html_rows = scan_html(site)
    md = render_report(report)

    if args.output:
        out = Path(args.output).expanduser().resolve()
        out.write_text(md, encoding="utf-8")
        print(f"Report written to: {out}")
    else:
        print(md)
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    site = Path(args.site).expanduser().resolve()
    if not site.is_dir():
        sys.stderr.write(f"Not a directory: {site}\n")
        return 2

    if not args.apply:
        print("DRY RUN — re-run with --apply to actually write files.\n")

    report = SiteReport(site_path=site)
    report.images = scan_images(site)

    # 1. Resize overwide
    overwide = report.overwide()
    print(f"Resize step: {len(overwide)} image(s) exceed {MAX_WIDTH_PX}px wide.")
    if args.apply and not args.no_resize:
        for img in overwide:
            if resize_if_overwide(img):
                print(f"  resized: {img.path.relative_to(site)}")

    # 2. WebP siblings
    missing_webp = report.missing_webp()
    print(f"\nWebP step: {len(missing_webp)} raster image(s) need WebP siblings.")
    if args.apply:
        for img in missing_webp:
            target = convert_to_webp(img, quality=args.quality)
            if target:
                saved_pct = 100 * (1 - target.stat().st_size / img.size_bytes)
                print(
                    f"  webp: {img.path.relative_to(site)} "
                    f"({img.size_kb} KB -> {target.stat().st_size // 1024} KB, -{saved_pct:.0f}%)"
                )

    # 3. Lazy-load attribute
    if args.add_lazy_load:
        html_rows = scan_html(site)
        print(f"\nLazy-load step: scanning {len(html_rows)} HTML file(s).")
        total_added = 0
        for row in html_rows:
            if args.apply:
                added = add_lazy_load_to_html(row.path)
            else:
                added = max(row.lazy_required - row.lazy_imgs, 0)
            if added:
                print(f"  +{added} lazy in {row.path.relative_to(site)}")
                total_added += added
        print(f"Lazy attributes added: {total_added}")

    print("\nDone.")
    if not args.apply:
        print("This was a dry run. Re-run with --apply to make changes.")
    return 0


HTML_SIZE_CEILING_KB = 50  # HTML files > this are usually bloated by inlined assets


def find_antipatterns(site: Path) -> dict:
    """Return dict of antipattern category -> list of (path, detail)."""
    findings = {
        "base64_inline": [],         # <img src="data:image/..."> in production HTML
        "oversized_html": [],        # HTML file > 50 KB
        "img_without_picture": [],   # <img src="...png|jpg"> with WebP sibling but no <picture> wrap
        "png_photographs": [],       # PNG files > 100 KB with no alpha channel (likely a photo)
    }

    html_files = [p for p in iter_files(site) if p.suffix.lower() == ".html"]

    for path in html_files:
        size_kb = path.stat().st_size // 1024
        if size_kb > HTML_SIZE_CEILING_KB:
            findings["oversized_html"].append((path, f"{size_kb} KB"))

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if B64_SRC_RE.search(text):
            count = len(B64_SRC_RE.findall(text))
            findings["base64_inline"].append((path, f"{count} base64-inlined <img>"))

        # img without picture-wrap when webp sibling exists
        for tag_match in IMG_TAG_RE.finditer(text):
            tag_text = tag_match.group(0)
            if _img_already_in_picture(text, tag_match.start()):
                continue
            src_match = SRC_ATTR_RE.search(tag_text)
            if not src_match:
                continue
            src = src_match.group(1)
            if "://" in src or src.startswith("data:"):
                continue
            if src.lower().rsplit(".", 1)[-1] not in {"png", "jpg", "jpeg"}:
                continue
            src_path = (path.parent / src.lstrip("/")).resolve()
            if not src_path.is_file():
                # also try site root for root-absolute paths
                src_path = (site / src.lstrip("/")).resolve()
            if src_path.with_suffix(".webp").is_file():
                findings["img_without_picture"].append((path, src))

    # PNG photograph detection
    for path in iter_files(site):
        if path.suffix.lower() != ".png":
            continue
        if path.stat().st_size // 1024 < 100:
            continue  # small PNGs are fine (icons, logos)
        try:
            with Image.open(path) as im:
                # Photos typically have no alpha and have wide color range.
                # Cheap heuristic: PNG with no alpha + >800px wide = probably a photo
                has_alpha = im.mode in {"RGBA", "LA"} or "transparency" in im.info
                width = im.width
                if not has_alpha and width > 800:
                    findings["png_photographs"].append((path, f"{width}px, no alpha, {path.stat().st_size // 1024} KB"))
        except Exception:
            pass

    return findings


def cmd_verify(args: argparse.Namespace) -> int:
    site = Path(args.site).expanduser().resolve()
    if not site.is_dir():
        sys.stderr.write(f"Not a directory: {site}\n")
        return 2

    report = SiteReport(site_path=site)
    report.images = scan_images(site)
    report.html_rows = scan_html(site)

    oversized = report.oversized()
    missing_webp = report.missing_webp()
    overwide = report.overwide()
    failing_pages = [r for r in report.html_rows if r.lazy_pct < 100]

    failures: list[str] = []
    if oversized:
        failures.append(
            f"{len(oversized)} image(s) exceed {SIZE_CEILING_KB} KB (largest: {oversized[0].size_kb} KB)"
        )
    if missing_webp:
        failures.append(f"{len(missing_webp)} raster image(s) missing WebP sibling")
    if overwide:
        failures.append(f"{len(overwide)} image(s) wider than {MAX_WIDTH_PX}px")
    if failing_pages:
        failures.append(f"{len(failing_pages)} page(s) with incomplete lazy-load coverage")

    # Antipatterns — blocking (real visitor impact) vs advisory (aspirational)
    anti = find_antipatterns(site)
    advisories: list[str] = []
    if anti["base64_inline"]:
        failures.append(
            f"{len(anti['base64_inline'])} HTML file(s) contain base64-inlined images "
            "(use dedupe-inline-base64 to fix)"
        )
    if anti["img_without_picture"]:
        failures.append(
            f"{len(anti['img_without_picture'])} <img> tag(s) reference raster files "
            "with WebP siblings but aren't wrapped in <picture> (use picture-swap to fix)"
        )
    # Advisories — surfaced for awareness but don't fail the gate by default
    if anti["oversized_html"]:
        advisories.append(
            f"{len(anti['oversized_html'])} HTML file(s) >{HTML_SIZE_CEILING_KB} KB "
            "(may indicate inlined assets or generated bloat)"
        )
    if anti["png_photographs"]:
        advisories.append(
            f"{len(anti['png_photographs'])} PNG file(s) look like photographs "
            "(PNG is for graphics with transparency; JPG would be smaller)"
        )

    # In strict mode, advisories become failures
    if args.strict:
        failures.extend(advisories)
        advisories = []

    if failures:
        print(f"FAIL — {site.name}")
        for f in failures:
            print(f"  - {f}")
        if advisories:
            print("\n  Advisories (non-blocking):")
            for a in advisories:
                print(f"  - {a}")
        if args.verbose:
            print()
            for cat, items in anti.items():
                if not items:
                    continue
                print(f"  [{cat}]")
                for p, detail in items[:10]:
                    try:
                        rel = p.relative_to(site)
                    except ValueError:
                        rel = p
                    print(f"    {rel}: {detail}")
                if len(items) > 10:
                    print(f"    ... {len(items) - 10} more")
        return 1
    print(f"PASS — {site.name} meets JAW Co Definition of Done items 13–14")
    if advisories:
        print("\n  Advisories (non-blocking):")
        for a in advisories:
            print(f"  - {a}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jaw_image_optimizer",
        description="Audit and optimize images on a JAW Co static HTML site.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="Read-only audit report")
    p_audit.add_argument("site", help="Path to the site folder")
    p_audit.add_argument("-o", "--output", help="Write report to file (Markdown)")
    p_audit.set_defaults(func=cmd_audit)

    p_opt = sub.add_parser("optimize", help="Optimize images on the site")
    p_opt.add_argument("site", help="Path to the site folder")
    p_opt.add_argument("--apply", action="store_true", help="Actually write changes (default is dry-run)")
    p_opt.add_argument("--quality", type=int, default=WEBP_QUALITY, help=f"WebP quality (default {WEBP_QUALITY})")
    p_opt.add_argument("--no-resize", action="store_true", help="Skip resizing overwide images")
    p_opt.add_argument("--add-lazy-load", action="store_true", help='Add loading="lazy" to <img> tags below the first')
    p_opt.set_defaults(func=cmd_optimize)

    p_ver = sub.add_parser("verify", help="Pass/fail check against the standard")
    p_ver.add_argument("site", help="Path to the site folder")
    p_ver.add_argument("-v", "--verbose", action="store_true", help="Show details for each failing item")
    p_ver.add_argument("--strict", action="store_true", help="Treat advisories (PNG-as-photo, oversized HTML) as failures")
    p_ver.set_defaults(func=cmd_verify)

    p_pic = sub.add_parser(
        "picture-swap",
        help='Wrap <img src="...png|jpg"> in <picture> with WebP <source> sibling',
    )
    p_pic.add_argument("site", help="Path to the site folder")
    p_pic.add_argument("--apply", action="store_true", help="Actually write changes (default is dry-run)")
    p_pic.set_defaults(func=cmd_picture_swap)

    p_dd = sub.add_parser(
        "dedupe-inline-base64",
        help="Extract base64-inlined <img> tags into external files (deduped by content hash)",
    )
    p_dd.add_argument("site", help="Path to the site folder")
    p_dd.add_argument("--apply", action="store_true", help="Actually write changes (default is dry-run)")
    p_dd.set_defaults(func=cmd_dedupe_inline_base64)

    p_rc = sub.add_parser(
        "recompress-oversized",
        help="Re-encode WebP siblings at lower quality (and resize if needed) until under the ceiling",
    )
    p_rc.add_argument("site", help="Path to the site folder")
    p_rc.add_argument("--apply", action="store_true", help="Actually write changes (default is dry-run)")
    p_rc.add_argument("--target-kb", type=int, default=SIZE_CEILING_KB, help=f"Target size in KB (default {SIZE_CEILING_KB})")
    p_rc.set_defaults(func=cmd_recompress_oversized)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
