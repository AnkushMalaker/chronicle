*** Settings ***
Documentation    Flexible setup keywords for test environments
...
...              DEFAULT MODE: Dev mode - keeps containers running for fast iteration
...
...              This file provides two primary modes:
...              - Dev Mode (default): Reuse containers, clear data only (~5s)
...              - Prod Mode: Complete teardown and rebuild (for CI/CD)
...
...              Control via environment variables:
...              - TEST_MODE: 'dev' (default) or 'prod' (CI/CD mode)
...              - REBUILD: Force container rebuild (dev mode only)
...
...              Quick usage:
...              - robot tests/                    # Dev mode (fast, keep containers)
...              - TEST_MODE=prod robot tests/     # Prod mode (CI/CD, fresh env)
Library          RequestsLibrary
Library          Collections
Library          OperatingSystem
Library          String
Library          Process
Variables        test_env.py
Resource         ../resources/session_keywords.robot
Resource         ../resources/system_keywords.robot
Resource         test_manager_keywords.robot



*** Keywords ***

Suite Setup
    [Documentation]    Flexible setup based on TEST_MODE environment variable
    ...                DEFAULT: dev mode (keep containers running)
    ...                TEST_MODE=prod for CI/CD (fresh environment)

    # Get test mode (default: dev)
    ${test_mode}=    Get Environment Variable    TEST_MODE    default=dev
    ${rebuild}=      Get Environment Variable    REBUILD      default=false

    # Handle different startup modes
    Run Keyword If    '${test_mode}' == 'prod'     Prod Mode Setup
    ...    ELSE IF    '${rebuild}' == 'true'       Dev Mode Setup With Rebuild
    ...    ELSE                                     Dev Mode Setup

    # Create admin session for all tests
    Create API Session    api

    # Create fixture conversation if CREATE_FIXTURE env var is set (typically in Makefile 'all' target)
    # This is best-effort - if it fails, tests continue
    ${create_fixture}=    Get Environment Variable    CREATE_FIXTURE    default=false
    Run Keyword If    '${create_fixture}' == 'true'    Run Keyword And Ignore Error    Create Fixture Conversation

Dev Mode Setup
    [Documentation]    Default development mode - reuse containers, clear data only (fastest)
    Log To Console    \n=== Dev Mode Setup (Default) ===

    Log To Console    Checking if services are ready at ${API_URL}...
    ${is_up}=    Run Keyword And Return Status    Check Services Ready    ${API_URL}
    Log To Console    Services ready check result: ${is_up}

    IF    ${is_up}
        Log To Console    ✓ Reusing existing containers (fast mode)
        Clear Test Databases
    ELSE
        Log To Console    ⚠ Containers not running, starting them...
        Start Docker Services
        Clear Test Databases
    END

    Log To Console    ✓ Dev environment ready!

Dev Mode Setup With Rebuild
    [Documentation]    Dev mode with forced rebuild (after code changes)
    Log To Console    \n=== Dev Mode Setup (with rebuild) ===

    Rebuild Docker Services

    Log To Console    Clearing test data...
    Clear Test Databases
    Log To Console    ✓ Dev environment ready!

Prod Mode Setup
    [Documentation]    Production/CI mode - complete teardown and rebuild (clean slate)
    Log To Console    \n=== Prod Mode Setup (CI/CD) ===
    Log To Console    Tearing down existing containers and volumes...

    Stop Docker Services    remove_volumes=${True}
    Run Process    rm    -rf    data/test_mongo_data    data/test_qdrant_data    data/test_audio_chunks    cwd=${BACKEND_DIR}    shell=True

    Log To Console    Building and starting fresh containers...
    Start Docker Services    build=${True}

    Log To Console    ✓ Prod environment ready!

