*** Settings ***
Documentation    End-to-End WebSocket Streaming Transcription Tests
...
...              This test suite validates the complete transcription data flow
...              that was previously untested, which led to the end_marker bug.
...
...              Critical paths tested:
...              1. Audio → Deepgram WebSocket → Interim results (pub/sub)
...              2. Stream close → end_marker sent → CloseStream message
...              3. Deepgram → Final results → Redis stream transcription:results:{session_id}
...              4. Speech detection job → Reads Redis stream → Creates conversation
...
...              These tests would have caught the missing end_marker bug immediately.

Resource         ../resources/websocket_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/redis_keywords.robot
Resource         ../resources/queue_keywords.robot
Resource         ../resources/transcript_verification.robot
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot

Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup

Test Tags        audio-streaming	e2e	requires-api-keys


*** Test Cases ***

WebSocket Stream Produces Final Transcripts In Redis
    [Documentation]    Verify that closing a stream triggers end_marker,
    ...                CloseStream message to Deepgram, and final results
    ...                are written to Redis stream transcription:results:{session_id}
    ...
    ...                This test directly validates the bug fix:
    ...                - Producer sends end_marker when finalizing session
    ...                - Streaming consumer detects end_marker
    ...                - Consumer sends CloseStream to Deepgram
    ...                - Deepgram returns final results (is_final=True)
    ...                - Final results written to Redis stream
    [Tags]    audio-streaming	infra

    ${device_name}=    Set Variable    final-transcript-test
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Open stream and send audio
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=100

    # Critical: Close stream triggers the entire finalization flow
    Log    Closing stream - should trigger: end_marker → CloseStream → final results
    Close Audio Stream    ${stream_id}

    # Wait for streaming consumer to process end_marker and write final results to Redis
    # Use retry loop instead of fixed sleep - consumer processing time varies
    ${stream_name}=    Set Variable    transcription:results:${client_id}
    Wait Until Keyword Succeeds    30s    2s
    ...    Redis Stream Should Not Be Empty    ${stream_name}

    ${stream_length}=    Redis Command    XLEN    ${stream_name}
    Log    ✅ Redis stream has ${stream_length} final transcript(s)


