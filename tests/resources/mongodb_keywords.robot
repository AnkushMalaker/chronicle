*** Settings ***
Documentation    MongoDB Audio Chunk Verification Keywords
...
...              Keywords for verifying MongoDB audio chunk storage.
...              Used to test the MongoDB migration from disk-based WAV files.
Library          Collections
Library          ../libs/mongodb_helper.py
Resource         session_keywords.robot
Resource         conversation_keywords.robot


*** Keywords ***

Get Audio Chunks For Conversation
    [Documentation]    Retrieve audio chunks from MongoDB for a conversation
    [Arguments]    ${conversation_id}

    ${chunks}=    Get Audio Chunks    ${conversation_id}
    RETURN    ${chunks}


Verify Audio Chunks Exist
    [Documentation]    Verify that audio chunks exist in MongoDB for a conversation
    [Arguments]    ${conversation_id}    ${min_chunks}=1

    ${chunks}=    Get Audio Chunks For Conversation    ${conversation_id}
    ${chunk_count}=    Get Length    ${chunks}

    Should Be True    ${chunk_count} >= ${min_chunks}
    ...    Expected at least ${min_chunks} chunks, found ${chunk_count}

    Log    ✅ Found ${chunk_count} audio chunks in MongoDB for conversation ${conversation_id}
    RETURN    ${chunks}


Verify Audio Chunk Metadata
    [Documentation]    Verify chunk has correct metadata structure
    [Arguments]    ${chunk}

    # Verify required fields exist
    Dictionary Should Contain Key    ${chunk}    conversation_id
    Dictionary Should Contain Key    ${chunk}    chunk_index
    Dictionary Should Contain Key    ${chunk}    original_size
    Dictionary Should Contain Key    ${chunk}    compressed_size
    Dictionary Should Contain Key    ${chunk}    start_time
    Dictionary Should Contain Key    ${chunk}    end_time
    Dictionary Should Contain Key    ${chunk}    duration
    Dictionary Should Contain Key    ${chunk}    sample_rate
    Dictionary Should Contain Key    ${chunk}    channels

    # Verify field values are valid
    Should Be True    ${chunk}[chunk_index] >= 0
    Should Be True    ${chunk}[original_size] > 0
    Should Be True    ${chunk}[compressed_size] > 0
    Should Be True    ${chunk}[duration] > 0
    Should Be Equal As Integers    ${chunk}[sample_rate]    16000
    Should Be Equal As Integers    ${chunk}[channels]    1

    Log    ✅ Chunk ${chunk}[chunk_index]: ${chunk}[duration]s duration


Verify Chunks Are Sequential
    [Documentation]    Verify chunks have sequential chunk_index values
    [Arguments]    ${chunks}

    ${chunk_count}=    Get Length    ${chunks}
    Should Be True    ${chunk_count} > 0    No chunks to verify

    # Sort by chunk_index
    ${sorted_chunks}=    Evaluate    sorted(${chunks}, key=lambda x: x['chunk_index'])

    # Verify sequential numbering starting from 0
    FOR    ${i}    IN RANGE    ${chunk_count}
        ${chunk}=    Set Variable    ${sorted_chunks}[${i}]
        Should Be Equal As Integers    ${chunk}[chunk_index]    ${i}
        ...    Chunk index mismatch: expected ${i}, got ${chunk}[chunk_index]
    END

    Log    ✅ ${chunk_count} chunks are sequential (0 to ${chunk_count - 1})


Calculate Total Audio Size
    [Documentation]    Calculate total original and compressed audio size from chunks
    [Arguments]    ${chunks}

    ${total_original}=    Set Variable    ${0}
    ${total_compressed}=    Set Variable    ${0}

    FOR    ${chunk}    IN    @{chunks}
        ${total_original}=    Evaluate    ${total_original} + ${chunk}[original_size]
        ${total_compressed}=    Evaluate    ${total_compressed} + ${chunk}[compressed_size]
    END

    ${overall_ratio}=    Evaluate    ${total_compressed} / ${total_original} if ${total_original} > 0 else 0
    ${savings_percent}=    Evaluate    (1 - ${overall_ratio}) * 100

    Log    📦 Total audio: ${total_original} bytes (PCM) → ${total_compressed} bytes (Opus)
    Log    📊 Compression: ${overall_ratio:.3f} ratio (${savings_percent:.1f}% savings)

    RETURN    ${total_original}    ${total_compressed}    ${overall_ratio}


Verify Conversation Has Chunk Metadata
    [Documentation]    Verify conversation has correct MongoDB chunk metadata fields
    [Arguments]    ${conversation}

    # Verify MongoDB chunk fields exist
    Dictionary Should Contain Key    ${conversation}    audio_chunks_count
    Dictionary Should Contain Key    ${conversation}    audio_total_duration

    # Verify values are valid
    Should Be True    ${conversation}[audio_chunks_count] > 0
    ...    Conversation should have audio_chunks_count > 0

    Should Be True    ${conversation}[audio_total_duration] > 0
    ...    Conversation should have audio_total_duration > 0

    Log    ✅ Conversation metadata: ${conversation}[audio_chunks_count] chunks, ${conversation}[audio_total_duration]s duration
