"""仅支持美股的确定性证券解析服务。"""

from collections.abc import Sequence
from dataclasses import dataclass

from trade_agent.capabilities.market_research.contracts import (
    SecurityId,
    SecurityResolution,
    SecurityResolutionStatus,
)
from trade_agent.core.config import MarketSettings


@dataclass(frozen=True, slots=True)
class SecurityResolutionCopy:
    """证券解析 service 返回的用户可见文案。

    Attributes:
        empty_input: 证券输入为空时的提示。
        unsupported_market: 市场不在产品范围时的提示。
        ambiguous: 多个候选同时命中时的提示。
        not_found: 找不到可靠候选时的提示。
    """

    empty_input: str
    unsupported_market: str
    ambiguous: str
    not_found: str

    def __post_init__(self) -> None:
        """拒绝无法展示的空白配置。"""

        if any(
            not value.strip()
            for value in (self.empty_input, self.unsupported_market, self.ambiguous, self.not_found)
        ):
            raise ValueError("证券解析文案不能为空")


def security_resolution_copy_from_settings(settings: MarketSettings) -> SecurityResolutionCopy:
    """把市场部署配置转换为证券解析 service 文案。"""

    return SecurityResolutionCopy(
        empty_input=settings.empty_security_message,
        unsupported_market=settings.unsupported_market_message,
        ambiguous=settings.ambiguous_security_message,
        not_found=settings.security_not_found_message,
    )


class SecurityResolver:
    def __init__(
        self,
        securities: Sequence[SecurityId],
        *,
        supported_market_hints: frozenset[str],
        copy: SecurityResolutionCopy,
    ) -> None:
        """建立只消费注入式市场目录的证券解析器。

        Args:
            securities: 当前 provider 可解析的规范证券目录。
            supported_market_hints: 配置允许的市场代码、别名和交易所提示集合。
            copy: 当前部署使用的证券解析用户文案。
        """

        normalized_hints = frozenset(item.strip().upper() for item in supported_market_hints)
        if not normalized_hints or any(not item for item in normalized_hints):
            raise ValueError("证券解析器必须显式提供市场提示目录")
        self._securities = tuple(securities)
        self._supported_market_hints = normalized_hints
        self._copy = copy

    def resolve(self, query: str, *, market_hint: str | None = None) -> SecurityResolution:
        normalized = query.strip().upper()
        if not normalized:
            return SecurityResolution(
                SecurityResolutionStatus.NOT_FOUND,
                message=self._copy.empty_input,
            )
        if (
            market_hint is not None
            and market_hint.strip().upper() not in self._supported_market_hints
        ):
            return SecurityResolution(
                SecurityResolutionStatus.UNSUPPORTED_MARKET,
                message=self._copy.unsupported_market,
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
                self._copy.ambiguous,
            )
        return SecurityResolution(
            SecurityResolutionStatus.NOT_FOUND,
            message=self._copy.not_found,
        )


__all__ = [
    "SecurityResolutionCopy",
    "SecurityResolver",
    "security_resolution_copy_from_settings",
]