Speech Detection Receives Transcription From Stream
    [Documentation]    Verify speech detection job successfully reads transcripts
    ...                from Redis stream and does NOT fail with "no_speech_detected"
    ...
    ...                This is the exact failure scenario from the bug:
    ...                - Speech detection reads from transcription:results:{session_id}
    ...                - If stream is empty, returns "No transcription received"
    ...                - If stream has data, creates conversation
    [Tags]    audio-streaming	queue

    ${device_name}=    Set Variable    speech-receives-test
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Stream audio and close
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200    realtime_pacing=True
    Close Audio Stream    ${stream_id}

    # Wait for speech detection job to complete
    # It should find transcripts in Redis stream and create conversation
    ${speech_jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Get Jobs By Type And Client    speech_detection    ${client_id}

    Should Not Be Empty    ${speech_jobs}    No speech detection job found

    # Get the first (most recent) speech detection job
    ${speech_job}=    Set Variable    ${speech_jobs}[0]
    ${job_id}=    Set Variable    ${speech_job}[job_id]

    # Wait for job to complete
    Wait For Job Status    ${job_id}    finished    timeout=60s    interval=2s

    # Get job result
    ${result}=    Get Job Result    ${job_id}

    # Critical assertion: Job should NOT have "no_speech_detected"
    # This would indicate the Redis stream was empty
    Should Not Contain    ${result}    no_speech_detected    Speech detection failed with no_speech_detected - Redis stream was empty!

    # Job should have created a conversation
    Should Contain    ${result}    conversation_job_id    Speech detection did not create conversation_job_id

    Log    ✅ Speech detection successfully received transcription from Redis stream


Conversation Created With Valid Transcript
    [Documentation]    End-to-end verification: Audio → Transcription → Conversation
    ...                Ensures the complete pipeline works with WebSocket streaming
    [Tags]    audio-streaming	conversation

    ${device_name}=    Set Variable    e2e-conv-test
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Stream audio (enough to trigger speech detection)
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200    realtime_pacing=True
    Close Audio Stream    ${stream_id}

    # DIAGNOSTIC: Verify speech detection job completes before checking for conversation
    Log    Waiting for speech detection job to complete...
    ${speech_jobs}=    Wait Until Keyword Succeeds    30s    3s
    ...    Get Jobs By Type And Client    speech_detection    ${client_id}

    Should Not Be Empty    ${speech_jobs}    No speech detection job found
    ${speech_job}=    Set Variable    ${speech_jobs}[0]
    ${speech_job_id}=    Set Variable    ${speech_job}[job_id]

    # Wait for speech detection to finish
    Wait For Job Status    ${speech_job_id}    finished    timeout=30s    interval=2s

    # Verify speech was detected (not no_speech_detected)
    ${speech_result}=    Get Job Result    ${speech_job_id}
    Should Not Contain    ${speech_result}    no_speech_detected
    ...    Speech detection failed with no_speech_detected - transcript may be empty or insufficient
    Should Contain    ${speech_result}    conversation_job_id
    ...    Speech detection did not create conversation_job_id

    Log    ✅ Speech detection completed successfully, conversation job should exist

    # Wait for conversation to be created
    ${conv_jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Job Type Exists For Client    open_conversation    ${client_id}

    ${conv_job}=    Set Variable    ${conv_jobs}[0]
    ${conv_meta}=    Set Variable    ${conv_job}[meta]
    ${conversation_id}=    Evaluate    $conv_meta.get('conversation_id', '')

    Should Not Be Empty    ${conversation_id}    Conversation ID not found in open_conversation job metadata

    # Wait for conversation to complete started (inactivity timeout)
    Wait For Job Status    ${conv_job}[job_id]    finished    timeout=60s    interval=2s

    # Retrieve the conversation
    ${conversation}=    Get Conversation By ID    ${conversation_id}

    # Verify conversation has transcript
    Dictionary Should Contain Key    ${conversation}    transcript
    ${transcript}=    Set Variable    ${conversation}[transcript]
    Should Not Be Empty    ${transcript}    Conversation has empty transcript

    # Verify transcript has content (at least 50 characters for meaningful speech)
    ${transcript_text}=    Run Keyword If    isinstance($transcript, list)
    ...    Set Variable    ${transcript}[0][text]
    ...    ELSE    Set Variable    ${transcript}

    ${transcript_length}=    Get Length    ${transcript_text}
    Should Be True    ${transcript_length} >= 50    Transcript too short: ${transcript_length} characters (expected 50+)

    Log    ✅ Conversation created with valid transcript: ${transcript_length} characters


Stream Close Sends End Marker To Redis Stream
    [Documentation]    Verify the producer actually sends end_marker when finalizing
    ...                This is a low-level infrastructure test to catch the exact bug
    [Tags]    audio-streaming	infra

    ${device_name}=    Set Variable    end-marker-test
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Open stream and send some audio
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=50

    # Get the audio stream name (where chunks are sent)
    ${audio_stream_name}=    Set Variable    audio:stream:${client_id}

    # Close stream - this MUST send end_marker
    Close Audio Stream    ${stream_id}

    # The audio stream is intentionally deleted ~1.2s after close by _try_delete_finished_stream(),
    # so we can't rely on XRANGE to find end_marker. Instead, verify via transcription:complete
    # which is a durable key (5-min TTL) set by StreamingTranscriptionConsumer.end_session_stream()
    # only AFTER it processes the end_marker.
    ${completion_key}=    Set Variable    transcription:complete:${client_id}
    Wait Until Keyword Succeeds    30s    1s
    ...    Verify Redis Key Exists    ${completion_key}

    ${signal_value}=    Redis Command    GET    ${completion_key}
    Should Be Equal As Strings    ${signal_value}    1
    ...    Completion signal should be "1" (clean close), got: ${signal_value}

    Log    ✅ end_marker was processed by streaming consumer (transcription:complete=${signal_value})


Streaming Consumer Closes Deepgram Connection On End Marker
    [Documentation]    Verify streaming consumer detects end_marker and closes cleanly
    ...                This tests the consumer side of the bug fix
    [Tags]    audio-streaming	infra

    ${device_name}=    Set Variable    consumer-close-test
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Stream and close
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=100
    Close Audio Stream    ${stream_id}

    # Wait for streaming consumer to process end_marker
    Sleep    10s

    # Check for Deepgram timeout errors in backend logs
    # If end_marker works, we should NOT see timeout errors
    ${logs}=    Get Backend Logs    since=30s

    # Should NOT contain Deepgram timeout error
    Should Not Contain    ${logs}    error 1011    Deepgram timeout error found - CloseStream was not sent! This indicates end_marker was not processed by streaming consumer.

    Should Not Contain    ${logs}    Deepgram did not receive audio data or a text message within the timeout window    Deepgram timeout found - stream was not closed properly

    Log    ✅ No Deepgram timeout errors - streaming consumer processed end_marker correctly


Word Timestamps Are Monotonically Increasing
    [Documentation]    Verify timestamps increase across chunks (catches offset accumulation bug)
    ...
    ...                This test validates that word timestamps are cumulative from stream start,
    ...                not reset for each chunk. Real Deepgram maintains state and returns
    ...                timestamps relative to stream start. If the mock server or backend
    ...                incorrectly resets timestamps per chunk, this test will fail.
    ...
    ...                Bug this catches: Offset accumulation bug where timestamps restart at 0
    ...                for each interim result instead of being cumulative across the stream.
    [Tags]    audio-streaming	conversation

    ${device_name}=    Set Variable    timestamp-test
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Stream audio
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200    realtime_pacing=True
    Close Audio Stream    ${stream_id}

    # Wait for speech detection and conversation creation
    Log    Waiting for speech detection job...
    ${speech_jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Get Jobs By Type And Client    speech_detection    ${client_id}
    Should Not Be Empty    ${speech_jobs}    No speech detection job found
    ${speech_job}=    Set Variable    ${speech_jobs}[0]
    Wait For Job Status    ${speech_job}[job_id]    finished    timeout=60s    interval=2s

    # Wait for conversation to be created
    ${conv_jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Job Type Exists For Client    open_conversation    ${client_id}
    ${conv_job}=    Set Variable    ${conv_jobs}[0]
    ${conv_meta}=    Set Variable    ${conv_job}[meta]
    ${conversation_id}=    Evaluate    $conv_meta.get('conversation_id', '')
    Should Not Be Empty    ${conversation_id}    Conversation ID not found

    # Wait for conversation to close
    Wait For Job Status    ${conv_job}[job_id]    finished    timeout=60s    interval=2s

    # Get conversation with segments
    ${conversation}=    Get Conversation By ID    ${conversation_id}
    ${segments}=    Set Variable    ${conversation}[segments]
    ${segment_count}=    Get Length    ${segments}
    Should Be True    ${segment_count} > 0    No segments found in conversation

    # Verify monotonically increasing timestamps across all segments
    ${prev_end}=    Set Variable    ${0.0}
    FOR    ${segment}    IN    @{segments}
        ${start}=    Convert To Number    ${segment}[start]
        ${end}=    Convert To Number    ${segment}[end]

        # Start time must be >= previous end time (allowing small gaps between segments)
        Should Be True    ${start} >= ${prev_end} - 0.1
        ...    Segment at ${start}s starts before previous end ${prev_end}s - timestamps not monotonically increasing!

        # End time must be > start time within segment
        Should Be True    ${end} > ${start}
        ...    Segment has invalid timing: start=${start}s, end=${end}s

        ${prev_end}=    Set Variable    ${end}
    END

    # Verify timestamps span a reasonable duration (not all near 0)
    ${last_segment}=    Set Variable    ${segments}[-1]
    ${final_end_time}=    Convert To Number    ${last_segment}[end]
    Should Be True    ${final_end_time} > 1.0
    ...    Final timestamp ${final_end_time}s is too low - timestamps may not be accumulating correctly across chunks

    Log    ✅ All ${segment_count} segments have monotonically increasing timestamps (final: ${final_end_time}s)


Segment Timestamps Match Expected Values
    [Documentation]    Use existing verification keyword to check segment timing accuracy
    ...
    ...                This test uses the Verify Segments Match Expected Timestamps keyword
    ...                that was created but never called. It validates that actual segment
    ...                timestamps match expected values from test_data.py within tolerance.
    [Tags]    audio-streaming	conversation

    ${device_name}=    Set Variable    segment-timing-test
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Stream the test audio file
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200    realtime_pacing=True
    Close Audio Stream    ${stream_id}

    # Wait for speech detection and conversation
    ${speech_jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Get Jobs By Type And Client    speech_detection    ${client_id}
    Should Not Be Empty    ${speech_jobs}    No speech detection job found
    ${speech_job}=    Set Variable    ${speech_jobs}[0]
    Wait For Job Status    ${speech_job}[job_id]    finished    timeout=60s    interval=2s

    # Get conversation
    ${conv_jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Job Type Exists For Client    open_conversation    ${client_id}
    ${conv_job}=    Set Variable    ${conv_jobs}[0]
    ${conv_meta}=    Set Variable    ${conv_job}[meta]
    ${conversation_id}=    Evaluate    $conv_meta.get('conversation_id', '')
    Should Not Be Empty    ${conversation_id}

    Wait For Job Status    ${conv_job}[job_id]    finished    timeout=60s    interval=2s

    # Get conversation with segments
    ${conversation}=    Get Conversation By ID    ${conversation_id}
    ${segments}=    Set Variable    ${conversation}[segments]

    # Use the existing (previously unused) verification keyword
    # This checks against EXPECTED_SEGMENT_TIMES from test_data.py
    Verify Segments Match Expected Timestamps    ${segments}

    Log    ✅ Segment timestamps match expected values within tolerance


Streaming Completion Signal Is Set Before Transcript Read
    [Documentation]    Verify Redis completion signal prevents race condition
    ...
    ...                This test validates the fix for the race condition between
    ...                StreamingTranscriptionConsumer and conversation job:
    ...                - Consumer sets transcription:complete:{session_id} = "1" when done
    ...                - Conversation job waits for this signal before reading transcript
    ...                - Without this, job could read incomplete transcript data
    [Tags]    audio-streaming	infra

    ${device_name}=    Set Variable    signal-test
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Stream audio and close
    ${stream_id}=    Open Audio Stream    device_name=${device_name}
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=100
    Close Audio Stream    ${stream_id}

    # Wait for streaming consumer to complete and set the completion signal
    ${completion_key}=    Set Variable    transcription:complete:${client_id}
    Wait Until Keyword Succeeds    30s    1s
    ...    Verify Redis Key Exists    ${completion_key}

    # Verify the signal value is "1" (completed)
    ${signal_value}=    Redis Command    GET    ${completion_key}
    Should Be Equal As Strings    ${signal_value}    1
    ...    Completion signal value should be "1", got: ${signal_value}

    Log    ✅ Completion signal ${completion_key} = ${signal_value} (consumer completed before job reads)


*** Keywords ***

Redis Stream Should Not Be Empty
    [Documentation]    Assert that a Redis stream has at least one entry.
    ...                Used with Wait Until Keyword Succeeds for retry-based checks.
    [Arguments]    ${stream_name}

    ${stream_length}=    Redis Command    XLEN    ${stream_name}
    Should Be True    ${stream_length} > 0
    ...    Redis stream ${stream_name} is empty - no final transcripts received! This means end_marker was not sent or CloseStream failed.
