*** Settings ***
Documentation    Memory Management Keywords
...
...              This file contains keywords for memory operations including retrieval,
...              search, and deletion. All keywords use session-based authentication.
...
...              Examples of keywords that belong here:
...              - Memory retrieval and listing
...              - Memory search operations
...              - Memory deletion
...              - Memory verification and validation
...
...              Keywords that should NOT be in this file:
...              - Verification/assertion keywords (belong in tests)
...              - Session management (belong in session_keywords.robot)
Library          RequestsLibrary
Library          Collections
Variables        ../setup/test_env.py

*** Keywords ***

Get User Memories
    [Documentation]    Get memories for authenticated user using session
    [Arguments]    ${session}    ${limit}=50    ${user_id}=${None}

    &{params}=     Create Dictionary    limit=${limit}

    IF    '${user_id}' != '${None}'
        Set To Dictionary    ${params}    user_id=${user_id}
    END

    ${response}=    GET On Session    ${session}    /api/memories    params=${params}
    RETURN    ${response}

Get Memories With Transcripts
    [Documentation]    Get memories with their source transcripts using session
    [Arguments]    ${session}    ${limit}=50

    &{params}=     Create Dictionary    limit=${limit}

    ${response}=    GET On Session    ${session}    /api/memories/with-transcripts    params=${params}
    RETURN    ${response}

Search Memories
    [Documentation]    Search memories by query using session
    [Arguments]    ${session}    ${query}    ${limit}=20    ${score_threshold}=0.0

    &{params}=     Create Dictionary    query=${query}    limit=${limit}    score_threshold=${score_threshold}

    ${response}=    GET On Session    ${session}    /api/memories/search    params=${params}
    RETURN    ${response}

Delete Memory
    [Documentation]    Delete a specific memory using session
    [Arguments]    ${session}    ${memory_id}

    ${response}=    DELETE On Session    ${session}    /api/memories/${memory_id}
    RETURN    ${response}

Get Unfiltered Memories
    [Documentation]    Get all memories including fallback transcript memories using session
    [Arguments]    ${session}    ${limit}=50

    &{params}=     Create Dictionary    limit=${limit}

    ${response}=    GET On Session    ${session}    /api/memories/unfiltered    params=${params}
    RETURN    ${response}

Get All Memories Admin
    [Documentation]    Get all memories across all users (admin only) using session
    [Arguments]    ${session}    ${limit}=200

    &{params}=     Create Dictionary    limit=${limit}

    ${response}=    GET On Session    ${session}    /api/memories/admin    params=${params}
    RETURN    ${response}

Count User Memories
    [Documentation]    Count memories for a user using session
    [Arguments]    ${session}

    ${response}=    Get User Memories    ${session}    1000
    Should Be Equal As Integers    ${response.status_code}    200
    ${memories_data}=    Set Variable    ${response.json()}
    ${memories}=    Set Variable    ${memories_data}[memories]
    ${count}=       Get Length    ${memories}
    RETURN    ${count}

Verify Memory Extraction
    [Documentation]    Verify memories were extracted successfully
    [Arguments]    ${conversation}    ${memories_data}    ${min_memories}=0

    # Check conversation memory count
    Dictionary Should Contain Key    ${conversation}    memory_count
    ${conv_memory_count}=    Set Variable    ${conversation}[memory_count]

    # Check API memories
    Dictionary Should Contain Key    ${memories_data}    memories
    ${memories}=    Set Variable    ${memories_data}[memories]
    ${api_memory_count}=    Get Length    ${memories}

    # Verify reasonable memory extraction
    Should Be True    ${conv_memory_count} >= ${min_memories}    Insufficient memories: ${conv_memory_count}
    Should Be True    ${api_memory_count} >= ${min_memories}    Insufficient API memories: ${api_memory_count}

    Log    Memory extraction verified: conversation=${conv_memory_count}, api=${api_memory_count}    INFO


