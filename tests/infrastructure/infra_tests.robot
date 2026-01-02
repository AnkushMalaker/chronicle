*** Settings ***
Documentation    Infrastructure Resilience Tests
...
...              This suite tests infrastructure components for resilience and recovery.
...              It simulates failure scenarios and verifies the system can detect and recover.
...
...              Test Scenarios:
...              - Worker registration loss (Redis restart/network issue)
...              - Service dependency failures
...              - Recovery mechanisms
...              - WebSocket disconnect tracking
Library          RequestsLibrary
Library          Collections
Library          Process
Library          OperatingSystem
Library          DateTime
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/session_keywords.robot
Resource         ../resources/websocket_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/queue_keywords.robot
Variables        ../setup/test_env.py
Variables        ../setup/test_data.py
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup
*** Variables ***
# Container names are now dynamically loaded from test_env.py based on COMPOSE_PROJECT_NAME
# This allows tests to work with different docker-compose project names

*** Keywords ***

Get Worker Count From Health Endpoint
    [Documentation]    Get current worker count from health endpoint
    Get Anonymous Session    health_session
    ${response}=    GET On Session    health_session    /health
    Should Be Equal As Integers    ${response.status_code}    200

    ${health}=    Set Variable    ${response.json()}
    ${services}=    Set Variable    ${health}[services]
    ${redis_service}=    Set Variable    ${services}[redis]

    ${worker_count}=    Set Variable    ${redis_service}[worker_count]
    RETURN    ${worker_count}

Verify Minimum Worker Count
    [Documentation]    Verify that at least the minimum number of workers are registered
    [Arguments]    ${min_count}
    ${worker_count}=    Get Worker Count From Health Endpoint
    Log To Console    Current worker count: ${worker_count}
    Should Be True    ${worker_count} >= ${min_count}    msg=Expected at least ${min_count} workers, got ${worker_count}
    RETURN    ${worker_count}

Simulate Worker Registration Loss
    [Documentation]    Simulate the scenario where workers lose Redis registration
    ...                This happens when:
    ...                - Redis restarts while workers are running
    ...                - Network interruption between workers and Redis
    ...                - Workers fail to send heartbeats
    ...
    ...                We simulate this by forcing workers to unregister from Redis
    Log To Console    \n🔧 Simulating worker registration loss...

    # Force workers to unregister by calling register_death() on all workers
    ${python_code}=    Set Variable    from rq import Worker; from redis import Redis; import os; r = Redis.from_url(os.getenv('REDIS_URL')); workers = Worker.all(connection=r); [w.register_death() for w in workers]; print(f'Unregistered all workers')
    ${result}=    Run Process    docker    exec    ${WORKERS_CONTAINER}    sh    -c    uv run python -c "${python_code}"
    ...    shell=False

    Log To Console    ${result.stdout}
    Log To Console    ${result.stderr}
    Should Be Equal As Integers    ${result.rc}    0
    Log To Console    ✅ Workers unregistered from Redis (simulating registration loss)

Verify Workers Still Running In Container
    [Documentation]    Verify worker processes are still running even after losing Redis registration
    Log To Console    \n🔍 Verifying worker processes are still running...

    # Check that worker processes exist in the container
    ${result}=    Run Process    docker    exec    ${WORKERS_CONTAINER}
    ...    sh    -c    find /proc -maxdepth 1 -name '[0-9]*' -type d | wc -l
    ...    shell=False

    ${process_count}=    Convert To Integer    ${result.stdout.strip()}
    Log To Console    Found ${process_count} processes in container
    Should Be True    ${process_count} > 10    msg=Expected multiple processes running
    Log To Console    ✅ Worker processes still running in container

Restart Workers Container
    [Documentation]    Restart the workers container to restore registration
    Log To Console    \n🔄 Restarting workers container...

    ${result}=    Run Process    docker    restart    ${WORKERS_CONTAINER}    shell=False
    Should Be Equal As Integers    ${result.rc}    0

    # Wait for workers to start
    Sleep    5s    reason=Wait for workers to initialize
    Log To Console    ✅ Workers container restarted

