from __future__ import annotations

from datetime import datetime, timezone
from textwrap import wrap
from typing import Any


PAGE_WIDTH = 612
PAGE_HEIGHT = 792
LEFT_MARGIN = 48
TOP_MARGIN = 744
BOTTOM_MARGIN = 52
LINE_HEIGHT = 13


def _pdf_escape(value: str) -> str:
    text = value.encode("latin-1", errors="replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _safe_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text or fallback


def provenance_report_filename(version_name: str | None, version_uid: str | None) -> str:
    label = _safe_text(version_name, _safe_text(version_uid, "main")).lower()
    safe = "".join(ch if ch.isalnum() else "-" for ch in label).strip("-")
    return f"inlumen-provenance-{safe or 'main'}.pdf"


def _lines_for_event(index: int, event: dict[str, Any]) -> list[tuple[str, int]]:
    timestamp = _safe_text(event.get("created_at"), "unknown time")
    actor = _safe_text(event.get("actor"), "unknown actor")
    action = _safe_text(event.get("action"), "change")
    summary = _safe_text(event.get("summary"), "Pipeline graph was modified.")

    header = f"{index}. {timestamp} - {actor} - {action}"
    lines: list[tuple[str, int]] = [(header, 10)]
    for line in wrap(summary, width=92) or [""]:
        lines.append((line, 9))
    details = event.get("details")
    if isinstance(details, dict) and details:
        user_query = _safe_text(details.get("user_query"))
        if user_query:
            for line in wrap(f"User query: {user_query}", width=92):
                lines.append((line, 8))
        compact = "; ".join(
            f"{key}: {_safe_text(value)}"
            for key, value in details.items()
            if key not in {"query", "raw_query", "user_query"}
        )
        if compact:
            for line in wrap(f"Details: {compact}", width=92):
                lines.append((line, 8))
    lines.append(("", 5))
    return lines


def _report_lines(payload: dict[str, Any]) -> list[tuple[str, int]]:
    version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
    pipeline = payload.get("pipeline") if isinstance(payload.get("pipeline"), dict) else {}
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    lines: list[tuple[str, int]] = [
        ("inLUMEN Provenance Report", 18),
        ("", 7),
        (f"Generated: {generated_at}", 9),
        (f"Pipeline: {_safe_text(pipeline.get('name') or pipeline.get('label'), 'Current design pipeline')}", 9),
        (f"Version: {_safe_text(version.get('name'), 'Main')} ({_safe_text(version.get('uid'), 'main')})", 9),
        (f"Events recorded: {len(events)}", 9),
        ("", 10),
        ("Modification Log", 13),
        ("", 6),
    ]

    if not events:
        lines.append(("No provenance events have been recorded for this version yet.", 9))
        return lines

    for index, event in enumerate(events, start=1):
        event_dict = event if isinstance(event, dict) else {}
        lines.extend(_lines_for_event(index, event_dict))
    return lines


def build_provenance_pdf(payload: dict[str, Any]) -> bytes:
    pages: list[list[tuple[str, int]]] = [[]]
    y = TOP_MARGIN
    for text, size in _report_lines(payload):
        needed = LINE_HEIGHT if text else max(size, 6)
        if y - needed < BOTTOM_MARGIN:
            pages.append([])
            y = TOP_MARGIN
        pages[-1].append((text, size))
        y -= needed

    objects: list[bytes] = []

    def add_object(content: bytes) -> int:
        objects.append(content)
        return len(objects)

    font_obj = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_objs: list[int] = []
    content_objs: list[int] = []

    for page_number, page_lines in enumerate(pages, start=1):
        y = TOP_MARGIN
        commands = ["BT"]
        for text, size in page_lines:
            if text:
                commands.append(f"/F1 {size} Tf")
                commands.append(f"1 0 0 1 {LEFT_MARGIN} {y} Tm")
                commands.append(f"({_pdf_escape(text)}) Tj")
            y -= LINE_HEIGHT if text else max(size, 6)
        commands.append("/F1 8 Tf")
        commands.append(f"1 0 0 1 {PAGE_WIDTH - 104} 30 Tm")
        commands.append(f"(Page {page_number} of {len(pages)}) Tj")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1", errors="replace")
        content_obj = add_object(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream"
        )
        content_objs.append(content_obj)

    pages_obj_placeholder = len(objects) + len(pages) + 1
    for content_obj in content_objs:
        page_obj = add_object(
            (
                f"<< /Type /Page /Parent {pages_obj_placeholder} 0 R "
                f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
                f"/Contents {content_obj} 0 R >>"
            ).encode("ascii")
        )
        page_objs.append(page_obj)

    pages_obj = add_object(
        (
            f"<< /Type /Pages /Count {len(page_objs)} /Kids "
            f"[{' '.join(f'{obj} 0 R' for obj in page_objs)}] >>"
        ).encode("ascii")
    )
    catalog_obj = add_object(f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode("ascii"))

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj_number, content in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{obj_number} 0 obj\n".encode("ascii"))
        pdf.extend(content)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_obj} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)
