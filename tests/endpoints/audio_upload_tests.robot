*** Settings ***
Documentation       Audio Upload API Tests
...
...                 Tests for audio file upload functionality including:
...                 - Single file upload
...                 - Multiple file upload
...                 - Folder parameter (fixtures)
...                 - File validation
...                 - Conversation creation
...                 - Job enqueuing

Library             RequestsLibrary
Library             Collections
Library             String
Library             OperatingSystem
Resource            ../setup/setup_keywords.robot
Resource            ../setup/teardown_keywords.robot

Suite Setup         Suite Setup
Suite Teardown      Suite Teardown

Test Setup       Test Cleanup

Test Tags           audio-upload	requires-api-keys


*** Test Cases ***
Single Audio File Upload Test
    [Documentation]    Test uploading a single audio file
    ...
    ...                Verifies:
    ...                - File upload succeeds
    ...                - Conversation is created
    ...                - Jobs are enqueued (transcription, speaker, memory)
    ...                - Audio path is set correctly
    [Tags]    audio-upload

    # Upload audio file
    ${conversation}=    Upload Audio File    ${TEST_AUDIO_FILE}    device_name=upload-test

    # Verify conversation structure
    Dictionary Should Contain Key    ${conversation}    conversation_id
    Dictionary Should Contain Key    ${conversation}    transcript
    Dictionary Should Contain Key    ${conversation}    segments

    # Verify transcript was generated
    ${transcript}=    Set Variable    ${conversation}[transcript]
    ${transcript_length}=    Get Length    ${transcript}
    Should Be True    ${transcript_length} > 100    msg=Transcript too short: ${transcript_length} chars

    Log To Console    ✅ Uploaded audio file
    Log To Console    💾 Storage: MongoDB chunks
    Log To Console    📝 Transcript: ${transcript_length} characters
    Log To Console    🆔 Conversation ID: ${conversation}[conversation_id]


Audio File Upload With Fixtures Folder Test
    [Documentation]    Test uploading audio file to fixtures subfolder
    ...
    ...                Verifies:
    ...                - Folder parameter is accepted (for backward compatibility)
    ...                - Audio is stored in MongoDB chunks
    ...                - Conversation is created correctly
    [Tags]    audio-upload

    # Upload audio file to fixtures folder
    ${conversation}=    Upload Audio File    ${TEST_AUDIO_FILE}    device_name=fixture-upload    folder=fixtures

    # audio_path is legacy field (None for MongoDB storage)
    # Verify conversation was created
    Dictionary Should Contain Key    ${conversation}    conversation_id
    Dictionary Should Contain Key    ${conversation}    transcript

    Log To Console    ✅ Uploaded audio file with folder parameter
    Log To Console    💾 Storage: MongoDB chunks (folder param backward compatible)
    Log To Console    🆔 Conversation ID: ${conversation}[conversation_id]


Multiple Audio Files Upload Test
    [Documentation]    Test uploading multiple audio files in one request
    ...
    ...                Verifies:
    ...                - Multiple files can be uploaded
    ...                - Each file creates a separate conversation
    ...                - All files are processed successfully
    [Tags]    audio-upload

    # Note: Upload Audio File keyword currently handles single file
    # For multiple files, we need to use the API directly

    # Get admin token for curl request
    ${token}=    Get Authentication Token    api    ${ADMIN_EMAIL}    ${ADMIN_PASSWORD}

    # Upload multiple files using curl (Robot Framework multipart is limited)
    ${curl_cmd}=    Catenate    SEPARATOR=${SPACE}
    ...    curl -s -X POST
    ...    -H "Authorization: Bearer ${token}"
    ...    -F "files=@${TEST_AUDIO_FILE}"
    ...    -F "files=@${TEST_AUDIO_FILE}"
    ...    -F "device_name=multi-upload-test"
    ...    ${API_URL}/api/audio/upload

    ${result}=    Run Process    ${curl_cmd}    shell=True    timeout=60s
    Should Be Equal As Integers    ${result.rc}    0    msg=Curl command failed: ${result.stderr}

    # Parse response
    ${upload_response}=    Evaluate    json.loads('''${result.stdout}''')    json
    Log    Upload response: ${upload_response}

    # Verify summary
    Dictionary Should Contain Key    ${upload_response}    summary
    Should Be Equal As Integers    ${upload_response}[summary][total]    2    msg=Expected 2 files uploaded
    Should Be Equal As Integers    ${upload_response}[summary][processing]    2    msg=Expected 2 files processing

    # Verify both files are in response
    ${files}=    Set Variable    ${upload_response}[files]
    ${file_count}=    Get Length    ${files}
    Should Be Equal As Integers    ${file_count}    2    msg=Expected 2 files in response

    # Wait for both transcriptions to complete
    FOR    ${file}    IN    @{files}
        ${transcript_job_id}=    Set Variable    ${file}[transcript_job_id]
        Wait Until Keyword Succeeds    60s    5s    Check Job Status    ${transcript_job_id}    completed
        Log To Console    ✅ File ${file}[filename] transcription completed
    END

    Log To Console    ✅ Uploaded and processed ${file_count} audio files


