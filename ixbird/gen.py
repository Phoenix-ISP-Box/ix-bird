#!/usr/bin/env python3
"""ix-bird generator: per-customer lanes on a Linux IX router, Static or BGP (BIRD 2).

One YAML per customer + one services.yaml  ->  out/<slug>.sh   (VLAN, source rules, routes, HTB shaper)
                                               out/<slug>.bird (BIRD include, only when a lane is BGP)

A lane = one customer x one service: its own VLAN, point-to-point subnet, kernel routing table and shaper.
Traffic from the service (cache) is matched by SOURCE (`ip rule from <service source> table <lane table>`),
so each lane's table only needs routes to that customer. Static lanes get those routes from the shell file;
BGP lanes get them from BIRD, which writes only that table.

Usage: ix-bird-gen --services services.yaml --out out/ customers/*.yaml
"""
import argparse
import ipaddress
import os
import re
import sys

import yaml

RESERVED_TABLES = {0, 253, 254, 255}
SLUG_RE = re.compile(r"[^a-z0-9]")


class ConfigError(Exception):
    pass


def slug(text):
    return SLUG_RE.sub("", str(text).lower())


def net(value, what):
    try:
        return ipaddress.ip_network(str(value), strict=True)
    except ValueError as exc:
        raise ConfigError(f"{what}: {value} is not a valid network ({exc})") from None


# ---------------------------------------------------------------- loading + validation

def load_services(path):
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    services = {}
    for name, s in (doc.get("services") or {}).items():
        name = str(name).upper()
        if not s.get("sources"):
            raise ConfigError(f"service {name}: sources missing")
        services[name] = {
            "local_as": int(s["local_as"]) if s.get("local_as") else None,
            "nexthop": str(s["nexthop"]) if s.get("nexthop") else None,
            "sources": [net(x, f"service {name} source") for x in s["sources"]],
            "announce": [net(x, f"service {name} announce") for x in (s.get("announce") or s["sources"])],
        }
    deny = [net(x, "deny") for x in (doc.get("deny") or [])]
    reserved = {int(t) for t in (doc.get("reserved_tables") or [])} | RESERVED_TABLES
    return services, deny, reserved


def load_customer(path, services):
    with open(path) as f:
        c = yaml.safe_load(f) or {}
    name = c.get("name") or os.path.splitext(os.path.basename(path))[0]
    s = slug(c.get("slug") or name)
    if not s:
        raise ConfigError(f"{path}: empty slug")
    prefixes = [net(p, f"{name} prefix") for p in (c.get("prefixes") or [])]
    if not prefixes:
        raise ConfigError(f"{name}: prefixes missing")
    lanes = []
    for i, l in enumerate(c.get("lanes") or []):
        where = f"{name} lane {i + 1}"
        svc = str(l.get("service", "")).upper()
        if svc not in services:
            raise ConfigError(f"{where}: unknown service {svc!r}")
        local = ipaddress.ip_interface(str(l["local"]))
        peer = ipaddress.ip_address(str(l["peer"]))
        if peer not in local.network or peer == local.ip:
            raise ConfigError(f"{where}: peer {peer} must be another address inside {local.network}")
        routing = str(l.get("routing", "static")).lower()
        if routing not in ("static", "bgp"):
            raise ConfigError(f"{where}: routing must be static or bgp")
        bgp = dict(l.get("bgp") or {})
        if routing == "bgp":
            if not str(bgp.get("peer_as", "")).isdigit():
                raise ConfigError(f"{where}: bgp.peer_as missing")
            if not (bgp.get("local_as") or services[svc]["local_as"]):
                raise ConfigError(f"{where}: no local AS (set bgp.local_as or the service local_as)")
        lanes.append({
            "service": svc,
            "table": int(l["table"]),
            "vlan": int(l["vlan"]),
            "parentif": str(l.get("parentif", "bond0")),
            "local": local,
            "peer": peer,
            "rate": str(l["rate"]),
            "routing": routing,
            "bgp": bgp,
        })
    if not lanes:
        raise ConfigError(f"{name}: no lanes")
    return {"name": name, "slug": s, "prefixes": prefixes, "lanes": lanes}


