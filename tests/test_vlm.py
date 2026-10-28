import json

import httpx
import pytest

from tower_rebuild.schema import Audit, Settings
from tower_rebuild.vlm import VlmClient, require_local_url, parse_answer, response_answer, inline_schema


@pytest.mark.parametrize("url", ["https://api.example.com/v1", "http://192.168.1.5/v1", "http://127.0.0.1.evil/v1", "file:///x", "http://user:secret@localhost/v1"])
def test_local_only(url):
    with pytest.raises(ValueError):
        require_local_url(url)


def test_singleton_object_wrapper_is_safe_but_multiple_objects_are_rejected():
    assert parse_answer('[{"findings": ["unclear mark"]}]', Audit).findings == ["unclear mark"]
    assert parse_answer('```json\n{"findings": []}\n```', Audit).findings == []
    with pytest.raises(ValueError):
        parse_answer('[{"findings": []}, {"findings": ["conflict"]}]', Audit)


def test_evidence_corner_order_is_normalized_without_clipping_invalid_values():
    from tower_rebuild.schema import Evidence
    assert parse_answer('{"page":1,"bbox":[0.1,0.9,0.5,0.2],"text":"mark"}', Evidence).bbox == (.1,.2,.5,.9)
    with pytest.raises(ValueError):
        parse_answer('{"page":1,"bbox":[-0.1,0.9,0.5,0.2],"text":"mark"}', Evidence)


def test_gateway_tool_result_with_end_turn_is_accepted_but_truncation_is_not():
    raw = {"stop_reason": "end_turn", "content": [{"type": "tool_use", "name": "record_drawing", "input": {"findings": []}}]}
    assert parse_answer(response_answer(raw), Audit).findings == []
    raw["stop_reason"] = "max_tokens"
    with pytest.raises(ValueError, match="不完整"):
        response_answer(raw)


def test_single_redundant_evidence_wrapper_preserves_the_record():
    from tower_rebuild.schema import NodeObservation
    raw = {"id": "n1", "xy": [.2,.3], "evidence": {"page": {"page": 1, "bbox": [.1,.2,.3,.4], "text": "node"}}}
    result = parse_answer(json.dumps(raw), NodeObservation)
    assert result.evidence.page == 1 and result.xy == (.2,.3)


def test_gateway_schema_expands_nested_objects_and_homogeneous_tuples():
    from tower_rebuild.schema import Face
    schema = inline_schema(Face.model_json_schema())
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema
    xy = schema["properties"]["nodes"]["items"]["properties"]["xy"]
    assert xy["items"]["type"] == "number" and xy["minItems"] == xy["maxItems"] == 2


def test_structured_vision_request_and_cache(tmp_path):
    requests = []
    image = tmp_path / "crop.png"
    image.write_bytes(b"image-one")
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "guided_json" not in body
        assert body["structured_outputs"]["json"] == Audit.model_json_schema()
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"findings":[]}'}}]})
    client = VlmClient(Settings(model="local-vision"), tmp_path / "cache", httpx.MockTransport(handler))
    try:
        assert not client.ask("audit", Audit, "read drawing", [image]).findings
        client.ask("audit", Audit, "read drawing", [image])
        assert len(requests) == 1
        image.write_bytes(b"changed-image")
        client.ask("audit", Audit, "read drawing", [image])
        assert len(requests) == 2
    finally:
        client.close()


def test_truncated_result_never_becomes_valid_cache(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": '{"findings":[]}'}}]}))
    client = VlmClient(Settings(model="local-vision"), tmp_path, transport)
    try:
        with pytest.raises(ValueError, match="不完整"):
            client.ask("audit", Audit, "test")
        assert len(list(tmp_path.glob("*.response.json"))) == 2
        assert not [p for p in tmp_path.glob("*.json") if not p.name.endswith(".response.json")]
    finally:
        client.close()


def test_anthropic_vision_and_structured_output(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "dummy-test-value")
    image = tmp_path / "crop.png"
    image.write_bytes(b"fake-test-image")
    def handler(request):
        assert request.url.path == "/apps/anthropic/v1/messages"
        assert request.headers["x-api-key"] == "dummy-test-value"
        body = json.loads(request.content)
        assert body["output_config"]["format"]["schema"] == Audit.model_json_schema()
        assert body["messages"][0]["content"][1]["source"]["type"] == "base64"
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"findings":[]}'}]})
    settings = Settings(provider="anthropic", model="qwen3.7-plus", base_url="https://coding.dashscope.aliyuncs.com/apps/anthropic",
                        allow_remote=True, api_key_env="TEST_API_KEY")
    client = VlmClient(settings, tmp_path / "cache", httpx.MockTransport(handler))
    try:
        client.ask("audit", Audit, "test", [image])
        assert all("dummy-test-value" not in p.read_text(encoding="utf-8") for p in (tmp_path / "cache").glob("*.json"))
    finally:
        client.close()
