"""Deterministic authorization policy: subject x action x resource x task, evaluated server-side.

Nothing in here reads model output as a source of truth. The subject comes from the verified
session, the task scope comes from the session the application opened, and approvals and
resource ownership come from the database.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .data import INTERNAL_EXPORT_DESTINATIONS, User

READ_ACTIONS = {"get_transactions", "get_customer_profile"}
WRITE_ACTIONS = {"update_transaction_status"}
EXPORT_ACTIONS = {"export_transactions"}
IDENTITY_ARGS = ("acting_user_id", "acting_role")


def identity_mismatch(session: "Session", args: dict) -> str | None:
    """Describe a model-written identity claim that differs from the verified user, if any."""
    u = session.user
    bad = [f"{k}={args[k]!r}" for k, want in (("acting_user_id", u.user_id), ("acting_role", u.role))
           if args.get(k) not in (None, "", want)]
    return ", ".join(bad) or None


@dataclass(frozen=True)
class Session:
    """Trusted server-side state for one conversation. The model never writes to this."""
    user: User
    task_scope: frozenset[str]       # e.g. {"read"} for an analysis chat, {"read", "write"} for a change ticket


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def _portfolio_of_txn(db, tid):
    r = db.execute("SELECT portfolio_id FROM transactions WHERE transaction_id=?", (tid,)).fetchone()
    return r and r[0]


def _portfolio_of_customer(db, cid):
    r = db.execute("SELECT portfolio_id FROM customers WHERE customer_id=?", (cid,)).fetchone()
    return r and r[0]


class Policy:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def authorize(self, session: Session, action: str, args: dict) -> Decision:
        # The subject is ALWAYS the verified session user. Identity fields the model writes into
        # tool arguments (acting_user_id / acting_role) are never read here; the gateway logs a
        # mismatch, but a claim cannot widen or narrow what the verified user may do.
        user = session.user

        if action == "get_transactions":
            pid = args.get("portfolio_id")
            if pid not in user.portfolios:
                return Decision(False, f"{user.user_id} is not assigned to portfolio {pid}")
            return Decision(True, "portfolio in user's scope")

        if action == "get_customer_profile":
            pid = _portfolio_of_customer(self.db, args.get("customer_id"))
            if pid is None or pid not in user.portfolios:
                return Decision(False, f"customer not in {user.user_id}'s portfolios")
            return Decision(True, "customer in user's scope")

        if action == "update_transaction_status":
            if "write" not in session.task_scope:
                return Decision(False, "this session is read-only")
            if user.role != "admin":
                return Decision(False, f"role {user.role} cannot modify records")
            tid = args.get("transaction_id")
            pid = _portfolio_of_txn(self.db, tid)
            if pid is None or pid not in user.portfolios:
                return Decision(False, "transaction not in user's scope")
            apr = self.db.execute("SELECT * FROM approvals WHERE approval_id=?",
                                  (args.get("approval_id"),)).fetchone()
            if apr is None:
                return Decision(False, "no valid approval supplied")
            if (apr["action"] != action or apr["transaction_id"] != tid or apr["used"]
                    or apr["requested_by"] != user.user_id or apr["approved_by"] == user.user_id
                    or apr["expires"] < "2026-10-18"):
                return Decision(False, f"approval {apr['approval_id']} does not cover this change")
            return Decision(True, "admin with a matching, unused, independent approval")

        if action == "export_transactions":
            if "export" not in session.task_scope:
                return Decision(False, "this session does not permit exports")
            if user.role != "admin":
                return Decision(False, f"role {user.role} cannot export data")
            if args.get("destination") not in INTERNAL_EXPORT_DESTINATIONS:
                return Decision(False, f"destination {args.get('destination')!r} is not an approved internal target")
            if args.get("portfolio_id") not in user.portfolios:
                return Decision(False, "portfolio not in user's scope")
            return Decision(True, "admin export to an internal destination")

        return Decision(False, f"unknown action {action!r}")
