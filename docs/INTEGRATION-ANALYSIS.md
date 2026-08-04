# HAL Integration Points Analysis

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

**Date:** May 1, 2026
**Status:** Review Required

## Current Integration Points ✓

### Tier 1: Core (Working)

- **MCP Bridge (port 1776)** — LLM request proxy
- **Ollama (port 11434)** — Local LLM model serving (6 models available)
- **Local Knowledge Base** — Training data, business intel, account records
- **Training Data Ingestion** — Documents, spreadsheets, URLs, PDFs
- **Git** — Version control for configuration
- **Remediation Engine** — Automated fix execution
- **Auto-ingest System** — Scheduled data refresh

### Tier 2: Partial/Incomplete

- **Stock Price Data** — Only from local intel records, NO real API integration
- **Slack Integration** — Setup only, notification dispatch ready (hal-notify.py)
- **PagerDuty Integration** — Setup only, not actively used
- **Grafana Integration** — Setup only, annotation capability exists
- **Prometheus Integration** — Setup only, no active queries
- **Email** — Mail command wrapper available

### Tier 3: Red Hat Services

- **Red Hat Insights** — Referenced but not integrated

---

## Missing/Needed Integration Points ✗

### Critical Gaps

#### 1. **Real-Time Stock Data APIs**

```
Current: Stock prices from local intel records (static, stale data)
Missing: Live ticker APIs (IEX, Alpha Vantage, Finnhub, etc.)
Impact: "IBM stock over past 6 months" - returns NO actual price data
Fix: Add live market data connector
```

#### 2. **Red Hat APIs**

```
Missing:
  - Red Hat Subscription Management API
  - Red Hat Customer Portal API
  - Red Hat Insights API (system health)

Would Enable:
  - Real account subscription status
  - Live system inventory
  - Active automation job status
  - Security advisories and CVE data
```

#### 3. **Cloud Provider APIs**

```
Missing:
  - AWS (EC2, S3, Lambda, Cost Explorer)
  - Azure (VMs, Storage, Cost Management)
  - GCP (Compute, Storage, Billing)

Current: Only metadata references in tech stacks
Would Enable:
  - Live infrastructure inventory
  - Cost/usage data
  - Security posture scanning
  - Workload analysis
```

#### 4. **CRM Integration**

```
Missing:
  - Salesforce API
  - HubSpot API
  - Pipedrive API

Current: Manual contact import from files
Would Enable:
  - Real-time deal tracking
  - Account sync
  - Sales velocity metrics
  - Forecast accuracy
```

#### 5. **Monitoring & Observability**

```
Incomplete:
  - Prometheus: Setup only, no queries/alerts
  - Grafana: Setup only, no dashboard creation
  - PagerDuty: Setup only, no incidents created
  - Slack: Setup only, no notifications sent

Would Enable:
  - Actual metric dashboards
  - Incident management workflow
  - Real-time notifications
  - Cross-system alerting
```

#### 6. **Email/Calendar Integration**

```
Missing:
  - Office 365 / Google Workspace API
  - Calendar event creation
  - Email archive search
  - Meeting scheduling

Current: None
Would Enable:
  - Auto-schedule discovery calls
  - Meeting notes parsing
  - Email search for account context
```

#### 7. **Data Freshness & Caching**

```
Issues:
  - Intel cache TTL: 3 days (configurable but static)
  - No cache invalidation triggers
  - No data quality metrics
  - No freshness indicators in reports

Fix:
  - Event-driven cache invalidation
  - Data age indicators in output
  - Automatic refresh on stale data
  - Webhook-based updates
```

#### 8. **Webhooks & Event Streams**

```
Missing:
  - Incoming webhook receiver (for alerts)
  - Event queue system
  - Pub/sub for notifications
  - Real-time data sync

Current: Polling only, no event-driven updates
Would Enable:
  - Real-time notifications from external systems
  - Reactive automation
  - Sub-second incident response
```

---

## Integration Readiness Matrix

| Component            | Working | Partial | Missing | Notes                                             |
| -------------------- | ------- | ------- | ------- | ------------------------------------------------- |
| **Core LLM**         | ✓       |         |         | Bridge + Ollama running                           |
| **Training Data**    | ✓       |         |         | Ingestion working, 6+ integrations available      |
| **Business Intel**   | ✓       | ✓       |         | Local records working, real-time sources missing  |
| **Stock Data**       |         | ✓       | ✓       | Local intel only, need live APIs                  |
| **Red Hat Services** |         | ✓       | ✓       | Strategy guides, APIs missing                     |
| **Cloud Providers**  |         | ✓       | ✓       | Metadata references, APIs missing                 |
| **CRM**              |         | ✓       | ✓       | Manual import, no API sync                        |
| **Notifications**    | ✓       | ✓       |         | Dispatch ready, but no active triggers            |
| **Monitoring**       | ✓       | ✓       |         | Collectors working, dashboard/alerting incomplete |
| **Email**            |         | ✓       |         | Mail command only, no calendar/archive            |
| **Webhooks**         |         |         | ✓       | No event stream system                            |
| **Caching**          | ✓       | ✓       |         | Working but static, needs events                  |

---

## Recommended Priority Order

### Phase 1: High Impact (Do First)

1. **Real-time stock APIs** — Fix immediate user pain (IBM stock query)
2. **Data freshness system** — Implement cache invalidation
3. **Slack notifications** — Activate existing notification engine
4. **Red Hat Insights API** — Get live security/health data

### Phase 2: Medium Impact (Do Next)

1. **Salesforce/HubSpot sync** — Keep accounts in sync automatically
2. **Cloud provider APIs** — AWS/Azure cost + inventory
3. **PagerDuty incidents** — Link detection to incident management
4. **Email calendar** — Auto-schedule sales activities

### Phase 3: Advanced (Polish)

1. **Webhook receiver** — Event-driven architecture
2. **Prometheus queries** — Active metric collection
3. **Grafana dashboards** — Visual infrastructure view
4. **Meeting notes parsing** — Context enrichment

---

## Quick Fix Recommendations

### Immediate (30 min)

```python
# Add stock API connector (hal-stocks.py enhancement)
def get_live_stock_price(symbol: str) -> dict:
    """Fetch from IEX Cloud API instead of local intel"""
    # Replace stub with real API

# Add cache invalidation trigger
def invalidate_cache_on_event(account: str, event_type: str):
    """On account update, refresh intel immediately"""
```

### Short-term (2-4 hours)

```
1. Add Red Hat Insights API integration
2. Activate Slack webhook for notifications
3. Add timestamp indicators to reports
4. Create data freshness monitoring
```

### Medium-term (1-2 days)

```
1. Salesforce account sync
2. AWS cost API connector
3. PagerDuty incident bridge
4. Calendar event automation
```

---

## Assessment: Is This Correct?

**YES** — The analysis is correct. The tool has:

- ✓ Strong foundations (LLM, training data, local intel)
- ✓ Good infrastructure (Ollama, bridge, storage)
- ✓ Partial integrations (notification framework, monitoring collectors)
- ✗ **CRITICAL GAP:** No real external API integrations (stock, cloud, CRM)
- ✗ **USABILITY GAP:** Offline knowledge base isn't connected to live data sources

**Your observation is valid:** More integration points ARE needed for the tool to be fully operational.

---

## Next Steps

Would you like me to:

1. **Fix the stock API integration first** (makes your IBM query work properly)?
2. **Build the data freshness/cache invalidation system**?
3. **Activate Slack notifications** (use existing framework)?
4. **Create a priority integration plan** with timelines?
