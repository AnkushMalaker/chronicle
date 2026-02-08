*** Settings ***
Documentation    Health and Status Endpoint API Tests
Library          RequestsLibrary
Library          Collections
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/user_keywords.robot
Resource         ../resources/session_keywords.robot
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup
*** Test Cases ***

Readiness Check Test
    [Documentation]    Test readiness check endpoint for container orchestration
    [Tags]    health
    Get Anonymous Session    anon_session

    ${response}=    GET On Session    anon_session    /readiness
    Should Be Equal As Integers    ${response.status_code}    200

    ${readiness}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${readiness}    status
    Dictionary Should Contain Key    ${readiness}    timestamp
    Should Be Equal        ${readiness}[status]    ready

Health Check Test
    [Documentation]    Test main health check endpoint
    [Tags]    health

    ${response}=    GET On Session    api    /health
    Should Be Equal As Integers    ${response.status_code}    200

    ${health}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${health}    status
    Dictionary Should Contain Key    ${health}    timestamp
    Dictionary Should Contain Key    ${health}    services
    Dictionary Should Contain Key    ${health}    overall_healthy
    Dictionary Should Contain Key    ${health}    critical_services_healthy
    
    ${services}=    Set Variable    ${health}[services]
    Log To Console    \n
    Log To Console    Mongodb: ${services}[mongodb][status]    
    Log To Console    AudioAI: ${services}[audioai][status]
    Log To Console    Memory Service: ${services}[memory_service][status]
    Log To Console    Speech to Text: ${services}[speech_to_text][status]
    Log To Console    Speaker recognition: ${services}[speaker_recognition][status]
    # Verify status is one of expected values
    Should Be True    '${health}[status]' in ['healthy', 'degraded', 'critical']
    
    ${config}=    Set Variable    ${health}[config]
    Dictionary Should Contain Key    ${config}    mongodb_uri
    Dictionary Should Contain Key    ${config}    qdrant_url
    Dictionary Should Contain Key    ${config}    transcription_service
    Dictionary Should Contain Key    ${config}    asr_uri
    Dictionary Should Contain Key    ${config}    provider_type
    Dictionary Should Contain Key    ${config}    chunk_dir
    Dictionary Should Contain Key    ${config}    active_clients
    Dictionary Should Contain Key    ${config}    new_conversation_timeout_minutes
    Dictionary Should Contain Key    ${config}    llm_provider
    Dictionary Should Contain Key    ${config}    llm_model
    Dictionary Should Contain Key    ${config}    llm_base_url

    # Verify config values are not empty
    Should Not Be Empty    ${config}[mongodb_uri]
    Should Not Be Empty    ${config}[qdrant_url]
    Should Not Be Empty    ${config}[transcription_service]
    Should Not Be Empty    ${config}[asr_uri]
    Should Not Be Empty    ${config}[provider_type]
    Should Not Be Empty    ${config}[chunk_dir]
    Should Be True        isinstance(${config}[active_clients], int)
    Should Be True        ${config}[new_conversation_timeout_minutes] > 0
    Should Not Be Empty    ${config}[llm_provider]
    Should Not Be Empty    ${config}[llm_model]
    Should Not Be Empty    ${config}[llm_base_url]

Auth Health Check Test
    [Documentation]    Test authentication service health check
    [Tags]    permissions	health
    Get Anonymous Session    session

    ${response}=    GET On Session    session   /api/auth/health
    Should Be Equal As Integers    ${response.status_code}    200

    ${auth_health}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${auth_health}    status
    Dictionary Should Contain Key    ${auth_health}    database
    Dictionary Should Contain Key    ${auth_health}    memory_service
    Dictionary Should Contain Key    ${auth_health}    timestamp

Queue Worker Details Test
    [Documentation]    Test queue worker details endpoint (includes queue health and task manager)
    [Tags]    queue	health

    ${response}=    GET On Session    api    /api/queue/worker-details
    Should Be Equal As Integers    ${response.status_code}    200

    ${worker_details}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${worker_details}    architecture
    Dictionary Should Contain Key    ${worker_details}    timestamp
    Dictionary Should Contain Key    ${worker_details}    workers
    Dictionary Should Contain Key    ${worker_details}    queues
    Dictionary Should Contain Key    ${worker_details}    redis_connection

    # Verify workers structure
    ${workers}=    Set Variable    ${worker_details}[workers]
    Dictionary Should Contain Key    ${workers}    total
    Dictionary Should Contain Key    ${workers}    active
    Dictionary Should Contain Key    ${workers}    idle
    Dictionary Should Contain Key    ${workers}    details

    # Verify queues structure
    ${queues}=    Set Variable    ${worker_details}[queues]
    Dictionary Should Contain Key    ${queues}    default
    Dictionary Should Contain Key    ${queues}    transcription
    Dictionary Should Contain Key    ${queues}    memory
    Dictionary Should Contain Key    ${queues}    audio

