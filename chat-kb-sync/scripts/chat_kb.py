"""Local chat archive planner. No model, source modification or network by default."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata

LIMIT = 32 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
SECRET = re.compile(
    r"(?i)(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk|ragflow|ghp)[_-][A-Za-z0-9_-]{20,}\b|"
    r"\b(?:api[_ -]?key|access[_ -]?token|password|client[_ -]?secret|密码|密钥)\s*[:=：]\s*[\w/+.-]{8,}|"
    r"\bbearer\s+[A-Za-z0-9_.-]{16,})"
)
RULES = {
    "机器人": ("机器人", "机械臂", "树莓派", "raspberry pi", "motor driver", "robotics", "lidar"),
    "水利": ("水利", "水位", "闸门", "水库", "流域", "hydrology", "flood control"),
    "现场交付": ("现场调研", "港口", "联调", "验收", "部署交付", "培训", "site acceptance"),
    "知识库与智能体": ("ragflow", "知识库", "向量检索", "codex", "mcp", "agent", "knowledge base"),
    "开发与运维": ("docker", "nginx", "数据库", "python", "接口", "故障", "database", "deployment"),
    "学习与调研": ("学习", "路线图", "对比", "课程", "research", "tutorial"),
    "个人与生活": ("旅行", "菜单", "食谱", "锻炼", "运动", "travel", "recipe"),
}
OTHER = "待分类"
SWITCH = re.compile(r"^(?:\s*[，、：:]?\s*)(?:换个话题|换个问题|说另一件事|另外一个项目|switch(?:ing)? topics?\b)", re.I)


def blocked_path(p):
    for parent in (p, *p.parents):
        try:
            s = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(s.st_mode) or getattr(s, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked or redirected local path")


def absolute(p):
    return Path(os.path.abspath(Path(p).expanduser()))


def read_json(p):
    blocked_path(p)
    if not p.is_file() or p.stat().st_size > LIMIT:
        raise ValueError("missing or oversized JSON file")
    with p.open("rb") as f:
        data = f.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("oversized JSON file")
    return json.loads(data.decode("utf-8-sig"))


def digest(body):
    if not isinstance(body, bytes):
        body = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def atomic(p, data):
    blocked_path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    blocked_path(p.parent)
    fd, fn = tempfile.mkstemp(dir=p.parent, prefix=".partial-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(fn, p)
    finally:
        Path(fn).unlink(missing_ok=True)


@contextmanager
def exclusive(root):
    blocked_path(root / ".sync.lock")
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".sync.lock").open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            if not f.read(1):
                f.write(b"\0")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield


def collect_exports(root, allowed_ids):
    for key in sorted(allowed_ids):
        p = root / (key + ".json")
        blocked_path(p)
        if not p.is_file():
            continue
        before = p.stat()
        c = read_json(p)
        after = p.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            continue
        if not isinstance(c, dict) or c.get("id") != key:
            raise ValueError("export file and conversation ID do not match")
        yield {**c, "source": "export"}


def timestamp(value):
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.utcoffset() is None:
        raise ValueError("source timestamp needs a timezone")
    return t.astimezone(timezone.utc)


def checked(c, expected, allow):
    if c["source"] != expected or c["id"] not in allow:
        raise ValueError("adapter returned a conversation outside the configured scope")
    messages = c["messages"]
    if not isinstance(messages, list) or len(messages) > 8000:
        raise ValueError("unsupported message count; export separately")
    clean, seen, chars = [], set(), 0
    for m in messages:
        key, role, body = m["id"], m["role"], m["text"]
        if not isinstance(key, str) or not 0 < len(key) <= 512 or key in seen or role not in {"user", "assistant"}:
            raise ValueError("missing or duplicate message identity")
        if not isinstance(body, str):
            raise ValueError("message body must be text")
        chars += len(body)
        if chars > LIMIT:
            raise ValueError("conversation not processed: size exceeds limit")
        seen.add(key)
        clean.append({"id": key, "role": role, "text": body})
    return clean


def excerpts(value, length):
    value = re.sub(r"```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)", " ", value)
    value = re.sub(r"https?://\S+|(?:[A-Za-z]:[\\/]|\\\\)[^\s<>]+", "[local reference withheld]", value)
    value = " ".join(value.split())
    value = "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf"))
    value = value.replace("<", "＜").replace(">", "＞").replace("[", "［").replace("]", "］")
    return value[:length] + ("…" if len(value) > length else "")


def classify(text, rules):
    text = unicodedata.normalize("NFKC", text).casefold()
    scored = []
    for label, terms in rules.items():
        hits = [word for word in terms if (re.search(r"(?<!\w)" + re.escape(word.casefold()) + r"(?!\w)", text)
                if word.isascii() else word.casefold() in text)]
        if hits:
            scored.append((len(hits), label, hits))
    scored.sort(reverse=True)
    if not scored or len(scored) > 1 and scored[0][0] == scored[1][0]:
        return OTHER, []
    return scored[0][1:]


def segment(messages, rules):
    parts, active, turn = [], None, None
    for m in messages:
        if m["role"] == "user":
            if turn:
                turn = None
            q = excerpts(m["text"], 900)
            category, words = classify(q, rules) if not SECRET.search(m["text"]) else (OTHER, [])
            explicit = bool(SWITCH.search(q))
            if active is None or explicit or (category != OTHER and active["category"] not in (OTHER, category)):
                active = {"category": category, "turns": [], "keywords": set(), "explicit": explicit}
                parts.append(active)
            if active["category"] == OTHER and category != OTHER:
                active["category"] = category
            turn = [m]
            active["turns"].append(turn)
            active["keywords"].update(words)
        elif turn:
            turn.append(m)
    return parts


def assign(parts, old, thread_key):
    """Reuse a topic identity only if both directions agree based on stable message IDs."""
    new = [{digest(t[0]["id"]) for t in p["turns"]} for p in parts]
    anchors = [digest(p["turns"][0][0]["id"]) for p in parts]
    old_ids = [p["key"] for p in old]
    forward = [[j for j, o in enumerate(old) if ids.intersection(o["messages"])] for ids in new]
    backward = [[i for i, refs in enumerate(forward) if j in refs] for j in range(len(old))]
    result, held = [], set()
    for i, ids in enumerate(new):
        refs = forward[i]
        if not refs:
            key = digest([thread_key, anchors[i]])
        elif len(refs) == 1 and len(backward[refs[0]]) == 1:
            key = old_ids[refs[0]]
        # On a split, a unique first-anchor retains the old topic; other segments require review.
        elif len(refs) == 1 and old[refs[0]]["anchor"] == anchors[i]:
            key = old_ids[refs[0]]
        else:
            held.update(refs)
            result.append(None)
            continue
        result.append({"key": key, "anchor": anchors[i], "messages": sorted(ids)})
    unmatched = {j for j in range(len(old)) if not backward[j]}
    return result, bool(held or unmatched)


def render(c, part, item, namespace):
    turns, held = [], 0
    for turn in part["turns"]:
        if any(SECRET.search(m["text"]) for m in turn):
            held += 1
            continue
        q = excerpts(turn[0]["text"], 240)
        a = [excerpts(m["text"], 480) for m in turn[1:]]
        if q:
            turns.append((q, " / ".join(x for x in a[-2:] if x)))
    if not turns:
        return None, held
    chosen = turns if len(turns) <= 7 else turns[:2] + turns[-5:]
    title = excerpts(chosen[0][0], 64)
    lines = [f"# {title}", "", f"- 分类：{part['category']}",
             f"- 来源：{c['source']} / {namespace} / {c['id']}",
             f"- 主题标识：{item['key']}", f"- 主题内可摘要轮次：{len(turns)}",
             "- 类型：本地抽取稿；AI 说法与操作结果未经本工具验证。", "",
             "## 问题与回答要点"]
    for i, (q, a) in enumerate(chosen, 1):
        lines.extend((f"{i}. 问题：{q}", f"   记录摘录：{a or '未提取到可见回答'}"))
    words = "、".join(sorted(part["keywords"]))
    if words:
        lines += ["", "## 检索关键词", words]
    if held:
        lines += ["", f"有 {held} 轮因疑似敏感信息未写入。"]
    return ("\n".join(lines) + "\n").encode(), held


def scan(cfg, root, stats):
    import codex_source
    import dsh_source
    readers = {"export": collect_exports, "codex": codex_source.collect, "deepseek-harness": dsh_source.collect}
    state_path = root / "scan-state.json"
    ready_path = root / "publish-ready.json"
    atomic(ready_path, b"[]")
    state = read_json(state_path) if state_path.exists() else {"version": 1, "threads": {}}
    if state["version"] != 1:
        raise ValueError("unsupported local state version")
    rules = cfg.get("categories", RULES)
    if not isinstance(rules, dict) or any(
        not isinstance(k, str) or not k or k != excerpts(k, 80) or not isinstance(v, (list, tuple)) or not v
        or any(not isinstance(w, str) or not w.strip() or w != excerpts(w, 80) for w in v) for k, v in rules.items()
    ) or OTHER in rules:
        raise ValueError("invalid category configuration")
    include_uncertain = cfg.get("publish_unclassified", False)
    if not isinstance(include_uncertain, bool):
        raise ValueError("invalid uncertain-topic policy")
    fingerprint = digest([1, rules, include_uncertain])
    min_idle = cfg.get("min_idle_seconds", 300)
    if not isinstance(min_idle, int) or min_idle < 0:
        raise ValueError("invalid quiet period")
    now = datetime.now(timezone.utc)
    used = set()
    ready = set()
    for source in cfg["sources"]:
        kind, namespace = source["type"], source["namespace"]
        if kind not in readers or not ID.fullmatch(namespace) or (kind, namespace) in used:
            raise ValueError("invalid or duplicate source namespace")
        used.add((kind, namespace))
        base = absolute(source["root"])
        blocked_path(base)
        if not base.is_dir() or base == root or root in base.parents or base in root.parents:
            raise ValueError("source and work directory must be separate existing locations")
        seen = set()
        try:
            mode = source.get("scope", "allowlist")
            options = {}
            if mode == "allowlist":
                values = read_json(absolute(source["allowlist"]))
                if not isinstance(values, list) or any(not isinstance(v, str) or not ID.fullmatch(v) for v in values):
                    raise ValueError("invalid allowlist")
                allow = {v.lower() if kind == "codex" else v for v in values}
            elif mode == "verified-codex-owner" and kind == "codex":
                options = {"owner_account": source["owner_account"], "owner_user": source["owner_user"]}
                allow = codex_source.discover(base, **options)
            elif mode == "owned-dsh-profile" and kind == "deepseek-harness":
                allow = dsh_source.discover(base)
            elif mode == "approved-export-folder" and kind == "export":
                allow = {x.stem for x in base.glob("*.json") if ID.fullmatch(x.stem)}
            else:
                raise ValueError("unsupported scope")
            stats["allowed"] += len(allow)
            for c in readers[kind](base, allow, **options):
                messages = checked(c, kind, allow)
                if c["id"] in seen:
                    raise ValueError("duplicate source conversation")
                seen.add(c["id"])
                modified = timestamp(c["modified"])
                if (now - modified).total_seconds() < min_idle:
                    stats["waiting"] += 1
                    continue
                key = digest([kind, namespace, c["id"]])
                prev = state["threads"].get(key, {})
                content = digest([fingerprint, messages])
                if content == prev.get("hash") and not prev.get("needs_review"):
                    stats["unchanged"] += 1
                    ready.update(prev.get("publishable", []))
                    if prev.get("modified") != modified.isoformat():
                        prev["modified"] = modified.isoformat()
                        atomic(state_path, json.dumps(state, ensure_ascii=False).encode())
                    continue
                parts = segment(messages, rules)
                old = prev.get("topics", [])
                mapping, review = assign(parts, old, key)
                if review and prev.get("topics"):
                    # Do not overwrite or silently supersede ambiguous content already archived.
                    stats["needs_review"] += 1
                    continue
                new = {}
                held = 0
                # Keep inferred '待分类' documents visible as such, never cross-merge chats.
                for part, item in zip(parts, mapping):
                    if item is None:
                        continue
                    rendered, n = render(c, part, item, namespace)
                    held += n
                    if rendered is None:
                        if any(p["key"] == item["key"] for p in old):
                            review = True
                        continue
                    new[item["key"]] = (item, rendered)
                if review or held and prev.get("topics"):
                    # A newly flagged edit could make previously published text stale. Freeze this thread.
                    stats["needs_review"] += 1
                    stats["withheld_turns"] += held
                    continue
                categories = {item["key"]: part["category"] for part, item in zip(parts, mapping) if item}
                publishable = []
                for doc_key, (item, data) in new.items():
                    waiting = categories[doc_key] == OTHER and not include_uncertain
                    if waiting:
                        stats["unclassified"] += 1
                    else:
                        publishable.append(doc_key)
                    target = root / ("review" if waiting else "outbox") / (doc_key + ".md")
                    blocked_path(target)
                    if not target.exists() or target.read_bytes() != data:
                        atomic(target, data)
                        stats["docs_written"] += 1
                state["threads"][key] = {"hash": content, "modified": modified.isoformat(),
                                           "needs_review": bool(held), "publishable": publishable,
                                           "topics": [v[0] for v in new.values()]}
                atomic(state_path, json.dumps(state, ensure_ascii=False).encode())
                ready.update(publishable)
                stats["withheld_turns"] += held
                stats["changed"] += 1
        except (OSError, ValueError, TypeError, KeyError, ImportError, RuntimeError):
            stats["source_errors"] += 1
            continue
        stats["not_yielded"] += len(allow - seen)
    if not stats["source_errors"]:
        atomic(ready_path, json.dumps(sorted(ready)).encode())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("action", choices=["scan", "publish", "sync"])
    args = p.parse_args(argv)
    try:
        cfg = read_json(absolute(args.config))
        root = absolute(cfg["work_dir"])
        blocked_path(root)
        # Never write outbox or private state into a Git checkout.
        if any((parent / ".git").exists() for parent in (root, *root.parents)):
            raise ValueError("private work_dir must be outside Git checkouts")
        stats = dict.fromkeys(("allowed", "not_yielded", "waiting", "unchanged", "changed", "docs_written",
                               "needs_review", "unclassified", "withheld_turns", "source_errors"), 0)
        with exclusive(root):
            if args.action in ("scan", "sync"):
                scan(cfg, root, stats)
            if args.action in ("publish", "sync") and not stats["source_errors"]:
                from ragflow_local import publish
                target = cfg["ragflow"]
                token = os.environ.get("RAGFLOW_API_KEY", "")
                if not token:
                    raise ValueError("RAGFLOW_API_KEY is required for explicit publication")
                keys = read_json(root / "publish-ready.json")
                if not isinstance(keys, list) or not all(isinstance(k, str) and re.fullmatch(r"[0-9a-f]{64}", k) for k in keys):
                    raise ValueError("invalid publication scope; run scan again")
                stats["ragflow"] = publish(root, target["dataset_id"], target["origin"], token, allowed_topics=set(keys))
        print(json.dumps(stats, ensure_ascii=True, sort_keys=True))
        return 1 if stats["source_errors"] or any(stats.get("ragflow", {}).get(k) for k in ("failed", "pending")) else 0
    except (OSError, ValueError, TypeError, KeyError, ImportError, RuntimeError):
        print("Archive stopped. Check configuration, isolated work directory, adapter setup and target permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
