from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


PAGE_WIDTH, PAGE_HEIGHT = letter
LEFT_MARGIN = 0.62 * inch
RIGHT_MARGIN = 0.62 * inch
TOP_MARGIN = 0.58 * inch
BOTTOM_MARGIN = 0.55 * inch
MAX_EVOLUTION_SNAPSHOTS = 20
MAX_DIAGRAM_NODES = 24


NODE_COLORS = {
    "input": colors.HexColor("#D8F3DC"),
    "action": colors.HexColor("#DCEBFF"),
    "output": colors.HexColor("#E7E2F7"),
    "config": colors.HexColor("#E8ECEF"),
    "storage": colors.HexColor("#FCE8CF"),
    "api": colors.HexColor("#D7F1EF"),
    "custom": colors.HexColor("#E8ECEF"),
}


def _safe_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text or fallback


def _paragraph_text(value: Any, fallback: str = "") -> str:
    return escape(_safe_text(value, fallback))


def provenance_report_filename(version_name: str | None, version_uid: str | None) -> str:
    label = _safe_text(version_name, _safe_text(version_uid, "main")).lower()
    safe = "".join(ch if ch.isalnum() else "-" for ch in label).strip("-")
    return f"inlumen-provenance-{safe or 'main'}.pdf"


def _flow_id_sort_key(value: Any) -> tuple[int, Any]:
    text = _safe_text(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def _compact_detail(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return _safe_text(value)


def _snapshot_signature(snapshot: dict[str, Any]) -> str:
    nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
    edges = snapshot.get("edges") if isinstance(snapshot.get("edges"), list) else []
    normalized = {
        "nodes": sorted(
            [
                {
                    "id": _safe_text(node.get("id")),
                    "label": _safe_text(node.get("label")),
                    "type": _safe_text(node.get("type")),
                    "x": round(float(node.get("x") or 0), 1),
                    "y": round(float(node.get("y") or 0), 1),
                }
                for node in nodes
                if isinstance(node, dict)
            ],
            key=lambda node: _flow_id_sort_key(node["id"]),
        ),
        "edges": sorted(
            [
                {
                    "source": _safe_text(edge.get("source")),
                    "target": _safe_text(edge.get("target")),
                }
                for edge in edges
                if isinstance(edge, dict)
            ],
            key=lambda edge: (
                _flow_id_sort_key(edge["source"]),
                _flow_id_sort_key(edge["target"]),
            ),
        ),
    }
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _representative_snapshot_events(
    events: list[Any],
    current_snapshot: dict[str, Any] | None = None,
    current_timestamp: str = "",
) -> tuple[list[dict[str, Any]], int]:
    distinct: list[dict[str, Any]] = []
    previous_signature = None
    for index, raw_event in enumerate(events, start=1):
        if not isinstance(raw_event, dict):
            continue
        details = raw_event.get("details")
        snapshot = details.get("graph_snapshot") if isinstance(details, dict) else None
        if not isinstance(snapshot, dict):
            continue
        signature = _snapshot_signature(snapshot)
        if signature == previous_signature:
            continue
        distinct.append({
            "event_index": index,
            "event": raw_event,
            "snapshot": snapshot,
        })
        previous_signature = signature

    if isinstance(current_snapshot, dict):
        current_signature = _snapshot_signature(current_snapshot)
        if current_signature != previous_signature:
            distinct.append({
                "event_index": None,
                "event": {
                    "action": "current_state",
                    "summary": "Current graph state for the selected version.",
                    "created_at": current_timestamp,
                },
                "snapshot": current_snapshot,
            })

    total = len(distinct)
    if total <= MAX_EVOLUTION_SNAPSHOTS:
        return distinct, total

    selected_indexes = {
        round(index * (total - 1) / (MAX_EVOLUTION_SNAPSHOTS - 1))
        for index in range(MAX_EVOLUTION_SNAPSHOTS)
    }
    return [distinct[index] for index in sorted(selected_indexes)], total


def _layered_positions(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    width: float,
    height: float,
) -> tuple[dict[str, tuple[float, float]], float, float]:
    node_ids = [_safe_text(node.get("id")) for node in nodes]
    node_id_set = set(node_ids)
    adjacency = {node_id: [] for node_id in node_ids}
    indegree = {node_id: 0 for node_id in node_ids}
    for edge in edges:
        source = _safe_text(edge.get("source"))
        target = _safe_text(edge.get("target"))
        if source not in node_id_set or target not in node_id_set or source == target:
            continue
        adjacency[source].append(target)
        indegree[target] += 1

    queue = sorted(
        [node_id for node_id in node_ids if indegree[node_id] == 0],
        key=_flow_id_sort_key,
    )
    levels = {node_id: 0 for node_id in queue}
    ordered: list[str] = []
    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for target in sorted(adjacency[current], key=_flow_id_sort_key):
            levels[target] = max(levels.get(target, 0), levels[current] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
                queue.sort(key=_flow_id_sort_key)

    next_level = max(levels.values(), default=-1) + 1
    for node_id in sorted(node_ids, key=_flow_id_sort_key):
        if node_id not in levels:
            levels[node_id] = next_level
            next_level += 1

    groups: dict[int, list[str]] = {}
    for node_id in node_ids:
        groups.setdefault(levels[node_id], []).append(node_id)
    for group in groups.values():
        group.sort(key=_flow_id_sort_key)

    level_count = max(len(groups), 1)
    max_rows = max((len(group) for group in groups.values()), default=1)
    if level_count > 10:
        ordered_ids = [
            node_id
            for level in sorted(groups)
            for node_id in groups[level]
        ]
        column_count = min(8, max(1, math.ceil(math.sqrt(len(ordered_ids) * 1.8))))
        row_count = max(1, math.ceil(len(ordered_ids) / column_count))
        node_width = max(42.0, min(70.0, width / column_count - 10.0))
        node_height = max(18.0, min(25.0, height / row_count - 8.0))
        positions = {}
        for index, node_id in enumerate(ordered_ids):
            column = index % column_count
            row = index // column_count
            positions[node_id] = (
                (column + 0.5) * width / column_count,
                height - (row + 0.5) * height / row_count,
            )
        return positions, node_width, node_height

    node_width = max(42.0, min(82.0, width / level_count - 12.0))
    node_height = max(18.0, min(25.0, height / max_rows - 8.0))
    positions: dict[str, tuple[float, float]] = {}
    for column, level in enumerate(sorted(groups)):
        group = groups[level]
        x = (column + 0.5) * width / level_count
        for row, node_id in enumerate(group):
            y = height - (row + 1) * height / (len(group) + 1)
            positions[node_id] = (x, y)
    return positions, node_width, node_height


class GraphSnapshotFlowable(Flowable):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__()
        nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
        edges = snapshot.get("edges") if isinstance(snapshot.get("edges"), list) else []
        self.nodes = [
            node for node in nodes if isinstance(node, dict)
        ][:MAX_DIAGRAM_NODES]
        visible_ids = {_safe_text(node.get("id")) for node in self.nodes}
        self.edges = [
            edge
            for edge in edges
            if isinstance(edge, dict)
            and _safe_text(edge.get("source")) in visible_ids
            and _safe_text(edge.get("target")) in visible_ids
        ]
        self.total_nodes = int(snapshot.get("node_count") or len(nodes))
        self.total_edges = int(snapshot.get("edge_count") or len(edges))
        self.snapshot_truncated = bool(snapshot.get("truncated"))
        self.width = 0
        self.height = 180

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        self.width = available_width
        level_rows = max(1, math.ceil(max(len(self.nodes), 1) / 6))
        self.height = min(235.0, max(155.0, 120.0 + level_rows * 20.0))
        return available_width, self.height

    def draw(self) -> None:
        canvas = self.canv
        canvas.saveState()
        canvas.setFillColor(colors.HexColor("#F7F9FA"))
        canvas.setStrokeColor(colors.HexColor("#CBD3D8"))
        canvas.roundRect(0, 0, self.width, self.height, 5, fill=1, stroke=1)

        footer_height = 22
        diagram_margin = 16
        diagram_width = self.width - 2 * diagram_margin
        diagram_height = self.height - footer_height - 2 * diagram_margin

        if not self.nodes:
            canvas.setFillColor(colors.HexColor("#68737B"))
            canvas.setFont("Helvetica", 9)
            canvas.drawCentredString(self.width / 2, self.height / 2, "Empty graph")
        else:
            positions, node_width, node_height = _layered_positions(
                self.nodes,
                self.edges,
                diagram_width,
                diagram_height,
            )

            canvas.setStrokeColor(colors.HexColor("#7D8991"))
            canvas.setLineWidth(0.8)
            for edge in self.edges:
                source = _safe_text(edge.get("source"))
                target = _safe_text(edge.get("target"))
                if source not in positions or target not in positions:
                    continue
                source_x, source_y = positions[source]
                target_x, target_y = positions[target]
                source_x += diagram_margin
                source_y += footer_height + diagram_margin
                target_x += diagram_margin
                target_y += footer_height + diagram_margin
                dx = target_x - source_x
                dy = target_y - source_y
                distance = math.hypot(dx, dy) or 1.0
                unit_x = dx / distance
                unit_y = dy / distance
                horizontal_offset = (
                    node_width / 2 / abs(unit_x)
                    if abs(unit_x) > 0.001
                    else float("inf")
                )
                vertical_offset = (
                    node_height / 2 / abs(unit_y)
                    if abs(unit_y) > 0.001
                    else float("inf")
                )
                boundary_offset = min(horizontal_offset, vertical_offset)
                start_x = source_x + unit_x * (boundary_offset + 1)
                start_y = source_y + unit_y * (boundary_offset + 1)
                end_x = target_x - unit_x * (boundary_offset + 2)
                end_y = target_y - unit_y * (boundary_offset + 2)
                canvas.line(start_x, start_y, end_x, end_y)
                arrow_size = 4.5
                angle = math.atan2(dy, dx)
                left_x = end_x - arrow_size * math.cos(angle - math.pi / 6)
                left_y = end_y - arrow_size * math.sin(angle - math.pi / 6)
                right_x = end_x - arrow_size * math.cos(angle + math.pi / 6)
                right_y = end_y - arrow_size * math.sin(angle + math.pi / 6)
                path = canvas.beginPath()
                path.moveTo(end_x, end_y)
                path.lineTo(left_x, left_y)
                path.lineTo(right_x, right_y)
                path.close()
                canvas.setFillColor(colors.HexColor("#7D8991"))
                canvas.drawPath(path, fill=1, stroke=0)

            node_by_id = {
                _safe_text(node.get("id")): node
                for node in self.nodes
            }
            for node_id, (x, y) in positions.items():
                node = node_by_id[node_id]
                node_type = _safe_text(node.get("type"), "custom").lower()
                canvas.setFillColor(NODE_COLORS.get(node_type, NODE_COLORS["custom"]))
                canvas.setStrokeColor(colors.HexColor("#66727A"))
                left = x + diagram_margin - node_width / 2
                bottom = y + footer_height + diagram_margin - node_height / 2
                canvas.roundRect(left, bottom, node_width, node_height, 4, fill=1, stroke=1)
                label = _safe_text(node.get("label"), node_type)
                max_label = max(5, int(node_width / 5.4))
                if len(label) > max_label:
                    label = label[: max_label - 3] + "..."
                canvas.setFillColor(colors.HexColor("#263238"))
                canvas.setFont("Helvetica-Bold", 7)
                canvas.drawCentredString(
                    left + node_width / 2,
                    bottom + node_height / 2 + (2 if node_height >= 22 else 0),
                    f"{node_id}: {label}",
                )
                if node_height >= 22:
                    canvas.setFont("Helvetica", 5.8)
                    canvas.setFillColor(colors.HexColor("#58636A"))
                    canvas.drawCentredString(
                        left + node_width / 2,
                        bottom + 4.5,
                        node_type,
                    )

        canvas.setStrokeColor(colors.HexColor("#D7DDE1"))
        canvas.line(10, footer_height, self.width - 10, footer_height)
        note = f"{self.total_nodes} node(s), {self.total_edges} edge(s)"
        if self.total_nodes > len(self.nodes) or self.snapshot_truncated:
            note += f"; diagram shows first {len(self.nodes)} nodes"
        canvas.setFillColor(colors.HexColor("#68737B"))
        canvas.setFont("Helvetica", 7)
        canvas.drawString(12, 8, note)
        canvas.restoreState()


def _page_footer(canvas: Canvas, document: SimpleDocTemplate) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D7DDE1"))
    canvas.line(LEFT_MARGIN, 32, PAGE_WIDTH - RIGHT_MARGIN, 32)
    canvas.setFillColor(colors.HexColor("#68737B"))
    canvas.setFont("Helvetica", 8)
    canvas.drawString(LEFT_MARGIN, 20, "inLUMEN Provenance Report")
    canvas.drawRightString(PAGE_WIDTH - RIGHT_MARGIN, 20, f"Page {document.page}")
    canvas.restoreState()


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            textColor=colors.HexColor("#1E2A30"),
            spaceAfter=12,
        ),
        "section": ParagraphStyle(
            "Section",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=16,
            textColor=colors.HexColor("#1E2A30"),
            spaceBefore=8,
            spaceAfter=8,
        ),
        "event_header": ParagraphStyle(
            "EventHeader",
            parent=base["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=9.5,
            leading=12,
            textColor=colors.HexColor("#263238"),
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#37474F"),
            spaceAfter=3,
        ),
        "small": ParagraphStyle(
            "Small",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9.5,
            textColor=colors.HexColor("#58636A"),
            spaceAfter=2,
        ),
        "note": ParagraphStyle(
            "Note",
            parent=base["BodyText"],
            fontName="Helvetica-Oblique",
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#68737B"),
            spaceAfter=8,
        ),
    }


def _event_story(index: int, event: dict[str, Any], styles: dict[str, ParagraphStyle]) -> list[Flowable]:
    timestamp = _safe_text(event.get("created_at"), "unknown time")
    actor = _safe_text(event.get("actor"), "unknown actor")
    action = _safe_text(event.get("action"), "change")
    summary = _safe_text(event.get("summary"), "Pipeline graph was modified.")
    story: list[Flowable] = [
        Paragraph(
            f"{index}. {_paragraph_text(timestamp)} - {_paragraph_text(actor)} - {_paragraph_text(action)}",
            styles["event_header"],
        ),
        Paragraph(_paragraph_text(summary), styles["body"]),
    ]
    details = event.get("details")
    if isinstance(details, dict) and details:
        user_query = _safe_text(details.get("user_query"))
        if user_query:
            story.append(
                Paragraph(
                    f"<b>User query:</b> {_paragraph_text(user_query)}",
                    styles["small"],
                )
            )
        visible_details = [
            f"<b>{_paragraph_text(key)}:</b> {_paragraph_text(_compact_detail(value))}"
            for key, value in details.items()
            if key not in {"query", "raw_query", "user_query", "graph_snapshot", "result"}
        ]
        if visible_details:
            story.append(Paragraph("; ".join(visible_details), styles["small"]))
    story.append(Spacer(1, 6))
    return story


def build_provenance_pdf(payload: dict[str, Any]) -> bytes:
    version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
    pipeline = payload.get("pipeline") if isinstance(payload.get("pipeline"), dict) else {}
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    styles = _styles()

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=letter,
        leftMargin=LEFT_MARGIN,
        rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        title="inLUMEN Provenance Report",
        author="inLUMEN",
    )

    metadata = [
        ["Generated", generated_at],
        ["Pipeline", _safe_text(pipeline.get("name") or pipeline.get("label"), "Current design pipeline")],
        ["Version", f"{_safe_text(version.get('name'), 'Main')} ({_safe_text(version.get('uid'), 'main')})"],
        ["Events recorded", str(len(events))],
    ]
    metadata_table = Table(metadata, colWidths=[1.15 * inch, 5.65 * inch])
    metadata_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#58636A")),
        ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#263238")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F7F9FA")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7DDE1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E4E8EA")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    story: list[Flowable] = [
        Paragraph("inLUMEN Provenance Report", styles["title"]),
        metadata_table,
        Spacer(1, 14),
        Paragraph("Modification Log", styles["section"]),
    ]

    if not events:
        story.append(
            Paragraph(
                "No provenance events have been recorded for this version yet.",
                styles["body"],
            )
        )
    else:
        for index, raw_event in enumerate(events, start=1):
            event = raw_event if isinstance(raw_event, dict) else {}
            story.extend(_event_story(index, event, styles))

    current_snapshot = (
        payload.get("current_graph_snapshot")
        if isinstance(payload.get("current_graph_snapshot"), dict)
        else None
    )
    snapshot_events, distinct_snapshot_count = _representative_snapshot_events(
        events,
        current_snapshot,
        _safe_text(version.get("updated_at")),
    )
    story.append(PageBreak())
    story.append(Paragraph("Graph Evolution", styles["section"]))
    if not snapshot_events:
        story.append(
            Paragraph(
                "No graph snapshots are available for these events. Snapshots are recorded for new modifications made after this reporting update.",
                styles["note"],
            )
        )
    else:
        if distinct_snapshot_count > len(snapshot_events):
            story.append(
                Paragraph(
                    f"Showing {len(snapshot_events)} representative states from {distinct_snapshot_count} distinct graph snapshots.",
                    styles["note"],
                )
            )
        else:
            story.append(
                Paragraph(
                    f"Showing {distinct_snapshot_count} distinct graph state(s). Consecutive duplicate structures are omitted.",
                    styles["note"],
                )
            )

        for item in snapshot_events:
            event = item["event"]
            if item["event_index"] is None:
                header = (
                    "Current version state"
                    + (
                        f" - {_safe_text(event.get('created_at'))}"
                        if _safe_text(event.get("created_at"))
                        else ""
                    )
                )
            else:
                header = (
                    f"After event {item['event_index']}: "
                    f"{_safe_text(event.get('action'), 'change')} - "
                    f"{_safe_text(event.get('created_at'), 'unknown time')}"
                )
            summary = _safe_text(event.get("summary"), "Pipeline graph was modified.")
            story.append(
                KeepTogether([
                    Paragraph(_paragraph_text(header), styles["event_header"]),
                    Paragraph(_paragraph_text(summary), styles["small"]),
                    Spacer(1, 3),
                    GraphSnapshotFlowable(item["snapshot"]),
                    Spacer(1, 12),
                ])
            )

    document.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return output.getvalue()
