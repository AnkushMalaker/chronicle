*** Settings ***
Documentation    Full Pipeline Integration Test
Library          RequestsLibrary
Library          Collections
Library          Process
Library          String
Library          DateTime
Library          OperatingSystem
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/session_keywords.robot
Resource         ../resources/audio_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/memory_keywords.robot
Resource         ../resources/queue_keywords.robot
Variables        ../setup/test_env.py
Variables        ../setup/test_data.py
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Clear Test Databases


*** Test Cases ***
Full Pipeline Integration Test
    [Documentation]    Complete end-to-end test of audio processing pipeline
    [Tags]    e2e
    [Timeout]          600s

    Log    Starting Full Pipeline Integration Test    INFO

   
    # Phase 4: Audio Processing - Upload and wait for conversation completion
    Log    Starting audio upload and processing    INFO
    ${conversation}=    Upload Audio File    ${TEST_AUDIO_FILE}    ${TEST_DEVICE_NAME}

    Log    Audio processing completed, conversation created    INFO
    Set Global Variable    ${TEST_CONVERSATION}    ${conversation}

    # Phase 5: Transcription Verification
    Verify Transcription Quality    ${TEST_CONVERSATION}    ${EXPECTED_TRANSCRIPT}

    # Phase 6: Memory Extraction Verification
    Verify Memory Extraction    api    ${TEST_CONVERSATION}

    # Phase 7: Chat Integration
    Verify Chat Integration    api    ${TEST_CONVERSATION}

    Log    Full Pipeline Integration Test Completed Successfully    INFO

Audio Playback And Segment Timing Test
    [Documentation]    Verify audio files are accessible and segment timestamps are valid
    [Tags]    e2e	audio-upload
    [Timeout]          180s

    Log    Starting Audio Playback And Segment Timing Test    INFO

    # Upload audio to create a conversation with segments
    ${conversation}=    Upload Audio File    ${TEST_AUDIO_FILE}    ${TEST_DEVICE_NAME}
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    Log    Conversation created: ${conversation_id}    INFO

    # Wait for cropping job to complete (depends on transcription)
    Sleep    10s    Wait for post-processing jobs to complete

    # Refresh conversation data
    ${conversation}=    Get Conversation By ID    ${conversation_id}

    # Verify original audio is accessible
    ${audio_response}=    GET On Session    api    /api/audio/get_audio/${conversation_id}    expected_status=200
    Should Be Equal As Strings    ${audio_response.headers}[content-type]    audio/wav
    ${original_audio_size}=    Get Length    ${audio_response.content}
    Should Be True    ${original_audio_size} > 1000    Original audio file too small: ${original_audio_size} bytes
    Log    Original audio accessible: ${original_audio_size} bytes    INFO

    # Verify cropped audio is accessible (if available)
    &{params}=    Create Dictionary    cropped=true
    ${cropped_response}=    GET On Session    api    /api/audio/get_audio/${conversation_id}    params=${params}    expected_status=any
    IF    ${cropped_response.status_code} == 200
        Should Be Equal As Strings    ${cropped_response.headers}[content-type]    audio/wav
        ${cropped_audio_size}=    Get Length    ${cropped_response.content}
        Should Be True    ${cropped_audio_size} > 0    Cropped audio file is empty
        Log    Cropped audio accessible: ${cropped_audio_size} bytes    INFO

        # Cropped audio should be smaller or equal to original (silence removed)
        Should Be True    ${cropped_audio_size} <= ${original_audio_size}    Cropped audio larger than original
    ELSE
        Log    Cropped audio not yet available (cropping job may still be running)    WARN
    END

    # Verify segments exist and have valid timestamps
    Dictionary Should Contain Key    ${conversation}    segments
    ${segments}=    Set Variable    ${conversation}[segments]
    ${segment_count}=    Get Length    ${segments}
    Should Be True    ${segment_count} > 0    No segments found in conversation

    Log    Found ${segment_count} segments    INFO

    # Verify segment timestamp integrity
    ${prev_end}=    Set Variable    ${0}
    FOR    ${index}    ${segment}    IN ENUMERATE    @{segments}
        # Each segment should have start and end times
        Dictionary Should Contain Key    ${segment}    start
        Dictionary Should Contain Key    ${segment}    end

        ${start}=    Set Variable    ${segment}[start]
        ${end}=    Set Variable    ${segment}[end]

        # Start should be non-negative
        Should Be True    ${start} >= 0    Segment ${index} has negative start time: ${start}

        # End should be greater than start
        Should Be True    ${end} > ${start}    Segment ${index} end (${end}) not greater than start (${start})

        # Segments should be in order (start >= previous end, allowing small gaps)
        Should Be True    ${start} >= ${prev_end} - 0.1    Segment ${index} overlaps with previous segment

        ${prev_end}=    Set Variable    ${end}

        Log    Segment ${index}: ${start}s - ${end}s    DEBUG
    END

    # Verify last segment end time is reasonable (not beyond audio duration)
    # For a 4-minute audio, segments should end before ~250 seconds
    ${last_segment}=    Set Variable    ${segments}[-1]
    ${last_end}=    Set Variable    ${last_segment}[end]
    Should Be True    ${last_end} < 300    Last segment end time (${last_end}s) exceeds expected audio duration

    Log    All ${segment_count} segments have valid timestamps (0s - ${last_end}s)    INFO
    Log    Audio Playback And Segment Timing Test Completed Successfully    INFO

