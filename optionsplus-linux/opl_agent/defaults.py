"""Fill proto3 default values into outgoing proto-JSON payloads.

The real agent prints every scalar, enum, repeated and map field even when it
holds its default value, and the UI relies on that (for example the splash
screen waits until `analytics.enabled` exists). Handlers here only set the
fields they care about; `fill()` adds the rest from the schema table.
"""

import copy

from .schema_defaults import TABLE

PREFIX = "type.googleapis.com/"


def _fill_msg(obj, mtype):
    row = TABLE.get(mtype)
    if row is None or not isinstance(obj, dict):
        return obj
    for name, (kind, val) in row.items():
        if kind == "default":
            if name not in obj:
                obj[name] = copy.copy(val)
        elif kind == "message":
            if isinstance(obj.get(name), dict):
                _fill_msg(obj[name], val)
        elif kind == "repeated_message":
            cur = obj.get(name)
            if cur is None:
                obj[name] = []
            elif isinstance(cur, list):
                for it in cur:
                    _fill_msg(it, val)
    return obj


def fill(payload):
    """Fill defaults in an Any payload (a dict carrying "@type")."""
    if isinstance(payload, dict):
        t = payload.get("@type", "")
        if t.startswith(PREFIX):
            _fill_msg(payload, t[len(PREFIX):])
    return payload
