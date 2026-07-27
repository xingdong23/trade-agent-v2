"""量化能力所需的模型与结果仓储协议。"""

from typing import Protocol

from trade_agent.capabilities.contracts import CapabilityRepository


class QuantitativeRepository(CapabilityRepository, Protocol):
    """供应用层读写量化工件版本的仓储协议。

    Contract:
        - 实现方必须保持 owner 隔离和版本化写入语义。
        - 读取结果必须对应已持久化事实, 不得在仓储层推导新的评分或预测。

    Implemented by:
        生产环境仓储 adapter 与测试 fake repository。
    """


__all__ = ["QuantitativeRepository"]