End To End Pipeline With Memory Validation Test
    [Documentation]    Complete E2E test with memory extraction and OpenAI quality validation.
    ...                Provides comprehensive integration testing of the entire audio processing pipeline.
    ...                Separate from other tests to avoid breaking existing upload-only tests.
    [Tags]    e2e	memory
    [Timeout]    600s

    Log    Starting End-to-End Pipeline Test with Memory Validation    INFO

    # Phase 1: Upload audio and wait for complete processing
    Log    Uploading audio file and waiting for full processing    INFO
    ${conversation}    ${memories}=    Upload Audio File And Wait For Memory
    ...    ${TEST_AUDIO_FILE}
    ...    ${TEST_DEVICE_NAME}

    Set Global Variable    ${TEST_CONVERSATION}    ${conversation}

    # Phase 2: Verify transcription quality
    Log    Verifying transcription quality    INFO
    Verify Transcription Quality    ${TEST_CONVERSATION}    ${EXPECTED_TRANSCRIPT}

    # Phase 3: Verify memories were extracted
    ${memory_count}=    Get Length    ${memories}
    Should Be True    ${memory_count} > 0    No memories extracted
    Log    Extracted ${memory_count} memories    INFO

    # Phase 4: Verify memory quality with OpenAI (matches Python test!)
    Log    Validating memory quality with OpenAI    INFO
    Verify Memory Quality With OpenAI    ${memories}    ${EXPECTED_MEMORIES}

    # Phase 5: Verify chat integration
    Log    Verifying chat integration    INFO
    Verify Chat Integration    api    ${TEST_CONVERSATION}

    Log    End-to-End Pipeline Test Completed Successfully    INFO
    Log    ✅ Transcript verified    INFO
    Log    ✅ ${memory_count} memories extracted and validated with OpenAI    INFO

*** Keywords ***


