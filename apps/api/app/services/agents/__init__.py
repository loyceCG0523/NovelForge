"""NovelForge 五个核心智能体的统一入口。

这里不急着搬空旧服务文件，而是先建立稳定的 Agent 边界：
StoryPlanningAgent、ChapterWritingAgent、MemoryAgent、QualityAgent、NovelProductionAgent。
旧服务继续作为底层能力存在，外部编排优先通过这些门面调用。
"""

