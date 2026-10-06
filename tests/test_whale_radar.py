"""Whale Radar 测试：纯标准库 unittest，运行时不联网、不落盘。"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whale_radar.analyzer import AnalyzeError, analyze
from whale_radar.converge import converge
from whale_radar.cycles import cycles
from whale_radar.layering import layering
from whale_radar.ranker import rank
from whale_radar.risk import trace_risk
from whale_radar.tracer import trace
from whale_radar.watch import watch

BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin",
    "whale-radar",
)

BASE_TS = "2026-10-04T10:00:00Z"


def tx(tid, frm, to, amount=1.0, usd=0.0, ts=BASE_TS,
       chain="eth", asset="ETH"):
    return {
        "id": tid,
        "timestamp": ts,
        "chain": chain,
        "asset": asset,
        "from_address": frm,
        "to_address": to,
        "amount": amount,
        "usd_value": usd,
    }


def route(rid, min_score, severity, chains=("*",), assets=("*",),
          target="ops"):
    return {
        "id": rid,
        "min_score": min_score,
        "severity": severity,
        "chains": list(chains),
        "assets": list(assets),
        "target": target,
    }


def payload(transfers, threshold=10000.0, routes=None):
    return {
        "transfers": transfers,
        "whale_threshold_usd": threshold,
        "routes": routes or [],
    }


class GraphTests(unittest.TestCase):
    def test_nodes_edges_aggregated_and_sorted(self):
        data = analyze(payload([
            tx("t2", "B", "A", amount=2.0, usd=200),
            tx("t1", "A", "B", amount=1.0, usd=100),
            tx("t3", "A", "B", amount=3.0, usd=300),
        ]))
        graph = data["graph"]
        self.assertEqual([n["address"] for n in graph["nodes"]], ["A", "B"])
        node_a = graph["nodes"][0]
        self.assertEqual(node_a["sent_usd"], 400)
        self.assertEqual(node_a["received_usd"], 200)
        self.assertEqual(node_a["sent_count"], 2)
        self.assertEqual(node_a["received_count"], 1)
        self.assertEqual(node_a["ids"], ["t1", "t2", "t3"])
        self.assertEqual(
            [(e["from_address"], e["to_address"]) for e in graph["edges"]],
            [("A", "B"), ("B", "A")],
        )
        edge = graph["edges"][0]
        self.assertEqual(edge["amount"], 4.0)
        self.assertEqual(edge["usd_value"], 400)
        self.assertEqual(edge["count"], 2)
        self.assertEqual(edge["ids"], ["t1", "t3"])


class WhaleTests(unittest.TestCase):
    def test_threshold_inclusive_and_id_sorted(self):
        data = analyze(payload([
            tx("t2", "A", "B", usd=10000),
            tx("t1", "C", "D", usd=50000),
            tx("t3", "E", "F", usd=9999.99),
        ], threshold=10000))
        self.assertEqual([w["id"] for w in data["whales"]], ["t1", "t2"])
        # 收录的是原始转账对象。
        self.assertEqual(data["whales"][0]["from_address"], "C")


class ScoreTests(unittest.TestCase):
    def test_value_ratio_and_cap(self):
        data = analyze(payload([
            tx("small", "A", "B", usd=5000),    # 10
            tx("exact", "C", "D", usd=10000),   # 20
            tx("huge", "E", "F", usd=999999),   # capped 40
            tx("zero", "G", "H", usd=0),        # 无 VALUE
        ], threshold=10000))
        by_id = {s["id"]: s for s in data["scores"]}
        self.assertEqual(by_id["small"]["score"], 10)
        self.assertEqual(by_id["small"]["reason"], ["VALUE"])
        self.assertEqual(by_id["exact"]["score"], 20)
        self.assertEqual(by_id["huge"]["score"], 40)
        self.assertEqual(by_id["zero"]["score"], 0)
        self.assertEqual(by_id["zero"]["reason"], [])

    def test_score_sort_desc_then_id(self):
        data = analyze(payload([
            tx("a-low", "A", "B", usd=5000),
            tx("b-low", "C", "D", usd=5000),
            tx("high", "E", "F", usd=50000),
        ], threshold=10000))
        self.assertEqual(
            [s["id"] for s in data["scores"]], ["high", "a-low", "b-low"]
        )

    def test_burst_closed_window_boundary(self):
        transfers = [
            tx("t%d" % (i + 1), "A", "x%d" % i, usd=0,
               ts="2026-10-04T10:00:%02dZ" % (i * 10))
            for i in range(5)
        ]
        # t1=:00 t2=:10 t3=:20 t4=:30 t5=:40 均在 t1 的 3600s 闭窗口内
        data = analyze(payload(transfers))
        by_id = {s["id"]: s for s in data["scores"]}
        self.assertIn("BURST", by_id["t1"]["reason"])

    def test_burst_exactly_3600_seconds_counts(self):
        transfers = [
            tx("t1", "A", "x0", usd=0),
            tx("t2", "A", "x1", usd=0, ts="2026-10-04T10:30:00Z"),
            tx("t3", "A", "x2", usd=0, ts="2026-10-04T10:45:00Z"),
            tx("t4", "A", "x3", usd=0, ts="2026-10-04T10:59:00Z"),
            tx("t5", "A", "x4", usd=0, ts="2026-10-04T11:00:00Z"),
        ]
        data = analyze(payload(transfers))
        by_id = {s["id"]: s for s in data["scores"]}
        self.assertIn("BURST", by_id["t1"]["reason"])

    def test_burst_only_looks_forward_and_other_senders_excluded(self):
        transfers = [
            tx("t1", "A", "x0", usd=0, ts="2026-10-04T12:00:00Z"),
            tx("t2", "A", "x1", usd=0, ts="2026-10-04T11:30:00Z"),
            tx("t3", "Z", "x2", usd=0, ts="2026-10-04T12:30:00Z"),
        ]
        data = analyze(payload(transfers))
        by_id = {s["id"]: s for s in data["scores"]}
        self.assertNotIn("BURST", by_id["t1"]["reason"])

    def test_fan_out_three_recipients(self):
        transfers = [
            tx("f1", "A", "r1", usd=0),
            tx("f2", "A", "r2", usd=0, ts="2026-10-04T10:10:00Z"),
            tx("f3", "A", "r3", usd=0, ts="2026-10-04T10:20:00Z"),
        ]
        data = analyze(payload(transfers))
        by_id = {s["id"]: s for s in data["scores"]}
        self.assertIn("FAN_OUT", by_id["f1"]["reason"])
        self.assertNotIn("BURST", by_id["f1"]["reason"])

    def test_round_trip_reverse_same_asset_equal_amount(self):
        data = analyze(payload([
            tx("out", "A", "B", amount=7.0, asset="USDC",
               usd=0, ts="2026-10-04T10:00:00Z"),
            tx("back", "B", "A", amount=7.0, asset="USDC",
               usd=0, ts="2026-10-04T11:30:00Z"),
            tx("diff", "A", "C", amount=7.0, asset="USDC",
               usd=0, ts="2026-10-04T11:45:00Z"),
        ]))
        by_id = {s["id"]: s for s in data["scores"]}
        self.assertIn("ROUND_TRIP", by_id["out"]["reason"])
        self.assertIn("ROUND_TRIP", by_id["back"]["reason"])
        self.assertNotIn("ROUND_TRIP", by_id["diff"]["reason"])

    def test_round_trip_requires_equal_amount_and_asset(self):
        data = analyze(payload([
            tx("out", "A", "B", amount=7.0, asset="USDC"),
            tx("back", "B", "A", amount=8.0, asset="USDC",
               ts="2026-10-04T11:30:00Z"),
        ]))
        by_id = {s["id"]: s for s in data["scores"]}
        self.assertNotIn("ROUND_TRIP", by_id["out"]["reason"])

    def test_total_clamped_to_100(self):
        transfers = [
            tx("t%d" % i, "A", "r%d" % i, amount=3.0, asset="ETH",
               usd=100000, ts="2026-10-04T10:00:%02dZ" % (i * 5))
            for i in range(5)
        ]
        transfers.append(tx(
            "back", "r0", "A", amount=3.0, asset="ETH", usd=0,
            ts="2026-10-04T12:00:00Z"))
        data = analyze(payload(transfers, threshold=10000))
        top = data["scores"][0]
        self.assertEqual(top["score"], 100)
        self.assertEqual(
            top["reason"], ["VALUE", "BURST", "FAN_OUT", "ROUND_TRIP"]
        )


class AlertTests(unittest.TestCase):
    def test_route_matching_and_sorting(self):
        transfers = [
            tx("eth-hi", "A", "B", usd=50000, chain="eth", asset="ETH"),
            tx("btc-hi", "C", "D", usd=50000, chain="btc", asset="BTC"),
            tx("eth-lo", "E", "F", usd=5000, chain="eth", asset="ETH"),
        ]
        routes = [
            route("r-critical", 40, "critical", chains=("eth",),
                  assets=("*",), target="pager"),
            route("r-any", 40, "warning", target="email"),
            route("r-low", 10, "info", chains=("eth",), assets=("ETH",),
                  target="slack"),
        ]
        data = analyze(payload(transfers, threshold=10000, routes=routes))
        alerts = data["alerts"]
        self.assertEqual(
            [(a["route_id"], a["transfer_id"]) for a in alerts],
            [("r-any", "btc-hi"), ("r-any", "eth-hi"),
             ("r-critical", "eth-hi"),
             ("r-low", "eth-hi"), ("r-low", "eth-lo")],
        )
        first = alerts[0]
        self.assertEqual(set(first),
                         {"route_id", "transfer_id", "severity",
                          "reason", "target"})
        self.assertEqual(first["severity"], "warning")
        self.assertEqual(first["target"], "email")
        self.assertEqual(first["reason"], ["VALUE"])

    def test_min_score_boundary_inclusive(self):
        data = analyze(
            payload([tx("t1", "A", "B", usd=5000)],
                    threshold=10000,
                    routes=[route("r", 10, "info")])
        )
        self.assertEqual(len(data["alerts"]), 1)

    def test_asset_filter_mismatch_no_alert(self):
        data = analyze(
            payload([tx("t1", "A", "B", usd=50000, asset="ETH")],
                    threshold=10000,
                    routes=[route("r", 40, "info", assets=("BTC",))])
        )
        self.assertEqual(data["alerts"], [])


class ValidationTests(unittest.TestCase):
    def assert_code(self, code, obj):
        with self.assertRaises(AnalyzeError) as ctx:
            analyze(obj)
        self.assertEqual(ctx.exception.code, code)

    def test_schema_errors(self):
        self.assert_code("INVALID_INPUT_SCHEMA", {"transfers": []})
        self.assert_code(
            "INVALID_INPUT_SCHEMA",
            {"transfers": [], "whale_threshold_usd": "100", "routes": []},
        )
        self.assert_code(
            "INVALID_INPUT_SCHEMA",
            payload([{"id": "t1"}]),
        )
        self.assert_code(
            "INVALID_INPUT_SCHEMA",
            payload([tx("t1", "A", "B", usd=0) | {"id": ""}]),
        )
        self.assert_code(
            "INVALID_INPUT_SCHEMA",
            payload([], routes=[{"id": "r", "min_score": "10"}]),
        )

    def test_duplicate_id_precedes_bad_value(self):
        bad = tx("dup", "A", "B", ts="not-a-time")
        self.assert_code(
            "DUPLICATE_TRANSFER_ID", payload([tx("dup", "A", "B"), bad])
        )

    def test_transfer_values(self):
        self.assert_code(
            "INVALID_TRANSFER_VALUE",
            payload([tx("t", "A", "B", ts="2026-10-04 10:00:00")]),
        )
        self.assert_code(
            "INVALID_TRANSFER_VALUE",
            payload([tx("t", "A", "B", amount=0)]),
        )
        self.assert_code(
            "INVALID_TRANSFER_VALUE",
            payload([tx("t", "A", "B", amount=-1, usd=0)]),
        )
        self.assert_code(
            "INVALID_TRANSFER_VALUE",
            payload([tx("t", "A", "B", usd=-0.01)]),
        )

    def test_threshold_precedes_route(self):
        self.assert_code(
            "INVALID_THRESHOLD",
            payload([], threshold=0,
                    routes=[route("r", 200, "info")]),
        )

    def test_route_errors(self):
        self.assert_code(
            "INVALID_ROUTE", payload([], routes=[route("r", 101, "info")])
        )
        self.assert_code(
            "INVALID_ROUTE", payload([], routes=[route("r", -1, "info")])
        )
        self.assert_code(
            "INVALID_ROUTE", payload([], routes=[route("r", 10, "urgent")])
        )
        self.assert_code(
            "INVALID_ROUTE",
            payload([], routes=[route("r", 10, "info", chains=())]),
        )
        self.assert_code(
            "INVALID_ROUTE",
            payload([], routes=[route("r", 10, "info", assets=())]),
        )
        self.assert_code(
            "INVALID_ROUTE",
            payload([], routes=[route("r", 10, "info", chains=(""))]),
        )
        self.assert_code(
            "INVALID_ROUTE",
            payload([], routes=[
                {**route("r", 10, "info"), "target": ""}
            ]),
        )


class CliTests(unittest.TestCase):
    def run_cli(self, raw):
        proc = subprocess.run(
            [BIN, "analyze"], input=raw, capture_output=True, text=True
        )
        return proc

    def test_success_stdout(self):
        proc = self.run_cli(json.dumps(payload([tx("t1", "A", "B", usd=1)])))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIn("data", out)
        self.assertEqual(set(out["data"]),
                         {"graph", "whales", "scores", "alerts"})
        self.assertEqual(proc.stderr, "")

    def test_not_json(self):
        proc = self.run_cli("{not json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr), {"error": "INPUT_NOT_JSON"})

    def test_error_codes_via_cli(self):
        proc = self.run_cli(json.dumps({"transfers": []}))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_INPUT_SCHEMA"}
        )


class TraceTests(unittest.TestCase):
    def query(self, transfers, start="A", end="D", max_hops=4,
              chain="eth", asset="ETH"):
        return {
            "transfers": transfers,
            "chain": chain,
            "asset": asset,
            "start_address": start,
            "end_address": end,
            "max_hops": max_hops,
        }

    def test_direct_and_multi_hop_paths(self):
        data = trace(self.query([
            tx("t1", "A", "B", amount=1.0, usd=10),
            tx("t2", "B", "D", amount=2.0, usd=20),
            tx("t3", "A", "D", amount=5.0, usd=50),
        ]))
        paths = data["paths"]
        self.assertEqual([p["transfer_ids"] for p in paths], [["t3"], ["t1", "t2"]])
        direct = paths[0]
        self.assertEqual(direct["nodes"], ["A", "D"])
        self.assertEqual(direct["hops"], 1)
        self.assertEqual(direct["amount"], 5)
        self.assertEqual(direct["usd_value"], 50)
        relay = paths[1]
        self.assertEqual(relay["nodes"], ["A", "B", "D"])
        self.assertEqual(relay["hops"], 2)
        self.assertEqual(relay["amount"], 3)
        self.assertEqual(relay["usd_value"], 30)

    def test_chain_and_asset_must_match_exactly(self):
        data = trace(self.query([
            tx("t1", "A", "D", chain="bsc"),
            tx("t2", "A", "D", asset="USDC"),
            tx("t3", "A", "D"),
        ]))
        self.assertEqual([p["transfer_ids"] for p in data["paths"]], [["t3"]])

    def test_no_path_returns_empty_list(self):
        data = trace(self.query([tx("t1", "A", "B")], end="Z"))
        self.assertEqual(data["paths"], [])

    def test_simple_paths_only_no_repeated_addresses(self):
        # 环 B->C->B 不可用于延长路径；A->B->C->D 是唯一多跳路径。
        data = trace(self.query([
            tx("t1", "A", "B"),
            tx("t2", "B", "C"),
            tx("t3", "C", "B"),
            tx("t4", "C", "D"),
        ]))
        self.assertEqual(
            [p["transfer_ids"] for p in data["paths"]], [["t1", "t2", "t4"]]
        )

    def test_max_hops_limits_depth(self):
        transfers = [
            tx("t1", "A", "B"),
            tx("t2", "B", "C"),
            tx("t3", "C", "D"),
        ]
        data = trace(self.query(transfers, max_hops=2))
        self.assertEqual(data["paths"], [])
        data = trace(self.query(transfers, max_hops=3))
        self.assertEqual(len(data["paths"]), 1)

    def test_parallel_edges_yield_distinct_paths_sorted(self):
        data = trace(self.query([
            tx("t2", "A", "D", amount=2.0),
            tx("t1", "A", "D", amount=1.0),
        ]))
        self.assertEqual(
            [p["transfer_ids"] for p in data["paths"]], [["t1"], ["t2"]]
        )

    def test_hops_then_transfer_ids_then_nodes_ordering(self):
        data = trace(self.query([
            tx("t9", "A", "D"),
            tx("t1", "A", "B"),
            tx("t2", "B", "D"),
            tx("t3", "A", "C"),
            tx("t4", "C", "D"),
        ]))
        self.assertEqual(
            [p["transfer_ids"] for p in data["paths"]],
            [["t9"], ["t1", "t2"], ["t3", "t4"]],
        )

    def test_amount_usd_round10_representation(self):
        data = trace(self.query([
            tx("t1", "A", "B", amount=0.1, usd=0.2),
            tx("t2", "B", "D", amount=0.2, usd=0.1),
        ]))
        path = data["paths"][0]
        self.assertEqual(path["amount"], 0.3)
        self.assertEqual(path["usd_value"], 0.3)

    def assert_trace_code(self, code, obj):
        with self.assertRaises(AnalyzeError) as ctx:
            trace(obj)
        self.assertEqual(ctx.exception.code, code)

    def test_schema_errors(self):
        self.assert_trace_code("INVALID_INPUT_SCHEMA", [])
        self.assert_trace_code("INVALID_INPUT_SCHEMA", {})
        self.assert_trace_code("INVALID_INPUT_SCHEMA", {"transfers": "x"})
        bad = self.query([{"id": "t1"}])
        self.assert_trace_code("INVALID_INPUT_SCHEMA", bad)

    def test_duplicate_id_precedes_value_and_query_errors(self):
        bad = self.query([tx("dup", "A", "B"), tx("dup", "A", "B", ts="bad")])
        del bad["chain"]
        self.assert_trace_code("DUPLICATE_TRANSFER_ID", bad)

    def test_value_error_precedes_query_error(self):
        bad = self.query([tx("t1", "A", "B", amount=0)])
        bad["max_hops"] = 0
        self.assert_trace_code("INVALID_TRANSFER_VALUE", bad)
        bad = self.query([tx("t1", "A", "B", ts="2026-10-04 10:00:00")])
        self.assert_trace_code("INVALID_TRANSFER_VALUE", bad)
        bad = self.query([tx("t1", "A", "B", usd=-1)])
        self.assert_trace_code("INVALID_TRANSFER_VALUE", bad)

    def test_query_errors(self):
        base = [tx("t1", "A", "D")]
        for field in ("chain", "asset", "start_address",
                      "end_address", "max_hops"):
            bad = self.query(base)
            del bad[field]
            self.assert_trace_code("INVALID_TRACE_QUERY", bad)
        self.assert_trace_code(
            "INVALID_TRACE_QUERY", self.query(base) | {"chain": 1}
        )
        self.assert_trace_code(
            "INVALID_TRACE_QUERY", self.query(base) | {"start_address": ""}
        )
        self.assert_trace_code(
            "INVALID_TRACE_QUERY", self.query(base, start="A", end="A")
        )
        for bad_hops in (0, 9, 2.5, "3", True):
            self.assert_trace_code(
                "INVALID_TRACE_QUERY", self.query(base, max_hops=bad_hops)
            )


class TraceCliTests(unittest.TestCase):
    def run_cli(self, raw):
        return subprocess.run(
            [BIN, "trace"], input=raw, capture_output=True, text=True
        )

    def test_success_stdout(self):
        payload = {
            "transfers": [tx("t1", "A", "D", usd=1)],
            "chain": "eth",
            "asset": "ETH",
            "start_address": "A",
            "end_address": "D",
            "max_hops": 3,
        }
        proc = self.run_cli(json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(set(out), {"data"})
        self.assertEqual(set(out["data"]), {"paths"})
        self.assertEqual(out["data"]["paths"][0]["transfer_ids"], ["t1"])
        self.assertEqual(proc.stderr, "")

    def test_not_json(self):
        proc = self.run_cli("{not json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr), {"error": "INPUT_NOT_JSON"})

    def test_error_code_via_cli(self):
        proc = self.run_cli(json.dumps({"transfers": []}))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_TRACE_QUERY"}
        )


class TraceRiskTests(unittest.TestCase):
    def risk_query(self, transfers, routes=None, start="A", end="D",
                   max_hops=4, chain="eth", asset="ETH", threshold=10000.0):
        result = payload(transfers, threshold=threshold, routes=routes)
        result.update({
            "chain": chain,
            "asset": asset,
            "start_address": start,
            "end_address": end,
            "max_hops": max_hops,
        })
        return result

    def test_path_fields_and_segment_sum_with_path_id(self):
        data = trace_risk(self.risk_query([
            tx("t1", "A", "B", usd=50000),   # 40 VALUE
            tx("t2", "B", "D", usd=5000),    # 10 VALUE
            tx("t3", "A", "D", usd=50000),   # 40 VALUE
        ]))
        self.assertEqual(set(data), {"paths", "alerts"})
        by_id = {p["path_id"]: p for p in data["paths"]}
        self.assertEqual(set(by_id), {"t1>t2", "t3"})
        multi = by_id["t1>t2"]
        self.assertEqual(
            set(multi),
            {"nodes", "transfer_ids", "hops", "amount", "usd_value",
             "score", "reason", "path_id", "segments"},
        )
        self.assertEqual(multi["nodes"], ["A", "B", "D"])
        self.assertEqual(multi["transfer_ids"], ["t1", "t2"])
        self.assertEqual(multi["hops"], 2)
        self.assertEqual(multi["amount"], 2)
        self.assertEqual(multi["usd_value"], 55000)
        self.assertEqual(multi["score"], 50)
        self.assertEqual(multi["reason"], ["VALUE"])
        self.assertEqual(
            multi["segments"],
            [
                {"transfer_id": "t1", "score": 40, "reason": ["VALUE"]},
                {"transfer_id": "t2", "score": 10, "reason": ["VALUE"]},
            ],
        )
        self.assertEqual(by_id["t3"]["score"], 40)
        self.assertEqual(
            by_id["t3"]["segments"],
            [{"transfer_id": "t3", "score": 40, "reason": ["VALUE"]}],
        )

    def test_score_sorted_desc_then_hops_then_transfer_ids(self):
        data = trace_risk(self.risk_query([
            tx("lo", "A", "D", usd=5000),             # 10，1 跳
            tx("hi1", "A", "B", usd=50000),           # 40
            tx("hi2", "B", "D", usd=50000),           # 40，合计 80，2 跳
        ]))
        self.assertEqual(
            [p["path_id"] for p in data["paths"]], ["hi1>hi2", "lo"]
        )

    def test_score_tie_breaks_by_hops(self):
        data = trace_risk(self.risk_query([
            tx("d", "A", "D", usd=10000),             # 20，1 跳
            tx("m1", "A", "B", usd=5000),             # 10
            tx("m2", "B", "D", usd=5000),             # 10，合计 20，2 跳
        ]))
        self.assertEqual(
            [p["path_id"] for p in data["paths"]], ["d", "m1>m2"]
        )

    def test_score_capped_at_100(self):
        transfers = [
            tx("w", "A", "B", amount=3.0, asset="ETH", usd=100000),
            tx("c2", "A", "x2", usd=1, ts="2026-10-04T10:05:00Z"),
            tx("c3", "A", "x3", usd=1, ts="2026-10-04T10:10:00Z"),
            tx("c4", "A", "x4", usd=1, ts="2026-10-04T10:20:00Z"),
            tx("c5", "A", "x5", usd=1, ts="2026-10-04T10:25:00Z"),
            tx("last", "B", "D", usd=100000),
        ]
        data = trace_risk(self.risk_query(transfers))
        path = next(p for p in data["paths"] if p["path_id"] == "w>last")
        # w=VALUE+BURST+FAN_OUT=85，last=40，合计截断 100。
        self.assertEqual(path["score"], 100)

    def test_reason_merged_dedup_in_fixed_order(self):
        data = trace_risk(self.risk_query([
            tx("rt", "B", "A", amount=2.0, asset="ETH", usd=0,
               ts="2026-10-04T11:30:00Z"),
            tx("s1", "A", "B", amount=2.0, asset="ETH", usd=50000),
            tx("s2", "B", "D", usd=50000),
        ]))
        path = next(p for p in data["paths"] if p["path_id"] == "s1>s2")
        # s1 同时有 VALUE 与 ROUND_TRIP；s2 仅 VALUE；合并去重且按固定顺序。
        self.assertEqual(path["reason"], ["VALUE", "ROUND_TRIP"])

    def test_alerts_one_per_route_path_and_sorted(self):
        routes = [
            route("r2", 40, "warning", chains=("eth",), target="email"),
            route("r1", 40, "critical", chains=("eth",), assets=("ETH",),
                  target="pager"),
            route("r-btc", 40, "info", chains=("btc",), target="x"),
        ]
        data = trace_risk(self.risk_query([
            tx("t1", "A", "B", usd=50000),
            tx("t2", "B", "D", usd=50000),
            tx("t3", "A", "D", usd=50000),
        ], routes=routes))
        alerts = data["alerts"]
        self.assertEqual(
            [(a["route_id"], a["path_id"]) for a in alerts],
            [("r1", "t1>t2"), ("r1", "t3"),
             ("r2", "t1>t2"), ("r2", "t3")],
        )
        first = alerts[0]
        self.assertEqual(
            set(first),
            {"route_id", "path_id", "severity", "score", "reason", "target"},
        )
        self.assertEqual(first["severity"], "critical")
        self.assertEqual(first["target"], "pager")
        self.assertEqual(first["score"], 80)
        self.assertEqual(first["reason"], ["VALUE"])

    def test_alert_min_score_boundary_inclusive(self):
        data = trace_risk(self.risk_query(
            [tx("t1", "A", "D", usd=5000)],   # 恰好 10
            routes=[route("r", 10, "info")],
        ))
        self.assertEqual(
            [(a["route_id"], a["path_id"]) for a in data["alerts"]],
            [("r", "t1")],
        )

    def test_alert_asset_star_and_mismatch(self):
        data = trace_risk(self.risk_query(
            [tx("t1", "A", "D", usd=50000)],
            routes=[route("r", 40, "info", assets=("BTC",))],
        ))
        self.assertEqual(data["alerts"], [])

    def test_no_path_no_alerts(self):
        data = trace_risk(self.risk_query(
            [tx("t1", "A", "B", usd=50000)],
            routes=[route("r", 0, "info")], end="Z",
        ))
        self.assertEqual(data, {"paths": [], "alerts": []})

    def assert_risk_code(self, code, obj):
        with self.assertRaises(AnalyzeError) as ctx:
            trace_risk(obj)
        self.assertEqual(ctx.exception.code, code)

    def test_error_precedence(self):
        self.assert_risk_code("INVALID_INPUT_SCHEMA", {})
        self.assert_risk_code("INVALID_INPUT_SCHEMA", {"transfers": []})
        # threshold -> route -> query。
        bad = self.risk_query([], threshold=0,
                              routes=[route("r", 200, "info")])
        del bad["chain"]
        self.assert_risk_code("INVALID_THRESHOLD", bad)
        bad = self.risk_query([], routes=[route("r", 200, "info")])
        del bad["chain"]
        self.assert_risk_code("INVALID_ROUTE", bad)
        bad = self.risk_query([])
        del bad["chain"]
        self.assert_risk_code("INVALID_TRACE_QUERY", bad)

    def test_duplicate_and_value_precedence(self):
        bad = self.risk_query(
            [tx("dup", "A", "B"), tx("dup", "B", "D", ts="bad")])
        self.assert_risk_code("DUPLICATE_TRANSFER_ID", bad)
        bad = self.risk_query([tx("t1", "A", "D", amount=0)])
        self.assert_risk_code("INVALID_TRANSFER_VALUE", bad)


class TraceRiskCliTests(unittest.TestCase):
    def run_cli(self, raw):
        return subprocess.run(
            [BIN, "trace-risk"], input=raw, capture_output=True, text=True
        )

    def test_success_stdout(self):
        body = {
            "transfers": [tx("t1", "A", "D", usd=1)],
            "whale_threshold_usd": 10000,
            "routes": [],
            "chain": "eth",
            "asset": "ETH",
            "start_address": "A",
            "end_address": "D",
            "max_hops": 3,
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(set(out), {"data"})
        self.assertEqual(set(out["data"]), {"paths", "alerts"})
        self.assertEqual(out["data"]["paths"][0]["path_id"], "t1")
        self.assertEqual(proc.stderr, "")

    def test_not_json(self):
        proc = self.run_cli("{not json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr), {"error": "INPUT_NOT_JSON"})

    def test_error_code_via_cli(self):
        proc = self.run_cli(json.dumps({"transfers": []}))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_INPUT_SCHEMA"}
        )


class RankTests(unittest.TestCase):
    def test_aggregation_net_and_self_transfer(self):
        data = rank(payload([
            tx("o1", "A", "B", usd=100),
            tx("o2", "A", "B", usd=50),
            tx("i1", "B", "A", usd=30),
            tx("s1", "S", "S", usd=90000),
        ], threshold=10000))
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertEqual(prof["A"]["sent_usd"], 150)
        self.assertEqual(prof["A"]["received_usd"], 30)
        self.assertEqual(prof["A"]["net_usd"], 120)
        # S 自转账：同时计入发起与收到，相反地址为自身。
        self.assertEqual(prof["S"]["sent_usd"], 90000)
        self.assertEqual(prof["S"]["received_usd"], 90000)
        self.assertEqual(prof["S"]["net_usd"], 0)
        self.assertEqual(prof["S"]["whale_transfers"], 1)
        self.assertEqual(prof["S"]["counterparties"], 1)

    def test_whale_exposure_touches_both_sides_inclusive(self):
        data = rank(payload([
            tx("lo", "A", "B", usd=9999.99),
            tx("hi", "C", "D", usd=10000),
        ], threshold=10000))
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertEqual(prof["A"]["whale_transfers"], 0)
        self.assertEqual(prof["B"]["whale_transfers"], 0)
        self.assertEqual(prof["C"]["whale_transfers"], 1)
        self.assertEqual(prof["D"]["whale_transfers"], 1)
        self.assertEqual(prof["C"]["risk_score"], 40)
        self.assertEqual(prof["C"]["reasons"], ["WHALE_EXPOSURE"])

    def test_counterparty_distribution(self):
        transfers = [
            tx("t%d" % i, "A", "r%d" % i, usd=1,
               ts="2026-10-04T10:%02d:00Z" % (i * 10))
            for i in range(3)
        ]
        data = rank(payload(transfers))
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertEqual(prof["A"]["counterparties"], 3)
        self.assertIn("COUNTERPARTY_DISTRIBUTION", prof["A"]["reasons"])
        self.assertNotIn("COUNTERPARTY_DISTRIBUTION",
                         prof["r0"]["reasons"])  # 收款方仅 1 个相反地址

    def test_round_trip_both_sides_self_transfer_excluded(self):
        data = rank(payload([
            tx("out", "A", "B", amount=7.0, asset="USDC"),
            tx("back", "B", "A", amount=7.0, asset="USDC",
               ts="2026-10-04T11:00:00Z"),
            tx("self", "C", "C", amount=7.0, asset="USDC",
               ts="2026-10-04T11:30:00Z"),
        ]))
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertIn("ROUND_TRIP_ACTIVITY", prof["A"]["reasons"])
        self.assertIn("ROUND_TRIP_ACTIVITY", prof["B"]["reasons"])
        self.assertNotIn("ROUND_TRIP_ACTIVITY", prof["C"]["reasons"])

    def test_round_trip_requires_same_asset_equal_amount(self):
        data = rank(payload([
            tx("out", "A", "B", amount=7.0, asset="USDC"),
            tx("back", "B", "A", amount=8.0, asset="USDC",
               ts="2026-10-04T11:00:00Z"),
        ]))
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertNotIn("ROUND_TRIP_ACTIVITY", prof["A"]["reasons"])

    def test_burst_closed_window_from_send_times_only(self):
        transfers = [
            tx("t1", "A", "x0", usd=1),
            tx("t2", "A", "x1", usd=1, ts="2026-10-04T10:30:00Z"),
            tx("t3", "A", "x2", usd=1, ts="2026-10-04T10:45:00Z"),
            tx("t4", "A", "x3", usd=1, ts="2026-10-04T10:59:00Z"),
            tx("t5", "A", "x4", usd=1, ts="2026-10-04T11:00:00Z"),
        ]
        data = rank(payload(transfers))
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertIn("BURST_ACTIVITY", prof["A"]["reasons"])
        # 收款时间不参与 burst：x0 等地址均无 BURST。
        self.assertNotIn("BURST_ACTIVITY", prof["x0"]["reasons"])

    def test_burst_needs_five_inside_window(self):
        transfers = [
            tx("t1", "A", "x0", usd=1),
            tx("t2", "A", "x1", usd=1, ts="2026-10-04T10:20:00Z"),
            tx("t3", "A", "x2", usd=1, ts="2026-10-04T10:40:00Z"),
            tx("t4", "A", "x3", usd=1, ts="2026-10-04T11:00:00Z"),
            tx("t5", "A", "x4", usd=1, ts="2026-10-04T11:20:00Z"),
        ]
        # 10:00 起 3600s 闭区间内只有 4 笔；11:20 的第 5 笔在窗口外。
        data = rank(payload(transfers))
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertNotIn("BURST_ACTIVITY", prof["A"]["reasons"])

    def test_score_sum_and_clamp_and_reason_order(self):
        transfers = [
            tx("w", "A", "B", amount=3.0, asset="ETH", usd=100000),
            tx("c2", "A", "C", usd=1, ts="2026-10-04T10:05:00Z"),
            tx("c3", "A", "D", usd=1, ts="2026-10-04T10:10:00Z"),
            tx("b2", "B", "A", amount=3.0, asset="ETH", usd=1,
               ts="2026-10-04T10:15:00Z"),
            tx("c4", "A", "E", usd=1, ts="2026-10-04T10:20:00Z"),
            tx("c5", "A", "F", usd=1, ts="2026-10-04T10:25:00Z"),
        ]
        data = rank(payload(transfers, threshold=10000))
        prof = {p["address"]: p for p in data["profiles"]}
        # 40+25+20+15=100，截断在 100。
        self.assertEqual(prof["A"]["risk_score"], 100)
        self.assertEqual(
            prof["A"]["reasons"],
            ["WHALE_EXPOSURE", "COUNTERPARTY_DISTRIBUTION",
             "ROUND_TRIP_ACTIVITY", "BURST_ACTIVITY"],
        )

    def test_profiles_sorted_score_desc_address_asc(self):
        data = rank(payload([
            tx("w1", "Z", "x1", usd=100000),
            tx("w2", "A", "x2", usd=100000),
            tx("w3", "M", "x3", usd=100000),
        ], threshold=10000))
        self.assertEqual(
            [(p["address"], p["risk_score"]) for p in data["profiles"][:3]],
            [("A", 40), ("M", 40), ("Z", 40)],
        )

    def test_alerts_one_per_route_address_and_sorting(self):
        routes = [
            route("r2", 40, "warning", chains=("eth",), target="email"),
            route("r1", 40, "critical", chains=("eth",), assets=("ETH",),
                  target="pager"),
        ]
        data = rank(payload([
            tx("w1", "A", "B", usd=50000, chain="eth", asset="ETH"),
            tx("w2", "A", "B", usd=50000, chain="eth", asset="ETH",
               ts="2026-10-04T10:30:00Z"),
            tx("w3", "C", "D", usd=50000, chain="btc", asset="BTC"),
        ], threshold=10000, routes=routes))
        alerts = data["alerts"]
        # 每对 (route, address) 至多一项；多笔匹配不重复。
        self.assertEqual(
            [(a["route_id"], a["address"]) for a in alerts],
            [("r1", "A"), ("r1", "B"),
             ("r2", "A"), ("r2", "B")],
        )
        first = alerts[0]
        self.assertEqual(set(first),
                         {"route_id", "address", "severity",
                          "reason", "target"})
        self.assertEqual(first["target"], "pager")
        # C/D 的触及转账在 btc 上，不命中 eth 路由。
        self.assertFalse(
            any(a["address"] in ("C", "D") for a in alerts)
        )

    def test_alert_match_requires_single_transfer_hit_both(self):
        data = rank(payload([
            tx("e", "P", "Q", usd=50000, chain="eth", asset="ETH"),
            tx("u", "P", "Q", usd=50000, chain="bsc", asset="USDC"),
        ], threshold=10000, routes=[
            route("r", 40, "info", chains=("eth",), assets=("USDC",))
        ]))
        self.assertEqual(data["alerts"], [])

    def test_alert_min_score_inclusive(self):
        data = rank(payload([tx("w", "A", "B", usd=10000)], threshold=10000,
                            routes=[route("r", 40, "info")]))
        self.assertEqual([a["address"] for a in data["alerts"]], ["A", "B"])

    def test_alert_star_matches_any(self):
        data = rank(payload([
            tx("w", "A", "B", usd=50000, chain="eth", asset="ETH"),
        ], threshold=10000, routes=[route("r", 40, "info")]))
        self.assertEqual(len(data["alerts"]), 2)

    def test_error_codes_reuse_analyze_pipeline(self):
        def assert_code(code, obj):
            with self.assertRaises(AnalyzeError) as ctx:
                rank(obj)
            self.assertEqual(ctx.exception.code, code)

        assert_code("INVALID_INPUT_SCHEMA", {"transfers": []})
        assert_code(
            "INVALID_INPUT_SCHEMA",
            payload([{"id": "t1"}]),
        )
        assert_code(
            "DUPLICATE_TRANSFER_ID",
            payload([tx("dup", "A", "B"),
                     tx("dup", "A", "B", ts="bad")]),
        )
        assert_code(
            "INVALID_TRANSFER_VALUE",
            payload([tx("t", "A", "B", amount=0)]),
        )
        assert_code(
            "INVALID_THRESHOLD",
            payload([], threshold=0, routes=[route("r", 50, "info")]),
        )
        assert_code(
            "INVALID_ROUTE",
            payload([], routes=[route("r", 101, "info")]),
        )


class RankCliTests(unittest.TestCase):
    def run_cli(self, raw):
        return subprocess.run(
            [BIN, "rank"], input=raw, capture_output=True, text=True
        )

    def test_success_stdout(self):
        proc = self.run_cli(json.dumps(
            payload([tx("t1", "A", "B", usd=10000)])))
        self.assertEqual(proc.returncode == 0, True)
        out = json.loads(proc.stdout)
        self.assertEqual(set(out), {"data"})
        self.assertEqual(set(out["data"]), {"profiles", "alerts"})
        self.assertEqual(proc.stderr, "")

    def test_not_json(self):
        proc = self.run_cli("{not json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr),
                         {"error": "INPUT_NOT_JSON"})

    def test_error_code_via_cli(self):
        proc = self.run_cli(json.dumps({"transfers": []}))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr),
                         {"error": "INVALID_INPUT_SCHEMA"})


class ScoringAnalyzeTests(unittest.TestCase):
    def with_scoring(self, transfers, scoring, threshold=10000.0, routes=None):
        body = payload(transfers, threshold=threshold, routes=routes)
        body["scoring"] = scoring
        return analyze(body)

    def test_ratio_and_cap_custom(self):
        data = self.with_scoring(
            [tx("big", "A", "B", usd=10000)],
            {"value_points_per_ratio": 50, "value_points_cap": 30},
        )
        item = data["scores"][0]
        self.assertEqual(item["score"], 30)
        self.assertEqual(item["reason"], ["VALUE"])

    def test_ratio_zero_suppresses_value(self):
        data = self.with_scoring(
            [tx("big", "A", "B", usd=999999)],
            {"value_points_per_ratio": 0},
        )
        item = data["scores"][0]
        self.assertEqual(item["score"], 0)
        self.assertEqual(item["reason"], [])

    def test_burst_count_and_points_custom(self):
        transfers = [
            tx("t%d" % i, "A", "r%d" % i, usd=0,
               ts="2026-10-04T10:%02d:00Z" % (i * 10))
            for i in range(3)
        ]
        data = self.with_scoring(
            transfers,
            {"burst_count": 3, "transfer_burst_points": 7,
             "fan_out_recipients": 100},
        )
        by_id = {s["id"]: s for s in data["scores"]}
        self.assertEqual(by_id["t0"]["score"], 7)
        self.assertEqual(by_id["t0"]["reason"], ["BURST"])

    def test_window_seconds_closed_interval(self):
        transfers = [
            tx("t1", "A", "x0", usd=0),
            tx("t2", "A", "x1", usd=0, ts="2026-10-04T10:10:00Z"),
        ]
        data = self.with_scoring(
            transfers, {"window_seconds": 600, "burst_count": 2,
                        "fan_out_recipients": 100}
        )
        by_id = {s["id"]: s for s in data["scores"]}
        # 恰好 600 秒落在闭窗口内。
        self.assertIn("BURST", by_id["t1"]["reason"])

    def test_fan_out_threshold_and_points_custom(self):
        transfers = [
            tx("f1", "A", "r1", usd=0),
            tx("f2", "A", "r2", usd=0, ts="2026-10-04T10:10:00Z"),
        ]
        data = self.with_scoring(
            transfers, {"fan_out_recipients": 2, "fan_out_points": 11,
                        "burst_count": 100}
        )
        by_id = {s["id"]: s for s in data["scores"]}
        self.assertEqual(by_id["f1"]["score"], 11)
        self.assertEqual(by_id["f1"]["reason"], ["FAN_OUT"])

    def test_round_trip_points_custom(self):
        data = self.with_scoring(
            [
                tx("out", "A", "B", amount=7.0, asset="USDC", usd=0),
                tx("back", "B", "A", amount=7.0, asset="USDC", usd=0,
                   ts="2026-10-04T11:00:00Z"),
            ],
            {"round_trip_points": 5},
        )
        by_id = {s["id"]: s for s in data["scores"]}
        self.assertEqual(by_id["out"]["score"], 5)
        self.assertEqual(by_id["back"]["score"], 5)

    def test_partial_config_missing_fields_keep_baseline(self):
        transfers = [
            tx("t%d" % i, "A", "r%d" % i, usd=0,
               ts="2026-10-04T10:%02d:00Z" % (i * 10))
            for i in range(5)
        ]
        # 仅覆盖突发分值；扇出仍为基线 20，窗口/阈值仍为基线。
        data = self.with_scoring(transfers, {"transfer_burst_points": 30})
        top = data["scores"][0]
        self.assertEqual(top["score"], 50)
        self.assertEqual(top["reason"], ["BURST", "FAN_OUT"])

    def test_total_still_clamped_to_100(self):
        data = self.with_scoring(
            [tx("big", "A", "B", usd=100000)],
            {"value_points_per_ratio": 1000, "value_points_cap": 100},
            threshold=10000,
        )
        self.assertEqual(data["scores"][0]["score"], 100)

    def assert_invalid(self, scoring):
        body = payload([])
        body["scoring"] = scoring
        with self.assertRaises(AnalyzeError) as ctx:
            analyze(body)
        self.assertEqual(ctx.exception.code, "INVALID_SCORING_CONFIG")

    def test_scoring_must_be_object(self):
        for bad in ([], "x", 1, 1.5, None, True):
            self.assert_invalid(bad)

    def test_unknown_field_rejected(self):
        self.assert_invalid({"unknown_field": 1})

    def test_int_fields_must_be_int_in_range(self):
        for field in ("window_seconds", "burst_count",
                      "fan_out_recipients", "counterparty_count"):
            for bad in (0, -1, 10001, 1.0, "5", True, None):
                self.assert_invalid({field: bad})
        # 边界合法。
        for field in ("window_seconds", "burst_count",
                      "fan_out_recipients", "counterparty_count"):
            body = payload([])
            body["scoring"] = {field: 1}
            self.assertEqual(analyze(body)["scores"], [])
            body["scoring"] = {field: 10000}
            self.assertEqual(analyze(body)["scores"], [])

    def test_ratio_field_type_and_range(self):
        for bad in (-0.01, 1000.01, "10", True, None,
                    float("inf"), float("nan")):
            self.assert_invalid({"value_points_per_ratio": bad})
        for ok in (0, 1000, 0.5, 1000.0):
            body = payload([])
            body["scoring"] = {"value_points_per_ratio": ok}
            analyze(body)

    def test_point_fields_type_and_range(self):
        fields = ("value_points_cap", "transfer_burst_points",
                  "fan_out_points", "round_trip_points", "whale_points",
                  "counterparty_points", "address_round_trip_points",
                  "address_burst_points")
        for field in fields:
            for bad in (-0.01, 100.01, "10", True, None,
                        float("inf"), float("nan")):
                self.assert_invalid({field: bad})
            # 0 与 100 边界合法（校验为全命令共享，analyze 也接受画像字段）。
            body = payload([])
            body["scoring"] = {field: 0}
            analyze(body)
            body["scoring"] = {field: 100}
            analyze(body)

    def test_error_precedence_scoring_last(self):
        def code(body):
            with self.assertRaises(AnalyzeError) as ctx:
                analyze(body)
            return ctx.exception.code

        body = {"transfers": [], "scoring": []}
        self.assertEqual(code(body), "INVALID_INPUT_SCHEMA")

        body = payload([tx("dup", "A", "B"),
                        tx("dup", "A", "B", ts="bad")])
        body["scoring"] = []
        self.assertEqual(code(body), "DUPLICATE_TRANSFER_ID")

        body = payload([tx("t", "A", "B", amount=0)])
        body["scoring"] = []
        self.assertEqual(code(body), "INVALID_TRANSFER_VALUE")

        body = payload([], threshold=0, routes=[route("r", 200, "info")])
        body["scoring"] = []
        self.assertEqual(code(body), "INVALID_THRESHOLD")

        body = payload([], routes=[route("r", 101, "info")])
        body["scoring"] = []
        self.assertEqual(code(body), "INVALID_ROUTE")


class ScoringRankTests(unittest.TestCase):
    def with_scoring(self, transfers, scoring, threshold=10000.0):
        body = payload(transfers, threshold=threshold)
        body["scoring"] = scoring
        return rank(body)

    def test_whale_points_custom(self):
        data = self.with_scoring(
            [tx("hi", "C", "D", usd=10000)], {"whale_points": 7}
        )
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertEqual(prof["C"]["risk_score"], 7)
        self.assertEqual(prof["D"]["risk_score"], 7)

    def test_counterparty_count_and_points_custom(self):
        transfers = [
            tx("t%d" % i, "A", "r%d" % i, usd=1)
            for i in range(2)
        ]
        data = self.with_scoring(
            transfers,
            {"counterparty_count": 2, "counterparty_points": 9,
             "whale_points": 0},
        )
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertEqual(prof["A"]["risk_score"], 9)

    def test_address_round_trip_and_burst_custom(self):
        data = self.with_scoring(
            [
                tx("out", "A", "B", amount=7.0, asset="USDC"),
                tx("back", "B", "A", amount=7.0, asset="USDC",
                   ts="2026-10-04T11:00:00Z"),
            ],
            {"address_round_trip_points": 5, "whale_points": 0},
        )
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertEqual(prof["A"]["risk_score"], 5)

        transfers = [
            tx("t%d" % i, "A", "x%d" % i, usd=1,
               ts="2026-10-04T10:%02d:00Z" % (i * 10))
            for i in range(5)
        ]
        data = self.with_scoring(
            transfers,
            {"address_burst_points": 6, "burst_count": 5,
             "counterparty_count": 100, "whale_points": 0},
        )
        prof = {p["address"]: p for p in data["profiles"]}
        self.assertEqual(prof["A"]["risk_score"], 6)

    def test_baseline_score_stays_integer(self):
        data = self.with_scoring(
            [tx("hi", "C", "D", usd=10000)], {"whale_points": 40}
        )
        prof = next(p for p in data["profiles"] if p["address"] == "C")
        self.assertIsInstance(prof["risk_score"], int)
        self.assertEqual(prof["risk_score"], 40)

    def test_clamped_to_100(self):
        data = self.with_scoring(
            [tx("hi", "C", "D", usd=10000)], {"whale_points": 100}
        )
        self.assertEqual(
            max(p["risk_score"] for p in data["profiles"]), 100
        )

    def test_invalid_scoring(self):
        body = payload([])
        body["scoring"] = {"whale_points": 101}
        with self.assertRaises(AnalyzeError) as ctx:
            rank(body)
        self.assertEqual(ctx.exception.code, "INVALID_SCORING_CONFIG")

    def test_route_error_precedes_scoring(self):
        body = payload([], routes=[route("r", 101, "info")])
        body["scoring"] = []
        with self.assertRaises(AnalyzeError) as ctx:
            rank(body)
        self.assertEqual(ctx.exception.code, "INVALID_ROUTE")


class ScoringTraceRiskTests(unittest.TestCase):
    def risk_with_scoring(self, scoring, **kwargs):
        transfers = kwargs.pop("transfers", [
            tx("t1", "A", "B", usd=10000),
            tx("t2", "B", "D", usd=5000),
        ])
        body = {
            "transfers": transfers,
            "whale_threshold_usd": 10000,
            "routes": kwargs.pop("routes", []),
            "chain": "eth",
            "asset": "ETH",
            "start_address": "A",
            "end_address": "D",
            "max_hops": 4,
        }
        body.update(kwargs)
        body["scoring"] = scoring
        return trace_risk(body)

    def test_scoring_changes_segment_sum(self):
        data = self.risk_with_scoring(
            {"value_points_per_ratio": 10, "value_points_cap": 100}
        )
        path = next(p for p in data["paths"] if p["path_id"] == "t1>t2")
        # t1=10、t2=5，合计 15。
        self.assertEqual(path["score"], 15)

    def test_path_sum_caps_at_100(self):
        data = self.risk_with_scoring(
            {"value_points_per_ratio": 1000, "value_points_cap": 100},
            transfers=[tx("t1", "A", "D", usd=10000)],
        )
        self.assertEqual(data["paths"][0]["score"], 100)

    def test_invalid_scoring_after_valid_query(self):
        with self.assertRaises(AnalyzeError) as ctx:
            self.risk_with_scoring([])
        self.assertEqual(ctx.exception.code, "INVALID_SCORING_CONFIG")

    def test_query_error_precedes_scoring(self):
        with self.assertRaises(AnalyzeError) as ctx:
            self.risk_with_scoring([], max_hops=0)
        self.assertEqual(ctx.exception.code, "INVALID_TRACE_QUERY")


class ScoringTraceIgnoredTests(unittest.TestCase):
    def test_trace_ignores_scoring_entirely(self):
        body = {
            "transfers": [tx("t1", "A", "D", usd=1)],
            "chain": "eth",
            "asset": "ETH",
            "start_address": "A",
            "end_address": "D",
            "max_hops": 3,
            "scoring": {"totally_unknown": True, "burst_count": 0},
        }
        data = trace(body)
        self.assertEqual(data["paths"][0]["transfer_ids"], ["t1"])


class ScoringCliTests(unittest.TestCase):
    def test_analyze_scoring_success(self):
        body = payload([tx("t1", "A", "B", usd=10000)])
        body["scoring"] = {"value_points_per_ratio": 5}
        proc = subprocess.run(
            [BIN, "analyze"], input=json.dumps(body),
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["data"]["scores"][0]["score"], 5)

    def assert_invalid_scoring(self, command):
        body = payload([])
        body["scoring"] = {"window_seconds": 0}
        proc = subprocess.run(
            [BIN, command], input=json.dumps(body),
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_SCORING_CONFIG"}
        )

    def test_analyze_rank_trace_risk_invalid_scoring(self):
        self.assert_invalid_scoring("analyze")
        self.assert_invalid_scoring("rank")
        risk = {
            "transfers": [], "whale_threshold_usd": 10000, "routes": [],
            "chain": "eth", "asset": "ETH", "start_address": "A",
            "end_address": "D", "max_hops": 3,
            "scoring": {"window_seconds": 0},
        }
        proc = subprocess.run(
            [BIN, "trace-risk"], input=json.dumps(risk),
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_SCORING_CONFIG"}
        )

    def test_trace_invalid_scoring_still_succeeds(self):
        body = {
            "transfers": [tx("t1", "A", "D", usd=1)],
            "chain": "eth", "asset": "ETH",
            "start_address": "A", "end_address": "D", "max_hops": 3,
            "scoring": "not-an-object",
        }
        proc = subprocess.run(
            [BIN, "trace"], input=json.dumps(body),
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout)["data"]["paths"][0]["transfer_ids"],
            ["t1"],
        )


class WatchTests(unittest.TestCase):
    def watch_query(self, transfers, watch=("A", "D"), routes=None,
                    max_hops=4, threshold=10000.0):
        result = payload(transfers, threshold=threshold, routes=routes)
        result["watch_addresses"] = list(watch)
        result["max_hops"] = max_hops
        return result

    def test_direct_and_multi_hop_paths_between_watch_addresses(self):
        data = watch(self.watch_query([
            tx("t1", "A", "B", amount=1.0, usd=10),
            tx("t2", "B", "D", amount=2.0, usd=20),
            tx("t3", "A", "D", amount=5.0, usd=50),
        ]))
        self.assertEqual(set(data), {"paths", "alerts"})
        self.assertEqual(
            [(p["path_id"], p["from_address"], p["to_address"])
             for p in data["paths"]],
            [("t3", "A", "D"), ("t1>t2", "A", "D")],
        )
        direct = data["paths"][0]
        self.assertEqual(
            set(direct),
            {"nodes", "transfer_ids", "hops", "amount", "usd_value",
             "from_address", "to_address", "chain", "asset", "path_id",
             "score", "reason", "segments"},
        )
        self.assertEqual(direct["nodes"], ["A", "D"])
        self.assertEqual(direct["hops"], 1)
        self.assertEqual(direct["amount"], 5)
        self.assertEqual(direct["usd_value"], 50)
        self.assertEqual(direct["chain"], "eth")
        self.assertEqual(direct["asset"], "ETH")
        # 50/10000*20 = 0.1。
        self.assertEqual(direct["score"], 0.1)
        self.assertEqual(direct["reason"], ["VALUE"])
        self.assertEqual(
            direct["segments"],
            [{"transfer_id": "t3", "score": 0.1, "reason": ["VALUE"]}],
        )
        relay = data["paths"][1]
        self.assertEqual(relay["nodes"], ["A", "B", "D"])
        self.assertEqual(relay["amount"], 3)
        self.assertEqual(relay["usd_value"], 30)
        # t1=0.02、t2=0.04，合计 0.06。
        self.assertEqual(relay["score"], 0.06)
        self.assertEqual(relay["reason"], ["VALUE"])
        self.assertEqual(
            relay["segments"],
            [
                {"transfer_id": "t1", "score": 0.02, "reason": ["VALUE"]},
                {"transfer_id": "t2", "score": 0.04, "reason": ["VALUE"]},
            ],
        )

    def test_paths_for_every_ordered_pair_of_watch_addresses(self):
        data = watch(self.watch_query([
            tx("a2b", "A", "B", usd=1),
            tx("b2c", "B", "C", usd=1),
            tx("c2d", "C", "D", usd=1),
            tx("d2a", "D", "A", usd=1),
        ], watch=("A", "B", "C", "D")))
        endpoints = {
            (p["from_address"], p["to_address"], p["hops"])
            for p in data["paths"]
        }
        self.assertIn(("A", "B", 1), endpoints)
        self.assertIn(("A", "C", 2), endpoints)
        self.assertIn(("A", "D", 3), endpoints)
        self.assertIn(("B", "C", 1), endpoints)
        self.assertIn(("B", "D", 2), endpoints)
        self.assertIn(("C", "D", 1), endpoints)
        self.assertIn(("D", "A", 1), endpoints)
        # D -> B：沿 D->A->B，2 跳；C -> A：沿 C->D->A，2 跳。
        self.assertIn(("D", "B", 2), endpoints)
        self.assertIn(("C", "A", 2), endpoints)

    def test_intermediate_watch_address_still_extends(self):
        # A->B->D 与 A->B->C->D 都应出现（B、C 同为关注地址）。
        data = watch(self.watch_query([
            tx("t1", "A", "B", usd=1),
            tx("t2", "B", "C", usd=1),
            tx("t3", "C", "D", usd=1),
        ], watch=("A", "B", "C", "D")))
        ids = {p["path_id"] for p in data["paths"]}
        self.assertIn("t1", ids)
        self.assertIn("t1>t2", ids)
        self.assertIn("t1>t2>t3", ids)
        self.assertIn("t2", ids)
        self.assertIn("t2>t3", ids)
        self.assertIn("t3", ids)

    def test_paths_stay_within_same_chain_and_asset(self):
        data = watch(self.watch_query([
            tx("t1", "A", "D", chain="bsc", asset="BNB"),
            tx("t2", "A", "B", chain="bsc", asset="USDT"),
            tx("t3", "B", "D", chain="eth", asset="BNB"),
            tx("t4", "A", "B"),
            tx("t5", "B", "D"),
        ]))
        ids = {p["path_id"] for p in data["paths"]}
        self.assertEqual(ids, {"t1", "t4>t5"})
        by_id = {p["path_id"]: p for p in data["paths"]}
        self.assertEqual(by_id["t1"]["chain"], "bsc")
        self.assertEqual(by_id["t1"]["asset"], "BNB")

    def test_no_path_when_no_connection_between_watched(self):
        data = watch(self.watch_query([
            tx("t1", "A", "x1"),
            tx("t2", "x2", "D"),
        ]))
        self.assertEqual(data, {"paths": [], "alerts": []})

    def test_non_watch_addresses_never_anchor_paths(self):
        # 仅 B->C 这一段不触及关注地址，即使直接相连也不产生路径。
        data = watch(self.watch_query([
            tx("t1", "A", "B", usd=1),
            tx("t2", "B", "C", usd=1),
            tx("t3", "C", "x", usd=1),
        ], watch=("A", "D")))
        self.assertEqual(data["paths"], [])

    def test_simple_paths_no_repeated_addresses(self):
        data = watch(self.watch_query([
            tx("t1", "A", "B"),
            tx("t2", "B", "C"),
            tx("t3", "C", "B"),
            tx("t4", "C", "D"),
        ]))
        self.assertEqual(
            sorted(p["path_id"] for p in data["paths"]), ["t1>t2>t4"]
        )

    def test_max_hops_limits_depth(self):
        transfers = [
            tx("t1", "A", "B"),
            tx("t2", "B", "C"),
            tx("t3", "C", "D"),
        ]
        self.assertEqual(watch(self.watch_query(transfers, max_hops=2))["paths"],
                         [])
        data = watch(self.watch_query(transfers, max_hops=3))
        self.assertEqual(
            [p["path_id"] for p in data["paths"]], ["t1>t2>t3"]
        )

    def test_parallel_edges_yield_distinct_paths(self):
        data = watch(self.watch_query([
            tx("t2", "A", "D", amount=2.0, usd=5000),
            tx("t1", "A", "D", amount=1.0, usd=50000),
        ]))
        # 分值不同：t1 为 40，t2 为 10，按分值降序。
        self.assertEqual(
            [p["path_id"] for p in data["paths"]], ["t1", "t2"]
        )

    def test_score_sum_clamped_and_reason_dedup(self):
        data = watch(self.watch_query([
            tx("rt", "B", "A", amount=2.0, asset="ETH", usd=0,
               ts="2026-10-04T11:30:00Z"),
            tx("s1", "A", "B", amount=2.0, asset="ETH", usd=50000),
            tx("s2", "B", "D", usd=50000),
        ]))
        path = next(p for p in data["paths"] if p["path_id"] == "s1>s2")
        # s1=VALUE 40 + ROUND_TRIP 15，s2=VALUE 40，合计 95。
        self.assertEqual(path["score"], 95)
        self.assertEqual(path["reason"], ["VALUE", "ROUND_TRIP"])

    def test_paths_sorted_score_desc_then_hops_chain_asset_ids(self):
        data = watch(self.watch_query([
            tx("lo", "A", "D", usd=10000),              # 20，1 跳
            tx("hi1", "A", "B", usd=50000),             # 40
            tx("hi2", "B", "D", usd=50000),             # 80，2 跳
        ]))
        self.assertEqual(
            [p["path_id"] for p in data["paths"]], ["hi1>hi2", "lo"]
        )

    def test_score_tie_breaks_by_hops(self):
        data = watch(self.watch_query([
            tx("d", "A", "D", usd=10000),     # 20
            tx("m1", "A", "B", usd=5000),     # 10
            tx("m2", "B", "D", usd=5000),     # 20
        ]))
        self.assertEqual(
            [p["path_id"] for p in data["paths"]], ["d", "m1>m2"]
        )

    def test_chain_asset_tie_break_ordering(self):
        data = watch(self.watch_query([
            tx("z", "A", "D", chain="eth", asset="USDC", usd=10000),
            tx("a", "A", "D", chain="eth", asset="ETH", usd=10000),
            tx("b", "A", "D", chain="bsc", asset="BNB", usd=10000),
        ]))
        # 同分同跳数：chain 升序后 asset 升序。
        self.assertEqual(
            [(p["chain"], p["asset"]) for p in data["paths"]],
            [("bsc", "BNB"), ("eth", "ETH"), ("eth", "USDC")],
        )

    def test_amount_usd_round10_representation(self):
        data = watch(self.watch_query([
            tx("t1", "A", "B", amount=0.1, usd=0.2),
            tx("t2", "B", "D", amount=0.2, usd=0.1),
        ]))
        path = data["paths"][0]
        self.assertEqual(path["amount"], 0.3)
        self.assertEqual(path["usd_value"], 0.3)

    def test_alerts_one_per_route_path_and_sorted(self):
        routes = [
            route("r2", 40, "warning", chains=("eth",), target="email"),
            route("r1", 40, "critical", chains=("eth",), assets=("ETH",),
                  target="pager"),
            route("r-btc", 40, "info", chains=("btc",), target="x"),
        ]
        data = watch(self.watch_query([
            tx("t1", "A", "B", usd=50000),
            tx("t2", "B", "D", usd=50000),
            tx("t3", "A", "D", usd=50000),
        ], routes=routes))
        alerts = data["alerts"]
        self.assertEqual(
            [(a["route_id"], a["path_id"]) for a in alerts],
            [("r1", "t1>t2"), ("r1", "t3"),
             ("r2", "t1>t2"), ("r2", "t3")],
        )
        first = alerts[0]
        self.assertEqual(
            set(first),
            {"route_id", "path_id", "from_address", "to_address",
             "chain", "asset", "severity", "score", "reason", "target"},
        )
        self.assertEqual(first["from_address"], "A")
        self.assertEqual(first["to_address"], "D")
        self.assertEqual(first["chain"], "eth")
        self.assertEqual(first["asset"], "ETH")
        self.assertEqual(first["severity"], "critical")
        self.assertEqual(first["target"], "pager")
        self.assertEqual(first["score"], 80)
        self.assertEqual(first["reason"], ["VALUE"])

    def test_alert_min_score_boundary_inclusive(self):
        data = watch(self.watch_query(
            [tx("t1", "A", "D", usd=5000)],
            routes=[route("r", 10, "info")],
        ))
        self.assertEqual(
            [(a["route_id"], a["path_id"]) for a in data["alerts"]],
            [("r", "t1")],
        )

    def test_alert_chain_asset_star_and_mismatch(self):
        data = watch(self.watch_query(
            [tx("t1", "A", "D", usd=50000)],
            routes=[route("r", 40, "info", chains=("eth",),
                          assets=("BTC",))],
        ))
        self.assertEqual(data["alerts"], [])

    def assert_watch_code(self, code, obj):
        with self.assertRaises(AnalyzeError) as ctx:
            watch(obj)
        self.assertEqual(ctx.exception.code, code)

    def test_watch_query_errors(self):
        base = [tx("t1", "A", "D")]
        for field in ("watch_addresses", "max_hops"):
            bad = self.watch_query(base)
            del bad[field]
            self.assert_watch_code("INVALID_WATCH_QUERY", bad)
        # 至少两个地址。
        self.assert_watch_code(
            "INVALID_WATCH_QUERY", self.watch_query(base, watch=("A",))
        )
        self.assert_watch_code(
            "INVALID_WATCH_QUERY", self.watch_query(base, watch=[])
        )
        # 必须互异。
        self.assert_watch_code(
            "INVALID_WATCH_QUERY", self.watch_query(base, watch=("A", "A"))
        )
        # 必须为非空字符串。
        self.assert_watch_code(
            "INVALID_WATCH_QUERY", self.watch_query(base, watch=("A", ""))
        )
        self.assert_watch_code(
            "INVALID_WATCH_QUERY", self.watch_query(base, watch=("A", 1))
        )
        bad = self.watch_query(base)
        bad["watch_addresses"] = "AD"
        self.assert_watch_code("INVALID_WATCH_QUERY", bad)
        # max_hops 1..8 整数。
        for bad_hops in (0, 9, 2.5, "3", True, None):
            self.assert_watch_code(
                "INVALID_WATCH_QUERY",
                self.watch_query(base, max_hops=bad_hops),
            )

    def test_error_precedence(self):
        # threshold -> route -> watch query -> scoring。
        bad = self.watch_query(
            [], threshold=0, routes=[route("r", 200, "info")], watch=("A",)
        )
        self.assert_watch_code("INVALID_THRESHOLD", bad)
        bad = self.watch_query([], routes=[route("r", 200, "info")],
                               watch=("A",))
        self.assert_watch_code("INVALID_ROUTE", bad)
        bad = self.watch_query([], watch=("A",))
        self.assert_watch_code("INVALID_WATCH_QUERY", bad)

    def test_duplicate_and_value_precedence(self):
        bad = self.watch_query(
            [tx("dup", "A", "B"), tx("dup", "B", "D", ts="bad")])
        self.assert_watch_code("DUPLICATE_TRANSFER_ID", bad)
        bad = self.watch_query([tx("t1", "A", "D", amount=0)])
        self.assert_watch_code("INVALID_TRANSFER_VALUE", bad)


class WatchScoringTests(unittest.TestCase):
    def watch_with_scoring(self, scoring, **kwargs):
        transfers = kwargs.pop("transfers", [
            tx("t1", "A", "B", usd=10000),
            tx("t2", "B", "D", usd=5000),
        ])
        body = {
            "transfers": transfers,
            "whale_threshold_usd": 10000,
            "routes": kwargs.pop("routes", []),
            "watch_addresses": ["A", "D"],
            "max_hops": 4,
        }
        body.update(kwargs)
        body["scoring"] = scoring
        return watch(body)

    def test_scoring_changes_segment_sum(self):
        data = self.watch_with_scoring(
            {"value_points_per_ratio": 10, "value_points_cap": 100}
        )
        path = next(p for p in data["paths"] if p["path_id"] == "t1>t2")
        # t1=10、t2=5，合计 15。
        self.assertEqual(path["score"], 15)

    def test_path_sum_caps_at_100(self):
        data = self.watch_with_scoring(
            {"value_points_per_ratio": 1000, "value_points_cap": 100},
            transfers=[tx("t1", "A", "D", usd=10000)],
        )
        self.assertEqual(data["paths"][0]["score"], 100)

    def test_invalid_scoring_after_watch_query(self):
        with self.assertRaises(AnalyzeError) as ctx:
            self.watch_with_scoring([])
        self.assertEqual(ctx.exception.code, "INVALID_SCORING_CONFIG")

    def test_watch_query_error_precedes_scoring(self):
        with self.assertRaises(AnalyzeError) as ctx:
            self.watch_with_scoring([], max_hops=0)
        self.assertEqual(ctx.exception.code, "INVALID_WATCH_QUERY")


class WatchCliTests(unittest.TestCase):
    def run_cli(self, raw):
        return subprocess.run(
            [BIN, "watch"], input=raw, capture_output=True, text=True
        )

    def test_success_stdout(self):
        body = {
            "transfers": [tx("t1", "A", "D", usd=1)],
            "whale_threshold_usd": 10000,
            "routes": [],
            "watch_addresses": ["A", "D"],
            "max_hops": 3,
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(set(out), {"data"})
        self.assertEqual(set(out["data"]), {"paths", "alerts"})
        self.assertEqual(out["data"]["paths"][0]["path_id"], "t1")
        self.assertEqual(proc.stderr, "")

    def test_empty_paths_and_alerts(self):
        body = {
            "transfers": [],
            "whale_threshold_usd": 10000,
            "routes": [],
            "watch_addresses": ["A", "D"],
            "max_hops": 3,
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            {"data": {"paths": [], "alerts": []}},
        )

    def test_not_json(self):
        proc = self.run_cli("{not json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr), {"error": "INPUT_NOT_JSON"})

    def test_error_codes_via_cli(self):
        proc = self.run_cli(json.dumps({"transfers": []}))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_INPUT_SCHEMA"}
        )
        body = {
            "transfers": [], "whale_threshold_usd": 10000, "routes": [],
            "watch_addresses": ["A"], "max_hops": 3,
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_WATCH_QUERY"}
        )
        body = {
            "transfers": [], "whale_threshold_usd": 10000, "routes": [],
            "watch_addresses": ["A", "D"], "max_hops": 3,
            "scoring": {"window_seconds": 0},
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_SCORING_CONFIG"}
        )


class ConvergeTests(unittest.TestCase):
    def converge_query(self, transfers, routes=None, threshold=10000.0,
                       window_seconds=3600, min_sources=2, min_usd_value=0):
        result = payload(transfers, threshold=threshold, routes=routes)
        result["convergence"] = {
            "window_seconds": window_seconds,
            "min_sources": min_sources,
            "min_usd_value": min_usd_value,
        }
        return result

    def test_fan_in_events_from_each_start_transfer(self):
        data = converge(self.converge_query([
            tx("t1", "s1", "R", amount=1.0, usd=100),
            tx("t2", "s2", "R", amount=2.0, usd=200,
               ts="2026-10-04T10:00:10Z"),
            tx("t3", "s3", "R", amount=4.0, usd=300,
               ts="2026-10-04T10:00:20Z"),
        ]))
        self.assertEqual(set(data), {"events", "alerts"})
        events = data["events"]
        self.assertEqual(
            [e["event_id"] for e in events], ["t1>t2>t3", "t2>t3"]
        )
        first = events[0]
        self.assertEqual(
            set(first),
            {"event_id", "recipient", "chain", "asset", "source_count",
             "transfer_ids", "amount", "usd_value", "score", "reason"},
        )
        self.assertEqual(first["recipient"], "R")
        self.assertEqual(first["chain"], "eth")
        self.assertEqual(first["asset"], "ETH")
        self.assertEqual(first["source_count"], 3)
        self.assertEqual(first["transfer_ids"], ["t1", "t2", "t3"])
        self.assertEqual(first["amount"], 7)
        self.assertEqual(first["usd_value"], 600)
        # 20*(3-2+1) + 50*600/10000 = 43。
        self.assertEqual(first["score"], 43)
        self.assertEqual(first["reason"], ["FAN_IN"])
        second = events[1]
        self.assertEqual(second["source_count"], 2)
        self.assertEqual(second["usd_value"], 500)
        # 20*(2-2+1) + 50*500/10000 = 22.5。
        self.assertEqual(second["score"], 22.5)

    def test_window_closed_boundary_and_forward_only(self):
        data = converge(self.converge_query([
            tx("t1", "s1", "R", usd=1),
            tx("t2", "s2", "R", usd=1, ts="2026-10-04T10:10:00Z"),
            tx("t3", "s3", "R", usd=1, ts="2026-10-04T10:10:01Z"),
        ], window_seconds=600))
        by_id = {e["event_id"]: e for e in data["events"]}
        # 恰好 600 秒落在闭窗口内；601 秒的 t3 不在 t1 窗口内。
        self.assertEqual(by_id["t1>t2"]["transfer_ids"], ["t1", "t2"])
        self.assertIn("t2>t3", by_id)
        self.assertNotIn("t1>t2>t3", by_id)

    def test_self_transfers_excluded(self):
        data = converge(self.converge_query([
            tx("t1", "s1", "R", usd=1),
            tx("self", "R", "R", usd=99999),
            tx("t2", "s2", "R", usd=1, ts="2026-10-04T10:00:10Z"),
        ]))
        self.assertEqual(
            [e["event_id"] for e in data["events"]], ["t1>t2"]
        )

    def test_groups_split_by_chain_asset_recipient(self):
        data = converge(self.converge_query([
            tx("t1", "s1", "R", usd=1),
            tx("t2", "s2", "R", usd=1, chain="bsc", asset="BNB"),
            tx("t3", "s3", "X", usd=1),
            tx("t4", "s4", "R", usd=1, asset="USDC"),
        ]))
        self.assertEqual(data["events"], [])

    def test_min_sources_and_min_usd_value_filters(self):
        transfers = [
            tx("t1", "s1", "R", usd=100),
            tx("t2", "s2", "R", usd=100, ts="2026-10-04T10:00:10Z"),
        ]
        data = converge(self.converge_query(transfers, min_sources=3))
        self.assertEqual(data["events"], [])
        data = converge(self.converge_query(transfers, min_usd_value=201))
        self.assertEqual(data["events"], [])
        # 边界等值命中。
        data = converge(self.converge_query(transfers, min_usd_value=200))
        self.assertEqual(len(data["events"]), 1)

    def test_same_source_counted_once(self):
        data = converge(self.converge_query([
            tx("t1", "s1", "R", usd=1),
            tx("t2", "s1", "R", usd=1, ts="2026-10-04T10:00:10Z"),
        ]))
        self.assertEqual(data["events"], [])

    def test_value_reason_appended_at_threshold(self):
        data = converge(self.converge_query([
            tx("t1", "s1", "R", usd=6000),
            tx("t2", "s2", "R", usd=4000, ts="2026-10-04T10:00:10Z"),
        ], threshold=10000))
        event = data["events"][0]
        self.assertEqual(event["usd_value"], 10000)
        self.assertEqual(event["reason"], ["FAN_IN", "VALUE"])
        # 20*(2-2+1) + 50*10000/10000 = 70。
        self.assertEqual(event["score"], 70)

    def test_score_capped_at_100(self):
        transfers = [
            tx("t%d" % i, "s%d" % i, "R", usd=100000,
               ts="2026-10-04T10:00:%02dZ" % i)
            for i in range(6)
        ]
        data = converge(self.converge_query(transfers, threshold=10000))
        self.assertEqual(data["events"][0]["score"], 100)

    def test_events_sorted_score_desc_then_event_id(self):
        data = converge(self.converge_query([
            tx("b1", "s1", "R", usd=0),
            tx("b2", "s2", "R", usd=0, ts="2026-10-04T10:00:10Z"),
            tx("a1", "s3", "Q", usd=0),
            tx("a2", "s4", "Q", usd=0, ts="2026-10-04T10:00:10Z"),
        ]))
        # 两组各 2 来源、usd 合计 0，同分 20，按 event_id 升序。
        self.assertEqual(
            [(e["event_id"], e["score"]) for e in data["events"]],
            [("a1>a2", 20), ("b1>b2", 20)],
        )

    def test_amount_usd_round10_representation(self):
        data = converge(self.converge_query([
            tx("t1", "s1", "R", amount=0.1, usd=0.2),
            tx("t2", "s2", "R", amount=0.2, usd=0.1,
               ts="2026-10-04T10:00:10Z"),
        ]))
        event = data["events"][0]
        self.assertEqual(event["amount"], 0.3)
        self.assertEqual(event["usd_value"], 0.3)

    def test_empty_events_keep_arrays(self):
        data = converge(self.converge_query([]))
        self.assertEqual(data, {"events": [], "alerts": []})

    def test_alerts_one_per_route_event_and_sorted(self):
        routes = [
            route("r2", 20, "warning", chains=("eth",), target="email"),
            route("r1", 40, "critical", chains=("eth",), assets=("ETH",),
                  target="pager"),
            route("r-btc", 0, "info", chains=("btc",), target="x"),
        ]
        data = converge(self.converge_query([
            tx("t1", "s1", "R", usd=100),
            tx("t2", "s2", "R", usd=200, ts="2026-10-04T10:00:10Z"),
            tx("t3", "s3", "R", usd=300, ts="2026-10-04T10:00:20Z"),
        ], routes=routes))
        alerts = data["alerts"]
        # t1>t2>t3 得 43，t2>t3 得 22.5；r1 仅命中前者，r2 两者皆中。
        self.assertEqual(
            [(a["route_id"], a["event_id"]) for a in alerts],
            [("r1", "t1>t2>t3"), ("r2", "t1>t2>t3"), ("r2", "t2>t3")],
        )
        first = alerts[0]
        self.assertEqual(
            set(first),
            {"route_id", "event_id", "recipient", "to_address", "chain",
             "asset", "severity", "score", "reason", "target"},
        )
        self.assertEqual(first["recipient"], "R")
        self.assertEqual(first["to_address"], "R")
        self.assertEqual(first["chain"], "eth")
        self.assertEqual(first["asset"], "ETH")
        self.assertEqual(first["severity"], "critical")
        self.assertEqual(first["target"], "pager")
        self.assertEqual(first["score"], 43)
        self.assertEqual(first["reason"], ["FAN_IN"])

    def test_alert_min_score_boundary_inclusive(self):
        data = converge(self.converge_query(
            [tx("t1", "s1", "R", usd=0),
             tx("t2", "s2", "R", usd=0, ts="2026-10-04T10:00:10Z")],
            routes=[route("r", 20, "info")],
        ))
        self.assertEqual(
            [(a["route_id"], a["event_id"]) for a in data["alerts"]],
            [("r", "t1>t2")],
        )

    def test_alert_chain_asset_star_and_mismatch(self):
        data = converge(self.converge_query(
            [tx("t1", "s1", "R", usd=0),
             tx("t2", "s2", "R", usd=0, ts="2026-10-04T10:00:10Z")],
            routes=[route("r", 0, "info", chains=("eth",),
                          assets=("BTC",))],
        ))
        self.assertEqual(data["alerts"], [])

    def assert_converge_code(self, code, obj):
        with self.assertRaises(AnalyzeError) as ctx:
            converge(obj)
        self.assertEqual(ctx.exception.code, code)

    def test_convergence_query_errors(self):
        base = [tx("t1", "s1", "R")]
        # 缺失 convergence 整体或任一字段。
        self.assert_converge_code("INVALID_CONVERGENCE_QUERY", payload(base))
        for field in ("window_seconds", "min_sources", "min_usd_value"):
            bad = self.converge_query(base)
            del bad["convergence"][field]
            self.assert_converge_code("INVALID_CONVERGENCE_QUERY", bad)
        # 未知字段与非对象。
        bad = self.converge_query(base)
        bad["convergence"]["unknown"] = 1
        self.assert_converge_code("INVALID_CONVERGENCE_QUERY", bad)
        for raw in ([], "x", 1, None, True):
            bad = self.converge_query(base)
            bad["convergence"] = raw
            self.assert_converge_code("INVALID_CONVERGENCE_QUERY", bad)
        # window_seconds：1..10000 整数。
        for bad_value in (0, -1, 10001, 1.0, "5", True, None):
            self.assert_converge_code(
                "INVALID_CONVERGENCE_QUERY",
                self.converge_query(base, window_seconds=bad_value),
            )
        # min_sources：2..10000 整数。
        for bad_value in (1, 0, -1, 10001, 2.0, "2", True, None):
            self.assert_converge_code(
                "INVALID_CONVERGENCE_QUERY",
                self.converge_query(base, min_sources=bad_value),
            )
        # min_usd_value：非负有限数。
        for bad_value in (-0.01, "0", True, None,
                          float("inf"), float("nan")):
            self.assert_converge_code(
                "INVALID_CONVERGENCE_QUERY",
                self.converge_query(base, min_usd_value=bad_value),
            )
        # 边界合法。
        converge(self.converge_query(base, window_seconds=1,
                                     min_sources=2, min_usd_value=0))
        converge(self.converge_query(base, window_seconds=10000,
                                     min_sources=10000,
                                     min_usd_value=0.0))

    def test_error_precedence(self):
        # threshold -> route -> convergence -> scoring。
        bad = self.converge_query([], threshold=0,
                                  routes=[route("r", 200, "info")],
                                  min_sources=1)
        self.assert_converge_code("INVALID_THRESHOLD", bad)
        bad = self.converge_query([], routes=[route("r", 200, "info")],
                                  min_sources=1)
        self.assert_converge_code("INVALID_ROUTE", bad)
        bad = self.converge_query([], min_sources=1)
        bad["scoring"] = []
        self.assert_converge_code("INVALID_CONVERGENCE_QUERY", bad)
        bad = self.converge_query([])
        bad["scoring"] = {"window_seconds": 0}
        self.assert_converge_code("INVALID_SCORING_CONFIG", bad)

    def test_duplicate_and_value_precedence(self):
        bad = self.converge_query(
            [tx("dup", "s1", "R"), tx("dup", "s2", "R", ts="bad")])
        self.assert_converge_code("DUPLICATE_TRANSFER_ID", bad)
        bad = self.converge_query([tx("t1", "s1", "R", amount=0)])
        self.assert_converge_code("INVALID_TRANSFER_VALUE", bad)


class ConvergeCliTests(unittest.TestCase):
    def run_cli(self, raw):
        return subprocess.run(
            [BIN, "converge"], input=raw, capture_output=True, text=True
        )

    def test_success_stdout(self):
        body = {
            "transfers": [tx("t1", "s1", "R", usd=1),
                           tx("t2", "s2", "R", usd=1)],
            "whale_threshold_usd": 10000,
            "routes": [],
            "convergence": {"window_seconds": 3600, "min_sources": 2,
                            "min_usd_value": 0},
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(set(out), {"data"})
        self.assertEqual(set(out["data"]), {"events", "alerts"})
        self.assertEqual(out["data"]["events"][0]["event_id"], "t1>t2")
        self.assertEqual(proc.stderr, "")

    def test_empty_events_and_alerts(self):
        body = {
            "transfers": [], "whale_threshold_usd": 10000, "routes": [],
            "convergence": {"window_seconds": 3600, "min_sources": 2,
                            "min_usd_value": 0},
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            {"data": {"events": [], "alerts": []}},
        )

    def test_not_json(self):
        proc = self.run_cli("{not json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr), {"error": "INPUT_NOT_JSON"})

    def test_error_codes_via_cli(self):
        proc = self.run_cli(json.dumps({"transfers": []}))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_INPUT_SCHEMA"}
        )
        body = {
            "transfers": [], "whale_threshold_usd": 10000, "routes": [],
            "convergence": {"window_seconds": 0, "min_sources": 2,
                            "min_usd_value": 0},
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_CONVERGENCE_QUERY"}
        )
        body = {
            "transfers": [], "whale_threshold_usd": 10000, "routes": [],
            "convergence": {"window_seconds": 3600, "min_sources": 2,
                            "min_usd_value": 0},
            "scoring": {"window_seconds": 0},
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_SCORING_CONFIG"}
        )


class CycleTests(unittest.TestCase):
    def cycle_query(self, transfers, routes=None, threshold=10000.0,
                    max_hops=8, min_usd_value=0):
        result = payload(transfers, threshold=threshold, routes=routes)
        result["cycle_query"] = {
            "max_hops": max_hops,
            "min_usd_value": min_usd_value,
        }
        return result

    def test_two_hop_round_trip(self):
        data = cycles(self.cycle_query([
            tx("out", "A", "B", usd=50000),
            tx("back", "B", "A", usd=50000, ts="2026-10-04T11:00:00Z"),
        ]))
        self.assertEqual(set(data), {"cycles", "alerts"})
        self.assertEqual(len(data["cycles"]), 1)
        cycle = data["cycles"][0]
        self.assertEqual(
            set(cycle),
            {"transfer_ids", "cycle_id", "nodes", "hops", "chain",
             "asset", "amount", "usd_value", "score", "reason", "segments"},
        )
        self.assertEqual(cycle["transfer_ids"], ["out", "back"])
        self.assertEqual(cycle["cycle_id"], "out>back")
        self.assertEqual(cycle["nodes"], ["A", "B", "A"])
        self.assertEqual(cycle["hops"], 2)
        self.assertEqual(cycle["chain"], "eth")
        self.assertEqual(cycle["asset"], "ETH")
        self.assertEqual(cycle["amount"], 2)
        self.assertEqual(cycle["usd_value"], 100000)
        # 每段 40 VALUE + 15 ROUND_TRIP = 55，合计 110 截到 100。
        self.assertEqual(cycle["score"], 100)
        self.assertEqual(cycle["reason"], ["VALUE", "ROUND_TRIP"])
        self.assertEqual(
            cycle["segments"],
            [
                {"transfer_id": "out", "score": 55,
                 "reason": ["VALUE", "ROUND_TRIP"]},
                {"transfer_id": "back", "score": 55,
                 "reason": ["VALUE", "ROUND_TRIP"]},
            ],
        )

    def test_triangle_starts_at_lexicographically_smallest_node(self):
        data = cycles(self.cycle_query([
            tx("ab", "A", "B", usd=10000),
            tx("bc", "B", "C", usd=10000, ts="2026-10-04T10:10:00Z"),
            tx("ca", "C", "A", usd=10000, ts="2026-10-04T10:20:00Z"),
        ]))
        self.assertEqual(len(data["cycles"]), 1)
        cycle = data["cycles"][0]
        self.assertEqual(cycle["transfer_ids"], ["ab", "bc", "ca"])
        self.assertEqual(cycle["cycle_id"], "ab>bc>ca")
        self.assertEqual(cycle["nodes"], ["A", "B", "C", "A"])
        self.assertEqual(cycle["hops"], 3)
        # 20*3 = 60。
        self.assertEqual(cycle["score"], 60)
        self.assertEqual(cycle["reason"], ["VALUE"])

    def test_reverse_direction_is_a_distinct_edge_sequence(self):
        data = cycles(self.cycle_query([
            tx("ab", "A", "B", usd=10000),
            tx("bc", "B", "C", usd=10000, ts="2026-10-04T10:10:00Z"),
            tx("ca", "C", "A", usd=10000, ts="2026-10-04T10:20:00Z"),
            tx("ac", "A", "C", usd=10000, ts="2026-10-04T11:00:00Z"),
            tx("cb", "C", "B", usd=10000, ts="2026-10-04T11:10:00Z"),
            tx("ba", "B", "A", usd=10000, ts="2026-10-04T11:20:00Z"),
        ]))
        ids = {c["cycle_id"] for c in data["cycles"]}
        # 两个方向的 3 跳回路，以及三对反向边形成的 2 跳回路，均保留。
        self.assertIn("ab>bc>ca", ids)
        self.assertIn("ac>cb>ba", ids)
        self.assertEqual(
            ids & {"ab>ba", "ac>ca", "bc>cb"},
            {"ab>ba", "ac>ca", "bc>cb"},
        )
        reversed_cycle = next(c for c in data["cycles"]
                              if c["cycle_id"] == "ac>cb>ba")
        self.assertEqual(reversed_cycle["nodes"], ["A", "C", "B", "A"])

    def test_parallel_edges_keep_every_edge_sequence(self):
        data = cycles(self.cycle_query([
            tx("t1", "A", "B", amount=1.0, usd=10000),
            tx("t2", "B", "A", amount=1.0, usd=10000,
               ts="2026-10-04T11:00:00Z"),
            tx("t3", "A", "B", amount=2.0, usd=10000,
               ts="2026-10-04T12:00:00Z"),
            tx("t4", "B", "A", amount=2.0, usd=10000,
               ts="2026-10-04T13:00:00Z"),
        ]))
        # 仅 t1/t2 与 t3/t4 互为等 amount 反向转账；但不同边序列全部保留。
        self.assertEqual(
            sorted(c["cycle_id"] for c in data["cycles"]),
            ["t1>t2", "t1>t4", "t3>t2", "t3>t4"],
        )
        for cycle in data["cycles"]:
            self.assertEqual(cycle["nodes"], ["A", "B", "A"])

    def test_cycles_stay_within_same_chain_and_asset(self):
        data = cycles(self.cycle_query([
            tx("a", "A", "B", chain="eth", asset="ETH"),
            tx("b", "B", "A", chain="bsc", asset="ETH",
               ts="2026-10-04T11:00:00Z"),
            tx("c", "A", "B", chain="eth", asset="USDC",
               ts="2026-10-04T12:00:00Z"),
        ]))
        self.assertEqual(data["cycles"], [])

    def test_self_transfer_never_a_cycle(self):
        data = cycles(self.cycle_query([
            tx("s", "A", "A", usd=99999),
        ]))
        self.assertEqual(data["cycles"], [])

    def test_max_hops_limits_cycle_length(self):
        triangle = [
            tx("ab", "A", "B", usd=10000),
            tx("bc", "B", "C", usd=10000, ts="2026-10-04T10:10:00Z"),
            tx("ca", "C", "A", usd=10000, ts="2026-10-04T10:20:00Z"),
        ]
        self.assertEqual(cycles(self.cycle_query(triangle, max_hops=2))["cycles"],
                         [])
        data = cycles(self.cycle_query(triangle, max_hops=3))
        self.assertEqual(
            [c["cycle_id"] for c in data["cycles"]], ["ab>bc>ca"]
        )

    def test_four_hop_cycle(self):
        data = cycles(self.cycle_query([
            tx("ab", "A", "B", usd=10000),
            tx("bc", "B", "C", usd=10000, ts="2026-10-04T10:10:00Z"),
            tx("cd", "C", "D", usd=10000, ts="2026-10-04T10:20:00Z"),
            tx("da", "D", "A", usd=10000, ts="2026-10-04T10:30:00Z"),
        ]))
        self.assertEqual(
            [c["cycle_id"] for c in data["cycles"]], ["ab>bc>cd>da"]
        )
        self.assertEqual(data["cycles"][0]["nodes"], ["A", "B", "C", "D", "A"])

    def test_min_usd_value_filter_inclusive(self):
        transfers = [
            tx("out", "A", "B", usd=100),
            tx("back", "B", "A", usd=100, ts="2026-10-04T11:00:00Z"),
        ]
        self.assertEqual(
            cycles(self.cycle_query(transfers, min_usd_value=201))["cycles"],
            [],
        )
        data = cycles(self.cycle_query(transfers, min_usd_value=200))
        self.assertEqual(len(data["cycles"]), 1)

    def test_cycles_sorted_score_desc_chain_asset_cycle_id(self):
        transfers = [
            tx("e1", "A", "B", usd=50000, chain="eth", asset="ETH"),
            tx("e2", "B", "A", usd=50000, chain="eth", asset="ETH",
               ts="2026-10-04T11:00:00Z"),
            tx("s1", "A", "B", usd=50000, chain="bsc", asset="BNB",
               ts="2026-10-04T12:00:00Z"),
            tx("s2", "B", "A", usd=50000, chain="bsc", asset="BNB",
               ts="2026-10-04T13:00:00Z"),
        ]
        # 同分：chain 升序（bsc 在 eth 前）。
        self.assertEqual(
            [(c["chain"], c["asset"], c["cycle_id"])
             for c in cycles(self.cycle_query(transfers))["cycles"]],
            [("bsc", "BNB", "s1>s2"), ("eth", "ETH", "e1>e2")],
        )

    def test_cycle_id_tie_break_ascending(self):
        data = cycles(self.cycle_query([
            tx("z1", "A", "B", usd=0),
            tx("z2", "B", "A", usd=0, ts="2026-10-04T11:00:00Z"),
            tx("a1", "A", "C", usd=0, ts="2026-10-04T12:00:00Z"),
            tx("a2", "C", "A", usd=0, ts="2026-10-04T13:00:00Z"),
        ]))
        # 两组 2 跳反向等 amount 转账，每段 15 分，合计 30；cycle_id 升序。
        self.assertEqual(
            [c["cycle_id"] for c in data["cycles"]],
            ["a1>a2", "z1>z2"],
        )

    def test_amount_usd_round10_representation(self):
        data = cycles(self.cycle_query([
            tx("a", "A", "B", amount=0.1, usd=0.2),
            tx("b", "B", "A", amount=0.2, usd=0.1,
               ts="2026-10-04T11:00:00Z"),
        ]))
        cycle = data["cycles"][0]
        self.assertEqual(cycle["amount"], 0.3)
        self.assertEqual(cycle["usd_value"], 0.3)

    def test_reason_merged_dedup_in_fixed_order(self):
        data = cycles(self.cycle_query([
            tx("out", "A", "B", usd=50000),
            tx("back", "B", "A", usd=0, ts="2026-10-04T11:00:00Z"),
        ]))
        cycle = data["cycles"][0]
        # out 有 VALUE/ROUND_TRIP，back 仅 ROUND_TRIP；合并去重且固定顺序。
        self.assertEqual(cycle["reason"], ["VALUE", "ROUND_TRIP"])

    def test_empty_cycles_keep_arrays(self):
        data = cycles(self.cycle_query([tx("t", "A", "B")]))
        self.assertEqual(data, {"cycles": [], "alerts": []})

    def test_non_ascii_preserved(self):
        data = cycles(self.cycle_query([
            tx("转甲", "地址甲", "地址乙", usd=10000),
            tx("转乙", "地址乙", "地址甲", usd=10000,
               ts="2026-10-04T11:00:00Z"),
        ]))
        cycle = data["cycles"][0]
        self.assertEqual(cycle["nodes"], ["地址乙", "地址甲", "地址乙"])
        self.assertEqual(cycle["cycle_id"], "转乙>转甲")

    def test_alerts_one_per_route_cycle_and_sorted(self):
        routes = [
            route("r2", 60, "warning", chains=("eth",), target="email"),
            route("r1", 60, "critical", chains=("eth",), assets=("ETH",),
                  target="pager"),
            route("r-btc", 0, "info", chains=("btc",), target="x"),
        ]
        data = cycles(self.cycle_query([
            tx("ab", "A", "B", usd=10000),
            tx("bc", "B", "C", usd=10000, ts="2026-10-04T10:10:00Z"),
            tx("ca", "C", "A", usd=10000, ts="2026-10-04T10:20:00Z"),
        ], routes=routes))
        alerts = data["alerts"]
        self.assertEqual(
            [(a["route_id"], a["cycle_id"]) for a in alerts],
            [("r1", "ab>bc>ca"), ("r2", "ab>bc>ca")],
        )
        first = alerts[0]
        self.assertEqual(
            set(first),
            {"route_id", "cycle_id", "chain", "asset", "severity",
             "score", "reason", "target"},
        )
        self.assertEqual(first["chain"], "eth")
        self.assertEqual(first["asset"], "ETH")
        self.assertEqual(first["severity"], "critical")
        self.assertEqual(first["target"], "pager")
        self.assertEqual(first["score"], 60)
        self.assertEqual(first["reason"], ["VALUE"])

    def test_alert_min_score_boundary_inclusive(self):
        data = cycles(self.cycle_query(
            [tx("a", "A", "B", usd=10000),
             tx("b", "B", "A", usd=10000, ts="2026-10-04T11:00:00Z")],
            routes=[route("r", 70, "info")],
        ))
        # 每段 20 VALUE + 15 ROUND_TRIP = 35，合计 70，恰好命中。
        self.assertEqual(
            [(a["route_id"], a["cycle_id"]) for a in data["alerts"]],
            [("r", "a>b")],
        )

    def test_alert_chain_asset_star_and_mismatch(self):
        data = cycles(self.cycle_query(
            [tx("a", "A", "B", usd=50000),
             tx("b", "B", "A", usd=50000, ts="2026-10-04T11:00:00Z")],
            routes=[route("r", 0, "info", assets=("BTC",))],
        ))
        self.assertEqual(data["alerts"], [])

    def assert_cycle_code(self, code, obj):
        with self.assertRaises(AnalyzeError) as ctx:
            cycles(obj)
        self.assertEqual(ctx.exception.code, code)

    def test_cycle_query_errors(self):
        base = [tx("a", "A", "B"), tx("b", "B", "A")]
        # 缺失 cycle_query 整体或任一字段。
        self.assert_cycle_code("INVALID_CYCLE_QUERY", payload(base))
        for field in ("max_hops", "min_usd_value"):
            bad = self.cycle_query(base)
            del bad["cycle_query"][field]
            self.assert_cycle_code("INVALID_CYCLE_QUERY", bad)
        # 未知字段与非对象。
        bad = self.cycle_query(base)
        bad["cycle_query"]["unknown"] = 1
        self.assert_cycle_code("INVALID_CYCLE_QUERY", bad)
        for raw in ([], "x", 1, None, True):
            bad = self.cycle_query(base)
            bad["cycle_query"] = raw
            self.assert_cycle_code("INVALID_CYCLE_QUERY", bad)
        # max_hops：2..8 整数。
        for bad_value in (0, 1, 9, 2.0, 2.5, "2", True, None):
            self.assert_cycle_code(
                "INVALID_CYCLE_QUERY",
                self.cycle_query(base, max_hops=bad_value),
            )
        # min_usd_value：非负有限数。
        for bad_value in (-0.01, "0", True, None,
                          float("inf"), float("nan")):
            self.assert_cycle_code(
                "INVALID_CYCLE_QUERY",
                self.cycle_query(base, min_usd_value=bad_value),
            )
        # 边界合法。
        cycles(self.cycle_query(base, max_hops=2, min_usd_value=0))
        cycles(self.cycle_query(base, max_hops=8, min_usd_value=0.0))

    def test_error_precedence(self):
        # threshold -> route -> cycle query -> scoring。
        bad = self.cycle_query([], threshold=0,
                               routes=[route("r", 200, "info")],
                               max_hops=1)
        self.assert_cycle_code("INVALID_THRESHOLD", bad)
        bad = self.cycle_query([], routes=[route("r", 200, "info")],
                               max_hops=1)
        self.assert_cycle_code("INVALID_ROUTE", bad)
        bad = self.cycle_query([], max_hops=1)
        bad["scoring"] = []
        self.assert_cycle_code("INVALID_CYCLE_QUERY", bad)
        bad = self.cycle_query([])
        bad["scoring"] = {"window_seconds": 0}
        self.assert_cycle_code("INVALID_SCORING_CONFIG", bad)

    def test_duplicate_and_value_precedence(self):
        bad = self.cycle_query(
            [tx("dup", "A", "B"), tx("dup", "B", "A", ts="bad")])
        self.assert_cycle_code("DUPLICATE_TRANSFER_ID", bad)
        bad = self.cycle_query([tx("t1", "A", "B", amount=0)])
        self.assert_cycle_code("INVALID_TRANSFER_VALUE", bad)


class CycleScoringTests(unittest.TestCase):
    def cycle_with_scoring(self, scoring, **kwargs):
        # 两段 amount 不同，避免 ROUND_TRIP 加分干扰逐段 VALUE 求和。
        transfers = kwargs.pop("transfers", [
            tx("a", "A", "B", amount=1.0, usd=10000),
            tx("b", "B", "A", amount=2.0, usd=5000,
               ts="2026-10-04T11:00:00Z"),
        ])
        max_hops = kwargs.pop("max_hops", 4)
        min_usd_value = kwargs.pop("min_usd_value", 0)
        body = {
            "transfers": transfers,
            "whale_threshold_usd": 10000,
            "routes": kwargs.pop("routes", []),
            "cycle_query": {"max_hops": max_hops,
                            "min_usd_value": min_usd_value},
        }
        body.update(kwargs)
        body["scoring"] = scoring
        return cycles(body)

    def test_scoring_changes_segment_sum(self):
        data = self.cycle_with_scoring(
            {"value_points_per_ratio": 10, "value_points_cap": 100}
        )
        cycle = data["cycles"][0]
        # a=10、b=5，合计 15。
        self.assertEqual(cycle["score"], 15)
        self.assertEqual(cycle["reason"], ["VALUE"])

    def test_sum_caps_at_100(self):
        data = self.cycle_with_scoring(
            {"value_points_per_ratio": 1000, "value_points_cap": 100},
            transfers=[
                tx("a", "A", "B", usd=10000),
                tx("b", "B", "A", usd=10000,
                   ts="2026-10-04T11:00:00Z"),
            ],
        )
        self.assertEqual(data["cycles"][0]["score"], 100)

    def test_invalid_scoring_after_cycle_query(self):
        with self.assertRaises(AnalyzeError) as ctx:
            self.cycle_with_scoring([])
        self.assertEqual(ctx.exception.code, "INVALID_SCORING_CONFIG")

    def test_cycle_query_error_precedes_scoring(self):
        with self.assertRaises(AnalyzeError) as ctx:
            self.cycle_with_scoring([], max_hops=0)
        self.assertEqual(ctx.exception.code, "INVALID_CYCLE_QUERY")


class CycleCliTests(unittest.TestCase):
    def run_cli(self, raw):
        return subprocess.run(
            [BIN, "cycles"], input=raw, capture_output=True, text=True
        )

    def test_success_stdout(self):
        body = {
            "transfers": [
                tx("a", "A", "B", usd=1),
                tx("b", "B", "A", usd=1, ts="2026-10-04T11:00:00Z"),
            ],
            "whale_threshold_usd": 10000,
            "routes": [],
            "cycle_query": {"max_hops": 3, "min_usd_value": 0},
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(set(out), {"data"})
        self.assertEqual(set(out["data"]), {"cycles", "alerts"})
        self.assertEqual(out["data"]["cycles"][0]["cycle_id"], "a>b")
        self.assertEqual(proc.stderr, "")

    def test_empty_cycles_and_alerts(self):
        body = {
            "transfers": [],
            "whale_threshold_usd": 10000,
            "routes": [],
            "cycle_query": {"max_hops": 3, "min_usd_value": 0},
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            {"data": {"cycles": [], "alerts": []}},
        )

    def test_not_json(self):
        proc = self.run_cli("{not json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr), {"error": "INPUT_NOT_JSON"})

    def test_error_codes_via_cli(self):
        proc = self.run_cli(json.dumps({"transfers": []}))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_INPUT_SCHEMA"}
        )
        body = {
            "transfers": [], "whale_threshold_usd": 10000, "routes": [],
            "cycle_query": {"max_hops": 1, "min_usd_value": 0},
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_CYCLE_QUERY"}
        )
        body = {
            "transfers": [], "whale_threshold_usd": 10000, "routes": [],
            "cycle_query": {"max_hops": 3, "min_usd_value": 0},
            "scoring": {"window_seconds": 0},
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_SCORING_CONFIG"}
        )

    def test_non_ascii_round_trip(self):
        body = {
            "transfers": [
                tx("转甲", "地址甲", "地址乙", usd=1),
                tx("转乙", "地址乙", "地址甲", usd=1,
                   ts="2026-10-04T11:00:00Z"),
            ],
            "whale_threshold_usd": 10000,
            "routes": [],
            "cycle_query": {"max_hops": 3, "min_usd_value": 0},
        }
        proc = self.run_cli(json.dumps(body, ensure_ascii=False))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(
            out["data"]["cycles"][0]["cycle_id"], "转乙>转甲"
        )


class LayeringTests(unittest.TestCase):
    def layering_query(self, transfers, routes=None, threshold=10000.0,
                       window_seconds=3600, min_sources=2, min_recipients=2,
                       min_usd_value=0):
        result = payload(transfers, threshold=threshold, routes=routes)
        result["layering_query"] = {
            "window_seconds": window_seconds,
            "min_sources": min_sources,
            "min_recipients": min_recipients,
            "min_usd_value": min_usd_value,
        }
        return result

    def funnel(self, ts_shift=0):
        ts_in = "2026-10-04T10:%02d:00Z"
        return [
            tx("t1", "s1", "H", amount=1.0, usd=50000, ts=ts_in % 0),
            tx("t2", "s2", "H", amount=2.0, usd=50000, ts=ts_in % 1),
            tx("t3", "H", "r1", amount=1.0, usd=50000, ts=ts_in % 2),
            tx("t4", "H", "r2", amount=2.0, usd=50000, ts=ts_in % 3),
        ]

    def test_basic_collect_then_distribute_event(self):
        data = layering(self.layering_query(self.funnel()))
        self.assertEqual(set(data), {"layering", "alerts"})
        events = data["layering"]
        # t2 起点窗口缺一个来源，只有 t1 起点成事件。
        self.assertEqual([e["event_id"] for e in events], ["t1"])
        event = events[0]
        self.assertEqual(
            set(event),
            {"event_id", "chain", "asset", "address", "transfer_ids",
             "source_count", "recipient_count", "amount", "usd_value",
             "score", "reason", "segments"},
        )
        self.assertEqual(event["address"], "H")
        self.assertEqual(event["chain"], "eth")
        self.assertEqual(event["asset"], "ETH")
        self.assertEqual(event["transfer_ids"], ["t1", "t2", "t3", "t4"])
        self.assertEqual(event["source_count"], 2)
        self.assertEqual(event["recipient_count"], 2)
        self.assertEqual(event["amount"], 6)
        self.assertEqual(event["usd_value"], 200000)
        # 四段各 40 VALUE，合计 160 截到 100。
        self.assertEqual(event["score"], 100)
        self.assertEqual(event["reason"], ["VALUE"])
        self.assertEqual(
            event["segments"],
            [
                {"transfer_id": "t1", "score": 40, "reason": ["VALUE"]},
                {"transfer_id": "t2", "score": 40, "reason": ["VALUE"]},
                {"transfer_id": "t3", "score": 40, "reason": ["VALUE"]},
                {"transfer_id": "t4", "score": 40, "reason": ["VALUE"]},
            ],
        )

    def test_same_timestamp_anchors_dedup_keep_min_id(self):
        transfers = [
            tx("t2", "s1", "H", usd=1, ts="2026-10-04T10:00:00Z"),
            tx("t1", "s2", "H", usd=1, ts="2026-10-04T10:00:00Z"),
            tx("t3", "H", "r1", usd=1, ts="2026-10-04T10:00:10Z"),
            tx("t4", "H", "r2", usd=1, ts="2026-10-04T10:00:20Z"),
        ]
        data = layering(self.layering_query(transfers))
        events = data["layering"]
        # 两个同 timestamp 转入起点窗口完全相同，仅留最小起点 id。
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], "t1")
        self.assertEqual(events[0]["transfer_ids"],
                         ["t1", "t2", "t3", "t4"])

    def test_window_closed_boundary(self):
        transfers = [
            tx("t1", "s1", "H", usd=1, ts="2026-10-04T10:00:00Z"),
            tx("t2", "s2", "H", usd=1, ts="2026-10-04T10:10:00Z"),
            tx("t3", "H", "r1", usd=1, ts="2026-10-04T10:10:00Z"),
            tx("t4", "H", "r2", usd=1, ts="2026-10-04T10:10:01Z"),
        ]
        # 600 秒闭窗口：t2/t3 恰在边界内，t4 在 601 秒外，接收方不足。
        self.assertEqual(
            layering(self.layering_query(transfers, window_seconds=600))[
                "layering"],
            [],
        )
        data = layering(self.layering_query(transfers, window_seconds=601))
        self.assertEqual(
            [e["event_id"] for e in data["layering"]], ["t1"]
        )

    def test_outgoing_transfer_never_anchors(self):
        # 只有 s1 一个来源；H->r1 不能作为起点补出事件。
        data = layering(self.layering_query([
            tx("t1", "s1", "H", usd=1),
            tx("t2", "H", "r1", usd=1, ts="2026-10-04T10:00:10Z"),
            tx("t3", "H", "r2", usd=1, ts="2026-10-04T10:00:20Z"),
        ]))
        self.assertEqual(data["layering"], [])

    def test_self_transfers_ignored_everywhere(self):
        data = layering(self.layering_query([
            tx("t1", "s1", "H", usd=1),
            tx("self", "H", "H", amount=7.0, usd=99999,
               ts="2026-10-04T10:00:05Z"),
            tx("t2", "s2", "H", usd=1, ts="2026-10-04T10:00:10Z"),
            tx("t3", "H", "r1", usd=1, ts="2026-10-04T10:00:20Z"),
            tx("t4", "H", "r2", usd=1, ts="2026-10-04T10:00:30Z"),
        ]))
        events = data["layering"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["transfer_ids"],
                         ["t1", "t2", "t3", "t4"])
        self.assertEqual(events[0]["source_count"], 2)
        self.assertEqual(events[0]["recipient_count"], 2)
        self.assertEqual(events[0]["usd_value"], 4)

        # H->H 不能凑接收方：真实接收方只有 r1。
        data = layering(self.layering_query([
            tx("t1", "s1", "H", usd=1),
            tx("t2", "s2", "H", usd=1, ts="2026-10-04T10:00:10Z"),
            tx("self", "H", "H", amount=7.0, usd=99999,
               ts="2026-10-04T10:00:15Z"),
            tx("t3", "H", "r1", usd=1, ts="2026-10-04T10:00:20Z"),
        ]))
        self.assertEqual(data["layering"], [])

    def test_groups_split_by_chain_asset_hub(self):
        data = layering(self.layering_query([
            tx("t1", "s1", "H", usd=1),
            tx("t2", "s2", "H", usd=1, chain="bsc", asset="BNB",
               ts="2026-10-04T10:00:10Z"),
            tx("t3", "H", "r1", usd=1, ts="2026-10-04T10:00:20Z"),
            tx("t4", "H", "r2", usd=1, ts="2026-10-04T10:00:30Z"),
        ]))
        # bsc 上无分发，eth 上只有一个跨链来源不计入。
        self.assertEqual(data["layering"], [])

    def test_min_counts_and_usd_filters_with_inclusive_boundary(self):
        transfers = self.funnel()
        self.assertEqual(
            layering(self.layering_query(transfers, min_sources=3))["layering"],
            [],
        )
        self.assertEqual(
            layering(self.layering_query(
                transfers, min_recipients=3))["layering"],
            [],
        )
        self.assertEqual(
            layering(self.layering_query(
                transfers, min_usd_value=200001))["layering"],
            [],
        )
        data = layering(self.layering_query(
            transfers, min_usd_value=200000))
        self.assertEqual(len(data["layering"]), 1)

    def test_duplicate_sources_and_recipients_counted_once(self):
        data = layering(self.layering_query([
            tx("t1", "s1", "H", usd=1),
            tx("t2", "s1", "H", usd=1, ts="2026-10-04T10:00:10Z"),
            tx("t3", "H", "r1", usd=1, ts="2026-10-04T10:00:20Z"),
            tx("t4", "H", "r1", usd=1, ts="2026-10-04T10:00:30Z"),
        ]))
        self.assertEqual(data["layering"], [])

    def test_segment_reasons_merged_dedup_in_fixed_order(self):
        # H 在窗口内向 5 个接收方发出 5 笔（无 VALUE）：t3 命中
        # BURST+FAN_OUT；t1/t2 0 分。
        outgoing = [
            tx("o%d" % i, "H", "r%d" % i, usd=0,
               ts="2026-10-04T10:00:%02dZ" % (20 + i * 5))
            for i in range(5)
        ]
        transfers = [
            tx("t1", "s1", "H", usd=0),
            tx("t2", "s2", "H", usd=0, ts="2026-10-04T10:00:10Z"),
        ] + outgoing
        data = layering(
            self.layering_query(transfers, min_recipients=2))
        event = data["layering"][0]
        self.assertIn("BURST", event["reason"])
        self.assertIn("FAN_OUT", event["reason"])
        # o0=45，o1/o2 各 20（扇出），其余 0，合计 85。
        self.assertEqual(event["score"], 85)
        self.assertEqual(
            event["segments"][2]["reason"], ["BURST", "FAN_OUT"])

    def test_round_trip_reason_merges_from_segments(self):
        data = layering(self.layering_query([
            tx("t1", "s1", "H", amount=3.0, usd=50000),
            tx("t2", "s2", "H", amount=3.0, usd=50000,
               ts="2026-10-04T10:00:10Z"),
            tx("t3", "H", "s1", amount=3.0, usd=50000,
               ts="2026-10-04T10:00:20Z"),
            tx("t4", "H", "r2", amount=3.0, usd=50000,
               ts="2026-10-04T10:00:30Z"),
        ]))
        event = data["layering"][0]
        self.assertEqual(event["reason"], ["VALUE", "ROUND_TRIP"])
        self.assertEqual(
            event["segments"][0]["reason"], ["VALUE", "ROUND_TRIP"])

    def test_multiple_windows_same_hub_each_kept(self):
        transfers = [
            tx("t1", "s1", "H", usd=0),
            tx("t2", "s2", "H", usd=0, ts="2026-10-04T10:00:10Z"),
            tx("t3", "H", "r1", usd=0, ts="2026-10-04T10:00:20Z"),
            tx("t4", "H", "r2", usd=0, ts="2026-10-04T10:00:30Z"),
            tx("t5", "s3", "H", usd=0, ts="2026-10-04T12:00:00Z"),
            tx("t6", "s4", "H", usd=0, ts="2026-10-04T12:00:10Z"),
            tx("t7", "H", "r3", usd=0, ts="2026-10-04T12:00:20Z"),
            tx("t8", "H", "r4", usd=0, ts="2026-10-04T12:00:30Z"),
        ]
        data = layering(self.layering_query(transfers))
        self.assertEqual(
            [e["event_id"] for e in data["layering"]], ["t1", "t5"])
        self.assertEqual(
            data["layering"][1]["transfer_ids"],
            ["t5", "t6", "t7", "t8"],
        )

    def test_sorted_score_desc_chain_asset_address_event_id(self):
        transfers = [
            tx("e1", "s1", "A", usd=0, chain="eth", asset="ETH"),
            tx("e2", "s2", "A", usd=0, chain="eth", asset="ETH",
               ts="2026-10-04T10:00:10Z"),
            tx("e3", "A", "r1", usd=0, chain="eth", asset="ETH",
               ts="2026-10-04T10:00:20Z"),
            tx("e4", "A", "r2", usd=0, chain="eth", asset="ETH",
               ts="2026-10-04T10:00:30Z"),
            tx("b1", "s1", "B", usd=0, chain="bsc", asset="BNB",
               ts="2026-10-04T11:00:00Z"),
            tx("b2", "s2", "B", usd=0, chain="bsc", asset="BNB",
               ts="2026-10-04T11:00:10Z"),
            tx("b3", "B", "r1", usd=0, chain="bsc", asset="BNB",
               ts="2026-10-04T11:00:20Z"),
            tx("b4", "B", "r2", usd=0, chain="bsc", asset="BNB",
               ts="2026-10-04T11:00:30Z"),
        ]
        # 全部 0 分：按 chain、asset、address、event_id 升序。
        data = layering(self.layering_query(transfers))
        self.assertEqual(
            [(e["chain"], e["asset"], e["address"], e["event_id"])
             for e in data["layering"]],
            [("bsc", "BNB", "B", "b1"), ("eth", "ETH", "A", "e1")],
        )

    def test_amount_usd_round10_representation(self):
        data = layering(self.layering_query([
            tx("t1", "s1", "H", amount=0.1, usd=0.2),
            tx("t2", "s2", "H", amount=0.2, usd=0.1,
               ts="2026-10-04T10:00:10Z"),
            tx("t3", "H", "r1", amount=0.3, usd=0.3,
               ts="2026-10-04T10:00:20Z"),
            tx("t4", "H", "r2", amount=0.4, usd=0.4,
               ts="2026-10-04T10:00:30Z"),
        ]))
        event = data["layering"][0]
        self.assertEqual(event["amount"], 1.0)
        self.assertEqual(event["usd_value"], 1.0)

    def test_empty_events_keep_arrays(self):
        data = layering(self.layering_query([tx("t1", "s1", "H")]))
        self.assertEqual(data, {"layering": [], "alerts": []})

    def test_alerts_one_per_route_event_and_sorted(self):
        routes = [
            route("r2", 40, "warning", chains=("eth",), target="email"),
            route("r1", 100, "critical", chains=("eth",), assets=("ETH",),
                  target="pager"),
            route("r-btc", 0, "info", chains=("btc",), target="x"),
        ]
        data = layering(self.layering_query(self.funnel(), routes=routes))
        alerts = data["alerts"]
        self.assertEqual(
            [(a["route_id"], a["event_id"]) for a in alerts],
            [("r1", "t1"), ("r2", "t1")],
        )
        first = alerts[0]
        self.assertEqual(
            set(first),
            {"route_id", "event_id", "chain", "asset", "severity",
             "score", "reason", "target"},
        )
        self.assertEqual(first["chain"], "eth")
        self.assertEqual(first["asset"], "ETH")
        self.assertEqual(first["severity"], "critical")
        self.assertEqual(first["target"], "pager")
        self.assertEqual(first["score"], 100)
        self.assertEqual(first["reason"], ["VALUE"])

    def test_alert_min_score_boundary_inclusive(self):
        data = layering(self.layering_query(
            [
                tx("t1", "s1", "H", usd=0),
                tx("t2", "s2", "H", usd=0, ts="2026-10-04T10:00:10Z"),
                tx("t3", "H", "r1", usd=0, ts="2026-10-04T10:00:20Z"),
                tx("t4", "H", "r2", usd=0, ts="2026-10-04T10:00:30Z"),
            ],
            routes=[route("r", 0, "info")],
        ))
        self.assertEqual(
            [(a["route_id"], a["event_id"]) for a in data["alerts"]],
            [("r", "t1")],
        )

    def test_alert_chain_asset_star_and_mismatch(self):
        data = layering(self.layering_query(
            self.funnel(),
            routes=[route("r", 0, "info", assets=("BTC",))],
        ))
        self.assertEqual(data["alerts"], [])

    def assert_layering_code(self, code, obj):
        with self.assertRaises(AnalyzeError) as ctx:
            layering(obj)
        self.assertEqual(ctx.exception.code, code)

    def test_layering_query_errors(self):
        base = [tx("t1", "s1", "H")]
        # 缺失 layering_query 整体或任一字段。
        self.assert_layering_code("INVALID_LAYERING_QUERY", payload(base))
        for field in ("window_seconds", "min_sources", "min_recipients",
                      "min_usd_value"):
            bad = self.layering_query(base)
            del bad["layering_query"][field]
            self.assert_layering_code("INVALID_LAYERING_QUERY", bad)
        # 未知字段与非对象。
        bad = self.layering_query(base)
        bad["layering_query"]["unknown"] = 1
        self.assert_layering_code("INVALID_LAYERING_QUERY", bad)
        for raw in ([], "x", 1, None, True):
            bad = self.layering_query(base)
            bad["layering_query"] = raw
            self.assert_layering_code("INVALID_LAYERING_QUERY", bad)
        # window_seconds：1..10000 整数。
        for bad_value in (0, -1, 10001, 1.0, "5", True, None):
            self.assert_layering_code(
                "INVALID_LAYERING_QUERY",
                self.layering_query(base, window_seconds=bad_value),
            )
        # min_sources / min_recipients：2..10000 整数。
        for field in ("min_sources", "min_recipients"):
            for bad_value in (1, 0, -1, 10001, 2.0, "2", True, None):
                kwargs = {field: bad_value}
                self.assert_layering_code(
                    "INVALID_LAYERING_QUERY",
                    self.layering_query(base, **kwargs),
                )
        # min_usd_value：非负有限数。
        for bad_value in (-0.01, "0", True, None,
                          float("inf"), float("nan")):
            self.assert_layering_code(
                "INVALID_LAYERING_QUERY",
                self.layering_query(base, min_usd_value=bad_value),
            )
        # 边界合法。
        layering(self.layering_query(
            base, window_seconds=1, min_sources=2, min_recipients=2,
            min_usd_value=0))
        layering(self.layering_query(
            base, window_seconds=10000, min_sources=10000,
            min_recipients=10000, min_usd_value=0.0))

    def test_error_precedence(self):
        # threshold -> route -> layering query -> scoring。
        bad = self.layering_query([], threshold=0,
                                  routes=[route("r", 200, "info")],
                                  min_sources=1)
        self.assert_layering_code("INVALID_THRESHOLD", bad)
        bad = self.layering_query([], routes=[route("r", 200, "info")],
                                  min_sources=1)
        self.assert_layering_code("INVALID_ROUTE", bad)
        bad = self.layering_query([], min_sources=1)
        bad["scoring"] = []
        self.assert_layering_code("INVALID_LAYERING_QUERY", bad)
        bad = self.layering_query([])
        bad["scoring"] = {"window_seconds": 0}
        self.assert_layering_code("INVALID_SCORING_CONFIG", bad)

    def test_duplicate_and_value_precedence(self):
        bad = self.layering_query(
            [tx("dup", "s1", "H"), tx("dup", "s2", "H", ts="bad")])
        self.assert_layering_code("DUPLICATE_TRANSFER_ID", bad)
        bad = self.layering_query([tx("t1", "s1", "H", amount=0)])
        self.assert_layering_code("INVALID_TRANSFER_VALUE", bad)


class LayeringScoringTests(unittest.TestCase):
    def layering_with_scoring(self, scoring, **kwargs):
        transfers = kwargs.pop("transfers", [
            tx("t1", "s1", "H", usd=10000),
            tx("t2", "s2", "H", usd=5000, ts="2026-10-04T10:00:10Z"),
            tx("t3", "H", "r1", usd=10000, ts="2026-10-04T10:00:20Z"),
            tx("t4", "H", "r2", usd=5000, ts="2026-10-04T10:00:30Z"),
        ])
        query = {"window_seconds": 3600, "min_sources": 2,
                 "min_recipients": 2, "min_usd_value": 0}
        for field in ("window_seconds", "min_sources", "min_recipients",
                      "min_usd_value"):
            if field in kwargs:
                query[field] = kwargs.pop(field)
        body = {
            "transfers": transfers,
            "whale_threshold_usd": 10000,
            "routes": kwargs.pop("routes", []),
            "layering_query": query,
        }
        body.update(kwargs)
        body["scoring"] = scoring
        return layering(body)

    def test_scoring_changes_segment_sum(self):
        data = self.layering_with_scoring(
            {"value_points_per_ratio": 10, "value_points_cap": 100}
        )
        event = data["layering"][0]
        # 10+5+10+5 = 30。
        self.assertEqual(event["score"], 30)
        self.assertEqual(event["segments"][0]["score"], 10)

    def test_sum_caps_at_100(self):
        data = self.layering_with_scoring(
            {"value_points_per_ratio": 1000, "value_points_cap": 100}
        )
        self.assertEqual(data["layering"][0]["score"], 100)

    def test_invalid_scoring_after_layering_query(self):
        with self.assertRaises(AnalyzeError) as ctx:
            self.layering_with_scoring([])
        self.assertEqual(ctx.exception.code, "INVALID_SCORING_CONFIG")

    def test_layering_query_error_precedes_scoring(self):
        with self.assertRaises(AnalyzeError) as ctx:
            self.layering_with_scoring([], min_sources=1)
        self.assertEqual(ctx.exception.code, "INVALID_LAYERING_QUERY")


class LayeringCliTests(unittest.TestCase):
    BODY = {
        "transfers": [
            tx("t1", "s1", "H", usd=1),
            tx("t2", "s2", "H", usd=1, ts="2026-10-04T10:00:10Z"),
            tx("t3", "H", "r1", usd=1, ts="2026-10-04T10:00:20Z"),
            tx("t4", "H", "r2", usd=1, ts="2026-10-04T10:00:30Z"),
        ],
        "whale_threshold_usd": 10000,
        "routes": [],
        "layering_query": {"window_seconds": 3600, "min_sources": 2,
                           "min_recipients": 2, "min_usd_value": 0},
    }

    def run_cli(self, raw, launcher=None):
        if launcher is None:
            launcher = [BIN]
        return subprocess.run(
            launcher + ["layering"], input=raw,
            capture_output=True, text=True,
        )

    def test_success_stdout(self):
        proc = self.run_cli(json.dumps(self.BODY))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(set(out), {"data"})
        self.assertEqual(set(out["data"]), {"layering", "alerts"})
        self.assertEqual(
            out["data"]["layering"][0]["event_id"], "t1")
        self.assertEqual(proc.stderr, "")

    def test_three_entry_points_identical(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        launchers = [
            [os.path.join(root, "whale-radar")],
            [BIN],
            [sys.executable, "-m", "whale_radar"],
        ]
        raw = json.dumps(self.BODY, ensure_ascii=False)
        outputs = set()
        for launcher in launchers:
            proc = self.run_cli(raw, launcher=launcher)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            outputs.add(proc.stdout)
        self.assertEqual(len(outputs), 1)

    def test_empty_layering_and_alerts(self):
        body = dict(self.BODY, transfers=[])
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            {"data": {"layering": [], "alerts": []}},
        )

    def test_not_json(self):
        proc = self.run_cli("{not json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr),
                         {"error": "INPUT_NOT_JSON"})

    def test_error_codes_via_cli(self):
        proc = self.run_cli(json.dumps({"transfers": []}))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_INPUT_SCHEMA"}
        )
        body = {
            "transfers": [], "whale_threshold_usd": 10000, "routes": [],
            "layering_query": {"window_seconds": 0, "min_sources": 2,
                               "min_recipients": 2, "min_usd_value": 0},
        }
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_LAYERING_QUERY"}
        )
        body["layering_query"]["window_seconds"] = 3600
        body["scoring"] = {"window_seconds": 0}
        proc = self.run_cli(json.dumps(body))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_SCORING_CONFIG"}
        )

    def test_non_ascii_round_trip(self):
        body = {
            "transfers": [
                tx("转一", "来源甲", "中心", usd=1),
                tx("转二", "来源乙", "中心", usd=1,
                   ts="2026-10-04T10:00:10Z"),
                tx("转三", "中心", "接收甲", usd=1,
                   ts="2026-10-04T10:00:20Z"),
                tx("转四", "中心", "接收乙", usd=1,
                   ts="2026-10-04T10:00:30Z"),
            ],
            "whale_threshold_usd": 10000,
            "routes": [],
            "layering_query": {"window_seconds": 3600, "min_sources": 2,
                               "min_recipients": 2, "min_usd_value": 0},
        }
        proc = self.run_cli(json.dumps(body, ensure_ascii=False))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        event = out["data"]["layering"][0]
        self.assertEqual(event["event_id"], "转一")
        self.assertEqual(event["address"], "中心")


if __name__ == "__main__":
    unittest.main()
