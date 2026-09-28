#!/usr/bin/env python3
"""Export legacy WPS Notes (note.wps.cn) to Markdown.

The exporter runs against the already authenticated web application.  Login
state and encryption material stay in the temporary browser context and are
never written to the export directory.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import traceback
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, unquote_to_bytes, urlparse
from zoneinfo import ZoneInfo

import yaml
from bs4 import BeautifulSoup, NavigableString, Tag
from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright


APP_URL = "https://note.wps.cn/"
NOTE_API_HOST = "note-api.wps.cn"
DEFAULT_TZ = "Asia/Shanghai"
PAGE_SIZE = 50
LOGIN_TIMEOUT_MS = 10 * 60 * 1000
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ExportError(RuntimeError):
    pass


@dataclass
class ExportIssue:
    kind: str
    message: str
    note_id: str = ""
    title: str = ""


@dataclass
class ExportedNote:
    wps_id: str
    title: str
    group: str
    path: str
    created_at: str
    updated_at: str
    pinned: bool
    image_count: int = 0
    unsupported: list[str] = field(default_factory=list)


@dataclass
class BrowserSession:
    browser: Browser | None
    context: BrowserContext
    page: Page
    attached: bool = False


def log(message: str) -> None:
    print(message, flush=True)


def safe_name(value: str, fallback: str = "未命名") -> str:
    value = INVALID_FILENAME.sub("_", (value or "").strip())
    value = re.sub(r"\s+", " ", value).rstrip(". ")
    if not value:
        value = fallback
    if value.upper() in WINDOWS_RESERVED:
        value = f"_{value}"
    return value[:120].rstrip(". ") or fallback


def unique_path(directory: Path, stem: str, suffix: str, identity: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    short_id = safe_name(identity, "duplicate")[-10:]
    candidate = directory / f"{stem}-{short_id}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{short_id}-{index}{suffix}"
        index += 1
    return candidate


def epoch_to_iso(value: Any, tz_name: str = DEFAULT_TZ) -> str:
    if value in (None, "", 0, "0"):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
            return parsed.astimezone(ZoneInfo(tz_name)).isoformat(timespec="seconds")
        except ValueError:
            return str(value)
    if number > 10_000_000_000:
        number /= 1000
    return datetime.fromtimestamp(number, tz=timezone.utc).astimezone(
        ZoneInfo(tz_name)
    ).isoformat(timespec="seconds")


def pick(note: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in note and note[key] is not None:
            return note[key]
    return default


def note_id(note: dict[str, Any]) -> str:
    return str(pick(note, "noteId", "note_id", "id", default=""))


def normalize_title(note: dict[str, Any]) -> str:
    title = str(pick(note, "title", default="") or "").strip()
    if not title:
        summary = str(pick(note, "summary", default="") or "").strip()
        title = summary.splitlines()[0].strip() if summary else "无标题便签"
    return title


def yaml_front_matter(metadata: dict[str, Any]) -> str:
    body = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{body}\n---\n"


def prepare_html(html: str) -> tuple[BeautifulSoup, list[str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    unsupported: set[str] = set()

    for checkbox in soup.select('input[type="checkbox"]'):
        checked = checkbox.has_attr("checked") or str(checkbox.get("aria-checked", "")).lower() == "true"
        checkbox.replace_with(NavigableString("[x] " if checked else "[ ] "))

    for element in soup.find_all(style=True):
        style = str(element.get("style", "")).lower()
        if "color:" in style:
            unsupported.add("文字颜色")
        if "font-size:" in style:
            unsupported.add("字体大小")
        if "text-align:" in style:
            unsupported.add("对齐方式")
        if "font-family:" in style:
            unsupported.add("字体")

    for element in soup.select('[notetype="AUDIO"], audio'):
        unsupported.add("音频组件")
        replacement = soup.new_tag("p")
        replacement.string = "[未转换的 WPS 音频组件]"
        element.replace_with(replacement)

    for element in soup.select("video, iframe, canvas"):
        unsupported.add(f"{element.name} 组件")

    for element in soup.find_all("font"):
        element.unwrap()

    for element in soup.select("script, style"):
        element.decompose()

    return soup, sorted(unsupported)


def _render_inline(node: Any) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    name = (node.name or "").lower()
    children = "".join(_render_inline(child) for child in node.children)
    if name == "br":
        return "  \n"
    if name in {"strong", "b"}:
        return f"**{children.strip()}**" if children.strip() else ""
    if name in {"em", "i"}:
        return f"*{children.strip()}*" if children.strip() else ""
    if name in {"s", "strike", "del"}:
        return f"~~{children.strip()}~~" if children.strip() else ""
    if name == "code" and (not node.parent or node.parent.name != "pre"):
        ticks = "``" if "`" in children else "`"
        return f"{ticks}{children}{ticks}"
    if name == "a":
        href = str(node.get("href", "")).strip()
        label = children.strip() or href
        return f"[{label}]({href})" if href else label
    if name == "img":
        source = str(node.get("src", "")).strip()
        alt = str(node.get("alt", "图片")).strip() or "图片"
        title = str(node.get("title", "")).strip()
        suffix = f' "{title}"' if title else ""
        return f"![{alt}]({source}{suffix})" if source else f"[{alt}]"
    return children


def _render_block(node: Any, depth: int = 0) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    name = (node.name or "").lower()
    if name in {"ul", "ol"}:
        lines: list[str] = []
        ordered = name == "ol"
        item_number = int(node.get("start", 1) or 1)
        for child in node.children:
            if not isinstance(child, Tag) or child.name != "li":
                continue
            prefix = f"{item_number}. " if ordered else "- "
            item_number += 1
            inline_parts: list[str] = []
            nested_parts: list[str] = []
            for item_child in child.children:
                if isinstance(item_child, Tag) and item_child.name in {"ul", "ol"}:
                    nested_parts.append(_render_block(item_child, depth + 1).rstrip())
                else:
                    inline_parts.append(_render_block(item_child, depth + 1))
            line = "".join(inline_parts).strip()
            lines.append("  " * depth + prefix + line)
            lines.extend(part for part in nested_parts if part)
        return "\n".join(lines) + "\n\n"
    if name == "pre":
        language = ""
        code = node.find("code")
        if code:
            classes = code.get("class", [])
            language = next((c.removeprefix("language-") for c in classes if c.startswith("language-")), "")
        text = node.get_text("", strip=False).rstrip("\n")
        return f"```{language}\n{text}\n```\n\n"
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return f"{'#' * int(name[1])} {_render_inline(node).strip()}\n\n"
    if name == "blockquote":
        body = "".join(_render_block(child, depth) for child in node.children).strip()
        return "\n".join(f"> {line}" for line in body.splitlines()) + "\n\n"
    if name == "hr":
        return "---\n\n"
    if name == "table":
        # Keeping valid HTML is safer than flattening merged cells or losing headers.
        return f"{str(node)}\n\n"
    if name in {"p", "div", "section", "article"}:
        body = "".join(_render_block(child, depth) for child in node.children).strip()
        return f"{body}\n\n" if body else ""
    if name == "li":
        return _render_inline(node)
    if name in {"br", "strong", "b", "em", "i", "s", "strike", "del", "code", "a", "img"}:
        return _render_inline(node)
    return "".join(_render_block(child, depth) for child in node.children)


def markdown_from_html(html: str) -> tuple[str, list[str]]:
    soup, unsupported = prepare_html(html)
    markdown = "".join(_render_block(child) for child in soup.children)
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = re.sub(r"(?m)^(\[[ xX]\])\s+", r"- \1 ", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return markdown, unsupported


def extension_for(content_type: str, url: str, data: bytes) -> str:
    clean_type = content_type.split(";", 1)[0].lower().strip()
    aliases = {"image/jpeg": ".jpg", "image/svg+xml": ".svg", "image/webp": ".webp"}
    if clean_type in aliases:
        return aliases[clean_type]
    guessed = mimetypes.guess_extension(clean_type) if clean_type else None
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    path_suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", path_suffix):
        return path_suffix
    signatures = ((b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"), (b"RIFF", ".webp"))
    for signature, suffix in signatures:
        if data.startswith(signature):
            return suffix
    return ".bin"


def decode_data_url(url: str) -> tuple[bytes, str]:
    header, payload = url.split(",", 1)
    content_type = header[5:].split(";", 1)[0] or "application/octet-stream"
    if ";base64" in header:
        return base64.b64decode(payload), content_type
    return unquote_to_bytes(payload), content_type


def launch_session(playwright: Any, args: argparse.Namespace) -> BrowserSession:
    if args.cdp:
        log(f"连接现有浏览器：{args.cdp}")
        browser = playwright.chromium.connect_over_cdp(args.cdp)
        contexts = browser.contexts
        if not contexts:
            raise ExportError("CDP 浏览器没有可用上下文。")
        context = contexts[0]
        page = next((p for p in context.pages if "note.wps.cn" in p.url), None)
        page = page or context.new_page()
        return BrowserSession(browser=browser, context=context, page=page, attached=True)

    launch_options: dict[str, Any] = {"headless": False}
    if args.browser == "edge":
        launch_options["channel"] = "msedge"
    try:
        browser = playwright.chromium.launch(**launch_options)
    except PlaywrightError as error:
        if args.browser == "auto" and sys.platform == "win32":
            edge_paths = (
                Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
                Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
            )
            edge = next((p for p in edge_paths if p.is_file()), None)
            if edge:
                log("未找到 Playwright Chromium，改用本机 Microsoft Edge。")
                browser = playwright.chromium.launch(headless=False, executable_path=str(edge))
            else:
                raise ExportError(
                    "没有可用浏览器。请先运行：playwright install chromium"
                ) from error
        else:
            raise ExportError(
                "无法启动浏览器。请先运行：playwright install chromium"
            ) from error
    context = browser.new_context(accept_downloads=False)
    page = context.new_page()
    return BrowserSession(browser=browser, context=context, page=page)


STORE_JS = """
() => {
  const root = document.querySelector('#route');
  const vm = root && root.__vue__;
  return vm && vm.$store ? vm.$store : null;
}
"""


def wait_for_login(page: Page, timeout_ms: int) -> dict[str, Any]:
    if "note.wps.cn" not in page.url:
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
    log("请在打开的浏览器中登录 WPS；登录成功后程序会自动继续。")
    try:
        page.wait_for_function(
            """
            () => {
              const root = document.querySelector('#route');
              const store = root && root.__vue__ && root.__vue__.$store;
              const user = store && store.state && store.state.user && store.state.user.user;
              return Boolean(user && (user.userid || user.userId));
            }
            """,
            timeout=timeout_ms,
        )
    except PlaywrightError as error:
        raise ExportError("等待登录超时，未读取任何便签。") from error
    snapshot = page.evaluate(
        """
        () => {
          const store = document.querySelector('#route').__vue__.$store;
          const user = store.state.user.user || {};
          return {userid: String(user.userid || user.userId || ''), nickname: user.nickname || user.name || ''};
        }
        """
    )
    if not snapshot.get("userid"):
        raise ExportError("已打开页面，但未能识别 WPS 用户。")
    log(f"已识别登录账号：{snapshot.get('nickname') or snapshot['userid']}")
    return snapshot


def _dispatch_once(
    page: Page,
    action: str,
    payload: dict[str, Any] | None,
    timeout_ms: int = 120_000,
) -> dict[str, Any]:
    """Run a Vuex action while retaining its Promise inside the web page.

    Awaiting WPS's Vuex Promise directly through Page.evaluate can make a long
    pagination request eligible for Chromium's promise garbage collection.
    Keeping both the task and Promise on window avoids that cross-context race.
    """
    task_id = f"wps-export-{uuid.uuid4().hex}"
    page.evaluate(
        """
        ({action, payload, taskId}) => {
          const root = document.querySelector('#route');
          const store = root && root.__vue__ && root.__vue__.$store;
          if (!store) throw new Error('WPS Vuex store not found');
          const tasks = window.__wpsExporterTasks || (window.__wpsExporterTasks = {});
          const task = tasks[taskId] = {done: false, result: null, error: null, promise: null};
          try {
            task.promise = Promise.resolve(store.dispatch(action, payload));
            task.promise.then(
              response => {
                const plainData = response && response.data != null
                  ? JSON.parse(JSON.stringify(response.data))
                  : null;
                task.result = response
                  ? {status: Number(response.status || 0), data: plainData}
                  : {status: 0, data: null};
                task.done = true;
                task.promise = null;
              },
              error => {
                task.error = {
                  name: String(error && error.name || 'Error'),
                  message: String(error && error.message || error || 'unknown error')
                };
                task.done = true;
                task.promise = null;
              }
            );
          } catch (error) {
            task.error = {
              name: String(error && error.name || 'Error'),
              message: String(error && error.message || error || 'unknown error')
            };
            task.done = true;
          }
        }
        """,
        {"action": action, "payload": payload, "taskId": task_id},
    )

    deadline = time.monotonic() + timeout_ms / 1000
    state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        state = page.evaluate(
            """
            taskId => {
              const tasks = window.__wpsExporterTasks || {};
              const task = tasks[taskId];
              if (!task) return {missing: true};
              if (!task.done) return {done: false};
              return {done: true, result: task.result, error: task.error};
            }
            """,
            task_id,
        )
        if state.get("missing"):
            raise PlaywrightError("WPS page was reloaded while waiting for its request")
        if state.get("done"):
            break
        page.wait_for_timeout(100)
    else:
        raise PlaywrightError(f"WPS action {action} timed out after {timeout_ms // 1000}s")

    page.evaluate(
        """taskId => { if (window.__wpsExporterTasks) delete window.__wpsExporterTasks[taskId]; }""",
        task_id,
    )
    if state and state.get("error"):
        error = state["error"]
        raise ExportError(
            f"WPS 页面内部调用 {action} 失败：{error.get('message', 'unknown error')}"
        )
    return (state or {}).get("result") or {"status": 0, "data": None}


def dispatch(page: Page, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    last_error: PlaywrightError | None = None
    for attempt in range(2):
        try:
            return _dispatch_once(page, action, payload)
        except PlaywrightError as error:
            last_error = error
            if attempt:
                break
            log(f"  {action} 调用中断，正在重试一次……")
            page.wait_for_function(
                """
                () => {
                  const root = document.querySelector('#route');
                  return Boolean(root && root.__vue__ && root.__vue__.$store);
                }
                """,
                timeout=30_000,
            )
            page.wait_for_timeout(300)
    raise ExportError(f"WPS 页面调用 {action} 中断：{last_error}") from last_error


def load_groups(page: Page) -> dict[str, dict[str, Any]]:
    response = dispatch(page, "getAllGroups")
    if response.get("status") != 200:
        raise ExportError(f"读取分组失败（HTTP {response.get('status', 0)}）。")
    groups = page.evaluate(
        """
        () => {
          const store = document.querySelector('#route').__vue__.$store;
          return JSON.parse(JSON.stringify(store.state.group.all || {}));
        }
        """
    )
    return {
        str(group_id): value
        for group_id, value in (groups or {}).items()
        if int(value.get("valid", 1) or 0) == 1
    }


def response_notes(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data") or {}
    notes = data.get("webNotes") or data.get("notes") or []
    return notes if isinstance(notes, list) else []


def paginate_action(
    page: Page,
    action: str,
    base_payload: dict[str, Any],
    label: str,
    max_pages: int,
) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for page_number in range(max_pages):
        payload = {**base_payload, "rows": PAGE_SIZE, "startIndex": page_number * PAGE_SIZE}
        response = dispatch(page, action, payload)
        if response.get("status") != 200:
            raise ExportError(f"读取{label}失败（HTTP {response.get('status', 0)}，第 {page_number + 1} 页）。")
        rows = response_notes(response)
        for row in rows:
            current_id = note_id(row)
            if current_id and current_id not in seen:
                ids.append(current_id)
                seen.add(current_id)
        log(f"  {label}：已读取 {len(ids)} 条")
        if len(rows) < PAGE_SIZE:
            return ids
    raise ExportError(f"{label}超过安全上限 {max_pages * PAGE_SIZE} 条，请提高 --max-pages。")


def collect_notes(
    page: Page,
    user_id: str,
    groups: dict[str, dict[str, Any]],
    max_pages: int,
    include_recycle: bool,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    membership: dict[str, str] = {}
    ordered_ids: list[str] = []
    seen: set[str] = set()

    home_ids = paginate_action(page, "getHomeNotes", {"uid": user_id}, "未分组", max_pages)
    for current_id in home_ids:
        if current_id not in seen:
            ordered_ids.append(current_id)
            seen.add(current_id)
        membership.setdefault(current_id, "未分组")

    for group_id, group in groups.items():
        group_name = str(group.get("groupName") or "未命名分组")
        group_ids = paginate_action(
            page,
            "getNotesByGroupId",
            {"groupId": group_id},
            f"分组“{group_name}”",
            max_pages,
        )
        for current_id in group_ids:
            if current_id not in seen:
                ordered_ids.append(current_id)
                seen.add(current_id)
            membership[current_id] = group_name

    if include_recycle:
        recycle_ids = paginate_action(
            page, "getRecycleBinNotes", {"uid": user_id}, "回收站", max_pages
        )
        for current_id in recycle_ids:
            if current_id not in seen:
                ordered_ids.append(current_id)
                seen.add(current_id)
            membership[current_id] = "回收站"

    notes_by_id = page.evaluate(
        """
        () => {
          const store = document.querySelector('#route').__vue__.$store;
          return JSON.parse(JSON.stringify(store.state.notes.all || {}));
        }
        """
    )
    notes = [notes_by_id[current_id] for current_id in ordered_ids if current_id in notes_by_id]
    missing = [current_id for current_id in ordered_ids if current_id not in notes_by_id]
    if missing:
        raise ExportError(f"列表中有 {len(missing)} 条便签未进入网页缓存，无法安全导出。")
    return notes, membership


def select_note_and_read_html(page: Page, current_id: str, recycle: bool = False) -> str:
    if recycle and "/recycle" not in page.url:
        page.evaluate(
            """() => document.querySelector('#route').__vue__.$router.push('/recycle').catch(() => {})"""
        )
        page.wait_for_timeout(400)
    elif not recycle and "/recycle" in page.url:
        page.evaluate(
            """() => document.querySelector('#route').__vue__.$router.push('/').catch(() => {})"""
        )
        page.wait_for_timeout(400)

    page.evaluate(
        """
        async (noteId) => {
          const store = document.querySelector('#route').__vue__.$store;
          const note = store.state.notes.all[noteId];
          if (!note) throw new Error(`note not found: ${noteId}`);
          await store.dispatch('selectNextNote', note);
        }
        """,
        current_id,
    )

    try:
        page.wait_for_function(
            """
            (noteId) => {
              const root = document.querySelector('#route').__vue__;
              const store = root.$store;
              if (String(store.state.content.noteId || '') !== String(noteId)) return false;
              const queue = [root];
              while (queue.length) {
                const vm = queue.shift();
                if (String(vm.noteId || '') === String(noteId) && vm.contentLoadingState) {
                  if (vm.contentLoadingState.fail) throw new Error('WPS editor failed to load note body');
                  if (vm.contentLoadingState.idle && vm.$el) {
                    const editor = vm.$el.querySelector('.ql-editor');
                    if (editor) {
                      editor.dataset.wpsExportNoteId = String(noteId);
                      return true;
                    }
                  }
                }
                if (vm.$children) queue.push(...vm.$children);
              }
              return false;
            }
            """,
            arg=current_id,
            timeout=45_000,
        )
    except PlaywrightError as error:
        raise ExportError("正文加载超时或网页编辑器报告失败。") from error
    page.wait_for_timeout(250)
    return page.evaluate(
        """
        (noteId) => {
          const editor = Array.from(document.querySelectorAll('.ql-editor'))
            .find(el => el.dataset.wpsExportNoteId === String(noteId));
          if (!editor) throw new Error('current WPS editor not found');
          return editor.innerHTML;
        }
        """,
        current_id,
    )


def enrich_image_sources(page: Page, html: str, current_id: str) -> str:
    """Turn WPS placeholder/background images into ordinary img tags."""
    try:
        page.wait_for_function(
            """
            (noteId) => {
              const editor = Array.from(document.querySelectorAll('.ql-editor'))
                .find(el => el.dataset.wpsExportNoteId === String(noteId));
              if (!editor) return false;
              const candidates = Array.from(editor.querySelectorAll('[noteimgkey], img'));
              return candidates.every(el => {
                const img = el.tagName === 'IMG' ? el : el.querySelector('img');
                const bg = getComputedStyle(el).backgroundImage || '';
                return Boolean((img && (img.currentSrc || img.src)) || (bg && bg !== 'none'));
              });
            }
            """,
            arg=current_id,
            timeout=5_000,
        )
    except PlaywrightError:
        # A broken remote image is recorded later and must not abort the note.
        pass
    resolved = page.evaluate(
        """
        (noteId) => {
          const editor = Array.from(document.querySelectorAll('.ql-editor'))
            .find(el => el.dataset.wpsExportNoteId === String(noteId));
          if (!editor) return [];
          return Array.from(editor.querySelectorAll('[noteimgkey], img')).map((el, index) => {
          const img = el.tagName === 'IMG' ? el : el.querySelector('img');
          const style = getComputedStyle(el);
          const bg = style.backgroundImage && style.backgroundImage.match(/^url\\(["']?(.*?)["']?\\)$/);
          return {
            index,
            key: el.getAttribute('noteimgkey') || (img && img.getAttribute('noteimgkey')) || '',
            src: (img && (img.currentSrc || img.src)) || (bg && bg[1]) || ''
          };
          });
        }
        """,
        current_id,
    )
    soup = BeautifulSoup(html or "", "html.parser")
    candidates = soup.select("[noteimgkey], img")
    for info, element in zip(resolved, candidates):
        source = info.get("src") or ""
        if not source:
            continue
        if element.name == "img":
            element["src"] = source
        else:
            image = soup.new_tag("img")
            image["src"] = source
            image["alt"] = info.get("key") or "图片"
            element.replace_with(image)
    return str(soup)


def fetch_asset(page: Page, context: BrowserContext, url: str) -> tuple[bytes, str]:
    if url.startswith("data:"):
        return decode_data_url(url)
    if url.startswith("blob:"):
        result = page.evaluate(
            """
            async (url) => {
              const response = await fetch(url);
              const buffer = await response.arrayBuffer();
              const bytes = new Uint8Array(buffer);
              let binary = '';
              const chunk = 0x8000;
              for (let i = 0; i < bytes.length; i += chunk) {
                binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
              }
              return {data: btoa(binary), type: response.headers.get('content-type') || ''};
            }
            """,
            url,
        )
        return base64.b64decode(result["data"]), result.get("type", "")
    response = context.request.get(url, timeout=45_000, fail_on_status_code=False)
    if not response.ok:
        raise ExportError(f"图片下载失败（HTTP {response.status}）。")
    return response.body(), response.headers.get("content-type", "")


def localize_images(
    page: Page,
    context: BrowserContext,
    html: str,
    images_dir: Path,
    markdown_dir: Path,
    current_id: str,
    title: str,
    issues: list[ExportIssue],
) -> tuple[str, int]:
    soup = BeautifulSoup(html or "", "html.parser")
    image_count = 0
    for index, image in enumerate(soup.find_all("img"), start=1):
        source = str(image.get("src", "")).strip()
        if not source:
            issues.append(ExportIssue("image", "图片没有可下载地址", current_id, title))
            continue
        try:
            data, content_type = fetch_asset(page, context, source)
            suffix = extension_for(content_type, source, data)
            destination = images_dir / f"{safe_name(current_id, 'note')}-{index}{suffix}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            relative = destination.relative_to(markdown_dir).as_posix()
            image["src"] = relative
            image_count += 1
        except Exception as error:  # one broken image must not abort the note
            issues.append(ExportIssue("image", str(error), current_id, title))
            image["alt"] = f"{image.get('alt', '图片')}（下载失败）"
    return str(soup), image_count


def build_markdown(
    note: dict[str, Any],
    group_name: str,
    body_markdown: str,
    tz_name: str,
) -> tuple[str, dict[str, Any]]:
    current_id = note_id(note)
    title = normalize_title(note)
    created = epoch_to_iso(pick(note, "createTime", "createdAt", "create_at"), tz_name)
    updated = epoch_to_iso(
        pick(note, "contentUpdateTime", "infoUpdateTime", "updatedAt", "updateTime"),
        tz_name,
    )
    metadata = {
        "wps_id": current_id,
        "group": group_name,
        "created_at": created or None,
        "updated_at": updated or None,
        "pinned": bool(int(pick(note, "star", "pinned", default=0) or 0)),
    }
    content = f"{yaml_front_matter(metadata)}\n# {title}\n"
    if body_markdown:
        content += f"\n{body_markdown}\n"
    return content, metadata


def write_report(
    root: Path,
    exported: list[ExportedNote],
    issues: list[ExportIssue],
    total_found: int,
) -> None:
    failed_note_ids = {issue.note_id for issue in issues if issue.kind == "note"}
    lines = [
        "# WPS 便签导出报告",
        "",
        f"- 发现便签：{total_found}",
        f"- 成功导出：{len(exported)}",
        f"- 失败便签：{len(failed_note_ids)}",
        f"- 图片问题：{sum(issue.kind == 'image' for issue in issues)}",
        f"- 含未完整映射格式的便签：{sum(bool(note.unsupported) for note in exported)}",
        "",
    ]
    formatted = [note for note in exported if note.unsupported]
    if formatted:
        lines.extend(["## 未完整映射的格式", ""])
        for note in formatted:
            lines.append(f"- {note.group}/{note.title}：{', '.join(note.unsupported)}")
        lines.append("")
    if issues:
        lines.extend(["## 错误与警告", ""])
        for issue in issues:
            identity = f"（{issue.title or issue.note_id}）" if issue.note_id else ""
            lines.append(f"- [{issue.kind}] {identity}{issue.message}")
        lines.append("")
    if not issues and not formatted:
        lines.extend(["没有发现导出错误或未映射格式。", ""])
    (root / "export-report.md").write_text("\n".join(lines), encoding="utf-8")


def make_zip(root: Path) -> Path:
    archive = root.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for item in root.rglob("*"):
            if item.is_file():
                handle.write(item, Path(root.name) / item.relative_to(root))
    return archive


def export_notes(
    session: BrowserSession,
    notes: list[dict[str, Any]],
    membership: dict[str, str],
    output_root: Path,
    tz_name: str,
) -> tuple[list[ExportedNote], list[ExportIssue]]:
    exported: list[ExportedNote] = []
    issues: list[ExportIssue] = []
    for index, note in enumerate(notes, start=1):
        current_id = note_id(note)
        title = normalize_title(note)
        group_name = membership.get(current_id) or "未分组"
        log(f"[{index}/{len(notes)}] {group_name} / {title}")
        try:
            html = select_note_and_read_html(
                session.page, current_id, recycle=group_name == "回收站"
            )
            html = enrich_image_sources(session.page, html, current_id)
            group_dir = output_root / safe_name(group_name, "未分组")
            images_dir = group_dir / "images"
            group_dir.mkdir(parents=True, exist_ok=True)
            html, image_count = localize_images(
                session.page,
                session.context,
                html,
                images_dir,
                group_dir,
                current_id,
                title,
                issues,
            )
            body, unsupported = markdown_from_html(html)
            markdown, metadata = build_markdown(note, group_name, body, tz_name)
            destination = unique_path(group_dir, safe_name(title), ".md", current_id)
            destination.write_text(markdown, encoding="utf-8", newline="\n")
            exported.append(
                ExportedNote(
                    wps_id=current_id,
                    title=title,
                    group=group_name,
                    path=destination.relative_to(output_root).as_posix(),
                    created_at=metadata["created_at"] or "",
                    updated_at=metadata["updated_at"] or "",
                    pinned=metadata["pinned"],
                    image_count=image_count,
                    unsupported=unsupported,
                )
            )
        except Exception as error:
            issues.append(ExportIssue("note", str(error), current_id, title))
            log(f"  跳过：{error}")
    return exported, issues


def demo_data() -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    tiny_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nXsAAAAASUVORK5CYII="
    )
    now = int(time.time() * 1000)
    notes = [{
        "noteId": "demo-note-001",
        "title": "导出演示",
        "createTime": now - 3600_000,
        "contentUpdateTime": now,
        "star": 1,
    }]
    membership = {"demo-note-001": "学习"}
    html = {
        "demo-note-001": (
            '<p><strong>加粗文字</strong>与<em>斜体</em></p>'
            '<p><input type="checkbox" checked> 已完成事项</p>'
            '<p><input type="checkbox"> 未完成事项</p>'
            f'<p><img src="{tiny_png}" alt="示例图片"></p>'
        )
    }
    return notes, membership, html


def run_demo(output_root: Path, tz_name: str) -> tuple[list[ExportedNote], list[ExportIssue]]:
    notes, membership, html_by_id = demo_data()
    exported: list[ExportedNote] = []
    issues: list[ExportIssue] = []
    for note in notes:
        current_id = note_id(note)
        title = normalize_title(note)
        group_name = membership[current_id]
        group_dir = output_root / safe_name(group_name)
        images_dir = group_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        soup = BeautifulSoup(html_by_id[current_id], "html.parser")
        count = 0
        for index, image in enumerate(soup.find_all("img"), start=1):
            data, content_type = decode_data_url(image["src"])
            destination = images_dir / f"{current_id}-{index}{extension_for(content_type, '', data)}"
            destination.write_bytes(data)
            image["src"] = destination.relative_to(group_dir).as_posix()
            count += 1
        body, unsupported = markdown_from_html(str(soup))
        markdown, metadata = build_markdown(note, group_name, body, tz_name)
        destination = unique_path(group_dir, safe_name(title), ".md", current_id)
        destination.write_text(markdown, encoding="utf-8", newline="\n")
        exported.append(ExportedNote(
            wps_id=current_id,
            title=title,
            group=group_name,
            path=destination.relative_to(output_root).as_posix(),
            created_at=metadata["created_at"] or "",
            updated_at=metadata["updated_at"] or "",
            pinned=metadata["pinned"],
            image_count=count,
            unsupported=unsupported,
        ))
    return exported, issues


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将旧版 WPS 便签导出为 Markdown + 图片 + 元数据。")
    parser.add_argument("--output", type=Path, default=Path("output"), help="输出目录（默认：output）")
    parser.add_argument("--browser", choices=("auto", "chromium", "edge"), default="auto")
    parser.add_argument("--cdp", help="连接已启用远程调试的 Chrome/Edge，例如 http://127.0.0.1:9222")
    parser.add_argument("--include-recycle", action="store_true", help="同时导出回收站内容")
    parser.add_argument("--zip-only", action="store_true", help="生成 ZIP 后删除展开目录")
    parser.add_argument("--timezone", default=DEFAULT_TZ, help=f"元数据时区（默认：{DEFAULT_TZ}）")
    parser.add_argument("--login-timeout", type=int, default=600, help="等待人工登录秒数（默认：600）")
    parser.add_argument("--max-pages", type=int, default=1000, help="每个列表最大页数（默认：1000）")
    parser.add_argument("--demo", action="store_true", help="不联网，生成一份演示导出包")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        ZoneInfo(args.timezone)
    except Exception:
        log(f"错误：无效时区 {args.timezone!r}")
        return 2
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_parent = args.output.expanduser().resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    output_root = output_parent / f"wps-notes-export-{timestamp}"
    output_root.mkdir(parents=True, exist_ok=False)
    session: BrowserSession | None = None
    exported: list[ExportedNote] = []
    issues: list[ExportIssue] = []
    total_found = 0
    try:
        if args.demo:
            exported, issues = run_demo(output_root, args.timezone)
            total_found = len(exported)
        else:
            with sync_playwright() as playwright:
                session = launch_session(playwright, args)
                if "note.wps.cn" not in session.page.url:
                    session.page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
                user = wait_for_login(session.page, args.login_timeout * 1000)
                log("读取分组和便签列表……")
                groups = load_groups(session.page)
                notes, membership = collect_notes(
                    session.page,
                    user["userid"],
                    groups,
                    args.max_pages,
                    args.include_recycle,
                )
                total_found = len(notes)
                log(f"共发现 {total_found} 条，开始导出正文和图片。")
                exported, issues = export_notes(
                    session, notes, membership, output_root, args.timezone
                )
                if session.browser and not session.attached:
                    session.browser.close()
        manifest = {
            "format_version": 1,
            "source": APP_URL,
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "timezone": args.timezone,
            "counts": {
                "found": total_found,
                "exported": len(exported),
                "failed": sum(issue.kind == "note" for issue in issues),
                "images": sum(note.image_count for note in exported),
            },
            "notes": [asdict(note) for note in exported],
        }
        (output_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        write_report(output_root, exported, issues, total_found)
        archive = make_zip(output_root)
        if args.zip_only:
            shutil.rmtree(output_root)
        log(f"完成：成功 {len(exported)}/{total_found} 条")
        log(f"ZIP：{archive}")
        if not args.zip_only:
            log(f"目录：{output_root}")
        return 0 if len(exported) == total_found else 1
    except KeyboardInterrupt:
        log("\n已取消。临时浏览器上下文将关闭，Cookie 不会保存。")
        return 130
    except Exception as error:
        log(f"错误：{error}")
        if os.environ.get("WPS_EXPORT_DEBUG"):
            traceback.print_exc()
        if output_root.exists() and not any(output_root.iterdir()):
            output_root.rmdir()
        return 2
    finally:
        if session and session.browser and not session.attached:
            try:
                session.browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
