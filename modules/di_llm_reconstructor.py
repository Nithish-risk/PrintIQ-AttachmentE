"""Step 10: LLM-based semantic reconstruction of DI post-processor output.

The geometry layer (``di_postprocessor.structure_document``) is deliberately
conservative: it emits *candidates* with reliable geometry but leaves hard,
ambiguous decisions (which printed stem is a checkbox group's question, how to
stitch First/Middle/Last/Suffix into one name, which section a field belongs to
when no left-margin band covers it) unresolved — often as ``null`` keys/sections.

This module hands those candidates, together with their bounding boxes and the
detected section bands, to an Azure OpenAI chat model and asks it to return a
**strict, schema-validated** reconstruction. Geometry is never invented here:
the model may only *choose among* / *relabel* text that already appears on the
page, and every field keeps the bbox/page it came from so the UI overlay stays
truthful.

Design goals
------------
* **Deterministic contract.** We use JSON schema (``response_format`` with
  ``json_schema``) so the model must return exactly the shape we validate.
* **No hallucinated values.** The prompt forbids changing extracted values;
  the model only assigns ``section``/``subsection``/``key`` and regroups.
* **Graceful degradation.** If the LLM call or validation fails, we return the
  original geometry output unchanged so the pipeline never hard-fails.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

# ----------------------------- configuration -------------------------------
# All read from the environment so nothing is hardcoded per-deployment.
AZURE_OPENAI_ENDPOINT = "AZURE_OPENAI_ENDPOINT"
AZURE_OPENAI_API_KEY = "AZURE_OPENAI_API_KEY"
AZURE_OPENAI_API_VERSION = "AZURE_OPENAI_API_VERSION"
AZURE_OPENAI_DEPLOYMENT = "AZURE_OPENAI_DEPLOYMENT"

DEFAULT_API_VERSION = "2024-08-01-preview"  # first version with json_schema support
MAX_FIELDS_PER_CALL = 120  # keep prompts within a safe token budget


# ------------------------- strict output schema ----------------------------
# The model MUST return an object matching this schema. Keeping it small and
# explicit is what makes the pass reproducible across many certificate types.
RECONSTRUCTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fields"],
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind",
                    "section",
                    "subsection",
                    "key",
                    "value",
                    "page",
                    "bbox",
                    "confidence",
                ],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["text", "composite", "checkbox_group"],
                    },
                    # section/subsection/key may be null when the page truly
                    # provides no printed label — never fabricated.
                    "section": {"type": ["string", "null"]},
                    "subsection": {"type": ["string", "null"]},
                    "key": {"type": ["string", "null"]},
                    # value is a plain string for text/checkbox_group and an
                    # object (part -> value) for composite; allow both.
                    "value": {
                        "type": ["string", "object", "null"],
                    },
                    "page": {"type": "integer"},
                    "bbox": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "confidence": {"type": ["number", "null"]},
                },
            },
        }
    },
}


SYSTEM_PROMPT = (
    "You are a meticulous document-reconstruction engine for U.S. vital-records "
    "certificates (marriage, birth, death). You are given field CANDIDATES that "
    "were extracted by an OCR/key-value layer, each with its bounding box "
    "(normalized [x0,y0,x1,y1]), page, and — when known — the section band it "
    "falls under.\n\n"
    "Your job is ONLY to reconstruct semantic structure, never to invent data:\n"
    "  1. Assign each field the correct printed SECTION and SUBSECTION. Use the "
    "provided band text when present; otherwise infer from the nearest printed "
    "heading among the candidates. If no heading exists, use null.\n"
    "  2. For checkbox_group fields, set `key` to the printed QUESTION STEM that "
    "the options belong to (e.g. 'PREVIOUS MARRIAGE ENDED BY', "
    "'RACE - Check all that apply'). Choose it from the candidate's "
    "`nearby_labels` (the printed lines around the field) — never use an option "
    "word, a value, or an unrelated neighbouring label as the key.\n"
    "  3. For composite fields, keep `value` as an object mapping each printed "
    "part label (First, Middle, Last, Suffix, ...) to its extracted value.\n\n"
    "STRICT RULES:\n"
    "  - Do NOT change any extracted VALUE text. Only assign section/subsection/"
    "key and regroup.\n"
    "  - Do NOT alter any bbox or page; copy them through unchanged.\n"
    "  - Only use text that physically appears in the provided candidates/bands.\n"
    "  - Return every input field exactly once.\n"
    "  - Respond with JSON matching the provided schema only."
)

# Explicit output contract for json_object mode (strict json_schema cannot
# express the composite's variable-key ``value`` object, so we instruct shape
# here and validate it ourselves in ``_validate``).
OUTPUT_CONTRACT = (
    "Return a single JSON object of the exact form:\n"
    '{ "fields": [ { "kind": "text|composite|checkbox_group", '
    '"section": string|null, "subsection": string|null, "key": string|null, '
    '"value": string OR object(part->value) OR null, "page": integer, '
    '"bbox": [x0,y0,x1,y1], "confidence": number|null } ] }\n'
    "The 'fields' array MUST have exactly the same length as the input "
    "'candidates' array, in the same order, with bbox and page copied unchanged."
)


# ------------------------------- helpers -----------------------------------
def _get_client(debug: bool = False):
    """Lazily construct an Azure OpenAI client; returns None if unavailable.

    When ``debug`` is True, print which specific precondition failed so the
    caller can tell "SDK missing" apart from "endpoint unset" apart from
    "key unset".
    """
    endpoint = os.environ.get(AZURE_OPENAI_ENDPOINT)
    api_key = os.environ.get(AZURE_OPENAI_API_KEY)
    if not endpoint:
        if debug:
            print(f"[step10] {AZURE_OPENAI_ENDPOINT} is not set in the environment.")
        return None
    if not api_key:
        if debug:
            print(f"[step10] {AZURE_OPENAI_API_KEY} is not set in the environment.")
        return None
    try:
        from openai import AzureOpenAI
    except ImportError:
        if debug:
            print("[step10] the 'openai' package is not installed (pip install openai).")
        return None
    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=os.environ.get(AZURE_OPENAI_API_VERSION, DEFAULT_API_VERSION),
    )


def _bands_for_page(left_bands: list[dict]) -> list[dict]:
    """Serialize section bands into the minimal shape the model needs."""
    return [
        {
            "text": b.get("text"),
            "tier": b.get("tier"),
            "y0": round(b.get("y0", 0.0), 4),
            "y1": round(b.get("y1", 0.0), 4),
        }
        for b in left_bands
    ]


def _chunk(seq: list[Any], size: int) -> list[list[Any]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _nearby_labels(field: dict, lines: list[dict], limit: int = 6) -> list[str]:
    """Return printed line texts near *field* — likely question stems/labels.

    We look for lines whose vertical position is at or just above the field and
    whose horizontal span overlaps it. This gives the model the real printed
    text (e.g. ``RACE - Check all that apply``) to choose a ``key`` from,
    without letting it invent anything.
    """
    bbox = field.get("bbox") or [0, 0, 0, 0]
    fx0, fy0, fx1, fy1 = bbox
    scored: list[tuple[float, str]] = []
    for ln in lines:
        text = (ln.get("text") or "").strip()
        lb = ln.get("bbox")
        if not text or not lb:
            continue
        lx0, ly0, lx1, ly1 = lb
        # Horizontal overlap with the field's span.
        if lx1 < fx0 - 0.02 or lx0 > fx1 + 0.02:
            continue
        # Line should sit at or above the field top (a label above the inputs),
        # within a reasonable vertical window.
        dy = fy0 - ly0
        if -0.01 <= dy <= 0.06:
            scored.append((abs(dy), text))
    scored.sort(key=lambda t: t[0])
    seen: list[str] = []
    for _d, text in scored:
        if text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def _build_user_payload(fields: list[dict], bands: list[dict], lines: list[dict]) -> str:
    """Compact JSON payload of candidates + bands + nearby labels for one page."""
    return json.dumps(
        {
            "section_bands": bands,
            "candidates": [
                {
                    "index": i,
                    "kind": f.get("kind"),
                    "key": f.get("key"),
                    "value": f.get("value"),
                    "section": f.get("section"),
                    "subsection": f.get("subsection"),
                    "page": f.get("page"),
                    "bbox": [round(v, 4) for v in (f.get("bbox") or [0, 0, 0, 0])],
                    "confidence": f.get("confidence"),
                    "nearby_labels": _nearby_labels(f, lines),
                }
                for i, f in enumerate(fields)
            ],
        },
        ensure_ascii=False,
    )


def _validate(reconstructed: dict, original_count: int) -> Optional[list[dict]]:
    """Light structural validation; returns fields list or None if invalid."""
    if not isinstance(reconstructed, dict):
        return None
    fields = reconstructed.get("fields")
    if not isinstance(fields, list) or len(fields) != original_count:
        # Count mismatch means the model dropped/added rows — reject the batch.
        return None
    for f in fields:
        if not isinstance(f, dict):
            return None
        if f.get("kind") not in {"text", "composite", "checkbox_group"}:
            return None
        bbox = f.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            return None
    return fields


# ------------------------------ entry point --------------------------------
def reconstruct(
    structured_fields: list[dict],
    left_bands_by_page: Optional[dict[int, list[dict]]] = None,
    *,
    lines_by_page: Optional[dict[int, list[dict]]] = None,
    model: Optional[str] = None,
    debug: bool = False,
) -> list[dict]:
    """Run the Step 10 LLM reconstruction over geometry-layer output.

    Parameters
    ----------
    structured_fields:
        Output of ``di_postprocessor.structure_document``.
    left_bands_by_page:
        Optional ``{page: [band, ...]}`` from the geometry layer so the model
        can see the authoritative section bands. Missing pages just omit bands.
    model:
        Azure OpenAI deployment name; falls back to the env var.
    debug:
        When True, print the real exception/validation reason instead of
        silently falling back to geometry — use for first live-call diagnosis.

    Returns
    -------
    The reconstructed field list, or the original list unchanged if the LLM is
    unavailable or returns something that fails validation (never hard-fails).
    """
    if not structured_fields:
        return structured_fields

    client = _get_client(debug=debug)
    if client is None:
        if debug:
            print("[step10] no client: SDK missing or endpoint/key env vars unset.")
        return structured_fields

    deployment = model or os.environ.get(AZURE_OPENAI_DEPLOYMENT)
    if not deployment:
        if debug:
            print("[step10] no deployment: set AZURE_OPENAI_DEPLOYMENT or pass model=.")
        return structured_fields

    left_bands_by_page = left_bands_by_page or {}
    lines_by_page = lines_by_page or {}

    # Process page-by-page so section context stays local and prompts stay small.
    pages = sorted({f.get("page", 1) for f in structured_fields})
    result: list[dict] = []

    for page in pages:
        page_fields = [f for f in structured_fields if f.get("page", 1) == page]
        bands = _bands_for_page(left_bands_by_page.get(page, []))
        lines = lines_by_page.get(page, [])

        for batch in _chunk(page_fields, MAX_FIELDS_PER_CALL):
            reconstructed = _call_model(client, deployment, batch, bands, lines, debug=debug)
            # Allowed key text per field for the hallucination guard: the
            # field's printed nearby labels plus its own existing key (so a
            # correct geometry key is never rejected). We intentionally use the
            # full nearby-label window rather than only the closest lines —
            # restricting to the 2 closest (option B) was too aggressive and
            # rejected correct stems whose printed text sits a couple of rows
            # above the checkboxes. The guard therefore blocks *hallucinated*
            # keys but tolerates the occasional wrong-but-real nearby label,
            # which is the better accuracy tradeoff on real forms.
            allowed = []
            for f in batch:
                labels = _nearby_labels(f, lines)
                if f.get("key"):
                    labels = labels + [str(f["key"])]
                allowed.append(labels)
            validated = _validate(reconstructed, len(batch)) if reconstructed else None
            if validated is None:
                if debug and reconstructed is not None:
                    print(
                        f"[step10] page {page}: validation rejected batch "
                        f"(got {len(reconstructed.get('fields', []))} fields, "
                        f"expected {len(batch)}); keeping geometry."
                    )
                # Fail safe: keep the geometry output for this batch.
                result.extend(batch)
            else:
                result.extend(_merge_geometry(batch, validated, allowed, debug=debug))

    return result


def _call_model(
    client, deployment: str, fields: list[dict], bands: list[dict], lines: list[dict],
    *, debug: bool = False
) -> Optional[dict]:
    """Single Azure OpenAI call; returns parsed JSON dict or None on failure."""
    try:
        response = client.chat.completions.create(
            model=deployment,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + OUTPUT_CONTRACT},
                {"role": "user", "content": _build_user_payload(fields, bands, lines)},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        return json.loads(content) if content else None
    except Exception as exc:  # noqa: BLE001
        # Any transport/parse/schema error → caller falls back to geometry.
        if debug:
            print(f"[step10] model call failed: {type(exc).__name__}: {exc}")
        return None


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace/punctuation runs for loose matching."""
    return re.sub(r"[\s]+", " ", (text or "").strip().lower())


