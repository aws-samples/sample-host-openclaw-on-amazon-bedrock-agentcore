"""Connector-generic approvable proposal contract.

``ActionProposalV1`` generalizes the capability-specific
``actions.models.DraftRevision`` (a persisted, approvable PREPARED proposal)
into the superset the persistence layer already consumes in
``DynamoActionRepository.create_prepared``: an exact, immutable binding of
action / user / draft revision / capability / resource / connection / arguments
with a canonical payload hash and an aware creation timestamp.

The existing Gmail ``DraftRevision`` structurally satisfies this Protocol with
two thin, read-only properties (``capability``/``connection_ref``) so it
registers as an ``ActionProposalV1`` with zero stored-shape change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, runtime_checkable

try:
    from .models import canonical_args_hash
except ImportError:  # pragma: no cover - bare-module load path (action_proposals)
    from action_models import canonical_args_hash


@runtime_checkable
class ActionProposalV1(Protocol):
    """The connector-generic superset of an approvable, persisted proposal.

    Structurally satisfied by ``actions.models.DraftRevision``. Never rename or
    add a *stored* field here: the persisted PREPARED item shape is frozen by
    ``DynamoActionRepository.create_prepared``.
    """

    action_id: str
    user_id: str
    draft_revision: int
    args: Mapping[str, object]
    created_at: datetime

    @property
    def capability(self) -> str: ...

    @property
    def resource(self) -> str: ...

    @property
    def connection_ref(self) -> str: ...

    @property
    def payload_hash(self) -> str: ...


@dataclass(frozen=True, slots=True)
class GenericActionProposalV1:
    """A connector-agnostic proposal for future (non-Gmail) connectors.

    v0 carries no per-capability argument validation: the concrete adapter is
    responsible for admitting only the arguments its capability allows before
    building a proposal. The canonical payload hash is computed exactly the way
    the Gmail path computes it, so approval/dispatch equality gates generalize
    unchanged.
    """

    action_id: str
    user_id: str
    draft_revision: int
    capability: str
    resource: str
    connection_ref: str
    args: Mapping[str, object]
    created_at: datetime

    @property
    def payload_hash(self) -> str:
        return canonical_args_hash(self.args)
