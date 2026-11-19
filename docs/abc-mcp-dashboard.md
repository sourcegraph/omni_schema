# ABC MCP dashboard setup and validation

The model supplies an ABC tab for the existing **MCP Usage** dashboard and an MCP Start cohort for **ABC Funnel Conversion**. Saved dashboard changes and live Omni/BigQuery validation are pending access. This change does not create a warehouse pipeline or change billing.

## Fields and tiles

Use topic `abc_mcp_usage` for calls. In the table below, `E` means `telligentsourcegraph_telemetry__v2_events`; replace it with that full view name when building the query. The topic inherits `events_v2` account joins and the 30-day default date filter, and restricts rows to endpoint 8. Discovery stays available as a separate measure.

| Tile | Fields / query | Presentation |
| --- | --- | --- |
| Calls and adoption | `E.abc_mcp_calls`, `E.abc_mcp_users`, `E.abc_mcp_instances` | Cards. Users include service accounts; instances are not customer accounts. |
| Daily usage | `E.timestamp[date]`, `E.abc_mcp_calls`, `E.abc_mcp_users` | Daily series. Distinct users over the selected period must be queried directly, not summed across days. |
| Tools | `E.mcp_tool_call_category`, `E.abc_mcp_calls` | Bar/table; remove zero-count discovery row if shown. |
| Start modes | `E.abc_mcp_start_mode`, `E.abc_mcp_start_calls` | Attended/unattended calls. Filter Start mode to non-null. |
| Start outcomes | `E.mcp_terminal_state`, `E.abc_mcp_start_calls` | Outcomes for Start calls, including failed/refused. |
| Outcomes by tool | `E.mcp_tool_call_category`, `E.mcp_terminal_state`, `E.abc_mcp_calls` | Success, Failure, Timeout, Canceled, Invalid request, and unknown. Failure includes policy refusals. |
| Latency by tool | `E.mcp_tool_call_category`, `E.abc_mcp_p50_duration`, `E.abc_mcp_p95_duration` | Milliseconds. Read tools can deliberately wait, so do not combine polling and mutation latency in one headline. |
| Clients | `E.abc_mcp_client`, `E.abc_mcp_calls`, `E.abc_mcp_users` | Existing server API user-agent attribution; missing values are Unknown. |
| Accounts | Existing `telligentsourcegraph_sourcegraph_analytics__server_instances.sfdc_name`, `E.abc_mcp_calls`, `E.abc_mcp_users` | Retain existing account access controls and unmapped instances. Do not add per-account distinct counts to produce a global total. |
| Discovery | `E.abc_mcp_discovery_calls` | Separate card, excluded from invocation totals. |
| Attribution coverage | `E.abc_mcp_attributed_start_calls`, `E.abc_mcp_unknown_start_calls` | Successful calls with/without usable run identity. Unknown is not UI origin. |
| Data observed | `E.abc_mcp_last_event`, `E.abc_mcp_last_ingested` | Latest event and ingestion within selection. Neither proves pipeline freshness. |

No new failure-rate formula is required. Keep the company-wide `failure_timeout_rate` unchanged: `(Failure + Timeout) / (Success + Failure + Timeout)`. Canceled, Invalid request and unknown stay separate. If a percentage is added to the ABC outcome chart, label it as a share of **all invocation outcomes**, and include those categories in its denominator.

Use topic `abc_mcp_starts` for conversion. `S` below means `abc_mcp_starts`.

| Tile | Fields / query | Presentation |
| --- | --- | --- |
| MCP-started agents | `S.runs`, `S.runs_merged`, `S.start_to_merge_conversion` | Cards. All attributed Starts remain in the denominator, even with no funnel row. |
| MCP funnel | `S.runs`, `S.runs_prompted`, `S.runs_workspaces_resolved`, `S.runs_workspaces_run`, `S.runs_published`, `S.runs_merged` | Observed Start followed by the five existing lifecycle stages. |
| Mode conversion | `S.start_mode`, `S.runs`, `S.runs_merged`, `S.start_to_merge_conversion` | Table. Do not sum percentages. |
| Start client / account conversion | `S.start_client` or `S.customer_name` with the same three measures | First observed Start client stays fixed across later replay or handover. Account uses the current existing funnel lookup rule. |

Add these tiles to the existing ABC Funnel Conversion dashboard as an MCP cohort section/tab; do not filter its original all-origin tiles globally. A merge-converted agent counts once even if it merged multiple changesets. These fields are not merged-PR counts or billable units.

## Filters, attribution and empty states

