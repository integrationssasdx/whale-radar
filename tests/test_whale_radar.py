"""Whale Radar 测试：纯标准库 unittest，运行时不联网、不落盘。"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whale_radar.analyzer import AnalyzeError, analyze
from whale_radar.ranker import rank
from whale_radar.tracer import trace

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


if __name__ == "__main__":
    unittest.main()
