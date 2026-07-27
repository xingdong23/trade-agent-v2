"""仅支持美股的确定性证券解析服务。"""

from collections.abc import Sequence

from trade_agent.capabilities.market_research.contracts import (
    Market,
    SecurityId,
    SecurityResolution,
    SecurityResolutionStatus,
)


class SecurityResolver:
    def __init__(self, securities: Sequence[SecurityId]) -> None:
        self._securities = tuple(securities)

    def resolve(self, query: str, *, market_hint: str | None = None) -> SecurityResolution:
        normalized = query.strip().upper()
        if not normalized:
            return SecurityResolution(SecurityResolutionStatus.NOT_FOUND, message="证券输入为空")
        if market_hint is not None and market_hint.strip().upper() not in {
            Market.US.value,
            "USA",
            "NASDAQ",
            "NYSE",
            "AMEX",
        }:
            return SecurityResolution(
                SecurityResolutionStatus.UNSUPPORTED_MARKET,
                message="首版仅支持美国交易所上市证券",
            )

        exact = tuple(
            security
            for security in self._securities
            if security.symbol.upper() == normalized
            or security.display_name.strip().upper() == normalized
        )
        if len(exact) == 1:
            return SecurityResolution(SecurityResolutionStatus.RESOLVED, exact)
        if len(exact) > 1:
            return SecurityResolution(
                SecurityResolutionStatus.AMBIGUOUS,
                exact,
                "证券输入对应多个美国上市标的, 需要用户澄清",
            )
        return SecurityResolution(
            SecurityResolutionStatus.NOT_FOUND,
            message="未找到可可靠匹配的美国上市证券",
        )
