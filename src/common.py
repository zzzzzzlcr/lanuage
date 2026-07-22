#!/usr/bin/env python3
"""Common utilities for form fill subprocess scripts."""

import json
import logging
import os
import subprocess
import base64
import time
from typing import Dict, Optional
import threading

# CDP binary path
CDP_PATH = os.environ.get("CDP_PATH", "/company/cdpcli/cdp")

# API endpoints
SCREENSHOT_API_URL = os.environ.get("SCREENSHOT_API_URL", "https://fmr.3tkj.cn/api/quest/screenshot")
FORM_API_URL = os.environ.get("FORM_API_URL", "https://fmr.3tkj.cn/api/quest/formMessage")

# Timeouts (in seconds)
CDP_TIMEOUT = 30
API_TIMEOUT = 15

# Logger setup lock for thread safety
_logger_setup_lock = threading.Lock()

# Configure logging at module level
logging.basicConfig(level=logging.DEBUG)


class CDPHelper:
    """Helper class for Chrome DevTools Protocol operations."""

    def __init__(self, ws_url: str):
        """
        Initialize CDP helper.

        Args:
            ws_url: WebSocket URL from bit.sh (e.g., "ws://192.168.1.222:9222/...")
        """
        self.host, self.port = self._parse_ws_url(ws_url)

    def _parse_ws_url(self, ws_url: str) -> tuple:
        """Parse WebSocket URL to extract host and port."""
        # Extract host and port from ws://host:port/path
        if not ws_url:
            return "127.0.0.1", "9222"

        # Remove ws:// prefix
        url = ws_url.replace("ws://", "").replace("wss://", "")

        # Split by / to get host:port part
        host_port = url.split("/")[0]

        if ":" in host_port:
            host, port = host_port.split(":")
            return host, port
        else:
            return host_port, "9222"

    def snapshot(self) -> Dict:
        """
        Get page accessibility snapshot.

        Returns:
            Dict with snapshot data including frames, trees, etc.
        """
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [CDP_PATH, "snapshot",
                     "--host", self.host,
                     "--port", self.port],
                    capture_output=True,
                    text=True,
                    timeout=CDP_TIMEOUT
                )

                if result.returncode == 0:
                    return json.loads(result.stdout)
                else:
                    error_msg = result.stderr or result.stdout
                    if attempt < 2:
                        time.sleep(2)
                        continue
                    raise Exception(f"CDP snapshot failed: {error_msg}")

            except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise Exception(f"CDP snapshot failed after 3 attempts: {e}")

    def click(self, selector: str, frame_id: str = "") -> str:
        """
        Click element on page.

        Args:
            selector: CSS selector for element
            frame_id: Optional frame ID for iframe elements

        Returns:
            Command output
        """
        cmd = [CDP_PATH, "click", "--selector", selector]
        if frame_id:
            cmd.extend(["--frame-id", frame_id])
        cmd.extend(["--host", self.host, "--port", self.port])

        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=CDP_TIMEOUT
        )
        return result.stdout + result.stderr

    def eval(self, script: str, frame_id: str = "") -> str:
        """
        Execute JavaScript in page.

        Args:
            script: JavaScript code to execute
            frame_id: Optional frame ID for iframe execution

        Returns:
            Execution result
        """
        cmd = [CDP_PATH, "eval", script]
        if frame_id:
            cmd.extend(["--frame-id", frame_id])
        cmd.extend(["--host", self.host, "--port", self.port])

        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=CDP_TIMEOUT
        )
        output = (result.stdout + result.stderr).strip()

        # Check for browser closed error
        if "failed to create client" in output or "no page target" in output or "BugError" in output:
            return "ERROR: Browser or page was closed"

        # CDP binary wraps all outputs in JSON quotes. Decode once.
        # If result is still a JSON array/object string (nested), decode again.
        try:
            decoded = json.loads(output)
            if isinstance(decoded, str) and decoded and decoded[0] in '{"[':
                decoded = json.loads(decoded)
            return decoded
        except (json.JSONDecodeError, ValueError):
            return output

    def form(self, selector: str, value: str = None, check: str = None,
             select: str = None, frame_id: str = "") -> str:
        """
        Fill form field with human-like behavior.

        Args:
            selector: CSS selector for the element
            value: Text value to input (for text fields)
            check: Checkbox state "true"/"false"
            select: Dropdown option value
            frame_id: Optional frame ID for iframe elements

        Returns:
            Execution result
        """
        cmd = [CDP_PATH, "form", selector]
        if value is not None:
            cmd.extend(["--value", value])
        if check is not None:
            cmd.extend(["--check", check])
        if select is not None:
            cmd.extend(["--select", select])
        if frame_id:
            cmd.extend(["--frame-id", frame_id])
        cmd.extend(["--host", self.host, "--port", self.port])

        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=CDP_TIMEOUT
        )
        output = result.stdout + result.stderr

        # Check for browser closed error
        if "failed to create client" in output or "no page target" in output or "BugError" in output:
            return "ERROR: Browser or page was closed"

        return output

    def navigate(self, url: str) -> str:
        """
        Navigate to URL using CDP navi command (handles navigation properly).

        Args:
            url: Target URL

        Returns:
            Navigation result
        """
        result = subprocess.run(
            [CDP_PATH, "navi", url,
             "--host", self.host, "--port", self.port],
            capture_output=True,
            text=True,
            timeout=CDP_TIMEOUT
        )
        return result.stdout + result.stderr

    def scroll(self, pixels: str = "300") -> str:
        """
        Scroll the page.

        Args:
            pixels: Number of pixels to scroll (default 300)

        Returns:
            Scroll result
        """
        result = subprocess.run(
            [CDP_PATH, "scroll", pixels,
             "--host", self.host, "--port", self.port],
            capture_output=True,
            text=True,
            timeout=CDP_TIMEOUT
        )
        return result.stdout + result.stderr

    def screenshot(self) -> str:
        """
        Capture screenshot from current page.

        Returns:
            Base64-encoded screenshot data
        """
        result = subprocess.run(
            [CDP_PATH, "screenshot",
             "--host", self.host, "--port", self.port],
            capture_output=True,
            text=True,
            timeout=CDP_TIMEOUT
        )
        return result.stdout.strip()

    def get_page_info(self) -> Dict:
        """
        Get current page URL and title.

        Returns:
            Dict with 'url' and 'title' keys
        """
        url_result = self.eval("window.location.href")
        title_result = self.eval("document.title")

        # Clean up quotes from result
        url = url_result.strip().strip('"').strip("'")
        title = title_result.strip().strip('"').strip("'")

        return {"url": url, "title": title}

    def wait_page_stable(self, timeout: int = 15, poll_interval: float = 0.8) -> bool:
        """Wait until page is fully loaded and DOM stops changing.

        Returns True if page stabilized, False if timed out.
        """
        deadline = time.time() + timeout
        last_body = ""
        stable_count = 0
        ready = False

        while time.time() < deadline:
            try:
                # Check readyState
                rs = self.eval("document.readyState", "").strip().strip('"')
                if rs == "complete":
                    ready = True
                # Check DOM stability (body text length stops changing)
                body = self.eval(
                    "(function(){return document.body?document.body.innerText.length:0;})()",
                    "").strip()
                if body == last_body and ready:
                    stable_count += 1
                    if stable_count >= 3:  # 3 consecutive stable checks
                        return True
                else:
                    stable_count = 0
                last_body = body
            except Exception:
                pass
            time.sleep(poll_interval)

        return ready  # timed out but at least readyState was complete

    def wait_for_element(self, selector: str, timeout: int = 15,
                         frame_id: str = "") -> bool:
        """Poll until element exists and is visible. Returns True if found."""
        deadline = time.time() + timeout
        esc = selector.replace("'", "\\'")
        while time.time() < deadline:
            try:
                result = self.eval(
                    f"(function(){{var e=document.querySelector('{esc}');"
                    f"return e&&e.offsetWidth>0?'yes':'no';}})()",
                    frame_id)
                if "yes" in result:
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False


