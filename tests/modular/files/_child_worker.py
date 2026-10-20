"""Synthetic child process used to exercise actual loss of worker memory."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from _support import SyntheticProducer, reopen_storage, service

catalog, company_id, task_id, mode, *extra = sys.argv[1:]
storage = reopen_storage(catalog)
producer = SyntheticProducer()
files = service(storage, producer)

if mode == "crash_produce":
    producer.produce_hook = lambda *args: os._exit(37)
elif mode == "crash_publish":
    producer.publish_hook = lambda *args: os._exit(38)
elif mode == "hold":
    marker = Path(extra[0])

    def hold(*args):
        marker.write_text("worker holds OS execution lock", encoding="utf-8")
        while True:
            time.sleep(0.05)

    producer.produce_hook = hold
elif mode == "crash_handoff":
    from ai_accounting_modular.files._repository import FilesRepository

    FilesRepository.insert = lambda *args: os._exit(39)
    plan = json.loads(Path(extra[0]).read_text(encoding="utf-8"))
    files.ensure_current(plan, plan["scope_head"], "successor request")
    raise AssertionError("handoff crash did not run")
else:
    raise ValueError(mode)

files.run(company_id, task_id)
raise AssertionError("worker crash did not run")
