"""Chat folders (dialog filters) and the rules that decide which chats belong to a folder."""

from __future__ import annotations

from dataclasses import dataclass

from telethon import types, utils

ARCHIVE_FOLDER_ID = 1  # Telegram's peer folder id of the archive


@dataclass(frozen=True)
class DialogFacts:
    """What a folder rule looks at for one dialog."""

    peer_id: int
    kind: str  # "user", "bot", "group" or "channel"
    is_contact: bool
    muted: bool
    unread: bool  # unread messages or marked as unread
    unread_mentions: bool
    archived: bool


@dataclass(frozen=True)
class Folder:
    id: int
    title: str
    kind: str  # "filter", "chatlist" (shared folder) or "default" (All chats)
    emoticon: str | None
    pinned: tuple[int, ...]
    include: tuple[int, ...]
    exclude: tuple[int, ...]
    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False
    exclude_muted: bool = False
    exclude_read: bool = False
    exclude_archived: bool = False

    def flags(self) -> dict[str, bool]:
        return {
            name: getattr(self, name)
            for name in ("contacts", "non_contacts", "groups", "broadcasts", "bots",
                         "exclude_muted", "exclude_read", "exclude_archived")
        }

    def contains(self, dialog: DialogFacts) -> bool:
        """Same order of checks as the official clients (TDLib `DialogFilter::need_dialog`)."""
        if self.kind == "default":
            return not dialog.archived
        if dialog.peer_id in self.pinned or dialog.peer_id in self.include:
            return True
        if dialog.peer_id in self.exclude:
            return False
        # Chats with unread mentions stay visible even when muted or read are excluded.
        if not dialog.unread_mentions:
            if self.exclude_muted and dialog.muted:
                return False
            if self.exclude_read and not dialog.unread:
                return False
        if self.exclude_archived and dialog.archived:
            return False
        if dialog.kind == "bot":
            return self.bots
        if dialog.kind == "user":
            return self.contacts if dialog.is_contact else self.non_contacts
        if dialog.kind == "group":
            return self.groups
        if dialog.kind == "channel":
            return self.broadcasts
        return False


def _title(value) -> str:
    return value.text if isinstance(value, types.TextWithEntities) else str(value or "")


def _peer_ids(peers, self_id: int | None) -> tuple[int, ...]:
    ids = []
    for peer in peers or ():
        if isinstance(peer, types.InputPeerSelf):
            if self_id is not None:
                ids.append(self_id)
            continue
        try:
            ids.append(utils.get_peer_id(peer))
        except TypeError:
            continue
    return tuple(ids)


def parse_filter(raw, self_id: int | None) -> Folder:
    """Turn a TL `DialogFilter*` object into a `Folder`."""
    if isinstance(raw, types.DialogFilterDefault):
        return Folder(id=0, title="All chats", kind="default", emoticon=None, pinned=(), include=(), exclude=())
    pinned = _peer_ids(raw.pinned_peers, self_id)
    include = _peer_ids(raw.include_peers, self_id)
    if isinstance(raw, types.DialogFilterChatlist):
        return Folder(id=raw.id, title=_title(raw.title), kind="chatlist", emoticon=raw.emoticon,
                      pinned=pinned, include=include, exclude=())
    return Folder(
        id=raw.id,
        title=_title(raw.title),
        kind="filter",
        emoticon=raw.emoticon,
        pinned=pinned,
        include=include,
        exclude=_peer_ids(raw.exclude_peers, self_id),
        contacts=bool(raw.contacts),
        non_contacts=bool(raw.non_contacts),
        groups=bool(raw.groups),
        broadcasts=bool(raw.broadcasts),
        bots=bool(raw.bots),
        exclude_muted=bool(raw.exclude_muted),
        exclude_read=bool(raw.exclude_read),
        exclude_archived=bool(raw.exclude_archived),
    )
