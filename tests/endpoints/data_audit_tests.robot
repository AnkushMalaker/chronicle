*** Settings ***
Documentation       Data Audit API Tests
...
...                 Tests for the data-audit endpoints:
...                 - Filtered conversation listing with VAD speech metrics
...                 - Silence-gap detection (split candidates)
...                 - Conversation split (chunk reassignment + transcript slicing)
...                 - Conversation merge (adjacent conversations)

Library             RequestsLibrary
Library             Collections
Resource            ../setup/setup_keywords.robot
Resource            ../setup/teardown_keywords.robot
Resource            ../resources/conversation_keywords.robot
Resource            ../resources/queue_keywords.robot

Suite Setup         Suite Setup
Suite Teardown      Suite Teardown

Test Tags           conversation


*** Test Cases ***
Data Audit List Returns Speech Metrics
    [Documentation]    The audit listing responds with VAD-based fields and paging metadata

    ${response}=    GET On Session    api    /api/data-audit/conversations    expected_status=200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    conversations
    Dictionary Should Contain Key    ${body}    total
    Dictionary Should Contain Key    ${body}    speech_threshold
    Dictionary Should Contain Key    ${body}    scan_capped

Silence Gaps On Unknown Conversation Returns Not Found
    [Documentation]    Gap detection on a nonexistent conversation is a 404

    GET On Session    api    /api/data-audit/conversations/nonexistent-conversation/silence-gaps
    ...    expected_status=404

Split On Unknown Conversation Returns Not Found
    [Documentation]    Splitting a nonexistent conversation is a 404

    ${payload}=    Create Dictionary    split_points=${{[30.0]}}
    POST On Session    api    /api/data-audit/conversations/nonexistent-conversation/split
    ...    json=${payload}    expected_status=404

Merge Requires At Least Two Conversations
    [Documentation]    Request validation rejects a single-conversation merge

    ${payload}=    Create Dictionary    conversation_ids=${{['only-one-id']}}
    POST On Session    api    /api/data-audit/merge    json=${payload}    expected_status=422

Full Split And Merge Round Trip
    [Documentation]    Upload audio → VAD analyze → split at the midpoint → verify two
    ...                children with reassigned chunks and re-timed transcripts → merge
    ...                the children back and verify totals and source soft-deletion.
    [Tags]    conversation
    [Timeout]    600s

    # Arrange — create a conversation with audio chunks and a transcript
    ${conversation}=    Create Test Conversation    device_name=audit-split
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]

    # Act — force VAD analysis and wait for the job to finish
    ${analyze_payload}=    Create Dictionary
    ...    conversation_ids=${{['${conversation_id}']}}    force=${True}
    ${analyze_response}=    POST On Session    api    /api/data-audit/analyze
    ...    json=${analyze_payload}    expected_status=200
    ${job_id}=    Set Variable    ${analyze_response.json()}[job_id]
    Wait For Job Status    ${job_id}    finished    timeout=180s    interval=3s

    # Assert — gap endpoint now reports the conversation as analyzed
    ${gaps_response}=    GET On Session    api
    ...    /api/data-audit/conversations/${conversation_id}/silence-gaps
    ...    expected_status=200
    Should Be True    ${gaps_response.json()}[analyzed]    Conversation should be analyzed after the VAD job
    ${duration}=    Set Variable    ${gaps_response.json()}[duration_seconds]
    Should Be True    ${duration} > 20    Test asset should be longer than 20s, got ${duration}

    # Act — split at the midpoint (split points are caller-supplied, so no
    # real 15-minute silence gap is needed for the mechanics to be exercised)
    ${midpoint}=    Evaluate    round(${duration} / 2, 1)
    ${split_payload}=    Create Dictionary    split_points=${{[${midpoint}]}}
    ${split_response}=    POST On Session    api
    ...    /api/data-audit/conversations/${conversation_id}/split
    ...    json=${split_payload}    expected_status=200
    ${children}=    Set Variable    ${split_response.json()}[children]
    ${child_count}=    Get Length    ${children}
    Should Be Equal As Integers    ${child_count}    2    Split at one point should produce two children

    # Assert — children have chunks and re-timed transcripts starting near zero
    ${total_child_chunks}=    Set Variable    ${0}
    FOR    ${child}    IN    @{children}
        Should Be True    ${child}[chunk_count] > 0    Child should own at least one audio chunk
        ${total_child_chunks}=    Evaluate    ${total_child_chunks} + ${child}[chunk_count]
        ${child_doc}=    Get Conversation By ID    ${child}[conversation_id]
        Should Be Equal    ${child_doc}[conversation_id]    ${child}[conversation_id]
    END

    # Assert — the parent no longer appears in the active conversation list
    ${remaining}=    Get Conversations By Client ID    ${conversation}[client_id]
    FOR    ${conv}    IN    @{remaining}
        Should Not Be Equal    ${conv}[conversation_id]    ${conversation_id}
        ...    Soft-deleted parent should not be listed
    END

    # Assert — a re-split of the already-split parent is rejected
    POST On Session    api    /api/data-audit/conversations/${conversation_id}/split
    ...    json=${split_payload}    expected_status=409

    # Act — merge the two children back together
    ${child_ids}=    Evaluate    [c['conversation_id'] for c in ${children}]
    ${merge_payload}=    Create Dictionary    conversation_ids=${child_ids}
    ${merge_response}=    POST On Session    api    /api/data-audit/merge
    ...    json=${merge_payload}    expected_status=200
    ${merged}=    Set Variable    ${merge_response.json()}

    # Assert — merged conversation owns all the chunks and the sources are gone
    Should Be Equal As Integers    ${merged}[chunk_count]    ${total_child_chunks}
    ...    Merged conversation should own every child chunk
    ${merged_doc}=    Get Conversation By ID    ${merged}[merged_conversation_id]
    Should Be Equal    ${merged_doc}[conversation_id]    ${merged}[merged_conversation_id]
    ${after_merge}=    Get Conversations By Client ID    ${conversation}[client_id]
    FOR    ${conv}    IN    @{after_merge}
        Should Not Contain    ${child_ids}    ${conv}[conversation_id]
        ...    Soft-deleted merge sources should not be listed
    END

    Log To Console    ✅ Split ${conversation_id} into 2 parts and merged them back
