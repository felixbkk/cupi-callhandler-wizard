# cupi-callhandler-wizard

Connects to a Cisco Unity Connection 12.x server via the CUPI REST API and generates an interactive D3.js force-directed graph visualization of the call handler routing tree. The output is a self-contained HTML file.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Create a `.env` file in the project root:

```
CUC_HOST=https://10.212.111.17
CUC_USER=admin
CUC_PASS=yourpassword
```

## Usage

```bash
python callhandler_wizard.py
```

The script will fetch all call handlers, routing rules, interview handlers, menu entries, transfer rules, and greetings from the CUPI API, then generate `callhandler_map.html` in the working directory.

Open `callhandler_map.html` in a browser to explore the interactive graph.

## Node Classifications

| Color  | Classification       | Description |
|--------|----------------------|-------------|
| Green  | **Root**             | Targeted by a routing rule and/or has no incoming edges from other handlers — valid entry points |
| Blue   | **Normal**           | Standard reachable call handler with both incoming and outgoing connections |
| Grey   | **True Orphan**      | No incoming edges, no outgoing edges, and not targeted by any routing rule |
| Orange | **Unreachable Subtree** | Has outgoing edges but no incoming edges and not targeted by a routing rule — exists but no caller can reach it |
| Red    | **Dead End**         | Has incoming edges but no outgoing edges and no transfer target — callers who reach this node have nowhere to go |

## Graph Features

- **Drag** nodes to rearrange the layout
- **Click** a node to view its details (DisplayName, extension, ObjectId, classification) in the sidebar
- **Toggle buttons** to show/hide true orphans, unreachable subtrees, and dead ends
- **Legend** explaining all node colors and classifications
- Edge labels show the key or rule name that triggers each route
