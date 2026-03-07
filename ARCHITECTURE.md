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
  |  Extension Resolution|  fetch_users + fetch_contacts
  |  (best-effort name   |  Builds extension -> name map
  |   lookup for phones) |
  +---------+-----------+
            |
            v
  +---------------------+
  |   Graph Building     |  build_graph()
  |   - Nodes + edges    |  Links handlers via menu entries,
  |   - Classification   |  transfer rules, greetings
  |   - BFS reachability |  Per-schedule reachability analysis
  |   - Schedule tagging |  Tags edges with schedule context
  |   - Warning detection|  Flags misconfigurations
  +---------+-----------+
            |
            v
  +---------------------+
  |   Audio Download     |  download_audio_files()
  |   (greeting WAVs)    |  Saved to audio/ for inline playback
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
| `/vmrest/users` | User extensions and display names (for extension resolution) |
| `/vmrest/contacts` | Contact extensions and display names (for extension resolution) |

The `probe` command tests 40+ additional endpoints to map server capabilities.

## TLS Connection

`connect()` probes with standard TLS first, then falls back to `_LegacySSLAdapter` (disables TLS 1.3, lowers minimum version) for older CUC servers. SSL verification is disabled throughout.

## Key Data Structures

### Nodes

Each node represents a call handler, interview handler, routing rule, phone extension, directory handler, or action node:

```python
{
    "id": "ObjectId",
    "name": "DisplayName",
    "extension": "DtmfAccessId",
    "type": "callhandler|interview|routingrule|phone|directory|action",
    "classification": "root|normal|orphan|unreachable|deadend",
    "scheduleName": "Business Hours",
    "audio": [{"greeting": "Standard", "url": "audio/...", "schedule": "standard", "enabled": True}],
    "warnings": ["Alternate greeting is enabled (overrides Standard)"],
    "unlockedKeys": ["1", "2", "3"],
    "digitTimeoutMs": "1500",
    "postGreeting": False,
    "system": False,
    "reachable_standard": True,
    "reachable_offhours": True,
    "reachable_holiday": False
}
```

Not all fields are present on all node types. `audio`, `warnings`, `unlockedKeys`, `digitTimeoutMs`, `postGreeting`, and `system` are only on call handler nodes.

### Edges

Each edge represents a routing connection between nodes:

```python
{
    "source": "source_node_id",
    "target": "target_node_id",
    "label": "Key 1|Xfer:Standard (ring 4x)|After:Standard",
    "schedule": "always|standard|offhours|holiday|alternate"
}
```

Transfer edge labels include type and rings: `Xfer:Standard (ring 4x)` for supervised, `Xfer:Standard (release)` for release transfer.

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

## Warning Detection

`build_graph()` detects common auto-attendant misconfigurations and stores them in each node's `warnings` list:

| Warning | Trigger |
|---------|---------|
| Alternate transfer enabled | `TransferEnabled` true on Alternate rule |
| Alternate greeting enabled | `Enabled` true on Alternate greeting |
| Supervised transfer | `TransferType` != 0 on enabled transfer rule |
| No timeout key (*) | No active action on * key when other keys are active |
| After-greeting = Hangup | `AfterGreetingAction` = 1 |
| After-greeting = Take Message | `AfterGreetingAction` = 4 |
| After-message = Hangup | `AfterMessageAction` = 1 on call handler |
| Menu key = Take Message | Menu entry `Action` = 4 |
| Self-loop | Menu key routes to same handler |
| Circular routing | A->B->A with no exit from one side |
| Record your message | `PlayPostGreetingRecording` enabled |
| Caller input disabled | `IgnoreDigits` true on enabled greeting with active menu keys |

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

All HTML files are written to `reports/<SiteName>_<timestamp>/`. Pages are self-contained with inline CSS/JS and embedded JSON data. They link to each other via a shared floating navigation pill.

| File | Generator | Dependencies |
|------|-----------|-------------|
| `index.html` | `generate_index_html` | None (links to other pages) |
| `callhandler_map.html` | `generate_html` | D3.js (local copy or CDN) |
| `callhandler_report.html` | `generate_table_html` | None (pure HTML/CSS/JS) |
| `callflow.html` | `generate_callflow_html` | None (pure HTML/CSS/JS, supports deep linking) |
| `schedules.html` | `generate_schedules_html` | None |
| `test_times.html` | `generate_test_times_html` | None |

### Shared Features Across Pages

- **Floating navigation pill** -- cross-links between all report pages
- **Dark/light mode toggle** -- persisted via localStorage (`chw-theme` key)
- **CUC admin deep links** -- `{host}/cuadmin/{type}.do?op=read&objectId={id}` for handlers and greetings
- **Schedule badges** -- visual indicators of which schedule context applies to edges and audio
- **Greeting enabled/disabled state** -- red "(disabled)" indicator on greeting audio entries

## Data Collection Details

- **Voicemail filtering** -- Handlers with numeric-only display names are filtered out during collection (these are user voicemail boxes, not auto-attendant handlers)
- **Extension resolution** -- Users and contacts are fetched to build an extension-to-name map for resolving phone transfer targets
- **Audio download** -- Greeting WAV files are downloaded during generation to `audio/` for inline `<audio>` playback
- **Audio detection** -- Greetings with `PlayWhat` of 1 or 2 are included (both have uploaded audio on typical CUC servers)
- **Smart endpoint fallback** -- Sub-resource endpoints that return 404 on first try are skipped for remaining handlers
- **Pagination** -- All list endpoints are fetched with pagination handling
- **Console logging** -- All output is tee'd to `run.log` in the report directory
