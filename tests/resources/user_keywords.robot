*** Settings ***
Documentation    User account management and lifecycle keywords
...
...              This file contains keywords for user account creation, deletion, and management.
...              Keywords in this file should handle user-related operations, user account lifecycle,
...              and user permission management.
...
...              Examples of keywords that belong here:
...              - User account creation and deletion
...              - User management operations
...              - User permission validation
...              - User account lifecycle operations
...
...              Keywords that should NOT be in this file:
...              - Verification/assertion keywords (belong in tests)
...              - API session management (belong in session_keywords.robot)
...              - Docker service management (belong in setup_env_keywords.robot)
Library          RequestsLibrary
Library          Collections
Library          String
Variables        ../setup/test_env.py
Resource         session_keywords.robot

*** Keywords ***

Create Test User
    [Documentation]    Create a test user for testing. Defaults to random email if not specified.
    [Arguments]    ${session}    ${email}=${EMPTY}    ${password}=${TEST_USER_PASSWORD}    ${is_superuser}=False

    # Generate random email if not provided
    IF    '${email}' == '${EMPTY}'
        ${random_id}=    Generate Random String    8    [LETTERS][NUMBERS]
        ${email}=        Set Variable    test-user-${random_id}@example.com
    END

    &{user_data}=   Create Dictionary    email=${email}    password=${password}    is_superuser=${is_superuser}
    ${response}=    POST On Session    ${session}    /api/users    json=${user_data}    expected_status=201

    RETURN    ${response.json()}

Delete User
    [Documentation]    Delete a user by ID
    [Arguments]    ${session_alias}    ${user_id}

    ${response}=    DELETE On Session    ${session_alias}    /api/users/${user_id}    expected_status=200
    RETURN    ${response.json()}

Get User Details
    [Documentation]    Get user details by ID, or current user if user_id is 'me' (default)
    [Arguments]    ${session_alias}    ${user_id}=me

    # /users/me is at root level (fastapi-users), /api/users/{id} is for admin user management
    IF    '${user_id}' == 'me'
        ${response}=    GET On Session    ${session_alias}    /users/me    expected_status=200
    ELSE
        ${response}=    GET On Session    ${session_alias}    /api/users/${user_id}    expected_status=200
    END
    RETURN    ${response.json()}

Get Admin User Details With Token
    [Documentation]    Get current admin user details using token (legacy compatibility)
    [Arguments]    ${token}

    Create Session    temp_user_session    ${API_URL}    verify=True
    &{headers}=    Create Dictionary    Authorization=Bearer ${token}
    ${response}=    GET On Session    temp_user_session    /users/me    headers=${headers}    expected_status=200
    ${user}=        Set Variable    ${response.json()}
    Delete All Sessions
    RETURN    ${user}

List All Users
    [Documentation]    List all users (admin only)
    [Arguments]    ${session}

    ${response}=    GET On Session    ${session}    /api/users    expected_status=200
    RETURN    ${response.json()}

Update User
    [Documentation]    Update user details
    [Arguments]    ${session}    ${user_id}    &{updates}

    ${response}=    PUT On Session    ${session}    /api/users/${user_id}    json=${updates}    expected_status=200
    RETURN    ${response.json()}