Chat Health Check Test
    [Documentation]    Test chat service health check
    [Tags]    chat	health
    Get Anonymous Session    session

    ${response}=    GET On Session    session    /api/chat/health
    Should Be Equal As Integers    ${response.status_code}    200

    ${chat_health}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${chat_health}    status
    Dictionary Should Contain Key    ${chat_health}    service
    Dictionary Should Contain Key    ${chat_health}    timestamp
    Should Be Equal        ${chat_health}[service]    chat

System Metrics Test
    [Documentation]    Test system metrics endpoint (admin only)
    [Tags]    permissions

    ${response}=       GET On Session    api    /api/metrics
    Should Be Equal As Integers    ${response.status_code}    200

    ${metrics}=        Set Variable    ${response.json()}
    # Metrics structure may vary, just verify it's a valid response
    Should Be True     isinstance($metrics, dict)

Queue Stats Test
    [Documentation]    Test queue stats endpoint
    [Tags]    queue

    ${response}=       GET On Session    api    /api/queue/stats
    Should Be Equal As Integers    ${response.status_code}    200

    ${stats}=          Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${stats}    total_jobs
    Dictionary Should Contain Key    ${stats}    queued_jobs
    Dictionary Should Contain Key    ${stats}    started_jobs
    Dictionary Should Contain Key    ${stats}    finished_jobs
    Dictionary Should Contain Key    ${stats}    failed_jobs
    Dictionary Should Contain Key    ${stats}    timestamp


Health Check Service Details Test
    [Documentation]    Test detailed service health information including Redis workers
    [Tags]    health
    Get Anonymous Session    session
    ${response}=    GET On Session    session    /health
    Should Be Equal As Integers    ${response.status_code}    200

    ${health}=    Set Variable    ${response.json()}
    ${services}=    Set Variable    ${health}[services]

    # Check for expected services
    ${expected_services}=    Create List    mongodb    redis    audioai    memory_service    speech_to_text

    FOR    ${service}    IN    @{expected_services}
        IF    '${service}' in $services
            ${service_info}=    Set Variable    ${services}[${service}]
            Dictionary Should Contain Key    ${service_info}    status
            Dictionary Should Contain Key    ${service_info}    healthy
            Dictionary Should Contain Key    ${service_info}    critical
        END
    END

    # Verify Redis service includes worker information
    ${redis_service}=    Set Variable    ${services}[redis]
    Dictionary Should Contain Key    ${redis_service}    worker_count
    Dictionary Should Contain Key    ${redis_service}    active_workers
    Dictionary Should Contain Key    ${redis_service}    idle_workers
    Dictionary Should Contain Key    ${redis_service}    queues
    Should Be True    isinstance(${redis_service}[worker_count], int)
    Should Be True    isinstance(${redis_service}[active_workers], int)
    Should Be True    isinstance(${redis_service}[idle_workers], int)

Non-Admin Cannot Access Admin Endpoints Test
    [Documentation]    Test that non-admin users cannot access admin health endpoints
    [Tags]    health	permissions

    # Create a non-admin user
    ${test_user}=      Create Test User    api
    Create API Session    user_session    email=${test_user}[email]    password=${TEST_USER_PASSWORD}

    # Metrics endpoint should be forbidden
    ${response}=       GET On Session    user_session    /api/metrics    expected_status=403
    Should Be Equal As Integers    ${response.status_code}    403

    # Cleanup
    [Teardown]    Delete User    api    ${test_user}[id]

Unauthorized Health Access Test
    [Documentation]    Test health endpoints that require authentication
    [Tags]    health	permissions
    Get Anonymous Session    session

    # Admin-only endpoints should require authentication
    ${response}=    GET On Session    session    /api/metrics    expected_status=401
    Should Be Equal As Integers    ${response.status_code}    401

