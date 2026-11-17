"""Reusable pre-HAVING G from an offline complete-integer-key hash sample.

All rows of a selected key are included; G=d/p is NOT row-sample NDV divided
by a fraction. Only a plain, unfiltered base scan with a nonnull integer key
is supported. Frequency bins are at most 256 entries; online work is bounded.
"""
import math
import time

from .ap_preflight import canonical, walk

SCHEMA = "jmt.complete-key-base-group-sample/v1"
SOURCE = "pre_execution_complete_key_hash_group_sample"


def summarize(histogram, denominator):
    if (isinstance(denominator, bool) or not isinstance(denominator, int)
            or not 128 <= denominator <= 65536 or denominator & (denominator - 1)
            or not isinstance(histogram, list) or not 1 <= len(histogram) <= 256):
        raise ValueError("bounded nonempty frequency histogram and power-of-two sampling denominator required")
    seen, groups, rows, squared = set(), 0, 0, 0
    for item in histogram:
        n, g = item["jmt_n"], item["sampled_groups"]
        if (isinstance(n, bool) or not isinstance(n, int) or n < 1
                or isinstance(g, bool) or not isinstance(g, int) or g <= 0 or n in seen):
            raise ValueError("invalid complete-key sample frequency bin")
        seen.add(n)
        groups += g
        rows += n * g
        squared += n * n * g
    if groups < 32:
        raise ValueError("too few sampled groups; never claim zero/known G")
    p = 1.0 / denominator
    return {
        "sampled_groups": groups, "estimated_groups": groups / p,
        "estimated_input_rows": rows / p,
        "group_RSE_under_uniform_hash_assumption": math.sqrt((1 - p) / groups),
        "frequency_histogram": [{"jmt_n": item["jmt_n"], "jmt_groups": item["sampled_groups"] / p}
                                for item in histogram],
        "qualification": "complete-key integer hash sample; uniform-hash assumption, "
                         "not row-sample distinct extrapolation or a certified confidence interval",
    }


class KeyGroupSample:
    def __init__(self, bundle, current_watermark_sha256):
        if (bundle.get("schema") != SCHEMA
                or bundle.get("watermark_sha256") != current_watermark_sha256
                or bundle.get("uses_target_execution_labels") is not False):
            raise ValueError("key-group sample source/watermark mismatch")
        if not 0 < bundle["expires_unix"] - bundle["generated_unix"] <= 21600:
            raise ValueError("group sample TTL must be <=6 hours")
        summary = summarize(bundle["histogram"], bundle["denominator"])
        if summary != bundle["summary"]:
            raise ValueError("key-group sample summary differs from raw bins")
        self.bundle, self.summary = bundle, summary
        self._check_time()

    def _check_time(self):
        if not self.bundle["generated_unix"] <= time.time() <= self.bundle["expires_unix"]:
            raise ValueError("key-group sample expired/from future; no clock override")

    def evidence(self, document, catalog):
        self._check_time()
        if catalog["database"] != self.bundle["database"]:
            raise ValueError("key-group sample database mismatch")
        profile = self.bundle["profile"]
        columns = [c for c in catalog["columns"] if c["tablename"] == profile["relation"]
                   and c["attname"] == profile["column"]]
        if (len(columns) != 1 or not columns[0].get("attnotnull")
                or columns[0].get("typname") not in ("int2", "int4", "int8")):
            raise ValueError("sample requires a nonnull integer base group key")
        result = {}
        for path, node in walk(document[0]["Plan"]):
            keys = node.get("Group By Key", node.get("Group Key", []))
            if (node["Node Type"] not in ("Aggregate", "HashAggregate") or not node.get("Filter")
                    or node.get("Grouping Sets") or len(keys) != 1
                    or canonical(keys[0]) != canonical(profile["column"])):
                continue
            child = node
            while len(child.get("Plans", [])) == 1:
                child = child["Plans"][0]
                if child["Node Type"] == "Seq Scan":
                    break
                if child["Node Type"] not in ("Sort", "Hash", "Materialize") or child.get("Filter"):
                    break
            if (child["Node Type"] != "Seq Scan" or child.get("Filter") or child.get("Plans")
                    or child.get("Schema", "public") != profile["schema"]
                    or child["Relation Name"] != profile["relation"]):
                continue
            n = float(child.get("Plan Rows", 0))
            sampled_n = self.summary["estimated_input_rows"]
            if n <= 0 or abs(n - sampled_n) > .2 * sampled_n:
                raise ValueError("base input N differs from key-sample scale; G-only replacement is unsafe")
            result[path] = {
                "source": SOURCE, "estimated_pre_having_groups": self.summary["estimated_groups"],
                "estimated_input_rows": self.summary["estimated_input_rows"],
                "uniform_hash_group_RSE": self.summary["group_RSE_under_uniform_hash_assumption"],
                "frequency_histogram": self.summary["frequency_histogram"],
                "qualification": self.summary["qualification"],
            }
        return result
