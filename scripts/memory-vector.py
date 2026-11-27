#!/usr/bin/env python3
"""Linux backend for project-scoped dense/BM25 memory search and indexing."""

import argparse
import datetime
import hashlib
import ipaddress
import json
import math
import os
import pathlib
import re
import stat
import sys
import urllib.error
import urllib.request
import urllib.parse
import uuid

def private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError("Expected a private file owned by the service user")
    return path


def absolute_path(value):
    if not isinstance(value, str) or not pathlib.Path(value).is_absolute():
        raise ValueError("Expected an absolute path")
    return pathlib.Path(value)


def local_endpoint(value, allow_path):
    if not isinstance(value, str):
        raise ValueError("Expected a local endpoint")
    url = urllib.parse.urlsplit(value)
    if (url.scheme not in ("http", "https") or url.username is not None
            or url.password is not None or url.query or url.fragment
            or not url.hostname or not ipaddress.ip_address(url.hostname).is_loopback
            or not allow_path and url.path not in ("", "/")):
        raise ValueError("Expected a loopback endpoint without credentials")
    if url.port == 0:
        raise ValueError("Invalid endpoint port")
    return value.rstrip("/")


def read_config():
    path = pathlib.Path.home() / ".config/ai-toolkit/memory-vector.json"
    try:
        config = json.loads(private_file(path).read_text(encoding="utf-8"))
        fields = {"client_roots", "native_projects", "qdrant_url", "ollama_url",
                  "collection", "model", "dimension", "key_file", "query_log"}
        if not isinstance(config, dict) or set(config) != fields:
            raise ValueError("Invalid config fields")
        roots = config["client_roots"]
        if not isinstance(roots, list) or not roots:
            raise ValueError("No client roots")
        config["client_roots"] = tuple(absolute_path(root) for root in roots)
        for key in ("native_projects", "key_file", "query_log"):
            config[key] = absolute_path(config[key])
        if not all(root.is_dir() for root in (*config["client_roots"], config["native_projects"])):
            raise ValueError("Unavailable source directory")
        private_file(config["key_file"])
        if (type(config["dimension"]) is not int or config["dimension"] <= 0
                or not isinstance(config["model"], str) or not config["model"].strip()
                or not isinstance(config["collection"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", config["collection"])
                or config["collection"] == "claude_memory"):
            raise ValueError("Invalid v2 collection or embedding configuration")
        config["qdrant_url"] = local_endpoint(config["qdrant_url"], False)
        config["ollama_url"] = local_endpoint(config["ollama_url"], True)
        return config
    except (OSError, ValueError, TypeError):
        raise RuntimeError("Invalid or missing private memory-vector.json") from None


config = read_config()
PROJECTS = config["native_projects"]
CLIENT_ROOTS = config["client_roots"]
QDRANT = config["qdrant_url"]
COLLECTION = config["collection"]
DEFAULT_COLLECTION = "claude_memory"
MEMORY_SCOPE_VERSION = 2
OLLAMA = config["ollama_url"]
MODEL = config["model"]
DIM = config["dimension"]
ENV_FILE = config["key_file"]
QUERY_LOG = config["query_log"]


# Классические параметры BM25: k1 - насыщение по частоте слова,
# b - насколько штрафуем длинные документы.
K1, B = 1.2, 0.75

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


# --------------------------------------------------------------------------- сеть

def api_key():
    if os.environ.get("QDRANT_API_KEY"):
        return os.environ["QDRANT_API_KEY"]
    if not ENV_FILE.exists():
        sys.exit(f"нет файла с ключом: {ENV_FILE}")
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("QDRANT__SERVICE__API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"в {ENV_FILE} не нашелся QDRANT__SERVICE__API_KEY")


def qdrant(path, method="GET", body=None, quiet=False):
    req = urllib.request.Request(
        QDRANT + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "api-key": api_key()},
    )
    try:
        with _opener.open(req, timeout=180) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if quiet and e.code == 404:
            return None
        sys.exit(f"Qdrant {e.code} на {method} {path}")
    except urllib.error.URLError as e:
        sys.exit(f"Qdrant недоступен ({e.reason}). Проверь: systemctl --user status qdrant")


def embed(texts):
    # keep_alive держит модель в памяти: холодный старт стоит 11 секунд против
    # 0.5 горячего, а хук на каждом промпте такой паузы не переживет.
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps({"model": MODEL, "input": texts, "keep_alive": "8h"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with _opener.open(req, timeout=900) as r:
            return json.load(r)["embeddings"]
    except urllib.error.URLError as e:
        sys.exit(f"Ollama недоступна ({e.reason}). Проверь: systemctl status ollama")


# ------------------------------------------------------------------------- BM25

def tokenize(text):
    """Токены для разреженного вектора.

    Составной токен индексируется целиком и по частям.
    """
    text = text.lower().replace("ё", "е")
    out = []
    for raw in re.findall(r"[a-zа-я0-9][a-zа-я0-9._/-]*", text):
        raw = raw.strip("._/-")
        if len(raw) < 2:
            continue
        out.append(raw)
        if re.search(r"[._/-]", raw):
            out.extend(p for p in re.split(r"[._/-]+", raw) if len(p) >= 2)
    return out


def term_id(token):
    """Стабильный uint32 под слово. Питоновский hash() не годится - он
    рандомизируется между запусками, и индекс развалился бы на следующий день."""
    return int.from_bytes(hashlib.md5(token.encode()).digest()[:4], "little")


def bm25_document(tokens, avgdl):
    """Веса документа. IDF тут НЕ применяется - его посчитает сам Qdrant
    по статистике коллекции (modifier: idf), и это правильнее: он видит все
    документы, а мы на инкрементальном заливе - только изменившиеся."""
    if not tokens:
        return {"indices": [], "values": []}
    tf = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    dl = len(tokens)
    norm = K1 * (1 - B + B * dl / avgdl) if avgdl else K1
    idx, val = [], []
    for t, f in tf.items():
        idx.append(term_id(t))
        val.append(f * (K1 + 1) / (f + norm))
    return {"indices": idx, "values": val}


def bm25_query(text):
    """Вес запроса - единица на слово. Всю значимость слова добавит IDF Qdrant-а."""
    seen = {}
    for t in tokenize(text):
        seen[term_id(t)] = 1.0
    return {"indices": list(seen), "values": list(seen.values())}


# ------------------------------------------------------------------- коллекция

def collection_exists():
    return qdrant(f"/collections/{COLLECTION}", quiet=True) is not None


def create_collection():
    qdrant(f"/collections/{COLLECTION}", "PUT", {
        "vectors": {"dense": {"size": DIM, "distance": "Cosine"}},
        "sparse_vectors": {"bm25": {"modifier": "idf"}},
    })
    for field in ("project", "type", "project_id", "identity_schema"):
        qdrant(f"/collections/{COLLECTION}/index", "PUT",
               {"field_name": field, "field_schema": "keyword"})
    print(f"коллекция {COLLECTION} создана (плотный dense + разреженный bm25)")


# ------------------------------------------------------------------------ обход

def frontmatter(text):
    """Достает name/description/type из шапки заметки памяти."""
    out = {}
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return out, text
    head, body = m.group(1), text[m.end():]
    for key in ("name", "description"):
        k = re.search(rf"^{key}:\s*(.+)$", head, re.M)
        if k:
            out[key] = k.group(1).strip()
    t = re.search(r"^\s*type:\s*(.+)$", head, re.M)
    if t:
        out["type"] = t.group(1).strip()
    return out, body.strip()


def _project_id(path):
    """Read and canonicalize the identity from a client's .AI metadata."""
    try:
        value = json.loads((path / ".AI" / "project.json").read_text(encoding="utf-8"))
        ident = value["project_id"]
        if not isinstance(ident, str) or not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", ident):
            raise ValueError("invalid project UUID")
        return str(uuid.UUID(ident))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise RuntimeError("scope_unavailable")


def _stable_sources():
    """Metadata-only discovery; return .AI roots without reading memory facts."""
    sources, legacy = {}, []
    for container in CLIENT_ROOTS:
        try:
            clients = sorted(container.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            raise RuntimeError("scope_unavailable")
        for client in clients:
            if client.name.startswith("."):
                continue
            ai = client / ".AI"
            try:
                if ai.is_dir():
                    real_ai = ai.resolve(strict=True)
                    ident = _project_id(real_ai.parent)
                    memory = real_ai / "memory"
                    key = (ident, real_ai)
                    sources.setdefault(key, {"project_id": ident, "project": client.name,
                                             "root": memory, "client_root": real_ai.parent})
                old = client / ".claude" / "memory"
                if old.is_dir() and not (client / ".AI" / "project.json").is_file():
                    legacy.append(client.name)
            except (OSError, RuntimeError):
                raise RuntimeError("scope_unavailable")
    # Native aliases are metadata only: they resolve to the canonical .AI root
    # already discovered above and never create a second logical source.
    for container in (PROJECTS,):
        try:
            aliases = sorted(container.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            aliases = []
        except OSError:
            raise RuntimeError("scope_unavailable")
        for alias in aliases:
            mem = alias / "memory"
            try:
                if mem.is_dir():
                    real = mem.resolve(strict=True)
                    if real.parent.name == ".AI":
                        client_root = real.parent.parent
                        ident = _project_id(client_root)
                        key = (ident, real.parent)
                        sources.setdefault(key, {"project_id": ident,
                            "project": client_root.name, "root": real,
                            "client_root": client_root})
                    else:
                        legacy.append(alias.name)
            except (OSError, RuntimeError):
                raise RuntimeError("scope_unavailable")
    return list(sources.values()), sorted(set(legacy))


def _v2_enabled():
    return (MEMORY_SCOPE_VERSION == 2 and COLLECTION != DEFAULT_COLLECTION)


def _v2_uuid(project_id, relative_path):
    value = json.dumps(["ai-memory-v2", project_id, relative_path],
                       ensure_ascii=False, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _collect_v2():
    if not _v2_enabled():
        raise RuntimeError("scope_unavailable")
    sources, legacy = _stable_sources()
    if legacy:
        raise RuntimeError("unsupported_scope")
    grouped = {}
    for source in sources:
        root = source["root"]
        try:
            real_root = root.resolve(strict=True)
            files = sorted(root.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            raise RuntimeError("scope_unavailable")
        for path in files:
            if path.suffix != ".md" or path.name == "MEMORY.md":
                continue
            try:
                real_path = path.resolve(strict=True)
                relative = real_path.relative_to(real_root).as_posix()
                raw = real_path.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, ValueError, UnicodeError):
                raise RuntimeError("scope_unavailable")
            if not text.strip():
                continue
            key = (source["project_id"], relative)
            previous = grouped.get(key)
            if previous is not None and previous["raw"] != raw:
                raise RuntimeError("identity_conflict")
            candidate = {"source": source, "path": real_path, "relative": relative,
                         "raw": raw, "text": text}
            if previous is None or str(real_path) < str(previous["path"]):
                grouped[key] = candidate
    docs, total = [], 0
    for (project_id, relative), entry in sorted(grouped.items()):
        meta, body = frontmatter(entry["text"].strip())
        blob = f"{meta.get('description', '')}\n\n{body}".strip()
        toks = tokenize(blob)
        total += len(toks)
        source, path = entry["source"], entry["path"]
        docs.append({"id": _v2_uuid(project_id, relative), "blob": blob,
            "tokens": toks, "hash": hashlib.sha256(entry["raw"]).hexdigest(),
            "payload": {"identity_schema": 2, "project_id": project_id,
                "relative_path": relative, "project": source["project"],
                "file": path.name, "path": str(path),
                "name": meta.get("name", path.stem),
                "description": meta.get("description", ""),
                "type": meta.get("type", "?"), "text": body[:4000]}})
    return docs, total / len(docs) if docs else 1


def resolve_memory_scope(project=None, project_id=None):
    """Resolve one source identity and prove the selected collection has no legacy point."""
    if not _v2_enabled() or bool(project) == bool(project_id):
        return {"status": "error", "error": "scope_unavailable"}
    try:
        sources, legacy = _stable_sources()
        if project_id is not None:
            try:
                ident = str(uuid.UUID(project_id))
            except (ValueError, TypeError, AttributeError):
                return {"status": "error", "error": "unknown_project"}
            matches = [item for item in sources if item["project_id"] == ident]
        else:
            matches = [item for item in sources if item["project"] == project]
            ids = {item["project_id"] for item in matches}
            if len(ids) > 1:
                return {"status": "error", "error": "ambiguous_project"}
            if not matches and project in legacy:
                return {"status": "error", "error": "scope_unavailable"}
        if not matches:
            return {"status": "error", "error": "unknown_project"}
        ids = {item["project_id"] for item in matches}
        if len(ids) != 1:
            return {"status": "error", "error": "ambiguous_project"}
        ident = next(iter(ids))
        display = sorted({item["project"] for item in matches})[0]
        probe_filter = {"must_not": [{"key": "identity_schema", "match": {"value": 2}}]}
        response = qdrant(f"/collections/{COLLECTION}/points/scroll", "POST", {
            "limit": 1, "with_payload": False, "with_vector": False,
            "filter": probe_filter})
        result = response["result"]
        if not isinstance(result, dict) or set(result) != {"points", "next_page_offset"}:
            raise ValueError()
        offset = result["next_page_offset"]
        valid_offset = offset is None
        if type(offset) is int:
            valid_offset = offset >= 0
        elif isinstance(offset, str):
            try:
                valid_offset = str(uuid.UUID(offset)) == offset
            except (ValueError, TypeError, AttributeError):
                valid_offset = False
        if not valid_offset:
            raise ValueError()
        points = result["points"]
        if not isinstance(points, list) or len(points) > 1:
            raise ValueError()
        if points:
            return {"status": "error", "error": "migration_required"}
        if offset is not None:
            raise ValueError()
        return {"status": "ok", "project": display, "project_id": ident,
                "filter": {"must": [{"key": "project_id", "match": {"value": ident}}]}}
    except RuntimeError as exc:
        code = str(exc)
        return {"status": "error", "error": code if code in {
            "scope_unavailable", "unsupported_scope"} else "backend_failure"}
    except Exception:
        return {"status": "error", "error": "backend_failure"}


def _validate_v2_catalog(points):
    seen = set()
    for point in points:
        payload = point.get("payload") if isinstance(point, dict) else None
        if not isinstance(payload, dict) or payload.get("identity_schema") != 2:
            raise RuntimeError("migration_required")
        ident, relative = payload.get("project_id"), payload.get("relative_path")
        try:
            canonical = str(uuid.UUID(ident))
        except (ValueError, TypeError, AttributeError):
            raise RuntimeError("migration_required")
        if canonical != ident or not isinstance(relative, str) or relative in ("", ".") or \
                relative.startswith("/") or pathlib.PurePosixPath(relative).as_posix() != relative or \
                ".." in pathlib.PurePosixPath(relative).parts or "\\" in relative:
            raise RuntimeError("migration_required")
        expected = _v2_uuid(ident, relative)
        if str(point.get("id")) != expected or expected in seen:
            raise RuntimeError("migration_required")
        seen.add(expected)


def cmd_index(args):
    if not _v2_enabled():
        raise RuntimeError("scope_unavailable")
    info = qdrant(f"/collections/{COLLECTION}", quiet=True)
    exists = info is not None
    if exists and info["result"]["config"]["params"]["vectors"]["dense"]["size"] != DIM:
        raise RuntimeError("collection dimension mismatch")
    points = scroll_all(["identity_schema", "project_id", "relative_path", "hash", "project", "path"]) if exists else []
    if exists:
        _validate_v2_catalog(points)
    before = source_health(points)
    # Source and byte-conflict validation precede collection creation or mutation.
    docs, avgdl = _collect_v2()
    if not docs and points:
        raise RuntimeError("Refusing to prune a populated collection from an empty corpus")
    if not exists:
        create_collection()
    have = {str(p["id"]): p["payload"] for p in points}
    fresh = [d for d in docs if args.force or any(
        have.get(d["id"], {}).get(field) != value for field, value in (
            ("hash", d["hash"]), ("project", d["payload"]["project"]),
            ("path", d["payload"]["path"]))) ]
    current = {d["id"] for d in docs}
    gone = [i for i in have if i not in current]
    for i in range(0, len(fresh), 16):
        batch = fresh[i:i + 16]
        vecs = embed([d["blob"] for d in batch])
        if len(vecs) != len(batch) or any(len(vector) != DIM for vector in vecs):
            raise RuntimeError("Ollama вернула неполный пакет эмбеддингов")
        result = qdrant(f"/collections/{COLLECTION}/points?wait=true", "PUT", {"points": [
            {"id": d["id"], "vector": {"dense": vector,
             "bm25": bm25_document(d["tokens"], avgdl)},
             "payload": {**d["payload"], "hash": d["hash"]}}
            for d, vector in zip(batch, vecs)]})
        if result.get("status") != "ok" or result.get("result", {}).get("status") != "completed":
            raise RuntimeError("Qdrant не подтвердил завершение upsert")
    after = source_health(points)
    if not stable_sources(before, after):
        print("неполный снимок источников; удаление отложено")
    elif gone:
        remaining = []
        for ident in gone:
            try:
                pathlib.Path(have[ident]["path"]).stat()
            except (FileNotFoundError, NotADirectoryError):
                remaining.append(ident)
            except (OSError, ValueError, TypeError):
                print("не удалось проверить путь источника; удаление отложено")
                remaining = []
                break
        else:
            if remaining:
                ack = qdrant(f"/collections/{COLLECTION}/points/delete?wait=true",
                             "POST", {"points": remaining})
                result = ack.get("result") if isinstance(ack, dict) else None
                if (not isinstance(result, dict) or ack.get("status") != "ok" or
                        result.get("status") != "completed"):
                    raise RuntimeError("Qdrant не подтвердил завершение удаления")


def scroll_all(fields, with_vector=False):
    out, offset = [], None
    while True:
        page = qdrant(f"/collections/{COLLECTION}/points/scroll", "POST",
                      {"limit": 250, "offset": offset,
                       "with_payload": fields, "with_vector": with_vector})["result"]
        out.extend(page["points"])
        offset = page.get("next_page_offset")
        if not offset:
            break
    return out


def source_health(points):
    """Capture readable source/container identities and known point parents."""
    complete = True
    directories = {}
    clients = set()
    pending = False

    def read_dir(path, required=False):
        nonlocal complete
        try:
            info = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            if required:
                complete = False
            return None
        except (OSError, ValueError, TypeError):
            complete = False
            return None
        if not stat.S_ISDIR(info.st_mode):
            if required:
                complete = False
            return None
        try:
            entries = tuple(sorted(child.name for child in path.iterdir()))
        except (OSError, ValueError, TypeError):
            complete = False
            return None
        directories[path] = (info.st_dev, info.st_ino, entries)
        return entries

    def check_journals(client):
        nonlocal complete, pending
        clients.add(client)
        for name in ("sync.json", "migration.json", "journal.json"):
            marker = client / ".ai-bootstrap" / name
            try:
                marker.lstat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except (OSError, ValueError, TypeError):
                complete = False
            else:
                pending = True

    def memory_dir(path):
        nonlocal complete
        entries = read_dir(path)
        if entries is None:
            return
        try:
            real = path.resolve(strict=True)
        except (OSError, ValueError, TypeError):
            complete = False
            return
        if real.name == "memory" and real.parent.name in (".AI", ".claude"):
            canonical_root = real.parent.parent
            read_dir(canonical_root, required=True)
            check_journals(canonical_root)

    for container in (PROJECTS, *CLIENT_ROOTS):
        entries = read_dir(container, required=True)
        if entries is None:
            continue
        for child in (container / name for name in entries):
            child_entries = read_dir(child)
            if child_entries is None:
                continue
            if container == PROJECTS:
                check_journals(child)
                memory_dir(child / "memory")
                continue
            if child.name.startswith("."):
                continue
            check_journals(child)
            for layout in (".AI", ".claude"):
                memory_dir(child / layout / "memory")

    for point in points:
        payload = point.get("payload") if isinstance(point, dict) else None
        path = payload.get("path") if isinstance(payload, dict) else None
        if not isinstance(path, str) or not path:
            complete = False
            continue
        try:
            point_path = pathlib.Path(path)
            if not point_path.is_absolute():
                complete = False
                continue
            read_dir(point_path.parent, required=True)
        except (OSError, ValueError, TypeError):
            complete = False

    return {"complete": complete and not pending,
            "directories": directories,
            "clients": frozenset(clients),
            "pending": pending}


def stable_sources(before, after):
    return (before["complete"] and after["complete"] and
            not before["pending"] and not after["pending"] and
            before["directories"] == after["directories"] and
            before["clients"] == after["clients"])


def log_query(mode, args, hits):
    """Журнал обращений: кто спрашивал и что получил.

    Текст запроса НЕ пишем - через него утекали бы промпты пользователя (та же
    причина, по которой из лога отказов хука убран stderr целиком). Вместо него
    короткий хеш: повторы и частоту по нему видно, содержимое - нет.

    Смысл журнала в том, что иначе обращения с другой машины неотличимы от
    локальных: со стороны сервера виден только суммарный счетчик Qdrant.
    """
    try:
        # SSH_CLIENT выставляется при выполнении команды через ssh - по нему и
        # отличаем удаленный вызов от локального.
        src = (os.environ.get("SSH_CLIENT") or "").split(" ")[0] or "локально"
        qh = hashlib.md5(args.query.encode()).hexdigest()[:4]
        top = f"{hits[0]['score']:.3f}" if hits else "-"
        QUERY_LOG.parent.mkdir(parents=True, exist_ok=True)
        if QUERY_LOG.exists() and QUERY_LOG.stat().st_size > 131072:
            QUERY_LOG.write_text(QUERY_LOG.read_text(errors="replace")[-32768:])
        with QUERY_LOG.open("a") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\t{src}\t{mode}"
                    f"\tk={args.k}\tq#{qh}\tнайдено={len(hits)}\tтоп={top}"
                    f"{chr(9) + 'проект=' + args.project if args.project else ''}\n")
    except Exception:
        pass  # журнал не имеет права мешать поиску


def build_filter(args):
    must = []
    project_id = getattr(args, "project_id", None)
    project = getattr(args, "project", None)
    if project_id:
        must.append({"key": "project_id", "match": {"value": project_id}})
    elif project:
        if MEMORY_SCOPE_VERSION == 2:
            scope = resolve_memory_scope(project=project)
            if scope.get("status") != "ok":
                raise RuntimeError(scope.get("error", "backend_failure"))
            must.extend(scope["filter"]["must"])
        else:
            must.append({"key": "project", "match": {"text": project}})
    if args.type:
        must.append({"key": "type", "match": {"value": args.type}})
    return {"must": must} if must else None


def cmd_search(args):
    project_id = getattr(args, "project_id", None)
    if project_id and getattr(args, "project", None):
        raise RuntimeError("invalid_parameters")
    if MEMORY_SCOPE_VERSION == 2:
        if not _v2_enabled():
            raise RuntimeError("scope_unavailable")
        if project_id is not None:
            try:
                canonical = str(uuid.UUID(project_id))
            except (ValueError, TypeError, AttributeError):
                raise RuntimeError("invalid_parameters")
            if canonical != project_id:
                raise RuntimeError("invalid_parameters")
            scope = resolve_memory_scope(project_id=canonical)
        elif getattr(args, "project", None):
            scope = resolve_memory_scope(project=args.project)
        else:
            raise RuntimeError("invalid_parameters")
        if scope and scope.get("status") != "ok":
            raise RuntimeError(scope.get("error", "backend_failure"))
        if scope:
            args.project_id = scope["project_id"]
    elif project_id is not None:
        raise RuntimeError("unsupported_scope")
    flt = build_filter(args)
    depth = max(args.k * 4, 20)

    if args.dense or args.sparse:
        # Одиночный поиск - для сравнения движков между собой
        using = "dense" if args.dense else "bm25"
        query = embed([args.query])[0] if args.dense else bm25_query(args.query)
        body = {"query": query, "using": using, "limit": args.k, "with_payload": True}
        mode = "только смысл" if args.dense else "только слова"
    else:
        body = {
            "prefetch": [
                {"query": embed([args.query])[0], "using": "dense", "limit": depth},
                {"query": bm25_query(args.query), "using": "bm25", "limit": depth},
            ],
            "query": {"fusion": args.fusion},
            "limit": args.k,
            "with_payload": True,
        }
        mode = "гибрид"
    if flt:
        body["filter"] = flt
        if MEMORY_SCOPE_VERSION == 2 and "prefetch" in body:
            for branch in body["prefetch"]:
                branch["filter"] = flt

    response = qdrant(f"/collections/{COLLECTION}/points/query", "POST", body)
    try:
        hits = response["result"]["points"]
        if not isinstance(hits, list):
            raise ValueError()
        if MEMORY_SCOPE_VERSION == 2:
            _validate_v2_catalog(hits)
            if any(hit["payload"]["project_id"] != args.project_id for hit in hits):
                raise ValueError()
    except Exception:
        if MEMORY_SCOPE_VERSION == 2:
            raise RuntimeError("scope_unavailable")
        raise
    log_query(mode if not args.dense and not args.sparse else mode, args, hits)

    if args.json:
        print(json.dumps([
            {"score": h["score"], **{k: h["payload"].get(k, "") for k in
                                     ("project", "file", "name", "description", "type")},
             **({"project_id": h["payload"]["project_id"]} if MEMORY_SCOPE_VERSION == 2 else {})}
            for h in hits], ensure_ascii=False))
        return

    if not hits:
        print("ничего не нашлось")
        return
    print(f"({mode})")
    for h in hits:
        p = h["payload"]
        print(f"[{h['score']:.3f}] {p['project']} / {p['file']}  ({p['type']})")
        if p.get("description"):
            print(f"    {p['description']}")
        if args.full:
            print("    " + p["text"].replace("\n", "\n    ") + "\n")


def cmd_status(args):
    info = qdrant(f"/collections/{COLLECTION}")["result"]
    print(f"коллекция:   {COLLECTION}")
    print(f"точек:       {info['points_count']}")
    print(f"статус:      {info['status']}")
    params = info["config"]["params"]
    dense = params["vectors"]["dense"]
    print(f"плотный:     {dense['size']} ({dense['distance']}), модель {MODEL}")
    print(f"разреженный: {list(params.get('sparse_vectors', {}))} (BM25, IDF на стороне Qdrant)")

    by = {}
    for p in scroll_all(["project"]):
        by[p["payload"]["project"]] = by.get(p["payload"]["project"], 0) + 1
    print("\nпо проектам:")
    for name, n in sorted(by.items(), key=lambda x: -x[1]):
        print(f"  {n:4}  {name}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("index", help="обойти проекты и залить изменившееся")
    p.add_argument("--force", action="store_true", help="перезалить все, игнорируя хеши")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="найти")
    p.add_argument("query")
    p.add_argument("--project", help="фильтр по проекту")
    p.add_argument("--project-id", help="точный UUID scope проекта (только v2)")
    p.add_argument("--type", help="фильтр по типу: user / feedback / project / reference")
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--dense", action="store_true", help="только смысловой поиск")
    p.add_argument("--sparse", action="store_true", help="только поиск по словам")
    p.add_argument("--full", action="store_true", help="печатать тело факта целиком")
    p.add_argument("--json", action="store_true", help="машинный вывод (для хука)")
    p.add_argument("--fusion", choices=["rrf", "dbsf"], default="rrf",
                   help="формула слияния: rrf по рангам, dbsf по нормированным оценкам")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("status", help="что в индексе")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("migrate", help="разовый перенос из старой коллекции")
    p.add_argument("--source", default="claude_memory_dense")
    p.add_argument("--dest", default=COLLECTION)
    p.set_defaults(func=lambda args: sys.exit("migration_required"))

    argv = sys.argv[1:]
    if argv and argv[0] not in {"index", "search", "status", "migrate", "-h", "--help"}:
        argv = ["search"] + argv
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
