# -*- coding: utf-8 -*-
"""
销售策略与高情商沟通官 本地 Web 控制台

零依赖：只用 Python 标准库 http.server。
启动后自动打开浏览器 http://127.0.0.1:8765

API 一览：
    GET  /                        -> web/index.html
    GET  /api/status              -> 监控/朋友圈进程状态
    GET  /api/config/<name>       -> 读 JSON 配置 (user/strategy/contacts/moments/moments_queue)
    PUT  /api/config/<name>       -> 写 JSON 配置
    POST /api/monitor/start      -> 启动聊天监控子进程
    POST /api/monitor/stop       -> 停止
    POST /api/moments/post       -> 立即发一条朋友圈 (body: text, images[])
    POST /api/moments/start-queue-> 启动队列发布
    POST /api/moments/stop       -> 停
    GET  /api/logs                -> 列日志文件
    GET  /api/logs/<name>         -> 读日志尾部
"""
from __future__ import annotations

import json
import csv
import io
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKILL_BACKEND = "http://saas-ai.cn:8080"

RUNTIME_DIR = Path(__file__).resolve().parent
WEB_DIR = RUNTIME_DIR / "web"
LOGS_DIR = RUNTIME_DIR / "logs"
PYTHON_EXE = RUNTIME_DIR / "python" / "python.exe"
if not PYTHON_EXE.exists():
    PYTHON_EXE = Path(sys.executable)

HOST = "127.0.0.1"
PORT = 8765

# 配置文件名映射
CONFIG_FILES = {
    "user": "user_config.json",
    "strategy": "chat_strategy_config.json",
    "contacts": "my_contacts.json",
    "moments": "moments_config.json",
    "moments_queue": "moments_queue.json",
}

# 子进程句柄
_state_lock = threading.Lock()
_monitor_proc: subprocess.Popen | None = None
_moments_proc: subprocess.Popen | None = None
_started_at: float = 0.0
# 监控进程的 pid 落盘：GUI 重启后 _monitor_proc 句柄就没了，
# 但监控可能还在跑，靠 pid 文件才能让「停止监控」按钮继续有效。
MONITOR_PID_FILE = RUNTIME_DIR / "monitor.pid"


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong(0)
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return False


def _read_pid_file() -> int:
    try:
        return int(MONITOR_PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _write_pid_file(pid: int) -> None:
    try:
        MONITOR_PID_FILE.write_text(str(pid), encoding="utf-8")
    except Exception:
        pass


def _detached_monitor_pid() -> int:
    """GUI 重启前启动、且仍在运行的监控进程 pid（0 = 没有）。"""
    pid = _read_pid_file()
    return pid if _pid_alive(pid) else 0


def _kill_detached_monitor() -> int:
    """按 pid 文件强杀遗留的监控进程，返回被杀 pid（0 = 没杀）。"""
    pid = _detached_monitor_pid()
    if pid:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=8)
        except Exception:
            pass
    try:
        if MONITOR_PID_FILE.exists():
            MONITOR_PID_FILE.unlink()
    except Exception:
        pass
    return pid


def _monitor_status() -> dict:
    if _monitor_proc is not None:
        rc = _monitor_proc.poll()
        if rc is None:
            return {"running": True, "pid": _monitor_proc.pid, "exit_code": None}
    pid = _detached_monitor_pid()
    if pid:
        return {"running": True, "pid": pid, "exit_code": None}
    return {"running": False, "pid": None, "exit_code": None}


def _proc_status(proc: subprocess.Popen | None) -> dict:
    if proc is None:
        return {"running": False, "pid": None, "exit_code": None}
    rc = proc.poll()
    return {
        "running": rc is None,
        "pid": proc.pid,
        "exit_code": rc,
    }


def _parse_skill_md(text: str) -> tuple[dict, str]:
    """解析 SKILL.md：返回 (frontmatter dict, 正文 strategy_text)。"""
    fm: dict = {}
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', text, re.DOTALL)
    if m:
        for line in m.group(1).split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip('"').strip("'")
        body_text = m.group(2).strip()
    else:
        body_text = text
    return fm, body_text


