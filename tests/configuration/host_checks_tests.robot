*** Settings ***
Documentation    Host-level health checks: container DNS, Tailscale state, TLS
...              certificates, and stale socket mounts.
...
...              These faults leave every container reporting healthy while the
...              deployment is unusable from outside — Mongo and Redis are
...              container-local, so they stay green throughout. Nothing else in
...              the suite covers them.
...
...              Container-independent by design. Each check reports
...              not_applicable when the thing it probes is absent, so this suite
...              is honest in CI (nothing configured) and on a real deployment
...              (everything configured) without being skipped in either.

Library          Collections
Library          ../libs/HostChecksHelper.py

Test Tags        infra


*** Test Cases ***
Host Checks Report No Failures
    [Documentation]    The headline assertion: nothing on this host is broken.
    ...                Passes in CI because everything is not_applicable there,
    ...                and on a deployment because everything is ok.

    ${failures}=    Get Failing Checks

    Should Be Empty    ${failures}
    ...    Host checks reported failures: ${failures}

Every Registered Check Produces A Result
    [Documentation]    Guards against a check being added to the registry but
    ...                silently throwing, which run_all_checks converts into a
    ...                not_applicable result rather than an error.

    ${results}=    Run Host Checks
    ${expected}=    Get Registered Check Count

    Length Should Be    ${results}    ${expected}
    ...    Every registered check should return exactly one result

Check Results Are Well Formed
    [Documentation]    The node agent serves these over /checks and the WebUI
    ...                renders them, so the shape is a contract.

    ${results}=    Run Host Checks
    ${valid}=    Get Valid Statuses

    FOR    ${check}    IN    @{results}
        Should Not Be Empty    ${check}[id]        Every check needs an id
        Should Not Be Empty    ${check}[title]     Every check needs a title
        Should Contain    ${valid}    ${check}[status]
        ...    Unexpected status '${check}[status]' from check '${check}[id]'
        Should Be True    isinstance($check['repairable'], bool)
        ...    repairable must be a bool so the payload stays JSON-safe
    END

Check Identifiers Are Unique
    [Documentation]    Ids key the watchdog's per-check failure counters and the
    ...                system-event incident keys, so a duplicate would make two
    ...                checks share repair state and resolve each other.

    ${ids}=    Get Check Ids
    ${unique}=    Remove Duplicates    ${ids}

    Length Should Be    ${ids}    ${{ len($unique) }}
    ...    Duplicate check ids found in registry: ${ids}
