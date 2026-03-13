*** Settings ***
Documentation    Authentication and User Management API Tests
Library          RequestsLibrary
Library          Collections
Library          String
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/user_keywords.robot
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup

*** Variables ***
# Test users are now imported from test_env.py via resource files

*** Test Cases ***

Login With Valid Credentials Test
    [Documentation]    Test successful login with admin credentials
    [Tags]    permissions
    ${user}=           Get User Details    api
    Should Be Equal    ${user}[email]    ${ADMIN_EMAIL}

Login With Invalid Credentials Test
    [Documentation]    Test login failure with invalid credentials
    [Tags]    permissions
    Get Anonymous Session    anon_session

    &{auth_data}=    Create Dictionary    username=${ADMIN_EMAIL}    password=wrong-password
    &{headers}=      Create Dictionary    Content-Type=application/x-www-form-urlencoded

    ${response}=    POST On Session    anon_session    /auth/jwt/login    data=${auth_data}    headers=${headers}    expected_status=400
    Should Be Equal As Integers    ${response.status_code}    400

Get Current User Test
    [Documentation]    Test getting current authenticated user
    [Tags]    permissions

    ${user}=           Get User Details    api

    Dictionary Should Contain Key    ${user}    email
    Dictionary Should Contain Key    ${user}    id
    Should Be Equal       ${user}[email]    ${ADMIN_EMAIL}

Unauthorized Access Test
    [Documentation]    Test that endpoints require authentication
    [Tags]    permissions
    Get Anonymous Session    anon_session

    # Try to access protected endpoint without token
    ${response}=    GET On Session   anon_session   /users/me    expected_status=401
    Should Be Equal As Integers    ${response.status_code}    401

Create User Test
    [Documentation]    Test creating a new user (admin only)
    [Tags]    permissions

    ${user}=           Create Test User    api

    Should Contain    ${user}[email]    test-user-

    # Cleanup
    [Teardown]    Delete User    api    ${user}[id]

Get All Users Test
    [Documentation]    Test getting all users (admin only)
    [Tags]    permissions

    ${response}=       GET On Session    api    /api/users

    Should Be Equal As Integers    ${response.status_code}    200
    Should Be True     isinstance($response.json(), list)

    # Should contain at least the admin user
    ${users}=    Set Variable    ${response.json()}
    ${admin_found}=    Set Variable    ${False}
    FOR    ${user}    IN    @{users}
        IF    '${user}[email]' == '${ADMIN_EMAIL}'
            ${admin_found}=    Set Variable    ${True}
        END
    END
    Should Be True    ${admin_found}

Non-Admin User Cannot Create Users Test
    [Documentation]    Test that non-admin users cannot create users
    [Tags]    permissions
    # Create a non-admin user
    ${test_user}=       Create Test User    api
    Create API Session    user_session    email=${test_user}[email]    password=${TEST_USER_PASSWORD}

    &{user_data}=   Create Dictionary    email=another-user@example.com    password=${TEST_USER_PASSWORD}    is_superuser=${False}
    ${response}=    POST On Session    user_session    /api/users    json=${user_data}    expected_status=403
    Should Be Equal As Integers    ${response.status_code}    403

    [Teardown]    Delete User    api    ${test_user}[id]

Update User Test
    [Documentation]    Test updating a user password (admin only)
    [Tags]    permissions

    ${test_user}=       Create Test User    api
    ${new_password}=    Set Variable    new-test-password-456

    # Update user password
    &{update_data}=    Create Dictionary    email=${test_user}[email]    password=${new_password}

    ${response}=       PUT On Session    api    /api/users/${test_user}[id]    json=${update_data}
    Should Be Equal As Integers    ${response.status_code}    200

    # Verify old password no longer works
    Get Anonymous Session    anon_session
    &{old_auth}=       Create Dictionary    username=${test_user}[email]    password=${TEST_USER_PASSWORD}
    &{headers}=        Create Dictionary    Content-Type=application/x-www-form-urlencoded
    ${old_response}=   POST On Session    anon_session    /auth/jwt/login    data=${old_auth}    headers=${headers}    expected_status=400
    Should Be Equal As Integers    ${old_response.status_code}    400

    # Verify new password works
    &{new_auth}=       Create Dictionary    username=${test_user}[email]    password=${new_password}
    ${new_response}=   POST On Session    anon_session    /auth/jwt/login    data=${new_auth}    headers=${headers}    expected_status=200
    Should Be Equal As Integers    ${new_response.status_code}    200

    [Teardown]    Delete User    api    ${test_user}[id]

Delete User Test
    [Documentation]    Test deleting a user (admin only)
    [Tags]    permissions

    ${user}=           Create Test User    api

    # Delete the user
    Delete User    api    ${user}[id]

    # Verify user is deleted by trying to login
    &{auth_data}=      Create Dictionary    username=${user}[email]    password=${TEST_USER_PASSWORD}
    &{headers}=        Create Dictionary    Content-Type=application/x-www-form-urlencoded

    ${response}=       POST On Session    api    /auth/jwt/login    data=${auth_data}    headers=${headers}    expected_status=400
    Should Be Equal As Integers    ${response.status_code}    400
