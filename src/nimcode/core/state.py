from enum import Enum


class AgentState(str, Enum):
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    EXECUTING = "EXECUTING"
    YOLO = "YOLO"


class AgentMode(str, Enum):
    PLAN = "plan"
    BUILD = "build"
    YOLO = "yolo"
