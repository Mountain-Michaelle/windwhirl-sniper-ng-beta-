# workflow_recorder.py
"""
BusinessWorkflowRecorder attaches to an already-open Playwright `page`
and produces a concise, human-readable log of user actions, plus
structured JSON, plus periodic full-page "structure" snapshots.

Capture is gated entirely by interactivity (recorder.js), not by
vocabulary or DOM position -- this file doesn't know or care whether
an element is "business-relevant", only whether the browser reported
it as a captured event. Any cosmetic relabeling for readability comes
from important_fields.FRIENDLY_LABEL_OVERRIDES, which is opt-in and
never affects whether something gets recorded.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

from important_fields import (
    NETWORK_METHODS,
    NETWORK_EXCLUDE_EXTENSIONS,
    NETWORK_EXCLUDE_DOMAINS,
    NETWORK_EXCLUDE_PATH_PATTERNS,
    NETWORK_CAPTURE_MODE,
    FRIENDLY_LABEL_OVERRIDES,
)

_VALID_NETWORK_MODES = ("separate", "inline", "action", "off")
ACTION_VERIFICATION_WINDOW_SECONDS = 5.0

_RECORDER_JS_PATH = Path(__file__).with_name("recorder.js")

DEDUPE_WINDOW_SECONDS = 3.0


@dataclass
class Step:
    step_number: int
    kind: str                 # "field" | "action" | "network"
    action_type: str
    description: str
    value: Optional[str]
    best_selector: Optional[str]
    fallback_selector: Optional[str]
    wait_condition: Optional[str]
    success_condition: Optional[str] = None
    field_tag: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    raw_url: Optional[str] = None  # full URL for "Navigation" steps, used by generate_playwright_script()
    backend_verification: Optional[str] = None

    def to_human_block(self) -> str:
        lines = [f"STEP {self.step_number}", "", self.action_type]
        if self.value:
            lines += ["", "Value:", str(self.value)]
        if self.best_selector:
            lines += ["", "Selector:", self.best_selector]
        if self.fallback_selector and self.fallback_selector != self.best_selector:
            lines += ["Fallback:", self.fallback_selector]
        if self.wait_condition:
            lines += ["", "Wait:", self.wait_condition]
        if self.success_condition:
            lines += ["", "Result:", self.success_condition]
        if self.backend_verification:
            lines += ["", "Backend verification:", self.backend_verification]
        return "\n".join(lines)


def _friendly_label(kind: str, action_type: str, description: str) -> str:
    desc_lower = (description or "").lower()
    for (k, a, keyword), label in FRIENDLY_LABEL_OVERRIDES.items():
        if k == kind and a == action_type and keyword in desc_lower:
            return label
    if kind == "field":
        return f"Fill {description.strip().title()}" if description else "Fill Field"
    return description.strip().title() if description else action_type.title()


class BusinessWorkflowRecorder:
    def __init__(self, page, output_dir: str = "recordings", session_name: str = "session",
                 debug: bool = False, exclude_url_patterns: Optional[List[str]] = None,
                 structure_threshold: int = 3):
        self.page = page
        self.output_dir = output_dir
        self.session_name = session_name
        self.debug = debug
        self.exclude_url_patterns = exclude_url_patterns or []
        self.structure_threshold = structure_threshold
        self._exclude_res = [re.compile(p, re.IGNORECASE) for p in self.exclude_url_patterns]

        self.network_capture_mode = NETWORK_CAPTURE_MODE if NETWORK_CAPTURE_MODE in _VALID_NETWORK_MODES else "separate"
        self._network_exclude_res = [re.compile(p, re.IGNORECASE) for p in NETWORK_EXCLUDE_PATH_PATTERNS]

        self.steps: List[Step] = []
        self.network_steps: List[Step] = []
        self.structure_snapshots: List[dict] = []
        self._last_signature: Optional[tuple] = None
        self._last_signature_time: float = 0.0
        self._last_navigation_url: Optional[str] = None
        self._pending_success_target: Optional[int] = None
        self._last_outcome: Optional[str] = None
        self._last_ui_step: Optional[Step] = None
        self._last_ui_step_time: float = 0.0

        os.makedirs(self.output_dir, exist_ok=True)

    def _in_scope(self) -> bool:
        if not self._exclude_res:
            return True
        try:
            url = self.page.url
        except Exception:
            return True
        return not any(r.search(url) for r in self._exclude_res)

    # ---------- setup ----------

    def start(self) -> None:
        if not _RECORDER_JS_PATH.exists():
            raise FileNotFoundError(
                f"recorder.js not found at {_RECORDER_JS_PATH}. It must sit "
                "alongside workflow_recorder.py. Copy it there and try again."
            )

        # json.dumps handles quoting/escaping safely -- the old version
        # hand-escaped a single string and would've broken silently on
        # a URL containing a quote or backslash.
        scope_script = (
            f"window.__recorderExcludePatterns = {json.dumps(self.exclude_url_patterns)};"
            f"window.__recorderStructureThreshold = {json.dumps(self.structure_threshold)};"
            f"window.__recorderDebug = {json.dumps(self.debug)};"
        )
        self.page.add_init_script(script=scope_script)
        self.page.expose_function("__recordStep", self._on_raw_step)
        self.page.add_init_script(path=str(_RECORDER_JS_PATH))

        # Re-inject into the *current* document too -- add_init_script
        # only affects future navigations, not the already-loaded page.
        self.page.evaluate(scope_script)
        self.page.evaluate(_RECORDER_JS_PATH.read_text())

        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.page.on("framenavigated", self._on_navigated)

        if self.debug:
            print("  [debug mode ON] every click/input/change will be reported.\n", flush=True)
        if self.exclude_url_patterns:
            print(f"  Excluding capture on URLs matching: {self.exclude_url_patterns}\n", flush=True)
        else:
            print("  No URL exclusions configured -- capturing everywhere.\n", flush=True)
        if self.network_capture_mode == "separate":
            print("  Network calls: captured separately -- see the _network.json file, "
                  "kept out of the main automation log.\n", flush=True)
        elif self.network_capture_mode == "off":
            print("  Network calls: not captured (NETWORK_CAPTURE_MODE = \"off\").\n", flush=True)
        elif self.network_capture_mode == "inline":
            print("  Network calls: inline (interleaved into the main step list).\n", flush=True)
        else:
            print("  Backend verification: attach minimal non-GET response status to "
                  "the triggering UI step only.\n", flush=True)

    # ---------- JS bridge ----------

    def _on_raw_step(self, raw_json: str) -> None:
        try:
            evt = json.loads(raw_json)
        except json.JSONDecodeError:
            return

        kind = evt.get("kind")

        if kind == "debug":
            print(f"  [debug] {evt.get('action_type')}: {evt.get('description')}"
                  + (f" = {evt.get('value')!r}" if evt.get("value") else ""), flush=True)
            return

        if not self._in_scope():
            return

        if kind == "outcome":
            self._attach_outcome(evt)
            return

        if kind == "structure":
            self._on_structure_snapshot(evt)
            return

        action_type = evt.get("action_type", "")
        description = evt.get("description", "") or ""

        if kind == "action" and action_type == "navigate":
            self._record_navigation(evt.get("url", ""))
            return

        label = _friendly_label(kind, action_type, description)

        # Dedupe the same event on the same element/value only.  Several
        # live forms reuse labels such as "All" or "Select", so a
        # description-only signature can discard real actions.
        signature = (
            kind,
            action_type,
            description.strip().lower(),
            evt.get("best_selector"),
            evt.get("value"),
        )
        now = evt.get("timestamp", time.time() * 1000) / 1000.0

        if signature == self._last_signature and (now - self._last_signature_time) < DEDUPE_WINDOW_SECONDS:
            self._last_signature_time = now
            return
        self._last_signature = signature
        self._last_signature_time = now

        step = Step(
            step_number=len(self.steps) + 1,
            kind=kind,
            action_type=label,
            description=description,
            value=evt.get("value"),
            best_selector=evt.get("best_selector"),
            fallback_selector=evt.get("fallback_selector"),
            wait_condition=evt.get("wait_condition"),
            field_tag=evt.get("tag"),
        )
        self.steps.append(step)
        self._last_ui_step = step
        self._last_ui_step_time = now
        self._print_live(step)
        self._autosave()

        if action_type == "submit" or "save" in description.lower():
            self._pending_success_target = step.step_number

    def _print_live(self, step: "Step") -> None:
        if step.value:
            print(f"  [{step.step_number}] {step.action_type}: {step.value}", flush=True)
        else:
            print(f"  [{step.step_number}] {step.action_type}", flush=True)

    def _record_navigation(self, url: str) -> None:
        if not url or url == self._last_navigation_url:
            return
        self._last_navigation_url = url
        step = Step(
            step_number=len(self.steps) + 1,
            kind="action",
            action_type="Navigation",
            description=f"Navigated to {self._short_url(url)}",
            value=None, best_selector=None, fallback_selector=None,
            wait_condition="domcontentloaded",
            raw_url=url,
        )
        self.steps.append(step)
        self._print_live(step)
        self._autosave()

    def _on_structure_snapshot(self, evt: dict) -> None:
        snapshot = {
            "trigger": evt.get("action_type"),
            "url": evt.get("url"),
            "description": evt.get("description"),
            "elements": evt.get("elements", []),
            "timestamp": evt.get("timestamp", time.time() * 1000) / 1000.0,
        }
        self.structure_snapshots.append(snapshot)
        if self.debug:
            print(f"  [structure] {snapshot['description']} (trigger: {snapshot['trigger']})", flush=True)
        self._autosave()

    def _attach_outcome(self, evt: dict) -> None:
        text = evt.get("description", "")
        outcome = f"{'Error' if evt.get('action_type') == 'error' else 'Success'}: {text}"
        if outcome == self._last_outcome:
            return
        self._last_outcome = outcome
        print(f"      -> {outcome}", flush=True)
        if self._pending_success_target is not None:
            for s in self.steps:
                if s.step_number == self._pending_success_target:
                    s.success_condition = outcome
                    break
            self._pending_success_target = None
        elif self.steps:
            self.steps[-1].success_condition = outcome
        self._autosave()

    # ---------- network filtering ----------

    def _on_request(self, request) -> None:
        if not self._in_scope():
            return
        if self.network_capture_mode in ("off", "action"):
            return
        url = request.url
        if any(domain in url for domain in NETWORK_EXCLUDE_DOMAINS):
            return
        if url.lower().split("?")[0].endswith(NETWORK_EXCLUDE_EXTENSIONS):
            return
        if request.method not in NETWORK_METHODS:
            return
        if any(r.search(url) for r in self._network_exclude_res):
            return

        if self.network_capture_mode == "inline":
            # Old behavior: interleaved into the same numbered list that
            # drives the automation-facing .txt/.json/generated script.
            step = Step(
                step_number=len(self.steps) + 1,
                kind="network",
                action_type="Network Call",
                description=f"{request.method} {self._short_url(url)}",
                value=None, best_selector=None, fallback_selector=None,
                wait_condition="response received",
            )
            self.steps.append(step)
            if self.debug:
                self._print_live(step)
            self._pending_success_target = step.step_number
        else:
            # "separate" (default): captured, but kept out of the list
            # that .txt/.json/generate_playwright_script() are built
            # from, so a chatty backend's housekeeping calls can't bury
            # the field/action steps you're trying to automate.
            step = Step(
                step_number=len(self.network_steps) + 1,
                kind="network",
                action_type="Network Call",
                description=f"{request.method} {self._short_url(url)}",
                value=None, best_selector=None, fallback_selector=None,
                wait_condition="response received",
            )
            self.network_steps.append(step)
            if self.debug:
                self._print_live(step)
        self._autosave()

    def _on_response(self, response) -> None:
        if self.network_capture_mode != "action" or not self._last_ui_step:
            return
        if not self._in_scope():
            return
        if response.request.method == "GET":
            return
        if any(domain in response.url for domain in NETWORK_EXCLUDE_DOMAINS):
            return
        if any(r.search(response.url) for r in self._network_exclude_res):
            return
        if time.time() - self._last_ui_step_time > ACTION_VERIFICATION_WINDOW_SECONDS:
            return

        path = response.url.split("://", 1)[-1].split("?", 1)[0]
        path = "/" + path.split("/", 1)[1] if "/" in path else "/"
        verification = (
            f"{response.request.method} {path} -> HTTP {response.status}"
        )
        if self._last_ui_step.backend_verification:
            existing = self._last_ui_step.backend_verification.split("; ")
            if verification in existing:
                return
            verification = "; ".join(existing + [verification])
        self._last_ui_step.backend_verification = verification
        self._autosave()

    @staticmethod
    def _short_url(url: str) -> str:
        return url.split("://", 1)[-1].split("?", 1)[0]

    def _on_navigated(self, frame) -> None:
        if frame != self.page.main_frame:
            return
        if self._exclude_res and any(r.search(frame.url) for r in self._exclude_res):
            return
        self._record_navigation(frame.url)

    # ---------- output ----------

    def _atomic_write(self, path: str, content: str) -> None:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    def _autosave(self) -> None:
        try:
            self._write_files(list(self.steps))
            self._write_structure_file(list(self.structure_snapshots))
            self._write_network_file(list(self.network_steps))
        except Exception as e:
            # Visible now, instead of silently swallowed -- a bad value
            # should never kill real-time saving for the rest of the
            # session without you knowing about it.
            print(f"  [warn] autosave failed: {e!r}", flush=True)

    def _write_files(self, steps: List["Step"]) -> dict:
        numbered = list(steps)
        for i, step in enumerate(numbered, start=1):
            step.step_number = i

        base = os.path.join(self.output_dir, self.session_name)
        text_path = f"{base}.txt"
        json_path = f"{base}.json"

        self._atomic_write(text_path, self._render_human_log(numbered))
        self._atomic_write(json_path, json.dumps([asdict(s) for s in numbered], indent=2))

        return {"text_path": text_path, "json_path": json_path}

    def _write_structure_file(self, snapshots: List[dict]) -> str:
        path = os.path.join(self.output_dir, f"{self.session_name}_structure.json")
        self._atomic_write(path, json.dumps(snapshots, indent=2))
        return path

    def _write_network_file(self, steps: List["Step"]) -> Optional[str]:
        # Only a separate file exists in "separate" mode. In "inline"
        # mode the calls already live in the main .txt/.json (via
        # self.steps); in "off" mode self.network_steps stays empty.
        if self.network_capture_mode != "separate":
            return None
        numbered = list(steps)
        for i, step in enumerate(numbered, start=1):
            step.step_number = i
        path = os.path.join(self.output_dir, f"{self.session_name}_network.json")
        self._atomic_write(path, json.dumps([asdict(s) for s in numbered], indent=2))
        return path

    def stop_and_save(self) -> dict:
        paths = self._write_files(self.steps)
        paths["structure_path"] = self._write_structure_file(self.structure_snapshots)
        network_path = self._write_network_file(self.network_steps)
        if network_path:
            paths["network_path"] = network_path
        script_path = os.path.join(self.output_dir, f"{self.session_name}_script.py")
        self._atomic_write(script_path, self.generate_playwright_script())
        paths["script_path"] = script_path
        return paths

    def _render_human_log(self, steps: Optional[List["Step"]] = None) -> str:
        blocks = [step.to_human_block() for step in (steps if steps is not None else self.steps)]
        separator = "\n\n" + "-" * 32 + "\n\n"
        return separator.join(blocks) + "\n"

    def generate_playwright_script(self) -> str:
        lines = [
            "from playwright.sync_api import sync_playwright",
            "",
            "with sync_playwright() as p:",
            "    browser = p.chromium.launch(headless=False)",
            "    page = browser.new_page()",
            "",
        ]
        for step in self.steps:
            if step.kind == "network":
                continue  # informational only -- never replayed as a UI action

            if step.action_type == "Navigation":
                # BUG FIXED: previously silently dropped -- best_selector
                # is always None for a navigation step, so it fell through
                # the "if not step.best_selector: continue" guard below
                # and the generated script never navigated anywhere.
                if step.raw_url:
                    lines.append(f'    page.goto("{step.raw_url}")  # {step.action_type}')
                continue

            if not step.best_selector:
                continue

            if step.value in ("checked", "unchecked"):
                # BUG FIXED: previously fell into the generic .fill()
                # branch below, which raises at runtime against a
                # checkbox/radio locator.
                method = "check" if step.value == "checked" else "uncheck"
                lines.append(f"    {step.best_selector}.{method}()  # {step.action_type}")
            elif step.kind == "action":
                lines.append(f"    {step.best_selector}.click()  # {step.action_type}")
            elif step.value is not None:
                safe_value = str(step.value).replace('"', '\\"')
                if step.field_tag == "select":
                    lines.append(f'    {step.best_selector}.select_option(label="{safe_value}")  # {step.action_type}')
                else:
                    lines.append(f'    {step.best_selector}.fill("{safe_value}")  # {step.action_type}')
        lines.append("")
        lines.append("    browser.close()")
        return "\n".join(lines)