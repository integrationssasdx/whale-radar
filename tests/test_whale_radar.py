"""Whale Radar 测试：纯标准库 unittest，运行时不联网、不落盘。"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whale_radar.analyzer import AnalyzeError, analyze
from whale_radar.tracer import TraceError, trace

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


class TraceTests(unittest.TestCase):
    def trace_payload(self, transfers, start, end, max_hops=8,
                      chain="eth", asset="ETH"):
        return {
            "transfers": transfers,
            "chain": chain,
            "asset": asset,
            "start_address": start,
            "end_address": end,
            "max_hops": max_hops,
        }

    def assert_code(self, code, obj):
        with self.assertRaises(TraceError) as ctx:
            trace(obj)
        self.assertEqual(ctx.exception.code, code)

    def test_direct_and_multihop_paths(self):
        # A->C 直达；A->B->C 两跳；A->D->E->C 三跳。
        transfers = [
            tx("a-c", "A", "C", amount=10.0, usd=100),
            tx("a-b", "A", "B", amount=1.0, usd=10,
               ts="2026-10-04T10:01:00Z"),
            tx("b-c", "B", "C", amount=2.0, usd=20,
               ts="2026-10-04T10:02:00Z"),
            tx("a-d", "A", "D", amount=4.0, usd=40,
               ts="2026-10-04T10:03:00Z"),
            tx("d-e", "D", "E", amount=8.0, usd=80,
               ts="2026-10-04T10:04:00Z"),
            tx("e-c", "E", "C", amount=16.0, usd=160,
               ts="2026-10-04T10:05:00Z"),
        ]
        data = trace(self.trace_payload(transfers, "A", "C"))
        paths = data["paths"]
        self.assertEqual(
            [(p["hops"], p["transfer_ids"], p["nodes"]) for p in paths],
            [
                (1, ["a-c"], ["A", "C"]),
                (2, ["a-b", "b-c"], ["A", "B", "C"]),
                (3, ["a-d", "d-e", "e-c"], ["A", "D", "E", "C"]),
            ],
        )
        self.assertEqual(paths[1]["amount"], 3)
        self.assertEqual(paths[1]["usd_value"], 30)
        self.assertEqual(paths[2]["amount"], 28)
        self.assertEqual(paths[2]["usd_value"], 280)

    def test_max_hops_bounds(self):
        transfers = [
            tx("a-b", "A", "B"),
            tx("b-c", "B", "C", ts="2026-10-04T10:01:00Z"),
            tx("c-d", "C", "D", ts="2026-10-04T10:02:00Z"),
        ]
        data = trace(self.trace_payload(transfers, "A", "D", max_hops=2))
        self.assertEqual(data["paths"], [])
        data = trace(self.trace_payload(transfers, "A", "D", max_hops=3))
        self.assertEqual(
            [p["transfer_ids"] for p in data["paths"]],
            [["a-b", "b-c", "c-d"]],
        )

    def test_only_same_chain_and_asset(self):
        transfers = [
            tx("a-b", "A", "B", chain="eth", asset="ETH"),
            tx("b-c1", "B", "C", chain="btc", asset="BTC"),
            tx("b-c2", "B", "C", chain="eth", asset="USDC"),
            tx("b-c3", "B", "C", chain="poly", asset="ETH"),
            tx("b-c", "B", "C", chain="eth", asset="ETH",
               ts="2026-10-04T10:01:00Z"),
        ]
        data = trace(self.trace_payload(transfers, "A", "C"))
        self.assertEqual(
            [p["transfer_ids"] for p in data["paths"]],
            [["a-b", "b-c"]],
        )

    def test_simple_paths_no_repeated_address(self):
        # A->B->A->C 不合法（A 重复）；只有 A->C 可达，另含环 A->B->A。
        transfers = [
            tx("a-b", "A", "B"),
            tx("b-a", "B", "A", ts="2026-10-04T10:01:00Z"),
            tx("a-c", "A", "C", ts="2026-10-04T10:02:00Z"),
        ]
        data = trace(self.trace_payload(transfers, "A", "C"))
        self.assertEqual(
            [p["transfer_ids"] for p in data["paths"]], [["a-c"]]
        )

    def test_diamond_yields_two_same_hop_paths_sorted(self):
        transfers = [
            tx("e1", "A", "B"),
            tx("e2", "A", "X"),
            tx("e3", "X", "C", ts="2026-10-04T10:01:00Z"),
            tx("e4", "B", "C", ts="2026-10-04T10:02:00Z"),
        ]
        data = trace(self.trace_payload(transfers, "A", "C"))
        # hops 相同，按 transfer_ids 字典序：[e1,e4] 在 [e2,e3] 之前。
        self.assertEqual(
            [p["transfer_ids"] for p in data["paths"]],
            [["e1", "e4"], ["e2", "e3"]],
        )

    def test_parallel_edges_both_enumerated(self):
        transfers = [
            tx("p2", "A", "B"),
            tx("p1", "A", "B", ts="2026-10-04T10:01:00Z"),
        ]
        data = trace(self.trace_payload(transfers, "A", "B"))
        self.assertEqual(
            [p["transfer_ids"] for p in data["paths"]], [["p1"], ["p2"]]
        )

    def test_no_path_empty_array_success(self):
        transfers = [tx("t1", "A", "B"), tx("t2", "C", "D",
                     ts="2026-10-04T10:01:00Z")]
        data = trace(self.trace_payload(transfers, "A", "D"))
        self.assertEqual(data, {"paths": []})

    def test_value_representation_keeps_decimals(self):
        transfers = [
            tx("a-b", "A", "B", amount=0.1, usd=0.1),
            tx("b-c", "B", "C", amount=0.2, usd=0.2,
               ts="2026-10-04T10:01:00Z"),
        ]
        data = trace(self.trace_payload(transfers, "A", "C"))
        path = data["paths"][0]
        self.assertEqual(path["amount"], 0.3)
        self.assertEqual(path["usd_value"], 0.3)
        self.assertNotIsInstance(path["amount"], int)

    def test_validation_order_transfers_before_query(self):
        bad_ts = tx("dup", "A", "B", ts="not-a-time")
        obj = self.trace_payload(
            [tx("dup", "A", "B"), bad_ts], "A", "B", max_hops=9
        )
        self.assert_code("DUPLICATE_TRANSFER_ID", obj)

    def test_schema_error(self):
        self.assert_code(
            "INVALID_INPUT_SCHEMA", {"start_address": "A"}
        )
        self.assert_code(
            "INVALID_INPUT_SCHEMA",
            self.trace_payload([{"id": "t1"}], "A", "B"),
        )

    def test_transfer_value_errors(self):
        self.assert_code(
            "INVALID_TRANSFER_VALUE",
            self.trace_payload(
                [tx("t", "A", "B", ts="2026-10-04 10:00:00")], "A", "B"
            ),
        )
        self.assert_code(
            "INVALID_TRANSFER_VALUE",
            self.trace_payload([tx("t", "A", "B", amount=0)], "A", "B"),
        )
        self.assert_code(
            "INVALID_TRANSFER_VALUE",
            self.trace_payload([tx("t", "A", "B", usd=-1)], "A", "B"),
        )

    def test_query_errors(self):
        base = [tx("t1", "A", "B")]
        self.assert_code(
            "INVALID_TRACE_QUERY", self.trace_payload(base, "A", "A")
        )
        self.assert_code(
            "INVALID_TRACE_QUERY", self.trace_payload(base, "", "B")
        )
        self.assert_code(
            "INVALID_TRACE_QUERY", self.trace_payload(base, "A", "B",
                                                      max_hops=0)
        )
        self.assert_code(
            "INVALID_TRACE_QUERY", self.trace_payload(base, "A", "B",
                                                      max_hops=9)
        )
        self.assert_code(
            "INVALID_TRACE_QUERY", self.trace_payload(base, "A", "B",
                                                      max_hops=True)
        )
        self.assert_code(
            "INVALID_TRACE_QUERY", self.trace_payload(base, "A", "B",
                                                      max_hops=1.0)
        )
        missing = self.trace_payload(base, "A", "B")
        del missing["chain"]
        self.assert_code("INVALID_TRACE_QUERY", missing)

    def test_query_check_after_value_check(self):
        obj = self.trace_payload(
            [tx("t", "A", "B", amount=float("nan"))], "A", "B", max_hops=9
        )
        self.assert_code("INVALID_TRANSFER_VALUE", obj)


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


class TraceCliTests(unittest.TestCase):
    def run_cli(self, raw):
        proc = subprocess.run(
            [BIN, "trace"], input=raw, capture_output=True, text=True
        )
        return proc

    def trace_input(self, transfers, start, end, max_hops=8):
        return {
            "transfers": transfers,
            "chain": "eth",
            "asset": "ETH",
            "start_address": start,
            "end_address": end,
            "max_hops": max_hops,
        }

    def test_success_stdout_only_trace_data(self):
        payload = self.trace_input(
            [
                tx("t1", "A", "B", amount=1.5, usd=1500),
                tx("t2", "B", "C", amount=2.5, usd=2500,
                   ts="2026-10-04T10:01:00Z"),
            ],
            "A", "C",
        )
        proc = self.run_cli(json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(set(out), {"data"})
        self.assertEqual(set(out["data"]), {"paths"})
        path = out["data"]["paths"][0]
        self.assertEqual(set(path),
                         {"nodes", "transfer_ids", "hops",
                          "amount", "usd_value"})
        self.assertEqual(path["nodes"], ["A", "B", "C"])
        self.assertEqual(path["transfer_ids"], ["t1", "t2"])
        self.assertEqual(path["hops"], 2)
        self.assertEqual(path["amount"], 4)
        self.assertEqual(path["usd_value"], 4000)
        self.assertEqual(proc.stderr, "")

    def test_empty_paths_success(self):
        proc = self.run_cli(json.dumps(self.trace_input(
            [tx("t1", "A", "B")], "A", "Z")))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"data": {"paths": []}})

    def test_not_json(self):
        proc = self.run_cli("{not json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr), {"error": "INPUT_NOT_JSON"})

    def test_trace_query_error(self):
        payload = self.trace_input([tx("t1", "A", "B")], "A", "A")
        proc = self.run_cli(json.dumps(payload))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            json.loads(proc.stderr), {"error": "INVALID_TRACE_QUERY"}
        )

    def test_trace_output_does_not_mix_analyze_keys(self):
        proc = self.run_cli(json.dumps(self.trace_input(
            [tx("t1", "A", "B")], "A", "B")))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertNotIn("graph", out["data"])
        self.assertNotIn("whales", out["data"])
        self.assertNotIn("scores", out["data"])
        self.assertNotIn("alerts", out["data"])


if __name__ == "__main__":
    unittest.main()
