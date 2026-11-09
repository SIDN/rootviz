# How rootviz metrics are computed

This document describes exactly how every number in the aggregated JSON files
and in the exported metrics is computed, step by step. It follows the code in
[`get_atlas_data.py`](get_atlas_data.py), [`geo_results.py`](geo_results.py),
[`aggregator.py`](aggregator.py) and [`post-json-to-grafana.py`](post-json-to-grafana.py).

All metrics are computed **per service, per IP version, per 30-minute window**.

- [1. Input: one row per Atlas result](#1-input-one-row-per-atlas-result)
- [2. Enrichment](#2-enrichment)
- [3. Row sets used by the metrics](#3-row-sets-used-by-the-metrics)
- [4. Aggregated JSON fields](#4-aggregated-json-fields)
- [5. Exported (Grafana) metrics](#5-exported-grafana-metrics)
- [6. Caveats](#6-caveats)

---

## 1. Input: one row per Atlas result

For each service, rootviz reads two RIPE Atlas measurements: one sent over IPv4,
one over IPv6. Every Atlas probe in these measurements periodically sends a
`hostname.bind CHAOS TXT` query and reports the outcome.

For each window `[start, stop)` (aligned to `batch_secs`, 1800 s by default),
`get_atlas_data.py` requests
`/api/v2/measurements/<id>/results/?start=<start>&stop=<stop>` and writes
**one row per result object**. A probe that queried 7 times in the window
produces 7 rows.

| Column | Taken from the Atlas result |
|--------|-----------------------------|
| `prb_id` | `prb_id` |
| `timestamp` | `timestamp` |
| `src_addr` | `from`: the probe's address as seen by the RIPE Atlas infrastructure |
| `rt` | `result.rt`: response time in milliseconds |
| `rdata` | First string of the first answer: `result.answers[0].RDATA[0]` |
| `rcode` | RCODE decoded from the raw DNS response `result.abuf`. Empty if there is no `abuf` or it cannot be decoded |
| `error_type` | First key of the `error` object, for example `timeout`. Only set when there is no `result` |

A row therefore has **either** a response (`rt`, `rcode`, maybe `rdata`)
**or** an error (`error_type`), never both. Control characters in any field are
replaced by spaces.

## 2. Enrichment

`geo_results.py` adds the following columns using the most recent RIPE Atlas
probe archive (`probes-YYYYMMDD.csv`, at most a few days old):

| Column | How it is set |
|--------|---------------|
| `asn` | The probe's `asn_v4` for an IPv4 measurement, its `asn_v6` for an IPv6 measurement. This is the probe's registered ASN, not derived from `src_addr`. Empty if unknown |
| `country` | The probe's `country_code`, as configured by the probe host. `Unknown` if the probe is not in the archive |
| `continent` | Continent of `country` (via `pycountry_convert`), or `Unknown` |
| `v4_works` | `false` if the probe carries the Atlas system tag `system-ipv4-doesnt-work`, otherwise `true`. Probes missing from the archive get `true` |
| `v6_works` | The same, with `system-ipv6-doesnt-work` |
| `chaos_valid` | `true` if `rdata` is present and `re.match(regex, rdata)` succeeds with the service's configured `regex`. If no `regex` is configured, `true` if `rdata` exactly equals one of the comma-separated `chaos_string` values. `false` when `rdata` is empty |

## 3. Row sets used by the metrics

Let `stack_works` be `v4_works` for an IPv4 file and `v6_works` for an IPv6
file. The aggregator builds these sets of **rows**:

| Set | Definition |
|-----|------------|
| **ALL** | Every row in the window |
| **D** | Rows of **ALL** where `stack_works` is true. This leaves out probes that Atlas knows cannot use this IP version |
| **OK** | Rows of **D** with `rcode = 0` **and** `chaos_valid` |
| **OK_ALL** | Rows of **ALL** with `rcode = 0` **and** `chaos_valid` (no stack filter). Only used for the ASN classification |
| **F** (failed) | Rows of **D** whose `rcode` is not 0 **or is empty**: every DNS error response, every timeout and every other Atlas error |

**F** is split further. A source address is *routable* if `src_addr` parses
as an IP address and is globally routable, according to Python's
`ipaddress.ip_address(src_addr).is_global`. Private, loopback, link-local,
CGNAT and documentation ranges are not routable. `T` is `agg_local_rtt_ms`,
5 ms by default.

| Subset of F | Condition |
|-------------|-----------|
| **F_invalid** | `src_addr` is **not** routable (any outcome) |
| **F_error** | routable, and `error_type` is set |
| **F_timeout** | **F_error** rows with `error_type = "timeout"` |
| **F_slow** | routable, no `error_type`, `rt > T`: a real DNS response with a non-zero RCODE |
| **F_fast** | routable, no `error_type`, `rt <= T` or `rt` missing: a non-zero RCODE that came back implausibly fast, probably from something close to the probe |

A *probe identity* below means the distinct combination
`(prb_id, src_addr, asn, country)`. In practice this is one per probe. A probe
that changed address during the window counts once per address.

## 4. Aggregated JSON fields

One `chaos_<service>__<IPv4|IPv6>_<YYYYMMDD>_<HHMM>_<HHMM>-agg.json` per window.

| Field | Computation |
|-------|-------------|
| `file` | Name of the input file |
| `timestamp` | Window **start**, Unix seconds, UTC |
| `total_queries` | Number of rows in **ALL**. *This is the only count without the stack filter* |
| `total_probes` | Distinct `prb_id` in **D** |
| `total_addresses` | Distinct non-empty `src_addr` in **D** |
| `rcode_counts` | Rows of **D** grouped by `rcode`. The key is the RCODE number as a string (`"0"`, `"2"`, `"5"`, …), and `"null"` for rows without an RCODE (errors and timeouts) |
| `error_counts` | Rows of **D** with `error_type` set, grouped by `error_type` |
| `valid_queries_rcode0_with_correct_chaos` | Number of rows in **OK** |
| `valid_probe_ids_rcode0_with_correct_chaos` | Distinct `prb_id` in **OK** |
| `valid_addresses_rcode0_with_correct_chaos` | Distinct `src_addr` in **OK** |
| `hijack_rcode0_wrong_chaos` | Rows of **D** with `rcode = 0` and `chaos_valid = false`. The probe got a NOERROR answer whose identity does not match the service: either a wrong string or no TXT answer at all |
| `rtt_rcode0_correct_chaos` | `p25`, `p50`, `p75`, `p99` of `rt` over the rows of **OK** (see [percentiles](#percentiles)) |
| `rtt_rcode0_correct_cahos_per_cont` | The same percentiles, computed separately per `continent`, over **OK** rows whose continent is known. (The misspelling is kept for compatibility) |
| `rtt_rcode0_correct_chaos_per_country` | The same, per `country` code |
| `failed_probes_invalid` | Distinct probe identities in **F_invalid** |
| `failed_probes_error` | Distinct `prb_id` in **F_error** |
| `failed_probes_timeout_but_stack_works` | Distinct probe identities in **F_timeout**. "Stack works" because **F** ⊆ **D** |
| `failed_probes_valid` | Distinct probe identities in **F_slow** |
| `failed_probes_maybe_hijack` | Distinct probe identities in **F_fast** |
| `asn_failed_probes_classification` | See below |
| `asn_failed_probes_classification_len` | Number of ASNs in `asn_failed_probes_classification` whose `working` list is empty |

### Percentiles

Percentiles are computed with linear interpolation between the closest ranks.
This is `numpy.percentile`'s default and equals `PERCENTILE_CONT` in SQL.
Results are rounded to 3 decimals. Rows with an empty `rt` are ignored. If no
rows remain, all four values are `null`.

Percentiles are computed over **rows, not probes**: a probe that reported 7
results in the window contributes 7 values.

### ASN classification

This answers the question: *"When probes in a network time out, is the
problem the probe, or does the whole network fail to reach the service?"*

For every ASN that has at least one row in **F_timeout** (rows with an
unknown ASN are skipped):

1. `timed_out` = distinct `prb_id` of that ASN's **F_timeout** rows.
2. `working` = distinct `prb_id` of that ASN's rows in **OK_ALL**, minus the
   probes in `timed_out`.

The result maps each ASN (as a string, because JSON keys must be strings) to
both sorted lists:

```json
"asn_failed_probes_classification": {
  "376":  {"timed_out": [1234, 5678], "working": []},
  "3320": {"timed_out": [42],         "working": [77, 91, 102]}
}
```

An empty `working` list means no other probe in that network got a valid
answer, so the whole network may be unable to reach the service (AS 376
above). A non-empty list points to a problem with the individual probes
instead (AS 3320). `asn_failed_probes_classification_len` counts the ASNs with
an empty `working` list.

A probe that timed out at least once in the window is never counted as an
"other" probe for its own ASN, even if it got valid answers at other times in
the window.

## 5. Exported (Grafana) metrics

`post-json-to-grafana.py` sends OTLP **gauges** named
`<grafana_metric_prefix>_<service>_<suffix>`. Each has two data points, one
per IP version, with the attribute `protocol` = `v4` or `v6`. Their time is
the window **start**.

| Suffix | Value | Unit | Type |
|--------|-------|------|------|
| `total_queries` | `total_queries` | count | int |
| `total_probes` | `total_probes` | count | int |
| `valid_rcode0_queries_with_correct_chaos` | `valid_queries_rcode0_with_correct_chaos` | count | int |
| `other_rcode_queries` | Sum of `rcode_counts` for every key except `"0"`, *including* `"null"`. This equals the number of rows in **F** | count | int |
| `timeout_probes` | `failed_probes_timeout_but_stack_works` | count | int |
| `valid_probes_with_errors_timeout_etc` | `failed_probes_error` | count | int |
| `valid_probes_failed_no_error` | `failed_probes_valid` | count | int |
| `median_rtt_rcode0_correct_chaos` | `rtt_rcode0_correct_chaos.p50` | ms | double |
| `p75_rtt_rcode0_correct_chaos` | `rtt_rcode0_correct_chaos.p75` | ms | double |
| `p99_rtt_rcode0_correct_chaos` | `rtt_rcode0_correct_chaos.p99` | ms | double |
| `failed_asns_valid_probes` | `asn_failed_probes_classification_len` | count | int |

**API names.** The public [API](API.md) exposes the same gauges as `<LETTER>_<suffix>` (for example `B_timeout_probes`): the `<grafana_metric_prefix>` is dropped and the service is the upper-case instance letter. The suffixes in the table are identical.

If a value is missing, for example no RTT because there were no valid answers,
that data point is left out rather than sent as 0. `p25`, the per-country and
per-continent RTTs, `rcode_counts`, `error_counts`, `failed_probes_invalid` and
`failed_probes_maybe_hijack` are only available in the JSON files.

Percentages shown on dashboards, such as *timed-out probes (%)*, are computed in
Grafana from these gauges, for example `timeout_probes / total_probes`. Both
of those come from set **D**.

## 6. Caveats

- **Stack filter mismatch.** `total_queries` counts all rows, while every other
  count only includes probes whose IP stack works (**D**). Don't divide
  stack-filtered counts by `total_queries`.
- **Rows vs. probes.** Some fields count rows (queries) and others count
  distinct probes. The field names say `queries` or `probes`. Probes with a
  shorter query interval weigh more in row-based counts and in percentiles.
- **One probe, several categories.** A probe can time out in one query and get a
  valid answer in the next. It then appears both in `valid_*` counts and in
  `failed_*` counts for the same window.
- **Probe metadata is a daily snapshot.** ASN, country and stack tags come from
  the newest probe archive, which may be up to a few days old. Country is
  the location set by the probe host.
- **Late results.** Results that Atlas receives after the window has been
  downloaded are not included.
- **Only the first TXT string is checked.** If a server returns several TXT
  strings, only the first one is validated.

See also the README section *Known issues in historical data* for bugs that
affected data produced before 9 November 2026.
