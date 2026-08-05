"""Unit tests for the Step 10 LLM reconstructor.

These lock in two safety invariants that must hold regardless of what the model
returns:

1. The model may never mutate extracted ``value`` / ``kind`` / geometry.
2. A model ``key`` is only accepted when it is supported by printed page text
   (no hallucinated stems).

The Azure OpenAI client is mocked, so no network/credentials are needed.
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules import di_llm_reconstructor as llm  # noqa: E402


def _make_fake_client(payload: dict):
    """Return an object shaped like AzureOpenAI whose completion returns *payload*."""
    message = SimpleNamespace(content=json.dumps(payload))
    choice = SimpleNamespace(message=message)
    response = SimpleNamespace(choices=[choice])
    create = mock.Mock(return_value=response)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client, create


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(llm.AZURE_OPENAI_ENDPOINT, "https://example.openai.azure.com/")
    monkeypatch.setenv(llm.AZURE_OPENAI_API_KEY, "test-key")
    monkeypatch.setenv(llm.AZURE_OPENAI_DEPLOYMENT, "gpt-4.1")


def test_value_and_kind_are_never_mutated(monkeypatch):
    """Even if the model rewrites value/kind, geometry values are preserved."""
    original = [
        {
            "kind": "checkbox_group",
            "section": None,
            "subsection": None,
            "key": "Annulment",
            "value": "Death-unselected;Divorce-unselected",
            "page": 1,
            "bbox": [0.1, 0.1, 0.5, 0.2],
            "confidence": 0.99,
            "children": [{"option": "Death", "selected": False}],
        }
    ]
    # Model tries to corrupt value + kind, and relabel section/key.
    model_payload = {
        "fields": [
            {
                "kind": "text",  # should be ignored
                "section": "ELIGIBILITY",
                "subsection": None,
                "key": "PREVIOUS MARRIAGE ENDED BY",
                "value": "TOTALLY DIFFERENT",  # should be ignored
                "page": 1,
                "bbox": [0, 0, 0, 0],
            }
        ]
    }
    client, _create = _make_fake_client(model_payload)
    monkeypatch.setattr(llm, "_get_client", lambda debug=False: client)

    lines = {1: [{"text": "PREVIOUS MARRIAGE ENDED BY", "bbox": [0.1, 0.08, 0.5, 0.1]}]}
    out = llm.reconstruct(original, {}, lines_by_page=lines)

    assert len(out) == 1
    field = out[0]
    # Protected fields untouched.
    assert field["kind"] == "checkbox_group"
    assert field["value"] == "Death-unselected;Divorce-unselected"
    assert field["bbox"] == [0.1, 0.1, 0.5, 0.2]
    assert field["children"] == [{"option": "Death", "selected": False}]
    # Semantic relabelling applied.
    assert field["section"] == "ELIGIBILITY"
    assert field["key"] == "PREVIOUS MARRIAGE ENDED BY"


def test_hallucinated_key_is_rejected(monkeypatch):
    """A key not present in page text is discarded; geometry key is kept."""
    original = [
        {
            "kind": "checkbox_group",
            "section": None,
            "subsection": None,
            "key": "Bride",
            "value": "Bride-unselected;Groom-unselected",
            "page": 1,
            "bbox": [0.3, 0.18, 0.5, 0.2],
            "confidence": 0.99,
            "children": [],
        }
    ]
    model_payload = {
        "fields": [
            {
                "kind": "checkbox_group",
                "section": "ELIGIBILITY",
                "subsection": None,
                "key": "PROOF OF STERILITY",  # not on the page -> reject
                "value": "Bride-unselected;Groom-unselected",
                "page": 1,
                "bbox": [0.3, 0.18, 0.5, 0.2],
            }
        ]
    }
    client, _create = _make_fake_client(model_payload)
    monkeypatch.setattr(llm, "_get_client", lambda debug=False: client)

    # Nearby text does NOT contain "PROOF OF STERILITY".
    lines = {1: [{"text": "APPLICANT IS THE", "bbox": [0.3, 0.17, 0.5, 0.18]}]}
    out = llm.reconstruct(original, {}, lines_by_page=lines)

    # Section still applied, but hallucinated key rejected -> original kept.
    assert out[0]["section"] == "ELIGIBILITY"
    assert out[0]["key"] == "Bride"


def test_key_on_page_is_accepted():
    assert llm._key_is_on_page(
        "RACE - Check all that apply",
        ["race - check all that apply", "White"],
    )
    assert not llm._key_is_on_page("PROOF OF STERILITY", ["applicant is the"])
