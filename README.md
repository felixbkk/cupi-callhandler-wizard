# cupi-callhandler-wizard

Connects to a Cisco Unity Connection 12.x server via the CUPI REST API and generates interactive visualizations of the call handler routing tree. All operations are **read-only** -- the tool never modifies any data on the CUC server.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

### Generate Reports (default)

```bash
python callhandler_wizard.py --host https://10.212.111.17 --user admin generate
```

You will be securely prompted for the password. The `generate` subcommand is the default and can be omitted:

```bash
python callhandler_wizard.py --host https://10.212.111.17 --user admin
```

Output is written to `reports/<SiteName>_<timestamp>/` containing:

| File | Description |
|------|-------------|
| `index.html` | Landing page with links to all reports |
| `callhandler_map.html` | Interactive D3.js force-directed graph |
| `callhandler_report.html` | Table report with call flow trees, debug tools, audio links |
| `callflow.html` | Dedicated call flow view with deep linking |
| `schedules.html` | Business hour and holiday schedule details |
| `test_times.html` | Recommended test times for each day of the week |
| `d3.v7.min.js` | Local D3 copy for offline use |
| `run.log` | Console output log for the generation run |

### CLI Debug Commands

**Look up a specific handler** by name, extension, or Object ID:

```bash
python callhandler_wizard.py --host https://... --user admin handler "Opening Greeting"
python callhandler_wizard.py --host https://... --user admin handler 2000
python callhandler_wizard.py --host https://... --user admin handler abc123 --raw
```

**List all schedules** (business hours and holidays):

```bash
python callhandler_wizard.py --host https://... --user admin schedules
```

**Find orphaned and unreachable handlers** with per-schedule reachability analysis:

```bash
python callhandler_wizard.py --host https://... --user admin orphans
```

**Probe CUPI endpoints** to see what's available on a given server:

```bash
python callhandler_wizard.py --host https://... --user admin probe
```

**Probe all handlers for uploaded greeting audio** (HEAD-checks every greeting URL):

```bash
python callhandler_wizard.py --host https://... --user admin audio
```

**Query any raw CUPI API path** and dump the JSON response:

```bash
python callhandler_wizard.py --host https://... --user admin query /vmrest/handlers/callhandlers
```

## Report Features

### Graph View (`callhandler_map.html`)

- **Layouts**: Force-directed, hierarchical, and radial
- **Drag** nodes to rearrange; **pin** nodes in place, **unpin all** to reset
- **Click** a node to view details in the sidebar (transfer rules, greetings, menu entries)
- **Zoom** and pan the graph
- **Toggle** visibility of orphans, unreachable subtrees, and dead ends
- **Color-coded edges** by schedule (standard, off hours, holiday, alternate)
- **CUC admin deep links** to open handlers directly in the CUC admin interface

### Table Report (`callhandler_report.html`)

- **Schedule mode selector** -- switch between Standard, Off Hours, Holiday, and All views to see how call routing changes by time of day
- **Search and filter** by name, extension, type, or classification
- **Call flow trees** -- expandable BFS trees showing the full path from each root handler
- **Audio links** for handlers with uploaded greeting recordings, with schedule badges
- **CUC admin deep links** for each handler and greeting
- **Schedules table** showing business hour time blocks per schedule
- **Holiday schedules table** with start/end dates
- **Debug panel** (bottom-right button) with node lookup, problem finder, and full JSON data dump

### Call Flow View (`callflow.html`)

- **Card-based call flow trees** from each routing rule entry point
- **Deep linking** -- link directly to a specific handler in the call flow
- **Audio playback links** with schedule badges
- **CUC admin deep links** for handlers and greetings

### Schedules View (`schedules.html`)

- Business hour schedules with day-by-day time blocks
- Holiday schedule listing with dates

### Test Times (`test_times.html`)

- Analyzes schedule data to compute recommended test times for each day of the week
- Identifies standard hours, off-hours, and transition points
- Includes a note about creating a temporary holiday for testing holiday routing

## Node Classifications

| Color  | Classification       | Description |
|--------|----------------------|-------------|
| Green  | **Root**             | Targeted by a routing rule -- valid entry point |
| Blue   | **Normal**           | Standard reachable call handler |
| Grey   | **True Orphan**      | No connections at all -- completely isolated |
| Orange | **Unreachable Subtree** | Has outgoing edges but nothing routes to it |
| Red    | **Dead End**         | Has incoming edges but callers have nowhere to go |
| Purple | **Interview Handler** | Interview handler node |
| Teal   | **Phone Extension**  | Transfer target phone extension |

## TLS Compatibility

The tool probes the server with standard TLS first. If the handshake fails (common on CUC 10.x and other legacy servers with self-signed certs), it automatically falls back to a legacy SSL adapter with relaxed settings. SSL certificate verification is disabled to support self-signed certificates.

## Security Notes

- All API calls are **read-only** (HTTP GET only). The tool never creates, updates, or deletes any data on the CUC server.
- Password is entered via secure prompt (hidden input) -- never stored to disk or passed as a CLI argument.
- Voicemail handlers (numeric-only display names) are automatically filtered out during data collection.
