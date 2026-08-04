# HAL Integration Roadmap

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

Date: 2026-05-01

## Ordered Execution Status

1. Real-time stock API integration: IMPLEMENTED

- Added live quote lookup from Yahoo Finance endpoint in HAL query flow.
- Added optional multi-month history summary using Stooq daily history endpoint.
- Improved stock intent detection to catch prompts such as "IBM stock over the past 6 months".
- Added ticker extraction from natural-language prompts when account matching is unavailable.

1. Data freshness and cache invalidation: IMPLEMENTED

- Added account-aware cache freshness checks against newest matching training data timestamp.
- Intel cache now invalidates automatically when source account data is newer than cached report.
- Business intel import now clears stale intel cache files after successful import.
- Supplemental section imports use centralized cache invalidation helper.
- Cached responses now display age and generated timestamp in report notes.

1. Notification activation: IMPLEMENTED

- Added CLI switch: --notify for intel report notifications.
- Added CLI switch: --notify-slack-test for direct Slack integration validation.
- Intel report generation now can emit notifications through configured channels.
- Notification severity is elevated when many integration signals are detected.

1. Red Hat Insights integration: IMPLEMENTED

- Added CLI switch: --insights-status for Insights local and API status summary.
- Added CLI switch: --insights-checkin to perform check-in after status query.
- Added natural-language query route for Insights status and health prompts.
- Added optional cloud API summary when RH_INSIGHTS_API_TOKEN is configured.

1. Implementation roadmap artifact: IMPLEMENTED

- This file is the executable plan and completion record.

## Validation Checklist

- Stock path:
- Run: hal "IBM stock over the past 6 months"
- Expect live quote and (when available) 6-month range/change summary.

- Cache invalidation:
- Import business intel: hal --import-business-intel PATH_TO_INTEL
- Generate report: hal --intel-report-all "Centene" --signals-only
- Modify or import updated account data and re-run report.
- Expect report not to serve stale cached content.

- Notifications:
- Configure Slack webhook in ~/.mcp-ai/hal-setup.json
- Run: hal --notify-slack-test
- Run: hal --intel-report-all "Centene" --signals-only --notify

- Red Hat Insights:
- Run: hal --insights-status
- Optional check-in: hal --insights-checkin
- Optional API mode: export RH_INSIGHTS_API_TOKEN=YOUR_TOKEN then re-run status command.

## Environment Variables

- HAL_NOTIFY_ON_INTEL=1
- Enables automatic notifications for intel report generation without passing --notify.

- RH_INSIGHTS_API_TOKEN=YOUR_TOKEN
- Enables Insights cloud API host summary in --insights-status output.

## Next Hardening Iteration

- Add retry/backoff and local rate limiting for external stock API calls.
- Add structured telemetry for notification delivery outcomes.
- Add integration health summary command combining bridge, notifications, and insights status.
- Add unit tests for stock intent routing, cache freshness decisions, and notification triggers.
