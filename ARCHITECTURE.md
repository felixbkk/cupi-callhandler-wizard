# Architecture

## Overview

`callhandler_wizard.py` is a single-file Python tool that reads call handler routing data from a Cisco Unity Connection server via the CUPI REST API and generates self-contained HTML visualizations. All operations are read-only.

## Data Flow

```
CUC Server (CUPI REST API)
        |
        v
  +---------------------+
  |   Data Collection    |  HTTP GET requests with Basic Auth
  |   (fetch_* funcs)    |  Paginated, handles CUPI quirks
  +---------+-----------+
            |
            v
  +---------------------+
  |   Graph Building     |  build_graph()
  |   - Nodes + edges    |  Links handlers via menu entries,
  |   - Classification   |  transfer rules, greetings
  |   - BFS reachability |  Per-schedule reachability analysis
  |   - Schedule tagging |  Tags edges with schedule context
  +---------+-----------+
            |
     +------+------+
     v      v      v
  +------+ +------+ +------+ +------+ +------+ +------+
  |index | | map  | |report| | flow | |sched | | test |
  |.html | |.html | |.html | |.html | |.html | |times |
  +------+ +------+ +------+ +------+ +------+ +------+
```

## CUPI API Endpoints Used

All endpoints are read-only (GET).

| Endpoint | Purpose |
|----------|---------|
| `/vmrest/cluster` | Identify site/server name |
| `/vmrest/vmsservers` | Fallback site identification |
| `/vmrest/handlers/callhandlers` | List all call handlers |
| `/vmrest/handlers/callhandlers/{id}/menuentries` | DTMF key routing for a handler |
| `/vmrest/handlers/callhandlers/{id}/transferrules` | Transfer rules (Standard/Off Hours/Alternate) |
| `/vmrest/handlers/callhandlers/{id}/greetings` | Greetings and after-greeting routing |
| `/vmrest/handlers/callhandlers/{id}/greetings/{type}/greetingstreamfiles/{lang}/audio` | Greeting audio (WAV) |
| `/vmrest/handlers/directoryhandlers` | List directory handlers |
| `/vmrest/handlers/interviewhandlers` | List interview handlers |
| `/vmrest/routingrules` | Routing rules (entry points) |
| `/vmrest/routingrules/{id}/routingruleconditions` | Conditions on a routing rule |
| `/vmrest/schedules` | Business hour schedule definitions |
| `/vmrest/schedules/{id}/scheduledetails` | Time blocks for a schedule |
| `/vmrest/schedulesets` | Schedule set definitions |
| `/vmrest/holidayschedules` | Holiday schedule definitions (legacy) |
| `/vmrest/holidayschedules/{id}/holidays` | Individual holidays in a schedule |

The `probe` command tests 40+ additional endpoints to map server capabilities.

## TLS Connection

`connect()` probes with standard TLS first, then falls back to `_LegacySSLAdapter` (disables TLS 1.3, lowers minimum version) for older CUC servers. SSL verification is disabled throughout.

## Key Data Structures

### Nodes

Each node represents a call handler, interview handler, routing rule, or phone extension:

```python
{
    "id": "ObjectId",
    "name": "DisplayName",
    "extension": "DtmfAccessId",
    "type": "callhandler|interview|routingrule|phone",
    "classification": "root|normal|orphan|unreachable|deadend",
    "scheduleName": "Business Hours",
    "audio": [{"greeting": "Standard", "url": "...", "schedule": "standard"}],
    "reachable_standard": True,
    "reachable_offhours": True,
    "reachable_holiday": False
}
```

### Edges

Each edge represents a routing connection between nodes:

```python
{
    "source": "source_node_id",
    "target": "target_node_id",
    "label": "Key 1|Xfer:0|After:Standard",
    "schedule": "always|standard|offhours|holiday|alternate"
}
```

### Schedule Tags

Edges are tagged with when they are active:

| Tag | Meaning |
|-----|---------|
| `always` | Active regardless of schedule (menu entries, routing rules) |
| `standard` | Active during standard business hours |
| `offhours` | Active during off-hours |
| `holiday` | Active during holidays |
| `alternate` | Active when alternate greeting is manually enabled |

## Classification Logic

Nodes are classified after all edges are built, using BFS reachability from routing rule nodes:

1. **Root** -- Targeted by a routing rule (entry point to the call flow)
2. **Normal** -- Reachable from a root via BFS traversal
3. **True Orphan** -- No incoming or outgoing edges, not a routing target
4. **Unreachable Subtree** -- Has outgoing edges but nothing routes into it
5. **Dead End** -- Has incoming edges but no outgoing edges or transfer targets

Per-schedule reachability (standard, off hours, holiday) is computed separately and stored on each node for schedule-aware filtering and the `orphans` command.

## CLI Commands

| Command | Function | Purpose |
|---------|----------|---------|
| `generate` (default) | `cmd_generate` | Full data collection, graph building, HTML generation |
| `handler <search>` | `cmd_handler` | Look up a handler by name/extension/ID, show its rules and greetings |
| `schedules` | `cmd_schedules` | List all business hour and holiday schedules |
| `orphans` | `cmd_orphans` | Find orphans, unreachable, dead ends, and per-schedule gaps |
| `query <path>` | `cmd_query` | Hit any CUPI API path and dump raw JSON |
| `probe` | `cmd_probe` | Test 40+ CUPI endpoints to map server capabilities |
| `audio` | `cmd_audio_probe` | HEAD-check every handler's greeting audio URLs |

## HTML Output

All HTML files are written to `reports/<SiteName>_<timestamp>/`. Pages are self-contained with inline CSS/JS and embedded JSON data. They link to each other via a shared navigation bar.

| File | Generator | Dependencies |
|------|-----------|-------------|
| `index.html` | `generate_index_html` | None (links to other pages) |
| `callhandler_map.html` | `generate_html` | D3.js (local copy or CDN) |
| `callhandler_report.html` | `generate_table_html` | None (pure HTML/CSS/JS) |
| `callflow.html` | `generate_callflow_html` | None (pure HTML/CSS/JS, supports deep linking) |
| `schedules.html` | `generate_schedules_html` | None |
| `test_times.html` | `generate_test_times_html` | None |

### Shared Features Across Pages

- **CUC admin deep links** -- `{host}/cuadmin/{type}.do?op=read&objectId={id}` for handlers and greetings
- **Schedule badges** -- visual indicators of which schedule context applies to edges and audio
- **Navigation bar** -- cross-links between all report pages

## Data Collection Details

- **Voicemail filtering** -- Handlers with numeric-only display names are filtered out during collection (these are user voicemail boxes, not auto-attendant handlers)
- **Audio detection** -- Greetings with `PlayWhat` of 1 or 2 are included (both have uploaded audio on typical CUC servers)
- **Smart endpoint fallback** -- Sub-resource endpoints that return 404 on first try are skipped for remaining handlers
- **Pagination** -- All list endpoints are fetched with pagination handling
- **Console logging** -- All output is tee'd to `run.log` in the report directory
