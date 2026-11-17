# ix-bird design

## Model

A **lane** = one customer × one service. It owns:

| Object | Name |
|---|---|
| VLAN + /30 (or any subnet) | `<parentif>.<vlan>` |
| kernel routing table | `table` (unique; never 0, 253, 254, 255 or a reserved table) |
| source rules | `ip rule from <service source> table <table>` |
| HTB shaper | root `1:`, class `1:10` = rate, `1:11` = ICMP with a 10 Mbit floor |
| BIRD (BGP lanes) | table `t_<service>_<customer>`, protocols `bgp_`, `ann_`, `fb_`, `krt_` + `<service>_<customer>`, filters `in_` / `out_` |

Routes only ever flow **up** inside a lane: the customer's announcement enters the lane's BIRD table, and the
kernel protocol copies the customer prefixes into the lane's kernel table. Nothing crosses between lanes, nothing
is written to main, and nothing the customer sends is announced to anyone else.

## Traffic

- Customer → service: arrives on the lane VLAN, follows main (`<cache> via <nexthop>`), which the router already has.
- Service → customer: the upstream (cache router or VRF) sends the customer prefix to this router (a static there),
  `ip rule from <cache source>` selects the lane table, the route goes out the lane VLAN, the shaper applies.

## BGP lane

- **Import:** reject `0.0.0.0/0` first; reject longer than /24; reject `IXBIRD_DENY` (bogons, RFC 1918, CGNAT,
  multicast, your own space and the caches from `services.yaml`); the first AS must be the peer; accept only the
  customer's prefixes; reject the rest.
- **Export:** only static routes for the service subnets (`ann_`), which point at the service next hop, so BIRD
  withdraws them by itself when that next hop disappears.
- **Kernel:** `kernel table <table>`, `learn off` (never reads or removes routes it did not create), `persist on`,
  `metric 32`, export only the customer prefixes.
- **Fallback:** `fb_` statics with preference 50 (BGP is 100) keep the lane routed while the session is down.
- **Session:** plain — BIRD defaults for timers and capabilities, `next hop self`. `max_prefix` adds an import limit.

## Safety on apply

`ix-bird-apply install` writes the file, runs `bird -p` on the whole configuration and only then
`birdc configure`. A rejected file is kept as `.bad`, the previous version is restored, and the running BIRD
keeps its old configuration; unchanged protocols are never restarted by a reconfigure, so other customers do
not flap. `remove` takes the lane out of BIRD first (otherwise BIRD would re-add its routes), flushes the
`proto bird` routes BIRD leaves behind (`persist on`), then removes rules and VLANs.

## Validation (generator)

Across all customer files: unique slug, table, VLAN and link subnet; peer inside the link subnet and not our
address; no customer prefix overlapping the deny list or any cache; on the same service, no two customers with
overlapping prefixes; BGP prefixes /24 or shorter and a peer AS present.
