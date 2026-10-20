#!/usr/bin/env python3
"""Recover .proto source from serialized FileDescriptorProto blobs embedded in a binary.

Usage: protodump.py <binary> <outdir>

No dependencies. Scans for `0x0a <len> <name>.proto 0x12`, parses the
FileDescriptorProto greedily until a field outside the known schema appears,
and renders proto2/proto3 text.
"""
import os
import re
import sys

# --- minimal protobuf wire decoding -------------------------------------------------


def varint(b, p):
    r = s = 0
    while True:
        c = b[p]
        p += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, p
        s += 7
        if s > 63:
            raise ValueError("varint")


def fields(b, allowed=None):
    """Yield (field, wiretype, value) until end or an unexpected field."""
    p = 0
    n = len(b)
    while p < n:
        key, q = varint(b, p)
        f, wt = key >> 3, key & 7
        if f == 0 or (allowed is not None and f not in allowed):
            return
        if wt == 0:
            v, q = varint(b, q)
        elif wt == 2:
            ln, q = varint(b, q)
            if q + ln > n:
                return
            v = b[q:q + ln]
            q += ln
        elif wt == 5:
            v = int.from_bytes(b[q:q + 4], "little")
            q += 4
        elif wt == 1:
            v = int.from_bytes(b[q:q + 8], "little")
            q += 8
        else:
            return
        yield f, wt, v
        p = q


def consumed(b, allowed):
    """Return number of bytes of b that parse as a message with the allowed top-level fields."""
    p = 0
    n = len(b)
    while p < n:
        try:
            key, q = varint(b, p)
        except (IndexError, ValueError):
            break
        f, wt = key >> 3, key & 7
        if f not in allowed:
            break
        try:
            if wt == 0:
                _, q = varint(b, q)
            elif wt == 2:
                ln, q = varint(b, q)
                if q + ln > n:
                    break
                q += ln
            else:
                break
        except (IndexError, ValueError):
            break
        p = q
    return p


# --- descriptor rendering -----------------------------------------------------------

TYPES = {1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32", 6: "fixed64",
         7: "fixed32", 8: "bool", 9: "string", 12: "bytes", 13: "uint32", 15: "sfixed32",
         16: "sfixed64", 17: "sint32", 18: "sint64"}
LABELS = {1: "optional", 2: "required", 3: "repeated"}


def s(v):
    return v.decode("utf-8", "replace")


def field_desc(b):
    d = {}
    for f, _, v in fields(b):
        if f == 1: d["name"] = s(v)
        elif f == 3: d["number"] = v
        elif f == 4: d["label"] = v
        elif f == 5: d["type"] = v
        elif f == 6: d["type_name"] = s(v)
        elif f == 7: d["default"] = s(v)
        elif f == 9: d["oneof"] = v
        elif f == 10: d["json_name"] = s(v)
        elif f == 17: d["proto3_optional"] = v
    return d


def render_field(fd, syntax, nested_maps, ind):
    t = TYPES.get(fd.get("type")) or fd.get("type_name", "?").lstrip(".")
    lab = fd.get("label", 1)
    tn = fd.get("type_name", "")
    short = tn.rsplit(".", 1)[-1]
    if lab == 3 and short in nested_maps:
        k, v = nested_maps[short]
        return f"{ind}map<{k}, {v}> {fd['name']} = {fd['number']};"
    pre = ""
    if lab == 3:
        pre = "repeated "
    elif lab == 2:
        pre = "required "
    elif syntax != "proto3" or fd.get("proto3_optional"):
        pre = "optional " if "oneof" not in fd or fd.get("proto3_optional") else ""
    extra = f" [default = {fd['default']}]" if "default" in fd else ""
    return f"{ind}{pre}{t} {fd['name']} = {fd['number']}{extra};"


def render_enum(b, ind):
    name = ""
    vals = []
    for f, _, v in fields(b):
        if f == 1:
            name = s(v)
        elif f == 2:
            vn = vnum = None
            for g, _, w in fields(v):
                if g == 1: vn = s(w)
                elif g == 2: vnum = w if w < 2**31 else w - 2**64
            vals.append((vn, vnum))
    out = [f"{ind}enum {name} {{"]
    out += [f"{ind}  {n} = {x};" for n, x in vals]
    out.append(f"{ind}}}")
    return out


