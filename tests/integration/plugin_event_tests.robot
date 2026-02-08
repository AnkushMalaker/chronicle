*** Settings ***
Documentation    Plugin Event System Integration Tests
...
...              Tests the event-driven plugin architecture by:
...              - Uploading audio and verifying transcript.batch events
...              - Streaming audio and verifying transcript.streaming events
...              - Verifying conversation.complete events after conversation ends
...              - Verifying memory.processed events after memory extraction
Library          RequestsLibrary
Library          Collections
Library          String
Library          OperatingSystem
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/user_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/audio_keywords.robot
Resource         ../resources/plugin_keywords.robot
Resource         ../resources/websocket_keywords.robot
Variables        ../setup/test_data.py
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown

*** Variables ***
# TEST_AUDIO_FILE is loaded from test_data.py

*** Test Cases ***

Verify Test Plugin Configuration
    [Documentation]    Verify test plugin config file is properly formatted
    [Tags]    infra

    # Verify test config file exists
    File Should Exist    ${CURDIR}/../config/plugins.test.yml
    ...    msg=Test plugin config file should exist

    # Verify test_event plugin is configured
    ${config_content}=    Get File    ${CURDIR}/../config/plugins.test.yml
    Should Contain    ${config_content}    test_event
    ...    msg=Test config should contain test_event plugin

    Should Contain    ${config_content}    transcript.streaming
    ...    msg=Test plugin should subscribe to transcript.streaming

    Should Contain    ${config_content}    transcript.batch
    ...    msg=Test plugin should subscribe to transcript.batch

Upload Audio And Verify Transcript Batch Event
    [Documentation]    Upload audio file and verify transcript.batch event is dispatched
    [Tags]    audio-upload

    # Clear any existing events
    Clear Plugin Events

    # Upload test audio file
    File Should Exist    ${TEST_AUDIO_FILE}
    ...    msg=Test audio file should exist
    ${conversation}=    Upload Audio File    ${TEST_AUDIO_FILE}
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    # Get baseline count for THIS specific conversation (should be 0 before waiting)
    ${baseline_count}=    Set Variable    ${0}

    # Wait for transcription to complete (polls every 2s, max 30s)
    # Filter by conversation_id to avoid picking up fixture conversation events
    ${new_events}=    Wait For Plugin Event    transcript.batch    ${baseline_count}    timeout=30s    conversation_id=${conversation_id}

    # Verify at least one new event was received
    Should Be True    ${new_events} > 0
    ...    msg=At least one transcript.batch event should be logged for conversation ${conversation_id}

    # Get the events and verify structure
    ${events}=    Get Plugin Events By Type    transcript.batch
    Should Not Be Empty    ${events}
    ...    msg=Should have transcript.batch events

    # Verify first event has required fields
    ${event}=    Set Variable    ${events}[0]
    Log    Event data: ${event}

    # Verify event contains required fields (API returns dictionaries)
    Dictionary Should Contain Key    ${event}    data
    ...    msg=Event should have data field
    Dictionary Should Contain Key    ${event}    user_id
    ...    msg=Event should have user_id field

Conversation Complete Should Trigger Event
    [Documentation]    Verify conversation.complete event after conversation ends
    [Tags]    conversation	requires-api-keys

    # Clear events
    Clear Plugin Events

    # Upload audio (triggers conversation creation and completion)
    File Should Exist    ${TEST_AUDIO_FILE}
    ${conversation}=    Upload Audio File    ${TEST_AUDIO_FILE}
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    # Get baseline count for THIS specific conversation (should be 0 before waiting)
    ${baseline_count}=    Set Variable    ${0}

    # Wait for full pipeline: transcription → conversation (polls every 2s, max 40s)
    # Filter by conversation_id to avoid picking up fixture conversation events
    ${new_events}=    Wait For Plugin Event    conversation.complete    ${baseline_count}    timeout=40s    conversation_id=${conversation_id}

    Should Be True    ${new_events} > 0
    ...    msg=At least one conversation.complete event should be logged for conversation ${conversation_id}

    # Verify event structure
    ${events}=    Get Plugin Events By Type    conversation.complete
    Should Not Be Empty    ${events}

    # Verify end_reason metadata in plugin event
    Verify Event Metadata    conversation.complete    end_reason    file_upload    ${conversation_id}

Memory Processing Should Trigger Event
    [Documentation]    Verify memory.processed event after memory extraction
    [Tags]    memory

    # Clear events
    Clear Plugin Events

    # Upload audio with meaningful content for memory extraction
    File Should Exist    ${TEST_AUDIO_FILE}
    ${conversation}=    Upload Audio File    ${TEST_AUDIO_FILE}
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    # Get baseline count for THIS specific conversation (should be 0 before waiting)
    ${baseline_count}=    Set Variable    ${0}

    # Wait for full pipeline: transcription → conversation → memory (polls every 2s, max 60s)
    # Filter by conversation_id to avoid picking up fixture conversation events
    ${new_events}=    Wait For Plugin Event    memory.processed    ${baseline_count}    timeout=60s    conversation_id=${conversation_id}

    Should Be True    ${new_events} > 0
    ...    msg=At least one memory.processed event should be logged for conversation ${conversation_id}

    # Verify event structure
    ${events}=    Get Plugin Events By Type    memory.processed
    Should Not Be Empty    ${events}

