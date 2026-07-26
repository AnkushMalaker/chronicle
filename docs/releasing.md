# Releasing Chronicle

A Chronicle release is a published, non-prerelease GitHub release with a semantic
version tag such as `v0.3.1`. Publishing it has two user-visible effects:

- `install.sh` resolves GitHub's latest release and clones that exact tag.
- `advanced-docker-compose-build.yml` builds the tagged backend, ASR, and speaker
  images, pushes both the version tag and `latest`, and appends the image names to
  the release notes.

The normal release target is `main`. A release may explicitly target `dev` when
requested, but that is an exception: it also makes the selected `dev` commit the
installer source and the source of the `latest` container images. Record the target
branch and commit in the release notes.

## Release checklist

Set the release version and candidate branch explicitly. Do not infer either from
the checked-out local branch.

```bash
RELEASE_VERSION=v0.3.1
RELEASE_BRANCH=main
REPOSITORY=SimpleOpenSoftware/chronicle

git fetch origin --prune --tags
git rev-parse "origin/${RELEASE_BRANCH}"
git tag --list "${RELEASE_VERSION}"
gh release view "${RELEASE_VERSION}" --repo "${REPOSITORY}"
```

The last two commands should find neither a local tag nor an existing GitHub
release. Keep the resolved commit SHA for later verification.

### 1. Review changes and write accurate notes

Preview GitHub's generated notes without creating a release:

```bash
gh api -X POST "repos/${REPOSITORY}/releases/generate-notes" \
  -f tag_name="${RELEASE_VERSION}" \
  -f target_commitish="${RELEASE_BRANCH}" \
  -f previous_tag_name="<previous-version>"
```

Add a short Highlights section above the generated pull-request list. Include
breaking changes and migrations when present. Include a Validation section with
links and exact outcomes; do not turn partial or failing CI into an “all tests
pass” claim.

### 2. Validate the candidate

At minimum, verify the latest candidate commit with:

- `Python Tests`
- `Robot Framework Tests (Full - With API Keys)`
- `Speaker Recognition Tests` when that service changed
- the appropriate frontend build/typecheck when a frontend changed

Manual full-suite example:

```bash
gh workflow run full-tests-with-api.yml \
  --repo "${REPOSITORY}" \
  --ref "${RELEASE_BRANCH}"
gh run watch <run-id> --repo "${REPOSITORY}" --exit-status
```

If a required check is red, fix it or disclose the exact failures in the notes and
get an explicit decision to release with them. A green unit-test workflow does not
imply that the Robot integration suite is green.

### 3. Build the iPhone release

The production iPhone path is TestFlight:

```bash
gh workflow run ios-testflight.yml \
  --repo "${REPOSITORY}" \
  --ref "${RELEASE_BRANCH}"
gh run watch <run-id> --repo "${REPOSITORY}" --exit-status
```

This uses the EAS `testflight` profile and submits the build to App Store Connect.
Success means the build and submit command completed; App Store Connect may still
show Apple processing afterward. The workflow uses the App Store Connect API key
stored in EAS; configure or rotate it with `eas credentials --platform ios`
before releasing.

If the EAS build succeeded but only the Apple submission failed, retry that build
without incrementing the build number or paying for another build:

```bash
gh workflow run ios-testflight.yml \
  --repo "${REPOSITORY}" \
  --ref "${RELEASE_BRANCH}" \
  -f build_id="<EAS-build-id>"
```

Do not use `ios-ipa-build.yml` as release evidence until its internal-distribution
credentials are configured. It currently requests an ad hoc/internal IPA, which is
different from the store-distributed TestFlight build.

### 4. Publish the GitHub release

Prepare the final notes in a temporary file, then publish:

```bash
gh release create "${RELEASE_VERSION}" \
  --repo "${REPOSITORY}" \
  --target "${RELEASE_BRANCH}" \
  --title "${RELEASE_VERSION}" \
  --notes-file /path/to/release-notes.md \
  --latest
```

This is the release point. It creates the version tag and triggers the Docker image
workflow through the `release: published` event.

### 5. Verify the release

Verify all of the following rather than stopping when workflows are merely queued:

```bash
gh release view "${RELEASE_VERSION}" \
  --repo "${REPOSITORY}" \
  --json tagName,targetCommitish,isDraft,isPrerelease,publishedAt,url,assets,body
git fetch origin --tags
git rev-parse "${RELEASE_VERSION}^{commit}"
gh run list --repo "${REPOSITORY}" --branch "${RELEASE_VERSION}" --limit 10
```

- The release is published, not a draft or prerelease, and is marked latest.
- The tag commit equals the candidate SHA recorded before release.
- The Docker workflow completed successfully for the release event.
- Release notes contain the Docker image section added by that workflow.
- Each versioned GHCR image exists. Do not rely only on the presence of `latest`.
- The TestFlight workflow completed successfully and its source SHA equals the
  release tag commit.
- The release URL and relevant validation/build URLs are included in the handoff.

## Current workflow map

| Workflow | Role in a release |
|---|---|
| `python-tests.yml` | Unit-test and coverage evidence |
| `full-tests-with-api.yml` | Full Robot integration evidence |
| `speaker-recognition-tests.yml` | Speaker-service integration evidence |
| `ios-testflight.yml` | Production iPhone build and TestFlight submission |
| `advanced-docker-compose-build.yml` | Versioned and `latest` GHCR images after publication |
| `ios-ipa-build.yml` | Internal IPA experiment; not a release gate |
| `android-apk-build.yml` | Standalone APK build; not integrated with semantic-version releases |
| `build-all-platforms.yml` | Legacy timestamped mobile prerelease path |
