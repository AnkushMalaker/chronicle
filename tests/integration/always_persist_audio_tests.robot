*** Settings ***
Documentation    Durable Audio Persistence Tests
...
...              Tests that verify raw audio is saved to MongoDB independently of
...              transcription success.
...
...              Critical scenarios:
...              - Technical capture session created immediately
...              - Audio chunks persisted despite transcription failure
...              - Semantic Conversation created only after speech

Resource         ../resources/websocket_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/mongodb_keywords.robot
Resource         ../resources/redis_keywords.robot
Resource         ../resources/queue_keywords.robot
Resource         ../resources/session_keywords.robot
Resource         ../resources/system_keywords.robot
Variables        ../setup/test_env.py

Suite Setup      Suite Setup Actions
Suite Teardown   Suite Teardown Actions
Test Teardown    Test Cleanup

*** Variables ***
${TEST_AUDIO_FILE}    ${CURDIR}/../test_assets/DIY_Experts_Glass_Blowing_16khz_mono_1min.wav

*** Keywords ***
Suite Setup Actions
    [Documentation]    Setup actions before running tests
    # Initialize API session for test user
    ${session}=    Get Admin API Session
    Set Suite Variable    ${API_SESSION}    ${session}

Suite Teardown Actions
    [Documentation]    Cleanup after all tests complete
    # Cleanup any remaining audio streams
    Cleanup All Audio Streams

Test Cleanup
    [Documentation]    Cleanup after each test
    # Stop any active audio streams
    Cleanup All Audio Streams
    Sleep    2s    # Allow backend to finalize processing

*** Test Cases ***

Capture Session Created Before Audio Ingress
    [Documentation]    Verify that durable capture identity exists before speech while no
    ...                semantic Conversation is materialized.
    [Tags]    conversation	audio-streaming

    ${device_name}=    Set Variable    test-placeholder
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Get baseline conversation count for THIS client_id only
    ${convs_before}=    Get Conversations By Client ID    ${client_id}
    ${count_before}=    Get Length    ${convs_before}
    ${stream_id}=    Open Durable Audio Stream    device_name=${device_name}
    ${session_id}=    Wait Until Keyword Succeeds    10s    250ms
    ...    Get Active Session ID For Client    ${client_id}
    ${capture}=    Wait Until Keyword Succeeds    10s    250ms
    ...    Get Capture Session By ID    ${session_id}

    Should Be Equal    ${capture}[capture_session_id]    ${session_id}
    Should Be Equal    ${capture}[capture_source_id]    ${client_id}
    Should Be Equal    ${capture}[status]    active
    Should Be Equal    ${capture}[origin]    streaming
    Should Be Equal As Integers    ${capture}[capture_epoch]    0
    Should Be Equal    ${capture}[processing_profile]    ambient
    Should Be Equal    ${capture}[effects][aec][reporting]    unreported
    Should Be Equal    ${capture}[effects][noise_suppression][reporting]    unreported

    ${convs_after}=    Get Conversations By Client ID    ${client_id}
    ${count_after}=    Get Length    ${convs_after}
    Should Be Equal As Integers    ${count_after}    ${count_before}
    ...    Silence before ingress must not create a semantic Conversation

    # Close stream
    Close Audio Stream    ${stream_id}

    Log    ✅ Durable capture session exists without a placeholder Conversation