WebSocket Disconnect Should Trigger Conversation Complete Event
    [Documentation]    Verify conversation.complete event when WebSocket disconnects
    [Tags]    audio-streaming	conversation	requires-api-keys
    [Timeout]    60s

    # Clear events
    Clear Plugin Events

    # Open WebSocket stream
    ${stream_id}=    Open Audio Stream    device_name=plugin-test-ws
    ${client_id}=    Get Client ID From Device Name    plugin-test-ws

    # Send audio chunks to create conversation (with realtime pacing for Deepgram to finalize segments)
    ${chunks_sent}=    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200    realtime_pacing=True

    # Wait for conversation job to be created (max 30s, poll every 2s)
    ${jobs}=    Wait Until Keyword Succeeds    30s    2s
    ...    Wait For New Job To Appear    open_conversation    ${client_id}    0
    Should Not Be Empty    ${jobs}    At least one conversation job should exist
    ${conv_meta}=    Set Variable    ${jobs}[0][meta]
    ${conversation_id}=    Evaluate    $conv_meta.get('conversation_id', '')
    Should Not Be Equal    ${conversation_id}    ${EMPTY}    Conversation ID should be set

    # Disconnect WebSocket abruptly without audio-stop (triggers websocket_disconnect end_reason)
    ${total_chunks}=    Close Audio Stream Without Stop Event    ${stream_id}
    Log    Closed WebSocket stream abruptly, sent ${total_chunks} total chunks

    # Get baseline count for THIS specific conversation (should be 0 before waiting)
    ${baseline_count}=    Set Variable    ${0}

    # Wait for plugin event dispatch (polls every 2s, max 30s)
    # Event dispatch depends on memory and title/summary jobs completing (~20-25s total)
    # Filter by conversation_id to avoid picking up events from other conversations
    ${new_events}=    Wait For Plugin Event    conversation.complete    ${baseline_count}    timeout=30s    conversation_id=${conversation_id}

    Should Be True    ${new_events} > 0
    ...    msg=At least one conversation.complete event should be logged for conversation ${conversation_id}

    # Verify plugin event has correct end_reason metadata
    Verify Event Metadata    conversation.complete    end_reason    websocket_disconnect    ${conversation_id}

    # Verify conversation has end_reason set in database
    # Wait for end_reason to be persisted (open_conversation_job saves it at the end)
    Wait Until Keyword Succeeds    10s    1s
    ...    Conversation Should Have End Reason    ${conversation_id}    websocket_disconnect

    # Verify completed_at timestamp is set
    ${updated_conversation}=    Get Conversation By ID    ${conversation_id}
    Should Not Be Equal    ${updated_conversation}[completed_at]    ${None}
    ...    msg=Conversation should have completed_at timestamp

Verify All Events Are Logged
    [Documentation]    Comprehensive test that verifies all event types are logged
    [Tags]    e2e

    # Clear all events
    Clear Plugin Events

    # Upload audio file (should trigger all events)
    File Should Exist    ${TEST_AUDIO_FILE}
    ${conversation}=    Upload Audio File    ${TEST_AUDIO_FILE}
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    # Get baseline counts for THIS specific conversation (should be 0 for each)
    ${batch_baseline}=    Set Variable    ${0}
    ${conv_baseline}=    Set Variable    ${0}
    ${mem_baseline}=    Set Variable    ${0}

    # Wait for events in pipeline order (polls every 2s for each)
    # Filter by conversation_id to avoid picking up fixture conversation events
    ${batch_new}=    Wait For Plugin Event    transcript.batch    ${batch_baseline}    timeout=30s    conversation_id=${conversation_id}
    ${conv_new}=    Wait For Plugin Event    conversation.complete    ${conv_baseline}    timeout=30s    conversation_id=${conversation_id}
    ${mem_new}=    Wait For Plugin Event    memory.processed    ${mem_baseline}    timeout=60s    conversation_id=${conversation_id}

    Should Be True    ${batch_new} > 0
    ...    msg=transcript.batch events should be logged for conversation ${conversation_id}

    Should Be True    ${conv_new} > 0
    ...    msg=conversation.complete events should be logged for conversation ${conversation_id}

    Should Be True    ${mem_new} > 0
    ...    msg=memory.processed events should be logged for conversation ${conversation_id}

    # Log summary
    Log    Events logged for conversation ${conversation_id} - Batch: ${batch_new}, Conversation: ${conv_new}, Memory: ${mem_new}

*** Keywords ***
Test Suite Setup
    [Documentation]    Setup for plugin event tests
    # Standard suite setup
    Suite Setup

    # Verify test audio file exists
    File Should Exist    ${TEST_AUDIO_FILE}
    ...    msg=Test audio file must exist for integration tests

Test Cleanup
    [Documentation]    Cleanup after each test
    # Standard cleanup
    # Note: We intentionally don't clear plugin events between tests
    # to allow for debugging and event inspection
