"""LLM-assisted validation helpers.

  * Phase C.1 — ``align_checkbox_options``: map Excel checkbox options to the
    labels detected in the PDF.
  * Phase C.2 — ``verify_kv_pairs``: assess each matched key/value pairing.
  * Phase D/E — ``validate_instruction``: judge a printed value against its
    print instruction (and, when the value is unknown, its ``if_unknown`` rule).

All functions are **best-effort and guardrailed**: the LLM only aligns, judges,
or flags — it never invents options or changes checkbox states. Any error,
missing config, or invalid model output falls back to the deterministic result.

The Azure OpenAI client is reused from ``AzureOpenAIHelper`` so these calls use
the exact same working configuration as the rest of the app.
"""

from __future__ import annotations

import json
from typing import List, Optional

from config.settings import settings
from modules.azure_openai_helper import AzureOpenAIHelper

# Single shared helper (builds the AzureOpenAI client once, if configured).
_helper: Optional[AzureOpenAIHelper] = None


def _get_helper() -> AzureOpenAIHelper:
    global _helper
    if _helper is None:
        _helper = AzureOpenAIHelper()
    return _helper


def _chat_json(messages: list, max_tokens: int = 400) -> Optional[dict]:
    """Call the shared chat model and parse a JSON object from the reply."""
    helper = _get_helper()
    if not helper.enabled or helper.client is None:
        return None
    try:
        resp = helper.client.chat.completions.create(
            model=settings.AZURE_OPENAI_DEPLOYMENT,
            messages=messages,
            temperature=0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        return json.loads(content)
    except Exception:
        # Retry once without response_format for models that don't support it.
        try:
            resp = helper.client.chat.completions.create(
                model=settings.AZURE_OPENAI_DEPLOYMENT,
                messages=messages,
                temperature=0,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content or ""
            start = content.find("{")
            end = content.rfind("}")
            if start >= 0 and end > start:
                return json.loads(content[start : end + 1])
        except Exception:
            return None
    return None


def align_checkbox_options(
    *,
    section: str,
    subsection: str,
    item: str,
    example: str,
    excel_options: List[str],
    di_options: List[dict],
) -> Optional[dict]:
    """Ask the LLM to map each Excel option to a DI option (or null).

    Returns a dict ``{"pairs": [{"excel": str, "di": str|null}], ...}`` or None
    on any failure. GUARDRAILS enforced by the caller: the LLM must only choose
    ``di`` labels from ``di_options``; it must never invent labels or change
    states. States are NOT sent for it to modify — they stay authoritative in
    the deterministic layer.
    """
    if not excel_options or not di_options:
        return None

    di_labels = [d.get("label", "") for d in di_options]
    system = (
        "You align checkbox option labels between an Excel print-rule spec and "
        "the labels actually detected in a PDF. You ONLY match labels. You must "
        "NEVER invent a PDF label that is not in the provided list, and you must "
        "NEVER decide or change whether a box is checked. Return strict JSON."
    )
    user = {
        "context": {
            "section": section,
            "subsection": subsection,
            "item": item,
            "example": example,
        },
        "excel_options": excel_options,
        "pdf_options": di_labels,
        "instructions": (
            "For each excel option, choose the single best matching pdf option "
            "from pdf_options, or null if none matches. Respond as JSON: "
            '{"pairs": [{"excel": "<excel option>", "di": "<pdf option or null>"}]}'
        ),
    }
    result = _chat_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ]
    )
    if not isinstance(result, dict) or "pairs" not in result:
        return None
    # Validate: every di value must be one of di_labels (or null).
    allowed = set(di_labels)
    clean_pairs = []
    for p in result.get("pairs", []):
        if not isinstance(p, dict):
            continue
        ex = p.get("excel")
        di = p.get("di")
        if di is not None and di not in allowed:
            di = None  # reject hallucinated label
        clean_pairs.append({"excel": ex, "di": di})
    if not clean_pairs:
        return None
    return {"pairs": clean_pairs}


