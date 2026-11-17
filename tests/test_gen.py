"""ix-bird generator tests: golden output + every overlap/validation rule. Run: python3 -m unittest -v"""
import copy
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import yaml

from ixbird import gen

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVICES = os.path.join(ROOT, "examples", "services.yaml")
CUSTOMERS = sorted(os.path.join(ROOT, "examples", "customers", f)
                   for f in os.listdir(os.path.join(ROOT, "examples", "customers")))


def run(services, customers, out):
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = gen.main(["--services", services, "--out", out] + customers)
    return rc, err.getvalue()


class Golden(unittest.TestCase):
    def test_examples_match_golden(self):
        with tempfile.TemporaryDirectory() as out:
            rc, err = run(SERVICES, CUSTOMERS, out)
            self.assertEqual(rc, 0, err)
            golden = os.path.join(HERE, "golden")
            self.assertEqual(sorted(os.listdir(out)), sorted(os.listdir(golden)))
            for name in os.listdir(golden):
                with open(os.path.join(out, name)) as a, open(os.path.join(golden, name)) as b:
                    self.assertEqual(a.read(), b.read(), name)

    def test_static_lane_has_routes_and_no_bird(self):
        with tempfile.TemporaryDirectory() as out:
            run(SERVICES, CUSTOMERS, out)
            sh = open(os.path.join(out, "examplestaticisp.sh")).read()
            self.assertIn("ip route add table 202 192.0.2.128/29 via 10.30.2.2", sh)
            self.assertFalse(os.path.exists(os.path.join(out, "examplestaticisp.bird")))

    def test_bgp_lane_has_no_static_routes_and_safe_filters(self):
        with tempfile.TemporaryDirectory() as out:
            run(SERVICES, CUSTOMERS, out)
            sh = open(os.path.join(out, "examplebgpnetworks.sh")).read()
            self.assertNotIn("ip route add", sh)
            bird = open(os.path.join(out, "examplebgpnetworks.bird")).read()
            first = bird.index("filter in_cache_examplebgpnetworks")
            self.assertIn("if net = 0.0.0.0/0 then reject;", bird[first:first + 120])
            self.assertIn("kernel table 201;", bird)
            self.assertIn("learn off;", bird)
            self.assertIn("local 10.30.1.1 as 64500;", bird)


class Validation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.base = [yaml.safe_load(open(p)) for p in CUSTOMERS]

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, docs, expect):
        paths = []
        for i, d in enumerate(docs):
            p = os.path.join(self.dir, f"c{i}.yaml")
            yaml.safe_dump(d, open(p, "w"))
            paths.append(p)
        rc, err = run(SERVICES, paths, os.path.join(self.dir, "out"))
        self.assertEqual(rc, 1)
        self.assertIn(expect, err)

    def variant(self, idx, fn):
        docs = copy.deepcopy(self.base)
        fn(docs[idx])
        return docs

    def test_same_prefix_two_customers_same_service(self):
        docs = self.variant(1, lambda d: d.update(prefixes=["203.0.113.0/26"]))
        self.check(docs, "overlaps")

    def test_duplicate_table(self):
        docs = self.variant(1, lambda d: d["lanes"][0].update(table=201))
        self.check(docs, "table 201 used by")

    def test_duplicate_vlan(self):
        docs = self.variant(1, lambda d: d["lanes"][0].update(vlan=3001))
        self.check(docs, "vlan bond0.3001 used by")

    def test_overlapping_link(self):
        docs = self.variant(1, lambda d: d["lanes"][0].update(local="10.30.1.5/29", peer="10.30.1.6"))
        self.check(docs, "overlaps")

    def test_denied_prefix(self):
        docs = self.variant(1, lambda d: d.update(prefixes=["198.51.100.200/29"]))
        self.check(docs, "overlaps denied")

    def test_reserved_table(self):
        docs = self.variant(1, lambda d: d["lanes"][0].update(table=999))
        self.check(docs, "reserved")

    def test_peer_outside_link(self):
        docs = self.variant(1, lambda d: d["lanes"][0].update(peer="10.99.0.2"))
        self.check(docs, "must be another address inside")

    def test_bgp_needs_peer_as(self):
        docs = self.variant(0, lambda d: d["lanes"][0].update(bgp={}))
        self.check(docs, "bgp.peer_as missing")

    def test_bgp_prefix_longer_than_24(self):
        docs = self.variant(0, lambda d: d.update(prefixes=["203.0.113.0/25"]))
        self.check(docs, "longer than /24")

    def test_unknown_service(self):
        docs = self.variant(1, lambda d: d["lanes"][0].update(service="NOPE"))
        self.check(docs, "unknown service")


if __name__ == "__main__":
    unittest.main()
