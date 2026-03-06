# Architecture

## Overview

`callhandler_wizard.py` is a single-file Python tool that reads call handler routing data from a Cisco Unity Connection server via the CUPI REST API and generates self-contained HTML visualizations. All operations are read-only.

## Data Flow

```
CUC Server (CUPI REST API)
        │
        ▼
   ┌─────────────────────┐
   │   Data Collection    │  HTTP GET requests with Basic Auth
   │   (fetch_* funcs)    │  Paginated, handles CUPI quirks
   └─────────┬───────────┘
             │
             ▼
   ┌─────────────────────┐
   │   Graph Building     │  build_graph()
   │   - Nodes + edges    │  Links handlers via menu entries,
   │   - Classification   │  transfer rules, greetings
   │   - Schedule tagging │  Tags edges with schedule context
   └─────────┬───────────┘
             │
        ┌────┴────┐
        ▼         ▼
   ┌─────────┐ ┌──────────────┐
   │ D3 Graph│ │ Table Report │
   │  .html  │ │    .html     │
   └─────────┘ └──────────────┘
```

## CUPI API Endpoints Used

All endpoints are read-only (GET).

| Endpoint | Purpose |
|----------|---------|
| `/vmrest/handlers/callhandlers` | List all call handlers |
| `/vmrest/handlers/callhandlers/{id}/menuentries` | DTMF key routing for a handler |
| `/vmrest/handlers/callhandlers/{id}/transferrules` | Transfer rules (Standard/Off Hours/Alternate) |
| `/vmrest/handlers/callhandlers/{id}/greetings` | Greetings and after-greeting routing |
| `/vmrest/handlers/callhandlers/{id}/greetings/{type}/greetingstreamfiles/{lang}/audio` | Greeting audio (WAV) |
| `/vmrest/handlers/interviewhandlers` | List interview handlers |
| `/vmrest/routingrules` | Routing rules (entry points) |
| `/vmrest/schedules` | Business hour schedule definitions |
| `/vmrest/schedules/{id}/scheduledetails` | Time blocks for a schedule |
| `/vmrest/holidayschedules` | Holiday schedule definitions |
| `/vmrest/holidayschedules/{id}/holidays` | Individual holidays in a schedule |

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
    "audio": [{"greeting": "Standard", "url": "...", "schedule": "standard"}]
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

Nodes are classified after all edges are built:

1. **Root** — Targeted by a routing rule (entry point to the call flow)
2. **Normal** — Has both incoming and outgoing connections
3. **True Orphan** — No incoming or outgoing edges, not a routing target
4. **Unreachable Subtree** — Has outgoing edges but nothing routes into it
5. **Dead End** — Has incoming edges but no outgoing edges or transfer targets

## CLI Commands

| Command | Purpose |
|---------|---------|
| `generate` (default) | Full data collection → HTML generation |
| `handler <search>` | Look up a specific handler, show its transfer rules, greetings, menu entries |
| `schedules` | List all business hour and holiday schedules |
| `query <path>` | Hit any CUPI API path and dump raw JSON |

## HTML Output

Both HTML files are fully self-contained (inline CSS/JS, SVG favicon, embedded data as JSON). They can be opened directly in a browser with no web server.

- **`callhandler_map.html`** — D3.js force-directed graph loaded from CDN
- **`callhandler_report.html`** — Pure HTML/CSS/JS, no external dependencies. All data embedded as a JSON blob, rendered client-side with schedule-aware filtering.

## Future Extensibility

- **Audio download**: `greeting_audio_url()` helper already builds the correct CUPI path. A download function would use the existing `session` to fetch WAV bytes and save to disk or embed as base64.
- **Additional node types**: User objects, distribution lists could be added as new node types.
- **Export**: The debug panel's "Dump All Data" already exports full JSON. Could add CSV/GraphML export.
