"""ORM 模型汇总。

``create_all()`` 依赖这里把所有模型 import 进来 —— 只要少 import 一张表，
metadata 里就没有它的定义，建表会静默漏掉。所以新增模型必须在这里登记。

注意：这里刻意没有定义任何 ``relationship()``。异步 Session 下访问未显式
预加载的关系会触发隐式 IO，报出的却是难以定位的 MissingGreenlet。Stage 1
改成在 Repository 里写显式 select，数据怎么读出来一眼可见，等确实需要
对象图导航时再补关系并配 ``lazy`` 策略。
"""

from app.models.agent import AgentRun, Artifact
from app.models.project import Project, ProjectMember
from app.models.requirement import Requirement
from app.models.tool import ToolCall
from app.models.user import User
from app.models.workflow import WorkflowRun

__all__ = [
    "AgentRun",
    "Artifact",
    "Project",
    "ProjectMember",
    "Requirement",
    "ToolCall",
    "User",
    "WorkflowRun",
]
