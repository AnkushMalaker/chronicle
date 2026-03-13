*** Settings ***
Documentation    Browser Authentication Tests
Library          Browser
Resource            ../setup/setup_keywords.robot
Suite Setup      Suite Setup



*** Variables ***



*** Test Cases ***
Test Browser Can Access Login Page
    [Documentation]    Test that the browser can access the login page

    Log    Testing browser access to login page    INFO

    # Use the backend URL from test.env
    ${backend_url}=    Set Variable    ${WEB_URL}

    Open Browser    ${backend_url}/login    chromium
    Wait For Elements State    id=email    visible    timeout=10s
    Fill Text    id=email    ${ADMIN_EMAIL}
    Fill Text    id=password    ${ADMIN_PASSWORD}
    Click   button[type="submit"]
    # Verify that we are logged in by checking for the presence of the dashboard
    Get Element    text=Chronicle Dashboard
    Log    Successfully accessed login page and logged in    INFO


    Close Browser
