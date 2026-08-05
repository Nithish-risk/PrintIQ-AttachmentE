"""CLI harness: run the DI geometry layer + Step 10 LLM reconstruction on a
saved Azure Document Intelligence JSON file and print a readable before/after
diff of the structured fields.

Usage
-----
    python -m tools.run_reconstruction path/to/azure_document_intelligence_output.json
    python tools/run_reconstruction.py path/to/output.json --json out.json

If the Azure OpenAI environment variables are not set (or the SDK is missing),
Step 10 is a no-op and the diff will simply show "no changes" — so this harness
is safe to run offline to sanity-check the geometry layer alone.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make the sibling ``modules`` package importable when run as a script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from modules import di_postprocessor as pp  # noqa: E402
from modules import di_llm_reconstructor as llm  # noqa: E402


def _load_dotenv(path: str) -> int:
    """Minimal .env loader (no dependency). Sets vars that aren't already set.

    Supports ``KEY=VALUE`` lines, ``#`` comments, optional surrounding quotes,
    and an optional leading ``export``. Returns the number of vars applied.
    """
    if not os.path.isfile(path):
        return 0
    applied = 0
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[len("export ") :]
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # Do not clobber a value already present in the real environment.
            if key and key not in os.environ:
                os.environ[key] = val
                applied += 1
    return applied


def _fmt_value(value) -> str:
    """Compact one-line rendering of a field value (string or composite dict)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}={v!r}" for k, v in value.items()) + "}"
    return repr(value)


def _summary(field: dict) -> str:
    """One-line signature of a field for diffing."""
    return (
        f"[p{field.get('page')}] {field.get('kind')} "
        f"section={field.get('section')!r} sub={field.get('subsection')!r} "
        f"key={field.get('key')!r} value={_fmt_value(field.get('value'))}"
    )


def _key(field: dict) -> tuple:
    """Stable identity for pairing before/after fields (page + bbox)."""
    bbox = field.get("bbox") or [0, 0, 0, 0]
    return (field.get("page"), tuple(round(v, 4) for v in bbox))


def _print_diff(before: list[dict], after: list[dict]) -> None:
    after_by_key = {_key(f): f for f in after}
    changed = 0
    for b in before:
        a = after_by_key.get(_key(b))
        if a is None:
            print(f"  DROPPED  {_summary(b)}")
            changed += 1
            continue
        if _summary(a) != _summary(b):
            changed += 1
            print(f"  BEFORE   {_summary(b)}")
            print(f"  AFTER    {_summary(a)}")
            print()
    print(f"\n{changed} of {len(before)} fields changed by Step 10.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", help="Path to the DI output JSON file.")
    parser.add_argument(
        "--json",
        dest="out_path",
        default=None,
        help="Optional path to write the final reconstructed fields as JSON.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip Step 10 entirely (geometry layer only).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Surface the real Step 10 exception/validation reason.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file with AZURE_OPENAI_* vars (default: .env).",
    )
    parser.add_argument("--endpoint", default=None, help="Azure OpenAI endpoint URL.")
    parser.add_argument("--api-key", default=None, help="Azure OpenAI API key.")
    parser.add_argument("--deployment", default=None, help="Azure OpenAI deployment name.")
    parser.add_argument("--api-version", default=None, help="Azure OpenAI API version.")
    args = parser.parse_args(argv)

    # Credentials precedence: explicit CLI flag > real env var > .env file.
    for flag, var in (
        (args.endpoint, llm.AZURE_OPENAI_ENDPOINT),
        (args.api_key, llm.AZURE_OPENAI_API_KEY),
        (args.deployment, llm.AZURE_OPENAI_DEPLOYMENT),
        (args.api_version, llm.AZURE_OPENAI_API_VERSION),
    ):
        if flag:
            os.environ[var] = flag
    loaded = _load_dotenv(args.env_file)
    if loaded:
        print(f"Loaded {loaded} var(s) from {args.env_file}")

    with open(args.json_path, "r", encoding="utf-8") as fh:
        analysis = json.load(fh)

    print(f"Loaded: {args.json_path}")
    before = pp.structure_document(analysis)
    bands = pp.detect_section_bands(analysis)
    lines = pp.page_lines(analysis)
    print(f"Geometry layer produced {len(before)} structured fields.")
    print(
        "Section bands per page: "
        + ", ".join(f"p{p}={len(b)}" for p, b in sorted(bands.items()))
    )

    if args.no_llm:
        after = before
        print("\n(--no-llm) Skipping Step 10.\n")
    else:
        llm_ready = bool(
            os.environ.get(llm.AZURE_OPENAI_ENDPOINT)
            and os.environ.get(llm.AZURE_OPENAI_API_KEY)
            and os.environ.get(llm.AZURE_OPENAI_DEPLOYMENT)
        )
        if not llm_ready:
            print(
                "\nAzure OpenAI env vars not fully set "
                f"({llm.AZURE_OPENAI_ENDPOINT}, {llm.AZURE_OPENAI_API_KEY}, "
                f"{llm.AZURE_OPENAI_DEPLOYMENT}); Step 10 will be a no-op.\n"
            )
        after = llm.reconstruct(before, bands, lines_by_page=lines, debug=args.debug)

    print("=" * 72)
    print("STEP 10 DIFF (geometry -> reconstructed)")
    print("=" * 72)
    _print_diff(before, after)

    if args.out_path:
        with open(args.out_path, "w", encoding="utf-8") as fh:
            json.dump(after, fh, ensure_ascii=False, indent=2)
        print(f"\nWrote reconstructed fields to {args.out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
