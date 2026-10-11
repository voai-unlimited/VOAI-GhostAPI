import asyncio

import pytest

from voai_ghostapi.interactive import (
    _request_body_shape,
    _request_parameters,
    _sanitize_url,
    classify_interaction,
    discover_api_requests,
)


def test_interaction_classification_covers_read_only_recipes():
    assert classify_interaction({"action": "click", "kind": "tab", "selector": "#tab"}) == "tab"
    assert classify_interaction({"action": "click", "name": "next page", "selector": "#next"}) == "pagination"
    assert classify_interaction({"action": "scroll", "name": "load more"}) == "read-only"


def test_interaction_classification_rejects_unknown_and_destructive_actions():
    with pytest.raises(ValueError, match="only click"):
        classify_interaction({"action": "hover", "selector": "#menu"})
    with pytest.raises(ValueError, match="never run"):
        classify_interaction({"action": "click", "name": "delete account", "selector": "#delete"})
    with pytest.raises(ValueError, match="unknown interaction kind"):
        classify_interaction({"action": "click", "kind": "custom", "selector": "#x"})


def test_request_metadata_is_sanitized_and_deterministic():
    counter = [0]
    assert _sanitize_url("https://example.test/api?token=secret&q=ok", counter).endswith(
        "/api?token=<REDACTED>&q=ok"
    )
    assert _request_parameters("https://example.test/api?q=one&q=two&token=secret", counter) == {
        "q": ["one", "two"],
        "token": "<REDACTED>",
    }
    shape = _request_body_shape('{"name":"Ada","enabled":true}', counter)
    assert shape == {
        "type": ["object"],
        "properties": {"enabled": {"type": ["boolean"]}, "name": {"type": ["string"]}},
    }


def test_interactive_limits_are_validated_before_browser_start():
    with pytest.raises(ValueError, match="max_interactions"):
        asyncio.run(discover_api_requests("https://example.test", max_interactions=0))
    with pytest.raises(ValueError, match="time limit"):
        asyncio.run(discover_api_requests("https://example.test", max_seconds=0))
    with pytest.raises(ValueError, match="depth"):
        asyncio.run(
            discover_api_requests(
                "https://example.test",
                max_depth=0,
                interactions=[{"action": "scroll", "depth": 1}],
            )
        )