def render_msg(b, syntax, ind=""):
    name = ""
    flds, nested, enums, oneofs = [], [], [], []
    for f, _, v in fields(b):
        if f == 1: name = s(v)
        elif f == 2: flds.append(field_desc(v))
        elif f == 3: nested.append(v)
        elif f == 4: enums.append(v)
        elif f == 8:
            on = ""
            for g, _, w in fields(v):
                if g == 1: on = s(w)
            oneofs.append(on)
    # detect map entry types
    maps = {}
    real_nested = []
    for nb in nested:
        nname, is_map, kv = "", False, {}
        for g, _, w in fields(nb):
            if g == 1: nname = s(w)
            elif g == 7:
                for h, _, x in fields(w):
                    if h == 7 and x: is_map = True
            elif g == 2:
                fd = field_desc(w)
                kv[fd["number"]] = TYPES.get(fd.get("type")) or fd.get("type_name", "?").lstrip(".")
        if is_map:
            maps[nname] = (kv.get(1), kv.get(2))
        else:
            real_nested.append(nb)
    out = [f"{ind}message {name} {{"]
    ii = ind + "  "
    for nb in real_nested:
        out += render_msg(nb, syntax, ii)
    for eb in enums:
        out += render_enum(eb, ii)
    done_oneof = set()
    for fd in flds:
        oi = fd.get("oneof")
        if oi is not None and not fd.get("proto3_optional"):
            if oi in done_oneof:
                continue
            done_oneof.add(oi)
            out.append(f"{ii}oneof {oneofs[oi] if oi < len(oneofs) else 'oneof_' + str(oi)} {{")
            for g in flds:
                if g.get("oneof") == oi and not g.get("proto3_optional"):
                    out.append(render_field(g, syntax, maps, ii + "  "))
            out.append(f"{ii}}}")
        else:
            out.append(render_field(fd, syntax, maps, ii))
    out.append(f"{ind}}}")
    return out


def render_service(b):
    name = ""
    meths = []
    for f, _, v in fields(b):
        if f == 1: name = s(v)
        elif f == 2:
            m = {}
            for g, _, w in fields(v):
                if g == 1: m["n"] = s(w)
                elif g == 2: m["i"] = s(w).lstrip(".")
                elif g == 3: m["o"] = s(w).lstrip(".")
            meths.append(m)
    out = [f"service {name} {{"]
    out += [f"  rpc {m.get('n')}({m.get('i')}) returns ({m.get('o')});" for m in meths]
    out.append("}")
    return out


FDP_FIELDS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14}


def render_file(b):
    name = pkg = ""
    syntax = "proto2"
    deps, msgs, enums, svcs = [], [], [], []
    for f, _, v in fields(b, FDP_FIELDS):
        if f == 1: name = s(v)
        elif f == 2: pkg = s(v)
        elif f == 3: deps.append(s(v))
        elif f == 4: msgs.append(v)
        elif f == 5: enums.append(v)
        elif f == 6: svcs.append(v)
        elif f == 12: syntax = s(v)
    out = [f'syntax = "{syntax}";', ""]
    if pkg:
        out += [f"package {pkg};", ""]
    out += [f'import "{d}";' for d in deps]
    if deps:
        out.append("")
    for e in enums:
        out += render_enum(e, "") + [""]
    for m in msgs:
        out += render_msg(m, syntax) + [""]
    for sv in svcs:
        out += render_service(sv) + [""]
    return name, "\n".join(out)


def main():
    data = open(sys.argv[1], "rb").read()
    outdir = sys.argv[2]
    seen = {}
    for m in re.finditer(rb"\x0a([\x05-\x7f])([A-Za-z0-9_/]+\.proto)\x12", data):
        if m.group(1)[0] != len(m.group(2)):
            continue
        start = m.start()
        blob = data[start:start + 4_000_000]
        n = consumed(blob, FDP_FIELDS)
        if n < 32:
            continue
        name, text = render_file(blob[:n])
        if name in seen and len(seen[name]) >= len(text):
            continue
        seen[name] = text
    for name, text in sorted(seen.items()):
        dst = os.path.join(outdir, name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w") as fh:
            fh.write(text)
    print(f"{len(seen)} proto files written to {outdir}")


if __name__ == "__main__":
    main()
