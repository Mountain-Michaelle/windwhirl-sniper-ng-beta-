#!/usr/bin/env python3
"""
flow_analyzer.py - SniperCRM Business Workflow Recorder

Observes the Add Multiple Order workflow and records meaningful business actions only.
Events like pointermove, scroll, and cosmetic DOM changes are filtered out.
"""

import asyncio
import json
import logging
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass, field

import aiofiles
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Request,
    Response,
    Dialog,
    Playwright,
    Frame,
    TimeoutError as PlaywrightTimeoutError,
)

# =============================================================================
# Constants and Configuration
# =============================================================================

TARGET_URL_PATTERN = "https://app.snipercrm.io/add_multi_order"

OUTPUT_DIR = Path("output")
SCREENSHOTS_DIR = OUTPUT_DIR / "screenshots"
HTML_DIR = OUTPUT_DIR / "html"
LOGS_DIR = OUTPUT_DIR / "logs"
NETWORK_DIR = OUTPUT_DIR / "network"
DOM_DIR = OUTPUT_DIR / "dom"
TRACE_DIR = OUTPUT_DIR / "trace"

DB_FILE = OUTPUT_DIR / "flow.db"

LOG_FILE = LOGS_DIR / "flow_analyzer.log"
FLOW_FILE = OUTPUT_DIR / "flow.txt"
SELECTORS_JSON = OUTPUT_DIR / "selectors.json"
NETWORK_JSON = OUTPUT_DIR / "network.json"
MUTATIONS_JSON = OUTPUT_DIR / "mutations.json"
CLICKS_JSON = OUTPUT_DIR / "clicks.json"
KEYBOARD_JSON = OUTPUT_DIR / "keyboard.json"
EVENTS_JSON = OUTPUT_DIR / "events.json"

IMPORTANT_CONTROLS = [
    "customer name", "phone", "whatsapp", "address",
    "state", "product", "price", "custom price",
    "payment method", "staff", "period",
    "save", "add more"
]

# -----------------------------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------------------------