def validate(customers, services, deny, reserved):
    """Cross-customer checks: nothing may overlap. Raises ConfigError with every problem found."""
    errors = []
    seen_slug, seen_table, seen_vlan, seen_p2p = {}, {}, {}, {}
    owners = {}  # service -> [(prefix, customer)]
    for c in customers:
        if c["slug"] in seen_slug:
            errors.append(f"slug {c['slug']} used by {seen_slug[c['slug']]} and {c['name']}")
        seen_slug[c["slug"]] = c["name"]
        for p in c["prefixes"]:
            for d in deny:
                if p.overlaps(d):
                    errors.append(f"{c['name']}: prefix {p} overlaps denied {d}")
            if p.prefixlen == 0:
                errors.append(f"{c['name']}: default route is never a customer prefix")
        for l in c["lanes"]:
            t, v = l["table"], (l["parentif"], l["vlan"])
            if t in reserved or not 1 <= t <= 2 ** 31:
                errors.append(f"{c['name']}: table {t} is reserved")
            if t in seen_table:
                errors.append(f"table {t} used by {seen_table[t]} and {c['name']}")
            seen_table[t] = c["name"]
            if v in seen_vlan:
                errors.append(f"vlan {v[0]}.{v[1]} used by {seen_vlan[v]} and {c['name']}")
            seen_vlan[v] = c["name"]
            for other, who in seen_p2p.items():
                if l["local"].network.overlaps(other):
                    errors.append(f"{c['name']}: link {l['local'].network} overlaps {who}'s {other}")
            seen_p2p[l["local"].network] = c["name"]
            svc = services[l["service"]]
            for p in c["prefixes"]:
                for cache in svc["sources"] + svc["announce"]:
                    if p.overlaps(cache):
                        errors.append(f"{c['name']}: prefix {p} overlaps {l['service']} cache {cache}")
                if l["routing"] == "bgp" and p.prefixlen > 24:
                    errors.append(f"{c['name']}: BGP prefix {p} longer than /24")
                for q, who in owners.get(l["service"], []):
                    if who != c["name"] and p.overlaps(q):
                        errors.append(f"{c['name']}: prefix {p} overlaps {who}'s {q} on {l['service']}")
            owners.setdefault(l["service"], []).extend((p, c["name"]) for p in c["prefixes"])
    if errors:
        raise ConfigError("\n".join(sorted(set(errors))))


# ---------------------------------------------------------------- output

def shell(c, services):
    out = [f"#!/bin/sh", f"# {c['name']}: generated by ix-bird; edit the customer yaml, not this file"]
    for l in c["lanes"]:
        dev = f"{l['parentif']}.{l['vlan']}"
        svc = services[l["service"]]
        r = l["rate"]
        out.append(f"\n# lane {l['service']} - {dev} - table {l['table']} - routing {l['routing']}")
        out += [f"ip rule add from {s} table {l['table']}" for s in svc["sources"]]
        out += [f"ip link add link {l['parentif']} name {dev} type vlan id {l['vlan']}",
                f"ip addr add dev {dev} {l['local']}",
                f"ip link set dev {dev} up"]
        if l["routing"] == "static":
            out += [f"ip route add table {l['table']} {p} via {l['peer']}" for p in c["prefixes"]]
        else:
            out.append(f"# routes in table {l['table']} come from BIRD (bgp_{slug(l['service'])}_{c['slug']})")
        out += [f"tc qdisc del dev {dev} root 2>/dev/null",
                f"tc qdisc add dev {dev} root handle 1: htb default 10",
                f"tc class add dev {dev} parent 1: classid 1:1 htb rate {r} ceil {r} burst 10000 cburst 10000 quantum 4000",
                f"tc class add dev {dev} parent 1:1 classid 1:10 htb rate {r} ceil {r} burst 10000 cburst 10000 quantum 4000",
                f"tc class add dev {dev} parent 1:1 classid 1:11 htb rate 10Mbit ceil {r} burst 10000 cburst 10000 quantum 4000",
                f"tc filter add dev {dev} protocol ip parent 1:0 prio 1 u32 match ip protocol 1 0xff flowid 1:11",
                f"tc filter add dev {dev} protocol ip parent 1:0 prio 1 u32 match u32 0 0 flowid 1:10"]
    return "\n".join(out) + "\n"


