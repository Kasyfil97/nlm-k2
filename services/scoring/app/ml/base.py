from collections.abc import Mapping
from typing import Any, Protocol


class TrustModel(Protocol):
    """Turns the scoring payload into P(correct) per contract field.

    Returns the §8.3 shape: `fields` for the two document fields and `anggota_keluarga` with seven
    per member, under their INTERNAL names -- the rename to the outgoing contract happens in the
    orchestrator. A field whose value is empty scores `None`, and `anggota_keluarga` is
    positionally aligned with the structuring result and must be the same length.
    """

    name: str

    def predict(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...
