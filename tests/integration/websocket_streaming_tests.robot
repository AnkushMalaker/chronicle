*** Settings ***
Documentation    Conversation Audio Streaming Integration Tests
...              Tests that verify WebSocket audio streaming creates the expected
...              background jobs.
Resource         ../resources/websocket_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/transcript_verification.robot
Resource         ../resources/queue_keywords.robot
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot


Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup        Test Cleanup

Test Tags         audio-streaming

*** Variables ***


*** Test Cases ***

Streaming jobs created on stream start
    [Documentation]    Verify both jobs are created and remain active during streaming
    [Tags]    audio-streaming	queue	e2e

    ${device_name}=    Set Variable    ws-test
    # Open stream
    ${stream_id}=    Open Audio Stream    device_name=ws-test

    # Send some audio chunks
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=5
    Sleep     2s
    # Check speech detection job
    ${jobs}=    Get Jobs By Type    speech_detection
    Should Not Be Empty    ${jobs}
    ${speech_job}=    Find Job For Client    ${jobs}    ${device_name}
    Should Not Be Equal    ${speech_job}    ${None}    Speech detection job not created

    # Check audio persistence job
    ${persist_job}=    Find Job For Client    ${jobs}    ${device_name}
    Should Not Be Equal    ${persist_job}    ${None}    Audio persistence job not created

    Log    Both jobs active during streaming
    Log    Speech detection: ${speech_job}[job_id]
    Log    Audio persistence: ${persist_job}[job_id]

    # Send more chunks while jobs are running
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=10

    # Jobs should still be present
    ${speech_jobs_after}=    Get Jobs By Type    speech_detection
    ${speech_after}=    Find Job For Client    ${speech_jobs_after}    ${device_name}
    Should Not Be Equal    ${speech_after}    ${None}    Speech detection job disappeared during streaming


Conversation Job Created After Speech Detection
    [Documentation]    Verify that after enough speech is detected (5+ words),
    ...                an open_conversation_job is created and linked to the
    ...                speech detection job via conversation_job_id in meta.
    [Tags]    audio-streaming	queue	conversation

    # Open stream
    ${device_name}=    Set Variable    ws-conv
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Send enough audio to trigger speech detection (test audio has speech)
    # Test audio is 4 minutes long at 16kHz, sending 200 chunks ensures enough speech
    # Use realtime pacing so Deepgram can finalize transcription segments as audio streams in
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200    realtime_pacing=True

    # Wait for open_conversation job to be created (transcription + speech analysis takes time)
    # Deepgram/OpenAI API calls + job started can take 30-60s with queue
    # Use 120s timeout: previous test's stream cleanup can trigger post-conversation jobs
    # that occupy workers, delaying this test's speech detection pipeline
    Wait Until Keyword Succeeds    120s    3s
    ...    Job Type Exists For Client    open_conversation    ${client_id}

    Log To Console    Open conversation job created after speech detection

    # Then verify speech detection job has conversation_job_id linked
    # Note: After conversation completes, a NEW speech_detection job is created for the next conversation
    # So we need to get the jobs and find the one with conversation_job_id set
    ${speech_jobs}=    Get Jobs By Type And Client    speech_detection    ${client_id}
    Should Not Be Empty    ${speech_jobs}    msg=No speech_detection jobs found for ${client_id}

    # Find the job with conversation_job_id (the original one that created the conversation)
    ${found_linked_job}=    Set Variable    ${False}
    FOR    ${job}    IN    @{speech_jobs}
        ${meta}=    Set Variable    ${job}[meta]
        ${conv_job_id}=    Evaluate    $meta.get('conversation_job_id')
        IF    '${conv_job_id}' != 'None'
            ${found_linked_job}=    Set Variable    ${True}
            Log To Console    Found speech_detection job with conversation_job_id: ${conv_job_id}
            BREAK
        END
    END
    Should Be True    ${found_linked_job}    msg=No speech_detection job has conversation_job_id set

    # Close stream after test completes
    ${total_chunks}=    Close Audio Stream    ${stream_id}
    Log    Closed stream, sent ${total_chunks} total chunks