*** Test Cases ***
Worker Registration Loss Detection Test
    [Documentation]    Test that the system can automatically recover when workers lose Redis registration
    ...
    ...                This test simulates the exact failure scenario experienced:
    ...                1. Workers are running and processing jobs
    ...                2. Workers lose Redis registration (Redis restart, network issue, etc.)
    ...                3. Health endpoint should detect 0 workers
    ...                4. Workers should still be running in container
    ...                5. Self-healing mechanism should detect and fix the issue
    ...
    ...                Expected behavior WITHOUT self-healing (this test will FAIL):
    ...                - Health endpoint reports 0 workers when registration is lost
    ...                - Worker processes continue running
    ...                - Workers DO NOT automatically re-register
    ...
    ...                Expected behavior WITH self-healing (this test should PASS):
    ...                - Health endpoint reports 0 workers when registration is lost
    ...                - Self-healing mechanism detects the issue
    ...                - Workers automatically re-register within monitoring interval
    [Tags]    infra	queue

    # Step 1: Verify workers are initially registered
    Log To Console    \n📊 Step 1: Check initial worker registration
    ${initial_workers}=    Get Worker Count From Health Endpoint
    Log To Console    Initial worker count: ${initial_workers}
    Should Be True    ${initial_workers} > 0    msg=Expected workers to be registered initially

    # Step 2: Simulate worker registration loss (the failure we experienced)
    Log To Console    \n📊 Step 2: Simulate worker registration loss
    Simulate Worker Registration Loss

    # Step 3: Verify health endpoint detects the failure
    Log To Console    \n📊 Step 3: Verify health endpoint detects 0 workers
    ${workers_after_loss}=    Get Worker Count From Health Endpoint
    Log To Console    Worker count after registration loss: ${workers_after_loss}
    Should Be Equal As Integers    ${workers_after_loss}    0    msg=Health endpoint should detect 0 workers

    # Step 4: Verify workers are still running (this proves it's a registration issue, not a crash)
    Log To Console    \n📊 Step 4: Verify worker processes still running
    Verify Workers Still Running In Container

    # Step 5: Wait for self-healing mechanism to detect and fix the issue
    # Without self-healing: This will fail because workers stay unregistered
    # With self-healing: Workers should re-register within monitoring interval (60s)
    Log To Console    \n📊 Step 5: Wait for self-healing mechanism to restore registration
    Log To Console    ⏳ Waiting up to 90 seconds for workers to auto-recover...

    ${recovered}=    Set Variable    ${False}
    FOR    ${i}    IN RANGE    18    # Check every 5 seconds for 90 seconds
        Sleep    5s    reason=Wait for self-healing to detect and fix
        ${current_workers}=    Get Worker Count From Health Endpoint
        Log To Console    [Check ${i+1}/18] Worker count: ${current_workers}

        IF    ${current_workers} > 0
            ${recovered}=    Set Variable    ${True}
            Log To Console    ✅ Self-healing detected! Workers recovered after ${${i}*5 + 5} seconds
            BREAK
        END
    END

    # Step 6: Verify self-healing worked
    IF    ${recovered}
        ${final_workers}=    Get Worker Count From Health Endpoint
        Log To Console    \n✅ Test PASSED: Self-healing mechanism successfully restored ${final_workers} workers
        Should Be True    ${final_workers} >= 6    msg=Expected at least 6 workers after self-healing
    ELSE
        Log To Console    \n❌ Test FAILED: Workers did not auto-recover after 90 seconds
        Log To Console    ⚠️ This is expected without self-healing mechanism
        Log To Console    💡 Next step: Implement self-healing in start-workers.sh
        Fail    Self-healing mechanism not working: Workers did not re-register after 90 seconds
    END

    # Cleanup: Always restart workers after this test to ensure subsequent tests work
    [Teardown]    Run Keywords
    ...    Log To Console    \n🧹 Cleanup: Restarting workers for subsequent tests
    ...    AND    Restart Workers Container

Worker Count Validation Test
    [Documentation]    Verify the health endpoint accurately reports worker counts
    ...
    ...                This test validates that:
    ...                - Health endpoint includes worker_count field
    ...                - Worker count matches expected number (7 workers: 6 RQ + 1 audio)
    ...                - Worker state information is accurate
    [Tags]    health	queue

    # Wait for workers to register (up to 20s, checking every 2s)
    Log To Console    \n⏳ Waiting for workers to register (up to 20s)...
    ${worker_count}=    Wait Until Keyword Succeeds    20s    2s
    ...    Verify Minimum Worker Count    6
    Log To Console    ✅ Workers registered: ${worker_count}

    # Get full health details for validation
    ${response}=    GET On Session    api    /health
    Should Be Equal As Integers    ${response.status_code}    200

    ${health}=    Set Variable    ${response.json()}
    ${services}=    Set Variable    ${health}[services]

    # Verify Redis service structure
    Dictionary Should Contain Key    ${services}    redis
    ${redis_service}=    Set Variable    ${services}[redis]

    # Verify worker count fields exist
    Dictionary Should Contain Key    ${redis_service}    worker_count
    Dictionary Should Contain Key    ${redis_service}    active_workers
    Dictionary Should Contain Key    ${redis_service}    idle_workers

    # Get worker state details
    ${worker_count}=    Set Variable    ${redis_service}[worker_count]
    ${active_workers}=    Set Variable    ${redis_service}[active_workers]
    ${idle_workers}=    Set Variable    ${redis_service}[idle_workers]

    Log To Console    \n📊 Worker Status:
    Log To Console    Total workers: ${worker_count}
    Log To Console    Active workers: ${active_workers}
    Log To Console    Idle workers: ${idle_workers}

    # Verify exact worker count
    # Expected: 7 RQ workers (6 general workers + 1 audio persistence worker)
    # Note: Audio stream workers (Deepgram/Parakeet) are NOT RQ workers - they don't register
    # We wait up to 20s for registration, so all workers should be present
    Should Be Equal As Integers    ${worker_count}    7    msg=Expected exactly 7 RQ workers (6 general + 1 audio persistence)

    # Verify active + idle = total
    ${sum}=    Evaluate    ${active_workers} + ${idle_workers}
    Should Be Equal As Integers    ${sum}    ${worker_count}    msg=Active + Idle should equal total

