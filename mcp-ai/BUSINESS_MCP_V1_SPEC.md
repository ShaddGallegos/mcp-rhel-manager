# Business MCP v1 Implementation Spec

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

## 1) Objective

Turn HAL into a business execution copilot that converts account data + technical findings into repeatable business outcomes:

- Better account planning quality
- Faster seller/architect follow-through
- Measurable pipeline impact

## 2) Scope (v1)

In scope:

- Account 360 record model and storage
- Business workflow commands in HAL
- Opportunity scoring and prioritization
- Business-value translation from diagnostics/remediation
- Weekly KPI summary

Out of scope (v2+):

- Full CRM bidirectional sync
- UI-heavy dashboard redesign
- Multi-tenant RBAC policies across organizations

## 3) Data Model

Use JSON files in `~/.mcp-ai/training/` with explicit type tags and freshness metadata.

### 3.1 Account 360 Record

File pattern: `acct-<account_slug>-<timestamp>.json`
Type: `business_account_360`

Example:

```json
{
  "type": "business_account_360",
  "account_name": "Centene",
  "account_slug": "centene",
  "industry": "Healthcare",
  "regions": ["US"],
  "strategic_priorities": [
    "Cost optimization",
    "Cloud modernization",
    "Security posture"
  ],
  "initiatives": [
    {
      "name": "Claims platform modernization",
      "status": "active",
      "timeline": "2026-H2"
    }
  ],
  "stakeholders": [
    {
      "name": "Jane Doe",
      "title": "CIO",
      "influence": "high",
      "relationship_strength": 3
    }
  ],
  "contract": {
    "current_state": "renewal-window",
    "renewal_date": "2026-11-30"
  },
  "risk_flags": [
    "Competing modernization vendor",
    "Budget review in Q3"
  ],
  "evidence": [
    {
      "source": "meeting_notes",
      "source_ref": "interaction-2026-04-27-101022.json",
      "captured_at": "2026-04-27T10:10:22Z"
    }
  ],
  "freshness": {
    "updated_at": "2026-04-28T13:00:00Z",
    "days_old": 0,
    "confidence": 0.82
  }
}
```

### 3.2 Opportunity Record

File pattern: `opp-<account_slug>-<id>.json`
Type: `business_opportunity`

Fields:

- `account_slug`
- `problem_statement`
- `value_hypothesis`
- `estimated_impact`
- `decision_timeline`
- `buying_committee`
- `technical_fit`
- `competitive_pressure`
- `score` (computed)
- `next_actions[]`

### 3.3 Business Action Record

File pattern: `action-<account_slug>-<timestamp>.json`
Type: `business_action_item`

Fields:

- `owner`
- `due_date`
- `action`
- `status` (`open|in_progress|done|blocked`)
- `source_interaction`

## 4) HAL Command Surface (v1)

Add business commands to `hal.py`:

- `HAL business account-brief --account <name>`
- `HAL business strategy --account <name> --horizon 30|60|90`
- `HAL business score-opps --account <name>`
- `HAL business next-actions --account <name>`
- `HAL business weekly-summary`

Output format requirements:

- Must include explicit sections (summary, assumptions, evidence, recommended next actions)
- Must include confidence and freshness indicators
- Must cite local evidence records used

## 5) Opportunity Scoring Model

Weighted score from 0 to 100:

$Score = 100 * (0.30P + 0.20B + 0.15T + 0.20F + 0.15C)$

Where each variable is normalized to $[0,1]$:

- $P$: pain severity
- $B$: budget likelihood
- $T$: decision timeline urgency
- $F$: technical fit
- $C$: competitive pressure

Scoring rubric (v1):

- Use deterministic rules first (from known fields)
- Use LLM to suggest missing values with confidence labels
- Never auto-promote inferred values to hard facts without marking `inferred: true`

## 6) Business Value Translation Layer

Create helper function in `hal.py` (or `mcp-ai/collector.py`) to map technical findings to business statements:

Input:

- diagnostics JSON
- remediation actions

Output:

- `risk_reduction`
- `downtime_avoidance_estimate`
- `compliance_impact`
- `ops_efficiency_impact`

Example statement:

- "Enabling auditd baseline controls reduces audit-readiness risk and lowers manual evidence prep effort for compliance reviews."

## 7) Runtime Flow

1. User asks business question (account strategy/brief).
2. HAL performs RAG retrieval across:
   - `business_account_360`
   - `business_intel_account`
   - `hal_interaction`
   - supplemental documents
3. HAL computes opportunity scores.
4. HAL generates structured response + citations.
5. HAL writes action items and summary records to training store.

## 8) KPIs (weekly)

Persist `kpi-week-<ISO_WEEK>.json` with:

- `accounts_with_fresh_360`
- `opportunities_created`
- `opportunities_high_score` (score >= 75)
- `actions_open`
- `actions_completed`
- `hal_assisted_interactions`
- `avg_response_with_evidence_ratio`

## 9) Governance and Safety

- Add `source_trace` for every business response
- Add `confidence` in each recommendation
- Enforce no external sharing mode unless `--export` is explicitly passed
- Redact sensitive fields in generated summaries when `HAL_REDACT=1`

## 10) Delivery Plan (2 Weeks)

### Week 1

- Implement data schemas + file IO helpers
- Add `account-brief` and `strategy` commands
- Add scoring engine (deterministic rules)
- Add evidence citation formatter

### Week 2

- Add `next-actions` and `weekly-summary`
- Add KPI generation job (daily cron or manual command)
- Add value-translation layer for diagnostics/remediation
- Run UAT with 2 accounts (one healthcare, one enterprise)

## 11) Acceptance Criteria

- `HAL business account-brief --account centene` returns structured brief with at least 3 evidence citations
- `HAL business score-opps --account centene` outputs at least one ranked opportunity and score explanation
- `HAL business weekly-summary` produces KPI JSON and readable report
- Offline mode still returns account strategy from local data when bridge is down

## 12) Suggested First Code Changes

1. Add new business command parser branch in `hal.py` `main()`.
2. Add `load_account_360()`, `save_opportunity()`, `compute_opp_score()` helpers.
3. Reuse existing `search_training_data_for_rag()` and extend record handling for `business_account_360`.
4. Add tests in `mcp-ai/tests/`:
   - `test_business_scoring.py`
   - `test_account_brief_output.py`

## 13) Minimal Backward-Compatible Strategy

- Do not change existing record types.
- Introduce new types additively.
- Gate all new command paths under `business` namespace.
- Keep existing HAL query behavior unchanged outside `business` commands.
