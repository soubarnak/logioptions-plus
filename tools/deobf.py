#!/usr/bin/env python3
"""Static string-array decoder for javascript-obfuscator output (plain array variant).

Usage: deobf.py <in.min.js> <out.js>

Does not execute the input. Reconstructs the rotated string array by evaluating the
rotation checksum expression in Python, then inlines every `alias(0xNNN)` call and
folds adjacent 'a'+'b' literal concatenations.
"""
import ast
import re
import sys


def js_parse_int(s):
    m = re.match(r"\s*([+-]?\d+)", s)
    return int(m.group(1)) if m else None  # None ~ NaN


def parse_js_string_array(src):
    """Parse a JS array literal of single-quoted strings."""
    out = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c in "'\"":
            q = c
            j = i + 1
            buf = []
            while src[j] != q:
                if src[j] == "\\":
                    buf.append(src[j:j + 2] if src[j + 1] != "x" and src[j + 1] != "u" else
                               src[j:j + (4 if src[j + 1] == "x" else 6)])
                    j += len(buf[-1])
                else:
                    buf.append(src[j])
                    j += 1
            raw = "".join(buf)
            out.append(ast.literal_eval("'" + raw.replace("'", "\\'").replace("\\\\'", "\\'") + "'"))
            i = j + 1
        else:
            i += 1
    return out


def main():
    code = open(sys.argv[1], encoding="utf-8").read()
    # decoder: function NAME(a,b){var c=ARRFN();return NAME=function(d,e){d=d-OFF;...
    m = re.search(r"function (\w+)\(\w+,\w+\)\{var \w+=(\w+)\(\);return \1=function\(\w+,\w+\)\{\w+=\w+-(0x[0-9a-f]+);", code)
    dec, arrfn, off = m.group(1), m.group(2), int(m.group(3), 16)
    am = re.search(r"function " + re.escape(arrfn) + r"\(\)\{var \w+=\[", code)
    start = am.end() - 1
    # find matching ']' for the array literal (strings may contain brackets)
    i, depth, q = start, 0, None
    while True:
        ch = code[i]
        if q:
            if ch == "\\":
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    arr = parse_js_string_array(code[start + 1:i])
    # rotation: (function(x,y){...while(!![]){try{var z=EXPR;if(z===y)break;...}}(ARRFN,TARGET))
    rm = re.search(r"try\{var \w+=(.*?);if\(\w+===\w+\)break;.*?\}\(" + re.escape(arrfn) + r",(0x[0-9a-f]+)\)\)", code)
    expr, target = rm.group(1), int(rm.group(2), 16)
    terms = re.sub(r"parseInt\(\w+\((0x[0-9a-f]+)\)\)", r"P(\1)", expr)

    def evaluate(a):
        def P(k):
            v = js_parse_int(a[k - off])
            if v is None:
                raise ValueError
            return v
        return eval(terms.replace("/", "/"), {"P": P})  # noqa: S307 - terms built from numeric literals only
    if not re.fullmatch(r"[P()0-9a-fx+\-*/ ]+", terms):
        raise SystemExit("unexpected rotation expression: " + terms[:200])
    for _ in range(len(arr)):
        try:
            if evaluate(arr) == target:
                break
        except (ValueError, ZeroDivisionError, IndexError):
            pass
        arr.append(arr.pop(0))
    else:
        raise SystemExit("rotation not found")

    # aliases: any identifier assigned directly from the decoder or from another alias
    aliases = {dec}
    changed = True
    while changed:
        changed = False
        for a, b in re.findall(r"(\w+)=(\w+)[,;]", code):
            if b in aliases and a not in aliases:
                aliases.add(a)
                changed = True

    def rep(mm):
        name, k = mm.group(1), int(mm.group(2), 16)
        if name not in aliases:
            return mm.group(0)
        idx = k - off
        if 0 <= idx < len(arr):
            return repr(arr[idx])
        return mm.group(0)
    code = re.sub(r"\b(\w+)\((0x[0-9a-f]+)\)", rep, code)
    # fold 'a'+'b'
    lit = r"'(?:[^'\\]|\\.)*'"
    pat = re.compile(r"(" + lit + r")\+(" + lit + r")")
    for _ in range(20):
        new = pat.sub(lambda mm: repr(ast.literal_eval(mm.group(1)) + ast.literal_eval(mm.group(2))), code)
        if new == code:
            break
        code = new
    open(sys.argv[2], "w", encoding="utf-8").write(code)
    print(f"decoder={dec} array={len(arr)} aliases={len(aliases)}")


if __name__ == "__main__":
    main()
