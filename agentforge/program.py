"""An interpreter for a bounded Python subset. Generated code is never exec'd."""

import ast
import copy
import json
import math

from .contracts import ExecutionError


MAX_TEXT = 120000
MAX_ITEMS = 10000
HOOK_BUDGET = 50000
TOOL_BUDGET = 200000
TOOL_MAX_TEXT = 600000
TOOL_MAX_ITEMS = 20000
MAX_SOURCE = 16000
MAX_AST_NODES = 2500
HELPERS = {"len", "str", "int", "float", "bool", "round", "abs", "min", "max", "sum",
            "get", "append", "join", "split", "lower", "upper", "sorted", "is_object", "is_array", "is_string"}
HELPER_ARITY = {"len": (1, 1), "str": (0, 1), "int": (0, 2), "float": (0, 1), "bool": (0, 1),
                "round": (1, 2), "abs": (1, 1), "min": (1, None), "max": (1, None), "sum": (1, 2),
                "get": (2, 3), "append": (2, 2), "join": (1, 2), "split": (1, 2),
                "lower": (1, 1), "upper": (1, 1), "sorted": (1, 1), "is_object": (1, 1),
                "is_array": (1, 1), "is_string": (1, 1), "dataset": (1, 1), "document": (1, 1), "observation": (1, 1)}
ALLOWED = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
           ast.If, ast.For, ast.Expr, ast.Pass, ast.Break, ast.Continue, ast.Name,
           ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Subscript,
           ast.Slice, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call, ast.IfExp,
           ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd,
           ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
            ast.In, ast.NotIn, ast.Is, ast.IsNot)


def interpreter_capabilities():
    """The same allowlists used by the interpreter, shared with design and repair."""
    return {"entry": "def run(context)", "ast_nodes": sorted(n.__name__ for n in ALLOWED),
            "helpers": sorted(HELPERS), "resource_helpers": ["dataset", "document", "observation"],
            "helper_arity": {k: {"minimum": low, "maximum": high} for k, (low, high) in HELPER_ARITY.items()},
            "helper_signatures": {"get": "get(object, key, default=None)", "append": "append(array, value) -> None",
                                  "join": "join(array_of_strings, separator='')", "split": "split(string, separator=None)",
                                  "sorted": "sorted(iterable): natural order only, no key/reverse keywords",
                                  "dataset": "dataset(authorized_id) -> {id,name,columns,rows}; cells are strings",
                                  "document": "document(authorized_id) -> {id,title,content}",
                                  "observation": "observation(existing_id) -> observation record"},
            "forbidden": ["imports", "attributes/methods", "comprehensions", "f-strings", "while", "try",
                          "nested functions", "keyword arguments", "regex/date libraries", "filesystem/network/process"],
            "limits": {"source_characters": MAX_SOURCE, "ast_nodes": MAX_AST_NODES,
                       "hook": {"steps": HOOK_BUDGET, "json_characters": MAX_TEXT, "iteration_items": MAX_ITEMS},
                       "tool": {"steps": TOOL_BUDGET, "json_characters": TOOL_MAX_TEXT, "iteration_items": TOOL_MAX_ITEMS}},
            "design_guidance": "Use bounded loops, string splitting and explicit comparisons; semantic document reasoning belongs "
                               "to runtime Codex. Resource helpers are available only inside custom tools. "
                               "Choose compact JSON contracts and validate nested values. An algorithm must fit these limits; "
                               "use builtins/trusted tools or simplify tool boundaries when it cannot."}


