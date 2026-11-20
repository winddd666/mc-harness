"""Count typed lifecycle events and legacy receipts from fleet JSONL."""
from collections import Counter
from dataclasses import dataclass, field


_KINDS = {
    "death": "death",
    "respawn": "respawn",
    "spawn": "respawn",
    "connection_changed": "connection_changed",
    "connected": "connection_changed",
    "disconnected": "connection_changed",
}
_RECEIPT_TAGS = {"[死亡 pos=": "death", "[已重生 pos=": "respawn"}


def _kind(event: dict) -> str | None:
    name = event.get("type") or event.get("event")
    return _KINDS.get(name.removeprefix("mc.")) if isinstance(name, str) else None


def _source_id(event: dict, *, embedded: bool) -> str | None:
    fields = ("event_id", "source_seq", "seq") if embedded else ("event_id", "source_seq")
    for field_name in fields:
        value = event.get(field_name)
        if type(value) is int or isinstance(value, str) and value:
            return str(value)
    return None


@dataclass
class LifecycleCounter:
    bot: str
    seen_ids: set[tuple[str, str]] = field(default_factory=set)
    identified_deaths: int = 0
    death_receipts: int = 0
    respawns: int = 0
    connection_changes: int = 0
    mixed_guardian_incidents: int = 0
    death_causes: Counter[str] = field(default_factory=Counter)

    @property
    def deaths(self) -> int:
        return self.identified_deaths + self.death_receipts

    def _record(
        self, kind: str, *, source_id: str | None = None, cause: str = "", count: int = 1
    ) -> None:
        if source_id is not None:
            key = (self.bot, source_id)
            if key in self.seen_ids:
                return
            self.seen_ids.add(key)
        if kind == "death":
            if source_id is None:
                self.death_receipts += count
            else:
                self.identified_deaths += count
            self.death_causes[cause] += count
        elif kind == "respawn":
            self.respawns += count
        elif kind == "connection_changed":
            self.connection_changes += count

    def observe(self, event: dict) -> None:
        """Use source IDs when present. Envelope seq and untyped event_seqs are local metadata."""
        record_type = event.get("type")
        kind = _kind(event)
        if kind:
            self._record(
                kind,
                source_id=_source_id(event, embedded=False),
                cause=str(event.get("cause") or event.get("message") or ""),
            )
            return
        if record_type not in ("guardian.incident", "mc.notable_events"):
            return
        if record_type == "guardian.incident" and "death_or_connection_changed" in (
            event.get("reasons") or ()
        ):
            self.mixed_guardian_incidents += 1

        typed_kinds = set()
        for item in event.get("events") or ():
            if not isinstance(item, dict) or not (kind := _kind(item)):
                continue
            typed_kinds.add(kind)
            self._record(
                kind,
                source_id=_source_id(item, embedded=True),
                cause=str(item.get("cause") or item.get("message") or ""),
            )

        if record_type == "mc.notable_events":
            # Typed items take precedence over their text rendering in the same message.
            for part in str(event.get("text") or "").split("; "):
                for tag, kind in _RECEIPT_TAGS.items():
                    if kind in typed_kinds or not part.startswith(tag):
                        continue
                    receipt, _, suffix = part.rpartition("×")
                    if receipt.endswith("]") and suffix.isdecimal():
                        self._record(kind, cause=receipt, count=int(suffix))
                    elif part.endswith("]"):
                        self._record(kind, cause=part)