Redis Connection Resilience Test
    [Documentation]    Verify health endpoint can detect Redis connection issues
    ...
    ...                This test validates that the health endpoint:
    ...                - Reports Redis status correctly
    ...                - Marks Redis as critical service
    ...                - Can detect connection failures
    [Tags]    health	infra

    ${response}=    GET On Session    health_session    /health
    Should Be Equal As Integers    ${response.status_code}    200

    ${health}=    Set Variable    ${response.json()}
    ${services}=    Set Variable    ${health}[services]
    ${redis_service}=    Set Variable    ${services}[redis]

    # Verify Redis service health fields
    Dictionary Should Contain Key    ${redis_service}    status
    Dictionary Should Contain Key    ${redis_service}    healthy
    Dictionary Should Contain Key    ${redis_service}    critical

    # Redis should be marked as critical
    Should Be True    ${redis_service}[critical]    msg=Redis should be marked as critical service

    # Redis should be healthy
    Should Be True    ${redis_service}[healthy]    msg=Redis should be healthy

    # Status should indicate Redis is working (can be "operational" or "✅ Connected")
    ${status}=    Set Variable    ${redis_service}[status]
    Should Not Be Empty    ${status}    msg=Redis status should not be empty
    Log To Console    Redis status: ${status}

    Log To Console    \n✅ Redis health check working correctly

WebSocket Disconnect Conversation End Reason Test
    [Documentation]    Test that WebSocket disconnects are tracked with proper end_reason
    ...
    ...                This test simulates a Bluetooth/network dropout scenario:
    ...                1. Start streaming audio and create conversation
    ...                2. Abruptly close WebSocket (simulating disconnect)
    ...                3. Verify job exits gracefully (no 3600s timeout)
    ...                4. Verify conversation has end_reason='websocket_disconnect'
    [Tags]    infra	audio-streaming

    # Start audio stream and send chunks to trigger conversation
    ${device_name}=    Set Variable    disconnect
    ${stream_id}=    Open Audio Stream    device_name=${device_name}

    # Send audio fast (no realtime pacing) to simulate disconnect before END signal
    Send Audio Chunks To Stream    ${stream_id}    ${TEST_AUDIO_FILE}    num_chunks=200 

    # Wait for conversation job to be created and conversation_id to be populated
    # Transcription + speech analysis takes time (30-60s with queue)
    ${conv_jobs}=    Wait Until Keyword Succeeds    60s    3s
    ...    Job Type Exists For Client    open_conversation    ${device_name}

    # Wait for conversation_id in job meta (created asynchronously)
    ${conversation_id}=    Wait Until Keyword Succeeds    10s    0.5s
    ...    Get Conversation ID From Job Meta    open_conversation    ${device_name}

    # Simulate WebSocket disconnect (Bluetooth dropout)
    Close Audio Stream    ${stream_id}

    # Wait for job to complete (should be fast, not 3600s timeout)
    ${conv_jobs}=    Get Jobs By Type And Client    open_conversation    ${device_name}
    ${conv_job}=    Get Most Recent Job    ${conv_jobs}
    Wait For Job Status    ${conv_job}[job_id]    completed    timeout=60s    interval=2s

    # Wait for end_reason to be saved to database (retry with timeout)
    ${conversation}=    Wait Until Keyword Succeeds    10s    0.5s
    ...    Check Conversation Has End Reason    ${conversation_id}

    # Verify conversation was saved with correct end_reason
    ${end_reason}=    Set Variable    ${conversation}[end_reason]
    Should Be Equal As Strings    ${end_reason}    websocket_disconnect
    Should Not Be Equal    ${conversation}[completed_at]    ${None}

    [Teardown]    Run Keyword And Ignore Error    Close Audio Stream    ${stream_id}