def bird(c, services):
    blocks = []
    for l in c["lanes"]:
        if l["routing"] != "bgp":
            continue
        svc = services[l["service"]]
        b = l["bgp"]
        n = f"{slug(l['service'])}_{c['slug']}"
        P = f"{c['slug'].upper()}_{slug(l['service']).upper()}_PREFIXES"
        peer_as = int(b["peer_as"])
        local_as = int(b.get("local_as") or svc["local_as"])
        dev = f"{l['parentif']}.{l['vlan']}"
        prefixes = ", ".join(str(p) for p in c["prefixes"])
        announce = ", ".join(str(a) for a in svc["announce"])
        how = f"via {svc['nexthop']}" if svc["nexthop"] else "unreachable"
        ann = "\n".join(f"    route {a} {how};" for a in svc["announce"])
        limit = f"\n        import limit {int(b['max_prefix'])} action block;" if b.get("max_prefix") else ""
        blocks.append(f"""# {c['name']} - {l['service']} - {dev} - AS{peer_as} - kernel table {l['table']}
# generated by ix-bird; edit the customer yaml, not this file
define {P} = [ {prefixes} ];

ipv4 table t_{n};

# what we announce: the service subnets (withdrawn by BIRD if the next hop goes away)
protocol static ann_{n} {{
    ipv4 {{ table t_{n}; }};
{ann}
}}
""")
        if str(b.get("fallback", "static")).lower() == "static":
            fb = "\n".join(f"    route {p} via {l['peer']};" for p in c["prefixes"])
            blocks.append(f"""# fallback: used only while the BGP route is missing (preference 50 < 100)
protocol static fb_{n} {{
    ipv4 {{ table t_{n}; preference 50; }};
{fb}
}}
""")
        blocks.append(f"""filter in_{n} {{
    if net = 0.0.0.0/0 then reject;          # first term: never a default
    if net.len > 24 then reject;
    if net ~ IXBIRD_DENY then reject;
    if bgp_path.first != {peer_as} then reject;
    if net ~ {P} then accept;
    reject;
}}

filter out_{n} {{
    if net = 0.0.0.0/0 then reject;
    if source = RTS_STATIC && net ~ [ {announce} ] then accept;
    reject;
}}

protocol bgp bgp_{n} {{
    description "{c['name']} AS{peer_as} - {l['service']} - {dev}";
    local {l['local'].ip} as {local_as};
    neighbor {l['peer']} as {peer_as};
    ipv4 {{
        table t_{n};
        import filter in_{n};
        export filter out_{n};{limit}
        import keep filtered on;
        next hop self;
    }};
}}

# writes ONLY kernel table {l['table']}; never main, never routes it did not create
protocol kernel krt_{n} {{
    kernel table {l['table']};
    learn off;
    persist on;
    metric 32;
    ipv4 {{
        table t_{n};
        import none;
        export where net ~ {P};
    }};
}}
""")
    return "\n".join(blocks)


def common(deny):
    nets = ",\n  ".join(f"{d}+" for d in deny) if deny else "0.0.0.0/32"
    return f"""# ix-bird common definitions; generated, edit services.yaml (deny:) instead.
define IXBIRD_DENY = [
  0.0.0.0/8+, 10.0.0.0/8+, 100.64.0.0/10+, 127.0.0.0/8+, 169.254.0.0/16+, 172.16.0.0/12+,
  192.0.0.0/24+, 192.168.0.0/16+, 198.18.0.0/15+, 224.0.0.0/3+,
  {nets}
];
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--services", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    ap.add_argument("customers", nargs="+")
    a = ap.parse_args(argv)
    try:
        services, deny, reserved = load_services(a.services)
        customers = [load_customer(p, services) for p in a.customers]
        validate(customers, services, deny, reserved)
    except (ConfigError, KeyError, ValueError) as exc:
        print(f"ix-bird: {exc}", file=sys.stderr)
        return 1
    if a.check:
        print(f"ix-bird: {len(customers)} customers OK")
        return 0
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "common.conf"), "w") as f:
        f.write(common(deny))
    for c in customers:
        with open(os.path.join(a.out, f"{c['slug']}.sh"), "w") as f:
            f.write(shell(c, services))
        bpath = os.path.join(a.out, f"{c['slug']}.bird")
        text = bird(c, services)
        if text:
            with open(bpath, "w") as f:
                f.write(text)
        elif os.path.exists(bpath):
            os.remove(bpath)
        print(f"ix-bird: {c['slug']}.sh{' + ' + c['slug'] + '.bird' if text else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
