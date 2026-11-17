# ix-bird

Per-customer lanes on a Linux IX router, routed **Static** or **BGP**, shaped one VLAN at a time.

You run caches (or any service) behind one router, and you sell them to other networks over that router: each
customer gets its own VLAN, point-to-point link, kernel routing table and HTB shaper. ix-bird turns one small
YAML file per customer into the shell commands for the lane and, for customers that peer with you, a BIRD 2
include that runs the eBGP session safely.

```
            cache (service)                          router (ix-bird)                       customer
 198.51.100.0/27 ──► 192.0.2.1 ──► ip rule from 198.51.100.0/27 table 201 ──► bond0.3001 ──► 10.30.1.2
                                       table 201 = routes to the customer only              (shaped: HTB)
                                       Static: written by the shell file
                                       BGP:    written by BIRD, from the customer's own announcement
```

## Why

- **Match by source, not destination.** Replies from the cache are steered by `ip rule from <cache> table <lane>`;
  each lane's table holds only that customer's routes, so lanes never see each other and main is never touched.
- **BGP without the risk.** A BGP lane is its own BIRD table + session + kernel protocol. The import filter rejects
  the default route first, then accepts only the customer's registered prefixes; the export filter sends only the
  service subnets; the kernel protocol writes only that lane's table (`learn off`). A typo in one customer file
  is rejected by `bird -p` before BIRD reloads, so the running sessions never see it.
- **No overlaps, by construction.** The generator refuses a prefix that another customer already has on the same
  service, a duplicate table, VLAN or link subnet, a reserved table, your own space and the caches.
- **Plain sessions.** BIRD defaults (hold/keepalive negotiate like any router), next-hop-self, no surprises.
  An import limit only when you set `max_prefix`.
- **Keeps working when BGP drops.** An optional fallback static (preference 50) keeps the route on the lane while
  the session is down; when the link itself is down, the route disappears.

## Quick start

```sh
pip install pyyaml
tools/ix-bird-gen --services examples/services.yaml --out out examples/customers/*.yaml
# out/common.conf, out/<customer>.sh, out/<customer>.bird (BGP customers only)

# on the router (BIRD 2 running, birdc in PATH):
IXBIRD_SRC=out tools/ix-bird-apply install examplebgpnetworks
tools/bgp                         # all sessions
tools/bgp examplebgp rec          # received: accepted + rejected by policy
tools/bgp examplebgp adv          # advertised
```

## Customer file

```yaml
name: Example Bgp Networks
prefixes: [203.0.113.0/24]          # what the customer owns (accepted from BGP, or static routes)
lanes:
  - service: CACHE                  # from services.yaml
    table: 201                      # kernel table of this lane (unique)
    vlan: 3001
    parentif: bond0
    local: 10.30.1.1/30             # our side
    peer: 10.30.1.2                 # customer side
    rate: 1000Mbit                  # HTB shaper
    routing: bgp                    # static (default) | bgp
    bgp:
      peer_as: 64510
      # local_as: 64500             # default: the service's local_as
      # max_prefix: 10              # optional import limit
      # fallback: none              # default: static
```

`services.yaml` lists each service (`local_as`, `nexthop`, `sources`, optional `announce`), the prefixes you
protect (`deny`) and tables that must never be used (`reserved_tables`). See [examples/](examples/).

## Status tool

`tools/bgp` gives a router-style view of the customer sessions:

```
Session                Neighbor        AS      LocAS  State        Since         Rcv  Acc  Adv
bgp_cache_examplebgpnetworks 10.30.1.2       64510   64500  Established  12:00:01.000    1    1    2
```

## Files on the router

| File | From |
|---|---|
| `/etc/<slug>.cfg` | `<slug>.sh` — VLAN, source rules, routes (static lanes), shaper; run it at boot |
| `/etc/bird/customers/<slug>.conf` | `<slug>.bird` — installed by `ix-bird-apply`, parse-checked first |
| `/etc/bird/ixbird-common.conf` | `common.conf` — the deny list (`IXBIRD_DENY`) |
| `bird.conf` tail | two `include` lines, added once by `ix-bird-apply` |

## Requirements

Linux with iproute2 and tc, BIRD 2.x (tested 2.15 and 2.17), Python 3.8+ with PyYAML for the generator.
The router side is POSIX sh (works on busybox ash).

## Tests

```sh
python3 -m unittest -v          # golden output + every overlap rule
tests/bird-parse.sh             # parses the generated config with a real BIRD
```

Design notes: [docs/DESIGN.md](docs/DESIGN.md). Licence: MIT.