Button Press Should Close Active Conversation
    [Documentation]    Verify that a button double press during an active conversation
    ...                closes it with end_reason=close_requested and triggers post-processing.
    ...                (Double press: the button_control plugin maps single_press to
    ...                stop_playback and double_press to close_conversation — see
    ...                plugins/button_control/config.yml.)
    [Tags]    audio-streaming	conversation
    [Timeout]    180s

    # Arrange: Open stream and get enough speech to start a conversation
    ${device_name}=    Set Variable    ws-button-close
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Get baseline conversation jobs
    ${baseline_jobs}=    Get Jobs By Type And Client    open_conversation    ${client_id}
    ${baseline_count}=    Get Length    ${baseline_jobs}

    # Send audio with speech (realtime pacing for Deepgram to finalize segments)
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200    realtime_pacing=True

    # Wait for open_conversation_job to start (speech detected -> conversation opened)
    ${jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Wait For New Job To Appear    open_conversation    ${client_id}    ${baseline_count}
    ${conversation_id}=    Evaluate    $jobs[0]['meta'].get('conversation_id', '')
    Should Not Be Empty    ${conversation_id}    msg=Conversation ID not found in job meta

    # Act: Send button double press to close the conversation
    # (single press is mapped to stop_playback since the button_control rework)
    Send Button Event To Stream    ${stream_id}    DOUBLE_PRESS

    # Assert: Conversation should close with end_reason=close_requested
    Wait Until Keyword Succeeds    90s    2s
    ...    Conversation Should Have End Reason    ${conversation_id}    close_requested

    # Cleanup
    [Teardown]    Run Keyword And Ignore Error    Close Audio Stream    ${stream_id}

Conversation Closes On Inactivity Timeout And Restarts Speech Detection
    [Documentation]    Verify that after SPEECH_INACTIVITY_THRESHOLD_SECONDS of silence (audio time),
    ...                the open_conversation job closes with timeout_triggered=True,
    ...                a new speech_detection job is created for the next conversation,
    ...                and post-conversation jobs are enqueued (speaker, memory, title).
    ...                Note: Streaming conversations use streaming transcript (no batch transcription).
    ...
    ...                Test environment sets SPEECH_INACTIVITY_THRESHOLD_SECONDS=20 in docker-compose-test.yml.
    [Tags]    audio-streaming	queue	conversation	slow

    ${device_name}=    Set Variable    test-post
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Open stream and send enough audio to trigger speech detection and conversation
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    # Use realtime pacing so Deepgram can finalize transcription segments as audio streams in
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200    realtime_pacing=True

    # Wait for conversation job to be created (transcription + speech analysis takes time)
    ${conv_jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Job Type Exists For Client    open_conversation    ${client_id}
    ${conv_job}=    Set Variable    ${conv_jobs}[0]
    ${conv_job_id}=    Set Variable    ${conv_job}[job_id]
    ${conv_meta}=    Set Variable    ${conv_job}[meta]
    ${conversation_id}=    Evaluate    $conv_meta.get('conversation_id', '')
    Log To Console    Conversation job created: ${conv_job_id}, conversation_id: ${conversation_id}

    # Record the initial speech detection job (will be replaced after timeout)
    ${initial_speech_jobs}=    Get Jobs By Type And Client    speech_detection    ${client_id}
    ${initial_speech_count}=    Get Length    ${initial_speech_jobs}
    Log To Console    Initial speech detection jobs: ${initial_speech_count}

    # Stop sending audio (simulate silence/inactivity)
    # The conversation should auto-close after SPEECH_INACTIVITY_THRESHOLD_SECONDS
    Log To Console    Waiting for inactivity timeout to trigger conversation close...

    # Wait for conversation job to complete (status changes from 'started' to 'finished')
    # Timeout needs: (audio send time ~60s) + (silence timeout 20s) + (buffer 10s) = 90s
    Wait For Job Status    ${conv_job_id}    finished    timeout=90s    interval=2s
    Log To Console    Conversation job finished (timeout triggered)

    # Verify a NEW speech detection job (2nd one) was created for next conversation
    # The handle_end_of_conversation function creates a new speech_detection job
    ${new_speech_jobs}=    Wait Until Keyword Succeeds    30s    2s
    ...    Job Type Exists For Client    speech_detection    ${client_id}    2
    ${new_speech_count}=    Get Length    ${new_speech_jobs}
    Should Be True    ${new_speech_count} >= ${initial_speech_count}
    ...    Expected new speech detection job but count is ${new_speech_count} (was ${initial_speech_count})
    Log To Console    New speech detection job created for next conversation

    # Verify post-conversation jobs were enqueued (linked by conversation_id, not client_id)
    # These jobs process the finished conversation: speaker recognition, memory, title
    # Note: Streaming conversations no longer have batch transcription - transcript comes from streaming
    Log To Console    Verifying post-conversation jobs (speaker, memory, title)...

    # Speaker recognition job should be created
    ${speaker_jobs}=    Get Jobs By Type And Conversation    recognise_speakers_job    ${conversation_id}
    Log To Console    Speaker recognition jobs found: ${speaker_jobs.__len__()}

    # The ordered summary bundle should create three independently timed jobs.
    ${title_jobs}=    Get Jobs By Type And Conversation    generate_title_job    ${conversation_id}
    ${short_summary_jobs}=    Get Jobs By Type And Conversation    generate_short_summary_job    ${conversation_id}
    ${detailed_summary_jobs}=    Get Jobs By Type And Conversation    generate_detailed_summary_job    ${conversation_id}
    Log To Console    Title jobs found: ${title_jobs.__len__()}
    Log To Console    Short-summary jobs found: ${short_summary_jobs.__len__()}
    Log To Console    Detailed-summary jobs found: ${detailed_summary_jobs.__len__()}

    # Memory extraction job should be created
    ${memory_jobs}=    Get Jobs By Type And Conversation    process_memory_job    ${conversation_id}
    Log To Console    Memory jobs found: ${memory_jobs.__len__()}


Live Transcript Available During Active Recording
    [Documentation]    Verify that conversation has live-v0 transcript version
    ...                while audio is still being streamed. The monitoring loop
    ...                should create and update the live version in MongoDB.
    ...
    ...                Strategy: Send audio fast (no realtime pacing) to quickly
    ...                trigger speech detection and conversation creation. Then keep
    ...                sending small batches to keep the conversation alive while
    ...                checking for live-v0 transcript in the database.
    [Tags]    audio-streaming	conversation
    [Timeout]    180s

    ${device_name}=    Set Variable    ws-live-tx
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Send initial batch fast to trigger speech detection quickly
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200

    # Wait for conversation to be created (speech detection → open_conversation_job)
    ${jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Job Type Exists For Client    open_conversation    ${client_id}
    ${conv_meta}=    Set Variable    ${jobs}[0][meta]
    ${conversation_id}=    Evaluate    $conv_meta.get('conversation_id', '')
    Should Not Be Empty    ${conversation_id}    msg=Conversation ID not found in job meta

    # Keep conversation alive by sending more audio while we check for live-v0
    # Send audio in small bursts with polling between
    FOR    ${i}    IN RANGE    10
        # Send more audio to keep conversation active (prevents inactivity timeout)
        Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=50

        # Check if live transcript has appeared
        ${status}=    Run Keyword And Return Status
        ...    Conversation Should Have Live Transcript    ${conversation_id}
        IF    ${status}
            ${conversation}=    Conversation Should Have Live Transcript    ${conversation_id}
            Log To Console    Live transcript found on iteration ${i}

            # Verify transcript has actual content
            Should Not Be Empty    ${conversation}[transcript]
            ...    Live transcript should have text content

            Log To Console    Live transcript verified: ${conversation}[transcript][:80]...
            BREAK
        END
        Sleep    2s
    END

    Should Be True    ${status}    Live transcript (live-v0) was never created during active recording

    [Teardown]    Run Keyword And Ignore Error    Close Audio Stream    ${stream_id}


API Close Request Stops Active Conversation
    [Documentation]    Verify that POST /api/conversations/{client_id}/close
    ...                stops an active streaming conversation with end_reason=close_requested
    [Tags]    audio-streaming	conversation
    [Timeout]    180s

    ${device_name}=    Set Variable    ws-api-close
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Get baseline conversation jobs
    ${baseline_jobs}=    Get Jobs By Type And Client    open_conversation    ${client_id}
    ${baseline_count}=    Get Length    ${baseline_jobs}

    # Send audio to create conversation
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}
    ...    num_chunks=200    realtime_pacing=True

    # Wait for conversation job
    ${jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Wait For New Job To Appear    open_conversation    ${client_id}    ${baseline_count}
    ${conversation_id}=    Evaluate    $jobs[0]['meta'].get('conversation_id', '')
    Should Not Be Empty    ${conversation_id}    msg=Conversation ID not found in job meta

    # Close via API (POST directly on the admin session)
    ${response}=    POST On Session    api    /api/conversations/${client_id}/close    expected_status=200

    # Verify end_reason
    Wait Until Keyword Succeeds    90s    2s
    ...    Conversation Should Have End Reason    ${conversation_id}    close_requested

    [Teardown]    Run Keyword And Ignore Error    Close Audio Stream    ${stream_id}
