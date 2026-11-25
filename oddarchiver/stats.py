"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Disc usage breakdown for `oddarchiver status --stats` and `--stats-exact`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oddarchiver.catalog import safe_display
from oddarchiver.log import _fmt_bytes
from oddarchiver.session import _encoded_size_upper_bound
from oddarchiver.volume import parse_blob_session

if TYPE_CHECKING:
    from oddarchiver.disc import DiscInfo
    from oddarchiver.manifest import Manifest, ManifestEntry

logger = logging.getLogger(__name__)
_log = logger

_SESSION_PREFIX = re.compile(r"^session_(\d{3})/")
_SESSION_CONTROL = frozenset({"manifest.enc", "manifest.json", "enc_mode.json"})
_ROOT_CONTROL = frozenset({
    "volume.json", "volume_baseline.enc", "volume_baseline.json",
})
_TOP_OTHER = 5


@dataclass
class CountBytes:
    """File count plus on-disc byte total."""

    files: int = 0
    nbytes: int = 0

    def as_json(self) -> dict[str, int]:
        """Input: none. Output: JSON object with files and bytes."""
        return {"files": self.files, "bytes": self.nbytes}


@dataclass
class DedupCount:
    """Dedup hits and plaintext bytes avoided on disc."""

    files: int = 0
    saved_bytes: int = 0

    def as_json(self) -> dict[str, int]:
        """Input: none. Output: JSON object with files and saved_bytes."""
        return {"files": self.files, "saved_bytes": self.saved_bytes}


@dataclass
class DeletedCount:
    """Count of deleted paths."""

    files: int = 0

    def as_json(self) -> dict[str, int]:
        """Input: none. Output: JSON object with files."""
        return {"files": self.files}


@dataclass
class SessionReport:
    """Per-session category totals."""

    session: int
    timestamp: str
    original: CountBytes = field(default_factory=CountBytes)
    changed_full: CountBytes = field(default_factory=CountBytes)
    changed_delta: CountBytes = field(default_factory=CountBytes)
    dedup: DedupCount = field(default_factory=DedupCount)
    deleted: DeletedCount = field(default_factory=DeletedCount)
    control: int = 0
    other: int = 0
    filesystem_overhead: int = 0

    def as_json(self) -> dict:
        """Input: none. Output: JSON object for one session."""
        return {
            "session": self.session,
            "timestamp": self.timestamp,
            "original": self.original.as_json(),
            "changed_full": self.changed_full.as_json(),
            "changed_delta": self.changed_delta.as_json(),
            "dedup": self.dedup.as_json(),
            "deleted": self.deleted.as_json(),
            "control": {"bytes": self.control},
            "other": {"bytes": self.other},
            "filesystem_overhead": {"bytes": self.filesystem_overhead},
        }


@dataclass
class Totals:
    """Whole-disc category totals."""

    original: CountBytes = field(default_factory=CountBytes)
    changed_full: CountBytes = field(default_factory=CountBytes)
    changed_delta: CountBytes = field(default_factory=CountBytes)
    dedup: DedupCount = field(default_factory=DedupCount)
    deleted: DeletedCount = field(default_factory=DeletedCount)
    control: int = 0
    other: int = 0
    filesystem_overhead: int = 0

    def as_json(self) -> dict:
        """Input: none. Output: JSON object for whole-disc totals."""
        return {
            "original": self.original.as_json(),
            "changed_full": self.changed_full.as_json(),
            "changed_delta": self.changed_delta.as_json(),
            "dedup": self.dedup.as_json(),
            "deleted": self.deleted.as_json(),
            "control": {"bytes": self.control},
            "other": {"bytes": self.other},
            "filesystem_overhead": {"bytes": self.filesystem_overhead},
        }


