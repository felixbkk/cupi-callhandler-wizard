#!/usr/bin/env python3
"""
cupi-callhandler-wizard
Fetches call handler routing data from Cisco Unity Connection CUPI REST API
and generates an interactive D3.js force graph visualization.
"""

import argparse
import getpass
import json
import os
import platform
import shutil
import webbrowser
import re
import subprocess
import sys
from collections import deque
from datetime import datetime
from urllib.parse import quote, urlparse

import ssl

import requests
from requests.adapters import HTTPAdapter

requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)

HEADERS = {"Accept": "application/json"}
ROWS_PER_PAGE = 512


API_TIMEOUT = 30  # seconds per request


def api_get(session, host, path, params=None):
    url = f"{host}{path}"
    resp = session.get(url, params=params, headers=HEADERS, verify=False, timeout=API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def paginated_fetch(session, host, path, record_key, label=None):
    """Fetch all records from a paginated CUPI endpoint.
    record_key is the JSON key for the record list (e.g. 'Callhandler', 'Schedule').
    """
    all_records = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, host, path, params)
        total = int(data.get("@total", 0))
        if total == 0:
            break
        records = data.get(record_key, [])
        # CUPI returns a single object instead of a list when there's only one record
        if isinstance(records, dict):
            records = [records]
        all_records.extend(records)
        if label:
            print(f"  Fetched {len(all_records)}/{total} {label}")
        if len(all_records) >= total:
            break
        page += 1
    return all_records


def fetch_site_id(session, host):
    """Fetch a unique site identifier from the CUC cluster info."""
    try:
        data = api_get(session, host, "/vmrest/cluster")
        servers = data.get("ClusterMember", [])
        if isinstance(servers, dict):
            servers = [servers]
        if servers:
            # Use the first server's name as the site ID
            name = servers[0].get("ServerName", "") or servers[0].get("Hostname", "")
            if name:
                return name
    except requests.exceptions.HTTPError:
        pass
    # Fallback: try vmsservers
    try:
        data = api_get(session, host, "/vmrest/vmsservers")
        servers = data.get("VmsServer", data.get("VMSServer", []))
        if isinstance(servers, dict):
            servers = [servers]
        if servers:
            name = servers[0].get("ServerName", "")
            if name:
                return name
    except requests.exceptions.HTTPError:
        pass
    # Last resort: derive from host URL
    parsed = urlparse(host)
    return parsed.hostname or "unknown-site"


def friendly_site_name(site_id):
    """Extract a friendly display name from the server ID.

    Strips common CUC suffixes like '-ch-cuc1', '-cuc-pub', etc.
    'nairobi-ch-cuc1' -> 'Nairobi', 'london-nyc-ch-cuc2' -> 'London-Nyc'
    """
    name = re.sub(r'[-_]ch[-_]cuc\d*$', '', site_id, flags=re.IGNORECASE)
    name = re.sub(r'[-_]cuc[-_]?(pub|sub)?\d*$', '', name, flags=re.IGNORECASE)
    return name.replace('-', ' ').replace('_', ' ').strip().title() or site_id


def sanitize_dirname(name):
    """Make a string safe for use as a directory name."""
    return re.sub(r'[^\w\-.]', '_', name).strip('_')


def prepare_site_dir(site_id):
    """Create reports/<FriendlyName>_YYYY-MM-DD/ directory."""
    safe_name = sanitize_dirname(friendly_site_name(site_id))
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = os.path.join("reports", f"{safe_name}_{timestamp}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def fetch_call_handlers(session, host):
    print("Fetching call handlers...")
    all_handlers = paginated_fetch(session, host,
        "/vmrest/handlers/callhandlers", "Callhandler", "call handlers")
    # Filter out user voicemail handlers (numeric-only names like 88712142)
    before = len(all_handlers)
    all_handlers = [h for h in all_handlers
                    if not h.get("DisplayName", "").strip().isdigit()]
    skipped = before - len(all_handlers)
    if skipped:
        print(f"  Filtered out {skipped} voicemail handlers ({before} → {len(all_handlers)})")
    return all_handlers


def fetch_directory_handlers(session, host):
    print("Fetching directory handlers...")
    return paginated_fetch(session, host,
        "/vmrest/handlers/directoryhandlers", "DirectoryHandler", "directory handlers")


def fetch_interview_handlers(session, host):
    print("Fetching interview handlers...")
    return paginated_fetch(session, host,
        "/vmrest/handlers/interviewhandlers", "InterviewHandler", "interview handlers")


def fetch_routing_rules(session, host):
    print("Fetching routing rules...")
    return paginated_fetch(session, host,
        "/vmrest/routingrules", "RoutingRule", "routing rules")


def fetch_routing_rule_conditions(session, host, rule_id, rule_name):
    """Fetch conditions for a routing rule (called/calling number patterns, etc.)."""
    path = f"/vmrest/routingrules/{rule_id}/routingruleconditions"
    try:
        data = api_get(session, host, path)
        conditions = data.get("RoutingRuleCondition", [])
        if isinstance(conditions, dict):
            conditions = [conditions]
        return conditions
    except requests.exceptions.HTTPError:
        return []


# Condition parameter types from CUPI docs
_CONDITION_PARAMS = {
    "1": "Calling Number",
    "2": "Called Number",
    "3": "Forwarded From",
    "4": "Origin",
    "5": "Phone System",
    "6": "Port",
    "7": "Reason",
    "8": "Schedule",
    "9": "Trunk",
}

_CONDITION_OPS = {
    "1": "In",
    "2": "Equals",
    "3": "Greater Than",
    "4": "Less Than",
    "5": "Less Than or Equal",
    "6": "Greater Than or Equal",
}


def fetch_schedule_sets(session, host):
    """Fetch schedule sets and their member schedules."""
    print("Fetching schedule sets...")
    all_sets = paginated_fetch(session, host,
        "/vmrest/schedulesets", "ScheduleSet", "schedule sets")
    # Filter out subscriber-owned sets
    before = len(all_sets)
    all_sets = [s for s in all_sets if not s.get("OwnerSubscriberObjectId")]
    skipped = before - len(all_sets)
    if skipped:
        print(f"  Filtered out {skipped} user schedule sets ({before} -> {len(all_sets)})")
    return all_sets


# Track endpoints that have 404'd so we don't spam warnings for every handler
_disabled_endpoints = set()


def _fetch_handler_sub(session, host, handler_id, handler_name, subpath, key, alt_paths=None):
    """Fetch a call handler sub-resource, trying alternative paths if the primary 404s.
    Once an endpoint 404s, it's disabled for the rest of the run."""
    paths_to_try = [subpath] + (alt_paths or [])
    for path in paths_to_try:
        full = f"/vmrest/handlers/callhandlers/{handler_id}/{path}"
        if path in _disabled_endpoints:
            continue
        try:
            data = api_get(session, host, full)
            records = data.get(key, [])
            if isinstance(records, dict):
                records = [records]
            return records
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                if path not in _disabled_endpoints:
                    _disabled_endpoints.add(path)
                    print(f"  Endpoint '{path}' not available (404) — skipping for all handlers")
            else:
                print(f"  Warning: {path} failed for '{handler_name}': {e}")
            continue
    return []


def fetch_menu_entries(session, host, handler_id, handler_name):
    return _fetch_handler_sub(session, host, handler_id, handler_name,
                              "menuentries", "MenuEntry")


def fetch_transfer_rules(session, host, handler_id, handler_name):
    return _fetch_handler_sub(session, host, handler_id, handler_name,
                              "transferrules", "TransferRule",
                              alt_paths=["transferoptions"])


def fetch_greetings(session, host, handler_id, handler_name):
    return _fetch_handler_sub(session, host, handler_id, handler_name,
                              "greetings", "Greeting")


def fetch_holiday_schedules(session, host):
    """Fetch holiday schedules.

    Tries the legacy /vmrest/holidayschedules endpoint first.
    Falls back to extracting schedules with IsHoliday=true from /vmrest/schedules.
    """
    # Try legacy endpoint first
    try:
        print("Fetching holiday schedules...")
        all_schedules = paginated_fetch(session, host,
            "/vmrest/holidayschedules", "HolidaySchedule", "holiday schedules")

        # Fetch individual holidays for each schedule
        for sched in all_schedules:
            sched_id = sched.get("ObjectId", "")
            sched_name = sched.get("DisplayName", "Unknown")
            try:
                data = api_get(session, host, f"/vmrest/holidayschedules/{sched_id}/holidays")
                holidays = data.get("Holiday", [])
                if isinstance(holidays, dict):
                    holidays = [holidays]
                sched["_holidays"] = holidays
            except requests.exceptions.HTTPError:
                sched["_holidays"] = []

        return all_schedules
    except requests.exceptions.HTTPError:
        pass

    # Fallback: extract from regular schedules (IsHoliday=true)
    print("  Legacy endpoint unavailable, checking schedules for IsHoliday flag...")
    all_schedules = paginated_fetch(session, host, "/vmrest/schedules", "Schedule")
    holiday_scheds = [s for s in all_schedules
                      if str(s.get("IsHoliday", "false")).lower() == "true"]
    print(f"  Found {len(holiday_scheds)} holiday schedules from {len(all_schedules)} total")

    # Fetch schedule details as the holiday entries
    for sched in holiday_scheds:
        sched_id = sched.get("ObjectId", "")
        try:
            data = api_get(session, host, f"/vmrest/schedules/{sched_id}/scheduledetails")
            details = data.get("ScheduleDetail", [])
            if isinstance(details, dict):
                details = [details]
            # Map detail fields to match legacy holiday format
            sched["_holidays"] = [{
                "DisplayName": d.get("Subject", d.get("DisplayName", "")),
                "StartDate": d.get("StartDate", ""),
                "EndDate": d.get("EndDate", ""),
            } for d in details]
        except requests.exceptions.HTTPError:
            sched["_holidays"] = []

    return holiday_scheds


def fetch_schedules(session, host):
    print("Fetching schedules...")
    all_schedules = paginated_fetch(session, host,
        "/vmrest/schedules", "Schedule", "schedules")
    # Filter out per-user schedules (Sync Schedule, voice recognition, etc.)
    # Primary: OwnerSubscriberObjectId set means subscriber-owned
    # Fallback: known system-generated schedule names
    _SKIP_NAMES = {"Sync Schedule", "Voice Recognition Update Schedule"}
    before = len(all_schedules)
    all_schedules = [s for s in all_schedules
                     if not s.get("OwnerSubscriberObjectId")
                     and s.get("DisplayName", "") not in _SKIP_NAMES
                     and str(s.get("IsHoliday", "false")).lower() != "true"]
    skipped = before - len(all_schedules)
    if skipped:
        print(f"  Filtered out {skipped} user/system schedules ({before} -> {len(all_schedules)})")

    # Fetch time blocks for each remaining schedule
    total_sched = len(all_schedules)
    for i, sched in enumerate(all_schedules):
        sched_id = sched.get("ObjectId", "")
        sched_name = sched.get("DisplayName", "Unknown")
        if (i + 1) % 5 == 0 or i == 0 or i == total_sched - 1:
            print(f"  Fetching schedule details {i + 1}/{total_sched}: {sched_name}")
        try:
            data = api_get(session, host, f"/vmrest/schedules/{sched_id}/scheduledetails")
            details = data.get("ScheduleDetail", [])
            if isinstance(details, dict):
                details = [details]
            sched["_details"] = details
        except (requests.exceptions.HTTPError, requests.exceptions.Timeout):
            sched["_details"] = []

    return all_schedules


# -- Action type constants from CUPI --
ACTION_IGNORE = "0"
ACTION_HANGUP = "1"
ACTION_GOTO = "2"       # Route to handler/conversation
ACTION_ERROR = "3"
ACTION_TAKE_MSG = "4"
ACTION_SKIP_GREETING = "5"
ACTION_RESTART_GREETING = "6"
ACTION_XFER_ALT = "7"   # Transfer to alternate contact number
ACTION_ROUTE_NEXT = "8" # Route from next call routing rule

ACTION_LABELS = {
    ACTION_IGNORE: "Ignore",
    ACTION_HANGUP: "Hangup",
    ACTION_GOTO: "Goto",
    ACTION_ERROR: "Error",
    ACTION_TAKE_MSG: "Take Message",
    ACTION_SKIP_GREETING: "Skip Greeting",
    ACTION_RESTART_GREETING: "Restart Greeting",
    ACTION_XFER_ALT: "Transfer Alt Contact",
    ACTION_ROUTE_NEXT: "Route Next Rule",
}

# TargetConversation values — what the caller gets routed to
CONVERSATION_LABELS = {
    "PHTransfer": "Transfer",
    "PHGreeting": "Greeting",
    "PHInterview": "Interview",
    "AD": "Directory",
    "SubSignIn": "Sign In",
    "SubSysTransfer": "Sys Transfer",
    "SystemTransfer": "Sys Transfer",
    "BroadcastMessageAdministrator": "Broadcast Admin",
    "GreetingAdministrator": "Greeting Admin",
}

# Schedule context for transfer rules (by RuleIndex) and greetings (by GreetingType)
TRANSFER_SCHEDULE = {
    "0": "standard", "1": "offhours", "2": "alternate",
    "Standard": "standard", "Off Hours": "offhours", "Alternate": "alternate",
}
GREETING_SCHEDULE = {
    "Standard": "standard", "Off Hours": "offhours", "Closed": "offhours",
    "Holiday": "holiday", "Alternate": "alternate",
    "Busy": "always", "Internal": "always", "Error": "always",
}


def greeting_audio_url(host, handler_id, greeting_type, language_code="1033"):
    """Build the CUPI URL for a greeting's audio stream (WAV).
    This is the direct API path — requires authentication to access.
    """
    gt = quote(greeting_type, safe="")
    return (
        f"{host}/vmrest/handlers/callhandlers/{handler_id}"
        f"/greetings/{gt}/greetingstreamfiles/{language_code}/audio"
    )


def download_audio_files(session, nodes, site_dir):
    """Download all greeting audio WAV files into site_dir/audio/.

    Rewrites each node's audio[].url to the local relative path.
    Files are named: HandlerName - GreetingType.wav
    """
    audio_dir = os.path.join(site_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    total = sum(len(n.get("audio", [])) for n in nodes)
    if not total:
        return
    downloaded = 0
    failed = 0
    for node in nodes:
        handler_name = re.sub(r'[^\w\s\-]', '', node.get("name", "unknown")).strip()
        for a in node.get("audio", []):
            remote_url = a["url"]
            greeting = a.get("greeting", "greeting")
            filename = f"{handler_name} - {greeting}.wav"
            # Sanitize for filesystem
            filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
            local_path = os.path.join(audio_dir, filename)
            try:
                resp = session.get(remote_url, verify=False, timeout=API_TIMEOUT, stream=True)
                if resp.status_code == 200:
                    with open(local_path, "wb") as f:
                        for chunk in resp.iter_content(8192):
                            f.write(chunk)
                    a["url"] = f"audio/{filename}"
                    downloaded += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            print(f"  Audio: {downloaded + failed}/{total} {'downloaded' if not failed else f'({failed} failed)'}", end="\r")
    result = f"  Audio: {downloaded} downloaded"
    if failed:
        result += f", {failed} failed"
    print(f"{result}{' ' * 20}")


_HANDLER_TYPE_MAP = {"3": "callhandler", "5": "interview", "6": "directory"}
_RULE_TYPES = {"1": "Direct", "2": "Forwarded", "3": "Both"}


def _infer_node_type(conversation):
    """Infer node type from a TargetConversation value."""
    if conversation == "AD":
        return "directory"
    if conversation == "PHInterview":
        return "interview"
    return "callhandler"


def _ensure_handler_node(nodes, target_id, conversation="", dir_handler_map=None,
                         display_name="", node_type=None):
    """Create a stub handler node if it doesn't already exist."""
    if target_id in nodes:
        return
    if not node_type:
        node_type = _infer_node_type(conversation)
    name = display_name
    if not name and dir_handler_map:
        name = dir_handler_map.get(target_id, "")
    if not name:
        conv_label = CONVERSATION_LABELS.get(conversation, "")
        if conv_label and conv_label not in ("Transfer", "Greeting"):
            name = f"{conv_label} ({target_id[:8]})"
        else:
            name = f"Unknown ({target_id[:8]})"
    nodes[target_id] = {
        "id": target_id, "name": name, "extension": "",
        "type": node_type, "classification": "normal",
    }


def _ensure_action_node(nodes, action_id, name):
    """Create an action node if it doesn't already exist."""
    if action_id not in nodes:
        nodes[action_id] = {
            "id": action_id, "name": name, "extension": "",
            "type": "action", "classification": "normal",
        }


def _conv_suffix(conversation):
    """Return a label suffix like ' [Directory]' for non-standard conversations."""
    if conversation and conversation not in ("PHTransfer", "PHGreeting"):
        return f" [{CONVERSATION_LABELS.get(conversation, conversation)}]"
    return ""


def _add_route_edge(nodes, edges, source_id, action, target_id, conversation,
                    label, schedule="always", dir_handler_map=None, display_name=""):
    """Process a routing action: create target node if needed and add edge."""
    if action == ACTION_GOTO and target_id:
        _ensure_handler_node(nodes, target_id, conversation, dir_handler_map, display_name)
        edges.append({
            "source": source_id, "target": target_id,
            "label": f"{label}{_conv_suffix(conversation)}", "schedule": schedule,
        })
    elif action == ACTION_GOTO and not target_id and conversation:
        action_node_id = f"conv_{conversation}"
        _ensure_action_node(nodes, action_node_id, CONVERSATION_LABELS.get(conversation, conversation))
        edges.append({
            "source": source_id, "target": action_node_id,
            "label": label, "schedule": schedule,
        })
    elif action in (ACTION_HANGUP, ACTION_RESTART_GREETING, ACTION_SKIP_GREETING,
                    ACTION_TAKE_MSG, ACTION_ROUTE_NEXT, ACTION_XFER_ALT):
        action_node_id = f"action_{action}"
        _ensure_action_node(nodes, action_node_id, ACTION_LABELS.get(action, f"Action {action}"))
        edges.append({
            "source": source_id, "target": action_node_id,
            "label": label, "schedule": schedule,
        })


def build_graph(call_handlers, interview_handlers, routing_rules, session, host,
                schedule_set_map=None, directory_handlers=None):
    nodes = {}
    edges = []
    dir_handler_map = {}  # ObjectId → DisplayName for directory handlers
    for dh in (directory_handlers or []):
        dir_handler_map[dh.get("ObjectId", "")] = dh.get("DisplayName", "Unknown")

    # Add call handler nodes
    for ch in call_handlers:
        oid = ch.get("ObjectId", "")
        name = ch.get("DisplayName", "Unknown")
        ext = ch.get("DtmfAccessId", "")
        sched_set_id = ch.get("ScheduleSetObjectId", "")
        sched_name = ""
        if schedule_set_map and sched_set_id in schedule_set_map:
            sched_name = schedule_set_map[sched_set_id]
        post_greeting = str(ch.get("PlayPostGreetingRecording", "0"))
        nodes[oid] = {
            "id": oid,
            "name": name,
            "extension": ext,
            "type": "callhandler",
            "classification": "normal",
            "audio": [],
            "scheduleName": sched_name,
            "system": str(ch.get("Undeletable", "false")).lower() == "true",
            "postGreeting": post_greeting != "0",
        }

    # Add interview handler nodes and after-message routing
    for ih in interview_handlers:
        oid = ih.get("ObjectId", "")
        name = ih.get("DisplayName", "Unknown")
        nodes[oid] = {
            "id": oid, "name": name, "extension": "",
            "type": "interview", "classification": "normal",
        }
        _add_route_edge(nodes, edges, oid,
            str(ih.get("AfterMessageAction", "0")),
            ih.get("AfterMessageTargetHandlerObjectId", ""),
            ih.get("AfterMessageTargetConversation", ""),
            "After Interview", dir_handler_map=dir_handler_map)

    # Add directory handler nodes and exit routing edges
    _DIR_EXIT_FIELDS = [
        ("Exit", "Exit (*/# key)"),
        ("NoInput", "No Input"),
        ("NoSelection", "No Selection"),
        ("Zero", "Press 0"),
    ]
    for dh in (directory_handlers or []):
        oid = dh.get("ObjectId", "")
        name = dh.get("DisplayName", "Unknown")
        ext = dh.get("DtmfAccessId", "")
        if oid not in nodes:
            nodes[oid] = {
                "id": oid, "name": name, "extension": ext,
                "type": "directory", "classification": "normal",
            }
        else:
            nodes[oid]["name"] = name
            nodes[oid]["extension"] = ext
        for prefix, label in _DIR_EXIT_FIELDS:
            _add_route_edge(nodes, edges, oid,
                str(dh.get(f"{prefix}Action", "0")),
                dh.get(f"{prefix}TargetHandlerObjectId", ""),
                dh.get(f"{prefix}TargetConversation", ""),
                label, dir_handler_map=dir_handler_map)

    # Track which handler OIDs are targeted by routing rules
    routing_targets = set()

    # Add routing rule nodes and edges
    total_rules = len(routing_rules)
    for i, rule in enumerate(routing_rules):
        rule_oid = rule.get("ObjectId", "")
        rule_name = rule.get("DisplayName", rule.get("RuleName", "Routing Rule"))
        target_oid = rule.get("RouteTargetHandlerObjectId", "")
        rule_state = str(rule.get("State", "0"))
        rule_type = str(rule.get("Type", "3"))
        _RULE_TYPES = {"1": "Direct", "2": "Forwarded", "3": "Both"}

        # Fetch conditions for this rule
        conditions = fetch_routing_rule_conditions(session, host, rule_oid, rule_name)
        cond_list = []
        for c in conditions:
            param = _CONDITION_PARAMS.get(str(c.get("Parameter", "")), "Unknown")
            op = _CONDITION_OPS.get(str(c.get("Operator", "")), "?")
            value = c.get("OperandValue", "")
            cond_list.append({"param": param, "op": op, "value": value})
        if (i + 1) % 5 == 0 or i == 0 or i == total_rules - 1:
            print(f"  Fetching rule conditions {i + 1}/{total_rules}: {rule_name}")

        nodes[rule_oid] = {
            "id": rule_oid,
            "name": rule_name,
            "extension": "",
            "type": "routingrule",
            "classification": "root",
            "conditions": cond_list,
            "ruleState": "Active" if rule_state == "0" else "Inactive" if rule_state == "1" else "Invalid",
            "ruleType": _RULE_TYPES.get(rule_type, "Unknown"),
        }

        route_conv = rule.get("RouteTargetConversation", "")
        if target_oid:
            # Use RouteTargetHandlerObjectType to infer type if available
            obj_type = str(rule.get("RouteTargetHandlerObjectType", ""))
            node_type = _HANDLER_TYPE_MAP.get(obj_type) or _infer_node_type(route_conv)
            _ensure_handler_node(nodes, target_oid, route_conv, dir_handler_map,
                display_name=rule.get("RouteTargetHandlerDisplayName", ""),
                node_type=node_type)
            routing_targets.add(target_oid)
            edges.append({
                "source": rule_oid, "target": target_oid,
                "label": f"{rule_name}{_conv_suffix(route_conv)}", "schedule": "always",
            })
        elif route_conv and route_conv not in ("PHTransfer", "PHGreeting"):
            action_node_id = f"conv_{route_conv}"
            _ensure_action_node(nodes, action_node_id, CONVERSATION_LABELS.get(route_conv, route_conv))
            edges.append({
                "source": rule_oid, "target": action_node_id,
                "label": rule_name, "schedule": "always",
            })

    # Track transfer target extensions for dead-end detection
    has_transfer_target = set()

    # Fetch menu entries, transfer rules, and greetings for each call handler
    total = len(call_handlers)
    for i, ch in enumerate(call_handlers):
        oid = ch.get("ObjectId", "")
        name = ch.get("DisplayName", "Unknown")
        if (i + 1) % 10 == 0 or i == 0 or i == total - 1:
            print(f"Fetching details for handler {i + 1}/{total}: {name}")

        # Menu entries
        menu_entries = fetch_menu_entries(session, host, oid, name)
        for entry in menu_entries:
            action = str(entry.get("Action", "0"))
            if action == ACTION_IGNORE:
                continue
            key = entry.get("TouchtoneKey", "?")
            _add_route_edge(nodes, edges, oid, action,
                entry.get("TargetHandlerObjectId", ""),
                entry.get("TargetConversation", ""),
                f"Key {key}", dir_handler_map=dir_handler_map)

        # Transfer rules
        transfer_rules = fetch_transfer_rules(session, host, oid, name)
        for tr in transfer_rules:
            rule_name_t = tr.get("RuleIndex", tr.get("TransferRuleDisplayName", "Transfer"))
            extension = tr.get("Extension", "")
            tr_enabled = tr.get("TransferEnabled", "false")
            target_handler = tr.get("TargetHandlerObjectId", "")
            tr_schedule = TRANSFER_SCHEDULE.get(str(rule_name_t), "standard")

            if target_handler and target_handler in nodes:
                edges.append({
                    "source": oid,
                    "target": target_handler,
                    "label": f"Xfer:{rule_name_t}",
                    "schedule": tr_schedule,
                })
                has_transfer_target.add(oid)
            elif extension and str(tr_enabled).lower() == "true":
                # Create a terminal phone node for the extension
                phone_id = f"phone_{extension}"
                if phone_id not in nodes:
                    nodes[phone_id] = {
                        "id": phone_id,
                        "name": f"Ext {extension}",
                        "extension": extension,
                        "type": "phone",
                        "classification": "normal",
                    }
                edges.append({
                    "source": oid,
                    "target": phone_id,
                    "label": f"Xfer:{rule_name_t}",
                    "schedule": tr_schedule,
                })
                has_transfer_target.add(oid)

        # Greetings (audio URLs + after-greeting routing)
        greetings = fetch_greetings(session, host, oid, name)
        for gr in greetings:
            greeting_name = gr.get("GreetingType", "Greeting")
            language_code = str(gr.get("LanguageCode", "1033"))
            play_what = str(gr.get("PlayWhat", ""))  # 0=nothing, 1=default/uploaded, 2=custom recording
            gr_schedule = GREETING_SCHEDULE.get(greeting_name, "always")
            enabled = str(gr.get("Enabled", "true")).lower() == "true"
            # Audio: include any greeting with a recording (PlayWhat 1 or 2)
            if play_what in ("1", "2"):
                audio_url = greeting_audio_url(host, oid, greeting_name, language_code)
                nodes[oid]["audio"].append({
                    "greeting": greeting_name,
                    "url": audio_url,
                    "schedule": gr_schedule,
                })
            # Routing: only follow after-greeting actions for enabled greetings
            if not enabled:
                continue
            action = str(gr.get("AfterGreetingAction", "0"))
            target = gr.get("AfterGreetingTargetHandlerObjectId", "")
            if action == ACTION_GOTO and target:
                _ensure_handler_node(nodes, target, dir_handler_map=dir_handler_map)
                edges.append({
                    "source": oid, "target": target,
                    "label": f"After:{greeting_name}", "schedule": gr_schedule,
                })

    # Build adjacency maps
    incoming = {nid: set() for nid in nodes}
    outgoing = {nid: set() for nid in nodes}
    outgoing_by_schedule = {}  # nid → {schedule → set of target nids}
    for edge in edges:
        src = edge["source"]
        tgt = edge["target"]
        sched = edge.get("schedule", "always")
        if tgt in incoming:
            incoming[tgt].add(src)
        if src in outgoing:
            outgoing[src].add(tgt)
        outgoing_by_schedule.setdefault(src, {}).setdefault(sched, set()).add(tgt)

    # BFS reachability from all routing rule nodes
    def bfs_reachable(start_nodes, edge_filter=None):
        """Return set of all node IDs reachable from start_nodes."""
        visited = set()
        queue = deque(start_nodes)
        while queue:
            nid = queue.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            if edge_filter is None:
                for tgt in outgoing.get(nid, set()):
                    if tgt not in visited:
                        queue.append(tgt)
            else:
                for sched, targets in outgoing_by_schedule.get(nid, {}).items():
                    if edge_filter(sched):
                        for tgt in targets:
                            if tgt not in visited:
                                queue.append(tgt)
        return visited

    # All routing rule node IDs are entry points
    root_ids = {nid for nid, n in nodes.items() if n["type"] == "routingrule"}

    # Global reachability (any schedule)
    reachable_all = bfs_reachable(root_ids)

    # Per-schedule reachability
    def schedule_filter(active_sched):
        return lambda s: s == "always" or s == active_sched

    reachable_standard = bfs_reachable(root_ids, schedule_filter("standard"))
    reachable_offhours = bfs_reachable(root_ids, schedule_filter("offhours"))
    reachable_holiday = bfs_reachable(root_ids, schedule_filter("holiday"))

    # Classify nodes using true reachability
    for nid, node in nodes.items():
        if node["type"] == "routingrule":
            node["classification"] = "root"
            node["reachable"] = {"standard": True, "offhours": True, "holiday": True}
            continue
        if node["type"] == "phone":
            node["reachable"] = {"standard": True, "offhours": True, "holiday": True}
            continue

        has_in = len(incoming[nid]) > 0
        has_out = len(outgoing[nid]) > 0
        is_routing_target = nid in routing_targets
        is_reachable = nid in reachable_all

        node["reachable"] = {
            "standard": nid in reachable_standard,
            "offhours": nid in reachable_offhours,
            "holiday": nid in reachable_holiday,
        }

        if is_routing_target:
            node["classification"] = "root"
        elif not has_in and not has_out:
            node["classification"] = "orphan"
        elif not is_reachable and has_out:
            node["classification"] = "unreachable"
        elif not is_reachable and has_in:
            # Part of a disconnected cluster — has edges but no path from any root
            node["classification"] = "unreachable"
        elif is_reachable and has_in and not has_out and nid not in has_transfer_target:
            node["classification"] = "deadend"
        else:
            node["classification"] = "normal"

    # Identify primary root: the root call handler with the most incoming edges
    root_handlers = [(nid, node) for nid, node in nodes.items()
                     if node["classification"] == "root" and node["type"] == "callhandler"]
    if root_handlers:
        primary_id = max(root_handlers, key=lambda x: len(incoming[x[0]]))[0]
        nodes[primary_id]["primary"] = True

    return list(nodes.values()), edges


NAV_PAGES = [
    ("index.html", "Home"),
    ("callhandler_map.html", "Graph"),
    ("callflow.html", "Call Flow"),
    ("callhandler_report.html", "Handlers"),
    ("schedules.html", "Schedules"),
    ("test_times.html", "Test Times"),
]


def floating_nav_html(current_file):
    """Generate a floating navigation pill (CSS + HTML) for use on any page.

    current_file: the filename of the page being generated (e.g. 'callhandler_map.html')
    """
    links = []
    for href, label in NAV_PAGES:
        if href == current_file:
            links.append(f'<span class="fnav-current">{label}</span>')
        else:
            links.append(f'<a href="{href}">{label}</a>')
    links_html = "\n".join(links)
    return f'''<style>
.fnav {{ position: fixed; top: 12px; right: 12px; z-index: 9999; display: flex; gap: 2px; align-items: center;
  background: #0d1b2a; border: 1px solid #0f3460; border-radius: 6px; padding: 6px 10px;
  font-size: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.4); opacity: 0.45; transition: opacity 0.2s; }}
.fnav:hover {{ opacity: 1; }}
.fnav a {{ color: #1abc9c; text-decoration: none; padding: 3px 8px; border-radius: 4px; white-space: nowrap; }}
.fnav a:hover {{ background: #16213e; color: #fff; }}
.fnav-current {{ color: #e94560; font-weight: 700; padding: 3px 8px; white-space: nowrap; }}
</style>
<div class="fnav">
{links_html}
</div>'''


D3_CDN_URL = "https://d3js.org/d3.v7.min.js"
D3_FILENAME = "d3.v7.min.js"


def copy_d3(site_dir):
    """Copy bundled D3.js into the report directory for offline use."""
    dest = os.path.join(site_dir, D3_FILENAME)
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), D3_FILENAME)
    try:
        shutil.copy2(src, dest)
        return True
    except Exception as e:
        print(f"  Warning: Could not copy bundled D3.js: {e}")
        print("  Graph will require internet access to load D3 from CDN")
        return False


