import importlib.util
import os

import pandas as pd
import pytest

import aggregator
import geo_results
import get_atlas_data
from common import parse_result_stem, read_table, result_stem, sanitize_field
from config import load_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_exporter():
    spec = importlib.util.spec_from_file_location("exporter", os.path.join(ROOT, "post-json-to-grafana.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- config ------------------------------------------------------------------

def test_config_substitution_is_recursive():
    cfg = load_config()
    assert cfg["agg_output_dir"] == "./results/agg"
    assert cfg["grafana_input_dir"] == "./results/agg"


# --- file names --------------------------------------------------------------

def test_result_stem_roundtrip_with_dotted_service():
    stem = result_stem("ns1.dns.nl", "IPv6", 1760000400, 1760002200)
    assert stem == "chaos_ns1.dns.nl__IPv6_20251009_0900_0930"
    meta = parse_result_stem(stem + "-agg")
    assert meta["service"] == "ns1.dns.nl" and meta["ipv"] == "IPv6" and meta["start"] == "0900"


# --- downloader --------------------------------------------------------------

def test_sanitize_field_blocks_row_injection(tmp_path):
    evil = {"prb_id": 1, "timestamp": 1, "from": "1.2.3.4",
            "result": {"rt": 3, "answers": [{"RDATA": ["x\n2\t1\t9.9.9.9\t1\tnnn1-fake\t0\t"]}]}}
    path = tmp_path / "x.tsv"
    get_atlas_data.write_results(str(path), [evil])
    df = read_table(str(path))
    assert len(df) == 1
    assert "\n" not in df.loc[0, "rdata"] and "\t" not in df.loc[0, "rdata"]
    assert sanitize_field(None) == ""


def test_extract_info_error_row():
    row = get_atlas_data.extract_info({"prb_id": 7, "from": "1.2.3.4", "error": {"timeout": 5000}})
    assert row["error_type"] == "timeout" and row["rcode"] is None


@pytest.mark.parametrize("last_done,expected", [(None, 1), (0, 4), ("current", 0), ("minus2", 2)])
def test_windows_to_fetch(last_done, expected):
    stop = 1800 * 1_000_000
    if last_done == "current":
        last_done = stop
    elif last_done == "minus2":
        last_done = stop - 2 * 1800
    assert len(get_atlas_data.windows_to_fetch(last_done, stop)) == expected


# --- enrichment --------------------------------------------------------------

def test_validator_regex_and_nan_substring():
    v = geo_results.build_validator("A")
    assert geo_results.validate_chaos_string(v, "nnn1-nanog")  # contains "nan"
    assert not geo_results.validate_chaos_string(v, "hijacked")
    assert not geo_results.validate_chaos_string(v, float("nan"))


def test_validator_chaos_string_fallback():
    v = geo_results.build_validator("exact.example")
    assert v("ns1-fra") and not v("ns1-lhr")
    assert geo_results.build_validator("unknown") is None


def test_read_table_keeps_namibia_and_numeric_rdata(tmp_path):
    p = tmp_path / "t.tsv"
    p.write_text("prb_id\trdata\tcountry\trt\n1\t123\tNA\t5\n2\t\tNL\t\n")
    df = read_table(str(p))
    assert df.loc[0, "country"] == "NA"
    assert df.loc[0, "rdata"] == "123"
    assert pd.isna(df.loc[1, "rdata"]) and pd.isna(df.loc[1, "rt"])
    assert geo_results.country_to_region("NA") == "Africa"


# --- aggregation -------------------------------------------------------------

def _frame(rows):
    cols = ["prb_id", "src_addr", "rt", "rdata", "rcode", "error_type", "asn",
            "country", "continent", "v4_works", "v6_works", "chaos_valid"]
    return pd.DataFrame(rows, columns=cols)


def test_aggregate_without_timeouts_does_not_crash():
    df = _frame([
        [1, "8.8.8.8", 20.0, "nnn1-a", 0, None, 1, "NL", "Europe", True, True, True],
        [2, "8.8.4.4", 30.0, "nnn1-b", 0, None, 1, "DE", "Europe", True, True, True],
    ])
    out = aggregator.aggregate(df, "IPv4")
    assert out["valid_queries_rcode0_with_correct_chaos"] == 2
    assert out["rtt_rcode0_correct_chaos"]["p50"] == 25.0
    assert out["asn_failed_probes_classification_len"] == 0


def test_aggregate_classification():
    df = _frame([
        [1, "8.8.8.8", 20.0, "nnn1-a", 0, None, 100, "NL", "Europe", True, True, True],
        [2, "9.9.9.9", None, None, None, "timeout", 100, "NL", "Europe", True, True, False],
        [3, "1.1.1.1", None, None, None, "timeout", 200, "US", "North America", True, True, False],
        [4, "192.168.1.1", None, None, None, "timeout", 300, "US", "North America", True, True, False],
        [5, "4.4.4.4", 1.0, "local", 0, None, 400, "BR", "South America", True, True, False],
        [6, "5.5.5.5", 2.0, None, 5, None, 400, "BR", "South America", True, True, False],
        [7, "6.6.6.6", 2.0, None, 5, None, 500, "BR", "South America", False, True, False],  # v4 broken
        [8, "7.7.7.7", None, None, 2, None, 600, "BR", "South America", True, True, False],  # no rt
    ])
    out = aggregator.aggregate(df, "IPv4")
    assert out["total_queries"] == 8
    assert out["total_probes"] == 7
    assert out["hijack_rcode0_wrong_chaos"] == 1
    assert out["failed_probes_timeout_but_stack_works"] == 2
    assert out["failed_probes_invalid"] == 1          # private source address
    assert out["failed_probes_maybe_hijack"] == 2     # probes 6 (fast) and 8 (no rt); 7 excluded (stack broken)
    assert out["rcode_counts"] == {"0": 2, "null": 3, "5": 1, "2": 1}
    assert out["asn_failed_probes_classification"] == {
        "100": {"timed_out": [2], "working": [1]},
        "200": {"timed_out": [3], "working": []},
    }
    assert out["asn_failed_probes_classification_len"] == 1  # ASN 200


# --- export ------------------------------------------------------------------

def test_exporter_payload_and_rcode_count():
    exp = _load_exporter()
    assert exp.other_rcode_count({"0": 5, "2": 1, "null": 3, "0.0": 1}) == 4
    data = {"IPv4": {"total_queries": 10, "rtt_rcode0_correct_chaos": {"p50": 12.5}},
            "IPv6": {"total_queries": 8, "rtt_rcode0_correct_chaos": {"p50": None}}}
    payload = exp.build_payload("A", data, 1_000_000_000)
    metrics = {m["name"]: m for m in payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
    assert len(metrics["rootviz_A_total_queries"]["gauge"]["dataPoints"]) == 2
    median = metrics["rootviz_A_median_rtt_rcode0_correct_chaos"]["gauge"]["dataPoints"]
    assert median == [{"asDouble": 12.5, "timeUnixNano": "1000000000",
                       "attributes": [{"key": "protocol", "value": {"stringValue": "v4"}}]}]
