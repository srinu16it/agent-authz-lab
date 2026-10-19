"""The execution boundary: the only path from a model's tool call to a mock operation.

A  authn only            the authenticated agent service is trusted; every call executes
B  authn + prompt rules  same backend as A; the only difference is the system prompt (see agent.py)
C  authn + authz         the policy decides, with the verified session identity, before execution

Every call in every configuration is ALSO judged by an independent oracle (the same policy,
using the true session identity). The oracle never changes what executes in A or B; it only
labels whether what executed was authorized. That separates "the model asked" from "the
backend did it".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .policy import IDENTITY_ARGS, Decision, Policy, Session, identity_mismatch
from .tools import TOOL_NAMES, execute_mock_operation

CONFIGS = ("A", "B", "C")


@dataclass
class CallRecord:
    action: str
    args: dict
    oracle_allowed: bool
    oracle_reason: str
    executed: bool
    enforced_decision: str | None      # C only: the policy's verdict at the boundary
    identity_claim: str | None         # model-written acting_user_id/acting_role that differs from the verified user
    policy_us: float | None            # C only: time spent in the policy check, microseconds


@dataclass
class Gateway:
    config: str
    session: Session
    db: object
    calls: list[CallRecord] = field(default_factory=list)

    def __post_init__(self):
        assert self.config in CONFIGS
        self.policy = Policy(self.db)

    def execute(self, action: str, args: dict) -> dict:
        if action not in TOOL_NAMES:
            return {"status": "ERROR", "error": f"unknown tool {action}"}
        claim = identity_mismatch(self.session, args)
        oracle: Decision = self.policy.authorize(self.session, action, args)
        enforced, us = None, None
        if self.config == "C":
            t0 = time.perf_counter()
            decision = self.policy.authorize(self.session, action, args)
            us = (time.perf_counter() - t0) * 1e6
            enforced = decision.reason
            if not decision.allowed:
                self.calls.append(CallRecord(action, args, oracle.allowed, oracle.reason, False, enforced, claim, us))
                return {"status": "DENIED", "reason": decision.reason}
            # execute under the verified identity, whatever the model wrote
            args = {**{k: v for k, v in args.items() if k not in IDENTITY_ARGS},
                    "acting_user_id": self.session.user.user_id, "acting_role": self.session.user.role}
        result = execute_mock_operation(self.db, action, args)
        self.calls.append(CallRecord(action, args, oracle.allowed, oracle.reason,
                                     result.get("status") == "OK", enforced, claim, us))
        return result