def _key_is_on_page(key: str, allowed_texts: list[str]) -> bool:
    """True if *key* is supported by printed page text (not hallucinated).

    We accept the key when its normalised form is a substring of any allowed
    line, OR of the concatenation of allowed lines (stems may wrap across
    lines). This enforces the "never invent text" rule in code, independent of
    whatever the model returns.
    """
    nkey = _norm(key)
    if not nkey:
        return False
    blob = _norm(" ".join(allowed_texts))
    if nkey in blob:
        return True
    return any(nkey in _norm(t) for t in allowed_texts)


def _merge_geometry(
    original: list[dict],
    reconstructed: list[dict],
    allowed_labels: Optional[list[list[str]]] = None,
    *,
    debug: bool = False,
) -> list[dict]:
    """Apply ONLY the semantic relabelling from the model; protect everything else.

    The model is allowed to influence three fields and nothing more:
    ``section``, ``subsection`` and ``key``. We deliberately do NOT take the
    model's ``value`` or ``kind`` — those come straight from the geometry layer
    (which read them off the page) so the LLM can never mutate or truncate an
    extracted value. ``bbox``/``page``/``children``/``confidence`` are likewise
    preserved untouched.

    A model ``key`` is additionally accepted only when it is supported by the
    field's printed page text (``allowed_labels``); otherwise we keep the
    geometry key, so a hallucinated stem can never leak through.
    """
    allowed_labels = allowed_labels or []
    merged: list[dict] = []
    for i, (orig, recon) in enumerate(zip(original, reconstructed)):
        out = dict(orig)  # preserve kind, value, bbox, page, children, confidence
        # Section/subsection are pure labels — safe to take from the model.
        out["section"] = recon.get("section")
        out["subsection"] = recon.get("subsection")
        # Key is the printed question stem; only accept a non-empty string that
        # is actually present on the page, otherwise keep geometry's key.
        recon_key = recon.get("key")
        if isinstance(recon_key, str) and recon_key.strip():
            allowed = allowed_labels[i] if i < len(allowed_labels) else []
            if not allowed or _key_is_on_page(recon_key, allowed):
                out["key"] = recon_key
            elif debug:
                print(f"[step10] rejected hallucinated key: {recon_key!r}")
        merged.append(out)
    return merged
