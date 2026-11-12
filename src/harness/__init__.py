"""评测框架：把一轮问答的结果归因到具体阶段，并汇总成可比较的统计。

当前只包含归因规则与汇总，尚未接入数据集版本管理、基线与门禁（后续迭代）。
规则的输入是评测器已经算好的 checks、证据和阶段记录，不额外调用模型。
"""

from harness.attribution import STAGE_ORDER, attribute, summarize

__all__ = ["STAGE_ORDER", "attribute", "summarize"]