@dataclass
class DiscStats:
    """Computed disc usage report."""

    mode: str
    label: str
    capacity_bytes: int
    used_bytes: int
    remaining_bytes: int
    totals: Totals
    sessions: list[SessionReport]
    suspect_sessions: list[int]
    other_files: list[tuple[str, int]] = field(default_factory=list)
    approximate: bool = False

    def as_json(self) -> dict:
        """Input: none. Output: JSON object matching the status --json shape."""
        return {
            "mode": self.mode,
            "label": self.label,
            "capacity_bytes": self.capacity_bytes,
            "used_bytes": self.used_bytes,
            "remaining_bytes": self.remaining_bytes,
            "totals": self.totals.as_json(),
            "sessions": [item.as_json() for item in self.sessions],
            "suspect_sessions": list(self.suspect_sessions),
        }


def compute_stats(
    manifests: list[Manifest],
    control_raw: dict[str, bytes],
    disc_info: DiscInfo,
    crypto: object,
    exact_sizes: dict[str, int] | None = None,
) -> DiscStats:
    """
    Input:  manifests - session manifests in disc order
            control_raw - control-file bytes already read
            disc_info - capacity snapshot
            crypto - backend used to bound encoded blob size
            exact_sizes - on-disc sizes from list_file_sizes, or None
    Output: DiscStats for text or JSON render
    Details:
        Estimate mode uses control_raw lengths and encoded-size bounds.
        Exact mode uses exact_sizes for blobs, control files, and other.
    """
    reports, suspects, approximate = _walk_sessions(
        manifests, crypto, exact_sizes,
    )
    control_sizes = _control_sizes(control_raw, exact_sizes)
    total_control = _add_control(reports, control_sizes)
    other_files = _other_files(exact_sizes, _referenced_blobs(manifests))
    total_other = _add_other(reports, other_files)
    totals = _sum_sessions(reports)
    totals.control = total_control
    totals.other = total_other
    data = (
        totals.original.nbytes
        + totals.changed_full.nbytes
        + totals.changed_delta.nbytes
    )
    totals.filesystem_overhead = _overhead(
        disc_info.used_bytes, data, total_control, total_other,
    )
    used = disc_info.used_bytes
    remaining = disc_info.remaining_bytes
    return DiscStats(
        mode="exact" if exact_sizes is not None else "estimate",
        label=disc_info.label or "",
        capacity_bytes=used + remaining,
        used_bytes=used,
        remaining_bytes=remaining,
        totals=totals,
        sessions=reports,
        suspect_sessions=suspects,
        other_files=other_files,
        approximate=approximate,
    )


def render_json(report: DiscStats) -> str:
    """
    Input:  report - computed disc stats
    Output: JSON object text
    """
    return json.dumps(report.as_json())


def render_text(report: DiscStats) -> str:
    """
    Input:  report - computed disc stats
    Output: plain text tables after the normal status lines
    Details:
        Estimate mode uses an estimate header and a ~ prefix on bounded
        blob sizes. Exact mode prints listed other files by size.
    """
    lines = _totals_lines(report)
    for session in report.sessions:
        lines.append("")
        lines.extend(_session_lines(session, report.mode == "estimate"))
    if report.suspect_sessions:
        lines.append("")
        lines.append(_suspect_line(report.suspect_sessions))
    if report.mode == "exact":
        lines.extend(_other_lines(report.other_files))
    if report.approximate:
        lines.append("")
        lines.append("Volume-split blob sizes are approximate.")
    return "\n".join(lines) + "\n"


def _totals_lines(report: DiscStats) -> list[str]:
    """Input: report. Output: whole-disc header and category lines."""
    estimate = report.mode == "estimate"
    kind = "estimate" if estimate else "exact"
    lines = [
        f"Disc usage ({kind}):",
        f"  Capacity:             {_fmt_bytes(report.capacity_bytes)}",
        f"  Used:                 {_fmt_bytes(report.used_bytes)}",
        f"  Free:                 {_fmt_bytes(report.remaining_bytes)}",
    ]
    lines.extend(_category_lines(report.totals, estimate, indent="  ", whole_disc=True))
    return lines


