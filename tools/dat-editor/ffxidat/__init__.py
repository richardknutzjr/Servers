"""FFXI item DAT reader/writer for gear customization.

Public API:
    from ffxidat import ItemDat, Item, Slot, Race, Job
"""

from .cipher import decode_bytes, encode_bytes
from .items import Item, ItemDat, ItemType, RECORD_SIZE
from .flags import Slot, Race, Job

__all__ = [
    "decode_bytes",
    "encode_bytes",
    "Item",
    "ItemDat",
    "ItemType",
    "RECORD_SIZE",
    "Slot",
    "Race",
    "Job",
]