Redis Capture State Set Before Audio Ingress
    [Documentation]    Verify Redis binds persistence to capture identity and carries
    ...                required provenance without a Conversation owner.
    [Tags]    audio-streaming	infra

    ${device_name}=    Set Variable    test-redis-key
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    ${stream_id}=    Open Durable Audio Stream    device_name=${device_name}

    # Every recording attempt has its own immutable session/WAL.
    ${session_id}=    Wait Until Keyword Succeeds    10s    250ms
    ...    Get Active Session ID For Client    ${client_id}

    ${session}=    Get Redis Session Data    ${session_id}
    Should Be Equal    ${session}[client_id]    ${client_id}
    Should Be Equal    ${session}[status]    active
    Should Be Equal    ${session}[processing_profile]    ambient
    Should Be Equal As Integers    ${session}[capture_epoch]    0
    Should Be Empty    ${session}[active_conversation_id]
    ${legacy_owner_exists}=    Redis Command    EXISTS    conversation:current:${session_id}
    Should Be Equal As Integers    ${legacy_owner_exists}    0

    Log    ✅ Redis session is capture-owned and has no provisional Conversation

    # Close stream
    Close Audio Stream    ${stream_id}


Multiple Streams Create Separate Capture Sessions
    [Documentation]    Verify that each recording attempt gets a distinct durable capture
    ...                session without creating a silent Conversation.
    [Tags]    conversation	audio-streaming

    # NOTE: Device names must be <=10 chars to be unique (backend truncates to 10 chars)
    # Using short names: multi-1, multi-2, multi-3 (7 chars each)

    # Get client IDs for each device
    ${client_id_1}=    Get Client ID From Device Name    multi-1
    ${client_id_2}=    Get Client ID From Device Name    multi-2
    ${client_id_3}=    Get Client ID From Device Name    multi-3

    # Get baseline conversation counts for each client
    ${convs_before_1}=    Get Conversations By Client ID    ${client_id_1}
    ${convs_before_2}=    Get Conversations By Client ID    ${client_id_2}
    ${convs_before_3}=    Get Conversations By Client ID    ${client_id_3}
    ${count_before_1}=    Get Length    ${convs_before_1}
    ${count_before_2}=    Get Length    ${convs_before_2}
    ${count_before_3}=    Get Length    ${convs_before_3}
    # Start 3 separate sessions
    ${stream_1}=    Open Durable Audio Stream    device_name=multi-1
    Sleep    1s
    ${stream_2}=    Open Durable Audio Stream    device_name=multi-2
    Sleep    1s
    ${stream_3}=    Open Durable Audio Stream    device_name=multi-3

    ${session_id_1}=    Wait Until Keyword Succeeds    10s    250ms
    ...    Get Active Session ID For Client    ${client_id_1}
    ${session_id_2}=    Wait Until Keyword Succeeds    10s    250ms
    ...    Get Active Session ID For Client    ${client_id_2}
    ${session_id_3}=    Wait Until Keyword Succeeds    10s    250ms
    ...    Get Active Session ID For Client    ${client_id_3}

    Should Not Be Equal    ${session_id_1}    ${session_id_2}
    Should Not Be Equal    ${session_id_2}    ${session_id_3}
    Should Not Be Equal    ${session_id_1}    ${session_id_3}

    ${capture_1}=    Get Capture Session By ID    ${session_id_1}
    ${capture_2}=    Get Capture Session By ID    ${session_id_2}
    ${capture_3}=    Get Capture Session By ID    ${session_id_3}
    Should Be Equal    ${capture_1}[processing_profile]    ambient
    Should Be Equal    ${capture_2}[processing_profile]    ambient
    Should Be Equal    ${capture_3}[processing_profile]    ambient

    ${convs_after_1}=    Get Conversations By Client ID    ${client_id_1}
    ${convs_after_2}=    Get Conversations By Client ID    ${client_id_2}
    ${convs_after_3}=    Get Conversations By Client ID    ${client_id_3}

    ${count_after_1}=    Get Length    ${convs_after_1}
    ${count_after_2}=    Get Length    ${convs_after_2}
    ${count_after_3}=    Get Length    ${convs_after_3}

    Should Be Equal As Integers    ${count_after_1}    ${count_before_1}
    Should Be Equal As Integers    ${count_after_2}    ${count_before_2}
    Should Be Equal As Integers    ${count_after_3}    ${count_before_3}

    Log    ✅ 3 separate capture sessions created without silent Conversations

    # Close all streams
    Close Audio Stream    ${stream_1}
    Close Audio Stream    ${stream_2}
    Close Audio Stream    ${stream_3}


