import copy
import unittest

from jmt.innovation_2_spill.ap_executor import aggregate_payload, append_tuple_width, expression_type
from jmt.innovation_2_spill.ap_expr_types import ExpressionTypes, infer
from jmt.innovation_2_spill.ap_plan_model import Context, estimate_plan
from jmt.innovation_2_spill.ap_preflight import sqlglot
from jmt.innovation_2_spill.ap_unique_groups import estimates as unique_groups, apply as apply_unique_groups
from jmt.innovation_2_spill.forecast import ForecastSession


def catalog():
    return {"database": "unit", "relations": [
        {"relname": t, "reltuples": 500000, "relpages": 10000} for t in ("t", "u")],
        "columns": [
            {"schemaname": "public", "tablename": t, "attname": n, "typname": typ,
             "typlen": length, "typbyval": byval, "typalign": align,
             "avg_width": width, "null_frac": null, "n_distinct": 100000}
            for t in ("t", "u") for n, typ, length, byval, align, width, null in [
                ("k", "int4", 4, True, "i", 4, 0),
                ("n", "numeric", -1, False, "i", 8, .1),
                ("f", "float8", 8, True, "d", 8, 0),
                ("s", "text", -1, False, "i", 40, .05),
                ("d", "timestamp", 8, True, "d", 8, 0),
            ]]}


def scan(rows=500000):
    return {"Node Type": "Seq Scan", "Relation Name": "t", "Schema": "public", "Alias": "t",
            "Plan Rows": rows, "Plan Width": 8, "Output": ["t.n"]}


def plan(expression, strategy="Plain", rows=500000, groups=1):
    return {"Plan": {"Node Type": "Aggregate", "Strategy": strategy, "Plan Rows": groups,
                     "Plan Width": 32, "Output": [expression], "Plans": [scan(rows)]}}


