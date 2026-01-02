*** Settings ***
Documentation    Flexible teardown keywords for test environments
...
...              DEFAULT MODE: Dev mode - keeps containers running for fast iteration
...
...              This file provides two primary modes:
...              - Dev Mode (default): Keep containers running
...              - Prod Mode: Stop containers and remove volumes (CI/CD)
...
...              Control via environment variable:
...              - TEST_MODE: 'dev' (default, no cleanup) or 'prod' (full cleanup)
...
...              Quick usage:
...              - robot tests/                    # Dev mode (keep containers)
...              - TEST_MODE=prod robot tests/     # Prod mode (cleanup)
Library          RequestsLibrary
Library          OperatingSystem
Library          Process
Variables        test_env.py
Resource         ../resources/queue_keywords.robot
Resource         setup_keywords.robot



*** Keywords ***

Suite Teardown
    [Documentation]    Conditional teardown based on TEST_MODE
    ...                DEFAULT: dev mode (keep containers running)
    ...                TEST_MODE=prod for CI/CD (full cleanup)
    ${test_mode}=    Get Environment Variable    TEST_MODE    default=dev

    Run Keyword If    '${test_mode}' == 'prod'    Prod Mode Teardown
    ...    ELSE                                    Dev Mode Teardown

Dev Mode Teardown
    [Documentation]    Default dev mode - keep containers running for fast iteration
    Log To Console    \n=== Dev Mode Teardown (Default) ===
    Log To Console    ✓ Keeping containers running for next test run
    Log To Console    Tip: Use 'TEST_MODE=prod' for full cleanup or run manually:
    Log To Console         docker compose -f ${BACKEND_DIR}/docker-compose-ci.yml down -v

    # Only delete HTTP sessions
    Delete All Sessions

Prod Mode Teardown
    [Documentation]    Production/CI mode - complete cleanup of containers and volumes
    Log To Console    \n=== Prod Mode Teardown (CI/CD) ===
    Log To Console    Stopping containers and removing volumes...

    # Stop and remove containers with volumes
    Stop Docker Services    remove_volumes=${True}

    # Clean up any remaining volumes
    Run Process    rm    -rf    ${BACKEND_DIR}/data/test_mongo_data    shell=True
    Run Process    rm    -rf    ${BACKEND_DIR}/data/test_qdrant_data    shell=True
    Run Process    rm    -rf    ${BACKEND_DIR}/data/test_audio_chunks    shell=True

    # Delete all HTTP sessions
    Delete All Sessions

    Log To Console    ✓ Cleanup complete!

# # Legacy keywords for backward compatibility
# Full Cleanup
#     [Documentation]    DEPRECATED: Use 'Prod Mode Teardown' instead
#     Prod Mode Teardown

# Partial Cleanup
#     [Documentation]    DEPRECATED: Dev mode teardown keeps containers running
#     Log To Console    \n=== Partial Cleanup (Legacy) ===
#     Log To Console    Stopping containers (preserving volumes)...
#     Run Process    docker    compose    -f    backends/advanced/docker-compose-ci.yml    stop    shell=True
#     Delete All Sessions
#     Log To Console    Containers stopped, volumes preserved for next run

# No Cleanup
#     [Documentation]    DEPRECATED: Use 'Dev Mode Teardown' instead
#     Dev Mode Teardown

# Cleanup Test User
#     [Documentation]    Remove a specific test user by email (for test teardown)
#     [Arguments]    ${user_email}

#     TRY
#         # Try to find and delete the user
#         Create API Session    cleanup_session
#         ${response}=    GET On Session    cleanup_session    /api/users    expected_status=any

#         IF    ${response.status_code} == 200
#             ${users}=    Set Variable    ${response.json()}
#             FOR    ${user}    IN    @{users}
#                 IF    "${user}[email]" == "${user_email}"
#                     ${response}=    DELETE On Session    cleanup_session    /api/users/${user}[id]    expected_status=any
#                     Log    Deleted test user: ${user_email}    INFO
#                     RETURN
#                 END
#             END
#             Log    Test user not found: ${user_email}    INFO
#         END
#     EXCEPT    AS    ${error}
#         Log    Failed to cleanup test user ${user_email}: ${error}    WARN
#     END

# Cleanup Test Users
#     [Documentation]    Remove multiple test users by email pattern
#     [Arguments]    @{user_emails}

#     FOR    ${email}    IN    @{user_emails}
#         Cleanup Test User    ${email}
#     END

# Emergency Cleanup
#     [Documentation]    Force cleanup when normal teardown fails
#     Log To Console    \n=== Emergency Cleanup ===

#     # Force remove containers
#     Run Process    docker    compose    -f    backends/advanced/docker-compose-ci.yml    kill    shell=True
#     Run Process    docker    compose    -f    backends/advanced/docker-compose-ci.yml    rm    -f    -v    shell=True

#     # Remove all test data
#     Run Process    rm    -rf    backends/advanced/data/test_*    shell=True

#     # Clean up orphaned volumes
#     Run Process    docker    volume    prune    -f    shell=True

#     Log To Console    Emergency cleanup complete

# Cleanup Speaker Recognition Service
#     [Documentation]    Teardown speaker recognition service
#     ${test_mode}=    Get Environment Variable    TEST_MODE    default=dev

#     IF    '${test_mode}' == 'prod'
#         Log To Console    Stopping speaker recognition service...
#         Stop Speaker Recognition Service    remove_volumes=${True}
#     ELSE
#         Log To Console    Skipping speaker recognition cleanup (dev mode)
#     END