Audio Chunks Persisted Despite Transcription Failure
    [Documentation]    Verify that when transcription fails (e.g., invalid Deepgram key),
    ...                audio chunks are still saved to MongoDB.
    ...
    ...                IMPORTANT: This test requires the mock-transcription-failure.yml config.
    ...                Run with: make test CONFIG=mock-transcription-failure.yml
    ...                The test will SKIP if transcription succeeds (real API keys).
    [Tags]    audio-streaming	infra	slow

    ${device_name}=    Set Variable    test-persist-fail
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Start stream with always_persist=true
    ${stream_id}=    Open Durable Audio Stream    device_name=${device_name}

    ${session_id}=    Wait Until Keyword Succeeds    10s    250ms
    ...    Get Active Session ID For Client    ${client_id}

    # Send audio chunks (transcription will fail due to invalid API key in config)
    # Use realtime pacing to ensure chunks arrive while persistence job is running
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=50    realtime_pacing=True

    # Close stream
    ${total_chunks}=    Close Audio Stream    ${stream_id}
    Log    Sent ${total_chunks} total chunks

    # Capture persists independently even when STT cannot materialize a Conversation.
    ${chunks}=    Wait Until Keyword Succeeds    60s    2s
    ...    Verify Capture Session Has Chunks    ${session_id}

    ${chunk_count}=    Get Length    ${chunks}
    Should Be True    ${chunk_count} > 0
    ...    Expected audio chunks to be saved despite transcription failure

    ${conversations}=    Get Conversations By Client ID    ${client_id}
    Should Be Empty    ${conversations}
    ...    Failed transcription must not create an empty semantic Conversation

    Log    ✅ Audio chunks persisted despite transcription failure (${chunk_count} chunks saved)


Speech Materializes And Completes Conversation
    [Documentation]    Verify successful speech materializes a Conversation and completes
    ...                it through the terminal processing job.
    [Tags]    conversation	audio-streaming

    ${device_name}=    Set Variable    test-complete
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Get baseline conversation count for THIS client_id only
    ${convs_before}=    Get Conversations By Client ID    ${client_id}
    ${count_before}=    Get Length    ${convs_before}
    ${expected_count}=    Evaluate    ${count_before} + 1

    # Start stream with always_persist=true
    ${stream_id}=    Open Durable Audio Stream    device_name=${device_name}

    # Send audio chunks with speech (transcription will succeed)
    # Use realtime pacing so Deepgram can finalize segments
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200    realtime_pacing=True

    ${convs_after}=    Wait Until Keyword Succeeds    60s    2s
    ...    Wait For Conversation By Client ID    ${client_id}    ${expected_count}
    ${conversation}=    Set Variable    ${convs_after}[0]
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    # Close stream
    Close Audio Stream    ${stream_id}

    # Wait for transcription and title generation to complete
    Wait Until Keyword Succeeds    90s    5s
    ...    Verify Conversation Processing Status    ${conversation_id}    completed

    # Verify title updated from placeholder to actual summary
    ${updated_conv}=    Get Conversation By ID    ${conversation_id}
    ${title}=    Set Variable    ${updated_conv}[title]

    # Title should NOT contain placeholder text
    ${title_lower}=    Convert To Lower Case    ${title}
    ${has_processing}=    Run Keyword And Return Status    Should Contain    ${title_lower}    processing
    ${has_failed}=    Run Keyword And Return Status    Should Contain    ${title_lower}    transcription failed

    ${is_placeholder}=    Evaluate    ${has_processing} or ${has_failed}
    Should Not Be True    ${is_placeholder}
    ...    Expected title to be updated, but still has placeholder: ${title}

    Log    ✅ Conversation updated to completed with title: ${title}
