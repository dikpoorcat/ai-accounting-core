"""One captured lifecycle protects both successful and failed model production."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CaptureGuard:
    scope_key: str
    component: str
    wanted_head: int
    format_version: int
    revision: int
    ready_head: int
    content_digest: str | None
    captured_head: int

    @classmethod
    def capture(cls, state, head):
        if state is None or state["state"] != "preparing":
            raise ValueError("only a preparing lifecycle can produce a model")
        return cls(
            state["scope_key"],
            state["component_key"],
            state["wanted_head"],
            state["format_version"],
            state["revision"],
            state["ready_head"],
            state["content_digest"],
            head,
        )

    def matches(self, state):
        return (
            state is not None
            and state["state"] == "preparing"
            and (
                state["scope_key"],
                state["component_key"],
                state["wanted_head"],
                state["format_version"],
                state["revision"],
                state["ready_head"],
                state["content_digest"],
            )
            == (
                self.scope_key,
                self.component,
                self.wanted_head,
                self.format_version,
                self.revision,
                self.ready_head,
                self.content_digest,
            )
        )