def setup_logging() -> None:
    """Configure logging to console and file."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

logger = logging.getLogger("flow_analyzer")

# -----------------------------------------------------------------------------
# Event Filtering
# -----------------------------------------------------------------------------

BUSINESS_EVENTS: Set[str] = {
    "click", "dblclick", "input", "change", "select",
    "submit", "beforeunload",  # navigation
    "popup", "dialog", "download",
}

IMPORTANT_KEYS = {"Enter", "Tab", "Escape"}

class EventFilter:
    """Decide whether an event is a meaningful business action."""

    @staticmethod
    def is_business_event(payload: dict) -> bool:
        event_type = payload.get("type", "")
        if event_type not in BUSINESS_EVENTS:
            return False
        if event_type == "keydown":
            key = payload.get("key", "")
            if key not in IMPORTANT_KEYS:
                return False
        return True

    @staticmethod
    def describe_action(payload: dict) -> str:
        event_type = payload["type"]
        target = payload.get("target") or {}
        tag = target.get("tag", "")
        text = (target.get("text") or target.get("placeholder") or "").strip()
        if len(text) > 50:
            text = text[:50] + "…"

        if event_type in ("click", "dblclick"):
            desc = "Double-clicked " if event_type == "dblclick" else "Clicked "
            if tag == "button":
                desc += f"button: {text or 'unnamed'}"
            elif tag == "select":
                desc += f"dropdown: {text or 'unnamed'}"
            elif tag == "a":
                desc += f"link: {text or 'unnamed'}"
            else:
                desc += f"element <{tag}>"
                if text:
                    desc += f" ({text})"
            return desc

        if event_type == "input":
            value = target.get("value", "")
            if value:
                return f"Typed '{value}' into {text or f'<{tag}>'}"
            return f"Typed into {text or f'<{tag}>'}"

        if event_type == "change":
            value = target.get("value", "")
            return f"Selected '{value}' in {text or f'<{tag}>'}"

        if event_type == "select":
            return f"Selected text in {text or f'<{tag}>'}"

        if event_type == "submit":
            return "Submitted form"

        if event_type == "keydown":
            key = payload.get("key", "")
            return f"Pressed {key} on {text or f'<{tag}>'}"

        if event_type == "beforeunload":
            return "Navigated away or reloaded"

        if event_type == "dialog":
            return "Dialog appeared"

        if event_type == "popup":
            return "Popup window opened"

        return f"{event_type} on <{tag}>"


# -----------------------------------------------------------------------------
# DOM Mutation Summarizer
# -----------------------------------------------------------------------------

class MutationSummarizer:
    """Analyse raw mutation records and produce a concise summary."""

    @staticmethod
    def summarize(mutations: List[Dict]) -> str:
        if not mutations:
            return "No DOM changes"

        added_counts = {}
        removed_counts = {}
        attr_changes = []

        for m in mutations:
            if m["type"] == "childList":
                for node in m.get("addedNodes") or []:
                    tag = node.get("tag", "unknown")
                    added_counts[tag] = added_counts.get(tag, 0) + 1
                for node in m.get("removedNodes") or []:
                    tag = node.get("tag", "unknown")
                    removed_counts[tag] = removed_counts.get(tag, 0) + 1
            elif m["type"] == "attributes":
                attr_name = m.get("attributeName", "unknown")
                target_info = m.get("target") or {}
                tag = target_info.get("tag", "unknown")
                old = m.get("oldValue")
                new = m.get("newValue")
                attr_changes.append(f"{tag}.{attr_name} changed from {old} to {new}")

        lines = []
        for tag, count in sorted(added_counts.items()):
            if tag == "option":
                lines.append(f"{count} options added to dropdown")
            elif tag == "div":
                lines.append(f"{count} <div> elements added")
            else:
                lines.append(f"{count} <{tag}> added")

        for tag, count in sorted(removed_counts.items()):
            lines.append(f"{count} <{tag}> removed")

        for change in attr_changes[:5]:
            lines.append(change)
        if len(attr_changes) > 5:
            lines.append(f"... and {len(attr_changes)-5} more attribute changes")

        # Heuristic detection
        combined = " ".join(lines).lower()
        if any("toast" in str(m.get("target", {}).get("class_list", [])).lower() for m in mutations if m.get("target")):
            lines.insert(0, "Toast message appeared")
        if any("dialog" in str(m.get("target", {}).get("class_list", [])).lower() for m in mutations if m.get("target")):
            lines.insert(0, "Modal opened")
        if "disabled" in combined and "changed" in combined:
            lines.insert(0, "Control disabled/enabled state changed")

        return "\n".join(lines) if lines else "Minor DOM update"

    @staticmethod
    def should_save_snapshot(mutations: List[Dict]) -> bool:
        if not mutations:
            return False
        summary = MutationSummarizer.summarize(mutations).lower()
        if any(keyword in summary for keyword in ["dropdown", "modal", "toast", "validation", "save"]):
            return True
        if len(mutations) > 10:
            return True
        return False


# -----------------------------------------------------------------------------
# Step Manager (thread‑safe, sequential)
# -----------------------------------------------------------------------------

class StepManager:
    """Guarantees ordered, atomic step recording."""

    def __init__(self, flow_file: Path):
        self.lock = asyncio.Lock()
        self.counter = 0
        self.flow_file = flow_file
        self.steps: List[Dict] = []

    async def record_step(self, step_data: dict) -> int:
        async with self.lock:
            self.counter += 1
            step_id = self.counter
            step_data["step_id"] = step_id
            self.steps.append(step_data)
            await self._write_flow(step_data)
            await self._update_json_files()
            return step_id

    async def _write_flow(self, step: dict):
        lines = []
        lines.append(f"\nSTEP {step['step_id']}")
        lines.append(f"Action: {step.get('action_desc', step['event_type'])}")
        target = step.get("target", {})
        if target:
            tag = target.get("tag", "")
            text = target.get("text", "") or target.get("placeholder", "") or ""
            lines.append(f"Target: <{tag}> {text}")
        lines.append("Selector:")
        for sel in step.get("selectors", []):
            lines.append(f"  {sel['rank']}. {sel['locator']}")
        if step.get("navigation"):
            lines.append("Navigation: yes")
        lines.append(f"Result: {step.get('result', '')}")
        dom_changes = step.get("dom_changes", "No DOM changes")
        lines.append(f"DOM Changes:\n  {dom_changes}")
        network = step.get("network_summary", "")
        if network:
            lines.append(f"Network: {network}")
        duration = step.get("duration")
        if duration:
            lines.append(f"Duration: {duration} sec")
        if step.get("important_control"):
            lines.append("*** IMPORTANT CONTROL ***")
        lines.append("-" * 60)

        async with aiofiles.open(self.flow_file, "a", encoding="utf-8") as f:
            await f.write("\n".join(lines) + "\n")

    async def _update_json_files(self):
        steps = self.steps
        with open(SELECTORS_JSON, "w", encoding="utf-8") as f:
            json.dump([s.get("target", {}) for s in steps], f, indent=2)
        with open(NETWORK_JSON, "w", encoding="utf-8") as f:
            json.dump([req for s in steps for req in s.get("network_requests", [])], f, indent=2)
        with open(MUTATIONS_JSON, "w", encoding="utf-8") as f:
            json.dump([m for s in steps for m in s.get("mutations", [])], f, indent=2)


# =============================================================================
# Database Helper
# =============================================================================

class Database:
    """Simple SQLite wrapper for persistent storage."""
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_id INTEGER UNIQUE,
                    timestamp TEXT,
                    url TEXT,
                    title TEXT,
                    action TEXT,
                    target_tag TEXT,
                    target_text TEXT,
                    target_id TEXT,
                    target_class TEXT,
                    xpath TEXT,
                    css_selector TEXT,
                    playwright_locator TEXT,
                    best_selector TEXT,
                    outer_html TEXT,
                    screenshot_before TEXT,
                    screenshot_after TEXT,
                    navigation BOOLEAN
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS network (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_id INTEGER,
                    request_url TEXT,
                    method TEXT,
                    headers TEXT,
                    payload TEXT,
                    response_status INTEGER,
                    response_headers TEXT,
                    timing REAL,
                    failure BOOLEAN,
                    redirect BOOLEAN
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mutations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_id INTEGER,
                    mutation_type TEXT,
                    target TEXT,
                    added_nodes TEXT,
                    removed_nodes TEXT,
                    attribute_name TEXT,
                    old_value TEXT,
                    new_value TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def executemany(self, sql: str, params: list) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executemany(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ElementInfo:
    tag: str = ""
    text: str = ""
    inner_text: str = ""
    outer_text: str = ""
    value: str = ""
    name: str = ""
    id: str = ""
    class_list: List[str] = field(default_factory=list)
    type: str = ""
    role: str = ""
    placeholder: str = ""
    aria_label: str = ""
    tab_index: int = -1
    disabled: bool = False
    visible: bool = False
    editable: bool = False
    checked: bool = False
    selected: bool = False
    bounding_rect: Dict[str, float] = field(default_factory=dict)
    computed_style: Dict[str, str] = field(default_factory=dict)
    parent_tree: Dict = field(default_factory=dict)
    sibling_tree: List[Dict] = field(default_factory=list)
    child_tree: List[Dict] = field(default_factory=list)
    outer_html: str = ""
    inner_html: str = ""
    xpath: str = ""
    css_selector: str = ""
    playwright_locator: str = ""
    best_selector: str = ""
    all_locators: List[Dict] = field(default_factory=list)


@dataclass
class Step:
    step_id: int
    timestamp: str
    url: str
    title: str
    action: str
    event_type: str
    target: ElementInfo
    coordinates: Optional[Dict[str, int]] = None
    key: Optional[str] = None
    modifiers: List[str] = field(default_factory=list)
    navigation: bool = False
    screenshot_before: Optional[str] = None
    screenshot_after: Optional[str] = None
    html_before: Optional[str] = None
    html_after: Optional[str] = None
    network_requests: List[Dict] = field(default_factory=list)
    mutations: List[Dict] = field(default_factory=list)
    dom_diff: Optional[Dict] = None
    important_control: bool = False


# =============================================================================
# Injected JavaScript
# =============================================================================

INJECTED_JS = """
(function() {
    if (window.__FLOW_ANALYZER_INJECTED) return;
    window.__FLOW_ANALYZER_INJECTED = true;

    function getElementInfo(el) {
        if (!el || el.nodeType !== 1) return null;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);

        const info = {
            tag: el.tagName.toLowerCase(),
            text: (el.textContent || '').trim(),
            inner_text: (el.innerText || '').trim(),
            outer_text: (el.outerText || '').trim(),
            value: el.value || '',
            name: el.name || '',
            id: el.id || '',
            class_list: Array.from(el.classList),
            type: el.type || '',
            role: el.getAttribute('role') || '',
            placeholder: el.placeholder || '',
            aria_label: el.getAttribute('aria-label') || '',
            tab_index: el.tabIndex !== undefined ? el.tabIndex : -1,
            disabled: el.disabled || false,
            visible: rect.width > 0 && rect.height > 0,
            editable: el.isContentEditable || false,
            checked: el.checked || false,
            selected: el.selected || false,
            bounding_rect: {
                x: rect.x, y: rect.y, width: rect.width, height: rect.height,
                top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left
            },
            computed_style: {
                color: style.color,
                backgroundColor: style.backgroundColor,
                fontSize: style.fontSize,
                fontFamily: style.fontFamily,
                fontWeight: style.fontWeight,
                opacity: style.opacity,
                display: style.display,
                visibility: style.visibility,
                position: style.position,
                zIndex: style.zIndex
            },
            outer_html: el.outerHTML,
            inner_html: el.innerHTML,
            xpath: getXPath(el),
            css_selector: getCSSSelector(el),
            all_locators: getAllLocators(el),
            parent_tree: getParentTree(el, 3),
            sibling_tree: getSiblings(el, 3),
            child_tree: getChildren(el, 2)
        };

        const locs = info.all_locators;
        info.best_selector = locs.length ? locs[0].locator : info.xpath;
        return info;
    }

    function getXPath(el) {
        if (!el || el.nodeType !== 1) return '';
        const parts = [];
        while (el && el.nodeType === 1) {
            let idx = 1;
            let sibling = el.previousElementSibling;
            while (sibling) {
                if (sibling.tagName === el.tagName) idx++;
                sibling = sibling.previousElementSibling;
            }
            parts.unshift(el.tagName.toLowerCase() + '[' + idx + ']');
            el = el.parentElement;
        }
        return '/' + parts.join('/');
    }

    function getCSSSelector(el) {
        if (!el || el.nodeType !== 1) return '';
        if (el.id) return '#' + CSS.escape(el.id);
        let selector = el.tagName.toLowerCase();
        if (el.className && typeof el.className === 'string') {
            const cls = el.className.trim().split(/\\s+/).join('.');
            if (cls) selector += '.' + cls;
        }
        const parent = el.parentElement;
        if (parent) {
            const children = parent.children;
            let idx = 1;
            for (let i = 0; i < children.length; i++) {
                if (children[i] === el) break;
                if (children[i].tagName === el.tagName) idx++;
            }
            if (idx > 1) selector += ':nth-of-type(' + idx + ')';
        }
        return selector;
    }

    function getAllLocators(el) {
        const locators = [];
        const push = (rank, type, loc) => {
            if (loc) locators.push({rank, type, locator: loc});
        };
        const role = el.getAttribute('role');
        if (role) {
            const name = el.getAttribute('aria-label') || el.textContent?.trim() || '';
            if (name) {
                push(1, 'role', `page.getByRole('${role}', { name: '${name.replace(/'/g, "\\\\'")}' })`);
            } else {
                push(1, 'role', `page.getByRole('${role}')`);
            }
        }
        const label = el.getAttribute('aria-label') || el.getAttribute('data-label');
        if (label) {
            push(2, 'label', `page.getByLabel('${label.replace(/'/g, "\\\\'")}')`);
        }
        if (el.placeholder) {
            push(3, 'placeholder', `page.getByPlaceholder('${el.placeholder.replace(/'/g, "\\\\'")}')`);
        }
        const text = el.textContent?.trim();
        if (text && text.length < 50 && el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') {
            push(4, 'text', `page.getByText('${text.replace(/'/g, "\\\\'")}')`);
        }
        const css = getCSSSelector(el);
        if (css) {
            push(5, 'css', `page.locator('${css}')`);
        }
        push(6, 'xpath', `page.locator('xpath=${getXPath(el)}')`);
        return locators;
    }

    function getParentTree(el, depth) {
        const tree = [];
        let parent = el.parentElement;
        while (parent && depth > 0) {
            tree.push({
                tag: parent.tagName.toLowerCase(),
                id: parent.id || '',
                class: parent.className || '',
                text: (parent.textContent || '').trim().slice(0, 30)
            });
            parent = parent.parentElement;
            depth--;
        }
        return tree;
    }

    function getSiblings(el, max) {
        const siblings = [];
        const parent = el.parentElement;
        if (parent) {
            let count = 0;
            for (let child of parent.children) {
                if (count >= max) break;
                if (child === el) continue;
                siblings.push({
                    tag: child.tagName.toLowerCase(),
                    id: child.id || '',
                    class: child.className || '',
                    text: (child.textContent || '').trim().slice(0, 30)
                });
                count++;
            }
        }
        return siblings;
    }

    function getChildren(el, depth) {
        const children = [];
        const childNodes = el.children;
        for (let i = 0; i < Math.min(childNodes.length, depth); i++) {
            const child = childNodes[i];
            children.push({
                tag: child.tagName.toLowerCase(),
                id: child.id || '',
                class: child.className || '',
                text: (child.textContent || '').trim().slice(0, 30)
            });
        }
        return children;
    }

    function reportEvent(eventType, event) {
        const target = event.target || event.currentTarget;
        const info = target ? getElementInfo(target) : null;
        const payload = {
            type: eventType,
            timestamp: new Date().toISOString(),
            url: window.location.href,
            title: document.title,
            target: info,
            coordinates: event.clientX !== undefined ? { x: event.clientX, y: event.clientY } : null,
            key: event.key || null,
            modifiers: []
        };
        if (event.ctrlKey) payload.modifiers.push('Ctrl');
        if (event.shiftKey) payload.modifiers.push('Shift');
        if (event.altKey) payload.modifiers.push('Alt');
        if (event.metaKey) payload.modifiers.push('Meta');
        if (window.reportEvent) {
            window.reportEvent(JSON.stringify(payload));
        } else {
            console.log('REPORT_EVENT:', JSON.stringify(payload));
        }
    }

    let mutationBuffer = [];
    let mutationTimeout = null;
    function flushMutations() {
        if (mutationBuffer.length === 0) return;
        const mutations = mutationBuffer.slice();
        mutationBuffer = [];
        if (window.reportMutation) {
            window.reportMutation(JSON.stringify(mutations));
        } else {
            console.log('MUTATIONS:', JSON.stringify(mutations));
        }
    }

    const observer = new MutationObserver((mutations) => {
        for (let m of mutations) {
            const record = {
                type: m.type,
                target: m.target ? getElementInfo(m.target) : null,
                addedNodes: Array.from(m.addedNodes).filter(n => n.nodeType === 1).map(n => getElementInfo(n)),
                removedNodes: Array.from(m.removedNodes).filter(n => n.nodeType === 1).map(n => getElementInfo(n)),
                attributeName: m.attributeName || null,
                oldValue: m.oldValue || null,
                newValue: m.target ? m.target.getAttribute(m.attributeName) : null,
            };
            mutationBuffer.push(record);
        }
        if (mutationTimeout) clearTimeout(mutationTimeout);
        mutationTimeout = setTimeout(flushMutations, 200);
    });

    observer.observe(document, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeOldValue: true,
        characterData: true,
        characterDataOldValue: true,
    });

    const events = [
        'click', 'dblclick', 'contextmenu',
        'keydown', 'focus', 'blur', 'change', 'input', 'submit',
        'scroll', 'drag', 'drop',
        'pointerdown', 'pointerup', 'pointermove',
        'beforeunload'
    ];
    for (let ev of events) {
        document.addEventListener(ev, function(e) {
            reportEvent(ev, e);
        }, true);
    }

    window.addEventListener('beforeunload', function(e) {
        reportEvent('beforeunload', e);
    });

    console.log('Flow Analyzer injected successfully.');
})();
"""


# =============================================================================
# Main FlowAnalyzer Class
# =============================================================================

class FlowAnalyzer:
    """Business Workflow Recorder for Add Multiple Order."""

    def __init__(self, slow_mo: int = 100, headless: bool = False):
        self.slow_mo = slow_mo
        self.headless = headless
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        self.recording_active = False
        self.step_manager = StepManager(FLOW_FILE)

        self.network_entries: List[Dict] = []
        self.mutation_entries: List[Dict] = []

        self.last_meaningful_screenshot: Optional[str] = None
        self.last_meaningful_html: Optional[str] = None

        self.db = Database(DB_FILE)

        self._setup_directories()
        self._init_flow_file()
        self._running = True

        self.important_control_names = [c.lower() for c in IMPORTANT_CONTROLS]
        self.buffer_lock = asyncio.Lock()

    def _setup_directories(self) -> None:
        for d in [OUTPUT_DIR, SCREENSHOTS_DIR, HTML_DIR, LOGS_DIR,
                  NETWORK_DIR, DOM_DIR, TRACE_DIR]:
            d.mkdir(parents=True, exist_ok=True)

    def _init_flow_file(self) -> None:
        with open(FLOW_FILE, 'w', encoding='utf-8') as f:
            f.write("===== SniperCRM Business Workflow Recorder =====\n")
            f.write(f"Started: {datetime.now().isoformat()}\n")
            f.write("Target URL: " + TARGET_URL_PATTERN + "\n")
            f.write("=" * 60 + "\n\n")

    async def _save_screenshot(self, prefix: str) -> Optional[str]:
        if not self.page:
            return None
        try:
            filename = f"{prefix}_{int(time.time()*1000)}.png"
            path = SCREENSHOTS_DIR / filename
            await self.page.screenshot(path=str(path), full_page=True, timeout=10000)
            return str(path)
        except PlaywrightTimeoutError:
            logger.warning(f"Screenshot timeout for {prefix}")
            return None
        except Exception as e:
            logger.error(f"Failed screenshot {prefix}: {e}")
            return None

    async def _save_html(self, prefix: str) -> Optional[str]:
        if not self.page:
            return None
        try:
            filename = f"{prefix}_{int(time.time()*1000)}.html"
            path = HTML_DIR / filename
            content = await self.page.content()
            async with aiofiles.open(path, 'w', encoding='utf-8') as f:
                await f.write(content)
            return str(path)
        except Exception as e:
            logger.error(f"Failed save HTML {prefix}: {e}")
            return None

    def _is_important_control(self, element_info: Optional[Dict]) -> bool:
        if not element_info:
            return False
        text = (element_info.get("text") or "").lower()
        placeholder = (element_info.get("placeholder") or "").lower()
        aria_label = (element_info.get("aria_label") or "").lower()
        name = (element_info.get("name") or "").lower()
        eid = (element_info.get("id") or "").lower()
        class_str = " ".join(element_info.get("class_list", [])).lower()
        combined = f"{text} {placeholder} {aria_label} {name} {eid} {class_str}"
        for control in self.important_control_names:
            if control in combined:
                return True
        return False

    async def _start_recording(self) -> None:
        if self.recording_active:
            return
        self.recording_active = True
        logger.info("[RECORDING STARTED]")
        async with aiofiles.open(FLOW_FILE, 'a', encoding='utf-8') as f:
            await f.write("\n" + "=" * 60 + "\n")
            await f.write("RECORDING STARTED\n")
            await f.write("=" * 60 + "\n\n")

        if not self.last_meaningful_screenshot:
            self.last_meaningful_screenshot = await self._save_screenshot("initial_before")
            self.last_meaningful_html = await self._save_html("initial_before")
            logger.info(f"Initial state captured: {self.last_meaningful_screenshot}")

    async def _stop_recording(self) -> None:
        if not self.recording_active:
            return
        self.recording_active = False
        logger.info("[RECORDING STOPPED]")
        async with aiofiles.open(FLOW_FILE, 'a', encoding='utf-8') as f:
            await f.write("\n" + "=" * 60 + "\n")
            await f.write("RECORDING STOPPED\n")
            await f.write("=" * 60 + "\n")
        await self._cleanup()

    async def _handle_event(self, payload_str: str) -> None:
        if not self.recording_active:
            return

        try:
            payload = json.loads(payload_str)
            event_url = payload.get("url", "")
            if TARGET_URL_PATTERN not in event_url:
                return

            if not EventFilter.is_business_event(payload):
                return

            if payload.get("type") == "keydown" and payload.get("key") == "Escape":
                await self._stop_recording()
                return

            await asyncio.sleep(0.4)

            async with self.buffer_lock:
                mutations_snapshot = list(self.mutation_entries)
                self.mutation_entries.clear()
                network_snapshot = list(self.network_entries)
                self.network_entries.clear()

            action_desc = EventFilter.describe_action(payload)
            target = payload.get("target") or {}
            important = self._is_important_control(target)

            selectors = target.get("all_locators", [])
            if not selectors:
                selectors = [{"rank": 1, "type": "xpath", "locator": target.get("xpath", "")}]

            is_navigation = payload.get("type") in ("beforeunload",)

            dom_summary = MutationSummarizer.summarize(mutations_snapshot)

            save_snapshot = MutationSummarizer.should_save_snapshot(mutations_snapshot)
            screenshot_after = None
            html_after = None
            if save_snapshot:
                screenshot_after = await self._save_screenshot(f"step_{self.step_manager.counter+1}_after")
                html_after = await self._save_html(f"step_{self.step_manager.counter+1}_after")
                self.last_meaningful_screenshot = screenshot_after or self.last_meaningful_screenshot
                self.last_meaningful_html = html_after or self.last_meaningful_html

            network_urls = [req["request_url"] for req in network_snapshot if req.get("request_url")]
            net_summary = "; ".join(network_urls[:3])
            if len(network_urls) > 3:
                net_summary += f" ... ({len(network_urls)} total)"

            duration = None
            if self.step_manager.steps:
                prev_ts = self.step_manager.steps[-1].get("timestamp")
                if prev_ts:
                    try:
                        prev_dt = datetime.fromisoformat(prev_ts)
                        curr_dt = datetime.fromisoformat(payload["timestamp"])
                        duration = round((curr_dt - prev_dt).total_seconds(), 2)
                    except Exception:
                        pass

            step_data = {
                "timestamp": payload["timestamp"],
                "url": event_url,
                "title": payload.get("title", ""),
                "event_type": payload["type"],
                "action_desc": action_desc,
                "target": target,
                "selectors": selectors,
                "coordinates": payload.get("coordinates"),
                "key": payload.get("key"),
                "modifiers": payload.get("modifiers", []),
                "navigation": is_navigation,
                "important_control": important,
                "screenshot_before": self.last_meaningful_screenshot,
                "screenshot_after": screenshot_after,
                "html_before": self.last_meaningful_html,
                "html_after": html_after,
                "network_requests": network_snapshot,
                "mutations": mutations_snapshot,
                "result": "Action performed",
                "dom_changes": dom_summary,
                "network_summary": net_summary if net_summary else "None",
                "duration": duration,
            }

            if "options added" in dom_summary.lower():
                step_data["result"] = "Dropdown opened"
            elif "modal" in dom_summary.lower():
                step_data["result"] = "Modal appeared"
            elif "toast" in dom_summary.lower():
                step_data["result"] = "Toast message displayed"
            elif "disabled" in dom_summary.lower():
                step_data["result"] = "Control state changed"
            else:
                step_data["result"] = f"Action performed: {action_desc}"

            await self.step_manager.record_step(step_data)

            logger.info(f"[STEP {self.step_manager.counter}] {action_desc}")

        except Exception as e:
            logger.error(f"Error handling event: {e}\n{traceback.format_exc()}")

    async def _report_mutation(self, mutations_str: str) -> None:
        if not self.recording_active:
            return
        try:
            mutations = json.loads(mutations_str)
            async with self.buffer_lock:
                self.mutation_entries.extend(mutations)
        except Exception as e:
            logger.error(f"Error handling mutation: {e}")

    async def on_request(self, request: Request) -> None:
        if not self.recording_active:
            return
        try:
            headers = dict(request.headers)
            entry = {
                "timestamp": datetime.now().isoformat(),
                "request_url": request.url,
                "method": request.method,
                "headers": headers,
                "payload": request.post_data,
                "response_status": None,
                "response_headers": None,
            }
            async with self.buffer_lock:
                self.network_entries.append(entry)
        except Exception as e:
            logger.error(f"Error on_request: {e}")

    async def on_response(self, response: Response) -> None:
        if not self.recording_active:
            return
        try:
            url = response.url
            async with self.buffer_lock:
                for entry in reversed(self.network_entries):
                    if entry["request_url"] == url and entry["response_status"] is None:
                        entry["response_status"] = response.status
                        entry["response_headers"] = dict(response.headers)
                        break
        except Exception as e:
            logger.error(f"Error on_response: {e}")

    async def on_dialog(self, dialog: Dialog) -> None:
        logger.info(f"[DIALOG] {dialog.type}: {dialog.message}")
        await dialog.dismiss()

    async def on_popup(self, popup: Page) -> None:
        logger.info(f"[POPUP] New popup: {popup.url}")

    async def on_pageerror(self, error: str) -> None:
        logger.error(f"[PAGE ERROR] {error}")

    async def on_console(self, msg) -> None:
        logger.info(f"[CONSOLE] {msg.type}: {msg.text}")

    async def on_frame_navigated(self, frame: Frame) -> None:
        if frame != self.page.main_frame:
            return
        url = frame.url
        logger.info(f"[NAVIGATION] {url}")
        if TARGET_URL_PATTERN in url and not self.recording_active:
            await self._start_recording()

    async def on_page_close(self, page: Page) -> None:
        logger.info("[PAGE CLOSED]")
        if self.recording_active:
            await self._stop_recording()
        self._running = False

    async def _inject_script(self, page: Page) -> None:
        await page.expose_function("reportEvent", self._handle_event)
        await page.expose_function("reportMutation", self._report_mutation)
        await page.add_init_script(INJECTED_JS)
        logger.info("Injected monitoring script.")

    async def _setup_page_listeners(self, page: Page) -> None:
        page.on("request", self.on_request)
        page.on("response", self.on_response)
        page.on("dialog", self.on_dialog)
        page.on("popup", self.on_popup)
        page.on("pageerror", self.on_pageerror)
        page.on("console", self.on_console)
        page.on("framenavigated", self.on_frame_navigated)
        page.on("close", self.on_page_close)

    async def _cleanup(self) -> None:
        logger.info("Generating final reports...")
        all_steps = self.step_manager.steps
        with open(EVENTS_JSON, "w", encoding="utf-8") as f:
            json.dump(all_steps, f, indent=2)
        with open(SELECTORS_JSON, "w", encoding="utf-8") as f:
            json.dump([s.get("target", {}) for s in all_steps], f, indent=2)
        with open(NETWORK_JSON, "w", encoding="utf-8") as f:
            json.dump([req for s in all_steps for req in s.get("network_requests", [])], f, indent=2)
        with open(MUTATIONS_JSON, "w", encoding="utf-8") as f:
            json.dump([m for s in all_steps for m in s.get("mutations", [])], f, indent=2)

        with open(FLOW_FILE, "a", encoding="utf-8") as f:
            f.write("\n\n===== FINAL SUMMARY =====\n")
            f.write(f"Total business steps recorded: {len(all_steps)}\n")
            f.write(f"End time: {datetime.now().isoformat()}\n")
            f.write("=" * 60 + "\n")

        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Cleanup complete.")

    async def start(self) -> None:
        logger.info("Starting Business Workflow Recorder...")
        logger.info(f"Target URL: {TARGET_URL_PATTERN}")
        logger.info("Please log in and navigate to the Add Multiple Order page.")
        logger.info("Recording starts automatically. Press ESC to stop.")

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=["--start-maximized"]
        )
        self.context = await self.browser.new_context(viewport=None, no_viewport=True)
        self.page = await self.context.new_page()

        await self._setup_page_listeners(self.page)
        await self._inject_script(self.page)

        await self.page.goto("about:blank")
        logger.info("Browser launched. Waiting for user navigation...")

        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("Task cancelled.")
        finally:
            if self.recording_active:
                await self._stop_recording()
            else:
                await self._cleanup()


# =============================================================================
# Entry Point
# =============================================================================

async def main() -> None:
    setup_logging()
    analyzer = FlowAnalyzer(slow_mo=100, headless=False)
    try:
        await analyzer.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Stopping...")
        if analyzer.recording_active:
            await analyzer._stop_recording()
        else:
            await analyzer._cleanup()
    except Exception as e:
        logger.error(f"Unhandled exception: {e}\n{traceback.format_exc()}")
        if analyzer.recording_active:
            await analyzer._stop_recording()
        else:
            await analyzer._cleanup()

if __name__ == "__main__":
    asyncio.run(main())