def report_screenshot(task_id: str, step: str, screenshot_b64: str, url: str = "",
                      api_url: str = "https://fmr.3tkj.cn/api/quest/screenshot") -> bool:
    """
    Report screenshot to API.

    Args:
        task_id: Task ID for tracking
        step: Step identifier (can be empty string)
        screenshot_b64: Base64-encoded screenshot data
        url: Current page URL (for tracking)
        api_url: API endpoint URL

    Returns:
        True if reported successfully, False otherwise
    """
    # Input validation
    if not task_id or not isinstance(task_id, str):
        return False

    if step is None or not isinstance(step, str):
        step = ""

    # Simplified validation - just check screenshot_b64 exists and not too large
    if not screenshot_b64 or not isinstance(screenshot_b64, str):
        return False

    if len(screenshot_b64) > 15 * 1024 * 1024:  # 15MB limit for base64 string
        return False

    # Prepare request
    data = {
        "task_id": task_id,
        "step": step,
        "base64": screenshot_b64,
        "url": url
    }

    for attempt in range(2):
        try:
            import urllib.request

            req = urllib.request.Request(
                api_url,
                data=json.dumps(data).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )

            with urllib.request.urlopen(req, timeout=15) as response:
                result = response.read().decode('utf-8')
                return True

        except Exception as e:
            if attempt < 1:
                time.sleep(1)
                continue
            # Log error but don't fail hard
            return False

    return False


