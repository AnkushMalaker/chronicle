*** Settings ***
Documentation    Always Persist Audio Feature Tests
...
...              Tests that verify the always_persist flag ensures audio is saved
...              to MongoDB even when transcription fails.
...
...              Critical scenarios:
...              - Placeholder conversation created immediately
...              - Audio chunks persisted despite transcription failure
...              - Processing status transitions correctly
...              - Normal behavior preserved when always_persist=false

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

Placeholder Conversation Created Immediately With Always Persist
    [Documentation]    Verify that when always_persist=true, a conversation is created
    ...                immediately (before speech detection) with placeholder title and
    ...                processing_status="pending_transcription".
    [Tags]    conversation	audio-streaming

    ${device_name}=    Set Variable    test-placeholder
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Get baseline conversation count for THIS client_id only
    ${convs_before}=    Get Conversations By Client ID    ${client_id}
    ${count_before}=    Get Length    ${convs_before}
    ${expected_count}=    Evaluate    ${count_before} + 1

    # Start stream with always_persist=true
    ${stream_id}=    Open Audio Stream With Always Persist    device_name=${device_name}

    # Poll for conversation to be created by audio persistence job (may take 10-15s to start)
    ${convs_after}=    Wait Until Keyword Succeeds    30s    2s
    ...    Wait For Conversation By Client ID    ${client_id}    ${expected_count}
    ${count_after}=    Get Length    ${convs_after}

    # Verify new conversation created for this client
    Should Be True    ${count_after} >= ${expected_count}
    ...    Expected at least ${expected_count} conversation(s) for client ${client_id}, found ${count_after}

    # Find the new conversation (most recent)
    ${new_conv}=    Set Variable    ${convs_after}[0]
    ${conversation_id}=    Set Variable    ${new_conv}[conversation_id]

    # Verify placeholder title
    Verify Placeholder Conversation Title    ${conversation_id}

    # Verify processing_status
    Verify Conversation Processing Status    ${conversation_id}    pending_transcription

    # Verify always_persist flag
    Verify Conversation Always Persist Flag    ${conversation_id}

    # Close stream
    Close Audio Stream    ${stream_id}

    Log    ✅ Placeholder conversation created immediately with always_persist=true


Normal Behavior Preserved When Always Persist Disabled
    [Documentation]    Verify that when always_persist=false, the system
    ...                behaves as before: no conversation created until speech detected.
    ...                This test temporarily disables the global always_persist setting.
    [Tags]    conversation	audio-streaming

    ${device_name}=    Set Variable    test-normal
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Temporarily disable always_persist for this test
    Set Always Persist Enabled    ${API_SESSION}    ${False}

    TRY
        # Get baseline conversation count for THIS client_id only
        ${convs_before}=    Get Conversations By Client ID    ${client_id}
        ${count_before}=    Get Length    ${convs_before}

        # Start stream with always_persist=false (disabled via API above)
        ${stream_id}=    Open Audio Stream    device_name=${device_name}

        # Conversation should NOT exist immediately for this client
        Sleep    3s
        ${convs_after}=    Get Conversations By Client ID    ${client_id}
        ${count_after}=    Get Length    ${convs_after}

        # Verify no new conversation created yet for this client
        Should Be Equal As Integers    ${count_after}    ${count_before}
        ...    Expected no conversation for client ${client_id}, but found ${count_after} - ${count_before} new conversations

        Log    ✅ No placeholder conversation created (always_persist=false)

        # Close stream
        Close Audio Stream    ${stream_id}
    FINALLY
        # Re-enable always_persist for other tests
        Set Always Persist Enabled    ${API_SESSION}    ${True}
    END


Redis Key Set Immediately With Always Persist
    [Documentation]    Verify that conversation:current:{session_id} Redis key is set
    ...                immediately when always_persist=true, allowing audio persistence
    ...                job to start saving chunks.
    [Tags]    audio-streaming	infra

    ${device_name}=    Set Variable    test-redis-key
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Get baseline conversation count for THIS client_id only
    ${convs_before}=    Get Conversations By Client ID    ${client_id}
    ${count_before}=    Get Length    ${convs_before}
    ${expected_count}=    Evaluate    ${count_before} + 1

    # Start stream with always_persist=true
    ${stream_id}=    Open Audio Stream With Always Persist    device_name=${device_name}

    # session_id == client_id for streaming mode (not stream_id!)
    ${session_id}=    Set Variable    ${client_id}

    # Poll for conversation to be created by audio persistence job
    ${convs_after}=    Wait Until Keyword Succeeds    30s    2s
    ...    Wait For Conversation By Client ID    ${client_id}    ${expected_count}
    ${count_after}=    Get Length    ${convs_after}

    # Verify new conversation created for this client
    Should Be True    ${count_after} >= ${expected_count}
    ...    Expected at least ${expected_count} conversation(s) for client ${client_id}, found ${count_after}

    # Get the new conversation (most recent)
    ${conversation}=    Set Variable    ${convs_after}[0]
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    # Verify Redis key exists and points to the conversation
    ${redis_conv_id}=    Verify Conversation Current Key    ${session_id}    ${conversation_id}

    Should Be Equal As Strings    ${redis_conv_id}    ${conversation_id}
    ...    Redis key should point to placeholder conversation

    Log    ✅ Redis key conversation:current:${session_id} correctly set to ${conversation_id}

    # Close stream
    Close Audio Stream    ${stream_id}