def validate_execution_plan(plan, location):
    if (not isinstance(plan, dict) or set(plan) != {"algorithm", "helpers", "syntax"}
            or not isinstance(plan["algorithm"], str) or not plan["algorithm"].strip()):
        raise ExecutionError("DESIGN_CAPABILITY", "工具必须说明算法、所需 helper 和 AST 语法。", location,
                             origin="design", retryable=False)
    available = interpreter_capabilities()
    for key, allowed in (("helpers", {*HELPERS, *available["resource_helpers"]}),
                         ("syntax", set(available["ast_nodes"]))):
        needed = plan[key]
        if not isinstance(needed, list) or any(not isinstance(n, str) for n in needed):
            raise ExecutionError("DESIGN_CAPABILITY", key + " 必须是字符串列表。", location, origin="design", retryable=False)
        unsupported = set(needed) - allowed
        if unsupported:
            raise ExecutionError("DESIGN_CAPABILITY", "解释器不支持 " + key + "：" + ", ".join(sorted(unsupported)),
                                 location, origin="design", retryable=False)


def bounded(value, limit=MAX_TEXT):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > limit:
        raise ExecutionError("CODE_LIMIT", "代码数据超过大小预算。")
    return value


class Flow(Exception):
    def __init__(self, kind, value=None):
        self.kind, self.value = kind, value


