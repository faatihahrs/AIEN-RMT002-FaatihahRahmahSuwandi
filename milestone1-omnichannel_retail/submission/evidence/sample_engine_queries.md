# Sample NL-to-SQL Engine Queries

Configuration: `NL2SQL_PROVIDER=mock` and `ANALYTICS_DB_TARGET=local`.
The SQL safety layer permits only read-only queries against the Gold schema.

## Example 1: Daily net revenue

**Question:** `What was the daily net revenue?`

**Generated SQL:**

```sql
SELECT metric_date, net_revenue
FROM gold.executive_kpis_daily
ORDER BY metric_date
```

**Result:** 90 daily rows. The UI selected a time-series chart.

## Example 2: Net revenue by channel

**Question:** `What is total net revenue by sales channel?`

**Generated SQL:**

```sql
SELECT sales_channel, SUM(net_revenue) AS net_revenue
FROM gold.order_360
GROUP BY sales_channel
ORDER BY net_revenue DESC
```

**Result:** Four sales-channel rows.

## Example 3: Campaign ROAS

**Question:** `Which campaigns had the best roas?`

**Generated SQL:**

```sql
SELECT campaign_id,
       SUM(net_revenue) / NULLIF(SUM(campaign_spend), 0) AS roas
FROM gold.channel_campaign_daily
GROUP BY campaign_id
HAVING SUM(campaign_spend) > 0
ORDER BY roas DESC
```

**Result:** Ten campaign rows.

## Benchmark Validation

`Summary: 25/25 queries passed`

Complete output: `evidence/nl2sql_benchmark.txt`.
