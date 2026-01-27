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
    # Start mock transcription server
    Start Mock Transcription Server

    # Initialize API session for test user
    ${session}=    Get Admin API Session
    Set Suite Variable    ${API_SESSION}    ${session}

Suite Teardown Actions
    [Documentation]    Cleanup after all tests complete
    # Cleanup any remaining audio streams
    Cleanup All Audio Streams

    # Stop mock transcription server
    Stop Mock Transcription Server

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

    # Get baseline conversation count
    ${convs_before}=    Get User Conversations
    ${count_before}=    Get Length    ${convs_before}

    # Start stream with always_persist=true
    ${stream_id}=    Open Audio Stream With Always Persist    device_name=${device_name}

    # Conversation created by audio persistence job (takes 3-5s to start)
    Sleep    5s    # Wait for audio persistence job to create placeholder
    ${convs_after}=    Get User Conversations
    ${count_after}=    Get Length    ${convs_after}

    # Verify new conversation created
    Should Be True    ${count_after} == ${count_before} + 1
    ...    Expected 1 new conversation, found ${count_after} - ${count_before}

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
    [Documentation]    Verify that when always_persist=false (default), the system
    ...                behaves as before: no conversation created until speech detected.
    [Tags]    conversation	audio-streaming

    ${device_name}=    Set Variable    test-normal
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Get baseline conversation count
    ${convs_before}=    Get User Conversations
    ${count_before}=    Get Length    ${convs_before}

    # Start stream with always_persist=false (default behavior)
    ${stream_id}=    Open Audio Stream    device_name=${device_name}

    # Conversation should NOT exist immediately
    Sleep    3s
    ${convs_after}=    Get User Conversations
    ${count_after}=    Get Length    ${convs_after}

    # Verify no new conversation created yet
    Should Be Equal As Integers    ${count_after}    ${count_before}
    ...    Expected no conversation until speech detected, but found ${count_after} - ${count_before} new conversations

    Log    ✅ No placeholder conversation created (always_persist=false)

    # Close stream
    Close Audio Stream    ${stream_id}


Redis Key Set Immediately With Always Persist
    [Documentation]    Verify that conversation:current:{session_id} Redis key is set
    ...                immediately when always_persist=true, allowing audio persistence
    ...                job to start saving chunks.
    [Tags]    audio-streaming	infra

    ${device_name}=    Set Variable    test-redis-key
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Get baseline conversation count
    ${convs_before}=    Get User Conversations
    ${count_before}=    Get Length    ${convs_before}

    # Start stream with always_persist=true
    ${stream_id}=    Open Audio Stream With Always Persist    device_name=${device_name}

    # session_id == client_id for streaming mode (not stream_id!)
    ${session_id}=    Set Variable    ${client_id}

    # Get conversation (created by audio persistence job)
    Sleep    5s    # Wait for audio persistence job to create placeholder
    ${convs_after}=    Get User Conversations
    ${count_after}=    Get Length    ${convs_after}

    # Verify new conversation created
    Should Be True    ${count_after} == ${count_before} + 1
    ...    Expected 1 new conversation, found ${count_after} - ${count_before}

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

    ${device_name}=    Set Variable    test-multi

    # Get baseline conversation count
    ${convs_before}=    Get User Conversations
    ${count_before}=    Get Length    ${convs_before}

    # Start 3 separate sessions
    ${stream_1}=    Open Audio Stream With Always Persist    device_name=${device_name}-1
    Sleep    1s
    ${stream_2}=    Open Audio Stream With Always Persist    device_name=${device_name}-2
    Sleep    1s
    ${stream_3}=    Open Audio Stream With Always Persist    device_name=${device_name}-3
    Sleep    5s    # Wait for all audio persistence jobs to create placeholders

    # Verify 3 new conversations created
    ${convs_after}=    Get User Conversations
    ${count_after}=    Get Length    ${convs_after}

    ${new_count}=    Evaluate    ${count_after} - ${count_before}
    Should Be Equal As Integers    ${new_count}    3
    ...    Expected 3 new conversations, found ${new_count}

    # Verify each conversation has unique conversation_id
    ${conv_ids}=    Create List
    FOR    ${i}    IN RANGE    3
        ${conv}=    Set Variable    ${convs_after}[${i}]
        ${conv_id}=    Set Variable    ${conv}[conversation_id]
        List Should Not Contain Value    ${conv_ids}    ${conv_id}
        ...    Duplicate conversation_id found: ${conv_id}
        Append To List    ${conv_ids}    ${conv_id}
    END

    Log    ✅ 3 separate conversations created with unique IDs

    # Close all streams
    Close Audio Stream    ${stream_1}
    Close Audio Stream    ${stream_2}
    Close Audio Stream    ${stream_3}


Audio Chunks Persisted Despite Transcription Failure
    [Documentation]    Verify that when transcription fails (e.g., invalid Deepgram key),
    ...                audio chunks are still saved to MongoDB.
    ...
    ...                NOTE: This test requires misconfigured transcription service to trigger failure.
    ...                Test uses mock-transcription-failure.yml config with invalid API key.
    [Tags]    audio-streaming	mongodb	requires-api-keys

    ${device_name}=    Set Variable    test-persist-fail
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Start stream with always_persist=true
    ${stream_id}=    Open Audio Stream With Always Persist    device_name=${device_name}

    # Wait for audio persistence job to start consuming from Redis Stream
    Sleep    2s

    # Send audio chunks (transcription will fail due to invalid API key in config)
    # Use realtime pacing to ensure chunks arrive while persistence job is running
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=50    realtime_pacing=True

    # Close stream
    ${total_chunks}=    Close Audio Stream    ${stream_id}
    Log    Sent ${total_chunks} total chunks

    # Wait for processing to attempt and fail
    Sleep    15s

    # Get the conversation (most recent)
    ${conversations}=    Get User Conversations
    ${conversation}=    Set Variable    ${conversations}[0]
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    # Verify processing_status is transcription_failed
    Verify Conversation Processing Status    ${conversation_id}    transcription_failed

    # Verify title indicates failure
    ${title}=    Set Variable    ${conversation}[title]
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

    # Get baseline conversation count
    ${convs_before}=    Get User Conversations
    ${count_before}=    Get Length    ${convs_before}

    # Start stream with always_persist=true
    ${stream_id}=    Open Audio Stream With Always Persist    device_name=${device_name}

    # Verify placeholder conversation exists (created by audio persistence job)
    Sleep    5s
    ${convs_after}=    Get User Conversations
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
