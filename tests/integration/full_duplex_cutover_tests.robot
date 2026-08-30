*** Settings ***
Documentation    One-shot full-duplex replacement gates executed inside the ordinary
...              mock integration suite. These tests invoke the real backend handlers,
...              Redis coordinators, recovery consumers, and fake Swiggy mode; they are
...              not a feature-flagged or separately skipped test profile.
Library          Process
Variables        ../setup/test_env.py

Test Tags        e2e


*** Keywords ***
Run Backend Contract Tests
    [Documentation]    Run selected backend tests in the isolated production image.
    [Arguments]    @{targets}

    ${result}=    Run Process
    ...    ${CONTAINER_ENGINE}
    ...    exec
    ...    ${BACKEND_CONTAINER}
    ...    python
    ...    -m
    ...    pytest
    ...    -q
    ...    -c
    ...    /app/pyproject.toml
    ...    -p
    ...    no:cacheprovider
    ...    @{targets}
    Log    ${result.stdout}
    Log    ${result.stderr}
    Should Be Equal As Integers    ${result.rc}    0
    ...    Backend contract gate failed:\n${result.stdout}\n${result.stderr}


*** Test Cases ***
Audio V2 Enforces Bound Capture Playback And Connection Fences
    [Documentation]    Audio-v2 contract tests cover generated controls, atomic media,
    ...                exact connection leases, durable packet ACKs, playback, and
    ...                stale-binding rejection.
    Run Backend Contract Tests
    ...    /workspace/backends/advanced/tests/test_audio_protocol_v2.py
    ...    /workspace/backends/advanced/tests/test_audio_v2_ingress.py
    ...    /workspace/backends/advanced/tests/test_audio_v2_downlink.py
    ...    /workspace/backends/advanced/tests/test_client_connection_lease.py
    ...    /workspace/backends/advanced/tests/test_audio_durability.py
    ...    /workspace/backends/advanced/tests/test_response_coordinator.py
    ...    /workspace/backends/advanced/tests/test_voice_session_coordinator.py
    ...    /workspace/backends/advanced/tests/test_voice_properties.py

Wake Consumer Contract And Post Stop Conversation Materialization Stay Wired
    [Documentation]    Guards the two deployed multi-service failures found by the
    ...                real-browser replay: health must reject a wake service consuming
    ...                the retired Redis contract, and speech detected after stop_capture
    ...                must still materialize a Conversation from the terminal capture.
    Run Backend Contract Tests
    ...    /workspace/backends/advanced/tests/test_wakeword_health_contract.py
    ...    /workspace/backends/advanced/tests/test_conversation_lifecycle.py

Committed Turn Consumers Recover Without Duplicate Routes Or Effects
    [Documentation]    Exercises the real committed-turn router and pending-stream
    ...                recovery paths, including worker replacement and the irreversible
    ...                effect fence that suppresses stale speech without replaying writes.
    Run Backend Contract Tests
    ...    /workspace/backends/advanced/tests/test_committed_turn_routing.py
    ...    /workspace/backends/advanced/tests/test_stream_consumer_recovery.py
    ...    /workspace/backends/advanced/tests/test_interaction_modes.py::test_worker_recovers_an_input_stranded_in_another_consumer
    ...    /workspace/backends/advanced/tests/test_interaction_modes.py::test_effect_fence_finishes_mutation_but_suppresses_stale_speech

Fake Swiggy Completes Multi Item UPI QR Flow Without Payment
    [Documentation]    Covers bounded multi-item collection, explicit ordinals, ambiguous
    ...                number phrases, interruption during search/cart/checkout, Redis
    ...                redelivery, deterministic confirm order, and fake UPI QR creation.
    Run Backend Contract Tests
    ...    /workspace/backends/advanced/tests/test_swiggy_instamart_mode.py
