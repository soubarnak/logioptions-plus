#!/usr/bin/env python3
"""Generate opl_agent/schema_defaults.py from the recovered .proto schema.

The real agent serialises replies with proto3 JSON "print fields with no
presence" semantics: every scalar, repeated and map field is emitted with its
default value even when unset. The Options+ UI depends on this (it checks
`exists(x.enabled)` etc.), so the native agent fills defaults the same way.

The generated table holds, per message type, each field's JSON name and either
its default value or the nested message type name. It contains no vendor data,
only interface field names.

usage: protodefaults.py <proto-root> <out.py>
"""
import os
import pprint
import re
import sys

SCALAR_DEFAULTS = {
    "bool": False, "string": "", "bytes": "",
    "int32": 0, "uint32": 0, "sint32": 0, "fixed32": 0, "sfixed32": 0,
    "float": 0, "double": 0,
    # proto3 JSON prints 64-bit integers as strings
    "int64": "0", "uint64": "0", "sint64": "0", "fixed64": "0", "sfixed64": "0",
}

TOK = re.compile(r'"[^"]*"|[A-Za-z_][\w.]*|-?\d+|[{}=;<>,\[\]()]')


def camel(name):
    parts = name.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def parse_file(text, messages, enums):
    text = re.sub(r"//[^\n]*", "", text)
    toks = TOK.findall(text)
    pkg = ""
    i = 0
    scope = []          # stack of (kind, fullname)

    def cur():
        return scope[-1][1] if scope else pkg

    while i < len(toks):
        t = toks[i]
        if t == "package":
            pkg = toks[i + 1]
            i += 3
            continue
        if t in ("syntax", "import", "option"):
            while toks[i] != ";":
                i += 1
            i += 1
            continue
        if t in ("message", "enum", "oneof", "service") and toks[i + 2] == "{":
            name = toks[i + 1]
            full = (cur() + "." + name) if t in ("message", "enum") else cur()
            if t == "message":
                messages.setdefault(full, [])
            if t == "enum":
                enums[full] = None
            scope.append((t, full))
            i += 3
            continue
        if t == "}":
            scope.pop()
            i += 1
            continue
        if t == "reserved" or t == "extensions":
            while toks[i] != ";":
                i += 1
            i += 1
            continue
        if not scope:
            i += 1
            continue
        kind, full = scope[-1]
        if kind == "enum":
            # NAME = number ;
            if toks[i + 1] == "=" and enums[full] is None and toks[i + 2] == "0":
                enums[full] = t
            while toks[i] != ";":
                i += 1
            i += 1
            continue
        if kind in ("message", "oneof"):
            msg = full
            label = ""
            if t in ("repeated", "optional", "required"):
                label = t
                i += 1
                t = toks[i]
            if t == "map":
                # map < K , V > name = n ;
                vtype = toks[i + 4]
                fname = toks[i + 6]
                messages[msg].append((camel(fname), "map", vtype, scope_names(scope)))
                while toks[i] != ";":
                    i += 1
                i += 1
                continue
            ftype, fname = t, toks[i + 1]
            if toks[i + 2] != "=":
                i += 1
                continue
            messages[msg].append((camel(fname), "repeated" if label == "repeated" else
                                  ("oneof" if kind == "oneof" else "single"),
                                  ftype, scope_names(scope)))
            while toks[i] != ";":
                i += 1
            i += 1
            continue
        i += 1
    return pkg


def scope_names(scope):
    return [s[1] for s in scope if s[0] in ("message",)]


def resolve(tname, ctx, messages, enums):
    """Resolve a type reference against the enclosing scopes."""
    if tname in SCALAR_DEFAULTS:
        return "scalar", tname
    cands = [tname]
    for c in reversed(ctx):
        cands.insert(0, c + "." + tname)
        parts = c.split(".")
        for k in range(len(parts) - 1, 0, -1):
            cands.append(".".join(parts[:k]) + "." + tname)
    for c in cands:
        if c in messages:
            return "message", c
        if c in enums:
            return "enum", c
    return "unknown", tname


def main():
    root, out = sys.argv[1], sys.argv[2]
    messages, enums = {}, {}
    for d, _, fs in os.walk(root):
        for f in fs:
            if f.endswith(".proto"):
                parse_file(open(os.path.join(d, f), encoding="utf-8").read(), messages, enums)
    table = {}
    for msg, fields in messages.items():
        row = {}
        for jname, kind, ftype, ctx in fields:
            if kind == "repeated":
                row[jname] = ("default", [])
                rk, rt = resolve(ftype, ctx, messages, enums)
                if rk == "message":
                    row[jname] = ("repeated_message", rt)
                continue
            if kind == "map":
                row[jname] = ("default", {})
                continue
            rk, rt = resolve(ftype, ctx, messages, enums)
            if rk == "scalar" and kind != "oneof":
                row[jname] = ("default", SCALAR_DEFAULTS[rt])
            elif rk == "enum" and kind != "oneof":
                row[jname] = ("default", enums[rt] or 0)
            elif rk == "message":
                row[jname] = ("message", rt)   # not printed when unset
        table[msg] = row
    with open(out, "w") as fh:
        fh.write('"""Generated by tools/protodefaults.py - do not edit.\n\n'
                 'Per message type: JSON field name -> (kind, default or nested type).\n"""\n\n')
        fh.write("TABLE = ")
        fh.write(pprint.pformat(table, width=120, compact=True))
        fh.write("\n")
    print(f"{len(table)} messages, {len(enums)} enums -> {out}")


if __name__ == "__main__":
    main()
