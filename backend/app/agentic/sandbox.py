"""Controlled Python execution for agent-generated code.

Layers (defence in depth, NOT a security boundary against a determined attacker — see docs/agentic.md):
 1. AST allow-list in the parent: only simple statements/expressions; imports limited to pure-compute modules; no dunder/`_private`
    attribute access, no exec/eval/open/getattr/..., no str.format (format-string attribute walks).
 2. A separate interpreter process (`-I`, empty environment, private temp cwd, new session) with RLIMIT_CPU / RLIMIT_AS / RLIMIT_FSIZE /
    RLIMIT_NOFILE / RLIMIT_NPROC, restricted builtins and an allow-listed `__import__`.
 3. Wall-clock timeout that kills the whole process group; stdout/stderr capped.
Deploy-time hardening (container without network, read-only FS, gVisor/Firecracker) is still required for untrusted multi-tenant use.
"""
from __future__ import annotations

import ast
import asyncio
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass

ALLOWED_MODULES = frozenset({"math", "statistics", "json", "re", "datetime", "decimal", "fractions", "itertools", "functools", "collections",
                             "string", "random", "operator", "heapq", "bisect", "textwrap"})
FORBIDDEN_NAMES = frozenset({"eval", "exec", "compile", "open", "__import__", "getattr", "setattr", "delattr", "globals", "locals", "vars", "input",
                             "breakpoint", "memoryview", "type", "super", "object", "help", "exit", "quit", "dir", "classmethod", "staticmethod", "property",
                             "__builtins__", "__loader__", "__spec__"})
FORBIDDEN_ATTRS = frozenset({"format", "format_map", "mro", "gi_frame", "gi_code", "cr_frame", "f_globals", "f_locals", "f_back", "tb_frame"})
MAX_CODE_CHARS = 8000
_ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For, ast.While, ast.If, ast.Break, ast.Continue, ast.Pass, ast.Return,
    ast.FunctionDef, ast.Lambda, ast.arguments, ast.arg, ast.Call, ast.keyword, ast.Name, ast.Load, ast.Store, ast.Del, ast.Constant, ast.JoinedStr,
    ast.FormattedValue, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Attribute, ast.Subscript, ast.Slice, ast.Starred, ast.List,
    ast.Tuple, ast.Dict, ast.Set, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension, ast.Import, ast.ImportFrom, ast.alias,
    ast.Try, ast.ExceptHandler, ast.Raise, ast.Assert, ast.Delete, ast.With, ast.withitem, ast.Global, ast.Nonlocal,
    ast.operator, ast.unaryop, ast.boolop, ast.cmpop, ast.expr_context,
)


class SandboxViolation(ValueError):
    pass


def validate_code(code: str) -> ast.Module:
    if len(code) > MAX_CODE_CHARS:
        raise SandboxViolation(f"Code exceeds {MAX_CODE_CHARS} characters")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxViolation(f"Syntax error: {exc.msg} (line {exc.lineno})") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise SandboxViolation(f"Syntax not allowed: {type(node).__name__}")
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_MODULES:
                    raise SandboxViolation(f"Import not allowed: {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_MODULES:
                raise SandboxViolation(f"Import not allowed: {node.module}")
        elif isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or (node.id.startswith("__") and node.id.endswith("__"))):
            raise SandboxViolation(f"Name not allowed: {node.id}")
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in FORBIDDEN_ATTRS):
            raise SandboxViolation(f"Attribute not allowed: {node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and ("__" in node.value):
            raise SandboxViolation("Strings containing '__' are not allowed")   # blocks getattr-by-string style probes
    return tree


# Executed inside the child interpreter. Kept as source so the child needs nothing from the application.
_CHILD = r'''
import sys, resource, builtins, io, json
cpu, mem, out_cap = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
allowed = set(sys.argv[4].split(","))
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
resource.setrlimit(resource.RLIMIT_AS, (mem << 20, mem << 20))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
try:
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
except (ValueError, OSError):
    pass
code = sys.stdin.read()
real_import = builtins.__import__
def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name.split(".")[0] not in allowed:
        raise ImportError("import not allowed: " + name)
    return real_import(name, globals, locals, fromlist, level)
SAFE = ["abs","all","any","bin","bool","bytes","chr","complex","dict","divmod","enumerate","filter","float","frozenset","hash","hex","int","isinstance",
        "issubclass","iter","len","list","map","max","min","next","oct","ord","pow","print","range","repr","reversed","round","set","slice","sorted",
        "str","sum","tuple","zip","ArithmeticError","AssertionError","Exception","IndexError","KeyError","LookupError","StopIteration","TypeError",
        "ValueError","ZeroDivisionError","True","False","None"]
env = {k: getattr(builtins, k) for k in SAFE}
env["__import__"] = safe_import
buf = io.StringIO()
class Cap(io.StringIO):
    def write(self, s):
        if buf.tell() + len(s) > out_cap:
            raise MemoryError("output limit exceeded")
        return buf.write(s)
sys.stdout = Cap()
status = 0
try:
    exec(compile(code, "<agent>", "exec"), {"__builtins__": env, "__name__": "agent"})
except BaseException as e:
    status = 1
    sys.stdout = sys.__stdout__
    sys.stderr.write(type(e).__name__ + ": " + str(e)[:500])
sys.stdout = sys.__stdout__
sys.stdout.write(buf.getvalue())
sys.exit(status)
'''


@dataclass(slots=True)
class SandboxResult:
    ok: bool
    stdout: str
    error: str | None
    duration_ms: float
    killed: str | None = None      # timeout | memory | output | signal


async def run_python(code: str, *, timeout: float = 5.0, memory_mb: int = 256, output_chars: int = 8000) -> SandboxResult:
    validate_code(code)
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="nexus-sbx-") as cwd:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-S", "-c", _CHILD, str(max(1, int(timeout) + 1)), str(memory_mb), str(output_chars), ",".join(sorted(ALLOWED_MODULES)),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd, env={}, start_new_session=True)
        killed = None
        try:
            out, err = await asyncio.wait_for(proc.communicate(code.encode()), timeout)
        except asyncio.TimeoutError:
            killed = "timeout"
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = await proc.communicate()
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    ms = (time.perf_counter() - t0) * 1000
    text, etext = out.decode(errors="replace")[:output_chars], err.decode(errors="replace")[:600]
    if killed is None and proc.returncode is not None and proc.returncode < 0:
        killed = "memory" if "MemoryError" in etext else "signal"
    if "MemoryError" in etext:
        killed = killed or ("output" if "output limit" in etext else "memory")
    ok = proc.returncode == 0 and killed is None
    return SandboxResult(ok, text, None if ok else (etext or f"terminated ({killed})"), round(ms, 1), killed)
