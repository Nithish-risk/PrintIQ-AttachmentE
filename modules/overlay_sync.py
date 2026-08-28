"""Single-source overlay preparation for PrintIQ.

Both the UI preview and downloaded PDF must consume the exact same list of
label-resolved ValidationResult objects. This module centralizes that contract.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, List, Dict, Any
from modules.visual_overlay import resolve_label_targets, group_results_by_page

@dataclass
class OverlayBundle:
    results: List
    by_page: Dict[int, List]
    audit: Dict[str, Any]
    enabled_statuses: tuple[str, ...]


def prepare_overlay_bundle(pdf_path, final_results: Iterable, enabled_statuses: Iterable[str]) -> OverlayBundle:
    enabled = tuple(enabled_statuses)
    enabled_set = set(enabled)
    selected = [r for r in final_results if r.status.value in enabled_set]
    resolved, audit = resolve_label_targets(pdf_path, selected)
    audit = dict(audit or {})
    audit.update({
        "filtered_final_results": len(selected),
        "resolved_result_ids": [getattr(r, "id", "") for r in resolved],
        "enabled_statuses": list(enabled),
        "overlay_dataset": "LABEL_ONLY_FINAL_REVIEWER_RESULTS",
    })
    return OverlayBundle(
        results=resolved,
        by_page=group_results_by_page(resolved),
        audit=audit,
        enabled_statuses=enabled,
    )
