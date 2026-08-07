from pathlib import Path

import yaml

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


def test_prompt_version_is_pinned():
    """Changing the prompt text without bumping this leaves cached runs on the old rules.

    Asserted once, here — not inside each content test, where two of them disagreeing is
    the only thing a second copy can achieve.
    """

    assert PROMPT_VERSION == "timeline-episodes-v10"


def test_prompt_treats_observations_as_coarse_sessions_without_cross_app_merging():
    prompt = build_prompt("result.json")

    assert "coarse application session" in prompt
    assert "Do not merge distinct foreground applications" in prompt
    assert "manufacture periodic sub-events" in prompt


def test_prompt_tells_the_agent_to_leave_confirmed_intervals_alone():
    prompt = build_prompt("result.json")

    assert "Confirmed episodes" in prompt
    assert "do not re-segment them" in prompt


def test_prompt_keeps_one_activity_in_one_episode_across_modalities():
    """A game with a friend must not split into a screen episode and an audio episode."""

    prompt = build_prompt("result.json")

    assert "One activity spans every modality that evidences it" in prompt
    assert "modality split, not two events" in prompt
    assert (
        "Only create a standalone audio episode when no concurrent foreground" in prompt
    )


def test_shipped_config_pins_the_same_prompt_version():
    """Run identity comes from config, not this constant — they must not drift.

    `request_timeline_analysis` keys a run on `timeline.prompt_version` from
    `config/defaults.yml`. A prompt edit that bumps only the constant changes nothing:
    completed days stay cached on the old rules and no reanalysis is triggered.
    """

    defaults = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "config" / "defaults.yml").read_text()
    )

    assert defaults["timeline"]["prompt_version"] == PROMPT_VERSION
