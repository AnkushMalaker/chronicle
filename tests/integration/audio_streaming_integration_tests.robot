*** Settings ***
Documentation    Audio Streaming Integration Tests
...              Tests for streaming transcription provider (Phase 1) and Redis session state (Phase 2)
...
...              This test suite validates:
...              - Phase 1: Registry-driven transcription provider works
...              - Phase 2: Redis sessions as single source of truth (user_email, job IDs, chunk tracking)
...              - Phase 2: Session lifecycle management (init, update, cleanup)
Resource         ../resources/websocket_keywords.robot
Resource         ../resources/queue_keywords.robot
Resource         ../resources/redis_keywords.robot
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot

Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup

Test Tags        audio-streaming	requires-api-keys

*** Variables ***


*** Test Cases ***

Redis Session Schema Contains All Required Fields
    [Documentation]    Verify Redis session has all Phase 2 fields after stream initialization
    [Tags]    infra	audio-streaming

    ${device_name}=    Set Variable    redis-schema-test
    ${stream_id}=    Open Audio Stream    device_name=${device_name}

    # Send a few chunks to trigger session initialization
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=5

    # Allow time for async session initialization to complete
    Sleep    2s

    # Get session data from Redis using client_id (not stream_id)
    ${client_id}=    Get Client ID From Device Name    ${device_name}
    ${session_data}=    Get Redis Session Data    ${client_id}

    # Verify required fields exist
    Should Not Be Empty    ${session_data}[user_id]    Session missing user_id
    Should Not Be Empty    ${session_data}[user_email]    Session missing user_email
    Should Not Be Empty    ${session_data}[client_id]    Session missing client_id
    Should Not Be Empty    ${session_data}[connection_id]    Session missing connection_id
    Should Not Be Empty    ${session_data}[stream_name]    Session missing stream_name
    Should Not Be Empty    ${session_data}[provider]    Session missing provider
    Should Not Be Empty    ${session_data}[mode]    Session should have mode

    # Verify job IDs are tracked
    Dictionary Should Contain Key    ${session_data}    speech_detection_job_id
    Dictionary Should Contain Key    ${session_data}    audio_persistence_job_id

    # Verify connection state
    Should Be Equal    ${session_data}[websocket_connected]    true
    Should Be Equal    ${session_data}[status]    active

    Log    ✅ Redis session schema verified

    # Close stream after test completes
    ${total_chunks}=    Close Audio Stream    ${stream_id}
    Log    Closed stream, sent ${total_chunks} total chunks


Chunk Count Increments In Redis Session
    [Documentation]    Verify chunk count is tracked in Redis (not ClientState).
    ...                Note: The producer re-chunks client audio into 250ms fixed-size chunks
    ...                (8000 bytes at 16kHz/16-bit/mono). Client sends 100ms chunks (3200 bytes).
    ...                So N client chunks produce floor(N * 3200 / 8000) published chunks.
    [Tags]    infra	audio-streaming

    ${device_name}=    Set Variable    chunk-count-test
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Send first batch: 10 client chunks (10 * 3200 = 32000 bytes → 4 published chunks)
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=10
    Sleep    1s    # Allow chunk counter to update
    ${session1}=    Get Redis Session Data    ${client_id}
    ${count1}=    Convert To Integer    ${session1}[chunks_published]
    Should Be True    ${count1} > 0    First batch should produce at least 1 published chunk

    # Send second batch: 10 more client chunks
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=10
    Sleep    1s    # Allow chunk counter to update
    ${session2}=    Get Redis Session Data    ${client_id}
    ${count2}=    Convert To Integer    ${session2}[chunks_published]

    # Verify count increased between batches
    Should Be True    ${count2} > ${count1}    Chunk count should increase after sending more audio (${count1} → ${count2})

    Log    Chunk count tracked in Redis: ${count1} → ${count2}

    # Close stream after test completes
    ${total_chunks}=    Close Audio Stream    ${stream_id}
    Log    Closed stream, sent ${total_chunks} total chunks


Job IDs Stored In Redis Session
    [Documentation]    Verify job IDs are stored in Redis session (not ClientState)
    [Tags]    infra	audio-streaming	queue

    ${device_name}=    Set Variable    job-ids-test
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Send audio to trigger jobs
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=10
    Sleep    2s

    # Get session data
    ${session_data}=    Get Redis Session Data    ${client_id}

    # Verify job IDs are populated (not empty strings)
    Should Not Be Empty    ${session_data}[speech_detection_job_id]
    Should Not Be Empty    ${session_data}[audio_persistence_job_id]

    Log    ✅ Speech detection job: ${session_data}[speech_detection_job_id]
    Log    ✅ Audio persistence job: ${session_data}[audio_persistence_job_id]

    # Close stream after test completes
    ${total_chunks}=    Close Audio Stream    ${stream_id}
    Log    Closed stream, sent ${total_chunks} total chunks


Generic Transcription Provider Works
    [Documentation]    Verify streaming transcription works with registry-driven provider
    ...                This tests Phase 1 provider consolidation
    [Tags]    audio-streaming	queue	e2e

    ${device_name}=    Set Variable    provider-test
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Send sufficient audio for transcription
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=50

    # Wait for speech detection job to process
    Wait Until Keyword Succeeds    30s    2s
    ...    Job Type Exists For Client    stream_speech_detection_job    ${client_id}

    # Verify provider is set in Redis session
    ${session_data}=    Get Redis Session Data    ${client_id}
    Should Not Be Empty    ${session_data}[provider]
    Log    ✅ Transcription provider: ${session_data}[provider]

    # Close stream after test completes
    ${total_chunks}=    Close Audio Stream    ${stream_id}
    Log    Closed stream, sent ${total_chunks} total chunks


Session Cleaned Up After Stream Close
    [Documentation]    Verify session status is updated when stream closes
    [Tags]    infra	audio-streaming

    ${device_name}=    Set Variable    cleanup-test
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Send some audio
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=5
    Sleep    1s    # Allow session to be initialized

    # Verify session is active
    ${session_before}=    Get Redis Session Data    ${client_id}
    Should Be Equal    ${session_before}[status]    active
    Should Be Equal    ${session_before}[websocket_connected]    true

    # Close stream
    Close Audio Stream    ${stream_id}

    # Wait for finalization
    Sleep    2s

    # Verify session is finalized or finished (jobs may finish quickly for short streams)
    ${session_after}=    Get Redis Session Data    ${client_id}
    Should Be True    '${session_after}[status]' in ['finalizing', 'finished']
    ...    Session status should be finalizing or finished, got: ${session_after}[status]

    Log    ✅ Session status updated to ${session_after}[status]


User Email Tracked In Session
    [Documentation]    Verify user_email is stored in Redis session for debugging
    [Tags]    infra	audio-streaming

    ${device_name}=    Set Variable    email-test
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Send a chunk
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=1
    Sleep    1s    # Allow session to be initialized

    # Get session and verify email
    ${session_data}=    Get Redis Session Data    ${client_id}
    Should Not Be Empty    ${session_data}[user_email]
    Should Contain    ${session_data}[user_email]    @    Email should contain @

    Log    ✅ User email tracked: ${session_data}[user_email]

    # Close stream after test completes
    ${total_chunks}=    Close Audio Stream    ${stream_id}
    Log    Closed stream, sent ${total_chunks} total chunks
