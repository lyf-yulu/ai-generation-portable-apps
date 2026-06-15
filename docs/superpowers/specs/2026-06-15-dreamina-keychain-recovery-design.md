# Dreamina Keychain Recovery Design

## Context

The Dreamina app currently supports multiple saved accounts by assigning each account an isolated HOME directory under `dreamina/accounts/<account_id>`. On macOS, those account homes include project-created keychains at:

`dreamina/accounts/<account_id>/Library/Keychains/login.keychain-db`

This used to work for the user's Dreamina accounts. On 2026-06-15, every saved Dreamina account began reporting offline. Direct checks showed:

- The normal macOS user HOME still has a valid Dreamina login.
- At least one saved account can return `dreamina user_credit` after its project-created keychain becomes accessible.
- Saved account checks can hang while Dreamina CLI or `security` tries to access account keychain state.
- The user keychain search list contains repeated project account keychain entries and stale test account entries.

The user chose to keep the current multi-HOME/keychain account model and repair it, because it worked before the current failure.

## Goals

- Recover the existing saved Dreamina accounts when their local OAuth credentials are still valid.
- Keep the current "one project account HOME per Dreamina account" model.
- Prevent one stuck account check from hanging account refresh, task dispatch, or the Dreamina backend.
- Report precise account failure reasons instead of only "offline".
- Avoid requiring the user to know or type any macOS Keychain password.
- Avoid destructive or silent changes to the user's system login keychain.
- Preserve the shared system account fallback so Dreamina remains usable even if all saved accounts need repair.

## Non-Goals

- Do not bypass Dreamina account restrictions, expiry, risk controls, or permission failures.
- Do not move to one macOS user per Dreamina account in this change.
- Do not delete account directories or keychain files automatically.
- Do not clean global macOS keychain search-list pollution without explicit user action.
- Do not promise an account is usable unless `dreamina user_credit` succeeds for that account runtime.

## Architecture

Add a small runtime layer around all Dreamina CLI calls for saved accounts.

`DreaminaAccountRuntime` owns account-local command execution, account preflight, recovery attempts for project-created keychains, timeout handling, and normalized error classification. Existing handlers should call this runtime instead of calling `run_cmd(..., env_override=get_account_env(...))` directly for account operations.

The runtime only operates on account homes under `dreamina/accounts`. It may inspect and unlock the account's project-created keychain with the empty password that the app originally used when creating it. It must not touch `/Users/<user>/Library/Keychains/login.keychain-db` except for reading normal Dreamina status through the existing system account path.

## Components

### Account Runtime

Responsibilities:

- Build the environment for an account.
- Run `dreamina` commands with hard timeouts.
- Kill command process groups after timeout.
- Return a structured result with stdout, stderr, return code, duration, timeout flag, and failure category.
- Never allow a stuck `dreamina` or `security` process to outlive the operation.

Failure categories:

- `ok`: command succeeded and output parsed.
- `timeout`: command did not finish before the deadline.
- `missing_home`: account home is missing.
- `missing_keychain`: expected project keychain is missing on macOS.
- `keychain_recovery_failed`: project keychain exists but cannot be made accessible.
- `not_logged_in`: CLI reports no local login or missing keyring secret.
- `cli_error`: CLI returned a non-zero error that is not recognized.
- `parse_error`: CLI returned success but output was not valid JSON where JSON was expected.

### Keychain Preflight

For each saved account on macOS:

1. Confirm the account home path is inside `dreamina/accounts`.
2. Confirm `Library/Keychains/login.keychain-db` exists.
3. Attempt a bounded `security unlock-keychain -p "" <account_keychain>`.
4. Attempt a bounded `security set-keychain-settings <account_keychain>`.
5. Ensure the account keychain appears once in the user keychain search list.
6. Do not remove stale entries automatically during normal preflight.

If any `security` command hangs or fails, the runtime records the exact step and reports `keychain_recovery_failed`.

### Search List Hygiene

Normal account operations may deduplicate entries when writing the search list for known existing account keychains. They must not silently remove stale paths outside the project's current account directories.

Add a separate admin-only diagnostic endpoint or script to show:

- duplicate keychain search-list entries,
- keychain paths under deleted account folders,
- keychain paths under old test folders,
- whether cleanup is recommended.

Cleanup must be a separate explicit action.

### Account Health Check

Add a health check flow for saved accounts:

1. Run preflight.
2. Run `dreamina user_credit`.
3. Parse user ID, VIP level, and total credit.
4. Mark account online only if the command succeeds and parses.
5. Store `last_check_at`, `last_ok_at`, `last_error_code`, and `last_error_detail`.

The check should be runnable for one account or all accounts. Checking all accounts must process them sequentially or with a low bounded parallelism so multiple Keychain prompts or hangs do not overload the machine.

### Task Dispatch

Task dispatch may use saved accounts only when:

- `logged_in` is true,
- `_login_verified_at` is fresh,
- `last_error_code` is empty or `ok`,
- the account is not currently marked as quarantined.

If all saved accounts fail health check, dispatch falls back to the shared system account when it is logged in. If the shared system account is unavailable too, the UI should show a clear setup error.

### UI

Replace ambiguous account state text with explicit statuses:

- Online
- Checking
- Needs repair
- Needs login
- Timed out
- CLI error

For each account show:

- account name,
- user ID when known,
- credit when known,
- last successful check time,
- last error category and short detail,
- actions: Check, Repair, Login, Logout, Delete.

The repair action should run the preflight and health check. It should not delete credentials.

## Data Model

Extend account records in `dreamina/state/accounts.json`:

```json
{
  "last_check_at": 1781487000.0,
  "last_ok_at": 1781487000.0,
  "last_error_code": null,
  "last_error_detail": null,
  "repair_attempted_at": 1781487000.0,
  "quarantined": false
}
```

Legacy account records without these fields remain valid. Missing fields mean "unknown".

## Safety Rules

- Only project account homes under `dreamina/accounts` can be repaired.
- Do not ask the user for a Keychain password.
- Do not log tokens, cookies, full OAuth secrets, or full keychain command output.
- Do not delete project account directories during repair.
- Do not remove search-list entries unless the user runs the explicit cleanup action.
- Use hard timeouts for every `security` and `dreamina` subprocess.

## Verification

Automated tests:

- Account preflight classifies missing home.
- Account preflight classifies missing keychain.
- Account preflight times out cleanly when `security` hangs.
- Account health check marks an account online when `dreamina user_credit` returns valid JSON.
- Account health check records `timeout` when `dreamina user_credit` hangs.
- Dispatch ignores stale or errored accounts and falls back to the shared system account.
- Search-list update deduplicates known project account keychain entries without removing unrelated paths.

Manual verification:

- Run all Dreamina account health checks from the UI.
- Confirm each saved account either returns user ID and credit or a specific reason.
- Submit a small Dreamina task through a recovered saved account.
- Restart the app and confirm recovered account state remains visible.
- Confirm Windows LAN clients can still submit through the portal without local Dreamina setup.

## Success Criteria

- A stuck saved account no longer hangs the backend.
- Existing valid Dreamina accounts can be repaired and used again.
- Every saved account has a clear online or actionable failure state.
- The shared system account still works as fallback.
- The app avoids silent destructive changes to macOS Keychain state.
