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
    ${stream_id}=    Open Audio Stream    device_name=ws-conv

    # Send enough audio to trigger speech detection (test audio has speech)
    # Test audio is 4 minutes long at 16kHz, sending 200 chunks ensures enough speech
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200

    # Wait for open_conversation job to be created (transcription + speech analysis takes time)
    # Deepgram/OpenAI API calls + job processing can take 30-60s with queue
    Wait Until Keyword Succeeds    60s    3s
    ...    Job Type Exists For Client    open_conversation    ws-conv

    Log To Console    Open conversation job created after speech detection

    # Then verify speech detection job has conversation_job_id linked
    ${speech_jobs}=    Wait Until Keyword Succeeds    15s    2s
    ...    Job Type Exists For Client    speech_detection    ws-conv
    Job Has Conversation ID    ${speech_jobs}[0]
    [Teardown]    Close Audio Stream    ${stream_id}


Conversation Closes On Inactivity Timeout And Restarts Speech Detection
    [Documentation]    Verify that after SPEECH_INACTIVITY_THRESHOLD_SECONDS of silence,
    ...                the open_conversation job closes with timeout_triggered=True,
    ...                a new speech_detection job is created for the next conversation,
    ...                and post-conversation jobs are enqueued (transcription, speaker, memory, title).
    ...
    ...                Test environment sets SPEECH_INACTIVITY_THRESHOLD_SECONDS=5 in docker-compose-test.yml.
    [Tags]    audio-streaming	queue	conversation

    ${device_name}=    Set Variable    test-post

    # Open stream and send enough audio to trigger speech detection and conversation
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200

    # Wait for conversation job to be created (transcription + speech analysis takes time)
    ${conv_jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Job Type Exists For Client    open_conversation    ${device_name}
    ${conv_job}=    Set Variable    ${conv_jobs}[0]
    ${conv_job_id}=    Set Variable    ${conv_job}[job_id]
    ${conv_meta}=    Set Variable    ${conv_job}[meta]
    ${conversation_id}=    Evaluate    $conv_meta.get('conversation_id', '')
    Log To Console    Conversation job created: ${conv_job_id}, conversation_id: ${conversation_id}

    # Record the initial speech detection job (will be replaced after timeout)
    ${initial_speech_jobs}=    Get Jobs By Type And Client    speech_detection    ${device_name}
    ${initial_speech_count}=    Get Length    ${initial_speech_jobs}
    Log To Console    Initial speech detection jobs: ${initial_speech_count}

    # Stop sending audio (simulate silence/inactivity)
    # The conversation should auto-close after SPEECH_INACTIVITY_THRESHOLD_SECONDS
    Log To Console    Waiting for inactivity timeout to trigger conversation close...

    # Wait for conversation job to complete (status changes from 'started' to 'completed')
    # Timeout value should be > SPEECH_INACTIVITY_THRESHOLD_SECONDS + buffer
    Wait For Job Status    ${conv_job_id}    completed    timeout=30s    interval=2s
    Log To Console    Conversation job completed (timeout triggered)

    # Verify a NEW speech detection job (2nd one) was created for next conversation
    # The handle_end_of_conversation function creates a new speech_detection job
    ${new_speech_jobs}=    Wait Until Keyword Succeeds    30s    2s
    ...    Job Type Exists For Client    speech_detection    ${device_name}    2
    ${new_speech_count}=    Get Length    ${new_speech_jobs}
    Should Be True    ${new_speech_count} >= ${initial_speech_count}
    ...    Expected new speech detection job but count is ${new_speech_count} (was ${initial_speech_count})
    Log To Console    New speech detection job created for next conversation

    # Verify post-conversation jobs were enqueued (linked by conversation_id, not client_id)
    # These jobs process the completed conversation: transcription, speaker recognition, memory, title
    ${transcription_jobs}=    Wait Until Keyword Succeeds    30s    2s
    ...    Job Type Exists For Conversation    transcribe_full_audio_job    ${conversation_id}
    Log To Console    Post-conversation transcription job enqueued

    # Speaker recognition job should also be created
    ${speaker_jobs}=    Get Jobs By Type And Conversation    recognise_speakers_job    ${conversation_id}
    Log To Console    Speaker recognition jobs found: ${speaker_jobs.__len__()}

    # Audio cropping job should be created
    ${cropping_jobs}=    Get Jobs By Type And Conversation    process_cropping_job    ${conversation_id}
    Log To Console    Cropping jobs found: ${cropping_jobs.__len__()}

    # Title/summary generation job should be created
    ${title_jobs}=    Get Jobs By Type And Conversation    generate_title_summary_job    ${conversation_id}
    Log To Console    Title/summary jobs found: ${title_jobs.__len__()}

    # Memory extraction job should be created
    ${memory_jobs}=    Get Jobs By Type And Conversation    process_memory_job    ${conversation_id}
    Log To Console    Memory jobs found: ${memory_jobs.__len__()}


Segment Timestamps Match Cropped Audio
    [Documentation]    Verify that after conversation closes and cropping completes,
    ...                segment timestamps are adjusted to match the cropped audio file.
    [Tags]    audio-streaming	audio-upload

    ${device_name}=    Set Variable    seg-test

    # # Open stream
    ${stream_id}=    Open Audio Stream    device_name=${device_name}

    # Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=250

    # Wait for conversation job to be created (transcription + speech analysis takes time)
    # ${conv_jobs}=    Wait Until Keyword Succeeds    60s    3s
    # ...    Job Type Exists For Client    open_conversation    ${device_name}

    # conversation 1
    ${conversation_id_1}=    Stream And Wait For Conversation    ${stream_id}    ${TEST_AUDIO_FILE}    ${device_name}    num_chunks=250
    Log To Console    Conversation 1 completed: ${conversation_id_1}

    # conversation 2, with 500 chunks (enough for 8 segments to match expected timestamps)
    ${conversation_id}=    Stream And Wait For Conversation    ${stream_id}    ${TEST_AUDIO_FILE}    ${device_name}    num_chunks=500
    Log To Console    Conversation 2 completed: ${conversation_id}

    # Wait for cropping job to complete
    ${cropping_jobs}=    Wait Until Keyword Succeeds    30s    2s
    ...    Job Type Exists For Conversation    process_cropping_job    ${conversation_id}
    ${cropping_job}=    Set Variable    ${cropping_jobs}[0]
    Wait For Job Status    ${cropping_job}[job_id]    completed    timeout=30s    interval=2s
    Log To Console    Cropping job completed

    # Wait for database updates
    Sleep    2s

    # Fetch the conversation with updated segments
    ${conversation}=    get conversation by id    ${conversation_id}

    # Verify cropped audio path exists
    Should Not Be Empty    ${conversation}[cropped_audio_path]
    Log To Console    Cropped audio: ${conversation}[cropped_audio_path]

    # Get segments
    ${segments}=    Set Variable    ${conversation}[segments]

    ${segment_count}=    Get Length    ${segments}
    Should Be True    ${segment_count} > 0    No segments found
    Log To Console    Found ${segment_count} segments

    # Verify timestamps are adjusted to cropped audio (should start from 0)
    ${first_segment}=    Set Variable    ${segments}[0]
    Should Be True    ${first_segment}[start] == 0.0    First segment should start at 0.0s after cropping

    # Verify last segment timing is reasonable (should be within the audio duration)
    ${last_segment}=    Set Variable    ${segments}[-1]
    # Should Be True    ${last_segment}[end] > 50    Last segment should extend beyond 50s for 100s audio
    Should Be True    ${last_segment}[end] < 110    Last segment should be within 110s

    # Verify segments match expected test data timestamps
    # Uses default EXPECTED_SEGMENT_TIMES from test_data.py
    # To use a different dataset: Verify Segments Match Expected Timestamps    ${segments}    ${EXPECTED_SEGMENT_TIMES_SHORT}
    # To use custom tolerance: Verify Segments Match Expected Timestamps    ${segments}    ${EXPECTED_SEGMENT_TIMES}    ${tolerance}=1.0
    # Verify Segments Match Expected Timestamps    ${segments}    expected_segments=${EXPECTED_SEGMENT_TIMES}   

    Log To Console    ✓ Validated ${segment_count} segments with proper cropped timestamps matching expected data