- Usage date maps to `E.timestamp`; cohort date maps to `S.observed_start_at`. Default both to 30 days. Filter cohort dates **after** first-Start deduplication. A replay tomorrow does not create tomorrow's cohort.
- Do not map the cohort date filter to `published_at` or `merged_at`. A run started in the selected period can convert later; outcomes reflect the latest available daily `abc_funnel` refresh.
- Usage client maps to `E.abc_mcp_client`; cohort client maps to `S.start_client`. Label them “Call client” and “Start client”, because a replay can use a different client. Account selectors map to the existing usage account field and `S.customer_name`; validate both mappings in preview.
- Keep service-account segmentation explicit through `E.service_account`. The new user metric includes service accounts, while existing company-wide human-user metrics retain their definitions.
- Runs use the existing `LOWER(external_url)|agent_id` convention. Exported UUIDs deduplicate calls; this key deduplicates successful Start replays. UI-created agents later read through MCP never enter the cohort.
- First observed Start can itself be a replay. Historical missing-ID events stay Unknown; do not infer run identity from user/time. A missing successful call export cannot be reconstructed from this join. Record the backend attribution deployment date in the dashboard description.
- Show an empty conversion denominator as “No attributable runs” with a blank rate, not 0%. If no ABC events exist, show “No observed data” and the selected date range. Unknown attribution and ingestion delays are not proof of zero use.
- The account lookup deduplicates server endpoints using the existing funnel's Primary/mapped-account preference. Unmapped cohort runs fall back to their instance URL, including runs with no funnel row. Existing usage account joins are unchanged; validate their endpoint/account cardinality before publishing account slices.

## Local deterministic checks

```sh
python3 -m venv /tmp/abc-mcp-model-tests
/tmp/abc-mcp-model-tests/bin/pip install -r tests/requirements.txt
PYTHONDONTWRITEBYTECODE=1 /tmp/abc-mcp-model-tests/bin/python -m unittest discover -s tests -v
```

The tests load actual model YAML expressions and query SQL, translate BigQuery syntax with SQLGlot and execute them in DuckDB using synthetic data. The fixture includes 12 raw rows, one repeated UUID, one Start replay, two instances sharing numeric agent ID 101, an agent with no funnel activity, a UI agent read through MCP, one historical Start without attribution, and later merges.

Expected results: **9 calls, 1 discovery, 7 Start calls (6 success, 1 failed), 6 attended / 1 unattended calls, 5 attributed successful calls, 1 unknown successful call, 4 unique runs, 1 merged run, 25% conversion**. Repeated merge-stage observations still count as one converted agent. Changeset counts are collapsed upstream and are outside this model's scope. Additional checks cover all new enum labels, reserved/future IDs, null metadata, client changes, empty cohorts, duplicate account endpoints, and percentile/user deduplication.

The local continuous-percentile reference expects p50=20 and p95=37 for durations `[10, 20, 20, 40]`; a duplicate UUID with duration 10 must not alter them. Omni's `percentile_distinct_on` is keyed by event UUID, so equal durations from distinct calls remain separate observations. Compare the generated BigQuery aggregation and rendered results in preview; local execution does not validate Omni compilation or warehouse percentile implementation.

## Before publishing

1. Load the model into an Omni development branch and validate every changed topic/field. Confirm inherited date filters, user-agent labels, percentile SQL and account joins. Render the new tiles in the existing dashboard drafts and compare each card to its query.
2. Dry-run the generated SQL in BigQuery using bounded cohort filters and the approved billing cap. The cohort explicitly bounds raw events to `timestamp >= 2026-06-23`, matching the existing ABC funnel. It must inspect history before selecting the displayed cohort so that late replays do not create new starts. The 30-day display filter does **not** guarantee a 30-day raw scan. Check actual bytes/partition pruning before enabling refresh; if unacceptable, ask the existing analytics pipeline owner to materialize the same cohort rather than truncate history silently.
3. Verify the warehouse identity convention, URL reuse/instance resets, endpoint-to-account mappings, backend deployment version/date, and the actual funnel refresh timestamp. This implementation follows the existing funnel's URL/agent key; it cannot distinguish different instances that reuse both the URL and agent ID. Record that limitation if applicable.
4. Run attended Start plus one replay through real MCP, publish and merge one test PR; run unattended Start to an unmerged Draft; issue one refused Start. Trace both actual agents through terminal telemetry, exported events, warehouse rows and rendered Omni results. Start-only totals are **4 calls, 3 success, 1 failed; 2 unique runs; 1 merged run; 50% conversion**. Cleanup calls add other invocation types.
5. Wait for documented export/funnel refresh schedules, reconcile actual host outcomes independently, and clean up test resources. Save source/event/model versions, exact query results and screenshots. Do not mark live verification complete from a green local test or an empty chart.

References: [Omni aggregate types](https://docs.omni.co/modeling/measures/parameters/aggregate-type), [custom aggregation keys](https://docs.omni.co/modeling/measures/parameters/custom-primary-key-sql), [query-view SQL](https://docs.omni.co/modeling/query-views/parameters/sql).
