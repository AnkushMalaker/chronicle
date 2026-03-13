*** Settings ***
Documentation    API session creation and authentication management keywords
...
...              This file contains keywords for API session management and authentication.
...              Keywords in this file should handle session creation, authentication workflows,
...              token management, and session cleanup.
...
...              Examples of keywords that belong here:
...              - API session creation and management
...              - Authentication workflows
...              - Token extraction (when needed for external tools)
...              - Session validation and cleanup
...
...              Keywords that should NOT be in this file:
...              - Verification/assertion keywords (belong in tests)
...              - User management operations (belong in user_keywords.robot)
...              - Docker service management (belong in setup_env_keywords.robot)
Library          RequestsLibrary
Library          Collections
Variables        ../setup/test_env.py

*** Keywords ***

# Core Session Creation
Create API Session
    [Documentation]    Create an API session (authenticated or anonymous)
    [Arguments]        ${session_name}    ${email}=${ADMIN_EMAIL}    ${password}=${ADMIN_PASSWORD}  ${base_url}=${API_URL}

    # Create base session
    Create Session    ${session_name}    ${base_url}    verify=True


    ${token}=    Get Authentication Token    ${session_name}    ${email}    ${password}
     &{headers}=    Create Dictionary    Authorization=Bearer ${token}
    # Update session with auth headers
    Create Session    ${session_name}    ${base_url}    verify=True    headers=${headers}
    Set Suite Variable    ${session_name}

Get Anonymous Session
    [Documentation]    Get an unauthenticated API session
    [Arguments]    ${session_name}    ${base_url}=${API_URL}

    Create Session    ${session_name}    ${base_url}    verify=True

Get Admin API Session
    [Documentation]    Get an authenticated admin API session (creates if doesn't exist)
    [Arguments]    ${session_name}=api

    # Try to use existing session, create if needed
    ${session_exists}=    Run Keyword And Return Status    GET On Session    ${session_name}    /health    expected_status=any
    IF    not ${session_exists}
        Create API Session    ${session_name}
    END

    RETURN    ${session_name}


# Core Authentication
Get Authentication Token
    [Documentation]    Get authentication token for any user from existing session
    [Arguments]    ${session_alias}    ${email}    ${password}

    &{auth_data}=    Create Dictionary    username=${email}    password=${password}
    &{headers}=      Create Dictionary    Content-Type=application/x-www-form-urlencoded

    ${response}=    POST On Session    ${session_alias}    /auth/jwt/login    data=${auth_data}    headers=${headers}    expected_status=200

    ${json_response}=    Set Variable    ${response.json()}
    ${token}=    Get From Dictionary    ${json_response}    access_token
    RETURN    ${token}


# Override RequestsLibrary Keywords with Enhanced Error Messages
POST On Session
    [Documentation]    Override POST On Session to show response body on errors
    [Arguments]    ${alias}    ${uri}    &{kwargs}

    # Extract expected_status if provided, default to 200
    ${expected_status}=    Get From Dictionary    ${kwargs}    expected_status    200

    # Remove expected_status from kwargs and set to 'anything'
    Remove From Dictionary    ${kwargs}    expected_status

    # Call original keyword with expected_status=anything
    ${response}=    RequestsLibrary.POST On Session    ${alias}    ${uri}    expected_status=anything    &{kwargs}

    # Validate status and provide detailed error if mismatch
    IF    '${expected_status}' != 'anything' and '${expected_status}' != 'any'
        ${status_matches}=    Evaluate    ${response.status_code} == ${expected_status}
        IF    not ${status_matches}
            Fail    POST ${uri} returned ${response.status_code} (expected ${expected_status}).\nResponse: ${response.text}
        END
    END

    RETURN    ${response}

GET On Session
    [Documentation]    Override GET On Session to show response body on errors
    [Arguments]    ${alias}    ${uri}    &{kwargs}

    # Extract expected_status if provided, default to 200
    ${expected_status}=    Get From Dictionary    ${kwargs}    expected_status    200

    # Remove expected_status from kwargs and set to 'anything'
    Remove From Dictionary    ${kwargs}    expected_status

    # Call original keyword with expected_status=anything
    ${response}=    RequestsLibrary.GET On Session    ${alias}    ${uri}    expected_status=anything    &{kwargs}

    # Validate status and provide detailed error if mismatch
    IF    '${expected_status}' != 'anything' and '${expected_status}' != 'any'
        ${status_matches}=    Evaluate    ${response.status_code} == ${expected_status}
        IF    not ${status_matches}
            Fail    GET ${uri} returned ${response.status_code} (expected ${expected_status}).\nResponse: ${response.text}
        END
    END

    RETURN    ${response}

PUT On Session
    [Documentation]    Override PUT On Session to show response body on errors
    [Arguments]    ${alias}    ${uri}    &{kwargs}

    # Extract expected_status if provided, default to 200
    ${expected_status}=    Get From Dictionary    ${kwargs}    expected_status    200

    # Remove expected_status from kwargs and set to 'anything'
    Remove From Dictionary    ${kwargs}    expected_status

    # Call original keyword with expected_status=anything
    ${response}=    RequestsLibrary.PUT On Session    ${alias}    ${uri}    expected_status=anything    &{kwargs}

    # Validate status and provide detailed error if mismatch
    IF    '${expected_status}' != 'anything' and '${expected_status}' != 'any'
        ${status_matches}=    Evaluate    ${response.status_code} == ${expected_status}
        IF    not ${status_matches}
            Fail    PUT ${uri} returned ${response.status_code} (expected ${expected_status}).\nResponse: ${response.text}
        END
    END

    RETURN    ${response}

DELETE On Session
    [Documentation]    Override DELETE On Session to show response body on errors
    [Arguments]    ${alias}    ${uri}    &{kwargs}

    # Extract expected_status if provided, default to 200
    ${expected_status}=    Get From Dictionary    ${kwargs}    expected_status    200

    # Remove expected_status from kwargs and set to 'anything'
    Remove From Dictionary    ${kwargs}    expected_status

    # Call original keyword with expected_status=anything
    ${response}=    RequestsLibrary.DELETE On Session    ${alias}    ${uri}    expected_status=anything    &{kwargs}

    # Validate status and provide detailed error if mismatch
    IF    '${expected_status}' != 'anything' and '${expected_status}' != 'any'
        ${status_matches}=    Evaluate    ${response.status_code} == ${expected_status}
        IF    not ${status_matches}
            Fail    DELETE ${uri} returned ${response.status_code} (expected ${expected_status}).\nResponse: ${response.text}
        END
    END

    RETURN    ${response}

Get Random ID
    [Documentation]    Generate a unique random ID for test data (call each time for new ID)
    ${random_id}=    Generate Random String    8    [LETTERS][NUMBERS]
    RETURN    ${random_id}