def report_url(cdp_helper: CDPHelper, task_id: str, step: str,
               log: logging.Logger = None, base64_content: str = "") -> bool:
    """
    Report current page URL to API (with optional base64 content).

    This function reports the URL, step, and optional base64 content.

    Args:
        cdp_helper: CDPHelper instance for browser communication
        task_id: Task ID for tracking
        step: Step identifier (e.g., "form_button_clicked", "form_field_filled")
        log: Optional logger for debug output
        base64_content: Optional base64 content (e.g., success reason)

    Returns:
        True if URL reported successfully, False otherwise
    """
    try:
        # Get current page URL
        page_info = cdp_helper.get_page_info()
        current_url = page_info.get("url", "")

        if log:
            log.info(f"[URL Report] step={step}, URL: {current_url[:100] if current_url else 'NO URL'}")
            if base64_content:
                log.info(f"[URL Report] base64_content: {base64_content[:100]}")

        # Prepare request
        data = {
            "task_id": task_id,
            "step": step,
            "base64": base64_content,
            "url": current_url
        }

        api_url = "https://fmr.3tkj.cn/api/quest/screenshot"

        for attempt in range(2):
            try:
                import urllib.request

                if log:
                    log.info(f"[URL Report] Sending data: {json.dumps(data)}")

                req = urllib.request.Request(
                    api_url,
                    data=json.dumps(data).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )

                with urllib.request.urlopen(req, timeout=15) as response:
                    result = response.read().decode('utf-8')
                    if log:
                        log.info(f"[URL Report] API response: {result[:100]}")
                    return True

            except Exception as e:
                if log:
                    log.warning(f"[URL Report] Attempt {attempt + 1} failed: {e}")
                if attempt < 1:
                    time.sleep(1)
                    continue
                return False

        return False

    except Exception as e:
        if log:
            log.warning(f"[URL Report] Failed: {e}")
        return False


def setup_logger(name: str) -> logging.Logger:
    """
    Set up thread-safe logger for form fill scripts.

    Args:
        name: Logger name (e.g., 'car-insurance', 'senior-survey')

    Returns:
        Configured logger instance
    """
    # Sanitize name to prevent directory traversal
    import re
    sanitized = re.sub(r'[^\w\-.]', '_', name)[:50]

    logger = logging.getLogger(f'form_fill_{sanitized}')

    # Thread-safe logger setup
    with _logger_setup_lock:
        if logger.handlers:
            return logger

        logger.setLevel(logging.INFO)

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)

        # File handler (with PID suffix to avoid permission conflicts)
        log_dir = "/opt/skills/auto-farm-skill/logs"
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f'{sanitized}.log')
        try:
            file_handler = logging.FileHandler(log_path)
        except (PermissionError, OSError):
            # File owned by another user - use PID suffix
            log_path = os.path.join(log_dir, f'{sanitized}_{os.getpid()}.log')
            file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(logging.DEBUG)

        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(formatter)
        file_handler.setFormatter(formatter)

        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    return logger


# CDP tool usage guide - shared across all form fill scripts
CDP_USAGE_GUIDE = """
**首先检查是否有CTA按钮**：很多页面不是直接显示表单，而是先有一个大按钮（如 "Get Quotes"、"Apply Now"、"Request Access"、"Get a Free Quote"、"Compare Now"、"START QUOTES" 等）。如果页面没有表单但有这类按钮，先点击按钮进入表单页。

## CDP工具正确用法：
### cdp_form - 填写表单字段（首选）
  - 文本输入: cdp_form(selector="#firstName", value="John")
  - 下拉选择: cdp_form(selector="select.day", select="15")
    **select参数用于SELECT下拉，value参数用于INPUT文本，别搞混！**
  - 复选框: cdp_form(selector="#agree", check="true")
### cdp_click - 点击按钮/链接
  - 普通: cdp_click(selector="button.submit")
  - iframe内: cdp_click(selector="button", frame_id=frameId)
### cdp_eval - 执行JS（仅用于验证字段值或关闭弹窗）
  - 验证: cdp_eval(script="document.querySelector('#fn').value", frame_id=frameId)
  - 关弹窗: cdp_eval(script="document.querySelector('.close').click()")
### iframe表单操作流程：
  1. cdp_snapshot() 获取页面JSON
  2. 看JSON末尾的 childFrames 数组，找到目标iframe的 frameId 和 name
     例: {"frameId":"6F7A...","name":"swift-registration-...","url":"..."}
  3. 操作iframe内元素时，所有cdp_xxx都要传 frame_id=那个frameId
  4. 先cdp_snapshot()看iframe里的字段ID，再用cdp_form填
### 重要提醒：
  - cdp_form返回 "filled: selector = value" 就是成功，不要重复填同一字段
  - 下拉选择器必须用select参数，不能用value参数
  - iframe内的所有操作都必须传frame_id
"""