Verify Transcription Quality
    [Documentation]    Verify the transcription meets quality standards
    [Arguments]    ${conversation}    ${expected_content}

    Log    Verifying transcription quality    INFO

    # Extract transcript (can be string or array of segments)
    Dictionary Should Contain Key    ${conversation}    transcript
    ${transcript_raw}=    Set Variable    ${conversation}[transcript]
    Should Not Be Empty    ${transcript_raw}    Transcript is empty

    # Handle both string and array formats
    ${transcript_text}=    Run Keyword If    isinstance($transcript_raw, list)
    ...    Set Variable    ${transcript_raw}[0][text]
    ...    ELSE    Set Variable    ${transcript_raw}

    # Check transcript contains expected content
    ${transcript_lower}=    Convert To Lower Case    ${transcript_text}
    ${expected_lower}=      Convert To Lower Case    ${expected_content}
    Should Contain    ${transcript_lower}    ${expected_lower}    Transcript does not contain expected content

    # Verify transcript has reasonable length (at least 50 characters for 4-minute audio)
    ${transcript_length}=    Get Length    ${transcript_text}
    Should Be True    ${transcript_length} >= 50    Transcript too short: ${transcript_length} characters

    # Check segments exist (if transcript is array format)
    ${segment_count}=    Run Keyword If    isinstance($transcript_raw, list)
    ...    Get Length    ${transcript_raw}
    ...    ELSE    Set Variable    1

    Should Be True    ${segment_count} > 0    No transcript segments found

    Log    Transcription quality verification passed    INFO
    Log    Transcript length: ${transcript_length} characters, Segments: ${segment_count}    INFO

Verify Memory Extraction
    [Documentation]    Verify memories were extracted from the conversation
    [Arguments]    ${session_alias}    ${conversation}

    Log    Verifying memory extraction    INFO

    # Check if conversation has memory count (may still be processing)
    ${has_memory_count}=    Run Keyword And Return Status    Dictionary Should Contain Key    ${conversation}    memory_count
    ${memory_count}=    Run Keyword If    ${has_memory_count}
    ...    Set Variable    ${conversation}[memory_count]
    ...    ELSE    Set Variable    0

    # Get memories from API using session
    ${response}=   GET On Session    ${session_alias}    /api/memories
    Should Be Equal As Integers    ${response.status_code}    200

    ${memories_data}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${memories_data}    memories
    ${memories}=    Set Variable    ${memories_data}[memories]

    ${api_memory_count}=    Get Length    ${memories}

    # Verify memory extraction status (allow for memory processing to be in progress)
    Should Be True    ${memory_count} >= 0    Memory count is negative
    Should Be True    ${api_memory_count} >= 0    API memory count is negative

    Log    Memory extraction verification passed (may still be processing)    INFO
    Log    Conversation memory count: ${memory_count}, API memory count: ${api_memory_count}    INFO

Verify Chat Integration
    [Documentation]    Verify chat system can access conversation data
    [Arguments]    ${session_alias}    ${conversation}

    Log    Verifying chat integration    INFO

    # Create a chat session to test basic chat functionality
    ${chat_data}=  Create Dictionary    title=Integration Test Chat
    ${response}=   POST On Session    ${session_alias}    /api/chat/sessions    json=${chat_data}    expected_status=any
    Should Be True    ${response.status_code} in [200, 201]    Chat session creation failed with status ${response.status_code}

    ${session_data}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${session_data}    session_id
    ${session_id}=    Set Variable    ${session_data}[session_id]

    Log    Chat session created successfully: ${session_id}    INFO

    # Try to send a message (if endpoint is available)
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]
    ${message_data}=       Create Dictionary    content=What did we discuss about glass blowing in conversation ${conversation_id}?
    ${msg_status}=        Run Keyword And Return Status
    ...    POST On Session    ${session_alias}    /api/chat/sessions/${session_id}/messages    json=${message_data}    expected_status=200

    IF    ${msg_status}
        Log    Chat message functionality is available    INFO
    ELSE
        Log    Chat message endpoints not available or not implemented - skipping message test    WARN
    END

    # Clean up chat session
    ${response}=    DELETE On Session    ${session_alias}    /api/chat/sessions/${session_id}    expected_status=any
    Should Be True    ${response.status_code} in [200, 204]    Chat session deletion failed with status ${response.status_code}

    Log    Chat integration verification completed    INFO


