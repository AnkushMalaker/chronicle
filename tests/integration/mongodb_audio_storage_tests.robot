*** Settings ***
Documentation    MongoDB Audio Chunk Storage Integration Tests
...
...              Validates that audio is stored as MongoDB chunks
...              instead of disk-based WAV files.
Resource         ../resources/websocket_keywords.robot
Resource         ../resources/audio_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/mongodb_keywords.robot
Resource         ../resources/queue_keywords.robot
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Variables        ../setup/test_data.py


Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup


*** Test Cases ***

MongoDB Chunks Created From File Upload
    [Documentation]    Verify that uploaded audio files are stored as MongoDB chunks
    [Tags]    audio-upload

    # Upload 1-minute test audio file
    ${response}=    POST On Session    api    /api/audio/upload
    ...             files=${{ {'files': open('${TEST_AUDIO_FILE}', 'rb')} }}
    ...             params=device_name=upload-mongodb-test
    ...             expected_status=200

    ${upload_data}=    Set Variable    ${response.json()}
    ${conversation_id}=    Set Variable    ${upload_data}[files][0][conversation_id]
    Log    Uploaded conversation: ${conversation_id}

    # Wait for chunks to be written to MongoDB
    Sleep    5s

    # Verify chunks exist in MongoDB (expect ~6 chunks for 1-minute audio)
    ${chunks}=    Verify Audio Chunks Exist    ${conversation_id}    min_chunks=5

    ${chunk_count}=    Get Length    ${chunks}
    Log    ✅ Found ${chunk_count} MongoDB chunks for uploaded file


MongoDB Chunks Are Sequential
    [Documentation]    Verify chunks have sequential chunk_index values
    [Tags]    audio-upload

    ${response}=    POST On Session    api    /api/audio/upload
    ...             files=${{ {'files': open('${TEST_AUDIO_FILE}', 'rb')} }}
    ...             params=device_name=sequential-test
    ...             expected_status=200

    ${conversation_id}=    Set Variable    ${response.json()}[files][0][conversation_id]
    Sleep    5s

    ${chunks}=    Get Audio Chunks For Conversation    ${conversation_id}

    # Verify sequential numbering
    Verify Chunks Are Sequential    ${chunks}

    ${chunk_count}=    Get Length    ${chunks}
    ${last_index}=    Evaluate    ${chunk_count} - 1
    Log    ✅ Chunks are sequential (0 to ${last_index})


Conversation Has MongoDB Chunk Metadata
    [Documentation]    Verify conversation has chunk count and duration metadata
    [Tags]    audio-upload

    ${response}=    POST On Session    api    /api/audio/upload
    ...             files=${{ {'files': open('${TEST_AUDIO_FILE}', 'rb')} }}
    ...             params=device_name=metadata-test
    ...             expected_status=200

    ${conversation_id}=    Set Variable    ${response.json()}[files][0][conversation_id]
    Sleep    5s

    # Get conversation and verify it has chunk metadata
    ${conversation}=    Get Conversation By ID    ${conversation_id}
    Verify Conversation Has Chunk Metadata    ${conversation}

    Log    ✅ Conversation has chunk metadata: ${conversation}[audio_chunks_count] chunks, ${conversation}[audio_total_duration]s


Each Chunk Has Valid Metadata
    [Documentation]    Verify chunk documents have all required fields
    [Tags]    audio-upload

    ${response}=    POST On Session    api    /api/audio/upload
    ...             files=${{ {'files': open('${TEST_AUDIO_FILE}', 'rb')} }}
    ...             params=device_name=chunk-metadata-test
    ...             expected_status=200

    ${conversation_id}=    Set Variable    ${response.json()}[files][0][conversation_id]
    Sleep    5s

    ${chunks}=    Get Audio Chunks For Conversation    ${conversation_id}

    # Verify first chunk has all required fields
    ${first_chunk}=    Set Variable    ${chunks}[0]
    Verify Audio Chunk Metadata    ${first_chunk}

    Log    ✅ Chunk metadata is valid