Multiple Sessions Create Separate Conversations
    [Documentation]    Verify that starting multiple audio sessions with always_persist=true
    ...                creates separate placeholder conversations for each session.
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
    ${expected_count_1}=    Evaluate    ${count_before_1} + 1
    ${expected_count_2}=    Evaluate    ${count_before_2} + 1
    ${expected_count_3}=    Evaluate    ${count_before_3} + 1

    # Start 3 separate sessions
    ${stream_1}=    Open Audio Stream With Always Persist    device_name=multi-1
    Sleep    1s
    ${stream_2}=    Open Audio Stream With Always Persist    device_name=multi-2
    Sleep    1s
    ${stream_3}=    Open Audio Stream With Always Persist    device_name=multi-3

    # Poll for each conversation to be created (audio persistence jobs may take 10-15s)
    ${convs_after_1}=    Wait Until Keyword Succeeds    30s    2s
    ...    Wait For Conversation By Client ID    ${client_id_1}    ${expected_count_1}
    ${convs_after_2}=    Wait Until Keyword Succeeds    30s    2s
    ...    Wait For Conversation By Client ID    ${client_id_2}    ${expected_count_2}
    ${convs_after_3}=    Wait Until Keyword Succeeds    30s    2s
    ...    Wait For Conversation By Client ID    ${client_id_3}    ${expected_count_3}

    ${count_after_1}=    Get Length    ${convs_after_1}
    ${count_after_2}=    Get Length    ${convs_after_2}
    ${count_after_3}=    Get Length    ${convs_after_3}

    # Verify each client has at least 1 new conversation
    Should Be True    ${count_after_1} >= ${expected_count_1}
    ...    Expected at least ${expected_count_1} conversation(s) for client ${client_id_1}, found ${count_after_1}
    Should Be True    ${count_after_2} >= ${expected_count_2}
    ...    Expected at least ${expected_count_2} conversation(s) for client ${client_id_2}, found ${count_after_2}
    Should Be True    ${count_after_3} >= ${expected_count_3}
    ...    Expected at least ${expected_count_3} conversation(s) for client ${client_id_3}, found ${count_after_3}

    # Verify each conversation has unique conversation_id
    ${conv_id_1}=    Set Variable    ${convs_after_1}[0][conversation_id]
    ${conv_id_2}=    Set Variable    ${convs_after_2}[0][conversation_id]
    ${conv_id_3}=    Set Variable    ${convs_after_3}[0][conversation_id]

    Should Not Be Equal    ${conv_id_1}    ${conv_id_2}
    ...    Duplicate conversation_id found: ${conv_id_1}
    Should Not Be Equal    ${conv_id_2}    ${conv_id_3}
    ...    Duplicate conversation_id found: ${conv_id_2}
    Should Not Be Equal    ${conv_id_1}    ${conv_id_3}
    ...    Duplicate conversation_id found: ${conv_id_1}

    Log    ✅ 3 separate conversations created with unique IDs

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
    ${stream_id}=    Open Audio Stream With Always Persist    device_name=${device_name}

    # Poll for conversation to be created by audio persistence job
    ${conversations}=    Wait Until Keyword Succeeds    30s    2s
    ...    Wait For Conversation By Client ID    ${client_id}    1

    # Send audio chunks (transcription will fail due to invalid API key in config)
    # Use realtime pacing to ensure chunks arrive while persistence job is running
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=50    realtime_pacing=True

    # Close stream
    ${total_chunks}=    Close Audio Stream    ${stream_id}
    Log    Sent ${total_chunks} total chunks

    # Get the conversation for this client
    ${conversation}=    Set Variable    ${conversations}[0]
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    # Wait for transcription to attempt and fail (poll instead of fixed sleep)
    Wait Until Keyword Succeeds    60s    5s
    ...    Verify Conversation Processing Status    ${conversation_id}    transcription_failed

    # Refresh conversation data after status change (title may have updated)
    ${updated_conv}=    Get Conversation By ID    ${conversation_id}

    # Verify title indicates failure
    ${title}=    Set Variable    ${updated_conv}[title]
    ${title_lower}=    Convert To Lower Case    ${title}
    Should Contain    ${title_lower}    transcription
    Should Contain    ${title_lower}    fail
    ...    Expected title to contain 'transcription' and 'fail', got: ${title}

    # CRITICAL: Verify audio chunks were saved despite transcription failure
    ${chunks}=    Verify Audio Chunks Exist    ${conversation_id}    min_chunks=1

    ${chunk_count}=    Get Length    ${chunks}
    Should Be True    ${chunk_count} > 0
    ...    Expected audio chunks to be saved despite transcription failure

    Log    ✅ Audio chunks persisted despite transcription failure (${chunk_count} chunks saved)


Conversation Updates To Completed When Transcription Succeeds
    [Documentation]    Verify that when transcription succeeds, the placeholder conversation
    ...                updates from processing_status="pending_transcription" to "completed",
    ...                and the title updates from placeholder to actual summary.
    [Tags]    conversation	audio-streaming	requires-api-keys

    ${device_name}=    Set Variable    test-complete
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Get baseline conversation count for THIS client_id only
    ${convs_before}=    Get Conversations By Client ID    ${client_id}
    ${count_before}=    Get Length    ${convs_before}
    ${expected_count}=    Evaluate    ${count_before} + 1

    # Start stream with always_persist=true
    ${stream_id}=    Open Audio Stream With Always Persist    device_name=${device_name}

    # Poll for placeholder conversation to be created by audio persistence job
    ${convs_after}=    Wait Until Keyword Succeeds    30s    2s
    ...    Wait For Conversation By Client ID    ${client_id}    ${expected_count}
    ${conversation}=    Set Variable    ${convs_after}[0]
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    # Verify initial placeholder state
    Verify Conversation Processing Status    ${conversation_id}    pending_transcription
    Verify Placeholder Conversation Title    ${conversation_id}

    # Send audio chunks with speech (transcription will succeed)
    # Use realtime pacing so Deepgram can finalize segments
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200    realtime_pacing=True

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
