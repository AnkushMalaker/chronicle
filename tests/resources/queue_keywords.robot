*** Settings ***
Documentation    Queue Management Keywords
Library          RequestsLibrary
Library          Collections
Variables        ../setup/test_env.py
Resource         session_keywords.robot

*** Keywords ***

Get job queue
    [Documentation]    Get the current job queue from Redis
    [Arguments]    ${queue_name}=default
    ${response}=    GET On Session    api    /api/queue/jobs
    ${jobs}=    Set Variable    ${response.json()}[jobs]
    RETURN    ${jobs}

Get queue length
    [Documentation]    Get the length of the specified job queue
    [Arguments]    ${queue_name}=default
    ${jobs}=     Get job queue     ${queue_name}
    ${length}=    Get Length    ${jobs}
    RETURN    ${length}

Get Job Details
    [Documentation]    Get job details from the queue API by searching the jobs list
    [Arguments]    ${job_id}

    ${response}=    GET On Session    api    /api/queue/jobs
    Should Be Equal As Integers    ${response.status_code}    200
    ${jobs_data}=    Set Variable    ${response.json()}
    ${jobs}=    Set Variable    ${jobs_data}[jobs]

    # Find the job with matching job_id
    FOR    ${job}    IN    @{jobs}
        IF    '${job}[job_id]' == '${job_id}'
            RETURN    ${job}
        END
    END

    # If we get here, job not found - return None
    RETURN    ${None}

Get Job Status
    [Documentation]    Get just the status of a specific job by ID (lightweight endpoint)
    [Arguments]    ${job_id}

    # Use the lightweight status endpoint - try to get the response
    ${success}=    Run Keyword And Return Status    GET On Session    api    /api/queue/jobs/${job_id}/status    expected_status=200

    IF    not ${success}
        # Job not found
        RETURN    ${None}
    END

    # Now actually get the response
    ${response}=    GET On Session    api    /api/queue/jobs/${job_id}/status
    RETURN    ${response.json()}


Check job status
    [Documentation]    Check the status of a specific job by ID
    ...                Fails immediately if job is in 'failed' state when expecting 'completed'
    [Arguments]    ${job_id}    ${expected_status}

    ${job}=    Get Job status    ${job_id}

    # If job is None (not found), fail explicitly
    Should Not Be Equal    ${job}[job_id]    ${None}    Job with ID ${job_id} not found in queue

    ${actual_status}=    Set Variable    ${job}[status]
    Log    Job ${job_id} status: ${actual_status} (expected: ${expected_status})

    # Fail fast if job is in failed state when we're expecting completed
    IF    '${actual_status}' == 'failed' and '${expected_status}' == 'completed'
        ${error_msg}=    Evaluate    $job.get('exc_info') or $job.get('error', 'Unknown error')
        Fail    Job ${job_id} failed: ${error_msg}
    END

    Should Be Equal As Strings    ${actual_status}    ${expected_status}    Job status is '${actual_status}', expected '${expected_status}'

    RETURN    ${job}
Clear job queue
    [Documentation]    Clear all jobs from the specified queue
    [Arguments]    ${queue_name}=default
    ${response}=    DELETE On Session    api    /api/queue/jobs
    RETURN    ${response}

Clear job by ID
    [Documentation]    Clear a specific job by ID
    [Arguments]    ${job_id}

    ${response}=    DELETE On Session    api    /api/queue/jobs/${job_id}
    RETURN    ${response}

Enqueue test job
    [Documentation]    Enqueue a test job into the specified queue
    [Arguments]    ${queue_name}=default    ${job_data}={}

    &{data}=    Create Dictionary    queue=${queue_name}    job_data=${job_data}
    ${response}=    POST On Session    api    /api/queue/enqueue    json=${data}
    RETURN    ${response}

Get Jobs By Type
    [Documentation]    Get jobs filtered by job type (uses API filtering)
    [Arguments]    ${job_type}    ${limit}=100

    &{params}=    Create Dictionary    job_type=${job_type}    limit=${limit}
    ${response}=    GET On Session    api    /api/queue/jobs    params=${params}
    Should Be Equal As Integers    ${response.status_code}    200

    ${jobs}=    Set Variable    ${response.json()}[jobs]
    Log    Found ${jobs.__len__()} jobs of type ${job_type}
    RETURN    ${jobs}

