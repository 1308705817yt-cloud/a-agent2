"""Loopback-only dashboard and a persistent, single-worker build queue."""

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .factory import FactoryAgent
from .resources import MAX_BYTES, catalog
from .tool_registry import registry_catalog
from .adaptive_runtime import builtin_catalog
from .executions import create_execution, execute_record, execution_path, continue_execution, recover_interrupted
from .trusted_tools import load_definitions, tool_catalog
from .resources import read_json, write_json


def recover_build_queue(factory, pending):
    recovered = 0
    for state in factory.store.iter_all():
        if factory.store.active(state["run_id"]):
            continue
        if state["status"] == "queued":
            pending.put(state["run_id"])
            recovered += 1
        elif state["status"] == "running":
            state.update(status="failed", last_error="上次进程在构建中退出；可从当前阶段重试。")
            factory.save(state)
    return recovered


def serve(root, host="127.0.0.1", port=8787, model=None, timeout=180):
    if host not in ("127.0.0.1", "localhost") or not 1 <= port <= 65535:
        raise ValueError("控制台只允许绑定本机地址，端口应为 1 至 65535。")
    factory = FactoryAgent(root, model=model, timeout=timeout)
    pending = queue.Queue()
    runtime_pending = queue.Queue(maxsize=20)
    recover_build_queue(factory, pending)

    def worker():
        while True:
            run_id = pending.get()
            try:
                saved = factory.load(run_id)
                engine = saved["engine"]
                runner = FactoryAgent(root, model=engine["model"], timeout=engine["timeout"])
                runner.drive(saved)
            except Exception as exc:
                print("后台任务未完成：" + str(exc), flush=True)
            finally:
                pending.task_done()
    threading.Thread(target=worker, name="agentforge-build-worker", daemon=True).start()
    recover_interrupted(factory)
    def runtime_worker():
        while True:
            run_id, execution_id = runtime_pending.get()
            try:
                execute_record(factory, run_id, execution_id)
            except Exception as exc:
                print("子 Agent 运行失败：" + str(exc), flush=True)
            finally:
                runtime_pending.task_done()
    threading.Thread(target=runtime_worker, name="agentforge-runtime-worker", daemon=True).start()
    static = Path(__file__).parent / "web"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data, content_type="application/json; charset=utf-8", filename=None):
            body = json.dumps(data, ensure_ascii=False).encode() if content_type.startswith("application/json") else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if filename:
                self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
            self.end_headers()
            self.wfile.write(body)

        def local_request(self):
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                raise ValueError("不接受非本机 Host。")
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + h for h in hosts}:
                raise ValueError("不接受跨站请求。")

        def do_GET(self):
            try:
                self.local_request()
                path = urlsplit(self.path).path
                if path in ("/", "/app.js", "/style.css"):
                    name = "index.html" if path == "/" else path[1:]
                    kind = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[name]
                    self.send(200, (static / name).read_bytes(), kind)
                elif path == "/api/jobs":
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if set(query) - {"limit", "cursor"} or any(len(v) != 1 for v in query.values()):
                        raise ValueError("任务分页参数无效。")
                    page = factory.store.page(int(query.get("limit", ["50"])[0]), query.get("cursor", [None])[0])
                    jobs = [{k: s.get(k) for k in ("run_id", "request", "status", "phase", "version", "repairs", "created_at", "updated_at", "requirements", "report", "acceptance_report")}
                            for s in page["jobs"]]
                    for job in jobs:
                        for key in ("report", "acceptance_report"):
                            if job[key]:
                                job[key] = {k: job[key][k] for k in ("passed", "total", "all_passed")}
                    self.send(200, {**page, "jobs": jobs})
                elif path == "/api/tools":
                    tools = [{**t, "input_kind": "JSON", "output_kind": "observation", "parameters": list(t["parameters"]),
                              "task_types": ["document_qa", "csv_analysis"]} for t in builtin_catalog()]
                    trusted = [{**t, "parameters": list(t["input_schema"]["properties"]), "input_kind": "JSON", "output_kind": "observation", "task_types": ["document_qa", "csv_analysis"]} for t in tool_catalog(load_definitions(factory.root))]
                    self.send(200, {"tools": tools + trusted, "custom_tools": "每个 Agent 按需求生成独立源码和契约"})
                elif path == "/api/benchmarks":
                    from .benchmarking import benchmark_catalog
                    self.send(200, {"tasks": benchmark_catalog()})
                elif path.startswith("/api/jobs/"):
                    parts = path.split("/")
                    run_id = parts[3]
                    state = factory.load(run_id)
                    if len(parts) == 5 and parts[4] == "download":
                        if state["status"] != "completed":
                            raise ValueError("此任务没有通过验收的导出产物。")
                        archive = factory.root / "dist" / (run_id + ".zip")
                        self.send(200, archive.read_bytes(), "application/zip", run_id + ".zip")
                    elif len(parts) == 4:
                        self.send(200, {**factory.detail(run_id), "resources": catalog(factory.resources(state))})
                    elif len(parts) == 6 and parts[4] == "executions":
                        self.send(200, read_json(execution_path(factory, run_id, parts[5])))
                    else:
                        self.send(404, {"error": "未找到接口。"})
                else:
                    self.send(404, {"error": "未找到页面。"})
            except (ValueError, OSError) as exc:
                self.send(400, {"error": str(exc)})

        def do_POST(self):
            try:
                self.local_request()
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("请求必须使用 application/json。")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BYTES:
                    raise ValueError("请求大小应在 1 字节至 10 MiB 之间。")
                data = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限数值")))
                if not isinstance(data, dict):
                    raise ValueError("请求体必须是对象。")
                parts = urlsplit(self.path).path.strip("/").split("/")
                if parts == ["api", "jobs"]:
                    state = factory.create(data.get("request"), uploads=data.get("files"), review=data.get("review", False), max_repairs=data.get("max_repairs", 3),
                                           max_seconds=data.get("max_seconds", 3600), run_max_seconds=data.get("run_max_seconds", 900),
                                           adaptive_steps=data.get("adaptive_steps", True))
                    pending.put(state["run_id"])
                    self.send(202, {"run_id": state["run_id"]})
                elif len(parts) == 6 and parts[:2] == ["api", "jobs"] and parts[3] == "executions" and parts[5] == "continue":
                    if runtime_pending.full():
                        raise ValueError("执行队列已满。")
                    record = continue_execution(factory, parts[2], parts[4], data.get("reply"), data.get("max_seconds"))
                    try:
                        runtime_pending.put_nowait((parts[2], parts[4]))
                    except queue.Full:
                        record.update(status="interrupted", error={"code": "QUEUE_FULL", "message": "执行队列已满，可以稍后继续。"})
                        write_json(execution_path(factory, parts[2], parts[4]), record)
                        raise ValueError("执行队列已满。")
                    self.send(202, record)
                elif len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                    run_id, action = parts[2:]
                    if action in ("resume", "retry"):
                        seconds = data.get("max_seconds")
                        if seconds is not None and (type(seconds) not in (int, float) or not 0 <= seconds <= 86400):
                            raise ValueError("总耗时应为 0 至 86400 秒。")
                        state = factory.continue_run(run_id, {"waiting_for_guidance"} if action == "resume" else {"failed", "time_limit", "stopped"},
                                                     data.get("direction", ""), max_seconds=seconds if action == "retry" else None,
                                                     max_repairs=data.get("max_repairs") if action == "retry" else None,
                                                     repair_scope=data.get("repair_scope") if action == "retry" else None,
                                                     run_max_seconds=data.get("run_max_seconds") if action == "retry" else None)
                        pending.put(run_id)
                        self.send(202, {"run_id": run_id, "status": state["status"]})
                    elif action == "run":
                        if factory.load(run_id)["schema_version"] == 4:
                            if runtime_pending.full():
                                raise ValueError("子 Agent 执行队列已满，请稍后运行。")
                            record = create_execution(factory, run_id, data.get("input", {}), data.get("max_seconds"))
                            try:
                                runtime_pending.put_nowait((run_id, record["execution_id"]))
                            except queue.Full:
                                record.update(status="failed", error={"code": "QUEUE_FULL", "message": "执行队列已满，请稍后运行。"})
                                write_json(execution_path(factory, run_id, record["execution_id"]), record)
                                raise ValueError("子 Agent 执行队列已满，请稍后运行。")
                            self.send(202, record)
                        else:
                            self.send(200, factory.run(run_id, data.get("input", {})))
                    else:
                        self.send(404, {"error": "未找到操作。"})
                else:
                    self.send(404, {"error": "未找到接口。"})
            except (ValueError, OSError, TypeError) as exc:
                self.send(400, {"error": str(exc)})

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"AgentForge 控制台：http://{host}:{port} （Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
