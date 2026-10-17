from enum import Enum


class AgentState(str, Enum):
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    EXECUTING = "EXECUTING"


class AgentMode(str, Enum):
    PLAN = "plan"
    BUILD = "build"
