from __future__ import annotations

from typing import TYPE_CHECKING, Sequence, cast

import crescent

if TYPE_CHECKING:
    from mcodingbot.bot import Bot

__all__: Sequence[str] = ("Context", "Plugin")


class Context(crescent.Context):
    app: Bot


class Plugin(crescent.Plugin):
    @property
    def app(self) -> Bot:
        return cast("Bot", super().app)
