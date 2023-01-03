from __future__ import annotations

import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Collection, Iterable, NamedTuple

import crescent
import flare
import hikari
from cachetools import TTLCache
from crescent.ext import tasks
from floodgate import FixedMapping

from mcodingbot.config import CONFIG
from mcodingbot.utils import Context, PEPInfo, PEPManager, Plugin


class PEPBucket(NamedTuple):
    pep: int
    channel: int


class MessageInfo(NamedTuple):
    message: int
    peps: set[PEPInfo]


PEP_REGEX = re.compile(
    r"(?<!https:\/\/peps\.python\.org\/)pep[\s-]*(?P<pep>\d{1,4}\b)",
    re.IGNORECASE,
)
DISMISS_BUTTON_ID = "dismiss"
MAX_AGE_FOR_SEND = timedelta(minutes=1)
MAX_AGE_FOR_EDIT = timedelta(minutes=5)

recent_pep_responses: TTLCache[int, MessageInfo] = TTLCache(
    maxsize=10_000, ttl=MAX_AGE_FOR_EDIT.total_seconds()
)
plugin = Plugin()
pep_manager = PEPManager()
pep_cooldown: FixedMapping[PEPBucket] = FixedMapping(*CONFIG.pep_cooldown)


def trigger_cooldowns(peps: Iterable[PEPInfo], channel_id: int) -> None:
    for pep in peps:
        pep_cooldown.trigger(PEPBucket(pep=pep.number, channel=channel_id))


def reset_cooldowns(peps: Iterable[PEPInfo], channel_id: int) -> None:
    for pep in peps:
        pep_cooldown.reset(PEPBucket(pep=pep.number, channel=channel_id))


def filter_can_send(
    peps: Iterable[PEPInfo], channel_id: int
) -> Iterable[PEPInfo]:
    """
    Removes peps that can not be sent because of the cooldown from the list
    of peps.
    """

    def is_sendable(pep: PEPInfo) -> bool:
        return pep_cooldown.can_trigger(
            PEPBucket(pep=pep.number, channel=channel_id)
        )

    return filter(is_sendable, peps)


@flare.button(label="Dismiss", style=hikari.ButtonStyle.SECONDARY)
async def dismiss_button(
    ctx: flare.MessageContext, author: hikari.Snowflakeish
) -> None:
    """
    When a pep message is dismissed, it will not show up again until the
    cooldown is over.
    NOTE: If the parent message is deleted, the pep message can appear
    immediately. I think changing this behavior would be too complex.
    """

    if ctx.user.id != author:
        await ctx.respond(
            "Only the person who triggered this message can dismiss it.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    await ctx.message.delete()


async def autocomplete_pep(
    ctx: crescent.AutocompleteContext,
    option: hikari.AutocompleteInteractionOption,
) -> list[hikari.CommandChoice]:
    if not option.value:
        return []

    results = pep_manager.search(str(option.value), limit=25)

    return [
        hikari.CommandChoice(name=pep.truncated_title, value=pep.number)
        for pep in results
    ]


@plugin.include
@tasks.loop(hours=12)
async def update_peps() -> None:
    await pep_manager.fetch_pep_info(plugin.app)


@plugin.include
@crescent.command(
    name="pep", description="Find a Python Enhancement Proposal."
)
class PEPCommand:
    pep = crescent.option(
        int, "The PEP number or title.", autocomplete=autocomplete_pep
    )

    async def callback(self, ctx: Context) -> None:
        if not (pep := pep_manager.get(self.pep)):
            await ctx.respond(
                f"{self.pep} is not a valid PEP.", ephemeral=True
            )
            return

        await ctx.respond(embed=pep.embed())


def within_age_cutoff(message_created_at: datetime) -> bool:
    return datetime.now(timezone.utc) - message_created_at <= MAX_AGE_FOR_SEND


def get_pep_refs(content: hikari.UndefinedNoneOr[str]) -> Iterable[PEPInfo]:
    """
    Return a sorted list of all the peps mentioned in a string.
    """
    if not content:
        return []

    peps = sorted(int(ref.group("pep")) for ref in PEP_REGEX.finditer(content))

    return filter(None, map(pep_manager.get, peps))


def get_peps_embed(
    refs: Collection[PEPInfo],
) -> hikari.UndefinedOr[hikari.Embed]:
    """
    Get a pep embed from a list of refs. If there are no refs,
    return `hikari.UNDEFINED`.
    """
    if not refs:
        return hikari.UNDEFINED

    pep_links_message = "\n".join(map(str, refs))

    embed = hikari.Embed(description=pep_links_message, color=CONFIG.theme)

    if (pep_count := len(refs)) > 5:
        embed.set_footer(f"{pep_count - 5} PEPs omitted")

    return embed


@plugin.include
@crescent.event
async def on_message(event: hikari.MessageCreateEvent) -> None:
    """
    Send a message with an embed containing the peps mentioned in a message
    that have not been mentioned recently.
    """
    if not event.message.content or event.author.is_bot:
        return

    if peps := set(
        filter_can_send(get_pep_refs(event.message.content), event.channel_id)
    ):
        trigger_cooldowns(peps, event.channel_id)

        embed = get_peps_embed(peps)
        response = await event.message.respond(
            embed=embed,
            component=await flare.Row(dismiss_button(event.author.id)),
            reply=True,
        )
        recent_pep_responses[event.message.id] = MessageInfo(response.id, peps)


@plugin.include
@crescent.event
async def on_message_edit(event: hikari.GuildMessageUpdateEvent) -> None:
    """
    Edit a message to reflect the new peps in the content. Any message that
    younger than `MAX_AGE_FOR_EDIT.total_seconds()` will be edited.

    Pep messages can immediately show up if the pep number was edited out of
    the parent message.
    """
    if not event.author or event.author.is_bot:
        return

    peps = set(get_pep_refs(event.message.content))

    if original := recent_pep_responses.get(event.message.id):
        # reset the cooldown for any peps that were removed
        reset_cooldowns(original.peps - peps, event.channel_id)

        # reset the cooldown for any peps that are kept
        reset_cooldowns(original.peps & peps, event.channel_id)

        final = set(filter_can_send(peps, event.channel_id))
        trigger_cooldowns(final, event.channel_id)

        embed = get_peps_embed(final)

        with suppress(hikari.NotFoundError):
            if embed:
                await plugin.app.rest.edit_message(
                    event.channel_id, original.message, embed=embed
                )
                recent_pep_responses[event.message.id] = MessageInfo(
                    original.message, final
                )
            else:
                await plugin.app.rest.delete_message(
                    event.channel_id, original.message
                )
                del recent_pep_responses[event.message.id]
    elif (
        peps := set(filter_can_send(peps, event.channel_id))
    ) and within_age_cutoff(event.message.created_at):
        embed = get_peps_embed(peps)
        assert embed
        trigger_cooldowns(peps, event.channel_id)
        response = await event.message.respond(
            embed=embed,
            component=await flare.Row(dismiss_button(event.author.id)),
            reply=True,
        )
        recent_pep_responses[event.message.id] = MessageInfo(response.id, peps)


@plugin.include
@crescent.event
async def on_message_delete(event: hikari.GuildMessageDeleteEvent) -> None:
    """
    Pep messages can immediately show up if the parent message was deleted.
    """
    if original := recent_pep_responses.get(event.message_id):
        reset_cooldowns(original.peps, event.channel_id)

        with suppress(hikari.NotFoundError):
            await plugin.app.rest.delete_message(
                event.channel_id, original.message
            )
            del recent_pep_responses[event.message_id]