def _session_lines(session: SessionReport, estimate: bool) -> list[str]:
    """Input: one session report. Output: heading plus category lines."""
    stamp = session.timestamp or "-"
    heading = f"Session {session.session:03d}  {safe_display(stamp)}"
    lines = [heading]
    lines.extend(_category_lines(session, estimate, indent="  ", whole_disc=False))
    return lines


def _category_lines(
    block: Totals | SessionReport, estimate: bool, indent: str, whole_disc: bool,
) -> list[str]:
    """
    Input: totals or session block, mode, indent, and whether block is the whole disc.
    Output: category rows. Estimate mode has no other row. Only the whole disc
    has a filesystem_overhead row, because the code measures overhead per disc.
    """
    lines = [
        f"{indent}original:            {_count_bytes(block.original, estimate)}",
        f"{indent}changed_full:        {_count_bytes(block.changed_full, estimate)}",
        f"{indent}changed_delta:       {_count_bytes(block.changed_delta, estimate)}",
        (
            f"{indent}dedup:               {block.dedup.files:>6} files  "
            f"{_fmt_bytes(block.dedup.saved_bytes)} saved"
        ),
        f"{indent}deleted:             {block.deleted.files:>6} files",
        f"{indent}control:                    {_fmt_bytes(block.control)}",
    ]
    if not estimate:
        lines.append(f"{indent}other:                      {_fmt_bytes(block.other)}")
    if whole_disc:
        lines.append(
            f"{indent}filesystem_overhead:        "
            f"{_fmt_bytes(block.filesystem_overhead)}"
        )
    return lines


def _count_bytes(item: CountBytes, estimate: bool) -> str:
    """Input: count plus bytes. Output: aligned files and size text."""
    prefix = "~" if estimate else ""
    return f"{item.files:>6} files  {prefix}{_fmt_bytes(item.nbytes)}"


def _suspect_line(sessions: list[int]) -> str:
    """Input: suspect session numbers. Output: one notice line."""
    names = ", ".join(f"{n:03d}" for n in sessions)
    noun = "session" if len(sessions) == 1 else "sessions"
    return f"Skipped {len(sessions)} SUSPECT {noun}: {names}"


def _other_lines(other_files: list[tuple[str, int]]) -> list[str]:
    """Input: other files by size. Output: top-size rows, or none."""
    if not other_files:
        return []
    lines = ["", "  other files:"]
    for path, size in other_files[:_TOP_OTHER]:
        lines.append(f"    {safe_display(path):<40} {_fmt_bytes(size)}")
    extra = len(other_files) - _TOP_OTHER
    if extra > 0:
        lines.append(f"    ({extra} more)")
    return lines


def _walk_sessions(
    manifests: list[Manifest],
    crypto: object,
    exact_sizes: dict[str, int] | None,
) -> tuple[list[SessionReport], list[int], bool]:
    """
    Input:  manifests, crypto, optional exact size map
    Output: session reports, suspect session numbers, approximate flag
    """
    live: set[str] = set()
    reports: list[SessionReport] = []
    suspects: list[int] = []
    approximate = False
    for manifest in manifests:
        if manifest.suspect:
            suspects.append(manifest.session)
            reports.append(_blank_session(manifest))
            continue
        report, approx = _session_report(manifest, live, crypto, exact_sizes)
        approximate = approximate or approx
        reports.append(report)
        _apply_live(live, manifest)
    return reports, suspects, approximate


def _blank_session(manifest: Manifest) -> SessionReport:
    """Input: suspect manifest. Output: zeroed session report."""
    return SessionReport(session=manifest.session, timestamp=manifest.timestamp)


def _session_report(
    manifest: Manifest,
    live: set[str],
    crypto: object,
    exact_sizes: dict[str, int] | None,
) -> tuple[SessionReport, bool]:
    """
    Input:  one trusted manifest and the live path set at session start
    Output: session report and whether any volume-split estimate ran
    """
    report = SessionReport(session=manifest.session, timestamp=manifest.timestamp)
    owned: set[str] = set()
    approximate = False
    for entry in manifest.entries:
        if exact_sizes is None and entry.volume_chunk is not None:
            approximate = True
        category = _classify_entry(entry, manifest.session, live, owned)
        nbytes = _category_bytes(category, entry, crypto, exact_sizes)
        _record_entry(report, category, entry, nbytes)
    report.deleted.files = len(manifest.deleted)
    return report, approximate