def _active_agent_brief() -> tuple[str, str]:
    """从 my_contacts.json 读当前激活员工名与技能 ID；读不到返回 ("", "")。"""
    contacts_path = RUNTIME_DIR / "my_contacts.json"
    try:
        if contacts_path.exists():
            data = json.loads(contacts_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return (str(data.get("skill_name") or "").strip(),
                        str(data.get("skill_id") or "").strip())
    except Exception:
        pass
    return "", ""


def _find_skill_md(skill_name: str) -> Path | None:
    """按员工(技能)名定位 skills/<name>/ 下的 SKILL.md；找不到返回 None。"""
    safe_name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', skill_name or "")
    if not safe_name:
        return None
    skill_dir = RUNTIME_DIR / "skills" / safe_name
    if not skill_dir.exists():
        # 兼容目录名与 frontmatter name 不一致：在 skills 下按名字兜底匹配
        root = RUNTIME_DIR / "skills"
        if root.exists():
            for d in root.iterdir():
                if d.is_dir():
                    md = next(d.rglob("SKILL.md"), None)
                    if md is not None:
                        try:
                            fm, _ = _parse_skill_md(md.read_text(encoding="utf-8", errors="replace"))
                        except Exception:
                            fm = {}
                        if fm.get("name", "").strip() == skill_name:
                            return md
        return None
    return next(skill_dir.rglob("SKILL.md"), None)


def _split_line_cols(line: str) -> list[str]:
    """一行内按逗号/顿号/制表符/竖线分隔成列。"""
    parts = re.split(r"[,，、\t|｜;；]", line)
    return [p.strip() for p in parts if p and p.strip()]


def _parse_contact_file(filename: str, raw: bytes) -> list[dict]:
    """解析名单文件，返回 [{name, chat_goal}]。

    支持 .txt/.csv/.xlsx/.docx。约定：第一列=备注名，第二列=聊天目标（可选）。
    """
    name_lower = (filename or "").lower()
    header_words = {"姓名", "备注名", "联系人", "客户", "name", "昵称", "微信名"}
    rows: list[list[str]] = []

    if name_lower.endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                rows.append(cells)
    elif name_lower.endswith(".docx"):
        import docx
        doc = docx.Document(io.BytesIO(raw))
        for para in doc.paragraphs:
            line = para.text.strip()
            if line:
                rows.append(_split_line_cols(line))
        for table in doc.tables:
            for r in table.rows:
                cells = [c.text.strip() for c in r.cells if c.text.strip()]
                if cells:
                    rows.append(cells)
    else:
        # txt / csv 按文本解析
        text = raw.decode("utf-8-sig", errors="replace")
        if name_lower.endswith(".csv"):
            reader = csv.reader(io.StringIO(text))
            for cols in reader:
                cols = [c.strip() for c in cols if c.strip()]
                if cols:
                    rows.append(cols)
        else:
            for line in text.splitlines():
                line = line.strip()
                if line:
                    cols = _split_line_cols(line)
                    if cols:
                        rows.append(cols)

    contacts: list[dict] = []
    seen: set[str] = set()
    for idx, cells in enumerate(rows):
        name = cells[0]
        goal = cells[1] if len(cells) > 1 else ""
        # 跳过表头行
        if idx == 0 and name in header_words:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        contacts.append({"name": name, "gender": "未知", "chat_goal": goal})
    return contacts


def _load_export_contacts() -> list[dict]:
    """读取 my_contacts.json 里有备注名的联系人；文件缺失/解析失败返回空列表。"""
    contacts_path = RUNTIME_DIR / "my_contacts.json"
    if not contacts_path.exists():
        return []
    try:
        data = json.loads(contacts_path.read_text(encoding="utf-8"))
        return [
            c for c in (data.get("contacts") or [])
            if isinstance(c, dict) and str(c.get("name") or "").strip()
        ]
    except Exception:
        return []


def build_contacts_export(fmt: str) -> tuple[bytes, str]:
    """生成名单导出内容，返回 (body_bytes, 文件名)；名单为空抛 ValueError。

    格式与 _parse_contact_file 的导入规则对齐，导出文件可直接回导。
    """
    contacts = _load_export_contacts()
    if not contacts:
        raise ValueError("名单为空，无可导出联系人")

    fmt = (fmt or "csv").lower()
    names = [str(c.get("name") or "").strip() for c in contacts]
    goals = [str(c.get("chat_goal") or "").strip() for c in contacts]

    if fmt == "txt":
        lines = ["备注名,聊天目标"]
        for name, goal in zip(names, goals):
            lines.append(f"{name},{goal}" if goal else name)
        return "\n".join(lines).encode("utf-8-sig"), "contacts_export.txt"

    if fmt == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["备注名", "聊天目标"])
        for name, goal in zip(names, goals):
            ws.append([name, goal])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "contacts_export.xlsx"

    if fmt == "docx":
        import docx
        doc = docx.Document()
        table = doc.add_table(rows=1, cols=2)
        hdr = table.rows[0].cells
        hdr[0].text = "备注名"
        hdr[1].text = "聊天目标"
        for name, goal in zip(names, goals):
            row = table.add_row().cells
            row[0].text = name
            row[1].text = goal
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue(), "contacts_export.docx"

    # csv 默认
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["备注名", "聊天目标"])
    for name, goal in zip(names, goals):
        writer.writerow([name, goal])
    return buf.getvalue().encode("utf-8-sig"), "contacts_export.csv"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默默认访问日志

    # ---------- 工具 ----------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_download(self, filename: str, content_type: str, body: bytes):
        """以附件形式返回一段文本/二进制内容，供浏览器/webview 触发下载。"""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _export_contacts(self, fmt: str):
        """HTTP 导出：把生成好的名单以附件形式返回（浏览器兜底路径）。"""
        try:
            body, filename = build_contacts_export(fmt)
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)
            return
        content_types = {
            ".csv": "text/csv; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        content_type = content_types.get(Path(filename).suffix, "application/octet-stream")
        self._send_download(filename, content_type, body)

    def _proxy_skill(self, method: str, path: str, query: str):
        """转发到技能平台后端。/skills-api/xxx -> /api/xxx"""
        sub = path[len("/skills-api/"):]
        url = f"{SKILL_BACKEND}/api/{sub}"
        if query:
            url += "?" + query
        headers = {}
        for h in ("Authorization", "X-Share-Code", "Content-Type"):
            v = self.headers.get(h)
            if v:
                headers[h] = v
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read()
                self.send_response(resp.status)
                ct = resp.headers.get("Content-Type", "application/octet-stream")
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            ct = e.headers.get("Content-Type", "application/json")
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._send_json({"error": f"代理失败: {e}"}, 502)

    def _install_skill(self, zip_bytes):
        """接收 zip 二进制，解压到 runtime/skills/<name>/，读 SKILL.md 配置策略。"""
        import re, shutil, zipfile, io
        # 从 query 拿 name
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        skill_name = qs.get("name", ["unknown"])[0]

        if not zip_bytes:
            self._send_json({"error": "空的 zip 数据"}, 400)
            return

        # 解压到 runtime/skills/<name>/
        safe_name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', skill_name)
        skill_dir = RUNTIME_DIR / "skills" / safe_name
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        skill_dir.mkdir(parents=True)
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zf.extractall(skill_dir)
        except Exception as e:
            self._send_json({"error": f"解压失败: {e}"}, 500)
            return

        # 找 SKILL.md
        skill_md = None
        for p in skill_dir.rglob("SKILL.md"):
            skill_md = p
            break
        if not skill_md:
            self._send_json({"error": "zip 里没有 SKILL.md"}, 400)
            return

        text = skill_md.read_text(encoding="utf-8")
        fm, body_text = _parse_skill_md(text)

        # 写 chat_strategy_config.json
        strategy_path = RUNTIME_DIR / "chat_strategy_config.json"
        cur = {}
        if strategy_path.exists():
            try:
                cur = json.loads(strategy_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        cur["ai_role"] = fm.get("name", skill_name)
        cur["strategy_text"] = body_text
        strategy_path.write_text(
            json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")

        # 写 downloaded_skills.json 记录
        dl_path = RUNTIME_DIR / "downloaded_skills.json"
        dl = []
        if dl_path.exists():
            try:
                dl = json.loads(dl_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        entry = {
            "id": qs.get("skill_id", [""])[0],
            "name": fm.get("name", skill_name),
            "version": fm.get("version", ""),
        }
        if not any(d.get("name") == entry["name"] for d in dl):
            dl.insert(0, entry)
        dl_path.write_text(json.dumps(dl, ensure_ascii=False, indent=2), encoding="utf-8")

        self._send_json({
            "ok": True,
            "name": fm.get("name", skill_name),
            "path": str(skill_dir),
            "configured": "chat_strategy_config.json",
        })

    def _delete_skill(self, body: dict):
        """删除已下载技能：移除 downloaded_skills.json 记录，并清理 skills/<name> 目录。"""
        import re, shutil
        name = str(body.get("name") or "").strip()
        if not name:
            self._send_json({"ok": False, "error": "缺少技能名称"}, 400)
            return

        # 1) 移除下载记录
        dl_path = RUNTIME_DIR / "downloaded_skills.json"
        dl = []
        if dl_path.exists():
            try:
                dl = json.loads(dl_path.read_text(encoding="utf-8"))
            except Exception:
                dl = []
        kept = [d for d in dl if str(d.get("name")) != name]
        dl_path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")

        # 2) 清理技能目录（限定在 runtime/skills 内，防路径穿越）
        skills_root = (RUNTIME_DIR / "skills").resolve()
        safe_name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', name)
        removed_dir = False
        if skills_root.exists():
            for d in skills_root.iterdir():
                if not d.is_dir():
                    continue
                target = d.resolve()
                if skills_root not in target.parents:
                    continue
                # 目录名匹配，或读取 SKILL.md 的 frontmatter name 匹配
                match = (d.name == safe_name)
                if not match:
                    md = next(d.rglob("SKILL.md"), None)
                    if md:
                        try:
                            txt = md.read_text(encoding="utf-8")
                            m = re.match(r'^---\s*\n(.*?)\n---', txt, re.DOTALL)
                            if m:
                                for line in m.group(1).split("\n"):
                                    if ":" in line:
                                        k, v = line.split(":", 1)
                                        if k.strip() == "name" and v.strip().strip('"').strip("'") == name:
                                            match = True
                                            break
                        except Exception:
                            pass
                if match:
                    shutil.rmtree(target, ignore_errors=True)
                    removed_dir = not target.exists()

        self._send_json({"ok": True, "name": name, "removed_dir": removed_dir})

    def _activate_skill(self, body: dict):
        """激活技能：设置联系人列表 + 主动发送开关。

        body.contacts: [{name, chat_goal}]（新格式，多个联系人）
        body.default_chat_goal: 全局默认聊天目标（弹窗预填的那个），
            联系人自己的 chat_goal 留空时由监控自动继承它。
        兼容旧格式 body.contact_name（单个联系人）。
        """
        proactive = bool(body.get("proactive", False))

        # 归一化为 contacts 列表
        raw_contacts = body.get("contacts")
        contacts_out = []
        if isinstance(raw_contacts, list):
            for c in raw_contacts:
                if isinstance(c, dict):
                    nm = str(c.get("name") or "").strip()
                    if nm:
                        contacts_out.append({
                            "name": nm,
                            "gender": str(c.get("gender") or "未知"),
                            "chat_goal": str(c.get("chat_goal") or "").strip(),
                        })
                elif isinstance(c, str) and c.strip():
                    contacts_out.append({"name": c.strip(), "gender": "未知", "chat_goal": ""})
        if not contacts_out:
            legacy = str(body.get("contact_name") or "").strip()
            if legacy:
                contacts_out.append({"name": legacy, "gender": "未知", "chat_goal": ""})
        if not contacts_out:
            self._send_json({"error": "缺少联系人名称"}, 400)
            return

        # 1. 写 my_contacts.json（保留原有字段，只替换 contacts）
        contacts_path = RUNTIME_DIR / "my_contacts.json"
        contacts = {"group_name": "Default", "strategy": "", "contacts": [], "default_chat_goal": ""}
        if contacts_path.exists():
            try:
                contacts = json.loads(contacts_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        # 弹窗只填了备注名和聊天目标，别把之前手工填的性别等信息抹成「未知」
        old_gender = {str(c.get("name") or "").strip(): str(c.get("gender") or "").strip()
                      for c in (contacts.get("contacts") or []) if isinstance(c, dict)}
        for c in contacts_out:
            kept = old_gender.get(c["name"], "")
            if kept and kept != "未知" and c.get("gender") in ("", "未知", None):
                c["gender"] = kept
        contacts["contacts"] = contacts_out
        # 记下当前激活的技能名，「策略配置」页据此判断这个技能带不带角色设定
        skill_name = str(body.get("skill_name") or "").strip()
        if skill_name:
            contacts["skill_name"] = skill_name
        skill_id = str(body.get("skill_id") or "").strip()
        if skill_id:
            contacts["skill_id"] = skill_id
        # 全局默认聊天目标也落盘：联系人没单独填目标时监控会用它兜底（chat_goal || default_chat_goal || group）
        default_goal = str(body.get("default_chat_goal") or "").strip()
        if default_goal:
            contacts["default_chat_goal"] = default_goal
        # 起始联系人：从名单里的哪一个开始轮询；空串表示从第一个开始。
        # 若该名字不在本次名单里，监控侧定位不到会自动回退到第一个。
        start_contact = str(body.get("start_contact") or "").strip()
        contacts["start_contact"] = start_contact
        contacts_path.write_text(
            json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8")

        # 2. 写 chat_strategy_config.json：proactive 开关 + 按当前激活员工回写策略正文。
        #    关键：策略不能停留在「最后下载」的那个技能。切换到别的已下载员工时，
        #    必须从对应 skills/<name>/SKILL.md 重新载入角色设定与策略文本，
        #    否则监控进程一直沿用旧员工的话术。
        strat_path = RUNTIME_DIR / "chat_strategy_config.json"
        strat = {}
        if strat_path.exists():
            try:
                strat = json.loads(strat_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        strat["proactive_enabled"] = proactive

        skill_md = _find_skill_md(skill_name) if skill_name else None
        if skill_md is not None:
            try:
                fm, body_text = _parse_skill_md(
                    skill_md.read_text(encoding="utf-8", errors="replace"))
                if fm.get("name"):
                    strat["ai_role"] = fm["name"]
                if body_text:
                    strat["strategy_text"] = body_text
            except Exception:
                pass
        strat_path.write_text(
            json.dumps(strat, ensure_ascii=False, indent=2), encoding="utf-8")

        self._send_json({"ok": True, "contacts": contacts_out, "proactive": proactive})

    # ---------- 路由 ----------
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 技能平台代理：/skills-api/xxx -> http://saas-ai.cn:8080/api/xxx
        if path.startswith("/skills-api/"):
            self._proxy_skill("GET", path, parsed.query)
            return

        if path == "/":
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return

        if path == "/api/status":
            with _state_lock:
                self._send_json({
                    "uptime": time.time() - _started_at,
                    "monitor": _monitor_status(),
                    "moments": _proc_status(_moments_proc),
                    "runtime_dir": str(RUNTIME_DIR),
                })
            return

        if path == "/api/session":
            # 从启动器共享的 session.json 读登录态：壳子数据目录已迁到 xHR，回退 x.HR 兼容旧版落盘
            import os
            appdata = Path(os.environ.get("APPDATA", ""))
            sp = appdata / "xHR" / "session.json"
            if not sp.exists():
                sp = appdata / "x.HR" / "session.json"
            if not sp.exists():
                self._send_json({"error": "未找到 session.json", "authenticated": False}, 404)
                return
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                self._send_json({
                    "authenticated": True,
                    "access_token": data.get("AccessToken", ""),
                    "username": data.get("Username", ""),
                    "user_id": data.get("UserId", 0),
                })
            except Exception as e:
                self._send_json({"error": str(e), "authenticated": False}, 500)
            return

        if path == "/api/version":
            # 返回 index.html 修改时间，前端据此自动刷新
            import os
            idx = RUNTIME_DIR / "web" / "index.html"
            self._send_json({"mtime": idx.stat().st_mtime if idx.exists() else 0})
            return

        if path == "/api/downloaded":
            # 读已下载技能列表
            p = RUNTIME_DIR / "downloaded_skills.json"
            if p.exists():
                try:
                    self._send_json(json.loads(p.read_text(encoding="utf-8")))
                except Exception:
                    self._send_json([])
            else:
                self._send_json([])
            return

        if path.startswith("/api/config/"):
            name = path.rsplit("/", 1)[-1]
            fname = CONFIG_FILES.get(name)
            if not fname:
                self._send_json({"error": "unknown config"}, 404)
                return
            p = RUNTIME_DIR / fname
            if not p.exists():
                self._send_json({})
                return
            try:
                self._send_json(json.loads(p.read_text(encoding="utf-8")))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/api/logs":
            logs = sorted(LOGS_DIR.glob("*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)
            self._send_json({
                "logs": [{"name": p.name, "size": p.stat().st_size,
                          "mtime": p.stat().st_mtime} for p in logs[:30]]
            })
            return

        if path == "/api/logs/live":
            # 返回最新修改的日志文件尾部，供前端实时滚动
            candidates = list(LOGS_DIR.glob("*.txt")) + list(LOGS_DIR.glob("*.log"))
            if not candidates:
                self._send_json({"name": "", "content": ""})
                return
            latest = max(candidates, key=lambda p: p.stat().st_mtime)
            try:
                text = latest.read_text(encoding="utf-8", errors="replace")
                tail = text[-4000:] if len(text) > 4000 else text
                self._send_json({"name": latest.name, "content": tail})
            except Exception as e:
                self._send_json({"name": latest.name, "content": f"[read error] {e}"})
            return

        if path == "/api/skills/role":
            self._send_json(_skill_role_info())
            return

        if path == "/api/contacts/export":
            self._export_contacts(qs.get("format", ["csv"])[0])
            return

        if path == "/api/stats":
            date_str = (qs.get("date", [""])[0]) or time.strftime("%Y-%m-%d")
            self._send_json(_build_daily_stats(date_str))
            return

        if path.startswith("/api/logs/"):
            name = path.rsplit("/", 1)[-1]
            p = LOGS_DIR / name
            if not p.exists() or ".." in name:
                self._send_json({"error": "not found"}, 404)
                return
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                if len(text) > 20000:
                    text = "...(truncated)...\n" + text[-20000:]
                self._send_json({"name": name, "content": text})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        # 静态图标等资源（logo.png / app.ico / favicon）
        if any(path.lower().endswith(ext) for ext in (".png", ".ico", ".svg", ".jpg", ".jpeg")):
            self._serve_static(path)
            return

        self._send_json({"error": "not found"}, 404)

    def _serve_static(self, path: str):
        """返回 web 目录下的图标等静态资源；白名单扩展名并限定在 web 目录内，防路径穿越。"""
        static_types = {
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".svg": "image/svg+xml",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }
        rel = path.lstrip("/")
        cand = (WEB_DIR / rel).resolve()
        web_root = WEB_DIR.resolve()
        if cand.suffix.lower() not in static_types:
            self._send_json({"error": "not found"}, 404)
            return
        if web_root not in cand.parents:
            self._send_json({"error": "not found"}, 404)
            return
        self._send_file(cand, static_types[cand.suffix.lower()])

    def do_PUT(self):
        path = self.path.split("?")[0]
        if not path.startswith("/api/config/"):
            self._send_json({"error": "not found"}, 404)
            return
        name = path.rsplit("/", 1)[-1]
        fname = CONFIG_FILES.get(name)
        if not fname:
            self._send_json({"error": "unknown config"}, 404)
            return
        body = self._read_body()
        p = RUNTIME_DIR / fname
        p.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        self._send_json({"ok": True, "path": fname})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}

        if path == "/api/skills/install":
            self._install_skill(raw)
            return

        if path == "/api/contacts/import":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            fname = (qs.get("file", [""])[0] or "contacts.txt")
            try:
                contacts = _parse_contact_file(fname, raw)
                self._send_json({"ok": True, "contacts": contacts})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/skills/activate":
            self._activate_skill(body)
            return

        if path == "/api/skills/delete":
            self._delete_skill(body)
            return

        if path == "/api/monitor/start":
            global _monitor_proc
            with _state_lock:
                if _monitor_proc and _monitor_proc.poll() is None:
                    self._send_json({"ok": False, "error": "already running"})
                    return
                stray = _detached_monitor_pid()
                if stray:
                    self._send_json({"ok": False, "error": f"already running (pid={stray})，请先点停止"})
                    return
                LOGS_DIR.mkdir(exist_ok=True)
                log_f = open(LOGS_DIR / f"web_{time.strftime('%Y%m%d_%H%M%S')}.log", "ab")
                # 把当前激活员工名/技能 ID 显式传给监控进程：
                # 进程启动即按该员工隔离记忆与策略，避免读缓存或沿用旧员工归属。
                agent_name, skill_id = _active_agent_brief()
                args = [str(PYTHON_EXE), "wechat_vision_rpa.py"]
                if agent_name:
                    args += ["--agent-name", agent_name]
                if skill_id:
                    args += ["--skill-id", skill_id]
                _monitor_proc = subprocess.Popen(
                    args,
                    cwd=str(RUNTIME_DIR),
                    stdout=log_f, stderr=subprocess.STDOUT,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                )
                _write_pid_file(_monitor_proc.pid)
            self._send_json({"ok": True, "pid": _monitor_proc.pid})
            return

        if path == "/api/monitor/stop":
            stopped_pid = 0
            with _state_lock:
                if _monitor_proc and _monitor_proc.poll() is None:
                    stopped_pid = _monitor_proc.pid
                    _monitor_proc.terminate()
                    try:
                        _monitor_proc.wait(timeout=5)
                    except Exception:
                        _monitor_proc.kill()
                else:
                    # GUI 重启过：句柄没了，但 pid 文件里的进程可能还在跑
                    stopped_pid = _kill_detached_monitor()
                try:
                    if MONITOR_PID_FILE.exists():
                        MONITOR_PID_FILE.unlink()
                except Exception:
                    pass
            self._send_json({"ok": True, "pid": stopped_pid})
            return

        if path == "/api/moments/post":
            text = body.get("text", "")
            images = body.get("images", [])
            args = [str(PYTHON_EXE), "wechat_moments_publisher.py", "--text", text]
            if images:
                args += ["--images"] + list(images)
            try:
                proc = subprocess.Popen(args, cwd=str(RUNTIME_DIR),
                                        creationflags=0x08000000)
                self._send_json({"ok": True, "pid": proc.pid})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/moments/start-queue":
            global _moments_proc
            with _state_lock:
                if _moments_proc and _moments_proc.poll() is None:
                    self._send_json({"ok": False, "error": "already running"})
                    return
                _moments_proc = subprocess.Popen(
                    [str(PYTHON_EXE), "wechat_moments_publisher.py", "--queue"],
                    cwd=str(RUNTIME_DIR),
                    creationflags=0x08000000,
                )
            self._send_json({"ok": True, "pid": _moments_proc.pid})
            return

        if path == "/api/moments/stop":
            with _state_lock:
                if _moments_proc and _moments_proc.poll() is None:
                    _moments_proc.terminate()
            self._send_json({"ok": True})
            return

        if path == "/api/cleanup/screenshots":
            # 删除对比图(compare)与聊天截图(chat)，保留朋友圈素材(moments)
            shot_root = RUNTIME_DIR / "screenshots"
            removed = 0
            freed = 0
            for sub in ("compare", "chat"):
                r, b = _purge_files(shot_root / sub, exts={".png", ".jpg", ".jpeg", ".bmp"})
                removed += r
                freed += b
            self._send_json({"ok": True, "removed": removed, "freed_bytes": freed})
            return

        if path == "/api/cleanup/logs":
            log_dir = RUNTIME_DIR / "logs"
            removed, freed = _purge_files(log_dir, exts={".txt", ".log"})
            self._send_json({"ok": True, "removed": removed, "freed_bytes": freed})
            return

        self._send_json({"error": "not found"}, 404)


def _parse_msg_time(ts) -> float | None:
    """把消息时间字段（ISO 字符串 / epoch 秒 / epoch 毫秒）解析成 epoch 秒。"""
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            v = float(ts)
            return v / 1000 if v > 1e12 else v
        s = str(ts).strip()
        if not s:
            return None
        # 纯数字
        if s.isdigit():
            v = float(s)
            return v / 1000 if v > 1e12 else v
        # ISO 字符串
        from datetime import datetime
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(s[:26], fmt).timestamp()
            except Exception:
                pass
        return None
    except Exception:
        return None


def _skill_role_info() -> dict:
    """判断当前激活的技能包里有没有「角色设定/技能介绍」这一段。

    没有的话，「策略配置」页就不显示 AI 角色设定输入框——角色本来就该由技能自带，
    留一个必须手填的空框只会让人以为是漏配。
    """
    role_markers = ("技能介绍", "角色设定", "你就是", "## 角色")
    try:
        contacts = json.loads((RUNTIME_DIR / "my_contacts.json").read_text(encoding="utf-8"))
    except Exception:
        contacts = {}
    active = str(contacts.get("skill_name") or "").strip()

    root = RUNTIME_DIR / "skills"
    candidates = []
    if root.exists():
        # 没记录激活技能时，按目录修改时间倒序，最近下的那个就是当前在用的
        dirs = sorted([d for d in root.iterdir() if d.is_dir()],
                      key=lambda d: d.stat().st_mtime, reverse=True)
        if active:
            dirs = [d for d in dirs if d.name == active] or dirs
        for d in dirs:
            candidates.extend(d.glob("*/SKILL.md"))
    has_role = False
    skill_name = active
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if any(k in text for k in role_markers):
            has_role = True
            skill_name = skill_name or p.parts[-3]
            break
    return {"has_role": has_role, "skill_name": skill_name}


def _extract_raw_goal(text: str) -> str:
    """从 SKILL.md / strategy_text 中提取「用户原始目标：xxx」，取不到返回空串。"""
    if not text:
        return ""
    m = re.search(r'用户原始目标[:：]\s*(.+)', text)
    if m:
        return m.group(1).strip().strip('，。,.!！?？~～… ')
    return ""


def _goal_from_skill(agent: str) -> str:
    """按员工(技能)名兜底取聊天目标。

    优先级：skills/<agent>/SKILL.md 的「用户原始目标」→ 该 SKILL.md frontmatter
    description → chat_strategy_config.json（仅 ai_role 与当前 agent 匹配）的 strategy_text
    「用户原始目标」。全部取不到返回空串。
    """
    agent = str(agent or "").strip()
    if not agent or agent == "未分配":
        return ""

    # 1) skills/<agent>/SKILL.md
    skill_dir = RUNTIME_DIR / "skills" / agent
    if skill_dir.exists():
        for p in skill_dir.rglob("SKILL.md"):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            goal = _extract_raw_goal(text)
            if goal:
                return goal
            # 回退 frontmatter description（截断过长文本，避免日报列被撑宽）
            m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
            if m:
                for line in m.group(1).split("\n"):
                    if line.strip().startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                        if desc:
                            return desc[:80] + ("…" if len(desc) > 80 else "")
            break  # 只看第一个 SKILL.md

    # 2) 当前激活员工的策略配置（ai_role 匹配才用，避免串到别的员工目标）
    try:
        sp = RUNTIME_DIR / "chat_strategy_config.json"
        if sp.exists():
            cfg = json.loads(sp.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and str(cfg.get("ai_role") or "").strip() == agent:
                goal = _extract_raw_goal(str(cfg.get("strategy_text") or ""))
                if goal:
                    return goal
    except Exception:
        pass
    return ""


def _build_daily_stats(date_str: str) -> dict:
    """汇总某一天的聊天统计：谁、聊了几条、目标、是否达成。"""
    from datetime import datetime, timedelta
    try:
        day_start = datetime.strptime(date_str, "%Y-%m-%d").timestamp()
    except Exception:
        day_start = time.time() - 86400
    day_end = day_start + 86400

    # 1. 联系人名单（当前激活员工）
    contacts_path = RUNTIME_DIR / "my_contacts.json"
    contacts = []
    cdata = {}
    try:
        cdata = json.loads(contacts_path.read_text(encoding="utf-8"))
        contacts = cdata.get("contacts", [])
    except Exception:
        cdata = {}
    default_goal = cdata.get("default_chat_goal", "") if contacts else ""
    current_agent = str(cdata.get("skill_name") or "").strip() or "未分配"

    # 2. 读对话记忆（兼容新结构 <agent>/<account>/<contact>.json 与旧结构 <account>/<contact>.json）
    mem_root = RUNTIME_DIR / "conversation_memory"
    per_contact = {}  # (agent, contact) -> {msg_count, last_active, file_mtime}
    if mem_root.exists():
        for jf in mem_root.rglob("*.json"):
            rel = jf.relative_to(mem_root).parts
            if len(rel) == 3:
                agent, _account, fname = rel
            elif len(rel) == 2:
                agent, fname = "未分配", rel[1]
            else:
                continue
            name = Path(fname).stem
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue
            file_agent = str(data.get("agent_name") or "").strip()
            if file_agent:
                agent = file_agent
            msgs = data.get("messages", [])
            today_count = 0
            last_ts = 0
            for m in msgs:
                # 记忆里的 timestamp 只有「14:02」，没有日期；日期在 seen_at 上
                t = _parse_msg_time(m.get("seen_at")) or _parse_msg_time(m.get("timestamp"))
                if t and day_start <= t < day_end:
                    today_count += 1
                    if t > last_ts:
                        last_ts = t
            key = (agent, name)
            if today_count > 0 or key not in per_contact:
                per_contact[key] = {
                    "msg_count": today_count,
                    "last_active": last_ts,
                    "file_mtime": jf.stat().st_mtime,
                }

    # 3. 读达成事件
    events_root = RUNTIME_DIR / "events"
    achieved = {}  # (agent, contact) -> {reason, summary, occurred_at, handoff}
    if events_root.exists():
        for jf in events_root.glob("*.jsonl"):
            try:
                for line in jf.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    ev = json.loads(line)
                    ts = ev.get("occurred_at", 0) / 1000
                    if not (day_start <= ts < day_end):
                        continue
                    c = ev.get("contact", "")
                    if not c or c.startswith("_test"):
                        continue
                    agent = str(ev.get("skill_name") or "").strip() or "未分配"
                    reason = ev.get("reason", "")
                    etype = ev.get("event_type", "")
                    # 「对方回复较敷衍/停止自动回复」这类是转人工，不是成交达成，
                    # 之前一股脑算成「达成」，日报里就出现了「达成 1 / 未达成 -1」
                    is_handoff = (etype == "human_handoff" or
                                  any(k in reason for k in ("敷衍", "停止自动回复", "转人工", "人工接管")))
                    achieved[(agent, c)] = {
                        "handoff": is_handoff,
                        "reason": reason,
                        "summary": ev.get("summary", ""),
                        "chat_goal": str(ev.get("chat_goal") or "").strip(),
                        "occurred_at": ev.get("occurred_at", 0),
                    }
            except Exception:
                continue

    # 4. 组装：只保留真正聊过的联系人（当天有消息记录，或当天有达成/转人工事件）
    goal_by_name = {}
    for c in contacts:
        nm = str(c.get("name") or "").strip()
        if nm:
            goal_by_name[nm] = str(c.get("chat_goal") or "").strip()

    def _goal_for(agent, name, ev):
        g = goal_by_name.get(name, "")
        if not g and ev:
            g = str(ev.get("chat_goal") or "").strip()
        if not g:
            g = str(default_goal or "").strip()
        if not g:
            # 联系人/默认都没填时，按员工(技能)名从 SKILL.md / 策略配置兜底取目标
            g = _goal_from_skill(agent)
        return g or "未设定目标"

    def _build_row(agent, name, goal, m, ev):
        count = m.get("msg_count", 0)
        last = m.get("last_active", 0) or m.get("file_mtime", 0)
        if not last and ev and ev.get("occurred_at"):
            last = ev["occurred_at"] / 1000
        if ev and ev.get("handoff"):
            status = "handoff"
            note = "已转人工"
            if ev.get("reason"):
                note += " · " + ev["reason"]
        elif ev:
            status = "done"
            note = ev.get("reason") or ev.get("summary") or "已达成目标"
        elif count > 0:
            status = "pending"
            note = "已聊但未记录达成事件"
        else:
            status = "idle"
            note = "今日无消息"
        return {
            "agent": agent,
            "name": name,
            "goal": goal,
            "msg_count": count,
            "last_active": time.strftime("%H:%M", time.localtime(last)) if last else "--",
            "status": status,
            "note": note,
        }

    rows = []
    seen = set()

    # 当天有消息记录的联系人
    for key, m in per_contact.items():
        if m.get("msg_count", 0) <= 0:
            continue
        agent, name = key
        ev = achieved.get(key)
        rows.append(_build_row(agent, name, _goal_for(agent, name, ev), m, ev))
        seen.add(key)

    # 当天有达成/转人工事件、但记忆里没消息的联系人
    for key, ev in achieved.items():
        if key in seen:
            continue
        agent, name = key
        rows.append(_build_row(agent, name, _goal_for(agent, name, ev), {}, ev))
        seen.add(key)

    rows.sort(key=lambda r: (r["agent"], r["name"]))

    done = sum(1 for r in rows if r["status"] == "done")
    chatting = sum(1 for r in rows if r["msg_count"] > 0)
    # 未达成 = 聊过没成交 + 已转人工；别再拿「聊天数 - 达成数」算，会算出负数
    pending = sum(1 for r in rows if r["status"] in ("pending", "handoff"))

    # 5. 按技能分组：downloaded_skills.json 里 name -> id 映射（id 归一为字符串）
    name2id = {}
    try:
        dl_raw = json.loads((RUNTIME_DIR / "downloaded_skills.json").read_text(encoding="utf-8"))
        if isinstance(dl_raw, list):
            for d in dl_raw:
                if isinstance(d, dict) and d.get("name"):
                    name2id[str(d["name"]).strip()] = str(d.get("id") or "")
    except Exception:
        pass

    groups, order = {}, []
    for r in rows:
        agent = r["agent"]
        if agent not in groups:
            groups[agent] = []
            order.append(agent)
        groups[agent].append(r)

    skills = []
    for agent in order:
        grow = groups[agent]
        skills.append({
            "id": name2id.get(agent, ""),
            "name": agent,
            "summary": {
                "total_contacts": len(grow),
                "chatting_today": sum(1 for r in grow if r["msg_count"] > 0),
                "achieved": sum(1 for r in grow if r["status"] == "done"),
                "not_achieved": sum(1 for r in grow if r["status"] in ("pending", "handoff")),
            },
            "rows": grow,
        })

    return {
        "date": date_str,
        "summary": {
            "total_contacts": len(rows),
            "chatting_today": chatting,
            "achieved": done,
            "not_achieved": pending,
        },
        "rows": rows,
        "skills": skills,
    }


def _purge_files(root: Path, exts: set[str]) -> tuple[int, int]:
    """删除 root 下所有指定扩展名的文件（递归），保留目录结构。"""
    if not root.exists():
        return 0, 0
    removed = 0
    freed = 0
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in exts:
            try:
                size = f.stat().st_size
                f.unlink()
                removed += 1
                freed += size
            except Exception:
                pass
    return removed, freed


def main():
    global _started_at
    _started_at = time.time()
    LOGS_DIR.mkdir(exist_ok=True)
    WEB_DIR.mkdir(exist_ok=True)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"销售策略与高情商沟通官: {url}")
    # 延迟开浏览器，等服务起来
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
        with _state_lock:
            for p in (_monitor_proc, _moments_proc):
                if p and p.poll() is None:
                    p.terminate()


if __name__ == "__main__":
    main()
