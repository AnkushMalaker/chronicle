from advanced_omi_backend.services.timeline.prompt import (
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    build_prompt,
)


def _assert_strict_objects(schema: object) -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False
        for value in schema.values():
            _assert_strict_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_strict_objects(value)


def test_timeline_output_schema_has_no_open_objects():
    _assert_strict_objects(OUTPUT_SCHEMA)


def test_timeline_output_schema_requires_grounded_episodes_and_assertions():
    episode = OUTPUT_SCHEMA["properties"]["episodes"]["items"]
    assert episode["properties"]["evidence_ids"]["minItems"] == 1
    assertion = episode["properties"]["assertions"]["items"]
    assert assertion["properties"]["evidence_ids"]["minItems"] == 1


def test_prompt_treats_observations_as_coarse_sessions_without_cross_app_merging():
    prompt = build_prompt("result.json")

    assert PROMPT_VERSION == "timeline-episodes-v6"
    assert "coarse application session" in prompt
    assert "Do not merge distinct foreground applications" in prompt
    assert "manufacture periodic sub-events" in prompt