def _classify_entry(
    entry: ManifestEntry,
    session: int,
    live: set[str],
    owned: set[str],
) -> str:
    """
    Input:  entry, session number, live paths, blobs owned earlier this session
    Output: original, changed_full, changed_delta, or dedup
    """
    if _is_dedup(entry, session, owned):
        return "dedup"
    blob = _blob_path(entry)
    if blob:
        owned.add(blob)
    if entry.type == "delta":
        return "changed_delta"
    if entry.path in live:
        return "changed_full"
    return "original"


def _is_dedup(entry: ManifestEntry, session: int, owned: set[str]) -> bool:
    """Input: entry and ownership state. Output: whether this entry is a reuse."""
    if _cross_session_ref(entry, session):
        return True
    blob = _blob_path(entry)
    return bool(blob) and blob in owned


def _cross_session_ref(entry: ManifestEntry, session: int) -> bool:
    """Input: entry and session. Output: whether the blob lives elsewhere."""
    origin = entry.blob_origin_session
    if origin is not None and origin != session:
        return True
    blob = _blob_path(entry)
    if not blob:
        return False
    try:
        return parse_blob_session(blob) != session
    except ValueError:
        _log.warning("Invalid blob path %r in session %s", blob, session)
        return False


def _blob_path(entry: ManifestEntry) -> str:
    """Input: manifest entry. Output: on-disc blob path, or empty."""
    if entry.type == "delta":
        return entry.delta_file or ""
    return entry.file or ""


def _category_bytes(
    category: str,
    entry: ManifestEntry,
    crypto: object,
    exact_sizes: dict[str, int] | None,
) -> int:
    """Input: category and entry. Output: disc bytes, or 0 for dedup."""
    if category == "dedup":
        return 0
    return _entry_disc_bytes(entry, crypto, exact_sizes)


def _record_entry(
    report: SessionReport,
    category: str,
    entry: ManifestEntry,
    nbytes: int,
) -> None:
    """Input: session report, category, entry, disc bytes. Output: none."""
    if category == "dedup":
        report.dedup.files += 1
        report.dedup.saved_bytes += entry.full_size_bytes
        return
    if category == "changed_full":
        report.changed_full.files += 1
        report.changed_full.nbytes += nbytes
        return
    if category == "changed_delta":
        report.changed_delta.files += 1
        report.changed_delta.nbytes += nbytes
        return
    report.original.files += 1
    report.original.nbytes += nbytes


def _entry_disc_bytes(
    entry: ManifestEntry,
    crypto: object,
    exact_sizes: dict[str, int] | None,
) -> int:
    """
    Input:  entry, crypto, optional exact size map
    Output: on-disc blob size in bytes
    """
    if exact_sizes is not None:
        return _exact_blob_size(entry, exact_sizes)
    plaintext = _estimate_plaintext(entry)
    return _encoded_size_upper_bound(plaintext, crypto)


def _exact_blob_size(entry: ManifestEntry, exact_sizes: dict[str, int]) -> int:
    """Input: entry and size map. Output: listed size, or 0 if missing."""
    blob = _blob_path(entry)
    size = exact_sizes.get(blob)
    if size is not None:
        return size
    _log.warning("Missing on-disc size for %s", blob)
    return 0


def _estimate_plaintext(entry: ManifestEntry) -> int:
    """
    Input:  manifest entry
    Output: plaintext bytes used for the encoded-size bound
    Details:
        Volume-split entries store the whole-file size in full_size_bytes.
        Each chunk uses full_size_bytes / volume_chunk_count.
    """
    if entry.volume_chunk is None:
        if entry.type == "delta":
            return entry.delta_size_bytes
        return entry.full_size_bytes
    count = entry.volume_chunk_count or 0
    if count <= 0:
        _log.warning(
            "volume_chunk is set without a positive volume_chunk_count"
        )
        return entry.full_size_bytes
    return entry.full_size_bytes // count


