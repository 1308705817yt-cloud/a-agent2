"""用户资源导入、快照与模型可见的资源目录。"""

import csv
import hashlib
import io
import json
import re
import time
import uuid
from pathlib import Path


MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 20000


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def artifact_fingerprint(spec, source, runtime_hash=None):
    value = {"spec": spec, "source": source}
    if runtime_hash:
        value["runtime_hash"] = runtime_hash
    return fingerprint(value)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        for attempt in range(21):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                # Windows readers may briefly hold a handle without FILE_SHARE_DELETE.
                if attempt == 20:
                    raise
                time.sleep(0.01 * min(attempt + 1, 5))
    finally:
        temporary.unlink(missing_ok=True)


def resource_id(name, content):
    stem = re.sub(r"[^\w-]+", "-", Path(name).stem).strip("-")[:40] or "resource"
    return stem + "-" + hashlib.sha256(content.encode()).hexdigest()[:8]


def prepare_resources(documents=None, datasets=None, uploads=None) -> dict:
    files = list(uploads or [])
    for path in [*(documents or []), *(datasets or [])]:
        source = Path(path).expanduser().resolve()
        if source.stat().st_size > MAX_BYTES:
            raise ValueError("单份资源不能超过 10 MiB。")
        files.append({"name": source.name, "content": source.read_text(encoding="utf-8-sig")})
    if not files:
        raise ValueError("请提供文档或 CSV 数据，不会自动使用样例资料。")
    if any(not isinstance(f, dict) or not isinstance(f.get("name"), str) or not isinstance(f.get("content"), str) for f in files):
        raise ValueError("上传资源必须包含文本 name 和 content。")
    if len(files) > 30 or sum(len(f["content"].encode("utf-8")) for f in files) > MAX_BYTES:
        raise ValueError("最多导入 30 份文件，总大小不能超过 10 MiB。")
    pack = {"documents": [], "datasets": []}
    for file in files:
        name, content = file["name"], file["content"].lstrip("\ufeff")
        if not isinstance(name, str) or not isinstance(content, str) or not content.strip():
            raise ValueError("文件名和内容必须是非空文本。")
        suffix = Path(name).suffix.lower()
        rid = resource_id(name, content)
        if suffix == ".csv":
            reader = csv.DictReader(io.StringIO(content))
            columns = reader.fieldnames
            if not columns or len(columns) > 100 or len(set(columns)) != len(columns) or any(not c for c in columns):
                raise ValueError("CSV 需要唯一、非空的列名，最多 100 列。")
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("CSV 行的字段数量必须与表头一致。")
                rows.append(row)
                if len(rows) > MAX_ROWS:
                    raise ValueError("单个数据集最多 20000 行。")
            pack["datasets"].append({"id": rid, "name": name, "columns": columns, "rows": rows})
        elif suffix == ".json":
            value = json.loads(content)
            value = value.get("documents") if isinstance(value, dict) else value
            if not isinstance(value, list) or not value:
                raise ValueError("文档 JSON 必须是非空列表，或包含 documents 列表。")
            for index, document in enumerate(value):
                if not isinstance(document, dict) or any(not isinstance(document.get(k), str) or not document[k].strip()
                                                        for k in ("title", "content")):
                    raise ValueError("每份 JSON 文档需要 title 和 content。")
                pack["documents"].append({"id": document.get("id") or f"{rid}-{index + 1}",
                                          "title": document["title"], "content": document["content"]})
        elif suffix in (".txt", ".md"):
            pack["documents"].append({"id": rid, "title": Path(name).stem, "content": content.strip()})
        else:
            raise ValueError("支持 TXT、Markdown、文档 JSON 和 CSV 文件。")
    ids = [item["id"] for kind in pack.values() for item in kind]
    if any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("资源 ID 必须唯一；请避免重复导入同一文件。")
    if len(pack["documents"]) > 100:
        raise ValueError("知识库最多 100 份文档。")
    return pack


def catalog(pack, include_content=False):
    return {"documents": [{"id": d["id"], "title": d["title"], "characters": len(d["content"]),
                            **({"content": d["content"][:8000]} if include_content else {})} for d in pack["documents"]],
            "datasets": [{"id": d["id"], "name": d["name"], "columns": d["columns"], "row_count": len(d["rows"]),
                          "preview": d["rows"][:12]} for d in pack["datasets"]]}
