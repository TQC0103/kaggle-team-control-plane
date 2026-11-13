"""Pure decisions. No network, credentials, GPU, local owner changes or submission."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

SHA = re.compile(r"^[a-f0-9]{64}$")
SLUG = re.compile(r"^[a-z0-9_-]+/[a-z0-9_-]+$")
REQUIRED_CHECKS = (
    "native_checkpoint", "models", "optimizers", "schedulers", "rng",
    "history_prefix", "source", "split", "metrics", "best_weights",
    "frozen_model", "private_archive_readback", "recipient_input_read",
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def validate_plan(plan: dict) -> None:
    if plan.get("schema") != 1 or not isinstance(plan.get("chain_id"), str):
        raise ValueError("invalid plan schema/chain")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", plan["chain_id"]):
        raise ValueError("invalid chain ID")
    if not SHA.fullmatch(str(plan.get("scientific_sha256", ""))):
        raise ValueError("scientific seal required")
    if plan.get("no_test") is not True or plan.get("no_sheet") is not True:
        raise ValueError("research boundaries must be explicit")
    if type(plan.get("cap")) is not int or not 1 <= plan["cap"] <= 100:
        raise ValueError("invalid authorized cap")
    if type(plan.get("seed")) is not int:
        raise ValueError("seed required")
    steps = plan.get("steps", [])
    if not steps or len(steps) > plan["cap"]:
        raise ValueError("steps must fit authorized cap")
    end = None
    for s in steps:
        if type(s.get("start")) is not int or type(s.get("stop")) is not int:
            raise ValueError("integer boundaries required")
        if s["start"] < 0 or s["start"] >= s["stop"] or (end is not None and s["start"] != end):
            raise ValueError("invalid/discontinuous boundaries")
        if not SHA.fullmatch(str(s.get("source_sha256", ""))):
            raise ValueError("step source seal required")
        end = s["stop"]
    if end != plan.get("max_iterations"):
        raise ValueError("final boundary must equal locked maximum")


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    key: str | None = None


def decide(plan: dict, state: dict, observation: dict | None,
           validation: dict | None, now: float) -> Decision:
    validate_plan(plan)
    seal = digest(plan)
    if state.get("plan_sha256") != seal:
        return Decision("STOP", "plan seal mismatch")
    if state.get("owner_kind") != "cloud" or not state.get("handover_receipt"):
        return Decision("WAIT_LOCAL_OWNER", "no verified single-writer handover")
    if state.get("hold"):
        return Decision("STOP", "chain hold")
    if state.get("uncertain_intent"):
        return Decision("RECONCILE", "uncertain request cannot be replayed")
    index = state.get("step_index")
    if type(index) is not int or not 0 <= index < len(plan["steps"]):
        return Decision("STOP", "invalid step index")
    receipt = state.get("receipt") or {}
    if not SLUG.fullmatch(str(receipt.get("kernel_slug", ""))) or type(receipt.get("version")) is not int or receipt["version"] < 1:
        return Decision("RECONCILE", "missing exact parent receipt")
    exact_ref = f"{receipt['kernel_slug']}/{receipt['version']}"
    if not observation or observation.get("ref") != exact_ref:
        return Decision("WAIT_REMOTE", "no matching remote observation")
    age = now - observation.get("observed_at", float("-inf"))
    if not 0 <= age <= 120:
        return Decision("WAIT_REMOTE", "remote evidence stale or from future")
    status = observation.get("status")
    if status in {"queued", "running"}:
        return Decision("WAIT_REMOTE", status)
    if status in {"failed", "cancelled"}:
        return Decision("STOP", "parent terminal infrastructure/scientific outcome needs review")
    if status != "complete":
        return Decision("WAIT_REMOTE", "unknown status is not queued or complete")
    step = plan["steps"][index]
    validation_key = digest({"chain": plan["chain_id"], "plan": seal,
                             "parent": exact_ref, "source": step["source_sha256"]})
    if not validation:
        return Decision("VALIDATE_PARENT", "completion does not prove native validity", validation_key)
    expected = {"key": validation_key, "parent_ref": exact_ref, "seed": plan["seed"],
                "iteration": step["stop"], "source_sha256": step["source_sha256"],
                "scientific_sha256": plan["scientific_sha256"]}
    if any(validation.get(k) != v for k, v in expected.items()):
        return Decision("STOP", "validation identity mismatch")
    if (validation.get("status") != "PASS" or
        not SHA.fullmatch(str(validation.get("checkpoint_sha256", ""))) or
        any(validation.get("checks", {}).get(k) is not True for k in REQUIRED_CHECKS)):
        return Decision("STOP", "native validation/archive/input gate failed or missing")
    if index == len(plan["steps"]) - 1:
        return Decision("DONE", "fixed final endpoint validated")
    if type(state.get("used")) is not int or state["used"] >= plan["cap"]:
        return Decision("STOP", "authorized attempt cap exhausted")
    admission = state.get("admission") or {}
    age = now - admission.get("observed_at", float("-inf"))
    if not 0 <= age <= 120 or admission.get("reconciled") is not True:
        return Decision("WAIT_ACCOUNT", "fresh exact account reconciliation required")
    if (not admission.get("enabled") or not 0 < admission.get("remaining_gpu_hours", 0) or
        type(admission.get("active_jobs")) is not int or
        not 0 <= admission["active_jobs"] < 2 or admission.get("input_read") is not True or
        not isinstance(admission.get("account_id"), str) or not admission["account_id"]):
        return Decision("WAIT_ACCOUNT", "no eligible quota/slot/input access")
    child = plan["steps"][index + 1]
    key = digest({"chain": plan["chain_id"], "plan": seal, "parent": exact_ref,
                  "checkpoint": validation["checkpoint_sha256"],
                  "source": child["source_sha256"], "start": child["start"],
                  "stop": child["stop"], "account": admission["account_id"]})
    return Decision("READY_CHILD", "all parent and account gates passed; shadow does not submit", key)