def _apply_live(live: set[str], manifest: Manifest) -> None:
    """Input: live path set and a trusted manifest. Output: none."""
    for entry in manifest.entries:
        live.add(entry.path)
    for path in manifest.deleted:
        live.discard(path)


def _control_session(path: str) -> int | None:
    """Input: disc-relative path. Output: session number, or None."""
    name = path.rsplit("/", 1)[-1]
    match = _SESSION_PREFIX.match(path)
    if match is not None and name in _SESSION_CONTROL:
        return int(match.group(1))
    return None


def _is_control_path(path: str) -> bool:
    """Input: disc-relative path. Output: whether it is a control file."""
    if path in _ROOT_CONTROL:
        return True
    return _control_session(path) is not None


def _control_sizes(
    control_raw: dict[str, bytes],
    exact_sizes: dict[str, int] | None,
) -> dict[str, int]:
    """Input: raw control bytes and optional size map. Output: control sizes."""
    if exact_sizes is not None:
        return {p: sz for p, sz in exact_sizes.items() if _is_control_path(p)}
    return {p: len(b) for p, b in control_raw.items() if _is_control_path(p)}


def _add_control(reports: list[SessionReport], control_sizes: dict[str, int]) -> int:
    """Input: session reports and control sizes. Output: total control bytes."""
    by_session = {item.session: item for item in reports}
    total = 0
    for path, size in control_sizes.items():
        total += size
        sess = _control_session(path)
        if sess is not None and sess in by_session:
            by_session[sess].control += size
    return total


def _referenced_blobs(manifests: list[Manifest]) -> set[str]:
    """Input: manifests. Output: blob paths named by any entry."""
    paths: set[str] = set()
    for manifest in manifests:
        for entry in manifest.entries:
            blob = _blob_path(entry)
            if blob:
                paths.add(blob)
    return paths


def _other_files(
    exact_sizes: dict[str, int] | None,
    blob_paths: set[str],
) -> list[tuple[str, int]]:
    """Input: size map and blob paths. Output: other files, largest first."""
    if exact_sizes is None:
        return []
    items = []
    for path, size in exact_sizes.items():
        if path in blob_paths or _is_control_path(path):
            continue
        items.append((path, size))
    items.sort(key=lambda item: (-item[1], item[0]))
    return items


def _add_other(
    reports: list[SessionReport],
    other_files: list[tuple[str, int]],
) -> int:
    """Input: session reports and other files. Output: total other bytes."""
    by_session = {item.session: item for item in reports}
    total = 0
    for path, size in other_files:
        total += size
        match = _SESSION_PREFIX.match(path)
        if match is None:
            continue
        sess = int(match.group(1))
        if sess in by_session:
            by_session[sess].other += size
    return total


def _sum_sessions(reports: list[SessionReport]) -> Totals:
    """Input: per-session reports. Output: summed file categories."""
    totals = Totals()
    for item in reports:
        _add_count(totals.original, item.original)
        _add_count(totals.changed_full, item.changed_full)
        _add_count(totals.changed_delta, item.changed_delta)
        totals.dedup.files += item.dedup.files
        totals.dedup.saved_bytes += item.dedup.saved_bytes
        totals.deleted.files += item.deleted.files
    return totals


def _add_count(dst: CountBytes, src: CountBytes) -> None:
    """Input: destination and source counts. Output: none."""
    dst.files += src.files
    dst.nbytes += src.nbytes


def _overhead(used: int, data: int, control: int, other: int) -> int:
    """
    Input:  used bytes and accounted data, control, and other
    Output: used minus accounted, clamped at 0
    """
    overhead = used - (data + control + other)
    if overhead < 0:
        _log.warning(
            "Filesystem overhead is negative (%s bytes); clamping to 0",
            overhead,
        )
        return 0
    return overhead