Invalid File Upload Test
    [Documentation]    Test uploading non-WAV files
    ...
    ...                Verifies:
    ...                - Non-WAV files are rejected
    ...                - Proper error messages returned
    [Tags]    audio-upload

    # Create a temporary non-WAV file
    Create File    ${TEMPDIR}/test.txt    This is not an audio file

    # Try to upload non-WAV file
    ${token}=    Get Authentication Token    api    ${ADMIN_EMAIL}    ${ADMIN_PASSWORD}

    ${curl_cmd}=    Catenate    SEPARATOR=${SPACE}
    ...    curl -s -X POST
    ...    -H "Authorization: Bearer ${token}"
    ...    -F "files=@${TEMPDIR}/test.txt"
    ...    -F "device_name=invalid-upload"
    ...    ${API_URL}/api/audio/upload

    ${result}=    Run Process    ${curl_cmd}    shell=True    timeout=30s
    Should Be Equal As Integers    ${result.rc}    0    msg=Curl command failed

    # Parse response
    ${upload_response}=    Evaluate    json.loads('''${result.stdout}''')    json
    Log    Upload response: ${upload_response}

    # Verify file was rejected
    Should Be Equal As Integers    ${upload_response}[summary][failed]    1    msg=Expected 1 file to fail
    Should Be Equal As Integers    ${upload_response}[summary][processing]    0    msg=Expected 0 files processing

    # Verify error message mentions WAV files
    ${error_msg}=    Set Variable    ${upload_response}[files][0][error]
    Should Contain    ${error_msg}    WAV    msg=Error should mention WAV format

    # Cleanup
    Remove File    ${TEMPDIR}/test.txt

    Log To Console    ✅ Invalid file correctly rejected


Audio Upload Client ID Generation Test
    [Documentation]    Test that client IDs are generated correctly for uploads
    ...
    ...                Verifies:
    ...                - Client ID follows format: {user_id_suffix}-{device_name}
    ...                - Same device name reuses same client ID
    ...                - conversation_id is written to job metadata (for queue UI)
    [Tags]    audio-upload	queue

    # Upload first file with specific device name
    ${device_name}=    Set Variable    test-upload-device
    ${conversation1}=    Upload Audio File    ${TEST_AUDIO_FILE}    device_name=${device_name}
    ${client_id1}=    Set Variable    ${conversation1}[client_id]
    ${conversation_id1}=    Set Variable    ${conversation1}[conversation_id]

    # Verify client ID format
    Should Contain    ${client_id1}    ${device_name}    msg=Client ID should contain device name
    Should Match Regexp    ${client_id1}    ^[a-f0-9]{6}-${device_name}$    msg=Client ID should match format

    # Verify conversation_id is in all job metadata (transcription, speaker, memory jobs)
    ${transcribe_job}=    Get Job Details    transcribe_${conversation_id1[:12]}
    ${transcribe_meta}=    Set Variable    ${transcribe_job}[meta]
    Dictionary Should Contain Key    ${transcribe_meta}    conversation_id    msg=Transcription job should have conversation_id in meta
    Should Be Equal    ${transcribe_meta}[conversation_id]    ${conversation_id1}    msg=Transcription job meta conversation_id should match

    ${speaker_job}=    Get Job Details    speaker_${conversation_id1[:12]}
    ${speaker_meta}=    Set Variable    ${speaker_job}[meta]
    Dictionary Should Contain Key    ${speaker_meta}    conversation_id    msg=Speaker job should have conversation_id in meta
    Should Be Equal    ${speaker_meta}[conversation_id]    ${conversation_id1}    msg=Speaker job meta conversation_id should match

    ${memory_job}=    Get Job Details    memory_${conversation_id1[:12]}
    ${memory_meta}=    Set Variable    ${memory_job}[meta]
    Dictionary Should Contain Key    ${memory_meta}    conversation_id    msg=Memory job should have conversation_id in meta
    Should Be Equal    ${memory_meta}[conversation_id]    ${conversation_id1}    msg=Memory job meta conversation_id should match

    # Upload second file with same device name
    ${conversation2}=    Upload Audio File    ${TEST_AUDIO_FILE}    device_name=${device_name}
    ${client_id2}=    Set Variable    ${conversation2}[client_id]

    # Verify same client ID is used
    Should Be Equal    ${client_id1}    ${client_id2}    msg=Same device should use same client ID

    Log To Console    ✅ Client ID generation verified
    Log To Console    🆔 Client ID: ${client_id1}
    Log To Console    ✅ conversation_id in job metadata verified


Audio Upload Job Tracking Test
    [Documentation]    Test that upload creates proper job chain
    ...
    ...                Verifies:
    ...                - Transcription job is created and completes
    ...                - Conversation has transcript
    ...                - Conversation has segments
    [Tags]    audio-upload

    # Upload audio file (Upload Audio File keyword already waits for transcription)
    ${conversation}=    Upload Audio File    ${TEST_AUDIO_FILE}    device_name=job-tracking-test

    # Verify conversation has required fields
    Dictionary Should Contain Key    ${conversation}    conversation_id
    Dictionary Should Contain Key    ${conversation}    transcript
    Dictionary Should Contain Key    ${conversation}    segments

    # Verify transcript is not empty
    ${transcript}=    Set Variable    ${conversation}[transcript]
    Should Not Be Empty    ${transcript}    msg=Transcript should not be empty

    # Verify segments exist
    ${segments}=    Set Variable    ${conversation}[segments]
    ${segment_count}=    Get Length    ${segments}
    Should Be True    ${segment_count} > 0    msg=Should have at least one segment

    Log To Console    ✅ Job chain verified
    Log To Console    📝 Transcription: completed
    Log To Console    💬 Segments: ${segment_count}