def validate_instruction(
    *,
    item: str,
    instruction: str,
    printed_value: str,
    example: str = "",
    max_chars: str = "",
    char_size: str = "",
    bold: str = "",
    font: str = "",
    is_unknown_value: bool = False,
    if_unknown: str = "",
) -> Optional[dict]:
    """Phase D: judge whether *printed_value* obeys the print *instruction*.

    Returns ``{"verdict": "pass"|"fail", "reason": str}`` or None on any failure.
    GUARDRAILS: the model judges ONLY whether the printed value *follows the
    rule's format/instruction* — it must NEVER judge whether the value is the
    *correct* value for the label, and must NEVER invent source data.

    Because we cannot (and must not) verify a value's correctness against an
    external source, when the instruction only dictates a *format* (e.g. a date
    like ``<month> <dd>, <yyyy>``), the model compares the printed value's
    FORMAT against the ``example`` provided from the Excel rules: same format ->
    pass, different format -> fail.

    Phase E hook: when ``is_unknown_value`` is True and an ``if_unknown`` rule is
    provided, the model additionally checks the value against that rule.
    """
    if not (instruction or if_unknown):
        return None

    system = (
        "You are a strict print-rule FORMAT validator. You are given a print "
        "rule's instruction, an example value from the spec, and the value "
        "actually printed in a PDF. Decide ONLY whether the printed value "
        "FOLLOWS the instruction's format and the supporting formatting "
        "constraints. You must NEVER judge whether the value is the correct "
        "value for the field, and you must NEVER invent source data. When the "
        "instruction dictates a format, compare the printed value's format "
        "against the example's format (same shape = pass, different = fail). "
        "Return strict JSON with verdict 'pass' or 'fail' only."
    )
    payload = {
        "item": item,
        "instruction": instruction,
        "example": example,
        "printed_value": printed_value,
        "constraints": {
            "max_chars": max_chars,
            "char_size": char_size,
            "bold": bold,
            "font": font,
        },
    }
    if is_unknown_value and if_unknown:
        payload["unknown_value_rule"] = if_unknown
        payload["note"] = (
            "The printed value is an unknown/placeholder value. First check the "
            "main instruction, then also check the unknown_value_rule."
        )
    user = {
        "data": payload,
        "instructions": (
            'Return JSON: {"verdict": "pass"|"fail", "reason": "short '
            'explanation focused on whether the FORMAT matches"}.'
        ),
    }
    result = _chat_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ],
        max_tokens=300,
    )
    if not isinstance(result, dict):
        return None
    verdict = str(result.get("verdict") or "").lower().strip()
    # Accept legacy "needs_source_data" gracefully by mapping it to no clear
    # verdict so the engine's own format-vs-example fallback decides.
    if verdict not in {"pass", "fail"}:
        return {"verdict": verdict or "unknown", "reason": str(result.get("reason") or "")}
    return {"verdict": verdict, "reason": str(result.get("reason") or "")}


def verify_kv_pairs(records: List[dict], max_items: int = 200) -> Optional[List[dict]]:
    """Final verification (Phase C.2): assess each matched K-V pair.

    ``records`` = list of ``{rule_id, section, subsection, item, example, di_value}``.
    Returns a verdict list ``{rule_id, verdict, note}`` for EVERY row, where
    ``verdict`` is "ok" or "irregular". The LLM may flag concerns and give a
    short note, but must never change any value. Returns None on failure.
    """
    if not records:
        return None
    trimmed = records[:max_items]
    system = (
        "You are a QA reviewer. Given rows pairing an Excel print rule (item + "
        "example) with the value detected in a PDF, assess whether each pairing "
        "looks consistent. Judge only whether the PDF value plausibly belongs to "
        "that rule/item (mismatched field, value clearly belongs elsewhere, "
        "obvious label/value swap = irregular). Do NOT judge correctness of the "
        "underlying data and do NOT invent values. Return strict JSON."
    )
    user = {
        "rows": trimmed,
        "instructions": (
            "For EVERY row return a verdict. JSON: {\"results\": [{\"rule_id\": "
            "\"...\", \"verdict\": \"ok\"|\"irregular\", \"note\": \"short reason\"}]}. "
            "Use verdict 'ok' with note '' when the pairing looks fine."
        ),
    }
    result = _chat_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ],
        max_tokens=1500,
    )
    if not isinstance(result, dict):
        return None
    rows = result.get("results")
    if not isinstance(rows, list):
        # Backward-compat: accept the old "findings" key too.
        rows = result.get("findings")
    if not isinstance(rows, list):
        return None
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("rule_id"):
            continue
        verdict = str(r.get("verdict") or ("irregular" if r.get("note") else "ok"))
        out.append(
            {
                "rule_id": str(r["rule_id"]),
                "verdict": verdict,
                "note": str(r.get("note") or ""),
            }
        )
    return out