Start Docker Services
    [Documentation]    Start Docker services using docker-compose
    ...                Checks if services are already running to avoid redundant starts
    [Arguments]    ${compose_file}=docker-compose-test.yml    ${working_dir}=${BACKEND_DIR}    ${build}=${False}

    ${is_up}=    Run Keyword And Return Status    Check Services Ready    ${API_URL}

    IF    ${is_up}
        Log    Services already running, skipping start
        RETURN
    END

    # Clean up any stopped/stuck containers first
    Run Process    docker    compose    -f    ${compose_file}    down    -v    cwd=${working_dir}    shell=True
    Run Process    docker    rm    -f    ${MONGO_CONTAINER}    ${REDIS_CONTAINER}    ${QDRANT_CONTAINER}    ${BACKEND_CONTAINER}    ${WORKERS_CONTAINER}    ${WEBUI_CONTAINER}    shell=True

    # Start containers
    IF    ${build}
        Run Process    docker    compose    -f    ${compose_file}    up    -d    --build    cwd=${working_dir}    shell=True
    ELSE
        Run Process    docker    compose    -f    ${compose_file}    up    -d    cwd=${working_dir}    shell=True
    END

    Log    Waiting for services to be ready...
    Wait Until Keyword Succeeds    60s    5s    Check Services Ready    ${API_URL}

Stop Docker Services
    [Documentation]    Stop Docker services using docker-compose
    [Arguments]    ${compose_file}=docker-compose-test.yml    ${working_dir}=${BACKEND_DIR}    ${remove_volumes}=${False}

    IF    ${remove_volumes}
        Run Process    docker    compose    -f    ${compose_file}    down    -v    cwd=${working_dir}    shell=True
    ELSE
        Run Process    docker    compose    -f    ${compose_file}    down    cwd=${working_dir}    shell=True
    END

Rebuild Docker Services
    [Documentation]    Rebuild and restart Docker services
    [Arguments]    ${compose_file}=docker-compose-test.yml    ${working_dir}=${BACKEND_DIR}

    Log To Console    Rebuilding containers with latest code...
    Run Process    docker    compose    -f    ${compose_file}    up    -d    --build    cwd=${working_dir}    shell=True

    Log To Console    Waiting for services to be ready...
    Wait Until Keyword Succeeds    60s    5s    Check Services Ready    ${API_URL}
    Log To Console    ✓ Services rebuilt and ready!

Check Services Ready
    [Documentation]    Check if services are ready via readiness endpoint
    [Arguments]    ${base_url}=${API_URL}

    Readiness Check    ${base_url}

Start Speaker Recognition Service
    [Documentation]    Start the speaker recognition service using docker compose
    ${is_up}=    Run Keyword And Return Status    Check Services Ready    ${SPEAKER_RECOGNITION_URL}

    IF    ${is_up}
        Log    speaker-recognition-service is already running
        RETURN
    END

    Log    Starting speaker-recognition-service
    Run Process    docker    compose    -f    docker-compose-test.yml    up    -d    --build    cwd=${SPEAKER_RECOGNITION_DIR}    shell=True

    Log    Waiting for speaker recognition service to start...
    Wait Until Keyword Succeeds    60s    5s    Check Services Ready    ${SPEAKER_RECOGNITION_URL}
    Log    Speaker recognition service is ready

Stop Speaker Recognition Service
    [Documentation]    Stop the speaker recognition service
    [Arguments]    ${remove_volumes}=${False}

    IF    ${remove_volumes}
        Run Process    docker    compose    -f    docker-compose-test.yml    down    -v    cwd=${SPEAKER_RECOGNITION_DIR}    shell=True
    ELSE
        Run Process    docker    compose    -f    docker-compose-test.yml    down    cwd=${SPEAKER_RECOGNITION_DIR}    shell=True
    END

Check Environment Variables
    [Documentation]    Check required environment variables and return missing ones
    [Arguments]    @{required_vars}

    @{missing_vars}=    Create List
    FOR    ${var}    IN    @{required_vars}
        ${value}=    Get Environment Variable    ${var}    ${EMPTY}
        IF    '${value}' == '${EMPTY}'
            Append To List    ${missing_vars}    ${var}
        ELSE
            Log    Environment variable ${var} is set    DEBUG
        END
    END
    RETURN    ${missing_vars}