Get Jobs By Type And Client
    [Documentation]    Get jobs filtered by job type and client ID (uses API filtering)
    [Arguments]    ${job_type}    ${client_id}    ${limit}=100

    &{params}=    Create Dictionary    job_type=${job_type}    client_id=${client_id}    limit=${limit}
    ${response}=    GET On Session    api    /api/queue/jobs    params=${params}
    Should Be Equal As Integers    ${response.status_code}    200

    ${jobs}=    Set Variable    ${response.json()}[jobs]
    Log    Found ${jobs.__len__()} jobs of type ${job_type} for client ${client_id}
    RETURN    ${jobs}

Find Job For Client
    [Documentation]    Find a job that matches the given client ID pattern in a list
    [Arguments]    ${jobs}    ${client_pattern}

    FOR    ${job}    IN    @{jobs}
        ${meta}=    Set Variable    ${job}[meta]
        ${client_id}=    Evaluate    $meta.get('client_id', '')

        ${matches}=    Evaluate    "${client_pattern}" in "${client_id}"
        IF    ${matches}
            RETURN    ${job}
        END
    END

    RETURN    ${None}

Wait For Job Status
    [Documentation]    Wait for a job to reach a specific status
    [Arguments]    ${job_id}    ${expected_status}    ${timeout}=60s    ${interval}=5s

    Wait Until Keyword Succeeds    ${timeout}    ${interval}
    ...    Check job status    ${job_id}    ${expected_status}

Find Job By ID In List
    [Documentation]    Find a job by its job_id in a list of jobs
    [Arguments]    ${jobs}    ${job_id}

    FOR    ${job}    IN    @{jobs}
        IF    '${job}[job_id]' == '${job_id}'
            RETURN    ${job}
        END
    END

    RETURN    ${None}

