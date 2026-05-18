#!/usr/bin/env python3
"""
Clipboard dscan helper

Install:
    pip install pyperclip requests beautifulsoup4

Run:
    python clipboard_dscan_helper.py
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

import pyperclip
import requests
import tkinter as tk
from bs4 import BeautifulSoup
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText


APP_TITLE = "Clipboard dscan Helper"
CONFIG_FILE = Path(__file__).with_name("clipboard_dscan_config.json")
DSCAN_HOME = "https://dscan.info/"
DEFAULT_DSCAN_FIELD = "paste"
DEFAULT_LOCAL_FIELD = "paste"

DSCAN_TAB_RE = re.compile(
    r"^\s*(?P<typeid>\d+)\t(?P<name>[^\t]+)\t(?P<group>[^\t]+)\t(?P<distance>.+?)\s*$"
)
DSCAN_AGG_RE = re.compile(
    r"^\s*(?P<name>.+?)\s+(?P<count>\d+)\s*$|^\s*(?P<typeid>\d{4,})\s+(?P<count2>\d+)\s*$"
)
DSCAN_OK_RE = re.compile(r"\bOK;(?P<id>[A-Za-z0-9]+)\b")

CODE_HINTS = [
    r"\bdef\s+\w+\s*\(",
    r"\bclass\s+\w+",
    r"\bimport\s+\w+",
    r"\bfrom\s+\w+\s+import\b",
    r"\bfunction\s+\w+\s*\(",
    r"=>\s*[{(]",
    r"\bconst\s+\w+\s*=",
    r"\blet\s+\w+\s*=",
    r"\bSELECT\b.+\bFROM\b",
    r"<\/?[a-zA-Z][^>]*>",
]


@dataclass
class AppConfig:
    poll_interval_ms: int = 1000
    auto_submit_dscan: bool = False
    auto_submit_local_after_dscan: bool = False
    dscan_url: str = DSCAN_HOME
    dscan_field_name: str = DEFAULT_DSCAN_FIELD
    local_field_name: str = DEFAULT_LOCAL_FIELD
    open_after_submit: bool = False
    copy_result_url_after_local: bool = True


@dataclass
class ClipboardItem:
    content: str
    content_type: str
    confidence: float
    details: str
    created_at: float = field(default_factory=time.time)


class ConfigStore:
    @staticmethod
    def load() -> AppConfig:
        if not CONFIG_FILE.exists():
            return AppConfig()

        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return AppConfig(
                poll_interval_ms=max(250, int(raw.get("poll_interval_ms", 1000))),
                auto_submit_dscan=bool(raw.get("auto_submit_dscan", False)),
                auto_submit_local_after_dscan=bool(raw.get("auto_submit_local_after_dscan", False)),
                dscan_url=str(raw.get("dscan_url", DSCAN_HOME)).strip() or DSCAN_HOME,
                dscan_field_name=str(raw.get("dscan_field_name", raw.get("form_field_name", DEFAULT_DSCAN_FIELD))).strip() or DEFAULT_DSCAN_FIELD,
                local_field_name=str(raw.get("local_field_name", DEFAULT_LOCAL_FIELD)).strip() or DEFAULT_LOCAL_FIELD,
                open_after_submit=bool(raw.get("open_after_submit", False)),
                copy_result_url_after_local=bool(raw.get("copy_result_url_after_local", True)),
            )
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, f"Could not load config. Using defaults.\n\n{exc}")
            return AppConfig()

    @staticmethod
    def save(config: AppConfig) -> None:
        payload = json.dumps(asdict(config), indent=2, ensure_ascii=False)
        CONFIG_FILE.write_text(payload, encoding="utf-8")


class ClipboardClassifier:
    @staticmethod
    def classify(content: Any) -> ClipboardItem:
        if content is None:
            content = ""

        if not isinstance(content, str):
            content = str(content)

        text = content.strip()

        if not text:
            return ClipboardItem(content, "empty", 1.0, "Clipboard is empty or whitespace only.")

        dscan_score, dscan_details = ClipboardClassifier._dscan_score(text)
        if dscan_score >= 0.70:
            return ClipboardItem(content, "eve_dscan", dscan_score, dscan_details)

        local_score, local_details = ClipboardClassifier._local_score(text)
        if local_score >= 0.70:
            return ClipboardItem(content, "eve_local", local_score, local_details)

        if ClipboardClassifier._is_url(text):
            return ClipboardItem(content, "url", 0.98, "Looks like a URL.")

        if ClipboardClassifier._is_email(text):
            return ClipboardItem(content, "email", 0.95, "Looks like an email address.")

        json_details = ClipboardClassifier._json_details(text)
        if json_details:
            return ClipboardItem(content, "json", 0.97, json_details)

        code_score, code_details = ClipboardClassifier._code_score(text)
        if code_score >= 0.65:
            return ClipboardItem(content, "code", code_score, code_details)

        if len(text.split()) <= 6 and not any(char in text for char in ".,;:{}[]()"):
            return ClipboardItem(content, "short_text", 0.75, "Short text or keyword-like content.")

        return ClipboardItem(content, "text", 0.80, "General plain text.")

    @staticmethod
    def _dscan_score(text: str) -> Tuple[float, str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        if len(lines) < 3:
            return 0.0, "Not enough lines for a dscan-like paste."

        tab_matches = sum(1 for line in lines if DSCAN_TAB_RE.match(line))
        agg_matches = sum(1 for line in lines if DSCAN_AGG_RE.match(line))
        matched = max(tab_matches, agg_matches)
        ratio = matched / len(lines)
        has_tabs = tab_matches >= max(2, len(lines) // 3)
        lower_text = text.lower()
        eve_words = [
            "wreck",
            "stargate",
            "keepstar",
            "athanor",
            "raitaru",
            "ishtar",
            "skyhook",
            "guristas",
        ]
        has_eve_words = any(word in lower_text for word in eve_words)
        score = min(0.98, ratio + (0.12 if has_tabs else 0.0) + (0.15 if has_eve_words else 0.0))
        mode = "tab-separated EVE d-scan" if tab_matches >= agg_matches else "aggregated d-scan-like"
        details = f"Looks like {mode}. Matched {matched}/{len(lines)} item line(s)."

        return score, details

    @staticmethod
    def _local_score(text: str) -> Tuple[float, str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        if len(lines) < 3:
            return 0.0, "Not enough lines for a local scan paste."

        if any(DSCAN_TAB_RE.match(line) for line in lines):
            return 0.0, "Looks more like d-scan than local scan."

        known_noise = {
            "pilot",
            "ship",
            "corporation",
            "alliance",
            "name",
            "standing",
            "security status",
        }

        pilot_like = 0

        for line in lines:
            lower = line.lower()

            if lower in known_noise:
                continue

            if len(line) > 64:
                continue

            if "http://" in lower or "https://" in lower or "@" in line:
                continue

            if any(char in line for char in "{}<>;="):
                continue

            words = line.split()

            if 1 <= len(words) <= 4 and any(char.isalpha() for char in line):
                pilot_like += 1

        ratio = pilot_like / len(lines)
        score = min(0.95, ratio + (0.10 if len(lines) >= 5 else 0.0))
        details = f"Looks like a local scan paste. Matched {pilot_like}/{len(lines)} pilot-like line(s)."

        return score, details

    @staticmethod
    def _is_url(text: str) -> bool:
        if "\n" in text or "\r" in text or " " in text:
            return False

        parsed = urlparse(text if "://" in text else f"https://{text}")
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc and "." in parsed.netloc)

    @staticmethod
    def _is_email(text: str) -> bool:
        pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        return bool(re.fullmatch(pattern, text))

    @staticmethod
    def _json_details(text: str) -> Optional[str]:
        is_object = text.startswith("{") and text.endswith("}")
        is_array = text.startswith("[") and text.endswith("]")

        if not (is_object or is_array):
            return None

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None

        if isinstance(parsed, dict):
            return f"Valid JSON object with {len(parsed)} top-level key(s)."

        if isinstance(parsed, list):
            return f"Valid JSON array with {len(parsed)} item(s)."

        return f"Valid JSON value: {type(parsed).__name__}."

    @staticmethod
    def _code_score(text: str) -> Tuple[float, str]:
        matches = 0

        for pattern in CODE_HINTS:
            if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
                matches += 1

        structural = 0
        structural += 1 if "{" in text and "}" in text else 0
        structural += 1 if "(" in text and ")" in text else 0
        structural += 1 if ";" in text else 0
        structural += 1 if len(text.splitlines()) >= 3 else 0
        structural += 1 if re.search(r"^\s{2,}\S", text, re.MULTILINE) else 0

        score = min(0.95, (matches * 0.18) + (structural * 0.08))
        details = f"Likely code snippet. Matched {matches} language hint(s) and {structural} structural hint(s)."

        return score, details


class LocalHandlers:
    @staticmethod
    def process(item: ClipboardItem) -> str:
        if item.content_type == "eve_dscan":
            return LocalHandlers._dscan_overview(item.content)

        if item.content_type == "eve_local":
            return LocalHandlers._local_overview(item.content)

        if item.content_type == "url":
            return LocalHandlers._url_overview(item.content.strip())

        if item.content_type == "json":
            parsed = json.loads(item.content)
            return "Pretty JSON\n" + json.dumps(parsed, indent=2, ensure_ascii=False)

        return LocalHandlers._text_overview(item)

    @staticmethod
    def _dscan_overview(text: str) -> str:
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        by_name: Dict[str, int] = {}
        by_group: Dict[str, int] = {}
        distances: Dict[str, str] = {}
        unmatched = 0

        for line in lines:
            tab_match = DSCAN_TAB_RE.match(line)

            if tab_match:
                name = tab_match.group("name").strip()
                group = tab_match.group("group").strip()
                distance = tab_match.group("distance").strip()
                by_name[name] = by_name.get(name, 0) + 1
                by_group[group] = by_group.get(group, 0) + 1
                distances[name] = distance
                continue

            agg_match = DSCAN_AGG_RE.match(line)

            if agg_match:
                name = (agg_match.group("name") or agg_match.group("typeid") or "Unknown").strip()
                count = int(agg_match.group("count") or agg_match.group("count2") or "0")
                by_name[name] = by_name.get(name, 0) + count
                continue

            unmatched += 1

        top_names = sorted(by_name.items(), key=lambda item: item[1], reverse=True)[:20]
        top_groups = sorted(by_group.items(), key=lambda item: item[1], reverse=True)[:10]

        output = [
            "Local d-scan overview",
            f"Lines: {len(lines):,}",
            f"Parsed unique names: {len(by_name):,}",
            f"Parsed groups: {len(by_group):,}",
            f"Total visible entries: {sum(by_name.values()):,}",
            f"Unmatched lines: {unmatched:,}",
            "",
            "Top groups:",
        ]

        for group, count in top_groups:
            output.append(f"{count:>5}  {group}")

        output.append("")
        output.append("Top names:")

        for name, count in top_names:
            distance = distances.get(name, "")
            output.append(f"{count:>5}  {name}  {distance}")

        return "\n".join(output)

    @staticmethod
    def _local_overview(text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        unique = list(dict.fromkeys(lines))

        output = [
            "Local scan overview",
            f"Pilots: {len(lines):,}",
            f"Unique pilots: {len(unique):,}",
            "",
            "Preview:",
        ]

        output.extend(unique[:20])

        return "\n".join(output)

    @staticmethod
    def _url_overview(url: str) -> str:
        normalized = url if "://" in url else f"https://{url}"
        parsed = urlparse(normalized)

        output = [
            "Local URL overview",
            f"Scheme: {parsed.scheme}",
            f"Host: {parsed.netloc}",
            f"Path: {parsed.path or '/'}",
            f"Query: {parsed.query or '(none)'}",
        ]

        return "\n".join(output)

    @staticmethod
    def _text_overview(item: ClipboardItem) -> str:
        text = item.content.strip()
        preview = text[:500]

        if len(text) > 500:
            preview += "..."

        output = [
            "Local text overview",
            f"Type: {item.content_type}",
            f"Characters: {len(text):,}",
            f"Words: {len(text.split()):,}",
            f"Lines: {len(text.splitlines()):,}",
            f"Preview: {preview}",
        ]

        return "\n".join(output)


class DscanClient:
    def __init__(self, url: str, dscan_field_name: str, local_field_name: str) -> None:
        self.url = url.strip() or DSCAN_HOME
        self.dscan_field_name = dscan_field_name.strip() or DEFAULT_DSCAN_FIELD
        self.local_field_name = local_field_name.strip() or DEFAULT_LOCAL_FIELD

    def submit_dscan(self, text: str) -> Tuple[str, str]:
        with requests.Session() as session:
            home = session.get(self.url, timeout=30)
            home.raise_for_status()
            action_url, fields = self._form_payload(home.text, home.url, self.dscan_field_name)
            fields[self.dscan_field_name] = text
            response = session.post(
                action_url,
                data=fields,
                headers={"Referer": home.url, "User-Agent": "ClipboardDscanHelper/1.0"},
                timeout=30,
                allow_redirects=True,
            )
            response.raise_for_status()
            result_url = self._dscan_result_url(response)
            output = [
                "dscan.info d-scan submission complete",
                f"Result URL: {result_url}",
                "",
                response.text[:5000],
            ]
            return result_url, "\n".join(output)

    def submit_local(self, dscan_result_url: str, text: str) -> Tuple[str, str]:
        with requests.Session() as session:
            page = session.get(dscan_result_url, timeout=30)
            page.raise_for_status()
            action_url, fields = self._form_payload(page.text, page.url, self.local_field_name)
            fields[self.local_field_name] = text
            response = session.post(
                action_url,
                data=fields,
                headers={"Referer": page.url, "User-Agent": "ClipboardDscanHelper/1.0"},
                timeout=30,
                allow_redirects=True,
            )
            response.raise_for_status()
            result_url = self._dscan_result_url(response, fallback=response.url or dscan_result_url)
            output = [
                "dscan.info local scan submission complete",
                f"Result URL: {result_url}",
                "",
                response.text[:5000],
            ]
            return result_url, "\n".join(output)

    def _form_payload(self, html: str, base_url: str, preferred_field: str) -> Tuple[str, Dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        forms = soup.find_all("form")

        if not forms:
            return base_url, {}

        form = self._best_form(forms, preferred_field)
        action = form.get("action") or base_url
        action_url = urljoin(base_url, action)
        fields: Dict[str, str] = {}

        for tag in form.find_all(["input", "textarea", "select"]):
            name = tag.get("name")

            if not name:
                continue

            value = tag.get("value") or tag.text or ""
            fields[str(name)] = str(value)

        textareas = [str(tag.get("name")) for tag in form.find_all("textarea") if tag.get("name")]

        if preferred_field not in fields and textareas:
            fields[textareas[0]] = ""

        return action_url, fields

    def _best_form(self, forms: Any, preferred_field: str) -> Any:
        for form in forms:
            if form.find(attrs={"name": preferred_field}):
                return form

        for form in forms:
            if form.find("textarea"):
                return form

        return forms[0]

    def _dscan_result_url(self, response: requests.Response, fallback: Optional[str] = None) -> str:
        match = DSCAN_OK_RE.search(response.text or "")

        if match:
            return urljoin(self.url, f"/v/{match.group('id')}")

        if response.url and response.url.rstrip("/") != self.url.rstrip("/"):
            return response.url

        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.find_all("a", href=True):
            href = urljoin(response.url, str(link["href"]))

            if "/v/" in href:
                return href

        return fallback or response.url


class ClipboardDscanApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x720")
        self.minsize(820, 560)
        self.config_data = ConfigStore.load()
        self.last_clipboard = ""
        self.last_submitted = ""
        self.latest_dscan_url = ""
        self.awaiting_local_scan = False
        self.current_item: Optional[ClipboardItem] = None
        self.worker_queue: queue.Queue[Tuple[str, str, str]] = queue.Queue()
        self._build_ui()
        self._watch_clipboard()
        self._drain_worker_queue()

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(root)
        top.pack(fill=tk.X)

        self.status_var = tk.StringVar(value="Watching clipboard")
        ttk.Label(top, textvariable=self.status_var, font=("TkDefaultFont", 11, "bold")).pack(side=tk.LEFT)

        self.auto_var = tk.BooleanVar(value=self.config_data.auto_submit_dscan)
        ttk.Checkbutton(
            top,
            text="Auto-submit detected d-scan",
            variable=self.auto_var,
            command=self._save_settings,
        ).pack(side=tk.RIGHT)

        main = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        left = ttk.Frame(main, padding=(0, 0, 8, 0))
        right = ttk.Frame(main, padding=(8, 0, 0, 0))
        main.add(left, weight=1)
        main.add(right, weight=1)

        info = ttk.LabelFrame(left, text="Clipboard overview", padding=8)
        info.pack(fill=tk.X)

        self.type_var = tk.StringVar(value="Type: —")
        self.conf_var = tk.StringVar(value="Confidence: —")
        self.details_var = tk.StringVar(value="Details: —")
        self.latest_url_var = tk.StringVar(value="Latest dscan URL: —")

        ttk.Label(info, textvariable=self.type_var).pack(anchor=tk.W)
        ttk.Label(info, textvariable=self.conf_var).pack(anchor=tk.W)
        ttk.Label(info, textvariable=self.details_var, wraplength=420).pack(anchor=tk.W)
        ttk.Label(info, textvariable=self.latest_url_var, wraplength=420).pack(anchor=tk.W, pady=(6, 0))

        content_frame = ttk.LabelFrame(left, text="Clipboard content", padding=8)
        content_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.content_box = ScrolledText(content_frame, height=14, wrap=tk.WORD)
        self.content_box.pack(fill=tk.BOTH, expand=True)

        actions = ttk.Frame(left)
        actions.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(actions, text="Refresh now", command=self._manual_refresh).pack(side=tk.LEFT)
        ttk.Button(actions, text="Local overview", command=self._local_process).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Submit d-scan", command=self._submit_dscan).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Submit local scan", command=self._submit_local).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Copy result", command=self._copy_result).pack(side=tk.RIGHT)

        config = ttk.LabelFrame(right, text="dscan.info settings", padding=8)
        config.pack(fill=tk.X)

        self.dscan_url_var = tk.StringVar(value=self.config_data.dscan_url)
        self.dscan_field_var = tk.StringVar(value=self.config_data.dscan_field_name)
        self.local_field_var = tk.StringVar(value=self.config_data.local_field_name)
        self.open_after_submit_var = tk.BooleanVar(value=self.config_data.open_after_submit)
        self.auto_local_var = tk.BooleanVar(value=self.config_data.auto_submit_local_after_dscan)
        self.copy_result_url_var = tk.BooleanVar(value=self.config_data.copy_result_url_after_local)

        self._field(config, "dscan URL", self.dscan_url_var)
        self._field(config, "D-scan paste field name", self.dscan_field_var)
        self._field(config, "Local paste field name", self.local_field_var)

        ttk.Checkbutton(
            config,
            text="Auto-submit next local scan after d-scan",
            variable=self.auto_local_var,
            command=self._save_settings,
        ).pack(anchor=tk.W, pady=(8, 0))

        ttk.Checkbutton(
            config,
            text="Open result in browser after submit",
            variable=self.open_after_submit_var,
            command=self._save_settings,
        ).pack(anchor=tk.W, pady=(4, 0))

        ttk.Checkbutton(
            config,
            text="Copy result URL after local submit",
            variable=self.copy_result_url_var,
            command=self._save_settings,
        ).pack(anchor=tk.W, pady=(4, 0))

        ttk.Button(config, text="Save settings", command=self._save_settings).pack(anchor=tk.W, pady=(8, 0))

        result_frame = ttk.LabelFrame(right, text="Results", padding=8)
        result_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.result_box = ScrolledText(result_frame, height=22, wrap=tk.WORD)
        self.result_box.pack(fill=tk.BOTH, expand=True)

    def _field(self, parent: ttk.Frame, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).pack(anchor=tk.W, pady=(4, 0))
        ttk.Entry(parent, textvariable=variable).pack(fill=tk.X)

    def _watch_clipboard(self) -> None:
        try:
            content = pyperclip.paste()

            if not isinstance(content, str):
                content = "" if content is None else str(content)

            if content != self.last_clipboard:
                self.last_clipboard = content
                self._set_current_content(content)

                if self.current_item:
                    should_submit_dscan = (
                        self.auto_var.get()
                        and self.current_item.content_type == "eve_dscan"
                        and content != self.last_submitted
                    )
                    should_submit_local = (
                        self.auto_local_var.get()
                        and self.awaiting_local_scan
                        and bool(self.latest_dscan_url)
                        and self.current_item.content_type == "eve_local"
                        and content != self.last_submitted
                    )

                    if should_submit_dscan:
                        self._submit_dscan()
                    elif should_submit_local:
                        self._submit_local()
        except Exception as exc:
            self.status_var.set(f"Clipboard read failed: {exc}")

        self.after(max(250, self.config_data.poll_interval_ms), self._watch_clipboard)

    def _manual_refresh(self) -> None:
        try:
            self._set_current_content(pyperclip.paste())
        except Exception as exc:
            self.status_var.set(f"Clipboard read failed: {exc}")

    def _set_current_content(self, content: Any) -> None:
        item = ClipboardClassifier.classify(content)
        self.current_item = item
        self.type_var.set(f"Type: {item.content_type}")
        self.conf_var.set(f"Confidence: {item.confidence:.0%}")
        self.details_var.set(f"Details: {item.details}")
        self.content_box.delete("1.0", tk.END)
        self.content_box.insert("1.0", item.content)
        self.status_var.set(f"Detected {item.content_type}")

    def _local_process(self) -> None:
        if not self.current_item:
            return

        try:
            result = LocalHandlers.process(self.current_item)
        except Exception as exc:
            result = f"Local handler failed: {type(exc).__name__}: {exc}"

        self._append_result("Local overview", result)

    def _client(self) -> DscanClient:
        return DscanClient(self.dscan_url_var.get(), self.dscan_field_var.get(), self.local_field_var.get())

    def _submit_dscan(self) -> None:
        if not self.current_item:
            return

        if self.current_item.content_type != "eve_dscan":
            message = f"Clipboard is classified as {self.current_item.content_type}, not eve_dscan."
            self._append_result("dscan.info", message)
            return

        self.last_submitted = self.current_item.content
        self.status_var.set("Submitting d-scan to dscan.info...")
        thread = threading.Thread(target=self._submit_dscan_worker, args=(self._client(), self.current_item.content), daemon=True)
        thread.start()

    def _submit_dscan_worker(self, client: DscanClient, content: str) -> None:
        try:
            result_url, result = client.submit_dscan(content)
        except Exception as exc:
            result_url = ""
            result = f"dscan.info d-scan submit failed\n{type(exc).__name__}: {exc}"

        self.worker_queue.put(("dscan.info d-scan", result_url, result))

    def _submit_local(self) -> None:
        if not self.current_item:
            return

        if not self.latest_dscan_url:
            self._append_result("dscan.info local", "No d-scan result URL is available yet.")
            return

        if self.current_item.content_type != "eve_local":
            message = f"Clipboard is classified as {self.current_item.content_type}, not eve_local."
            self._append_result("dscan.info local", message)
            return

        self.last_submitted = self.current_item.content
        self.status_var.set("Submitting local scan to latest dscan page...")
        thread = threading.Thread(
            target=self._submit_local_worker,
            args=(self._client(), self.latest_dscan_url, self.current_item.content),
            daemon=True,
        )
        thread.start()

    def _submit_local_worker(self, client: DscanClient, dscan_url: str, content: str) -> None:
        try:
            result_url, result = client.submit_local(dscan_url, content)
        except Exception as exc:
            result_url = ""
            result = f"dscan.info local submit failed\n{type(exc).__name__}: {exc}"

        self.worker_queue.put(("dscan.info local", result_url, result))

    def _drain_worker_queue(self) -> None:
        try:
            while True:
                title, result_url, result = self.worker_queue.get_nowait()

                if result_url:
                    self.latest_dscan_url = result_url
                    self.latest_url_var.set(f"Latest dscan URL: {result_url}")

                    if title == "dscan.info d-scan":
                        self.awaiting_local_scan = True
                        self.status_var.set("D-scan submitted. Copy local scan next.")

                    if title == "dscan.info local":
                        self.awaiting_local_scan = False
                        if self.copy_result_url_var.get():
                            pyperclip.copy(result_url)
                            self.last_clipboard = result_url
                            self.status_var.set("Final result URL copied to clipboard")

                self._append_result(title, result)
                self._maybe_open_result(result_url or result)

                if not (title == "dscan.info d-scan" and result_url) and not (title == "dscan.info local" and result_url and self.copy_result_url_var.get()):
                    self.status_var.set("Done")
        except queue.Empty:
            pass

        self.after(200, self._drain_worker_queue)

    def _maybe_open_result(self, result: str) -> None:
        if not self.open_after_submit_var.get():
            return

        if result.startswith("http://") or result.startswith("https://"):
            webbrowser.open(result)
            return

        match = re.search(r"Result URL:\s*(\S+)", result)

        if match:
            webbrowser.open(match.group(1))

    def _append_result(self, title: str, body: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.result_box.insert(tk.END, f"\n--- {title} @ {timestamp} ---\n{body}\n")
        self.result_box.see(tk.END)

    def _copy_result(self) -> None:
        text = self.result_box.get("1.0", tk.END).strip()

        if text:
            pyperclip.copy(text)
            self.status_var.set("Result copied")

    def _save_settings(self) -> None:
        self.config_data.auto_submit_dscan = self.auto_var.get()
        self.config_data.auto_submit_local_after_dscan = self.auto_local_var.get()
        self.config_data.dscan_url = self.dscan_url_var.get().strip() or DSCAN_HOME
        self.config_data.dscan_field_name = self.dscan_field_var.get().strip() or DEFAULT_DSCAN_FIELD
        self.config_data.local_field_name = self.local_field_var.get().strip() or DEFAULT_LOCAL_FIELD
        self.config_data.open_after_submit = self.open_after_submit_var.get()
        self.config_data.copy_result_url_after_local = self.copy_result_url_var.get()

        try:
            ConfigStore.save(self.config_data)
            self.status_var.set("Settings saved")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not save settings.\n\n{exc}")


def main() -> None:
    app = ClipboardDscanApp()
    app.mainloop()


if __name__ == "__main__":
    main()
