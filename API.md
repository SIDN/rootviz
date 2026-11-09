# Rootviz API Reference

## Production URL

The production base URL is `https://api.rootviz.sidnlabs.nl`.

FastAPI's interactive documentation is served on the same HTTPS host and port:

- Swagger UI: `https://api.rootviz.sidnlabs.nl/docs`
- ReDoc: `https://api.rootviz.sidnlabs.nl/redoc`
- OpenAPI JSON: `https://api.rootviz.sidnlabs.nl/openapi.json`

## Authentication

Include the API key issued to you in the `X-API-Key` request header. Keep it secret and do not commit it to source control. To request an API key, contact `sidnlabs@sidn.nl`.

## Endpoint

### `GET /metrics`

Returns time-series data for one or more requested metrics. Each series includes labels such as the protocol (`v4` or `v6`) and timestamp/value samples.

#### Query parameters

| Name | Required | Default | Description |
| --- | --- | --- | --- |
| `start` | Yes | | Range start as a timezone-aware ISO-8601 datetime, such as `2026-11-05T10:00:00Z`. |
| `end` | Yes | | Range end as a timezone-aware ISO-8601 datetime. Must be after `start`. |
| `step` | No | `1800` | Sampling interval in seconds. Minimum is 1800 seconds (30 minutes). |
| `metric` | No | All supported metrics | Metric selector; repeat the parameter to request multiple metrics. |

The maximum range is currently one day. To retrieve a week, make seven daily requests. Align start and end to the 30-minute sampling boundaries where possible. Adjacent ranges may include the same boundary sample; deduplicate by timestamp and labels when combining responses.

#### Metric selectors

Use a letter from A through M followed by one of these selectors:

- `median_rtt_rcode0_correct_chaos`
- `p75_rtt_rcode0_correct_chaos`
- `p99_rtt_rcode0_correct_chaos`
- `failed_asns_valid_probes`
- `other_rcode_queries`
- `timeout_probes`
- `total_probes`
- `total_queries`
- `valid_probes_failed_no_error`
- `valid_probes_with_errors_timeout_etc`
- `valid_rcode0_queries_with_correct_chaos`
- `ratio_timeout`

For compatibility, the shorter selectors `<letter>_timeout` and `<letter>_total` are also accepted. `<letter>_ratio_timeout` returns the timeout percentage. Omitting `metric` requests all supported metrics. An unsupported selector returns HTTP 422.

#### Example request

```bash
curl -G 'https://api.rootviz.sidnlabs.nl/metrics' \
  -H 'X-API-Key: <your-issued-api-key>' \
  --data-urlencode 'start=2026-11-05T10:00:00Z' \
  --data-urlencode 'end=2026-11-05T11:00:00Z' \
  --data-urlencode 'step=1800' \
  --data-urlencode 'metric=B_timeout_probes' \
  --data-urlencode 'metric=B_total_probes' \
  --data-urlencode 'metric=B_ratio_timeout'
```

The JSON response contains `status` and `data`. `data.result` is a list of series; each series includes labels and a `values` list of `[Unix timestamp, string value]` samples. An empty `data.result` means the request succeeded but there were no samples for the selected metrics and evaluation times.

#### HTTP errors

| Status | Meaning |
| --- | --- |
| `401` | Missing or invalid API key. |
| `422` | Invalid selector, timezone-naive timestamps, invalid range, or `step` below 1800 seconds. |
| `429` | Rate limit exceeded. Check the `Retry-After` header. |
| `502` | The upstream data request failed. |

The default rate limit is 10 requests per minute per key. Limits are held in memory per API process and reset on restart; clients may also be throttled by the service's edge controls.
