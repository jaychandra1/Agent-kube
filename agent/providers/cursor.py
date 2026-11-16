"""Cursor, through the Cursor SDK (LLM_PROVIDER=cursor, key CURSOR_API_KEY or LLM_API_KEY).

Unlike the other providers, Cursor runs the agent loop itself. kubesupport hands Cursor's
agent the question plus kubesupport's read-only Kubernetes tools as *custom tools*, and the
agent calls them back. Cursor's own built-in tools (shell, file edits, ...) are switched off
(`tools=[]`, and `shell` is explicitly disallowed), and the agent works in an empty folder,
so the only thing it can do is call kubesupport's tools, which never change the cluster.

The SDK starts a helper process (its "bridge", bundled in the cursor-sdk package) on first
use and keeps it running. One Cursor agent runs at a time.

Needs a paid Cursor plan; usage is billed to the account that owns the API key.
"""
import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from contextlib import contextmanager

from .base import AgentProvider, Limits, ProviderError

logger = logging.getLogger("kubesupport.llm")

WORKDIR = os.path.join(tempfile.gettempdir(), "kubesupport-cursor")  # empty working folder for the agent
_waiter = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cursor-wait")


def _sdk():
    try:
        import cursor_sdk
    except ImportError as e:
        raise ProviderError("the cursor-sdk package is not in this image "
                            "(it was built with --build-arg WITH_CURSOR=false)") from e
    return cursor_sdk


class CursorSession:
    """One conversation with a Cursor agent; the system prompt goes with the first message."""

    def __init__(self, agent, system: str, timeout: float):
        self._agent, self._system, self._timeout = agent, system, timeout

    def send(self, text: str) -> str:
        if self._system:
            text, self._system = f"{self._system}\n\n---\n\n{text}", ""
        run = self._agent.send(text)
        future = _waiter.submit(run.wait)
        try:
            result = future.result(timeout=self._timeout)
        except FuturesTimeout:
            try:
                run.cancel()
            except Exception:  # the run may have just finished
                pass
            raise ProviderError(f"Cursor run took longer than {self._timeout:.0f}s (LLM_TIMEOUT_SECONDS)")
        status = str(getattr(result, "status", ""))
        if status != "finished":
            detail = getattr(result, "error_message", None) or getattr(result, "error_code", None) or ""
            raise ProviderError(f"Cursor run ended with status '{status}' {detail}".strip())
        return getattr(result, "result", "") or ""


class CursorProvider(AgentProvider):
    _lock = threading.Lock()

    def __init__(self, name: str, title: str, model: str, limits: Limits, api_key: str):
        super().__init__(name, title, model, limits)
        _sdk()  # fail at startup, not on the first question, when the SDK is missing
        self.api_key = api_key

    @contextmanager
    def session(self, system, tools, execute):
        sdk = _sdk()
        os.makedirs(WORKDIR, exist_ok=True)

        def runner(tool_name):
            return lambda args, _context: execute(tool_name, dict(args or {}))

        custom_tools = {t["name"]: sdk.CustomTool(execute=runner(t["name"]), description=t.get("description", ""),
                                                  input_schema=t["input_schema"]) for t in tools}
        options = sdk.AgentOptions(
            model=self.model, api_key=self.api_key, name="kubesupport",
            tools=[],                    # no built-in tools: no shell, no file access, no web
            disallowed_tools=["shell"],  # and, to be sure, never the shell
            local=sdk.LocalAgentOptions(cwd=WORKDIR, setting_sources=[], custom_tools=custom_tools),
        )
        with self._lock:
            agent = sdk.Agent.create(options)
            try:
                yield CursorSession(agent, system, self.limits.timeout)
            finally:
                try:
                    agent.close()
                except Exception as e:
                    logger.debug("Closing the Cursor agent failed: %s", type(e).__name__)