@unittest.skipIf(sqlglot is None, "optional pinned SQL parser not installed")
class ExpressionCoverageTest(unittest.TestCase):
    def typ(self, text, types=None):
        types = types or {"k": "int4", "n": "numeric", "f": "float8", "d": "timestamp"}
        return infer(sqlglot.parse_one(text, read="postgres"), lambda c: types.get(c.name))

    def test_arithmetic_casts_each_operand_not_final_cast_regex(self):
        for text, typ in [
            ("k * (1::numeric - n)", "numeric"),
            ("(k::int8 + k)::numeric", "numeric"),
            ("k::float8 + k::int4", "float8"),
            ("k::int4 + k::float8", "float8"),
            ("k::real + k", "float8"),
            ("k::int2 + k::int4", "int4"),
            ("k::int4 % k::int8", "int8"),
            ("f * (1 - n)", "float8"),
            ("d-d", "interval"),
            ("d::date-d::date", "int4"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.typ(text), typ)

    def test_CASE_value_types_ignore_predicate_cast_and_handle_nesting(self):
        self.assertEqual(self.typ(
            "CASE WHEN k::float8>0 THEN n*(1-k) ELSE 0 END"), "numeric")
        self.assertEqual(self.typ(
            "CASE WHEN k>0 THEN CASE WHEN k>2 THEN n ELSE n+1 END ELSE 0 END"), "numeric")
        self.assertIsNone(self.typ("CASE WHEN k>0 THEN n ELSE unknown_fn(k) END"))

    def test_coalesce_nullif_and_null_unknown_literals(self):
        for text, typ in [
            ("coalesce(n,0)", "numeric"), ("coalesce(NULL,n)", "numeric"),
            ("nullif(k,k)", "int4"), ("nullif(k,n)", None),
            ("'1'::numeric*(1-n)", "numeric"), ("n+'literal'", None),
        ]:
            self.assertEqual(self.typ(text), typ, text)

    def test_nested_aggregate_returns_and_statistics_boolean_types(self):
        for text, typ in [
            ("sum(k)", "int8"), ("sum(sum(k))", "numeric"), ("avg(sum(n))", "numeric"),
            ("stddev_samp(k)", "numeric"), ("stddev_pop(f)", "float8"),
            ("bool_and(k>2)", "bool"), ("bool_or(k IS NULL)", "bool"),
        ]:
            self.assertEqual(self.typ(text), typ, text)

    def test_DATE_alias_depends_on_openGauss_database_compatibility(self):
        expression = sqlglot.parse_one("DATE '2000-01-02' - DATE '2000-01-01'", read="postgres")
        self.assertEqual(infer(expression, lambda c: None), "int4")
        self.assertEqual(infer(expression, lambda c: None, date_cast_type="timestamp"), "interval")
        self.assertIsNone(infer(expression, lambda c: None, date_cast_type=None))

    def test_unknown_user_functions_and_scalar_subquery_abstain(self):
        for text in ["unknown_fn(k)", "private.abs(k)", "(SELECT k FROM u)",
                     "k || 's'", "coalesce(n,unknown_fn(k))", "n::private.money"]:
            self.assertIsNone(self.typ(text), text)

    def test_hard_recursion_node_budgets(self):
        self.assertIsNone(infer(sqlglot.parse_one("k+1"), lambda c: "int4", budget=[1]))
        self.assertIsNone(infer(sqlglot.parse_one("k"), lambda c: "int4", depth=33))

    def test_derived_expression_lineage_nested_CTE_and_union(self):
        sql = ("WITH a AS (SELECT k, n*(1-k) AS revenue FROM t), "
               "b AS (SELECT k,sum(revenue) AS total FROM a GROUP BY k) "
               "SELECT avg(x.total) FROM (SELECT total FROM b UNION ALL SELECT n AS total FROM u) x")
        session = ForecastSession(sql, catalog())
        p = plan("avg(x.total)", "Hashed", 100000, 10000)
        result = session.forecast(p, 4)
        self.assertEqual(result["expression_type_evidence"]["resolved_argument_expressions"]["x.total"],
                         "numeric")
        self.assertTrue(result["logical_temp_estimate_complete"])

    def test_derived_types_have_no_invented_statistics(self):
        ast = sqlglot.parse_one("SELECT sum(x.v) FROM (SELECT n*k AS v FROM t) x")
        types = ExpressionTypes(ast, catalog())
        p = plan("sum(x.v)")
        columns = types.derived_columns(p)
        self.assertEqual(columns["x.v"]["typname"], "numeric")
        for field in ("avg_width", "n_distinct", "null_frac"):
            self.assertNotIn(field, columns["x.v"])

    def test_CTE_explicit_column_list_types_unaliased_aggregate_positionally(self):
        sql = ("WITH r(k,total) AS (SELECT k,sum(n*(1-k)) FROM t GROUP BY k) "
               "SELECT max(r.total) FROM r")
        resolver = ExpressionTypes(sqlglot.parse_one(sql), catalog())
        metadata = resolver.derived_columns(plan("max(r.total)"))
        self.assertEqual(metadata["r.total"]["typname"], "numeric")

    def test_scoped_alias_collision_different_types_abstains(self):
        c = catalog()
        c["columns"] = [dict(r, typname="float8") if r["tablename"] == "u" and r["attname"] == "n"
                        else r for r in c["columns"]]
        ast = sqlglot.parse_one(
            "SELECT sum(x.v) FROM (SELECT n AS v FROM t) x "
            "UNION ALL SELECT sum(x.v) FROM (SELECT n AS v FROM u) x")
        types = ExpressionTypes(ast, c)
        self.assertNotIn("x.v", types.derived_columns(plan("sum(x.v)")))

    def test_UNKNOWN_union_arm_cannot_disappear_from_lineage(self):
        ast = sqlglot.parse_one(
            "SELECT sum(x.v) FROM (SELECT n AS v FROM t UNION ALL SELECT mystery(n) AS v FROM u) x")
        types = ExpressionTypes(ast, catalog())
        self.assertNotIn("x.v", types.derived_columns(plan("sum(x.v)")))
        f = ForecastSession(
            "SELECT x.k,sum(x.v) FROM (SELECT k,n AS v FROM t UNION ALL "
            "SELECT k,mystery(n) AS v FROM u) x GROUP BY x.k", catalog()).forecast(
                plan("sum(x.v)", "Hashed", groups=10000), 4)
        self.assertFalse(f["logical_temp_estimate_complete"])
        self.assertIn("x.v", f["expression_type_evidence"]["unresolved_argument_expressions"])

    def test_context_type_evidence_overrides_old_trailing_cast_heuristic(self):
        ctx = Context(expression_types={"unknown_fn(k::int4)": None, "a::int8+b::int4": "int8"})
        self.assertIsNone(expression_type("unknown_fn(k::int4)", ctx))
        self.assertEqual(expression_type("a::int8+b::int4", ctx), "int8")

    def test_plan_expression_parse_cache_hard_size_and_length_limits(self):
        session = ForecastSession("SELECT sum(n) FROM t", catalog())
        session.expression_types.cache = {str(i): None for i in range(1024)}
        f = session.forecast(plan("sum(t.n+t.k)"), 4)
        self.assertIn("t.n+t.k", f["expression_type_evidence"]["unresolved_argument_expressions"])
        self.assertEqual(len(session.expression_types.cache), 1024)


@unittest.skipIf(sqlglot is None, "optional pinned SQL parser not installed")
class InternalAggregateSortCoverageTest(unittest.TestCase):
    def forecast(self, call, wm, strategy="Plain", rows=500000, groups=1):
        sql = "SELECT "+call+" FROM t" + (" GROUP BY k" if strategy == "Sorted" else "")
        return ForecastSession(sql, catalog()).forecast(plan(call, strategy, rows, groups), wm)

    def test_plain_numeric_distinct_uses_byref_datum_not_minimal_tuple(self):
        f = self.forecast("count(DISTINCT t.n)", 4)
        self.assertTrue(f["logical_temp_estimate_complete"])
        s = f["operators"][0]["details"]["internal_aggregate_sorts"][0]
        self.assertFalse(s["datum_layout_evidence"]["datum_byval"])
        self.assertEqual(s["datum_layout_evidence"]["raw_datum_bytes"], 8)
        self.assertGreater(s["write_bytes"], 0)
        self.assertEqual(s["executor_array_layout"]["spilled"], True)

    def test_plain_text_distinct_catalog_width_and_nulls(self):
        f = self.forecast("count(DISTINCT t.s)", 4)
        self.assertTrue(f["logical_temp_estimate_complete"])
        s = f["operators"][0]["details"]["internal_aggregate_sorts"][0]
        self.assertEqual(s["datum_layout_evidence"]["raw_datum_bytes"], 40)
        self.assertEqual(s["datum_layout_evidence"]["null_fraction"], .05)

    def test_ordered_single_same_expression_resolved_without_hidden_extra_columns(self):
        f = self.forecast("sum(t.n ORDER BY t.n)", 4)
        self.assertTrue(f["logical_temp_estimate_complete"])
        self.assertGreater(f["logical_temp_write_bytes_estimate"], 0)
        f = self.forecast("sum(t.n ORDER BY t.k)", 4)
        self.assertFalse(f["logical_temp_estimate_complete"])

    def test_grouped_sort_lifetime_not_whole_input_or_G_times_peak(self):
        small = self.forecast("count(DISTINCT t.k)", 16, "Sorted", groups=100000)
        self.assertTrue(small["logical_temp_estimate_complete"])
        self.assertEqual(small["logical_temp_write_bytes_estimate"], 0)
        s = small["operators"][0]["details"]["internal_aggregate_sorts"][0]
        self.assertIn("bound_fits", s["distribution_source"])
        self.assertLess(s["memory_mib"], 1)

    def test_unknown_skew_stays_partial_when_a_group_may_spill(self):
        f = self.forecast("count(DISTINCT t.k)", 1, "Sorted", groups=100)
        self.assertFalse(f["logical_temp_estimate_complete"])
        self.assertIsNone(f["logical_temp_write_bytes_estimate"])
        self.assertIn("internal_group_size_distribution_unknown", f["uncertainties"])

    def test_catalog_MCV_all_keys_supports_small_group_sorts_without_source_scan(self):
        c = catalog()
        next(x for x in c["columns"] if (x["tablename"], x["attname"]) == ("t", "k")).update(
            n_distinct=2, mcv_freqs=[.8, .2])
        p = plan("count(DISTINCT t.n)", "Sorted", groups=2)
        p["Plan"]["Group By Key"] = ["t.k"]
        f = ForecastSession("SELECT k,count(DISTINCT n) FROM t GROUP BY k", c).forecast(p, 1)
        self.assertTrue(f["logical_temp_estimate_complete"])
        internal = f["operators"][0]["details"]["internal_aggregate_sorts"][0]
        self.assertEqual(internal["distribution_source"], "catalog_all_group_MCV")
        self.assertGreater(internal["write_bytes"], 0)
        next(x for x in c["columns"] if (x["tablename"], x["attname"]) == ("t", "k"))[
            "mcv_freqs"] = [".8", ".2"]
        self.assertTrue(ForecastSession(
            "SELECT k,count(DISTINCT n) FROM t GROUP BY k", c).forecast(p, 1)[
                "logical_temp_estimate_complete"])

    def test_partial_MCV_or_filtered_scan_must_not_claim_full_group_histogram(self):
        for frequencies, filtered in [([.8], False), ([.8, .2], True), ([.8, .1], False)]:
            c = catalog()
            next(x for x in c["columns"] if (x["tablename"], x["attname"]) == ("t", "k")).update(
                n_distinct=2, mcv_freqs=frequencies)
            p = plan("count(DISTINCT t.n)", "Sorted", groups=2)
            p["Plan"]["Group By Key"] = ["t.k"]
            if filtered:
                p["Plan"]["Plans"][0]["Filter"] = "t.n>0"
            f = ForecastSession("SELECT k,count(DISTINCT n) FROM t GROUP BY k", c).forecast(p, 1)
            self.assertFalse(f["logical_temp_estimate_complete"])

    def test_multi_input_record_and_unknown_varwidth_expression_remain_partial(self):
        for expression in ["count(DISTINCT (t.k,t.n))", "count(DISTINCT unknown_fn(t.s))",
                           "count(DISTINCT coalesce(t.s,'x'))"]:
            f = self.forecast(expression, 4)
            self.assertFalse(f["logical_temp_estimate_complete"], expression)

    def test_two_sorts_are_each_charged_their_own_memory_and_IO(self):
        p = plan("count(DISTINCT t.n)")
        p["Plan"]["Output"].append("count(DISTINCT t.s)")
        f = ForecastSession("SELECT count(DISTINCT n),count(DISTINCT s) FROM t", catalog()).forecast(p, 4)
        sorts = f["operators"][0]["details"]["internal_aggregate_sorts"]
        self.assertEqual(len(sorts), 2)
        self.assertEqual(f["logical_temp_write_bytes_estimate"], sum(s["write_bytes"] for s in sorts))

    def test_variance_arrays_and_interval_payloads_not_missing(self):
        ctx = Context(expression_types={"t.k": "int4", "t.f": "float8", "t.d-t.d": "interval"})
        for fn, arg, payload, collect in [
            ("stddev_samp", "t.k", 256, 96), ("var_pop", "t.f", 192, 96),
            ("max", "t.d-t.d", 48, 0),
        ]:
            n, issues, details = aggregate_payload(
                [{"function": fn, "arguments": arg}], ctx, 1000, 10)
            self.assertEqual(n, payload)
            self.assertEqual(details[0]["collect_initial_payload_bytes"], collect)
            self.assertNotIn("aggregate_transition_payload_unresolved", issues)


@unittest.skipIf(sqlglot is None, "optional pinned SQL parser not installed")
class UniqueUnionGroupTest(unittest.TestCase):
    def fixture(self):
        c = catalog()
        c["nonnull_unique_keys"] = [{"schema": "public", "relation": "t", "columns": ["k"]}]
        a, b = scan(200000), scan(300000)
        for branch in (a, b):
            branch["Output"] = ["t.k", "t.n"]
        a["Filter"], b["Filter"] = "t.k<=300000", "t.k>=300001"
        p = plan("sum(t.n)", "Hashed", groups=200)
        p["Plan"]["Group By Key"] = ["t.k"]
        p["Plan"]["Plans"] = [{"Node Type": "Append", "Plan Rows": 500000,
                                "Plan Width": 12, "Plans": [a, b]}]
        return p, c

    def test_disjoint_unique_branches_exactly_one_key_per_input_estimate(self):
        p, c = self.fixture()
        ev = unique_groups(p, c)
        self.assertEqual(ev[""]["groups"], 500000)
        q, effects = apply_unique_groups(p, ev)
        self.assertEqual(q["Plan"]["Plan Rows"], 500000)
        self.assertEqual(p["Plan"]["Plan Rows"], 200)
        self.assertTrue(effects)

    def test_non_key_integer_range_can_prove_row_disjointness(self):
        p, c = self.fixture()
        for i, branch in enumerate(p["Plan"]["Plans"][0]["Plans"]):
            branch["Filter"] = "t.other<=7" if i == 0 else "t.other>=8"
        c["columns"].append({"tablename": "t", "attname": "other", "typname": "int4"})
        self.assertEqual(unique_groups(p, c)[""]["groups"], 500000)

    def test_overlapping_unknown_foreign_or_projected_key_does_not_prove_uniqueness(self):
        for modify in [
            lambda p, c: p["Plan"]["Plans"][0]["Plans"][1].update(Filter="t.k>=300000"),
            lambda p, c: p["Plan"]["Plans"][0]["Plans"][1].update(Filter="abs(t.k)>300000"),
            lambda p, c: c.update(nonnull_unique_keys=[]),
            lambda p, c: p["Plan"]["Plans"][0]["Plans"][1].update(Schema="other"),
            lambda p, c: p["Plan"]["Plans"][0]["Plans"][1].update(Output=["t.k/2", "t.n"]),
        ]:
            p, c = self.fixture()
            modify(p, c)
            self.assertFalse(unique_groups(p, c))

    def test_cardinality_propagates_only_through_safe_stream_not_joins(self):
        p, c = self.fixture()
        p = {"Plan": {"Node Type": "Sort", "Plan Rows": 200, "Plan Width": 12,
                       "Plans": [p["Plan"]]}}
        q, effects = apply_unique_groups(p, unique_groups(p, c))
        self.assertEqual(q["Plan"]["Plan Rows"], 500000)
        self.assertEqual(len(effects), 2)

    def test_Append_width_follows_weighted_input_not_widened_plan_header(self):
        p, c = self.fixture()
        append = p["Plan"]["Plans"][0]
        append["Plan Width"] = 90
        append["Plans"][0]["Plan Width"] = 16
        append["Plans"][1]["Plan Width"] = 10
        layout = append_tuple_width(append)
        self.assertAlmostEqual(layout["data_width"], 12.4)
        with self.subTest("group memory unchanged but disk tuple width corrected"):
            ctx = Context.from_catalog(c)
            ctx.expression_types = {"t.n": "numeric"}
            d = estimate_plan(p, 1, ctx)["operators"][0]["details"]
            self.assertAlmostEqual(d["spill_row_bytes_estimate"], 28.4)
        append["Plans"] *= 17
        self.assertIsNone(append_tuple_width(append))  # no partial/truncated width proof

    def test_root_identity_Result_may_carry_Append_width_but_not_recompute_columns(self):
        p, c = self.fixture()
        append = p["Plan"]["Plans"][0]
        append["Plan Width"] = 80
        append["Plans"][0]["Plan Width"] = 16
        append["Plans"][1]["Plan Width"] = 10
        result = {"Node Type": "Result", "Plan Rows": 500000, "Plan Width": 80,
                  "Output": ["t.k", "t.n"], "Plans": [append]}
        root = {"Plan": {"Node Type": "Sort", "Plan Rows": 500000, "Plan Width": 80,
                         "Sort Key": ["t.k"], "Output": ["t.k", "t.n"], "Plans": [result]}}
        sql = ("SELECT k,n FROM (SELECT k,n FROM t WHERE k<300000 "
               "UNION ALL SELECT k,n FROM t WHERE k>=300000) x ORDER BY k")
        resolver = ExpressionTypes(sqlglot.parse_one(sql), c)
        facts = resolver.root_append_projection_width(root)
        self.assertAlmostEqual(facts[""]["data_width"], 12.4)
        result["Output"] = ["((t.k))", "((t.n))"]
        self.assertAlmostEqual(resolver.root_append_projection_width(root)[""]["data_width"], 12.4)
        unsafe = ExpressionTypes(sqlglot.parse_one(sql.replace("SELECT k,n FROM", "SELECT k,n+1 FROM", 1)), c)
        self.assertFalse(unsafe.root_append_projection_width(root))
        root["Plan"]["Plans"][0]["Output"] = ["t.k", "t.n+1"]
        self.assertFalse(resolver.root_append_projection_width(root))


if __name__ == "__main__":
    unittest.main()
