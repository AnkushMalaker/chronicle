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


Get Capture Session By ID
    [Documentation]    Retrieve the technical capture session that owns an ingest attempt
    [Arguments]    ${capture_session_id}

    ${capture}=    Get Capture Session    ${capture_session_id}
    RETURN    ${capture}


Get Capture Session Chunks
    [Documentation]    Retrieve immutable chunks owned by one technical capture session
    [Arguments]    ${capture_session_id}

    ${chunks}=    Get Audio Chunks For Capture Session    ${capture_session_id}
    RETURN    ${chunks}


Verify Capture Session Has Chunks
    [Documentation]    Wait-friendly assertion that a capture session owns persisted chunks
    [Arguments]    ${capture_session_id}    ${min_chunks}=1

    ${chunks}=    Get Audio Chunks For Capture Session    ${capture_session_id}
    ${count}=    Get Length    ${chunks}
    Should Be True    ${count} >= ${min_chunks}
    ...    Expected at least ${min_chunks} chunks for capture ${capture_session_id}, found ${count}
    RETURN    ${chunks}


Get Client Conversations Including Deleted
    [Documentation]    All conversations for a client (incl. soft-deleted) with
    ...                deletion_reason / audio fields / transcript char count. The API
    ...                hides soft-deleted conversations, so this reads from Mongo.
    [Arguments]    ${client_id}

    ${conversations}=    Find Client Conversations    ${client_id}
    RETURN    ${conversations}


Get Orphaned Transcript Count
    [Documentation]    Count conversations soft-deleted as audio_chunks_not_ready that
    ...                still carry a transcript (the data-loss the reconnect fix prevents).
    [Arguments]    ${client_id}

    ${count}=    Count Orphaned Transcripts    ${client_id}
    RETURN    ${count}


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
    Dictionary Should Contain Key    ${chunk}    user_id
    Dictionary Should Contain Key    ${chunk}    capture_source_id
    Dictionary Should Contain Key    ${chunk}    capture_session_id
    Dictionary Should Contain Key    ${chunk}    sequence
    Dictionary Should Contain Key    ${chunk}    original_size
    Dictionary Should Contain Key    ${chunk}    compressed_size
    Dictionary Should Contain Key    ${chunk}    captured_at
    Dictionary Should Contain Key    ${chunk}    duration
    Dictionary Should Contain Key    ${chunk}    sample_rate
    Dictionary Should Contain Key    ${chunk}    channels

    # Verify field values are valid
    Should Be True    ${chunk}[sequence] >= 0
    Should Be True    ${chunk}[original_size] > 0
    Should Be True    ${chunk}[compressed_size] > 0
    Should Be True    ${chunk}[duration] > 0
    Should Be Equal As Integers    ${chunk}[sample_rate]    16000
    Should Be Equal As Integers    ${chunk}[channels]    1

    Log    ✅ Capture chunk ${chunk}[sequence]: ${chunk}[duration]s duration


Verify Chunks Are Sequential
    [Documentation]    Verify chunks have sequential capture-owned sequence values
    [Arguments]    ${chunks}

    ${chunk_count}=    Get Length    ${chunks}
    Should Be True    ${chunk_count} > 0    No chunks to verify

    ${capture_session_ids}=    Evaluate    {c['capture_session_id'] for c in ${chunks}}
    ${capture_session_count}=    Get Length    ${capture_session_ids}
    Should Be Equal As Integers    ${capture_session_count}    1
    ...    This assertion expects one finite upload capture session

    # Sort by capture-session sequence
    ${sorted_chunks}=    Evaluate    sorted(${chunks}, key=lambda x: x['sequence'])

    # Verify sequential numbering starting from 0
    FOR    ${i}    IN RANGE    ${chunk_count}
        ${chunk}=    Set Variable    ${sorted_chunks}[${i}]
        Should Be Equal As Integers    ${chunk}[sequence]    ${i}
        ...    Capture sequence mismatch: expected ${i}, got ${chunk}[sequence]
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
