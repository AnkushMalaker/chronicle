from advanced_omi_backend.services.timeline.prompt import OUTPUT_SCHEMA


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