Wait For Memory Extraction
    [Documentation]    Wait for memory job to complete and fetch extracted memories.
    ...                Returns a result dictionary with success status, job details, and memories.
    ...                Does not perform assertions - calling tests should verify the results.
    ...
    ...                Return value structure:
    ...                {
    ...                  'success': True/False,
    ...                  'error_message': 'Error description' (only if success=False),
    ...                  'status': 'completed'/'failed'/'timeout'/'not_found',
    ...                  'job': {job object} (if available),
    ...                  'memories': [list of memories] (if successful),
    ...                  'memory_count': int (if successful)
    ...                }
    [Arguments]    ${memory_job_id}    ${timeout}=120

    Log    Waiting for memory job ${memory_job_id} to complete...

    # 1. Check if job exists before waiting
    ${job_status}=    Get Job Status    ${memory_job_id}
    IF    ${job_status} == ${None}
        ${result}=    Create Dictionary
        ...    success=${False}
        ...    error_message=Memory job ${memory_job_id} not found in queue
        ...    status=not_found
        RETURN    ${result}
    END

    # 2. Check if job already failed
    ${current_status}=    Set Variable    ${job_status}[status]
    IF    '${current_status}' == 'failed'
        ${error_info}=    Evaluate    $job_status.get('exc_info', 'Unknown error')
        ${result}=    Create Dictionary
        ...    success=${False}
        ...    error_message=Memory job already failed: ${error_info}
        ...    status=failed
        ...    job=${job_status}
        RETURN    ${result}
    END

    # 3. Wait for job completion with status monitoring
    ${start_time}=    Get Time    epoch
    ${end_time}=    Evaluate    ${start_time} + ${timeout}
    ${final_job}=    Set Variable    ${job_status}
    ${final_status}=    Set Variable    ${current_status}

    WHILE    True
        # Get current job status
        ${job}=    Get Job Status    ${memory_job_id}

        # Handle job not found (e.g., expired from queue)
        IF    ${job} == ${None}
            ${result}=    Create Dictionary
            ...    success=${False}
            ...    error_message=Memory job ${memory_job_id} disappeared from queue during wait
            ...    status=not_found
            ...    job=${final_job}
            RETURN    ${result}
        END

        ${status}=    Set Variable    ${job}[status]
        ${final_job}=    Set Variable    ${job}
        ${final_status}=    Set Variable    ${status}

        # Success case - job completed
        IF    '${status}' == 'completed' or '${status}' == 'finished'
            Log    Memory job completed successfully
            BREAK
        END

        # Failure case - job failed
        IF    '${status}' == 'failed'
            ${error_info}=    Evaluate    $job.get('exc_info', 'Unknown error')
            ${result}=    Create Dictionary
            ...    success=${False}
            ...    error_message=Memory job failed during processing: ${error_info}
            ...    status=failed
            ...    job=${job}
            RETURN    ${result}
        END

        # Timeout check
        ${current_time}=    Get Time    epoch
        IF    ${current_time} >= ${end_time}
            ${result}=    Create Dictionary
            ...    success=${False}
            ...    error_message=Memory job did not complete within ${timeout}s (last status: ${status})
            ...    status=timeout
            ...    job=${job}
            RETURN    ${result}
        END

        # Log progress every iteration
        Log    Memory job status: ${status} (waiting...)    DEBUG

        # Wait before next check
        Sleep    5s
    END

    # 4. Fetch memories from API
    TRY
        ${response}=    GET On Session    api    /api/memories    expected_status=200
        ${memories_data}=    Set Variable    ${response.json()}
        ${memories}=    Set Variable    ${memories_data}[memories]
        ${memory_count}=    Get Length    ${memories}

        # Return success result
        ${result}=    Create Dictionary
        ...    success=${True}
        ...    status=completed
        ...    job=${final_job}
        ...    memories=${memories}
        ...    memory_count=${memory_count}

        Log    Successfully extracted ${memory_count} memories
        RETURN    ${result}
    EXCEPT    AS    ${error}
        # Return error if API fetch fails
        ${result}=    Create Dictionary
        ...    success=${False}
        ...    error_message=Failed to fetch memories from API: ${error}
        ...    status=api_error
        ...    job=${final_job}
        RETURN    ${result}
    END


Check Memory Similarity With OpenAI
    [Documentation]    Use OpenAI to check if extracted memories match expected memories
    [Arguments]    ${actual_memories}    ${expected_memories}    ${openai_api_key}

    # Extract just the memory text from actual memories
    ${actual_memory_texts}=    Evaluate    [mem.get('memory', '') for mem in $actual_memories]

    # Build OpenAI prompt (same as Python test)
    ${prompt}=    Catenate    SEPARATOR=\n
    ...    Compare these two lists of memories to determine if they represent content from the same audio source.
    ...
    ...    EXPECTED MEMORIES:
    ...    ${expected_memories}
    ...
    ...    EXTRACTED MEMORIES:
    ...    ${actual_memory_texts}
    ...
    ...    Respond in JSON format with:
    ...    {"similar": true/false, "reason": "brief explanation"}

    # Call OpenAI API
    ${headers}=    Create Dictionary    Authorization=Bearer ${openai_api_key}    Content-Type=application/json
    ${payload}=    Create Dictionary
    ...    model=gpt-4o-mini
    ...    messages=${{ [{"role": "user", "content": """${prompt}"""}] }}
    ...    response_format=${{ {"type": "json_object"} }}

    ${response}=    POST    https://api.openai.com/v1/chat/completions
    ...    headers=${headers}
    ...    json=${payload}
    ...    expected_status=200

    ${result_json}=    Set Variable    ${response.json()}
    ${content}=    Set Variable    ${result_json}[choices][0][message][content]
    ${similarity_result}=    Evaluate    json.loads("""${content}""")    json

    Log    Memory similarity: ${similarity_result}[similar]    INFO
    Log    Reason: ${similarity_result}[reason]    INFO

    RETURN    ${similarity_result}


Verify Memory Quality With OpenAI
    [Documentation]    Verify extracted memories match expected memories using OpenAI
    [Arguments]    ${actual_memories}    ${expected_memories}

    # Get OpenAI API key from environment
    ${openai_key}=    Get Environment Variable    OPENAI_API_KEY

    # Check similarity
    ${result}=    Check Memory Similarity With OpenAI    ${actual_memories}    ${expected_memories}    ${openai_key}

    # Assert memories are similar
    Should Be True    ${result}[similar] == ${True}
    ...    Memory similarity check failed: ${result}[reason]

    Log    ✅ Memory quality validated    INFO