def generate_html(nodes, edges, d3_local=False, site_name="", host=""):
    graph_data = json.dumps({"nodes": nodes, "links": edges, "host": host, "siteName": site_name})
    d3_tag = f'<script src="{D3_FILENAME}"></script>' if d3_local else f'<script src="{D3_CDN_URL}"></script>'
    title_prefix = f"{site_name} — " if site_name else ""
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_prefix}Call Handler Routing Map</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='12' fill='%231a1a2e'/><path d='M16 20a4 4 0 014-4h8a4 4 0 014 4v24a4 4 0 01-4 4h-8a4 4 0 01-4-4z' fill='%23e94560'/><circle cx='24' cy='42' r='2' fill='%231a1a2e'/><path d='M36 28h10m0 0l-4-4m4 4l-4 4' stroke='%232ecc71' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/><path d='M36 38h10m0 0l-4-4m4 4l-4 4' stroke='%233498db' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>">
{d3_tag}
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; height: 100vh; background: #1a1a2e; color: #e0e0e0; }}
#detail-panel {{ width: 320px; background: #16213e; border-right: 1px solid #0f3460; padding: 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; transition: width 0.2s; }}
#detail-panel.collapsed {{ width: 0; padding: 0; overflow: hidden; border: none; }}
#detail-panel h3 {{ color: #e94560; font-size: 14px; margin-top: 8px; }}
.detail-row {{ display: flex; flex-direction: column; gap: 2px; padding: 4px 0; }}
.detail-label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
.detail-value {{ font-size: 14px; word-break: break-all; }}
#graph-container {{ flex: 1; position: relative; overflow: hidden; }}
svg {{ width: 100%; height: 100%; }}
#sidebar {{ width: 280px; background: #16213e; border-left: 1px solid #0f3460; padding: 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; }}
#sidebar h2 {{ color: #e94560; font-size: 18px; border-bottom: 1px solid #0f3460; padding-bottom: 8px; }}
#sidebar h3 {{ color: #e94560; font-size: 14px; margin-top: 8px; }}
.controls {{ display: flex; flex-direction: column; gap: 8px; }}
.toggle-btn {{ padding: 8px 12px; border: 1px solid #0f3460; background: #16213e; color: #e0e0e0; cursor: pointer; border-radius: 4px; font-size: 12px; text-align: left; transition: background 0.2s; }}
.toggle-btn:hover {{ background: #0f3460; }}
.toggle-btn.active {{ background: #0f3460; border-color: #e94560; }}
.legend {{ display: flex; flex-direction: column; gap: 6px; }}
.legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 12px; }}
.legend-dot {{ width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }}
.node-label {{ font-size: 10px; fill: #ccc; pointer-events: none; }}
.link-label {{ font-size: 9px; fill: #888; pointer-events: none; }}
.link {{ stroke-opacity: 0.5; fill: none; }}
marker {{ fill: #666; }}
</style>
</head>
<body>
<div id="detail-panel" class="collapsed">
<div id="node-details">
<h3>Node Details</h3>
<p style="font-size:12px; color:#666;">Click a node to see details</p>
</div>
</div>
<div id="graph-container">
<svg></svg>
</div>
<div id="sidebar">
<h2>{title_prefix}Call Handler Map</h2>
<div class="controls">
<h3>Layout</h3>
<div style="display:flex; gap:6px; flex-wrap:wrap;">
<button class="toggle-btn active" id="layout-force" onclick="setLayout(\'force\')">Force</button>
<button class="toggle-btn" id="layout-hierarchical" onclick="setLayout(\'hierarchical\')">Hierarchical</button>
<button class="toggle-btn" id="layout-radial" onclick="setLayout(\'radial\')">Radial</button>
</div>
</div>
<div class="controls">
<h3>Navigation</h3>
<div style="display:flex; gap:6px; flex-wrap:wrap;">
<button class="toggle-btn" onclick="zoomIn()">Zoom In</button>
<button class="toggle-btn" onclick="zoomOut()">Zoom Out</button>
<button class="toggle-btn" onclick="fitAll()">Fit All</button>
<button class="toggle-btn" onclick="unpinAll()">Unpin All</button>
</div>
</div>
<div class="controls">
<h3>Toggle Visibility</h3>
<button class="toggle-btn active" data-class="orphan" onclick="toggleClass(this, \'orphan\')">Show True Orphans</button>
<button class="toggle-btn active" data-class="unreachable" onclick="toggleClass(this, \'unreachable\')">Show Unreachable Subtrees</button>
<button class="toggle-btn active" data-class="deadend" onclick="toggleClass(this, \'deadend\')">Show Dead Ends</button>
</div>
<div class="legend">
<h3>Legend</h3>
<div class="legend-item"><span class="legend-dot" style="background:#ffd700"></span> Primary Root</div>
<div class="legend-item"><span class="legend-dot" style="background:#2ecc71"></span> Root (entry point)</div>
<div class="legend-item"><span class="legend-dot" style="background:#3498db"></span> Normal</div>
<div class="legend-item"><span class="legend-dot" style="background:#95a5a6"></span> True Orphan (isolated)</div>
<div class="legend-item"><span class="legend-dot" style="background:#e67e22"></span> Unreachable Subtree</div>
<div class="legend-item"><span class="legend-dot" style="background:#e74c3c"></span> Dead End</div>
<div class="legend-item"><span class="legend-dot" style="background:#9b59b6"></span> Interview Handler</div>
<div class="legend-item"><span class="legend-dot" style="background:#1abc9c"></span> Phone Extension</div>
<div class="legend-item"><span class="legend-dot" style="background:#f39c12"></span> Directory Handler</div>
<div class="legend-item"><span class="legend-dot" style="background:#e74c3c"></span> Action (Hangup, etc.)</div>
</div>
</div>
<script>
const graphData = {graph_data};

function adminUrl(node) {{
    if (!graphData.host || !node || !node.id) return "";
    const ops = {{
        callhandler: "callhandler.do?op=read",
        interview: "interviewhandler.do?op=read",
        directory: "directoryhandler.do?op=read",
        routingrule: "routingrule.do?op=read",
    }};
    const op = ops[node.type];
    return op ? graphData.host + "/cuadmin/" + op + "&objectId=" + node.id : "";
}}

function greetingUrl(handlerId, greetingType) {{
    if (!graphData.host) return "";
    return graphData.host + "/cuadmin/greeting.do?op=readCallhandler&objectId=" + handlerId + "&greetingType=" + encodeURIComponent(greetingType);
}}

const colorMap = {{
    root: "#2ecc71",
    normal: "#3498db",
    orphan: "#95a5a6",
    unreachable: "#e67e22",
    deadend: "#e74c3c"
}};

const typeColorOverride = {{
    interview: "#9b59b6",
    phone: "#1abc9c",
    routingrule: "#2ecc71",
    directory: "#f39c12",
    action: "#e74c3c"
}};

function nodeColor(d) {{
    if (d.primary) return "#ffd700";
    if (typeColorOverride[d.type]) return typeColorOverride[d.type];
    return colorMap[d.classification] || colorMap.normal;
}}

function nodeRadius(d) {{
    if (d.primary) return 14;
    if (d.type === "routingrule") return 10;
    if (d.type === "phone" || d.type === "action") return 6;
    return 8;
}}

const hiddenClasses = new Set();

function toggleClass(btn, cls) {{
    btn.classList.toggle("active");
    if (hiddenClasses.has(cls)) {{
        hiddenClasses.delete(cls);
    }} else {{
        hiddenClasses.add(cls);
    }}
    updateVisibility();
}}

function updateVisibility() {{
    node.style("display", d => hiddenClasses.has(d.classification) ? "none" : null);
    label.style("display", d => hiddenClasses.has(d.classification) ? "none" : null);
    link.style("display", d => {{
        const srcNode = typeof d.source === "object" ? d.source : graphData.nodes.find(n => n.id === d.source);
        const tgtNode = typeof d.target === "object" ? d.target : graphData.nodes.find(n => n.id === d.target);
        if (!srcNode || !tgtNode) return null;
        return (hiddenClasses.has(srcNode.classification) || hiddenClasses.has(tgtNode.classification)) ? "none" : null;
    }});
    linkLabel.style("display", d => {{
        const srcNode = typeof d.source === "object" ? d.source : graphData.nodes.find(n => n.id === d.source);
        const tgtNode = typeof d.target === "object" ? d.target : graphData.nodes.find(n => n.id === d.target);
        if (!srcNode || !tgtNode) return null;
        return (hiddenClasses.has(srcNode.classification) || hiddenClasses.has(tgtNode.classification)) ? "none" : null;
    }});
}}

const container = document.getElementById("graph-container");
const width = container.clientWidth;
const height = container.clientHeight;

const svg = d3.select("svg")
    .attr("viewBox", [0, 0, width, height]);

const g = svg.append("g");

const zoom = d3.zoom()
    .scaleExtent([0.1, 8])
    .on("zoom", (event) => g.attr("transform", event.transform));
svg.call(zoom);

function zoomIn() {{
    svg.transition().duration(300).call(zoom.scaleBy, 1.5);
}}
function zoomOut() {{
    svg.transition().duration(300).call(zoom.scaleBy, 0.67);
}}
function fitAll() {{
    const bounds = g.node().getBBox();
    if (bounds.width === 0 || bounds.height === 0) return;
    const pad = 40;
    const scale = Math.min(
        width / (bounds.width + pad * 2),
        height / (bounds.height + pad * 2)
    );
    const tx = width / 2 - (bounds.x + bounds.width / 2) * scale;
    const ty = height / 2 - (bounds.y + bounds.height / 2) * scale;
    svg.transition().duration(500)
        .call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
}}
function unpinAll() {{
    graphData.nodes.forEach(d => {{ d.fx = null; d.fy = null; }});
    simulation.alphaTarget(0.3).restart();
    setTimeout(() => simulation.alphaTarget(0), 500);
    updatePinIndicators();
}}

const defs = svg.append("defs");
[["arrowhead", "#666"], ["arrow-out", "#2ecc71"], ["arrow-in", "#3498db"]].forEach(([id, color]) => {{
    defs.append("marker")
        .attr("id", id)
        .attr("viewBox", "0 -5 10 10")
        .attr("refX", 20).attr("refY", 0)
        .attr("markerWidth", 6).attr("markerHeight", 6)
        .attr("orient", "auto")
        .append("path").attr("d", "M0,-5L10,0L0,5").attr("fill", color);
}});

// Click background to clear selection
svg.on("click", function(event) {{
    if (event.target.tagName !== "circle") {{
        clearHighlight();
        document.getElementById("detail-panel").classList.add("collapsed");
        document.getElementById("node-details").innerHTML = '<h3>Node Details</h3><p style="font-size:12px; color:#666;">Click a node to see details</p>';
    }}
}});

// BFS depth from root nodes for hierarchical/radial layouts
const adj = {{}};
graphData.nodes.forEach(n => adj[n.id] = []);
graphData.links.forEach(l => {{
    const sid = typeof l.source === "object" ? l.source.id : l.source;
    const tid = typeof l.target === "object" ? l.target.id : l.target;
    adj[sid].push(tid);
}});
const roots = graphData.nodes.filter(n => n.type === "routingrule" || n.classification === "root");
const depthMap = {{}};
const bfsQueue = roots.map(r => {{ depthMap[r.id] = 0; return r.id; }});
while (bfsQueue.length) {{
    const nid = bfsQueue.shift();
    (adj[nid] || []).forEach(tid => {{
        if (depthMap[tid] === undefined) {{
            depthMap[tid] = depthMap[nid] + 1;
            bfsQueue.push(tid);
        }}
    }});
}}
const maxDepth = Math.max(1, ...Object.values(depthMap));
// Assign depth to unreachable nodes
graphData.nodes.forEach(n => {{
    if (depthMap[n.id] === undefined) depthMap[n.id] = maxDepth + 1;
}});

let currentLayout = "force";

const simulation = d3.forceSimulation(graphData.nodes)
    .force("link", d3.forceLink(graphData.links).id(d => d.id).distance(120))
    .force("charge", d3.forceManyBody().strength(-300))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collision", d3.forceCollide().radius(20));

function setLayout(mode) {{
    currentLayout = mode;
    document.querySelectorAll("[id^=layout-]").forEach(b => b.classList.remove("active"));
    document.getElementById("layout-" + mode).classList.add("active");

    // Clear all pins
    graphData.nodes.forEach(d => {{ d.fx = null; d.fy = null; }});

    if (mode === "force") {{
        simulation
            .force("center", d3.forceCenter(width / 2, height / 2))
            .force("charge", d3.forceManyBody().strength(-300))
            .force("x", null)
            .force("y", null);
    }} else if (mode === "hierarchical") {{
        const layerH = height / (maxDepth + 3);
        // Count nodes per depth for horizontal spread
        const perDepth = {{}};
        graphData.nodes.forEach(n => {{
            const d = depthMap[n.id];
            perDepth[d] = (perDepth[d] || 0) + 1;
        }});
        const depthIdx = {{}};
        graphData.nodes.forEach(n => {{
            const d = depthMap[n.id];
            depthIdx[d] = (depthIdx[d] || 0) + 1;
            const count = perDepth[d];
            const spacing = width / (count + 1);
            n.fx = spacing * depthIdx[d];
            n.fy = layerH * (d + 1);
        }});
        simulation
            .force("center", null)
            .force("charge", null)
            .force("x", null)
            .force("y", null);
    }} else if (mode === "radial") {{
        const maxR = Math.min(width, height) / 2 - 60;
        const perDepth = {{}};
        graphData.nodes.forEach(n => {{
            const d = depthMap[n.id];
            perDepth[d] = (perDepth[d] || 0) + 1;
        }});
        const depthIdx = {{}};
        graphData.nodes.forEach(n => {{
            const d = depthMap[n.id];
            depthIdx[d] = (depthIdx[d] || 0) + 1;
            const count = perDepth[d];
            const r = d === 0 ? 0 : (d / (maxDepth + 1)) * maxR;
            const angle = (2 * Math.PI * depthIdx[d]) / count - Math.PI / 2;
            if (d === 0 && count === 1) {{
                n.fx = width / 2;
                n.fy = height / 2;
            }} else {{
                n.fx = width / 2 + r * Math.cos(angle);
                n.fy = height / 2 + r * Math.sin(angle);
            }}
        }});
        simulation
            .force("center", null)
            .force("charge", null)
            .force("x", null)
            .force("y", null);
    }}

    updatePinIndicators();
    simulation.alpha(1).restart();
    setTimeout(() => fitAll(), 600);
}}

const link = g.append("g")
    .selectAll("line")
    .data(graphData.links)
    .join("line")
    .attr("class", "link")
    .attr("stroke", "#666")
    .attr("stroke-width", 1.5)
    .attr("marker-end", "url(#arrowhead)");

const linkLabel = g.append("g")
    .selectAll("text")
    .data(graphData.links)
    .join("text")
    .attr("class", "link-label")
    .text(d => d.label);

const node = g.append("g")
    .selectAll("circle")
    .data(graphData.nodes)
    .join("circle")
    .attr("r", d => nodeRadius(d))
    .attr("fill", d => nodeColor(d))
    .attr("stroke", "#fff")
    .attr("stroke-width", 1.5)
    .style("cursor", "pointer")
    .call(d3.drag()
        .on("start", dragstarted)
        .on("drag", dragged)
        .on("end", dragended))
    .on("click", (event, d) => {{
        event.stopPropagation();
        showDetails(d);
    }})
    .on("dblclick", (event, d) => {{
        d.fx = null;
        d.fy = null;
        simulation.alphaTarget(0.3).restart();
        setTimeout(() => simulation.alphaTarget(0), 300);
        updatePinIndicators();
    }});

const label = g.append("g")
    .selectAll("text")
    .data(graphData.nodes)
    .join("text")
    .attr("class", "node-label")
    .attr("dy", -12)
    .attr("text-anchor", "middle")
    .text(d => d.name.length > 25 ? d.name.substring(0, 22) + "..." : d.name);

simulation.on("tick", () => {{
    link
        .attr("x1", d => d.source.x)
        .attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x)
        .attr("y2", d => d.target.y);
    linkLabel
        .attr("x", d => (d.source.x + d.target.x) / 2)
        .attr("y", d => (d.source.y + d.target.y) / 2);
    node
        .attr("cx", d => d.x)
        .attr("cy", d => d.y);
    label
        .attr("x", d => d.x)
        .attr("y", d => d.y);
}});

let _hasDragged = false;
let _wasPinned = false;

function dragstarted(event) {{
    if (!event.active) simulation.alphaTarget(0.3).restart();
    _wasPinned = event.subject.fx != null;
    _hasDragged = false;
    event.subject.fx = event.subject.x;
    event.subject.fy = event.subject.y;
}}

function dragged(event) {{
    _hasDragged = true;
    event.subject.fx = event.x;
    event.subject.fy = event.y;
}}

function dragended(event) {{
    if (!event.active) simulation.alphaTarget(0);
    if (_hasDragged) {{
        // Actually moved — pin it
        event.subject.fx = event.x;
        event.subject.fy = event.y;
    }} else {{
        // Just a click — restore original pin state
        if (!_wasPinned) {{
            event.subject.fx = null;
            event.subject.fy = null;
        }}
    }}
    updatePinIndicators();
}}

function updatePinIndicators() {{
    node.attr("stroke", d => d.fx != null ? "#e94560" : "#fff")
        .attr("stroke-width", d => d.fx != null ? 2.5 : 1.5);
}}

const classLabels = {{
    root: "Root (Entry Point)",
    normal: "Normal",
    orphan: "True Orphan",
    unreachable: "Unreachable Subtree",
    deadend: "Dead End"
}};

function showDetails(d) {{
    const esc = s => {{ const el = document.createElement("span"); el.textContent = s || ""; return el.innerHTML; }};

    const outgoing = graphData.links.filter(l => {{
        const sid = typeof l.source === "object" ? l.source.id : l.source;
        return sid === d.id;
    }});
    const incoming = graphData.links.filter(l => {{
        const tid = typeof l.target === "object" ? l.target.id : l.target;
        return tid === d.id;
    }});

    const aUrl = adminUrl(d);
    let html = '<h3>Node Details</h3>' +
        (aUrl ? '<div style="margin-bottom:8px;"><a href="' + esc(aUrl) + '" target="_blank" style="color:#1abc9c; font-size:12px; text-decoration:none;">View on ' + esc(graphData.siteName || 'Unity') + ' Unity &rarr;</a></div>' : '') +
        '<div class="detail-row"><span class="detail-label">Display Name</span><span class="detail-value">' + esc(d.name) + '</span></div>' +
        '<div class="detail-row"><span class="detail-label">Extension</span><span class="detail-value">' + esc(d.extension || "N/A") + '</span></div>' +
        '<div class="detail-row"><span class="detail-label">Type</span><span class="detail-value">' + esc(d.type) + '</span></div>' +
        '<div class="detail-row"><span class="detail-label">Classification</span><span class="detail-value" style="color:' + nodeColor(d) + '">' + esc(classLabels[d.classification] || d.classification) + '</span></div>';

    if (d.scheduleName) {{
        html += '<div class="detail-row"><span class="detail-label">Schedule</span><span class="detail-value">' + esc(d.scheduleName) + '</span></div>';
    }}

    if (d.audio && d.audio.length) {{
        html += '<h3 style="margin-top:12px;">Audio</h3>';
        d.audio.forEach(a => {{
            html += '<div class="detail-row" style="padding:4px 0;"><span style="color:#1abc9c; font-size:12px;">&#9835; ' + esc(a.greeting) + '</span><br>' +
                '<audio controls preload="none" style="width:100%; height:28px; margin-top:2px;"><source src="' + esc(a.url) + '" type="audio/wav"></audio></div>';
        }});
    }}

    if (incoming.length) {{
        html += '<h3 style="margin-top:12px; color:#3498db;">Incoming (' + incoming.length + ')</h3>';
        incoming.forEach(l => {{
            const src = typeof l.source === "object" ? l.source : graphData.nodes.find(n => n.id === l.source);
            const srcId = src ? src.id : null;
            html += '<div class="detail-row rel-link" style="padding:2px 0; cursor:pointer;" data-node="' + (srcId || '') + '">' +
                '<span class="detail-value" style="font-size:12px;"><span style="color:#888;">' + esc(l.label || '') + '</span> &larr; <strong style="color:#3498db;">' + esc(src ? src.name : "?") + '</strong></span></div>';
        }});
    }}

    if (outgoing.length) {{
        html += '<h3 style="margin-top:12px; color:#2ecc71;">Outgoing (' + outgoing.length + ')</h3>';
        outgoing.forEach(l => {{
            const tgt = typeof l.target === "object" ? l.target : graphData.nodes.find(n => n.id === l.target);
            const tgtId = tgt ? tgt.id : null;
            html += '<div class="detail-row rel-link" style="padding:2px 0; cursor:pointer;" data-node="' + (tgtId || '') + '">' +
                '<span class="detail-value" style="font-size:12px;"><span style="color:#888;">' + esc(l.label || '') + '</span> &rarr; <strong style="color:#2ecc71;">' + esc(tgt ? tgt.name : "?") + '</strong></span></div>';
        }});
    }}

    if (!incoming.length && !outgoing.length) {{
        html += '<div class="detail-row"><span class="detail-value" style="color:#666; font-size:12px;">No connections</span></div>';
    }}

    const panel = document.getElementById("detail-panel");
    panel.classList.remove("collapsed");
    const details = document.getElementById("node-details");
    details.innerHTML = html;

    // Make relationship names clickable — select that node
    details.querySelectorAll(".rel-link").forEach(el => {{
        el.addEventListener("click", () => {{
            const nid = el.dataset.node;
            const target = graphData.nodes.find(n => n.id === nid);
            if (target) showDetails(target);
        }});
        el.addEventListener("mouseenter", () => el.style.background = "#0f3460");
        el.addEventListener("mouseleave", () => el.style.background = "");
    }});

    highlightConnections(d);
}}

function highlightConnections(d) {{
    const connIds = new Set([d.id]);
    graphData.links.forEach(l => {{
        const sid = typeof l.source === "object" ? l.source.id : l.source;
        const tid = typeof l.target === "object" ? l.target.id : l.target;
        if (sid === d.id) connIds.add(tid);
        if (tid === d.id) connIds.add(sid);
    }});
    node.transition().duration(200)
        .attr("opacity", n => connIds.has(n.id) ? 1 : 0.12);
    label.transition().duration(200)
        .attr("opacity", n => connIds.has(n.id) ? 1 : 0.12);
    link.transition().duration(200)
        .attr("stroke", l => {{
            const sid = typeof l.source === "object" ? l.source.id : l.source;
            const tid = typeof l.target === "object" ? l.target.id : l.target;
            if (sid === d.id) return "#2ecc71";  // outgoing = green
            if (tid === d.id) return "#3498db";  // incoming = blue
            return "#666";
        }})
        .attr("stroke-opacity", l => {{
            const sid = typeof l.source === "object" ? l.source.id : l.source;
            const tid = typeof l.target === "object" ? l.target.id : l.target;
            return (sid === d.id || tid === d.id) ? 0.9 : 0.04;
        }})
        .attr("stroke-width", l => {{
            const sid = typeof l.source === "object" ? l.source.id : l.source;
            const tid = typeof l.target === "object" ? l.target.id : l.target;
            return (sid === d.id || tid === d.id) ? 2.5 : 1.5;
        }})
        .attr("marker-end", l => {{
            const sid = typeof l.source === "object" ? l.source.id : l.source;
            const tid = typeof l.target === "object" ? l.target.id : l.target;
            if (sid === d.id) return "url(#arrow-out)";
            if (tid === d.id) return "url(#arrow-in)";
            return "url(#arrowhead)";
        }});
    linkLabel.transition().duration(200)
        .attr("opacity", l => {{
            const sid = typeof l.source === "object" ? l.source.id : l.source;
            const tid = typeof l.target === "object" ? l.target.id : l.target;
            return (sid === d.id || tid === d.id) ? 1 : 0.04;
        }})
        .attr("fill", l => {{
            const sid = typeof l.source === "object" ? l.source.id : l.source;
            const tid = typeof l.target === "object" ? l.target.id : l.target;
            if (sid === d.id) return "#2ecc71";
            if (tid === d.id) return "#3498db";
            return "#888";
        }});
}}

function clearHighlight() {{
    node.transition().duration(200).attr("opacity", 1);
    label.transition().duration(200).attr("opacity", 1);
    link.transition().duration(200).attr("stroke", "#666").attr("stroke-opacity", 0.5).attr("stroke-width", 1.5).attr("marker-end", "url(#arrowhead)");
    linkLabel.transition().duration(200).attr("opacity", 1).attr("fill", "#888");
}}
</script>
{floating_nav_html("callhandler_map.html")}
</body>
</html>'''


def _format_minutes(mins):
    """Convert minutes-from-midnight to HH:MM AM/PM."""
    try:
        m = int(mins)
    except (ValueError, TypeError):
        return str(mins)
    h, mm = divmod(m, 60)
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d} {ampm}"


_DAY_FLAGS = [
    ("IsActiveMonday", "Mon"), ("IsActiveTuesday", "Tue"), ("IsActiveWednesday", "Wed"),
    ("IsActiveThursday", "Thu"), ("IsActiveFriday", "Fri"),
    ("IsActiveSaturday", "Sat"), ("IsActiveSunday", "Sun"),
]


def _active_days(detail):
    """Return a compact day string from ScheduleDetail boolean flags."""
    days = [abbr for flag, abbr in _DAY_FLAGS
            if str(detail.get(flag, "false")).lower() == "true"]
    if not days:
        return ""
    if len(days) == 7:
        return "Every day"
    if days == ["Mon", "Tue", "Wed", "Thu", "Fri"]:
        return "Mon \u2013 Fri"
    if len(days) > 2:
        # Check for contiguous range
        all_abbrs = [abbr for _, abbr in _DAY_FLAGS]
        indices = [all_abbrs.index(d) for d in days]
        if indices == list(range(indices[0], indices[0] + len(indices))):
            return f"{days[0]} \u2013 {days[-1]}"
    return ", ".join(days)


def generate_table_html(nodes, edges, holiday_schedules, schedules, site_name="", host=""):
    title_prefix = f"{site_name} — " if site_name else ""
    report_data = json.dumps({
        "nodes": nodes,
        "edges": edges,
        "host": host,
        "siteName": site_name,
    })

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_prefix}Call Handler Report</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='12' fill='%231a1a2e'/><path d='M16 20a4 4 0 014-4h8a4 4 0 014 4v24a4 4 0 01-4 4h-8a4 4 0 01-4-4z' fill='%23e94560'/><circle cx='24' cy='42' r='2' fill='%231a1a2e'/><path d='M36 28h10m0 0l-4-4m4 4l-4 4' stroke='%232ecc71' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/><path d='M36 38h10m0 0l-4-4m4 4l-4 4' stroke='%233498db' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a2e; color: #e0e0e0; padding: 24px; }}
h1 {{ color: #e94560; margin-bottom: 8px; }}
h2 {{ color: #e94560; margin: 32px 0 12px 0; font-size: 20px; border-bottom: 1px solid #0f3460; padding-bottom: 8px; }}
.summary {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0; }}
.summary-badge {{ padding: 6px 14px; border-radius: 4px; font-size: 13px; font-weight: 600; color: #fff; }}
.stats {{ color: #888; font-size: 14px; margin-bottom: 16px; }}
.toc {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin: 16px 0; padding: 12px 16px; background: #16213e; border: 1px solid #0f3460; border-radius: 6px; }}
.toc-label {{ font-size: 13px; color: #888; font-weight: 600; }}
.toc a {{ color: #1abc9c; font-size: 13px; text-decoration: none; padding: 4px 8px; border-radius: 3px; transition: background 0.2s; }}
.toc a:hover {{ background: #0f3460; text-decoration: underline; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; font-size: 13px; }}
th {{ background: #16213e; color: #e94560; text-align: left; padding: 10px 12px; position: sticky; top: 0; border-bottom: 2px solid #0f3460; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #0f3460; vertical-align: top; }}
tr:hover {{ background: #16213e; }}
.muted {{ color: #555; }}
.oid {{ font-family: monospace; font-size: 11px; color: #666; }}
.filter-bar {{ margin: 12px 0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
.filter-bar input {{ padding: 8px 12px; border: 1px solid #0f3460; background: #16213e; color: #e0e0e0; border-radius: 4px; font-size: 13px; width: 300px; }}
.filter-bar select {{ padding: 8px 12px; border: 1px solid #0f3460; background: #16213e; color: #e0e0e0; border-radius: 4px; font-size: 13px; }}
.audio-link {{ color: #1abc9c; text-decoration: none; font-size: 12px; }}
.audio-link:hover {{ text-decoration: underline; }}
.back-to-top {{ position: fixed; bottom: 16px; left: 16px; padding: 8px 14px; background: #0f3460; border: 1px solid #0f3460; color: #e0e0e0; cursor: pointer; border-radius: 4px; font-size: 12px; z-index: 100; text-decoration: none; }}
.back-to-top:hover {{ background: #1a1a4e; border-color: #e94560; }}
.debug-toggle {{ position: fixed; bottom: 16px; right: 16px; padding: 8px 14px; background: #0f3460; border: 1px solid #e94560; color: #e94560; cursor: pointer; border-radius: 4px; font-size: 12px; z-index: 100; }}
.debug-toggle:hover {{ background: #1a1a4e; }}
#debugPanel {{ display: none; background: #0d1b2a; border: 1px solid #0f3460; border-radius: 8px; padding: 20px; margin-top: 32px; }}
#debugPanel h2 {{ color: #e94560; margin-bottom: 12px; }}
.debug-bar {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }}
.debug-bar input {{ flex: 1; min-width: 200px; padding: 8px 12px; border: 1px solid #0f3460; background: #16213e; color: #e0e0e0; border-radius: 4px; font-size: 13px; }}
.debug-btn {{ padding: 8px 14px; border: 1px solid #0f3460; background: #16213e; color: #e0e0e0; cursor: pointer; border-radius: 4px; font-size: 12px; white-space: nowrap; }}
.debug-btn:hover {{ background: #0f3460; }}
#debugOutput {{ background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px; padding: 12px; font-family: monospace; font-size: 12px; white-space: pre-wrap; max-height: 500px; overflow-y: auto; color: #aaa; }}
.schedule-bar {{ display: flex; gap: 4px; margin: 16px 0; }}
.schedule-btn {{ padding: 8px 16px; border: 2px solid #0f3460; background: #16213e; color: #e0e0e0; cursor: pointer; border-radius: 4px; font-size: 13px; font-weight: 600; transition: all 0.2s; }}
.schedule-btn:hover {{ border-color: #e94560; }}
.schedule-btn.active {{ background: #0f3460; border-color: #e94560; color: #fff; }}
.schedule-label {{ font-size: 13px; color: #888; align-self: center; margin-right: 8px; }}
.diff-highlight {{ background: #2a1a3e; }}
.flow-tree {{ background: #16213e; border: 1px solid #0f3460; border-radius: 6px; padding: 16px; margin-bottom: 16px; font-family: monospace; font-size: 13px; line-height: 1.6; white-space: pre; overflow-x: auto; }}
.flow-tree .flow-root {{ color: #2ecc71; font-weight: 700; }}
.flow-tree .flow-handler {{ color: #3498db; }}
.flow-tree .flow-label {{ color: #e94560; }}
.flow-tree .flow-muted {{ color: #555; }}
.flow-tree .flow-visited {{ color: #555; font-style: italic; }}
.sched-tag {{ display: inline-block; padding: 1px 6px; border-radius: 8px; font-size: 10px; font-weight: 600; margin-left: 4px; }}
.sched-tag.offhours {{ background: #5b3a1e; color: #f39c12; }}
.sched-tag.holiday {{ background: #4a1a2e; color: #e74c3c; }}
.sched-tag.standard {{ background: #1a3a2e; color: #2ecc71; }}
.sched-tag.alternate {{ background: #2a1a4e; color: #9b59b6; }}
.section-header {{ display: flex; align-items: center; gap: 12px; }}
.section-header h2 {{ margin: 0; }}
.copy-btn {{ padding: 4px 10px; border: 1px solid #0f3460; background: #16213e; color: #888; cursor: pointer; border-radius: 3px; font-size: 11px; transition: all 0.2s; }}
.copy-btn:hover {{ color: #e0e0e0; border-color: #e94560; }}
</style>
</head>
<body>
<h1>{title_prefix}Handlers &amp; Routing</h1>
<div id="stats" class="stats"></div>
<div id="summary" class="summary"></div>

<nav class="toc">
<span class="toc-label">Jump to:</span>
<a href="#schedule-view">Schedule View</a>
<a href="#flow-trees">Call Flow Trees</a>
<a href="#handlers">Handlers &amp; Routing</a>
</nav>

<h2 id="schedule-view">Call Flow Schedule View</h2>
<div class="schedule-bar">
<span class="schedule-label">Active schedule:</span>
<button class="schedule-btn active" data-schedule="standard" onclick="setSchedule('standard')">Standard</button>
<button class="schedule-btn" data-schedule="offhours" onclick="setSchedule('offhours')">Off Hours</button>
<button class="schedule-btn" data-schedule="holiday" onclick="setSchedule('holiday')">Holiday</button>
<button class="schedule-btn" data-schedule="all" onclick="setSchedule('all')">All (raw)</button>
</div>

<div class="section-header"><h2 id="flow-trees">Call Flow Trees</h2><button class="copy-btn" onclick="copyFlowTrees(this)">Copy</button></div>
<div id="callFlowTrees"></div>

<div class="section-header"><h2 id="handlers">Call Handlers &amp; Routing</h2><button class="copy-btn" onclick="copyHandlerTable(this)">Copy as Markdown</button></div>
<div class="filter-bar">
<input type="text" id="search" placeholder="Filter by name, extension, or type..." oninput="renderTable()">
<select id="classFilter" onchange="renderTable()">
<option value="">All Classifications</option>
<option value="root">Root (Entry Point)</option>
<option value="normal">Normal</option>
<option value="deadend">Dead End</option>
<option value="unreachable">Unreachable Subtree</option>
<option value="orphan">True Orphan</option>
</select>
</div>
<table id="handlerTable">
<thead>
<tr><th>Name</th><th>Extension</th><th>Type</th><th>Classification</th><th>Schedule / Conditions</th><th>Incoming</th><th>Outgoing</th><th>Audio</th><th>Object ID</th></tr>
</thead>
<tbody></tbody>
</table>

<script>
const data = {report_data};
let activeSchedule = "standard";

const classColors = {{
    root: "#2ecc71", normal: "#3498db", orphan: "#95a5a6",
    unreachable: "#e67e22", deadend: "#e74c3c"
}};
const typeColors = {{ interview: "#9b59b6", phone: "#1abc9c", routingrule: "#2ecc71", directory: "#f39c12", action: "#e74c3c" }};
const classLabels = {{
    root: "Root (Entry Point)", normal: "Normal", orphan: "True Orphan",
    unreachable: "Unreachable Subtree", deadend: "Dead End"
}};

const nodeMap = {{}};
data.nodes.forEach(n => nodeMap[n.id] = n);

function nodeColor(n) {{
    if (n.primary) return "#ffd700";
    return typeColors[n.type] || classColors[n.classification] || "#3498db";
}}

function esc(s) {{
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
}}

function adminUrl(node) {{
    if (!data.host || !node || !node.id) return "";
    const ops = {{
        callhandler: "callhandler.do?op=read",
        interview: "interviewhandler.do?op=read",
        directory: "directoryhandler.do?op=read",
        routingrule: "routingrule.do?op=read",
    }};
    const op = ops[node.type];
    return op ? data.host + "/cuadmin/" + op + "&objectId=" + node.id : "";
}}

function greetingUrl(handlerId, greetingType) {{
    if (!data.host) return "";
    return data.host + "/cuadmin/greeting.do?op=readCallhandler&objectId=" + handlerId + "&greetingType=" + encodeURIComponent(greetingType);
}}

function edgeMatchesSchedule(e) {{
    if (activeSchedule === "all") return true;
    return e.schedule === "always" || e.schedule === activeSchedule;
}}

function audioMatchesSchedule(a) {{
    if (activeSchedule === "all") return true;
    return a.schedule === "always" || a.schedule === activeSchedule;
}}

const schedLabels = {{ standard: "Standard", offhours: "Off Hours", holiday: "Holiday", alternate: "Alternate" }};
function schedTag(sched) {{
    if (!sched || sched === "always") return "";
    if (activeSchedule !== "all" && sched === activeSchedule) return "";
    return '<span class="sched-tag ' + sched + '">' + (schedLabels[sched] || sched) + '</span>';
}}

function setSchedule(mode) {{
    activeSchedule = mode;
    document.querySelectorAll(".schedule-btn").forEach(btn => {{
        btn.classList.toggle("active", btn.dataset.schedule === mode);
    }});
    renderTable();
}}

function renderTable() {{
    const search = (document.getElementById("search").value || "").toLowerCase();
    const clsFilter = document.getElementById("classFilter").value;

    // Filter edges by schedule
    const activeEdges = data.edges.filter(edgeMatchesSchedule);
    const outgoing = {{}};
    const incoming = {{}};
    activeEdges.forEach(e => {{
        (outgoing[e.source] = outgoing[e.source] || []).push(e);
        (incoming[e.target] = incoming[e.target] || []).push(e);
    }});

    // Sort nodes
    const typeOrder = {{ callhandler: 0, directory: 1, interview: 2, routingrule: 3, phone: 4 }};
    const classOrder = {{ root: 0, normal: 1, deadend: 2, unreachable: 3, orphan: 4 }};
    const sorted = [...data.nodes].sort((a, b) =>
        (typeOrder[a.type] ?? 9) - (typeOrder[b.type] ?? 9) ||
        (classOrder[a.classification] ?? 9) - (classOrder[b.classification] ?? 9) ||
        a.name.toLowerCase().localeCompare(b.name.toLowerCase())
    );

    const tbody = document.querySelector("#handlerTable tbody");
    tbody.innerHTML = "";

    sorted.forEach(n => {{
        const color = nodeColor(n);
        const clsLabel = n.primary ? "Primary Root" : (classLabels[n.classification] || n.classification);

        // Text filter
        const text = (n.name + " " + n.extension + " " + n.type + " " + clsLabel).toLowerCase();
        if (search && !text.includes(search)) return;
        if (clsFilter && n.classification !== clsFilter) return;

        const outLinks = outgoing[n.id] || [];
        const inLinks = incoming[n.id] || [];

        const outHtml = outLinks.length
            ? outLinks.map(e => esc(e.label) + " &rarr; " + esc((nodeMap[e.target] || {{}}).name || "?")).join("<br>")
            : '<span class="muted">None</span>';
        const inHtml = inLinks.length
            ? inLinks.map(e => esc((nodeMap[e.source] || {{}}).name || "?") + " &rarr; " + esc(e.label)).join("<br>")
            : '<span class="muted">None</span>';

        const audioList = n.audio || [];
        const audioHtml = audioList.length
            ? audioList.map(a => '<span class="audio-link">' + esc(a.greeting) + '</span>' + schedTag(a.schedule) +
                '<br><audio controls preload="none" style="width:180px; height:24px;"><source src="' + esc(a.url) + '" type="audio/wav"></audio>').join("<br>")
            : '<span class="muted">&mdash;</span>';

        // Schedule name for handlers, conditions for routing rules
        let schedCondHtml = '<span class="muted">&mdash;</span>';
        if (n.type === "routingrule") {{
            const conds = n.conditions || [];
            const stateTag = n.ruleState !== "Active" ? ' <span class="muted">(' + esc(n.ruleState) + ')</span>' : "";
            if (conds.length) {{
                schedCondHtml = conds.map(c => esc(c.param) + " " + esc(c.op) + " " + esc(c.value)).join("<br>") + stateTag;
            }} else {{
                schedCondHtml = '<span class="muted">No conditions (matches all)</span>' + stateTag;
            }}
        }} else if (n.scheduleName) {{
            schedCondHtml = esc(n.scheduleName);
        }}

        const aUrl = adminUrl(n);
        const unityLink = aUrl ? ' <a href="' + esc(aUrl) + '" target="_blank" title="View on ' + esc(data.siteName || 'Unity') + ' Unity" style="color:#1abc9c; text-decoration:none; font-size:11px;">&#9741;</a>' : '';
        const tr = document.createElement("tr");
        tr.innerHTML =
            '<td style="color:' + color + '; font-weight:600">' + esc(n.name) + unityLink + (n.system ? ' <span class="muted">(system)</span>' : "") + (n.postGreeting ? ' <span style="color:#e67e22">&#9654; post-greeting</span>' : "") + '</td>' +
            '<td>' + esc(n.extension) + '</td>' +
            '<td>' + esc(n.type) + '</td>' +
            '<td style="color:' + color + '">' + esc(clsLabel) + '</td>' +
            '<td>' + schedCondHtml + '</td>' +
            '<td>' + inHtml + '</td>' +
            '<td>' + outHtml + '</td>' +
            '<td>' + audioHtml + '</td>' +
            '<td class="oid">' + esc(n.id) + '</td>';
        tbody.appendChild(tr);
    }});

    // Update stats
    const counts = {{}};
    data.nodes.forEach(n => counts[n.classification] = (counts[n.classification] || 0) + 1);
    document.getElementById("stats").innerHTML =
        data.nodes.length + " nodes &middot; " + activeEdges.length + " active connections &middot; " +
        data.edges.length + " total connections";
    document.getElementById("summary").innerHTML =
        ["root","normal","deadend","unreachable","orphan"]
            .filter(c => counts[c])
            .map(c => '<span class="summary-badge" style="background:' + classColors[c] + '">' + classLabels[c] + ': ' + counts[c] + '</span>')
            .join("");

    renderCallFlowTrees(activeEdges);
}}

function renderCallFlowTrees(activeEdges) {{
    const container = document.getElementById("callFlowTrees");
    // Build adjacency: source -> [{{label, target, schedule}}]
    const adj = {{}};
    activeEdges.forEach(e => {{
        (adj[e.source] = adj[e.source] || []).push({{ label: e.label, target: e.target, schedule: e.schedule }});
    }});
    // Sort edges: Key entries first (by key), then After:, then Xfer:
    function edgeSortKey(e) {{
        if (e.label.startsWith("Key ")) return "0_" + e.label;
        if (e.label.startsWith("After:")) return "1_" + e.label;
        return "2_" + e.label;
    }}
    Object.values(adj).forEach(edges => edges.sort((a, b) => edgeSortKey(a).localeCompare(edgeSortKey(b))));

    // Find routing rules that connect to call handlers
    const roots = data.nodes.filter(n => n.type === "routingrule" && (adj[n.id] || []).length > 0);
    // Sort: routing rules targeting the primary root come first
    const primaryId = (data.nodes.find(n => n.primary) || {{}}).id;
    roots.sort((a, b) => {{
        const aTarget = (adj[a.id] || [])[0];
        const bTarget = (adj[b.id] || [])[0];
        const aHitsPrimary = aTarget && aTarget.target === primaryId ? 0 : 1;
        const bHitsPrimary = bTarget && bTarget.target === primaryId ? 0 : 1;
        return aHitsPrimary - bHitsPrimary;
    }});
    if (!roots.length) {{
        container.innerHTML = '<p class="muted">No routing rules with connections found.</p>';
        return;
    }}

    let html = "";
    roots.forEach(root => {{
        const target = (adj[root.id] || [])[0];
        if (!target) return;
        const targetNode = nodeMap[target.target];
        if (!targetNode) return;

        function audioLinks(node, indent) {{
            if (!node || !node.audio) return [];
            if (!node.audio.length) return [];
            const prefix = "  ".repeat(indent);
            return node.audio.map(a => prefix + '<span class="audio-link">&#9835; ' + esc(a.greeting) + ' greeting</span>' + schedTag(a.schedule) +
                '<br>' + prefix + '<audio controls preload="none" style="width:180px; height:24px;"><source src="' + esc(a.url) + '" type="audio/wav"></audio>');
        }}

        let lines = [];
        // Show conditions on the root line
        const conds = root.conditions || [];
        const condStr = conds.length
            ? ' <span class="flow-muted">[' + conds.map(c => esc(c.param) + " " + esc(c.op) + " " + esc(c.value)).join(", ") + ']</span>'
            : "";
        lines.push('<span class="flow-root">' + esc(root.name) + '</span>' + condStr + ' -> <span class="flow-handler">' + esc(targetNode.name) + (targetNode.extension ? " (" + esc(targetNode.extension) + ")" : "") + '</span>' + (targetNode.scheduleName ? ' <span class="flow-muted">[' + esc(targetNode.scheduleName) + ']</span>' : ""));
        lines.push(...audioLinks(targetNode, 1));

        // BFS tree with depth tracking
        const visited = new Set([root.id]);
        function walk(nodeId, indent) {{
            if (!adj[nodeId]) return;
            adj[nodeId].forEach(edge => {{
                const tgt = nodeMap[edge.target];
                const name = tgt ? tgt.name : "?";
                const ext = tgt && tgt.extension ? " (" + esc(tgt.extension) + ")" : "";
                const prefix = "  ".repeat(indent);
                if (visited.has(edge.target)) {{
                    lines.push(prefix + '<span class="flow-label">[' + esc(edge.label) + ']</span> -> <span class="flow-visited">' + esc(name) + ext + ' (see above)</span>' + schedTag(edge.schedule));
                    return;
                }}
                visited.add(edge.target);
                lines.push(prefix + '<span class="flow-label">[' + esc(edge.label) + ']</span> -> <span class="flow-handler">' + esc(name) + ext + '</span>' + schedTag(edge.schedule));
                lines.push(...audioLinks(tgt, indent + 1));
                walk(edge.target, indent + 1);
            }});
        }}
        visited.add(target.target);
        walk(target.target, 1);

        html += '<div class="flow-tree">' + lines.join("\\n") + '</div>';
    }});

    container.innerHTML = html;
}}

// Initial render
renderTable();

// --- Copy helpers ---
function flashBtn(btn, msg) {{
    const orig = btn.textContent;
    btn.textContent = msg || "Copied!";
    setTimeout(() => btn.textContent = orig, 1500);
}}

function copyFlowTrees(btn) {{
    const el = document.getElementById("callFlowTrees");
    const text = el.innerText;
    navigator.clipboard.writeText(text).then(() => flashBtn(btn));
}}

function copyHandlerTable(btn) {{
    const activeEdges = data.edges.filter(edgeMatchesSchedule);
    const outgoing = {{}};
    const incoming = {{}};
    activeEdges.forEach(e => {{
        (outgoing[e.source] = outgoing[e.source] || []).push(e);
        (incoming[e.target] = incoming[e.target] || []).push(e);
    }});
    const headers = ["Name", "Extension", "Type", "Classification", "Schedule / Conditions", "Incoming", "Outgoing", "Object ID"];
    const rows = [headers.join(" | "), headers.map(() => "---").join(" | ")];
    const typeOrder = {{ callhandler: 0, directory: 1, interview: 2, routingrule: 3, phone: 4 }};
    const classOrder = {{ root: 0, normal: 1, deadend: 2, unreachable: 3, orphan: 4 }};
    const sorted = [...data.nodes].sort((a, b) =>
        (typeOrder[a.type] ?? 9) - (typeOrder[b.type] ?? 9) ||
        (classOrder[a.classification] ?? 9) - (classOrder[b.classification] ?? 9) ||
        a.name.toLowerCase().localeCompare(b.name.toLowerCase())
    );
    sorted.forEach(n => {{
        const clsLabel = n.primary ? "Primary Root" : (classLabels[n.classification] || n.classification);
        const outLinks = (outgoing[n.id] || []).map(e => e.label + " -> " + ((nodeMap[e.target] || {{}}).name || "?")).join("; ");
        const inLinks = (incoming[n.id] || []).map(e => ((nodeMap[e.source] || {{}}).name || "?") + " -> " + e.label).join("; ");
        let schedCond = "";
        if (n.type === "routingrule") {{
            schedCond = (n.conditions || []).map(c => c.param + " " + c.op + " " + c.value).join(", ") || "No conditions";
        }} else {{
            schedCond = n.scheduleName || "";
        }}
        rows.push([n.name, n.extension || "", n.type, clsLabel, schedCond, inLinks || "None", outLinks || "None", n.id].join(" | "));
    }});
    navigator.clipboard.writeText(rows.join("\\n")).then(() => flashBtn(btn));
}}

function copyTableAsMd(tableId, btn) {{
    const table = document.getElementById(tableId);
    const headerCells = Array.from(table.querySelectorAll("thead th"));
    const headers = headerCells.map(th => th.textContent);
    const lines = [headers.join(" | "), headers.map(() => "---").join(" | ")];
    table.querySelectorAll("tbody tr").forEach(tr => {{
        const cells = Array.from(tr.querySelectorAll("td")).map(td => td.textContent.trim());
        lines.push(cells.join(" | "));
    }});
    navigator.clipboard.writeText(lines.join("\\n")).then(() => flashBtn(btn));
}}

// --- Debug Tools ---
function toggleDebug() {{
    const panel = document.getElementById("debugPanel");
    panel.style.display = panel.style.display === "block" ? "none" : "block";
}}

function debugLookup() {{
    const q = document.getElementById("debugQuery").value.trim().toLowerCase();
    const out = document.getElementById("debugOutput");
    if (!q) {{ out.textContent = "Enter a name, extension, or Object ID to search."; return; }}

    const matchedNodes = data.nodes.filter(n =>
        n.name.toLowerCase().includes(q) ||
        (n.extension && n.extension.toLowerCase().includes(q)) ||
        n.id.toLowerCase().includes(q)
    );

    if (!matchedNodes.length) {{ out.textContent = "No matching nodes found."; return; }}

    const results = matchedNodes.map(n => {{
        const outEdges = data.edges.filter(e => e.source === n.id);
        const inEdges = data.edges.filter(e => e.target === n.id);
        return {{
            node: n,
            outgoing: outEdges.map(e => ({{
                label: e.label,
                schedule: e.schedule,
                target_id: e.target,
                target_name: (nodeMap[e.target] || {{}}).name || "?"
            }})),
            incoming: inEdges.map(e => ({{
                label: e.label,
                schedule: e.schedule,
                source_id: e.source,
                source_name: (nodeMap[e.source] || {{}}).name || "?"
            }}))
        }};
    }});
    out.textContent = JSON.stringify(results, null, 2);
}}

function debugDumpAll() {{
    const out = document.getElementById("debugOutput");
    out.textContent = JSON.stringify(data, null, 2);
}}

function debugOrphans() {{
    const out = document.getElementById("debugOutput");
    const report = {{}};

    // True orphans: zero connections
    report.trueOrphans = data.nodes
        .filter(n => n.classification === "orphan")
        .map(n => ({{ name: n.name, extension: n.extension, id: n.id }}));

    // Unreachable: have edges but no path from any routing rule
    report.unreachable = data.nodes
        .filter(n => n.classification === "unreachable")
        .map(n => {{
            const outEdges = data.edges.filter(e => e.source === n.id);
            const inEdges = data.edges.filter(e => e.target === n.id);
            return {{
                name: n.name, extension: n.extension, id: n.id,
                connectsTo: outEdges.map(e => (nodeMap[e.target] || {{}}).name || e.target),
                connectedFrom: inEdges.map(e => (nodeMap[e.source] || {{}}).name || e.source)
            }};
        }});

    // Dead ends: reachable but callers get stuck
    report.deadEnds = data.nodes
        .filter(n => n.classification === "deadend")
        .map(n => {{
            const inEdges = data.edges.filter(e => e.target === n.id);
            return {{
                name: n.name, extension: n.extension, id: n.id,
                reachedVia: inEdges.map(e => (nodeMap[e.source] || {{}}).name + " (" + e.label + ")")
            }};
        }});

    // Schedule gaps: reachable in some schedules but not others
    report.scheduleGaps = data.nodes
        .filter(n => n.reachable && n.type === "callhandler" && n.classification !== "orphan" &&
            !(n.reachable.standard && n.reachable.offhours && n.reachable.holiday))
        .map(n => ({{
            name: n.name, extension: n.extension, id: n.id,
            reachableDuring: {{
                standard: n.reachable.standard,
                offhours: n.reachable.offhours,
                holiday: n.reachable.holiday
            }}
        }}));

    // Per-schedule edge counts
    const scheduleCounts = {{}};
    data.edges.forEach(e => scheduleCounts[e.schedule] = (scheduleCounts[e.schedule] || 0) + 1);
    report.edgesBySchedule = scheduleCounts;

    report.summary = {{
        totalHandlers: data.nodes.filter(n => n.type === "callhandler").length,
        trueOrphans: report.trueOrphans.length,
        unreachable: report.unreachable.length,
        deadEnds: report.deadEnds.length,
        scheduleGaps: report.scheduleGaps.length
    }};

    out.textContent = JSON.stringify(report, null, 2);
}}
function copyDebugOutput() {{
    const text = document.getElementById("debugOutput").textContent;
    navigator.clipboard.writeText(text).then(() => {{
        const btn = document.getElementById("copyBtn");
        btn.textContent = "Copied!";
        setTimeout(() => btn.textContent = "Copy Output", 1500);
    }});
}}
</script>

<a href="#" class="back-to-top">&uarr; Top</a>
<button class="debug-toggle" onclick="toggleDebug()">Debug Tools</button>
<div id="debugPanel">
<h2>Debug Tools</h2>
<div class="debug-bar">
<input type="text" id="debugQuery" placeholder="Search by name, extension, or Object ID..." onkeydown="if(event.key==='Enter')debugLookup()">
<button class="debug-btn" onclick="debugLookup()">Lookup Node</button>
<button class="debug-btn" onclick="debugOrphans()">Find Problems</button>
<button class="debug-btn" onclick="debugDumpAll()">Dump All Data</button>
<button class="debug-btn" onclick="copyDebugOutput()" id="copyBtn">Copy Output</button>
</div>
<pre id="debugOutput">Use the tools above to inspect raw data.

&bull; Lookup Node &mdash; search for a handler by name, extension, or ID to see its full data, all connections, and schedule tags
&bull; Find Problems &mdash; list dead ends, orphans, unreachable nodes, and edge counts per schedule
&bull; Dump All Data &mdash; export the complete JSON dataset (nodes, edges, schedules, holidays)</pre>
</div>
{floating_nav_html("callhandler_report.html")}
</body>
</html>'''


def generate_callflow_html(nodes, edges, site_name="", host=""):
    title_prefix = f"{site_name} — " if site_name else ""
    report_data = json.dumps({"nodes": nodes, "edges": edges, "host": host, "siteName": site_name})
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_prefix}Call Flow Explorer</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='12' fill='%231a1a2e'/><path d='M16 20a4 4 0 014-4h8a4 4 0 014 4v24a4 4 0 01-4 4h-8a4 4 0 01-4-4z' fill='%23e94560'/><circle cx='24' cy='42' r='2' fill='%231a1a2e'/><path d='M36 28h10m0 0l-4-4m4 4l-4 4' stroke='%232ecc71' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/><path d='M36 38h10m0 0l-4-4m4 4l-4 4' stroke='%233498db' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a2e; color: #e0e0e0; }}
.topbar {{ background: #0d1b2a; border-bottom: 1px solid #0f3460; padding: 16px 24px; display: flex; justify-content: space-between; align-items: center; }}
.topbar h1 {{ color: #e94560; font-size: 20px; }}
.controls {{ display: flex; gap: 24px; align-items: center; flex-wrap: wrap; padding: 12px 24px; background: #16213e; border-bottom: 1px solid #0f3460; }}
.schedule-bar {{ display: flex; gap: 4px; align-items: center; }}
.schedule-label {{ font-size: 13px; color: #888; margin-right: 8px; }}
.schedule-btn {{ padding: 6px 14px; border: 2px solid #0f3460; background: #16213e; color: #e0e0e0; cursor: pointer; border-radius: 4px; font-size: 12px; font-weight: 600; transition: all 0.2s; }}
.schedule-btn:hover {{ border-color: #e94560; }}
.schedule-btn.active {{ background: #0f3460; border-color: #e94560; color: #fff; }}
.entry-select {{ display: flex; align-items: center; gap: 8px; }}
.entry-select label {{ font-size: 13px; color: #888; }}
.entry-select select {{ padding: 6px 12px; border: 1px solid #0f3460; background: #0d1b2a; color: #e0e0e0; border-radius: 4px; font-size: 13px; max-width: 420px; }}
.breadcrumb {{ position: sticky; top: 0; z-index: 10; display: flex; align-items: center; gap: 0; padding: 10px 24px; background: #0d1b2a; border-bottom: 1px solid #0f3460; flex-wrap: wrap; min-height: 40px; }}
.copy-link-btn {{ margin-left: auto; padding: 4px 10px; border: 1px solid #0f3460; background: #16213e; color: #1abc9c; cursor: pointer; border-radius: 4px; font-size: 11px; white-space: nowrap; transition: all 0.2s; }}
.copy-link-btn:hover {{ background: #0f3460; border-color: #1abc9c; }}
.bc-step {{ padding: 4px 10px; border-radius: 4px; font-size: 13px; color: #3498db; cursor: pointer; white-space: nowrap; }}
.bc-step:hover {{ background: #16213e; }}
.bc-current {{ color: #e94560; font-weight: 700; }}
.bc-sep {{ color: #555; font-size: 13px; padding: 0 2px; }}
.bc-label {{ color: #e94560; font-family: monospace; font-size: 12px; padding: 4px 8px; }}
.flow-container {{ padding: 24px; max-width: 600px; margin: 0 auto; }}
.flow-card {{ background: #16213e; border: 2px solid #0f3460; border-radius: 8px; overflow: hidden; }}
.flow-card.entry-point {{ border-color: #2ecc71; }}
.flow-card.handler {{ border-color: #3498db; }}
.flow-card.primary {{ border-color: #ffd700; box-shadow: 0 0 12px rgba(255,215,0,0.15); }}
.flow-card.dead-end {{ border-color: #e74c3c; }}
.flow-card.expanded {{ border-color: #e94560; box-shadow: 0 0 10px rgba(233,69,96,0.2); }}
.card-header {{ padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #0f3460; }}
.card-name {{ font-weight: 700; font-size: 15px; color: #e0e0e0; }}
.card-ext {{ color: #888; font-size: 13px; margin-left: 8px; }}
.card-badges {{ display: flex; gap: 6px; align-items: center; }}
.schedule-pill {{ padding: 2px 8px; border-radius: 10px; font-size: 11px; background: #0f3460; color: #1abc9c; }}
.type-pill {{ padding: 2px 8px; border-radius: 10px; font-size: 11px; background: #0f3460; }}
.type-pill.routingrule {{ color: #2ecc71; }}
.type-pill.directory {{ color: #f39c12; }}
.type-pill.interview {{ color: #9b59b6; }}
.audio-row {{ display: flex; align-items: center; gap: 8px; padding: 8px 16px; background: #12192e; border-bottom: 1px solid #0a1628; }}
.audio-badge {{ color: #1abc9c; font-size: 12px; text-decoration: none; }}
.audio-badge:hover {{ text-decoration: underline; }}
.cond-row {{ padding: 8px 16px; font-size: 12px; color: #888; background: #12192e; border-bottom: 1px solid #0a1628; }}
.menu-row {{ display: flex; align-items: center; padding: 8px 16px; border-bottom: 1px solid #0a1628; cursor: default; transition: background 0.15s; }}
.menu-row.clickable {{ cursor: pointer; }}
.menu-row.clickable:hover {{ background: #0f3460; }}
.menu-row.active-row {{ background: #1a1040; border-left: 3px solid #e94560; }}
.menu-key {{ width: 64px; font-weight: 700; color: #e94560; font-family: monospace; font-size: 14px; flex-shrink: 0; }}
.menu-arrow {{ color: #555; margin: 0 8px; }}
.menu-target {{ flex: 1; color: #3498db; font-size: 13px; }}
.menu-target.action {{ color: #e67e22; }}
.menu-target.self-ref {{ color: #888; font-style: italic; }}
.menu-row.after-greeting {{ border-left: 3px solid #9b59b6; background: #12192e; }}
.menu-row.after-greeting .menu-key {{ color: #9b59b6; font-size: 12px; width: auto; font-weight: 600; font-family: inherit; }}
.connector {{ width: 2px; height: 32px; background: #0f3460; margin: 0 auto; position: relative; }}
.connector::after {{ content: ''; position: absolute; bottom: -4px; left: 50%; transform: translateX(-50%); border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 6px solid #0f3460; }}
.connector-label {{ position: absolute; left: 16px; top: 6px; font-size: 11px; color: #e94560; font-weight: 600; white-space: nowrap; font-family: monospace; }}
.empty-msg {{ text-align: center; color: #555; padding: 48px; font-size: 14px; }}
.sched-tag {{ display: inline-block; padding: 1px 6px; border-radius: 8px; font-size: 10px; font-weight: 600; margin-left: 6px; vertical-align: middle; }}
.sched-tag.offhours {{ background: #5b3a1e; color: #f39c12; }}
.sched-tag.holiday {{ background: #4a1a2e; color: #e74c3c; }}
.sched-tag.standard {{ background: #1a3a2e; color: #2ecc71; }}
.sched-tag.alternate {{ background: #2a1a4e; color: #9b59b6; }}
@keyframes flash {{ 0%,100% {{ box-shadow: none; }} 50% {{ box-shadow: 0 0 20px rgba(233,69,96,0.5); }} }}
.flash {{ animation: flash 0.6s ease 2; }}
</style>
</head>
<body>
<div class="topbar">
<h1>{title_prefix}Call Flow Explorer</h1>
</div>
<div class="controls">
<div class="schedule-bar">
<span class="schedule-label">Schedule:</span>
<button class="schedule-btn active" data-schedule="standard" onclick="setSchedule('standard')">Standard</button>
<button class="schedule-btn" data-schedule="offhours" onclick="setSchedule('offhours')">Off Hours</button>
<button class="schedule-btn" data-schedule="holiday" onclick="setSchedule('holiday')">Holiday</button>
<button class="schedule-btn" data-schedule="all" onclick="setSchedule('all')">All</button>
</div>
<div class="entry-select">
<label>Start from:</label>
<select id="entryPoint" onchange="renderFlow()"></select>
</div>
</div>
<div class="breadcrumb" id="breadcrumb"></div>
<div class="flow-container" id="flowContainer">
<div class="empty-msg">Select an entry point above to trace a call flow.</div>
</div>
<script>
const data = {report_data};
let activeSchedule = "standard";
let trailPath = []; // [{{nodeId, edgeLabel}}]

const nodeMap = {{}};
data.nodes.forEach(n => nodeMap[n.id] = n);

function adminUrl(node) {{
    if (!data.host || !node || !node.id) return "";
    const ops = {{
        callhandler: "callhandler.do?op=read",
        interview: "interviewhandler.do?op=read",
        directory: "directoryhandler.do?op=read",
        routingrule: "routingrule.do?op=read",
    }};
    const op = ops[node.type];
    return op ? data.host + "/cuadmin/" + op + "&objectId=" + node.id : "";
}}

function greetingUrl(handlerId, greetingType) {{
    if (!data.host) return "";
    return data.host + "/cuadmin/greeting.do?op=readCallhandler&objectId=" + handlerId + "&greetingType=" + encodeURIComponent(greetingType);
}}

function esc(s) {{
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
}}

function edgeMatch(e) {{
    if (activeSchedule === "all") return true;
    return e.schedule === "always" || e.schedule === activeSchedule;
}}

function getEdges(sourceId) {{
    return data.edges.filter(e => e.source === sourceId && edgeMatch(e));
}}

function isHandlerNode(n) {{
    return n && (n.type === "callhandler" || n.type === "directory" || n.type === "interview");
}}

// Populate entry point dropdown
function populateEntryPoints() {{
    const sel = document.getElementById("entryPoint");
    const prev = sel.value;
    sel.innerHTML = "";
    const rules = data.nodes.filter(n => n.type === "routingrule");
    // Find primary root target to sort it first
    const primaryNode = data.nodes.find(n => n.primary);
    rules.sort((a, b) => {{
        if (primaryNode) {{
            const aEdge = data.edges.find(e => e.source === a.id);
            const bEdge = data.edges.find(e => e.source === b.id);
            const aHits = aEdge && aEdge.target === primaryNode.id ? 0 : 1;
            const bHits = bEdge && bEdge.target === primaryNode.id ? 0 : 1;
            if (aHits !== bHits) return aHits - bHits;
        }}
        return a.name.localeCompare(b.name);
    }});
    // Also add root call handlers that aren't targeted by rules
    const ruleTargets = new Set(rules.flatMap(r => getEdges(r.id).map(e => e.target)));
    const directRoots = data.nodes.filter(n => n.classification === "root" && isHandlerNode(n) && !ruleTargets.has(n.id));
    rules.forEach(r => {{
        const conds = (r.conditions || []).map(c => c.param + " " + c.op + " " + c.value).join(", ");
        const label = r.name + (conds ? " [" + conds + "]" : "") + (r.ruleType ? " (" + r.ruleType + ")" : "");
        const opt = document.createElement("option");
        opt.value = r.id;
        opt.textContent = label;
        sel.appendChild(opt);
    }});
    directRoots.forEach(n => {{
        const opt = document.createElement("option");
        opt.value = n.id;
        opt.textContent = n.name + (n.extension ? " (" + n.extension + ")" : "") + " [direct]";
        sel.appendChild(opt);
    }});
    if (prev && sel.querySelector('option[value="' + prev + '"]')) sel.value = prev;
}}

const schedLabels = {{ standard: "Standard", offhours: "Off Hours", holiday: "Holiday", alternate: "Alternate" }};
function schedTag(sched) {{
    if (!sched || sched === "always") return "";
    if (activeSchedule !== "all" && sched === activeSchedule) return "";
    return '<span class="sched-tag ' + sched + '">' + (schedLabels[sched] || sched) + '</span>';
}}

function setSchedule(mode) {{
    activeSchedule = mode;
    document.querySelectorAll(".schedule-btn").forEach(btn => {{
        btn.classList.toggle("active", btn.dataset.schedule === mode);
    }});
    populateEntryPoints();
    renderFlow();
}}

function renderFlow() {{
    const container = document.getElementById("flowContainer");
    container.innerHTML = "";
    trailPath = [];
    const startId = document.getElementById("entryPoint").value;
    if (!startId) {{ container.innerHTML = '<div class="empty-msg">No entry points found.</div>'; updateBreadcrumb(); return; }}
    const startNode = nodeMap[startId];
    if (!startNode) return;

    // Render entry card
    const entryCard = createCard(startNode, true);
    container.appendChild(entryCard);
    trailPath.push({{ nodeId: startId, label: "Entry" }});

    // If routing rule, follow its target
    if (startNode.type === "routingrule") {{
        const targetEdge = getEdges(startId)[0];
        if (targetEdge && nodeMap[targetEdge.target]) {{
            container.appendChild(createConnector(targetEdge.label));
            const targetCard = createCard(nodeMap[targetEdge.target]);
            container.appendChild(targetCard);
            trailPath.push({{ nodeId: targetEdge.target, label: targetEdge.label }});
        }}
    }}
    updateBreadcrumb();
}}

function createCard(node, isEntry) {{
    const card = document.createElement("div");
    card.className = "flow-card" + (isEntry ? " entry-point" : " handler") + (node.primary ? " primary" : "");
    card.id = "card-" + node.id;

    // Header
    const header = document.createElement("div");
    header.className = "card-header";
    const nameSpan = document.createElement("span");
    nameSpan.innerHTML = '<span class="card-name">' + esc(node.name) + '</span>' +
        (node.extension ? '<span class="card-ext">ext. ' + esc(node.extension) + '</span>' : '');
    header.appendChild(nameSpan);
    const badges = document.createElement("div");
    badges.className = "card-badges";
    if (node.type === "routingrule") {{
        badges.innerHTML = '<span class="type-pill routingrule">' + esc(node.ruleType || "Rule") + '</span>';
    }} else if (node.type === "directory") {{
        badges.innerHTML = '<span class="type-pill directory">Directory</span>';
    }} else if (node.type === "interview") {{
        badges.innerHTML = '<span class="type-pill interview">Interview</span>';
    }}
    if (node.scheduleName) badges.innerHTML += '<span class="schedule-pill">' + esc(node.scheduleName) + '</span>';
    const aUrl = adminUrl(node);
    if (aUrl) badges.innerHTML += '<a href="' + esc(aUrl) + '" target="_blank" style="color:#1abc9c; font-size:11px; text-decoration:none; margin-left:4px;" title="View on ' + esc(data.siteName || 'Unity') + ' Unity">&#9741;</a>';
    header.appendChild(badges);
    card.appendChild(header);

    // Conditions (routing rules)
    if (node.conditions && node.conditions.length) {{
        const condDiv = document.createElement("div");
        condDiv.className = "cond-row";
        condDiv.innerHTML = node.conditions.map(c => esc(c.param) + " " + esc(c.op) + " <strong>" + esc(c.value) + "</strong>").join("<br>");
        card.appendChild(condDiv);
    }}

    // Audio
    if (node.audio && node.audio.length) {{
        node.audio.forEach(a => {{
            const row = document.createElement("div");
            row.className = "audio-row";
            const gUrl = greetingUrl(node.id, a.greeting);
            row.innerHTML = '&#9835; <span class="audio-badge">' + esc(a.greeting) + ' greeting</span>' +
                (gUrl ? ' <a href="' + esc(gUrl) + '" target="_blank" style="color:#888; font-size:10px; text-decoration:none;" title="Edit in Unity">&#9881;</a>' : '') +
                schedTag(a.schedule) +
                '<br><audio controls preload="none" style="width:100%; height:28px; margin-top:2px;"><source src="' + esc(a.url) + '" type="audio/wav"></audio>';
            card.appendChild(row);
        }});
    }}

    // Menu rows (edges from this node, skip if routing rule — handled above)
    if (node.type !== "routingrule") {{
        const edges = getEdges(node.id);
        // Sort: Key entries first, then After:
        edges.sort((a, b) => {{
            const ak = a.label.startsWith("Key ") ? "0" + a.label : a.label.startsWith("After:") ? "2" + a.label : "1" + a.label;
            const bk = b.label.startsWith("Key ") ? "0" + b.label : b.label.startsWith("After:") ? "2" + b.label : "1" + b.label;
            return ak.localeCompare(bk);
        }});
        edges.forEach(edge => {{
            const targetNode = nodeMap[edge.target];
            const row = document.createElement("div");
            const isAfter = edge.label.startsWith("After:") || edge.label.startsWith("Xfer:");
            const isSelf = edge.target === node.id;
            const isClickable = targetNode && isHandlerNode(targetNode) && !isSelf;
            row.className = "menu-row" + (isAfter ? " after-greeting" : "") + (isClickable ? " clickable" : "");
            row.dataset.target = edge.target;
            row.dataset.label = edge.label;

            const targetName = targetNode ? targetNode.name : "?";
            const isAction = targetNode && targetNode.type === "action";

            row.innerHTML =
                '<span class="menu-key">' + esc(edge.label) + '</span>' +
                '<span class="menu-arrow">&rarr;</span>' +
                '<span class="menu-target' + (isAction ? " action" : "") + (isSelf ? " self-ref" : "") + '">' +
                esc(targetName) + (isSelf ? " (loops back)" : "") +
                (targetNode && targetNode.extension ? ' <span style="color:#888">(' + esc(targetNode.extension) + ')</span>' : '') +
                '</span>' +
                schedTag(edge.schedule);

            if (isClickable) {{
                row.addEventListener("click", () => expandTarget(edge, card));
            }}
            card.appendChild(row);
        }});
        if (edges.length === 0 && node.type !== "routingrule") {{
            const row = document.createElement("div");
            row.className = "menu-row";
            row.innerHTML = '<span style="color:#555; font-size:12px;">No outgoing routes in this schedule</span>';
            card.appendChild(row);
        }}
    }}
    return card;
}}

function expandTarget(edge, parentCard) {{
    const targetNode = nodeMap[edge.target];
    if (!targetNode) return;

    // Loop detection — scroll to existing card
    if (trailPath.some(p => p.nodeId === edge.target)) {{
        const existing = document.getElementById("card-" + edge.target);
        if (existing) {{
            existing.scrollIntoView({{ behavior: "smooth", block: "center" }});
            existing.classList.remove("flash");
            void existing.offsetWidth;
            existing.classList.add("flash");
        }}
        return;
    }}

    // Remove any cards/connectors below the parent
    let sibling = parentCard.nextElementSibling;
    while (sibling) {{
        const next = sibling.nextElementSibling;
        sibling.remove();
        sibling = next;
    }}
    // Trim trail to parent
    const parentIdx = trailPath.findIndex(p => p.nodeId === parentCard.id.replace("card-", ""));
    if (parentIdx >= 0) trailPath = trailPath.slice(0, parentIdx + 1);

    // Clear active row highlights on parent
    parentCard.querySelectorAll(".active-row").forEach(r => r.classList.remove("active-row"));
    // Highlight clicked row
    parentCard.querySelectorAll(".menu-row").forEach(r => {{
        if (r.dataset.target === edge.target && r.dataset.label === edge.label) r.classList.add("active-row");
    }});

    // Add connector + new card
    const container = document.getElementById("flowContainer");
    container.appendChild(createConnector(edge.label));
    const newCard = createCard(targetNode);
    newCard.classList.add("expanded");
    container.appendChild(newCard);
    trailPath.push({{ nodeId: edge.target, label: edge.label }});
    updateBreadcrumb();
    newCard.scrollIntoView({{ behavior: "smooth", block: "center" }});
}}

function createConnector(label) {{
    const conn = document.createElement("div");
    conn.className = "connector";
    if (label) conn.innerHTML = '<span class="connector-label">' + esc(label) + '</span>';
    return conn;
}}

function updateBreadcrumb() {{
    const bc = document.getElementById("breadcrumb");
    bc.innerHTML = trailPath.map((step, i) => {{
        const node = nodeMap[step.nodeId];
        const name = node ? node.name : "?";
        const isLast = i === trailPath.length - 1;
        return (i > 0 ? '<span class="bc-sep"> &gt; </span>' : '') +
            (i > 0 && step.label ? '<span class="bc-label">[' + esc(step.label) + ']</span><span class="bc-sep"> &gt; </span>' : '') +
            '<span class="bc-step' + (isLast ? ' bc-current' : '') + '" onclick="scrollToCard(\\'' + step.nodeId + '\\')">' + esc(name) + '</span>';
    }}).join("");
    if (trailPath.length > 1) {{
        bc.innerHTML += '<button class="copy-link-btn" onclick="copyDeepLink()">Copy Link</button>';
    }}
    syncHash();
}}

function syncHash() {{
    if (!trailPath.length) {{ history.replaceState(null, "", location.pathname); return; }}
    const entry = trailPath[0].nodeId;
    // Steps after the auto-followed rule target (index 0 = entry, index 1 = rule target if routing rule)
    const startNode = nodeMap[entry];
    const skipCount = (startNode && startNode.type === "routingrule") ? 2 : 1;
    const steps = trailPath.slice(skipCount).map(s => encodeURIComponent(s.label) + "~" + s.nodeId);
    let h = "s=" + activeSchedule + "&e=" + entry;
    if (steps.length) h += "&p=" + steps.join(",");
    history.replaceState(null, "", "#" + h);
}}

function copyDeepLink() {{
    const url = location.href;
    navigator.clipboard.writeText(url).then(() => {{
        const btn = document.querySelector(".copy-link-btn");
        if (btn) {{ btn.textContent = "Copied!"; setTimeout(() => btn.textContent = "Copy Link", 1500); }}
    }}).catch(() => {{
        // Fallback for non-HTTPS contexts
        const ta = document.createElement("textarea");
        ta.value = location.href;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        const btn = document.querySelector(".copy-link-btn");
        if (btn) {{ btn.textContent = "Copied!"; setTimeout(() => btn.textContent = "Copy Link", 1500); }}
    }});
}}

function loadFromHash() {{
    const h = location.hash.replace(/^#/, "");
    if (!h) return false;
    const params = {{}};
    h.split("&").forEach(p => {{ const [k, ...v] = p.split("="); params[k] = v.join("="); }});
    if (!params.e) return false;

    // Set schedule
    if (params.s && ["standard", "offhours", "holiday", "all"].includes(params.s)) {{
        activeSchedule = params.s;
        document.querySelectorAll(".schedule-btn").forEach(btn => {{
            btn.classList.toggle("active", btn.dataset.schedule === params.s);
        }});
    }}

    // Set entry point
    populateEntryPoints();
    const sel = document.getElementById("entryPoint");
    if (!sel.querySelector('option[value="' + params.e + '"]')) return false;
    sel.value = params.e;
    renderFlow();

    // Replay path steps
    if (params.p) {{
        const steps = params.p.split(",").map(s => {{
            const tilde = s.indexOf("~");
            return {{ label: decodeURIComponent(s.substring(0, tilde)), nodeId: s.substring(tilde + 1) }};
        }});
        for (const step of steps) {{
            const lastNodeId = trailPath[trailPath.length - 1].nodeId;
            const lastCard = document.getElementById("card-" + lastNodeId);
            if (!lastCard) break;
            const edge = data.edges.find(e => e.source === lastNodeId && e.target === step.nodeId && e.label === step.label && edgeMatch(e));
            if (!edge) break;
            expandTarget(edge, lastCard);
        }}
    }}
    return true;
}}

function scrollToCard(nodeId) {{
    const el = document.getElementById("card-" + nodeId);
    if (el) {{
        el.scrollIntoView({{ behavior: "smooth", block: "center" }});
        el.classList.remove("flash");
        void el.offsetWidth;
        el.classList.add("flash");
    }}
}}

// Init
populateEntryPoints();
if (!loadFromHash()) {{
    renderFlow();
}}
</script>
{floating_nav_html("callflow.html")}
</body>
</html>'''


def generate_schedules_html(holiday_schedules, schedules, site_name="", host=""):
    title_prefix = f"{site_name} — " if site_name else ""
    report_data = json.dumps({
        "host": host,
        "siteName": site_name,
        "holidays": [{
            "name": s.get("DisplayName", ""),
            "entries": [{
                "name": h.get("DisplayName", ""),
                "start": h.get("StartDate", "").split(" ")[0],
                "end": h.get("EndDate", "").split(" ")[0],
            } for h in s.get("_holidays", [])]
        } for s in holiday_schedules],
        "schedules": [{
            "name": s.get("DisplayName", ""),
            "id": s.get("ObjectId", ""),
            "details": [{
                "days": _active_days(d),
                "startTime": _format_minutes(d.get("StartTime", "")),
                "endTime": _format_minutes(d.get("EndTime", "")),
                "active": str(d.get("IsActive", "true")).lower() == "true",
            } for d in s.get("_details", [])]
        } for s in schedules],
    })

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_prefix}Schedules</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='12' fill='%231a1a2e'/><path d='M16 20a4 4 0 014-4h8a4 4 0 014 4v24a4 4 0 01-4 4h-8a4 4 0 01-4-4z' fill='%23e94560'/><circle cx='24' cy='42' r='2' fill='%231a1a2e'/><path d='M36 28h10m0 0l-4-4m4 4l-4 4' stroke='%232ecc71' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/><path d='M36 38h10m0 0l-4-4m4 4l-4 4' stroke='%233498db' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a2e; color: #e0e0e0; padding: 24px; }}
h1 {{ color: #e94560; margin-bottom: 8px; }}
h2 {{ color: #e94560; margin: 32px 0 12px 0; font-size: 20px; border-bottom: 1px solid #0f3460; padding-bottom: 8px; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; font-size: 13px; }}
th {{ background: #16213e; color: #e94560; text-align: left; padding: 10px 12px; position: sticky; top: 0; border-bottom: 2px solid #0f3460; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #0f3460; vertical-align: top; }}
tr:hover {{ background: #16213e; }}
.muted {{ color: #555; }}
.section-header {{ display: flex; align-items: center; gap: 12px; }}
.section-header h2 {{ margin: 0; }}
.copy-btn {{ padding: 4px 10px; border: 1px solid #0f3460; background: #16213e; color: #888; cursor: pointer; border-radius: 3px; font-size: 11px; transition: all 0.2s; }}
.copy-btn:hover {{ color: #e0e0e0; border-color: #e94560; }}
.back-to-top {{ position: fixed; bottom: 16px; left: 16px; padding: 8px 14px; background: #0f3460; border: 1px solid #0f3460; color: #e0e0e0; cursor: pointer; border-radius: 4px; font-size: 12px; z-index: 100; text-decoration: none; }}
.back-to-top:hover {{ background: #1a1a4e; border-color: #e94560; }}
</style>
</head>
<body>
<h1>{title_prefix}Schedules</h1>

<div class="section-header"><h2 id="schedules">Business Hours</h2><button class="copy-btn" onclick="copyTableAsMd('scheduleTable', this)">Copy as Markdown</button></div>
<table id="scheduleTable">
<thead>
<tr><th>Schedule</th><th>Days</th><th>Start Time</th><th>End Time</th><th>Active</th></tr>
</thead>
<tbody></tbody>
</table>

<div class="section-header"><h2 id="holidays">Holiday Schedules</h2><button class="copy-btn" onclick="copyTableAsMd('holidayTable', this)">Copy as Markdown</button></div>
<table id="holidayTable">
<thead>
<tr><th>Schedule</th><th>Holiday</th><th>Date</th></tr>
</thead>
<tbody></tbody>
</table>

<script>
const data = {report_data};

function esc(s) {{
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
}}

// Render schedules
(function() {{
    const tbody = document.querySelector("#scheduleTable tbody");
    if (!data.schedules.length) {{
        tbody.innerHTML = '<tr><td colspan="5" class="muted">No schedules found</td></tr>';
        return;
    }}
    data.schedules.forEach(s => {{
        if (!s.details.length) {{
            const tr = document.createElement("tr");
            tr.innerHTML = '<td>' + esc(s.name) + '</td><td>All days</td><td>12:00 AM</td><td>11:59 PM</td><td>Yes</td>';
            tbody.appendChild(tr);
            return;
        }}
        s.details.forEach(d => {{
            const tr = document.createElement("tr");
            tr.innerHTML = '<td>' + esc(s.name) + '</td><td>' + esc(d.days) + '</td><td>' + esc(d.startTime) + '</td><td>' + esc(d.endTime) + '</td><td>' + (d.active ? "Yes" : '<span class="muted">No</span>') + '</td>';
            tbody.appendChild(tr);
        }});
    }});
}})();

// Render holidays
(function() {{
    const tbody = document.querySelector("#holidayTable tbody");
    if (!data.holidays.length) {{
        tbody.innerHTML = '<tr><td colspan="3" class="muted">No holiday schedules found</td></tr>';
        return;
    }}
    data.holidays.forEach(s => {{
        if (!s.entries.length) return;
        s.entries.forEach(h => {{
            const tr = document.createElement("tr");
            const dateStr = h.start === h.end ? esc(h.start) : esc(h.start) + ' &ndash; ' + esc(h.end);
            tr.innerHTML = '<td>' + esc(s.name) + '</td><td>' + esc(h.name) + '</td><td>' + dateStr + '</td>';
            tbody.appendChild(tr);
        }});
    }});
}})();

function flashBtn(btn, msg) {{
    const orig = btn.textContent;
    btn.textContent = msg || "Copied!";
    setTimeout(() => btn.textContent = orig, 1500);
}}

function copyTableAsMd(tableId, btn) {{
    const table = document.getElementById(tableId);
    const headerCells = Array.from(table.querySelectorAll("thead th"));
    const headers = headerCells.map(th => th.textContent);
    const lines = [headers.join(" | "), headers.map(() => "---").join(" | ")];
    table.querySelectorAll("tbody tr").forEach(tr => {{
        const cells = Array.from(tr.querySelectorAll("td")).map(td => td.textContent.trim());
        lines.push(cells.join(" | "));
    }});
    navigator.clipboard.writeText(lines.join("\\n")).then(() => flashBtn(btn));
}}
</script>
<a href="#" class="back-to-top">&uarr; Top</a>
{floating_nav_html("schedules.html")}
</body>
</html>'''


def generate_test_times_html(schedules, site_name="", host=""):
    """Generate a page listing all unique times to test the call tree for a generic week."""
    title_prefix = f"{site_name} — " if site_name else ""
    # Build raw schedule data with minute values and day flags for JS processing
    raw_schedules = []
    for s in schedules:
        details = []
        for d in s.get("_details", []):
            if str(d.get("IsActive", "true")).lower() != "true":
                continue
            try:
                start_min = int(d.get("StartTime", 0))
                end_min = int(d.get("EndTime", 1440))
            except (ValueError, TypeError):
                continue
            day_flags = {}
            for flag, abbr in _DAY_FLAGS:
                day_flags[abbr] = str(d.get(flag, "false")).lower() == "true"
            details.append({"start": start_min, "end": end_min, "days": day_flags})
        if details:
            raw_schedules.append({"name": s.get("DisplayName", ""), "details": details})
    report_data = json.dumps({"schedules": raw_schedules, "siteName": site_name})

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_prefix}Test Times</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='12' fill='%231a1a2e'/><path d='M16 20a4 4 0 014-4h8a4 4 0 014 4v24a4 4 0 01-4 4h-8a4 4 0 01-4-4z' fill='%23e94560'/><circle cx='24' cy='42' r='2' fill='%231a1a2e'/><path d='M36 28h10m0 0l-4-4m4 4l-4 4' stroke='%232ecc71' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/><path d='M36 38h10m0 0l-4-4m4 4l-4 4' stroke='%233498db' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a2e; color: #e0e0e0; padding: 24px; }}
h1 {{ color: #e94560; margin-bottom: 4px; }}
.subtitle {{ color: #888; font-size: 13px; margin-bottom: 24px; }}
h2 {{ color: #e94560; margin: 28px 0 10px 0; font-size: 18px; border-bottom: 1px solid #0f3460; padding-bottom: 6px; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; font-size: 13px; }}
th {{ background: #16213e; color: #e94560; text-align: left; padding: 8px 12px; position: sticky; top: 0; border-bottom: 2px solid #0f3460; }}
td {{ padding: 7px 12px; border-bottom: 1px solid #0f3460; vertical-align: top; }}
tr:hover {{ background: #16213e; }}
.muted {{ color: #555; }}
.state-standard {{ color: #2ecc71; font-weight: 600; }}
.state-offhours {{ color: #f39c12; font-weight: 600; }}
.state-holiday {{ color: #e74c3c; font-weight: 600; }}
.reason {{ color: #888; font-size: 12px; }}
.note {{ background: #16213e; border-left: 4px solid #e74c3c; padding: 16px 20px; margin: 24px 0; border-radius: 0 6px 6px 0; line-height: 1.6; }}
.note strong {{ color: #e74c3c; }}
.no-transitions {{ color: #888; font-style: italic; padding: 16px 0; }}
.copy-btn {{ padding: 4px 10px; border: 1px solid #0f3460; background: #16213e; color: #888; cursor: pointer; border-radius: 3px; font-size: 11px; transition: all 0.2s; margin-left: 12px; }}
.copy-btn:hover {{ color: #e0e0e0; border-color: #e94560; }}
.section-header {{ display: flex; align-items: center; gap: 12px; }}
.section-header h2 {{ margin: 0; }}
.back-to-top {{ position: fixed; bottom: 16px; left: 16px; padding: 8px 14px; background: #0f3460; border: 1px solid #0f3460; color: #e0e0e0; cursor: pointer; border-radius: 4px; font-size: 12px; z-index: 100; text-decoration: none; }}
.back-to-top:hover {{ background: #1a1a4e; border-color: #e94560; }}
.day-off {{ background: #1a1a2e; }}
</style>
</head>
<body>
<h1>{title_prefix}Test Times</h1>
<p class="subtitle">Unique times to call and verify the auto attendant routes correctly for each schedule state.</p>

<div class="note">
<strong>Holiday Testing:</strong> Create a temporary holiday schedule entry for today's date in Cisco Unity Connection
to test the Holiday greeting path. Holidays override all other schedules, so this is the
only way to verify that Holiday greetings and routing work correctly without waiting for an actual holiday.
</div>

<div id="content"></div>

<script>
const data = {report_data};

function fmtTime(minutes) {{
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    const ampm = h < 12 ? "AM" : "PM";
    const h12 = h % 12 || 12;
    return h12 + ":" + String(m).padStart(2, "0") + " " + ampm;
}}

function esc(s) {{
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
}}

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DAY_LABELS = {{ Mon: "Monday", Tue: "Tuesday", Wed: "Wednesday", Thu: "Thursday", Fri: "Friday", Sat: "Saturday", Sun: "Sunday" }};

(function() {{
    const container = document.getElementById("content");

    // For each schedule, compute per-day active windows
    // Merge all schedules' windows per day to find all transitions
    const dayWindows = {{}};  // day -> list of window objects
    DAYS.forEach(d => dayWindows[d] = []);

    data.schedules.forEach(s => {{
        s.details.forEach(det => {{
            DAYS.forEach(day => {{
                if (det.days[day]) {{
                    dayWindows[day].push({{ start: det.start, end: det.end, schedule: s.name }});
                }}
            }});
        }});
    }});

    if (!data.schedules.length) {{
        container.innerHTML = '<p class="no-transitions">No schedule data available. Run the report with schedule data to generate test times.</p>';
        return;
    }}

    // Build a fingerprint per day so we can group days with identical schedules
    const dayFingerprints = {{}};
    DAYS.forEach(day => {{
        const sorted = dayWindows[day].slice().sort((a, b) => a.start - b.start || a.end - b.end || a.schedule.localeCompare(b.schedule));
        dayFingerprints[day] = JSON.stringify(sorted.map(w => [w.start, w.end, w.schedule]));
    }});

    // Group consecutive days with the same fingerprint (preserve day order)
    const groups = [];
    DAYS.forEach(day => {{
        const fp = dayFingerprints[day];
        const last = groups.length ? groups[groups.length - 1] : null;
        if (last && last.fp === fp) {{
            last.days.push(day);
        }} else {{
            groups.push({{ fp, days: [day], windows: dayWindows[day].slice().sort((a, b) => a.start - b.start) }});
        }}
    }});

    function groupLabel(days) {{
        if (days.length === 7) return "Every Day";
        if (days.length === 1) return DAY_LABELS[days[0]];
        return DAY_LABELS[days[0]] + " – " + DAY_LABELS[days[days.length - 1]];
    }}

    groups.forEach(group => {{
        const section = document.createElement("div");
        const windows = group.windows;
        const gid = group.days.join("-");
        const label = groupLabel(group.days);

        let heading = '<div class="section-header"><h2>' + label + '</h2>';
        heading += '<button class="copy-btn" data-day="' + gid + '">Copy as Markdown</button></div>';
        section.innerHTML = heading;
        section.querySelector(".copy-btn").addEventListener("click", function() {{ copyDayTable(this.dataset.day, this, label); }});

        if (!windows.length) {{
            section.innerHTML += '<p class="no-transitions">No business hours scheduled — Off Hours all day.</p>';
            const tbl = document.createElement("table");
            tbl.id = "table-" + gid;
            tbl.innerHTML = '<thead><tr><th>Test Time</th><th>Expected State</th><th>Reason</th></tr></thead>' +
                '<tbody><tr><td>Any time</td><td class="state-offhours">Off Hours</td><td class="reason">No schedule active this day</td></tr></tbody>';
            section.appendChild(tbl);
            container.appendChild(section);
            return;
        }}

        // Merge overlapping windows into non-overlapping segments
        const merged = [];
        let cur = null;
        windows.forEach(w => {{
            if (!cur) {{ cur = {{ start: w.start, end: w.end, schedules: [w.schedule] }}; return; }}
            if (w.start <= cur.end) {{
                cur.end = Math.max(cur.end, w.end);
                if (!cur.schedules.includes(w.schedule)) cur.schedules.push(w.schedule);
            }} else {{
                merged.push(cur);
                cur = {{ start: w.start, end: w.end, schedules: [w.schedule] }};
            }}
        }});
        if (cur) merged.push(cur);

        // Compute test times — one sample per distinct state period
        const tests = [];

        // Off hours before first window — 5 min after midnight
        if (merged[0].start > 0) {{
            tests.push({{ time: 5, state: "offhours", reason: "Off hours — before business hours" }});
        }}

        merged.forEach((w, i) => {{
            const schedNote = w.schedules.length > 0 && data.schedules.length > 1
                ? " (" + w.schedules.join(", ") + ")" : "";

            // During this business hours window — 5 min after open
            tests.push({{ time: w.start + 5, state: "standard", reason: "During business hours" + schedNote }});

            // Gap between this window and next — 5 min after close
            if (i < merged.length - 1) {{
                tests.push({{ time: w.end + 5, state: "offhours", reason: "Off hours — gap between shifts" }});
            }}
        }});

        // Off hours after last window — 5 min after close
        const lastEnd = merged[merged.length - 1].end;
        if (lastEnd < 1440) {{
            tests.push({{ time: lastEnd + 5, state: "offhours", reason: "Off hours — after business hours" }});
        }} else {{
            tests.push({{ time: 720, state: "standard", reason: "24-hour schedule — always Standard" }});
        }}

        // Deduplicate by time
        const seen = new Set();
        const unique = [];
        tests.sort((a, b) => a.time - b.time);
        tests.forEach(t => {{
            if (!seen.has(t.time)) {{
                seen.add(t.time);
                unique.push(t);
            }}
        }});

        const tbl = document.createElement("table");
        tbl.id = "table-" + gid;
        tbl.innerHTML = '<thead><tr><th>Test Time</th><th>Expected State</th><th>Reason</th></tr></thead>';
        const tbody = document.createElement("tbody");
        unique.forEach(t => {{
            const tr = document.createElement("tr");
            const stateClass = t.state === "standard" ? "state-standard" : "state-offhours";
            const stateLabel = t.state === "standard" ? "Standard" : "Off Hours";
            tr.innerHTML = '<td>' + fmtTime(t.time) + '</td><td class="' + stateClass + '">' + stateLabel + '</td><td class="reason">' + esc(t.reason) + '</td>';
            tbody.appendChild(tr);
        }});
        tbl.appendChild(tbody);
        section.appendChild(tbl);
        container.appendChild(section);
    }});

    // Holiday test section
    const holidaySection = document.createElement("div");
    holidaySection.innerHTML = '<div class="section-header"><h2>Holiday Override</h2></div>' +
        '<table id="table-holiday"><thead><tr><th>Test Time</th><th>Expected State</th><th>Reason</th></tr></thead>' +
        '<tbody>' +
        '<tr><td>During business hours</td><td class="state-holiday">Holiday</td><td class="reason">Holiday overrides Standard — should hear Holiday greeting</td></tr>' +
        '<tr><td>Outside business hours</td><td class="state-holiday">Holiday</td><td class="reason">Holiday overrides Off Hours — should hear Holiday greeting, not Off Hours</td></tr>' +
        '</tbody></table>' +
        '<p style="color:#888; font-size:12px; margin-top:8px;">Create a temporary holiday for today in Unity Connection to test these.</p>';
    container.appendChild(holidaySection);
}})();

function flashBtn(btn, msg) {{
    const orig = btn.textContent;
    btn.textContent = msg || "Copied!";
    setTimeout(() => btn.textContent = orig, 1500);
}}

function copyDayTable(day, btn, label) {{
    const table = document.getElementById("table-" + day);
    if (!table) return;
    const headerCells = Array.from(table.querySelectorAll("thead th"));
    const headers = headerCells.map(th => th.textContent);
    const title = label || DAY_LABELS[day] || day;
    const lines = [title, headers.join(" | "), headers.map(() => "---").join(" | ")];
    table.querySelectorAll("tbody tr").forEach(tr => {{
        const cells = Array.from(tr.querySelectorAll("td")).map(td => td.textContent.trim());
        lines.push(cells.join(" | "));
    }});
    navigator.clipboard.writeText(lines.join("\\n")).then(() => flashBtn(btn));
}}
</script>
<a href="#" class="back-to-top">&uarr; Top</a>
{floating_nav_html("test_times.html")}
</body>
</html>'''


def generate_index_html(site_name=""):
    title_prefix = f"{site_name} — " if site_name else ""
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_prefix}Call Handler Reports</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='12' fill='%231a1a2e'/><path d='M16 20a4 4 0 014-4h8a4 4 0 014 4v24a4 4 0 01-4 4h-8a4 4 0 01-4-4z' fill='%23e94560'/><circle cx='24' cy='42' r='2' fill='%231a1a2e'/><path d='M36 28h10m0 0l-4-4m4 4l-4 4' stroke='%232ecc71' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/><path d='M36 38h10m0 0l-4-4m4 4l-4 4' stroke='%233498db' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a2e; color: #e0e0e0; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
.index {{ max-width: 480px; width: 100%; padding: 48px 32px; }}
h1 {{ color: #e94560; font-size: 24px; margin-bottom: 8px; }}
.subtitle {{ color: #888; font-size: 14px; margin-bottom: 32px; }}
.card {{ display: block; background: #16213e; border: 2px solid #0f3460; border-radius: 8px; padding: 20px; margin-bottom: 12px; text-decoration: none; color: #e0e0e0; transition: all 0.2s; }}
.card:hover {{ border-color: #e94560; background: #1a2540; }}
.card h2 {{ font-size: 16px; color: #1abc9c; margin-bottom: 4px; }}
.card p {{ font-size: 13px; color: #888; }}
</style>
</head>
<body>
<div class="index">
<h1>{title_prefix}Call Handler Reports</h1>
<p class="subtitle">Choose a view to explore the call handler routing data.</p>
<a href="callflow.html" class="card">
<h2>Call Flow Explorer</h2>
<p>Trace calls step by step — select an entry point, click key presses to drill down through the IVR.</p>
</a>
<a href="callhandler_map.html" class="card">
<h2>Graph View</h2>
<p>Interactive D3.js force graph showing all handlers and their connections.</p>
</a>
<a href="callhandler_report.html" class="card">
<h2>Handlers &amp; Routing</h2>
<p>Call flow trees, handler table with routing rules, classifications, and debug tools.</p>
</a>
<a href="schedules.html" class="card">
<h2>Schedules</h2>
<p>Business hours and holiday schedules.</p>
</a>
<a href="test_times.html" class="card">
<h2>Test Times</h2>
<p>Unique times to call each day of the week to verify all schedule transitions route correctly.</p>
</a>
</div>
</body>
</html>'''


def ping_check(host):
    """Ping the host to verify network connectivity before attempting API calls."""
    hostname = urlparse(host).hostname or host
    print(f"Checking connectivity to {hostname}...")
    param = "-n" if platform.system().lower() == "windows" else "-c"
    try:
        result = subprocess.run(
            ["ping", param, "2", hostname],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"\nError: Cannot reach {hostname}.")
            print("Are you connected to the VPN?")
            sys.exit(1)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print(f"\nError: Cannot reach {hostname}.")
        print("Are you connected to the VPN?")
        sys.exit(1)
    print(f"  {hostname} is reachable.")


class _LegacySSLAdapter(HTTPAdapter):
    """HTTPS adapter that tolerates legacy TLS on older CUC servers."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        # Disable TLS 1.3 — old servers may reject its handshake extensions
        ctx.options |= ssl.OP_NO_TLSv1_3
        ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def connect(args):
    """Create an authenticated session from CLI args.

    Tries a standard TLS connection first.  If the handshake fails
    (common on CUC 10.x and other legacy servers), automatically
    retries with the legacy SSL adapter.
    """
    host = args.host.rstrip("/")
    ping_check(host)
    password = getpass.getpass(f"Password for {args.user}@{host}: ")
    session = requests.Session()
    session.auth = (args.user, password)

    # Quick probe with default TLS settings
    test_url = f"{host}/vmrest/version"
    try:
        session.get(test_url, verify=False, timeout=10)
        print("  TLS: standard connection OK")
    except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
        print("  TLS: standard handshake failed, falling back to legacy TLS")
        session.mount("https://", _LegacySSLAdapter())
        try:
            session.get(test_url, verify=False, timeout=10)
            print("  TLS: legacy connection OK")
        except Exception as e:
            print(f"  Warning: legacy TLS probe also failed: {e}")
            # Keep the legacy adapter mounted — let later calls surface the real error

    return session, host


class TeeLogger:
    """Write to both stdout and a log file."""
    def __init__(self, log_path):
        self.terminal = sys.stdout
        self.log = open(log_path, "w", encoding="utf-8")

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def close(self):
        self.log.close()
        sys.stdout = self.terminal


def cmd_generate(args):
    """Full report generation (default command)."""
    session, host = connect(args)

    print("Identifying site...")
    site_id = fetch_site_id(session, host)
    print(f"  Site: {site_id}")

    site_name = friendly_site_name(site_id)
    site_dir = prepare_site_dir(site_id)

    # Start logging to file
    log_path = os.path.join(site_dir, "run.log")
    tee = TeeLogger(log_path)
    sys.stdout = tee
    try:
        print(f"Log: {log_path}")
        print(f"Site: {site_id} ({site_name})")
        print(f"Host: {host}")
        print(f"User: {args.user}")
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()

        try:
            call_handlers = fetch_call_handlers(session, host)
            interview_handlers = fetch_interview_handlers(session, host)
            directory_handlers = fetch_directory_handlers(session, host)
            routing_rules = fetch_routing_rules(session, host)
        except requests.exceptions.ConnectionError as e:
            print(f"Error: Could not connect to {host}: {e}")
            sys.exit(1)
        except requests.exceptions.HTTPError as e:
            print(f"Error: API request failed: {e}")
            sys.exit(1)

        # Non-critical data — continue if endpoints are unavailable
        try:
            holiday_schedules = fetch_holiday_schedules(session, host)
        except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
            print(f"  Warning: Could not fetch holiday schedules: {e}")
            holiday_schedules = []

        try:
            schedules = fetch_schedules(session, host)
        except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
            print(f"  Warning: Could not fetch schedules: {e}")
            schedules = []

        try:
            schedule_sets = fetch_schedule_sets(session, host)
        except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
            print(f"  Warning: Could not fetch schedule sets: {e}")
            schedule_sets = []

        # Build schedule set OID -> display name lookup
        schedule_set_map = {s["ObjectId"]: s.get("DisplayName", "") for s in schedule_sets}

        print(f"\nFound {len(call_handlers)} call handlers, "
              f"{len(interview_handlers)} interview handlers, "
              f"{len(directory_handlers)} directory handlers, "
              f"{len(routing_rules)} routing rules, "
              f"{len(holiday_schedules)} holiday schedules, "
              f"{len(schedules)} schedules, "
              f"{len(schedule_sets)} schedule sets")

        print("\nBuilding graph (fetching menu entries, transfer rules, greetings, rule conditions)...")
        nodes, edges = build_graph(call_handlers, interview_handlers, routing_rules, session, host,
                                   schedule_set_map=schedule_set_map,
                                   directory_handlers=directory_handlers)

        # Summary
        classifications = {}
        for n in nodes:
            c = n["classification"]
            classifications[c] = classifications.get(c, 0) + 1

        audio_count = sum(len(n.get("audio", [])) for n in nodes)
        print(f"\nGraph: {len(nodes)} nodes, {len(edges)} edges, {audio_count} audio greetings")
        for cls, count in sorted(classifications.items()):
            print(f"  {cls}: {count}")

        if audio_count:
            print("\nDownloading greeting audio files...")
            download_audio_files(session, nodes, site_dir)

        d3_local = copy_d3(site_dir)

        map_path = os.path.join(site_dir, "callhandler_map.html")
        report_path = os.path.join(site_dir, "callhandler_report.html")
        flow_path = os.path.join(site_dir, "callflow.html")
        sched_path = os.path.join(site_dir, "schedules.html")
        index_path = os.path.join(site_dir, "index.html")

        print(f"\nGenerating reports in {site_dir}/...")
        html = generate_html(nodes, edges, d3_local=d3_local, site_name=site_name, host=host)
        with open(map_path, "w", encoding="utf-8") as f:
            f.write(html)

        table_html = generate_table_html(nodes, edges, holiday_schedules, schedules, site_name=site_name, host=host)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(table_html)

        flow_html = generate_callflow_html(nodes, edges, site_name=site_name, host=host)
        with open(flow_path, "w", encoding="utf-8") as f:
            f.write(flow_html)

        sched_html = generate_schedules_html(holiday_schedules, schedules, site_name=site_name, host=host)
        with open(sched_path, "w", encoding="utf-8") as f:
            f.write(sched_html)

        test_path = os.path.join(site_dir, "test_times.html")
        test_html = generate_test_times_html(schedules, site_name=site_name, host=host)
        with open(test_path, "w", encoding="utf-8") as f:
            f.write(test_html)

        idx_html = generate_index_html(site_name=site_name)
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(idx_html)

        print(f"Done! Reports written to {site_dir}/")

        webbrowser.open(f"file://{os.path.abspath(index_path)}")
    finally:
        tee.close()


def cmd_query(args):
    """Query a specific CUPI API path and dump raw JSON."""
    session, host = connect(args)
    path = args.path if args.path.startswith("/") else f"/{args.path}"
    try:
        data = api_get(session, host, path)
        print(json.dumps(data, indent=2))
    except requests.exceptions.HTTPError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_handler(args):
    """Look up a call handler by name or extension and dump its details."""
    session, host = connect(args)
    call_handlers = fetch_call_handlers(session, host)

    q = args.search.lower()
    matches = [ch for ch in call_handlers if
               q in ch.get("DisplayName", "").lower() or
               q == ch.get("DtmfAccessId", "").lower() or
               q in ch.get("ObjectId", "").lower()]

    if not matches:
        print(f"No handlers matching '{args.search}'")
        sys.exit(1)

    for ch in matches:
        oid = ch.get("ObjectId", "")
        name = ch.get("DisplayName", "")
        print(f"\n{'='*60}")
        print(f"Handler: {name}")
        print(f"Extension: {ch.get('DtmfAccessId', 'N/A')}")
        print(f"ObjectId: {oid}")
        print(f"{'='*60}")

        print("\n--- Transfer Rules ---")
        for tr in fetch_transfer_rules(session, host, oid, name):
            rule_idx = tr.get("RuleIndex", "?")
            print(f"  [{rule_idx}] Enabled={tr.get('TransferEnabled')} "
                  f"Extension={tr.get('Extension', '')} "
                  f"Target={tr.get('TargetHandlerObjectId', 'N/A')}")

        print("\n--- Greetings ---")
        for gr in fetch_greetings(session, host, oid, name):
            gt = gr.get("GreetingType", "?")
            play = gr.get("PlayWhat", "?")
            action = gr.get("AfterGreetingAction", "?")
            target = gr.get("AfterGreetingTargetHandlerObjectId", "")
            enabled = gr.get("Enabled", "?")
            lang = gr.get("LanguageCode", "?")
            print(f"  [{gt}] Enabled={enabled} PlayWhat={play} Lang={lang} "
                  f"AfterAction={action} Target={target or 'N/A'}")

        print("\n--- Menu Entries ---")
        for me in fetch_menu_entries(session, host, oid, name):
            key = me.get("TouchtoneKey", "?")
            action = me.get("Action", "?")
            target = me.get("TargetHandlerObjectId", "")
            print(f"  Key {key}: Action={action} Target={target or 'N/A'}")

        if args.raw:
            print("\n--- Raw Handler JSON ---")
            print(json.dumps(ch, indent=2))


def cmd_audio_probe(args):
    """Probe every call handler's greetings for uploaded audio."""
    session, host = connect(args)
    call_handlers = fetch_call_handlers(session, host)

    print(f"\nProbing greetings for {len(call_handlers)} call handlers...\n")

    total_greetings = 0
    total_audio = 0
    handlers_with_audio = 0

    for ch in call_handlers:
        oid = ch.get("ObjectId", "")
        name = ch.get("DisplayName", "")
        greetings = fetch_greetings(session, host, oid, name)
        handler_has_audio = False

        print(f"  {name}")
        for gr in greetings:
            gt = gr.get("GreetingType", "?")
            enabled = gr.get("Enabled", "?")
            play_what = gr.get("PlayWhat", "?")
            lang = str(gr.get("LanguageCode", "1033"))
            total_greetings += 1

            # Build audio URL and probe it
            audio_url = greeting_audio_url(host, oid, gt, lang)
            status = "?"
            try:
                resp = session.head(audio_url, verify=False, timeout=10)
                status = resp.status_code
            except Exception as e:
                status = f"ERR: {e}"

            has_audio = status == 200
            if has_audio:
                total_audio += 1
                handler_has_audio = True

            marker = "AUDIO" if has_audio else "-----"
            print(f"    [{gt}] Enabled={enabled} PlayWhat={play_what} "
                  f"HEAD={status} {marker}")

        if handler_has_audio:
            handlers_with_audio += 1
        print()

    print(f"{'='*60}")
    print(f"Summary: {total_audio} audio files found across {handlers_with_audio} handlers")
    print(f"         {total_greetings} total greetings checked on {len(call_handlers)} handlers")
    if total_audio > 0:
        pw_note = "PlayWhat=2" if total_audio > 0 else ""
        print(f"\nIf audio exists but PlayWhat != 2, the greeting data may not")
        print(f"match the actual uploaded files. Report this as a bug.")


PROBE_ENDPOINTS = [
    # Core
    ("/vmrest/handlers/callhandlers", "Call Handlers"),
    ("/vmrest/handlers/interviewhandlers", "Interview Handlers"),
    ("/vmrest/handlers/directoryhandlers", "Directory Handlers"),
    ("/vmrest/routingrules", "Routing Rules"),
    ("/vmrest/routingruleconditions", "Routing Rule Conditions"),
    # Schedules
    ("/vmrest/schedules", "Schedules"),
    ("/vmrest/schedulesets", "Schedule Sets"),
    ("/vmrest/holidayschedules", "Holiday Schedules (legacy)"),
    ("/vmrest/schedules/{sched_id}/scheduledetails", "Schedule Details (sample)"),
    ("/vmrest/schedulesets/{schedset_id}/schedulessetmembers", "Schedule Set Members (sample)"),
    ("/vmrest/schedulesets/{schedset_id}/schedulesetmembers", "Schedule Set Members alt (sample)"),
    # Users & contacts
    ("/vmrest/users", "Users"),
    ("/vmrest/contacts", "Contacts"),
    ("/vmrest/distributionlists", "Distribution Lists"),
    # System
    ("/vmrest/cluster", "Cluster"),
    ("/vmrest/vmsservers", "VMS Servers"),
    ("/vmrest/systemconfig", "System Config"),
    ("/vmrest/portgroups", "Port Groups"),
    ("/vmrest/ports", "Ports"),
    ("/vmrest/phonerecordings", "Phone Recordings"),
    # Call handler sub-resources (tested against first handler found)
    ("/vmrest/handlers/callhandlers/{id}/menuentries", "Menu Entries (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/transferrules", "Transfer Rules (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/transferoptions", "Transfer Options (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/greetings", "Greetings (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/callerInput", "Caller Input (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/callerinput", "Caller Input lowercase (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/callhandlerowner", "Call Handler Owner (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/transferrule", "Transfer Rule singular (sample)"),
    # Directory handler sub-resources
    ("/vmrest/handlers/directoryhandlers/{dir_id}", "Directory Handler detail (sample)"),
    ("/vmrest/handlers/directoryhandlers/{dir_id}/directoryhandlerstreamfiles", "Directory Handler Greetings (sample)"),
    # Interview handler sub-resources
    ("/vmrest/handlers/interviewhandlers/{ih_id}/interviewquestions", "Interview Questions (sample)"),
    # Other
    ("/vmrest/callhandlertemplates", "Call Handler Templates"),
    ("/vmrest/notificationdevices", "Notification Devices"),
    ("/vmrest/smpproviders", "SMPP Providers"),
    ("/vmrest/tenants", "Tenants"),
    ("/vmrest/timezones", "Time Zones"),
    ("/vmrest/partitions", "Partitions"),
    ("/vmrest/searchspaces", "Search Spaces"),
    ("/vmrest/cosses", "Classes of Service"),
    ("/vmrest/restrictionpatterns", "Restriction Patterns"),
    ("/vmrest/restrictiontables", "Restriction Tables"),
    ("/vmrest/ldapdirectories", "LDAP Directories"),
    ("/vmrest/externalservices", "External Services"),
    ("/vmrest/policies", "Policies"),
]


def cmd_probe(args):
    """Probe known CUPI endpoints to see what's available on this server."""
    session, host = connect(args)

    # Get sample IDs for sub-resource probes
    sample_id = None
    sample_sched_id = None
    sample_schedset_id = None
    sample_dir_id = None
    sample_ih_id = None
    try:
        data = api_get(session, host, "/vmrest/handlers/callhandlers", {"rowsPerPage": 1})
        handlers = data.get("Callhandler", [])
        if isinstance(handlers, dict):
            handlers = [handlers]
        if handlers:
            sample_id = handlers[0].get("ObjectId", "")
    except Exception:
        pass
    try:
        data = api_get(session, host, "/vmrest/schedules", {"rowsPerPage": 1})
        scheds = data.get("Schedule", [])
        if isinstance(scheds, dict):
            scheds = [scheds]
        if scheds:
            sample_sched_id = scheds[0].get("ObjectId", "")
    except Exception:
        pass
    try:
        data = api_get(session, host, "/vmrest/schedulesets", {"rowsPerPage": 1})
        ssets = data.get("ScheduleSet", [])
        if isinstance(ssets, dict):
            ssets = [ssets]
        if ssets:
            sample_schedset_id = ssets[0].get("ObjectId", "")
    except Exception:
        pass
    try:
        data = api_get(session, host, "/vmrest/handlers/directoryhandlers", {"rowsPerPage": 1})
        dhs = data.get("DirectoryHandler", [])
        if isinstance(dhs, dict):
            dhs = [dhs]
        if dhs:
            sample_dir_id = dhs[0].get("ObjectId", "")
    except Exception:
        pass
    try:
        data = api_get(session, host, "/vmrest/handlers/interviewhandlers", {"rowsPerPage": 1})
        ihs = data.get("InterviewHandler", [])
        if isinstance(ihs, dict):
            ihs = [ihs]
        if ihs:
            sample_ih_id = ihs[0].get("ObjectId", "")
    except Exception:
        pass

    print(f"\n{'='*70}")
    print(f"CUPI ENDPOINT PROBE — {host}")
    print(f"{'='*70}\n")

    available = []
    unavailable = []
    errors = []

    id_subs = {
        "{id}": sample_id,
        "{sched_id}": sample_sched_id,
        "{schedset_id}": sample_schedset_id,
        "{dir_id}": sample_dir_id,
        "{ih_id}": sample_ih_id,
    }

    for path, label in PROBE_ENDPOINTS:
        resolved = path
        skip = False
        for placeholder, val in id_subs.items():
            if placeholder in resolved:
                if not val:
                    unavailable.append((path, label, "skipped — no sample ID"))
                    skip = True
                    break
                resolved = resolved.replace(placeholder, val)
        if skip:
            continue

        try:
            url = f"{host}{resolved}"
            resp = session.get(url, params={"rowsPerPage": 1}, headers=HEADERS, verify=False)
            code = resp.status_code
            total = ""
            if code == 200:
                try:
                    data = resp.json()
                    t = data.get("@total", "")
                    if t:
                        total = f" ({t} records)"
                except Exception:
                    pass
                available.append((path, label, f"{code}{total}"))
            elif code == 404:
                unavailable.append((path, label, str(code)))
            else:
                errors.append((path, label, f"{code} {resp.reason}"))
        except requests.exceptions.ConnectionError:
            errors.append((path, label, "connection error"))
        except Exception as e:
            errors.append((path, label, str(e)))

    print(f"  AVAILABLE ({len(available)}):")
    for path, label, info in available:
        print(f"    ✓ {label:<35} {path:<60} {info}")

    if unavailable:
        print(f"\n  NOT FOUND ({len(unavailable)}):")
        for path, label, info in unavailable:
            print(f"    ✗ {label:<35} {path:<60} {info}")

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for path, label, info in errors:
            print(f"    ! {label:<35} {path:<60} {info}")

    print(f"\n{'='*70}")
    print(f"  {len(available)} available / {len(unavailable)} not found / {len(errors)} errors")
    print(f"{'='*70}")


def cmd_schedules(args):
    """List all schedules and their time blocks."""
    session, host = connect(args)

    try:
        schedules = fetch_schedules(session, host)
    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
        print(f"  Warning: Could not fetch schedules: {e}")
        schedules = []

    try:
        holiday_schedules = fetch_holiday_schedules(session, host)
    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
        print(f"  Warning: Could not fetch holiday schedules: {e}")
        holiday_schedules = []

    print(f"\n{'='*60}")
    print("BUSINESS HOUR SCHEDULES")
    print(f"{'='*60}")
    for s in schedules:
        print(f"\n  {s.get('DisplayName', 'Unknown')} ({s.get('ObjectId', '')})")
        details = s.get("_details", [])
        if not details:
            print("    All day, every day")
        for d in details:
            days = _active_days(d) or "?"
            start_time = _format_minutes(d.get("StartTime", ""))
            end_time = _format_minutes(d.get("EndTime", ""))
            active = str(d.get("IsActive", "true")).lower() == "true"
            print(f"    {days}: {start_time} - {end_time} {'(active)' if active else '(inactive)'}")

    print(f"\n{'='*60}")
    print("HOLIDAY SCHEDULES")
    print(f"{'='*60}")
    for s in holiday_schedules:
        print(f"\n  {s.get('DisplayName', 'Unknown')}")
        for h in s.get("_holidays", []):
            print(f"    {h.get('DisplayName', '?')}: {h.get('StartDate', '?')} - {h.get('EndDate', '?')}")


def cmd_orphans(args):
    """Find orphaned, unreachable, and dead-end call handlers."""
    session, host = connect(args)

    try:
        call_handlers = fetch_call_handlers(session, host)
        interview_handlers = fetch_interview_handlers(session, host)
        directory_handlers = fetch_directory_handlers(session, host)
        routing_rules = fetch_routing_rules(session, host)
    except requests.exceptions.ConnectionError as e:
        print(f"Error: Could not connect to {host}: {e}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"Error: API request failed: {e}")
        sys.exit(1)

    print("\nBuilding graph...")
    nodes, edges = build_graph(call_handlers, interview_handlers, routing_rules, session, host,
                               directory_handlers=directory_handlers)

    # Group by classification
    by_class = {}
    for n in nodes:
        if n["type"] in ("routingrule", "phone"):
            continue
        by_class.setdefault(n["classification"], []).append(n)

    total_handlers = sum(len(v) for v in by_class.values())
    node_map = {n["id"]: n for n in nodes}

    print(f"\n{'='*70}")
    print(f"ORPHAN ANALYSIS — {total_handlers} call/interview handlers")
    print(f"{'='*70}")

    # True orphans
    orphans = by_class.get("orphan", [])
    print(f"\n  TRUE ORPHANS ({len(orphans)}) — no connections at all:")
    if orphans:
        for n in sorted(orphans, key=lambda x: x["name"].lower()):
            ext = f" (ext {n['extension']})" if n["extension"] else ""
            print(f"    - {n['name']}{ext}")
    else:
        print("    (none)")

    # Unreachable
    unreachable = by_class.get("unreachable", [])
    print(f"\n  UNREACHABLE ({len(unreachable)}) — have edges but no path from any routing rule:")
    if unreachable:
        for n in sorted(unreachable, key=lambda x: x["name"].lower()):
            ext = f" (ext {n['extension']})" if n["extension"] else ""
            out_edges = [e for e in edges if e["source"] == n["id"]]
            in_edges = [e for e in edges if e["target"] == n["id"]]
            connections = []
            for e in out_edges:
                tgt = node_map.get(e["target"], {})
                connections.append(f"→ {tgt.get('name', '?')} [{e['label']}]")
            for e in in_edges:
                src = node_map.get(e["source"], {})
                connections.append(f"← {src.get('name', '?')} [{e['label']}]")
            print(f"    - {n['name']}{ext}")
            for c in connections:
                print(f"        {c}")
    else:
        print("    (none)")

    # Dead ends
    deadends = by_class.get("deadend", [])
    print(f"\n  DEAD ENDS ({len(deadends)}) — reachable but callers get stuck:")
    if deadends:
        for n in sorted(deadends, key=lambda x: x["name"].lower()):
            ext = f" (ext {n['extension']})" if n["extension"] else ""
            in_edges = [e for e in edges if e["target"] == n["id"]]
            via = ", ".join(f"{node_map.get(e['source'], {}).get('name', '?')} [{e['label']}]" for e in in_edges)
            print(f"    - {n['name']}{ext}")
            if via:
                print(f"        reached via: {via}")
    else:
        print("    (none)")

    # Schedule gaps
    schedule_gaps = [n for n in nodes
                     if n.get("reachable") and n["type"] == "callhandler"
                     and n["classification"] not in ("orphan", "unreachable")
                     and not (n["reachable"].get("standard") and n["reachable"].get("offhours") and n["reachable"].get("holiday"))]
    print(f"\n  SCHEDULE GAPS ({len(schedule_gaps)}) — reachable in some schedules but not all:")
    if schedule_gaps:
        for n in sorted(schedule_gaps, key=lambda x: x["name"].lower()):
            ext = f" (ext {n['extension']})" if n["extension"] else ""
            r = n["reachable"]
            missing = []
            if not r.get("standard"):
                missing.append("standard")
            if not r.get("offhours"):
                missing.append("off-hours")
            if not r.get("holiday"):
                missing.append("holiday")
            print(f"    - {n['name']}{ext}")
            print(f"        NOT reachable during: {', '.join(missing)}")
    else:
        print("    (none)")

    # Summary
    normal = len(by_class.get("normal", []))
    roots = len(by_class.get("root", []))
    print(f"\n{'='*70}")
    print(f"  Roots: {roots}  |  Normal: {normal}  |  Orphans: {len(orphans)}  |  "
          f"Unreachable: {len(unreachable)}  |  Dead Ends: {len(deadends)}  |  Schedule Gaps: {len(schedule_gaps)}")
    print(f"{'='*70}")


def main():
    parser = argparse.ArgumentParser(
        description="CUC Call Handler Wizard — CUPI routing visualizer and debug tool."
    )
    parser.add_argument("--host", required=True, help="CUC server URL (e.g. https://10.212.111.17)")
    parser.add_argument("--user", required=True, help="CUC admin username")

    subparsers = parser.add_subparsers(dest="command")

    # generate (default)
    sub_gen = subparsers.add_parser("generate", help="Generate HTML report and graph (default)")

    # query — raw API path
    sub_query = subparsers.add_parser("query", help="Query a raw CUPI API path and dump JSON")
    sub_query.add_argument("path", help="API path (e.g. /vmrest/handlers/callhandlers)")

    # handler — lookup a specific handler
    sub_handler = subparsers.add_parser("handler", help="Look up a call handler by name, extension, or ID")
    sub_handler.add_argument("search", help="Handler name, extension, or Object ID to search for")
    sub_handler.add_argument("--raw", action="store_true", help="Also dump raw JSON for the handler")

    # schedules — list all schedules
    sub_sched = subparsers.add_parser("schedules", help="List all schedules and holiday schedules")

    # orphans — find unreachable handlers
    sub_orphans = subparsers.add_parser("orphans", help="Find orphaned, unreachable, and dead-end handlers")

    # probe — test what endpoints exist
    sub_probe = subparsers.add_parser("probe", help="Probe CUPI endpoints to see what's available on this server")

    # audio — probe greeting audio files
    sub_audio = subparsers.add_parser("audio", help="Probe all handlers for uploaded greeting audio files")

    args = parser.parse_args()

    if args.command is None or args.command == "generate":
        cmd_generate(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "handler":
        cmd_handler(args)
    elif args.command == "schedules":
        cmd_schedules(args)
    elif args.command == "orphans":
        cmd_orphans(args)
    elif args.command == "probe":
        cmd_probe(args)
    elif args.command == "audio":
        cmd_audio_probe(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted. Exiting.")
        sys.exit(0)
