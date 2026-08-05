"""Parsing engine output.

Covers the specification's parsing requirements: a complete result, a result
with optional fields missing, malformed output, unit conversion, and the
distinction between a measured zero and a missing measurement.
"""

from __future__ import annotations

import json

import pytest

from bandwidth_logger.core.models import bps_to_mbps, bytes_per_second_to_bps
from bandwidth_logger.core.result_parser import (
    ParseError,
    coerce_float,
    coerce_int,
    parse_ookla_json,
)
from support.fake_engine import SPARSE_RESULT, SUCCESS_RESULT, ZERO_RESULT


class TestCompleteOutput:
    def test_parses_every_field_of_a_full_result(self):
        measurement = parse_ookla_json(json.dumps(SUCCESS_RESULT), engine_version="1.2.0")

        # Ookla reports bytes per second; 11,875,000 B/s is 95 Mbps.
        assert measurement.download_bps == 95_000_000
        assert measurement.upload_bps == 19_000_000
        assert measurement.idle_latency_ms == 10.5
        assert measurement.jitter_ms == 1.234
        assert measurement.download_latency_ms == 15.25
        assert measurement.upload_latency_ms == 18.75
        assert measurement.packet_loss_percent == 0
        assert measurement.server_id == "12345"
        assert measurement.server_name == "Example Telecom"
        assert measurement.server_host == "speedtest.example.net"
        assert measurement.server_city == "Manchester"
        assert measurement.server_country == "United Kingdom"
        assert measurement.isp_name == "Example Internet"
        assert measurement.external_ip == "203.0.113.42"
        assert measurement.interface_name == "enp3s0"
        assert measurement.engine_version == "1.2.0"

    def test_keeps_the_raw_result_for_diagnosis(self):
        measurement = parse_ookla_json(json.dumps(SUCCESS_RESULT))
        assert json.loads(measurement.raw_result_json)["server"]["id"] == 12345

    def test_reads_the_result_when_progress_lines_precede_it(self):
        noisy = "Speedtest by Ookla\nRetrieving configuration...\n" + json.dumps(SUCCESS_RESULT)
        measurement = parse_ookla_json(noisy)
        assert measurement.download_bps == 95_000_000


class TestOptionalFieldsMissing:
    def test_absent_fields_become_none_not_zero(self):
        measurement = parse_ookla_json(json.dumps(SPARSE_RESULT))

        assert measurement.download_bps == 10_000_000
        assert measurement.idle_latency_ms == 42.0
        # Nothing the engine did not report may be invented.
        assert measurement.jitter_ms is None
        assert measurement.packet_loss_percent is None
        assert measurement.download_latency_ms is None
        assert measurement.upload_latency_ms is None
        assert measurement.server_name is None
        assert measurement.server_id is None
        assert measurement.isp_name is None
        assert measurement.external_ip is None


class TestZeroIsNotMissing:
    def test_measured_zeros_are_preserved(self):
        measurement = parse_ookla_json(json.dumps(ZERO_RESULT))

        assert measurement.download_bps == 0
        assert measurement.upload_bps == 0
        assert measurement.idle_latency_ms == 0.0
        assert measurement.jitter_ms == 0.0
        assert measurement.packet_loss_percent == 0.0

    def test_zero_and_missing_are_distinguishable_after_parsing(self):
        zeros = parse_ookla_json(json.dumps(ZERO_RESULT))
        sparse = parse_ookla_json(json.dumps(SPARSE_RESULT))

        assert zeros.packet_loss_percent == 0.0
        assert sparse.packet_loss_percent is None
        assert zeros.packet_loss_percent is not None

    @pytest.mark.parametrize("value", [0, 0.0, "0"])
    def test_coercion_preserves_zero(self, value):
        assert coerce_float(value) == 0.0
        assert coerce_int(value) == 0

    @pytest.mark.parametrize("value", [None, "", "  ", "not a number"])
    def test_coercion_returns_none_for_missing(self, value):
        assert coerce_float(value) is None
        assert coerce_int(value) is None

    def test_booleans_are_not_treated_as_numbers(self):
        # A JSON true must never become a measurement of 1.
        assert coerce_float(True) is None
        assert coerce_int(False) is None


class TestMalformedOutput:
    def test_human_formatted_text_is_rejected(self):
        with pytest.raises(ParseError):
            parse_ookla_json("Speedtest by Ookla\n  Download: 95.00 Mbps")

    def test_empty_output_is_rejected(self):
        with pytest.raises(ParseError):
            parse_ookla_json("")

    def test_json_that_is_not_an_object_is_rejected(self):
        with pytest.raises(ParseError):
            parse_ookla_json("[1, 2, 3]")

    def test_result_without_any_bandwidth_is_rejected(self):
        with pytest.raises(ParseError):
            parse_ookla_json(json.dumps({"type": "result", "ping": {"latency": 10}}))

    def test_engine_error_payload_is_rejected_with_its_message(self):
        payload = {"type": "log", "error": "Cannot resolve the test server"}
        with pytest.raises(ParseError, match="Cannot resolve"):
            parse_ookla_json(json.dumps(payload))


class TestUnitConversion:
    @pytest.mark.parametrize(
        ("bytes_per_second", "expected_bps"),
        [(0, 0), (1, 8), (1_000_000, 8_000_000), (11_875_000, 95_000_000)],
    )
    def test_bytes_per_second_to_bits(self, bytes_per_second, expected_bps):
        assert bytes_per_second_to_bps(bytes_per_second) == expected_bps

    @pytest.mark.parametrize(
        ("bps", "expected_mbps"),
        [(0, 0.0), (1_000_000, 1.0), (95_000_000, 95.0), (12_345_678, 12.35)],
    )
    def test_bits_per_second_to_megabits(self, bps, expected_mbps):
        assert bps_to_mbps(bps) == expected_mbps

    def test_conversion_of_missing_data_stays_missing(self):
        assert bps_to_mbps(None) is None
        assert bytes_per_second_to_bps(None) is None
