"""Test-only helpers that model the CLI's explicit operator confirmation step."""

from __future__ import annotations

from typing import Any

from ourob.bootstrap.amend import AmendmentLedger, _confirmed_operator_action
from ourob.fsx import sha256_file


def operator_authorize(
    ledger: AmendmentLedger,
    amendment_id: str,
    *,
    authorized_by: str = "operator-test",
) -> dict[str, Any]:
    """Run the library grant under the same confirmed-action scope as the CLI."""
    proposal_sha256 = sha256_file(ledger._path(amendment_id))
    with _confirmed_operator_action(amendment_id, amendment_id, proposal_sha256):
        return ledger.authorize(
            amendment_id,
            confirmation=amendment_id,
            authorized_by=authorized_by,
        )
