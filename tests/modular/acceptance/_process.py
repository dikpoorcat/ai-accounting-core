"""A deterministic real Kernel child; restarting never replays a business command."""

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path


class Host:
    def __init__(self, root, mode="original"):
        self.root = Path(root)
        self.root.mkdir()
        self.process = None
        self.mode = mode
        self.build_label = "synthetic-build-a"
        self.start()

    def start(self):
        assert self.process is None or self.process.poll() is not None
        self.output = queue.Queue()
        self.stderr = (self.root / "worker.stderr").open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                "-u",
                "-m",
                "tests.modular.acceptance._worker",
                str(self.root),
                self.mode,
                self.build_label,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            encoding="utf-8",
        )

        def receive():
            for line in self.process.stdout:
                self.output.put(line)
            self.output.put(None)

        self.reader = threading.Thread(target=receive, daemon=True)
        self.reader.start()
        try:
            hello = self._receive()
            assert hello == {"started": True, "build_label": self.build_label}, hello
        except BaseException:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=10)
            self.process.stdin.close()
            self.process.stdout.close()
            self.reader.join(timeout=5)
            self.stderr.close()
            raise

    def _receive(self):
        line = self.output.get(timeout=60)
        if line is None:
            self.process.wait(timeout=10)
            return {"stopped": self.process.returncode}
        value = json.loads(line)
        assert "traceback" not in value, value.get("traceback")
        return value

    def call(self, action, **input):
        assert self.process.poll() is None, "Explicit restart required before another request"
        self.process.stdin.write(json.dumps({"action": action, **input}, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        return self._receive()

    def restart(self, build_label=None):
        self.close()
        if build_label is not None:
            self.build_label = build_label
        self.start()

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.call("close")
                self.process.wait(timeout=20)
            self.process.stdin.close()
            self.process.stdout.close()
            self.reader.join(timeout=5)
            self.stderr.close()
