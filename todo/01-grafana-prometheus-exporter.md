# Task 1 — Grafana + Prometheus Exporter

> **DO NOT START.** This task is deferred. The homelab doesn't have a usable Grafana/Prometheus stack yet. Do not implement any part of this file until the user explicitly unblocks it.
>
> If you're a fresh session pointed at this file: stop, read `todo/README.md`, and tell the user the task is on hold.

**Prereq when eventually started**: read `todo/README.md` first. Also read `/Users/kai/projects/coilysiren/kai-server` for cluster conventions — Claude can't run write-kubectl directly, GH Actions → cluster is the path.

## Goal

Build a Prometheus exporter for the Eco server and a Grafana dashboard that renders snapshots via an MCP tool.

## Endpoints to scrape (every 30 s — do not go faster)

Public:
- `GET /info`
- `GET /datasets/flatlist` (one-time at startup to discover stat names) + `GET /datasets/get?dataset=X&dayStart=0&dayEnd=<now>` for ~15 chosen stats.
  - All of these are in `/datasets/flatlist` (verified): `PayWages`, `RepaidLoanOrBond`, `DefaultedOnLoanOrBond`, `PostedContract`, `CompletedContract`, `PropertyTransfer`, `ReputationTransfer`, `TransferMoney`, `PayTax`, `SettlementFounded`, `BecomeCitizen`, `ItemCraftedAction`, `ChopTree`, `HarvestOrHunt`. **Do NOT** try `TotalCulture` as a dataset — it's only a `/info` field and `/datasets/get?dataset=TotalCulture` returns 500.
  - Also expose derived gauges: `online_players`, `days_running` (from `/info`).

Admin (`X-API-Key` from SSM `/eco-mcp-app/api-admin-token`, **region `us-east-1`**):
- `GET /api/v1/users?hoursPlayedGte=0`

## Deliverables

1. New top-level directory `exporter/` with a Python `/metrics` endpoint using `prometheus_client`:
   - Gauges for snapshot values (players online, days running, active loans, etc.).
   - Counters (with `_total` suffix) for cumulative event series (wages paid, contracts completed).
2. `exporter/Dockerfile` + k3s manifest at `deploy/exporter.yml`, matching the `backend` repo deploy pattern. See `/Users/kai/projects/coilysiren/backend` for the canonical rig (Dockerfile shape, Makefile targets, GH Actions publish, Traefik ingress).
3. Grafana dashboard JSON at `exporter/dashboards/eco.json` with panels:
   - Players Online (gauge + timeseries)
   - Loan Defaults (rate panel)
   - Wage Velocity (rate panel)
   - Contracts Completed (counter rate)
   - Top Craft Events (table, top-N by `ItemCraftedAction` delta)
4. New MCP tool `get_grafana_snapshot(panel_id)` in `src/eco_mcp_app/server.py` that calls Grafana's `/render` API and **inlines the resulting PNG as a data URI** in an iframe card (CSP, per `claude-ai-mcp#40`).

## Constraints

- **Scrape cadence: 30 s floor.** The datasets endpoints serve heavy CSVs; going faster risks backpressure on the game server.
- Grafana already sits behind the homelab's Traefik. **Do not build custom auth.**
- Claude Code in this repo cannot run write-kubectl. Deploys land via GH Actions; mirror the pattern in `deploy/main.yml`.

## Acceptance

- Exporter returns non-zero gauges for `players_online` and at least one counter series.
- Dashboard JSON imports cleanly into Grafana.
- MCP tool smoke-tested via `inv smoke`.
- k3s manifest deploys via the existing GH Actions → cluster path (do not attempt from local).
