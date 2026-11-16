"""并发接收消息，主循环在安全节点取走；输入线程不修改历史或写事件。（源自 winddd666/nano-agent）"""

import codecs
import os
import select
import sys
import termios
from copy import deepcopy
from datetime import datetime, timezone
from threading import Event, Lock, Thread


class InputInbox:
    def __init__(self):
        self._lock = Lock()
        self._pending = []
        self._next_id = 1
        self._closed = False
        self._error = None
        self._notified = set()

    def _put(self, kind, priority, **data):
        item = {
            "id": self._next_id,
            "kind": kind,
            "priority": priority,
            "received_at": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        self._next_id += 1
        self._pending.append(item)
        return item["id"]

    def _take(self, predicate):
        chosen = [item for item in self._pending if predicate(item)]
        self._pending = [item for item in self._pending if not predicate(item)]
        priority = {"now": 0, "next": 1, "later": 2}
        return sorted(chosen, key=lambda item: (priority[item["priority"]], item["id"]))

    def submit(self, text, *, immediate=False):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("追加消息必须是非空字符串")
        with self._lock:
            if self._closed:
                raise RuntimeError("本次运行已关闭输入，请启动新任务。")
            return self._put(
                "user",
                "now" if immediate else "next",
                text=text,
                mode="immediate" if immediate else "normal",
            )

    def send_now(self, text="请立即停止当前次要动作，基于最新游戏状态汇报进展并等待下一步。"):
        return self.submit(text, immediate=True)

    def urgent(self):
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            return any(item["kind"] == "user" and item["priority"] == "now" for item in self._pending)

    def fail(self, error):
        with self._lock:
            if not self._closed:
                self._error = error

    def drain_events(self):
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            return self._take(lambda item: True)

    def seal_if_empty(self):
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            if self._pending:
                return False
            self._closed = True
            return True

    def close_events(self):
        with self._lock:
            self._closed = True
            return deepcopy(self._take(lambda item: True))


class StdinInput:
    """显式启用的按行输入；EOF 仅结束接收，退出时不遗留阻塞读取线程。"""

    def __init__(self, inbox, stream=None):
        self.inbox = inbox
        self.stream = sys.stdin if stream is None else stream
        self.stop = Event()

    def __enter__(self):
        self.fd = self.stream.fileno()
        self.terminal = None
        if self.stream.isatty():
            self.terminal = termios.tcgetattr(self.fd)
            settings = termios.tcgetattr(self.fd)
            settings[3] &= ~termios.ICANON
            settings[6][termios.VMIN] = 1
            settings[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, settings)
        self.thread = Thread(target=self._read, name="mc-agent-input", daemon=True)
        self.thread.start()
        return self

    def _read(self):
        decoder = codecs.getincrementaldecoder(self.stream.encoding or "utf-8")(errors="replace")
        buffered = ""
        try:
            while not self.stop.is_set():
                readable, _, _ = select.select([self.fd], [], [], 0.1)
                if not readable:
                    continue
                chunk = os.read(self.fd, 4096)
                for char in decoder.decode(chunk, final=not chunk):
                    if char == "\x04":
                        if buffered.strip():
                            self._submit(buffered)
                        return
                    elif char in ("\x7f", "\b"):
                        buffered = buffered[:-1]
                    elif char == "\n":
                        if buffered.strip():
                            self._submit(buffered.rstrip("\r"))
                        buffered = ""
                    else:
                        buffered += char
                if not chunk:
                    if buffered.strip():
                        self._submit(buffered)
                    return
        except OSError as exc:
            self.inbox.fail(f"运行中输入读取失败：{type(exc).__name__}")
        except RuntimeError:
            return

    def _submit(self, line):
        if line == "/now":
            self.inbox.send_now()
        elif line.startswith("/now "):
            self.inbox.send_now(line[5:])
        else:
            self.inbox.submit(line)

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=1)
        if self.terminal is not None:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.terminal)
