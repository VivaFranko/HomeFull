"""One photo + advert details, with a text fallback if Telegram rejects the image."""
import logging
from telegram.constants import ParseMode
from telegram.error import BadRequest

log = logging.getLogger(__name__)


async def send_listing(bot, chat_id, text, photo_url=None):
    if photo_url:
        # Raw HTML length is a conservative bound for Telegram's parsed caption.
        short = len(text.encode("utf-16-le")) // 2 <= 1024
        try:
            photo = await bot.send_photo(chat_id, photo_url,
                                         caption=text if short else None,
                                         parse_mode=ParseMode.HTML if short else None)
        except BadRequest:
            log.warning("Telegram rejected an advert photo; sending text instead")
        else:
            if short:
                return
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True,
                                   reply_to_message_id=photo.message_id)
            return
    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                           disable_web_page_preview=True)