Job Type Exists For Client
    [Documentation]    Check if at least one job of given type exists for client. Returns the jobs list.
    [Arguments]    ${job_type}    ${client_pattern}    ${number}=1

    ${jobs}=    Get Jobs By Type And Client    ${job_type}    ${client_pattern}
    Length Should Be    ${jobs}  ${number}    Expected ${number} jobs of ${job_type} type, found len(${jobs} for ${client_pattern}
    RETURN    ${jobs}

Wait For New Job To Appear
    [Documentation]    Wait for a new job to appear (job count increases from baseline).
    ...                Returns the current jobs list when count > baseline_count.
    [Arguments]    ${job_type}    ${client_id}    ${baseline_count}

    ${jobs}=    Get Jobs By Type And Client    ${job_type}    ${client_id}
    ${current_count}=    Get Length    ${jobs}
    Should Be True    ${current_count} > ${baseline_count}
    ...    Expected new ${job_type} job to appear for ${client_id}, but count is still ${current_count} (baseline: ${baseline_count})
    RETURN    ${jobs}

Wait For New Conversation Job
    [Documentation]    Wait for a new conversation job with a conversation_id not in the existing list.
    ...                Returns the new job when found.
    [Arguments]    ${job_type}    ${client_id}    ${existing_conv_ids}

    ${jobs}=    Get Jobs By Type And Client    ${job_type}    ${client_id}

    # Look for a job with a new conversation_id
    FOR    ${job}    IN    @{jobs}
        ${meta}=    Set Variable    ${job}[meta]
        ${conv_id}=    Evaluate    $meta.get('conversation_id', '')
        ${is_new}=    Evaluate    $conv_id not in $existing_conv_ids if $conv_id else False
        IF    ${is_new}
            Log    Found new conversation job: ${conv_id}
            RETURN    ${job}
        END
    END

    # If we get here, no new conversation was found
    Fail    Expected new ${job_type} job with new conversation_id for ${client_id}, found only existing conversations: ${existing_conv_ids}

Job Has Conversation ID
    [Documentation]    Check if job has conversation_job_id in meta
    [Arguments]    ${job}

    ${meta}=    Set Variable    ${job}[meta]
    ${conv_job_id}=    Evaluate    $meta.get('conversation_job_id')
    Should Not Be Equal    ${conv_job_id}    ${None}    conversation_job_id not set in job meta
    RETURN    ${conv_job_id}

Get Jobs By Type And Conversation
    [Documentation]    Get jobs filtered by job type and conversation_id in meta
    [Arguments]    ${job_type}    ${conversation_id}    ${limit}=100

    # Get all jobs of this type, then filter by conversation_id in meta
    ${all_jobs}=    Get Jobs By Type    ${job_type}    ${limit}
    ${matching_jobs}=    Create List

    FOR    ${job}    IN    @{all_jobs}
        ${meta}=    Set Variable    ${job}[meta]
        ${job_conv_id}=    Evaluate    $meta.get('conversation_id', '')
        IF    '${job_conv_id}' == '${conversation_id}'
            Append To List    ${matching_jobs}    ${job}
        END
    END

    Log    Found ${matching_jobs.__len__()} jobs of type ${job_type} for conversation ${conversation_id}
    RETURN    ${matching_jobs}

Job Type Exists For Conversation
    [Documentation]    Check if at least one job of given type exists for conversation. Returns the jobs list.
    [Arguments]    ${job_type}    ${conversation_id}

    ${jobs}=    Get Jobs By Type And Conversation    ${job_type}    ${conversation_id}
    Should Not Be Empty    ${jobs}    No ${job_type} jobs found for conversation ${conversation_id}
    RETURN    ${jobs}

Cancel All Running Jobs
    [Documentation]    Cancel all running/queued jobs in all queues
    ...                Use in test teardown to clean up lingering jobs

    Log    Cancelling all running jobs
    TRY
        ${payload}=    Create Dictionary    confirm=${True}
        ${response}=    POST On Session    api    /api/queue/flush-all    json=${payload}    expected_status=200
        Log    Successfully cancelled all jobs: ${response.json()}
    EXCEPT    AS    ${error}
        Log    Failed to cancel jobs: ${error}
    END

Flush In Progress Jobs
    [Documentation]    Flush only queued and in-progress jobs (preserves completed/failed jobs)
    ...                Use in test cleanup to reset queue state without losing job history

    Log To Console    Flushing in-progress and queued jobs...
    TRY
        ${payload}=    Create Dictionary    confirm=${True}
        ${response}=    POST On Session    api    /api/queue/flush-all    json=${payload}    expected_status=200
        ${result}=    Set Variable    ${response.json()}
        Log To Console    Successfully flushed ${result}[total_removed] jobs
        RETURN    ${result}[total_removed]
    EXCEPT    AS    ${error}
        Log    Failed to flush jobs: ${error}    WARN
        RETURN    0
    END

Get Queue Stats
    [Documentation]    Get current queue statistics from the queue API
    ${response}=    GET On Session    api    /api/queue/stats    expected_status=200
    RETURN    ${response.json()}

Get Queue Worker Details
    [Documentation]    Get queue worker health and status information
    ${response}=    GET On Session    api    /api/queue/worker-details    expected_status=200
    RETURN    ${response.json()}

Get Most Recent Job
    [Documentation]    Get the job with the most recent created_at timestamp from a list
    ...                Optionally filter by job_type
    [Arguments]    ${jobs}    ${job_type}=${None}

    ${most_recent}=    Set Variable    ${None}
    ${most_recent_time}=    Set Variable    ${None}

    FOR    ${job}    IN    @{jobs}
        # Filter by job_type if specified
        IF    '${job_type}' != '${None}'
            ${current_job_type}=    Set Variable    ${job}[job_type]
            ${type_matches}=    Evaluate    '${current_job_type}' == '${job_type}'
            IF    not ${type_matches}
                CONTINUE
            END
        END

        ${created_at}=    Set Variable    ${job}[created_at]
        IF    $most_recent_time is None or $created_at > $most_recent_time
            ${most_recent}=    Set Variable    ${job}
            ${most_recent_time}=    Set Variable    ${created_at}
        END
    END

    IF    '${job_type}' != '${None}'
        Should Not Be Equal    ${most_recent}    ${None}    No jobs found in list with job_type=${job_type}
    ELSE
        Should Not Be Equal    ${most_recent}    ${None}    No jobs found in list
    END
    Log    Most recent job created at: ${most_recent_time}
    RETURN    ${most_recent}

Get Conversation ID From Job Meta
    [Documentation]    Extract conversation_id from job meta, fails if not present
    [Arguments]    ${job_type}    ${device_name}

    ${conv_jobs}=    Get Jobs By Type And Client    ${job_type}    ${device_name}
    ${conv_job}=    Get Most Recent Job    ${conv_jobs}
    ${conv_meta}=    Set Variable    ${conv_job}[meta]
    ${conversation_id}=    Evaluate    $conv_meta.get('conversation_id', '')
    Should Not Be Empty    ${conversation_id}    msg=Conversation ID not found in job meta
    RETURN    ${conversation_id}

Job Should Be Complete
    [Documentation]    Check if job has reached a completed state (completed, finished, or failed)
    [Arguments]    ${job_id}

    ${job}=    Get Job status    ${job_id}
    ${status}=    Set Variable    ${job}[status]
    Should Be True    '${status}' in ['completed', 'finished', 'failed']    Job status: ${status}