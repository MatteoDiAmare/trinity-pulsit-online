"""Static policy for strategy source code that Saba writes herself (first line of defence).

The code runs later in an isolated worker (see worker.py), but it is rejected up front if it steps outside a small
closed subset of Python. This is a deny-by-default allowlist of syntax: anything not listed is refused.
"""
from __future__ import annotations

import ast

MAX_SOURCE_BYTES = 40_000
ALLOWED_IMPORTS = {"math", "statistics", "numpy"}       # `numpy` is mapped to a curated, file-free subset
ALLOWED_NODES = {
    "Module", "FunctionDef", "ClassDef", "Return", "Assign", "AugAssign", "AnnAssign", "For", "While", "If", "Expr", "Pass",
    "Break", "Continue", "Import", "ImportFrom", "alias", "Try", "ExceptHandler", "Raise", "Assert",
    "BoolOp", "BinOp", "UnaryOp", "Lambda", "IfExp", "Dict", "Set", "ListComp", "SetComp", "DictComp", "GeneratorExp",
    "Compare", "Call", "Constant", "Attribute", "Subscript", "Starred", "Name", "List", "Tuple", "Slice", "comprehension",
    "arguments", "arg", "keyword", "NamedExpr", "JoinedStr", "FormattedValue",
    "Load", "Store", "Del", "And", "Or", "Add", "Sub", "Mult", "Div", "FloorDiv", "Mod", "Pow", "USub", "UAdd", "Not", "Invert",
    "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot", "In", "NotIn", "BitAnd", "BitOr", "BitXor", "LShift", "RShift",
}
# attributes that open a way out even though they do not start with an underscore
DENIED_ATTRS = {
    "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "f_back", "f_globals", "f_locals", "f_builtins", "f_code", "tb_frame",
    "tb_next", "co_code", "mro", "format", "format_map", "load", "loads", "dump", "dumps", "tofile", "fromfile", "memmap",
    "ctypes", "data", "base", "random", "testing", "f2py", "distutils", "ctypeslib", "lib", "os", "sys", "system", "popen",
    "write", "read", "open", "save", "savetxt", "loadtxt", "genfromtxt", "fromstring", "frombuffer", "buffer", "getbuffer",
}
DENIED_NAMES = {"eval", "exec", "compile", "open", "input", "breakpoint", "getattr", "setattr", "delattr", "hasattr", "globals", "locals",
                "vars", "dir", "type", "object", "super", "memoryview", "bytearray", "bytes", "classmethod", "staticmethod", "property",
                "help", "exit", "quit", "id", "hash", "callable", "iter", "next", "print_exception"}


class PolicyError(ValueError):
    pass


def validate_code(source: str) -> ast.Module:
    if not isinstance(source, str) or not source.strip():
        raise PolicyError("strategy source must be non-empty text")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise PolicyError(f"strategy source too large (max {MAX_SOURCE_BYTES} bytes)")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise PolicyError(f"syntax error: {exc.msg} (line {exc.lineno})") from None
    strategy_classes = 0
    for node in ast.walk(tree):
        kind = type(node).__name__
        line = getattr(node, "lineno", "?")
        if kind not in ALLOWED_NODES:
            raise PolicyError(f"{kind} is not allowed (line {line})")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in DENIED_ATTRS:
                raise PolicyError(f"attribute '{node.attr}' is not allowed (line {line})")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") or node.id in DENIED_NAMES:
                raise PolicyError(f"name '{node.id}' is not allowed (line {line})")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if isinstance(node, ast.ImportFrom) and (node.level or any(a.name.startswith("_") for a in node.names)):
                raise PolicyError(f"relative or private import is not allowed (line {line})")
            for name in names:
                if name not in ALLOWED_IMPORTS:
                    raise PolicyError(f"import of '{name}' is not allowed; allowed: {sorted(ALLOWED_IMPORTS)} (line {line})")
        elif isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                raise PolicyError(f"decorators are not allowed (line {line})")
            if node.name.startswith("_") and node.name != "__init__":
                raise PolicyError(f"function name '{node.name}' is not allowed (line {line})")
        elif isinstance(node, ast.ClassDef):
            if node.bases or node.keywords or node.decorator_list:
                raise PolicyError(f"classes may not use bases, metaclasses or decorators (line {line})")
            if node.name.startswith("_"):
                raise PolicyError(f"class name '{node.name}' is not allowed (line {line})")
            strategy_classes += node.name == "Strategy"
        elif isinstance(node, ast.arg):
            if node.arg.startswith("__"):
                raise PolicyError(f"argument name '{node.arg}' is not allowed (line {line})")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "format":
            raise PolicyError(f"format() is not allowed (line {line})")
    if strategy_classes != 1:
        raise PolicyError("define exactly one class named Strategy with on_bar(self, view, position)")
    return tree