class Program:
    def __init__(self, source, budget=HOOK_BUDGET, extra_helpers=None, max_text=MAX_TEXT, max_items=MAX_ITEMS):
        self.source, self.budget = source, budget
        self.helpers = dict(extra_helpers or {})
        if set(self.helpers) - {"dataset", "document", "observation"}:
            raise ExecutionError("CODE_INVALID", "扩展辅助函数不在资源访问范围。")
        self.max_text, self.max_items = max_text, max_items
        if not isinstance(source, str) or len(source) > MAX_SOURCE:
            raise ExecutionError("CODE_INVALID", "logic.py 最多 16000 字符。")
        try:
            tree = ast.parse(source, filename="logic.py")
        except SyntaxError as exc:
            raise ExecutionError("CODE_INVALID", exc.msg, f"logic.py:{exc.lineno}") from exc
        nodes = list(ast.walk(tree))
        if len(nodes) > MAX_AST_NODES or any(not isinstance(n, ALLOWED) for n in nodes):
            bad = next((n for n in nodes if not isinstance(n, ALLOWED)), tree)
            raise ExecutionError("CODE_INVALID", "不允许该 Python 语法：" + type(bad).__name__,
                                 f"logic.py:{getattr(bad, 'lineno', 1)}")
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            raise ExecutionError("CODE_INVALID", "只允许一个 def run(context) 函数。")
        fn = tree.body[0]
        args = fn.args
        if (fn.name != "run" or fn.decorator_list or fn.returns or args.defaults or args.kw_defaults
                or args.vararg or args.kwarg or args.kwonlyargs or args.posonlyargs
                or len(args.args) != 1 or args.args[0].arg != "context" or args.args[0].annotation):
            raise ExecutionError("CODE_INVALID", "入口必须是 def run(context)。")
        for node in nodes:
            if isinstance(node, ast.FunctionDef) and node is not fn:
                raise ExecutionError("CODE_INVALID", "不支持嵌套函数。")
            if isinstance(node, ast.Name) and (node.id.startswith("_") or len(node.id) > 80):
                raise ExecutionError("CODE_INVALID", "变量名无效。")
            if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name)
                    or node.func.id not in (HELPERS | set(self.helpers)) or node.keywords):
                raise ExecutionError("CODE_INVALID", "只能调用列出的纯数据辅助函数。", f"logic.py:{node.lineno}")
            if isinstance(node, ast.Call):
                low, high = HELPER_ARITY[node.func.id]
                if len(node.args) < low or high is not None and len(node.args) > high:
                    raise ExecutionError("CODE_INVALID", node.func.id + " 的参数数量不符合解释器签名。",
                                         f"logic.py:{node.lineno}")
            if isinstance(node, ast.Dict) and any(k is None for k in node.keys):
                raise ExecutionError("CODE_INVALID", "不支持字典展开。")
        self.body = fn.body

    def bound(self, value):
        try:
            return bounded(value, self.max_text)
        except ExecutionError as exc:
            raise ExecutionError(exc.code, str(exc), exc.location or f"logic.py:{getattr(self, 'line', 1)}",
                                 origin=exc.origin, retryable=exc.retryable, recovery=exc.recovery) from exc

    def tick(self, node):
        self.steps += 1
        self.line = getattr(node, "lineno", self.line)
        if self.steps > self.budget:
            raise ExecutionError("CODE_LIMIT", "代码超过解释执行步数预算。", f"logic.py:{self.line}")

    def run(self, context):
        self.steps, self.line = 0, 1
        self.env = {}
        try:
            try:
                self.env = {"context": copy.deepcopy(self.bound(context))}
            except ExecutionError as exc:
                # The hook can receive a legal tool result larger than its input budget.
                # No generated statement has executed; do not blame its source.
                raise ExecutionError(exc.code, str(exc), "logic.py:1", origin="runtime", retryable=False) from exc
            self.block(self.body)
        except Flow as flow:
            if flow.kind == "return":
                return self.bound(flow.value)
            raise ExecutionError("CODE_INVALID", "break/continue 必须位于循环内。")
        except ExecutionError as exc:
            if exc.location:
                raise
            raise ExecutionError(exc.code, str(exc), f"logic.py:{self.line}", origin=exc.origin,
                                 retryable=exc.retryable, recovery=exc.recovery) from exc
        except (ValueError, TypeError, AttributeError, KeyError, IndexError, ZeroDivisionError, OverflowError, RecursionError) as exc:
            raise ExecutionError("CODE_RUNTIME", str(exc)[:300], f"logic.py:{self.line}") from exc
        raise ExecutionError("CODE_RUNTIME", "run(context) 未返回响应。", f"logic.py:{self.line}")

    def block(self, statements):
        for node in statements:
            self.tick(node)
            if isinstance(node, ast.Return):
                raise Flow("return", self.expr(node.value) if node.value else None)
            if isinstance(node, ast.Assign):
                value = self.expr(node.value)
                for target in node.targets:
                    self.assign(target, value)
            elif isinstance(node, ast.If):
                self.block(node.body if self.expr(node.test) else node.orelse)
            elif isinstance(node, ast.For):
                values = self.expr(node.iter)
                if not isinstance(values, (list, tuple, dict, str)) or len(values) > self.max_items:
                    raise ExecutionError("CODE_LIMIT", "循环仅支持不超过 10000 项的数据集合。")
                interrupted = False
                for value in list(values):
                    self.tick(node)
                    self.assign(node.target, value)
                    try:
                        self.block(node.body)
                    except Flow as flow:
                        if flow.kind == "break":
                            interrupted = True
                            break
                        if flow.kind != "continue":
                            raise
                if not interrupted:
                    self.block(node.orelse)
            elif isinstance(node, ast.Expr):
                self.expr(node.value)
            elif isinstance(node, (ast.Break, ast.Continue)):
                raise Flow("break" if isinstance(node, ast.Break) else "continue")

    def assign(self, target, value):
        if isinstance(target, ast.Name):
            self.env[target.id] = self.bound(value)
        elif isinstance(target, ast.Subscript):
            obj, key = self.expr(target.value), self.expr(target.slice)
            obj[key] = value
            self.bound(obj)
        elif isinstance(target, (ast.Tuple, ast.List)):
            if len(target.elts) != len(value):
                raise ValueError("解包数量不一致。")
            for child, item in zip(target.elts, value):
                self.assign(child, item)
        else:
            raise ExecutionError("CODE_INVALID", "赋值目标不受支持。")

    def expr(self, node):
        self.tick(node)
        if isinstance(node, ast.Constant):
            if type(node.value) not in (str, int, float, bool, type(None)):
                raise ValueError("只允许 JSON 值。")
            return self.bound(node.value)
        if isinstance(node, ast.Name):
            return self.env[node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            return self.bound([self.expr(n) for n in node.elts])
        if isinstance(node, ast.Dict):
            return self.bound({self.expr(k): self.expr(v) for k, v in zip(node.keys, node.values)})
        if isinstance(node, ast.Subscript):
            return self.expr(node.value)[self.expr(node.slice)]
        if isinstance(node, ast.Slice):
            return slice(*(self.expr(n) if n else None for n in (node.lower, node.upper, node.step)))
        if isinstance(node, ast.IfExp):
            return self.expr(node.body if self.expr(node.test) else node.orelse)
        if isinstance(node, ast.UnaryOp):
            v = self.expr(node.operand)
            return not v if isinstance(node.op, ast.Not) else (-v if isinstance(node.op, ast.USub) else +v)
        if isinstance(node, ast.BoolOp):
            value = self.expr(node.values[0])
            for child in node.values[1:]:
                if isinstance(node.op, ast.And) and not value or isinstance(node.op, ast.Or) and value:
                    break
                value = self.expr(child)
            return value
        if isinstance(node, ast.BinOp):
            a, b = self.expr(node.left), self.expr(node.right)
            if isinstance(node.op, ast.Mult) and (isinstance(a, (str, list)) or isinstance(b, (str, list))):
                seq, count = (a, b) if isinstance(a, (str, list)) else (b, a)
                if type(count) is not int or len(seq) * max(count, 0) > self.max_text:
                    raise ExecutionError("CODE_LIMIT", "序列扩展超过预算。")
            if isinstance(node.op, ast.Mult) and type(a) is int and type(b) is int and a.bit_length() + b.bit_length() > 4096:
                raise ExecutionError("CODE_LIMIT", "整数运算超过预算。")
            ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b,
                   ast.Div: lambda: a / b, ast.FloorDiv: lambda: a // b, ast.Mod: lambda: a % b}
            if isinstance(node.op, ast.Mod) and isinstance(a, str):
                raise ExecutionError("CODE_INVALID", "字符串格式请使用 join/str/加法。")
            return self.bound(ops[type(node.op)]())
        if isinstance(node, ast.Compare):
            a = self.expr(node.left)
            for op, child in zip(node.ops, node.comparators):
                b = self.expr(child)
                ops = {ast.Eq: lambda: a == b, ast.NotEq: lambda: a != b, ast.Lt: lambda: a < b,
                       ast.LtE: lambda: a <= b, ast.Gt: lambda: a > b, ast.GtE: lambda: a >= b,
                       ast.In: lambda: a in b, ast.NotIn: lambda: a not in b,
                       ast.Is: lambda: a is b, ast.IsNot: lambda: a is not b}
                if not ops[type(op)]():
                    return False
                a = b
            return True
        if isinstance(node, ast.Call):
            args = [self.expr(n) for n in node.args]
            name = node.func.id
            if name == "append":
                if not isinstance(args[0], list) or len(args[0]) >= self.max_items:
                    raise ValueError("append 需要有界列表。")
                args[0].append(args[1])
                self.bound(args[0])
                return None
            functions = {"len": len, "str": str, "int": int, "float": float, "bool": bool,
                         "round": round, "abs": abs, "min": min, "max": max, "sum": sum,
                         "sorted": sorted, "get": lambda obj, key, default=None: obj.get(key, default),
                         "join": lambda items, separator="": separator.join(items),
                         "split": lambda text, separator=None: text.split(separator),
                         "is_object": lambda value: isinstance(value, dict),
                         "is_array": lambda value: isinstance(value, list),
                         "is_string": lambda value: isinstance(value, str),
                         "lower": lambda text: text.lower(), "upper": lambda text: text.upper()}
            functions.update(self.helpers)
            value = functions[name](*args)
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("不允许非有限数值。")
            return self.bound(value)
        raise ExecutionError("CODE_INVALID", "表达式不受支持。")
