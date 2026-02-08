*** Settings ***
Documentation    Conversation Queue Integration Tests
Library          RequestsLibrary
Library          Collections
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/session_keywords.robot
Resource         ../resources/audio_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/queue_keywords.robot
Variables        ../setup/test_env.py
Variables        ../setup/test_data.py
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Clear Test Databases

Test Tags        queue	requires-api-keys

*** Test Cases ***

Test Upload audio creates transcription job
    [Documentation]    Test that uploading audio creates a transcription job in the queue
    [Tags]    e2e	queue	audio-upload
    [Timeout]          120s

    Log    Starting Upload Job Queue Test    INFO

    # Verify queue is empty
    ${initial_job_count}=    Get queue length
   

    # Upload audio file to create conversation and trigger transcription job
    ${conversation}=    Upload Audio File   ${TEST_AUDIO_FILE}    ${TEST_DEVICE_NAME}
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    Log    Created conversation: ${conversation_id}    INFO

    # Verify a new job has been added to the queue
    Wait Until Keyword Succeeds    10s    2s    Get queue length
    ${job_count}=    Get queue length
    Should Be True    ${job_count} >= 1    Expected at least 1 job in queue, got ${job_count}

    # Get the list of jobs and find the transcription job for our conversation
    ${jobs}=    Get job queue

    # Find the transcription job for our conversation
    # Note: transcript jobs have job_type "reprocess_transcript" and conversation_id as args[0]
    ${transcription_job}=    Set Variable    None
    FOR    ${job}    IN    @{jobs}
        ${job_type}=    Set Variable    ${job}[job_type]
        # Check if this is a transcript job (job_type contains "transcript")
        ${is_transcript_job}=    Evaluate    "transcribe" in "${job_type}".lower()
        IF    ${is_transcript_job}
            # Get conversation_id from args[0] (first argument to transcript job)
            ${job_conv_id}=    Set Variable    ${job}[meta][conversation_id]
            # Check if conversation_id matches (compare first 8 chars for short IDs)
            ${conv_id_short}=    Evaluate    "${conversation_id}"[:8]
            ${job_conv_id_short}=    Evaluate    "${job_conv_id}"[:8]
            IF    '${conv_id_short}' == '${job_conv_id_short}'
                ${transcription_job}=    Set Variable    ${job}
                Exit For Loop
            END
        END
    END
    Should Not Be Equal    ${transcription_job}    None    Transcription job for conversation ${conversation_id} not found in queue

Test Reprocess Conversation Job Queue
    [Documentation]    Test that reprocess transcript jobs are created and processed correctly
    [Tags]    e2e	queue
    [Timeout]          180s

    Log    Starting Reprocess Job Queue Test    INFO

    # First, create a conversation by uploading audio
    ${conversation}=    Upload Audio File   ${TEST_AUDIO_FILE}    ${TEST_DEVICE_NAME}
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    Log    Created conversation: ${conversation_id}    INFO

    # Wait for initial upload started to complete (transcription job chain)
    Log    Waiting for initial conversation started to complete...    INFO
    Sleep    10s    # Give time for initial job chain (transcription -> speaker -> memory)

    # Get conversation to verify initial state
    ${initial_conversation}=    Get Conversation By ID    ${conversation_id}
    Dictionary Should Contain Key    ${initial_conversation}    transcript_version_count
    ${initial_version_count}=    Set Variable    ${initial_conversation}[transcript_version_count]
    Log    Initial transcript version count: ${initial_version_count}    INFO
    ${active_version}=    Set Variable    ${initial_conversation}[active_transcript_version]
    Log    Active transcript version: ${active_version}    INFO

    # Trigger transcript reprocessing
    Log    Triggering transcript reprocessing for conversation ${conversation_id}    INFO
    ${reprocess_data}=    Reprocess Transcript    ${conversation_id}
    ${job_id}=    Set Variable    ${reprocess_data}[job_id]
    ${version_id}=    Set Variable    ${reprocess_data}[version_id]


    # Wait for transcription job to complete (Deepgram API + started takes time in CI)
    Log    Waiting for transcription job ${job_id} to complete...    INFO
    Wait For Job Status    ${job_id}    finished    timeout=60s    interval=3s

    # Verify conversation was updated with new transcript version
    ${updated_conversation}=    Get Conversation By ID    ${conversation_id}
    ${transcript_versions}=    Get Conversation Versions    ${conversation_id}  
    Length Should Be    ${transcript_versions}    ${initial_version_count + 1}    Expected transcript versions to increase by 1

    # Verify transcript versions array exists and has correct count
    Should Not Be Equal    ${updated_conversation}[active_transcript_version]    ${active_version}

    Log    Reprocess Job Queue Test Completed Successfully    INFO

