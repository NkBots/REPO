#!/usr/local/bin/python3
# coding: utf-8

# ytdlbot - new.py
# 8/14/21 14:37
#

__author__ = "Benny <benny.think@gmail.com>"

import ast
import asyncio
import contextlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import traceback
import typing
from io import BytesIO
from slugify import slugify
import pyrogram.errors
from apscheduler.schedulers.background import BackgroundScheduler
from pyrogram import Client, filters, types
from pyrogram.errors.exceptions.bad_request_400 import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from tgbot_ping import get_runtime
from youtubesearchpython import VideosSearch

from client_init import create_app
from config import (AUTHORIZED_USER, ENABLE_CELERY, ENABLE_VIP, OWNER,
                    REQUIRED_MEMBERSHIP, ZEE5_URL_FORMAT)
from constant import BotText
from db import InfluxDB, MySQL, Redis
from limit import VIP, verify_payment
from tasks import app as celery_app
from tasks import (audio_entrance, direct_download_entrance, hot_patch,
                   ytdl_download_entrance)
from utils import (auto_restart, customize_logger, get_revision,
                   get_user_settings, set_user_settings)
from config import THUMBNAIL_LOCATION


customize_logger(["pyrogram.client", "pyrogram.session.session", "pyrogram.connection.connection"])
logging.getLogger('apscheduler.executors.default').propagate = False

app = create_app()
bot_text = BotText()

logging.info("Authorized users are %s", AUTHORIZED_USER)

class temp(object):
    IS_RUNNING = False
    CANCELLED = {}


thumb_location = f"{os.path.dirname(os.path.abspath(__file__))}/{THUMBNAIL_LOCATION}"

def main_video_dl(client, message, url):
    red = Redis()
    chat_id = message.from_user.id
    client.send_chat_action(chat_id, 'typing')
    red.user_count(chat_id)

    logging.info("start %s", url)

    if not re.findall(r"^https?://", url.lower()) and "voot" not in url and "zee5" not in url:
        red.update_metrics("bad_request")
        message.reply_text("I think you should send me a link.", quote=True)
        return

    if re.findall(r"^https://www\.youtube\.com/channel/", VIP.extract_canonical_link(url)):
        message.reply_text("Channel download is disabled now. Please send me individual video link.", quote=True)
        red.update_metrics("reject_channel")
        return

    red.update_metrics("video_request")
    text = bot_text.get_receive_link_text()
    try:
        # raise pyrogram.errors.exceptions.FloodWait(10)
        bot_msg: typing.Union["types.Message", "typing.Any"] = message.reply_text(text, quote=True)
    except pyrogram.errors.Flood as e:
        bot_msg = _extracted_from_main_video_dl_25(e, message, client)
    client.send_chat_action(chat_id, 'upload_video')
    bot_msg.chat = message.chat
    ytdl_download_entrance(bot_msg, client, url)


# TODO Rename this here and in `main_video_dl`
def _extracted_from_main_video_dl_25(e, message, client):
    f = BytesIO()
    f.write(str(e).encode())
    f.write(b"Your job will be done soon. Just wait! Don't rush.")
    f.name = "Please don't flood me.txt"
    result = message.reply_document(f, caption=f"Flood wait! Please wait {e.x} seconds...." f"Your job will start automatically", quote=True)

    f.close()
    client.send_message(OWNER, f"Flood wait! 🙁 {e.x} sccodds....")
    time.sleep(e.x)

    return result

    
def private_use(func):
    def wrapper(client: "Client", message: "types.Message"):
        chat_id = getattr(message.from_user, "id", None)

        # message type check
        if message.chat.type != "private" and not message.text.lower().startswith("/ytdl"):
            logging.warning("%s, it's annoying me...🙄️ ", message.text)
            return

        # authorized users check
        if AUTHORIZED_USER:
            users = [int(i) for i in AUTHORIZED_USER.split(",")] if AUTHORIZED_USER else []
            if users and chat_id and chat_id not in users:
                message.reply_text(bot_text.private, quote=True)
                return

        # membership check
        if REQUIRED_MEMBERSHIP:
            try:
                app.get_chat_member(REQUIRED_MEMBERSHIP, chat_id)
                logging.info("user %s check passed for group/channel %s.", chat_id, REQUIRED_MEMBERSHIP)
            except UserNotParticipant:
                logging.warning("user %s is not a member of group/channel %s", chat_id, REQUIRED_MEMBERSHIP)
                message.reply_text(bot_text.membership_require, quote=True)
                return

        return func(client, message)

    return wrapper


@app.on_message(filters.command(["start"]))
def start_handler(client: "Client", message: "types.Message"):
    from_id = message.from_user.id
    logging.info("Welcome to youtube-dl bot!")
    client.send_chat_action(from_id, "typing")
    greeting = bot_text.get_vip_greeting(from_id)
    quota = bot_text.remaining_quota_caption(from_id)
    custom_text = bot_text.custom_text
    text = f"{greeting}{bot_text.start}\n\n{quota}\n{custom_text}"

    client.send_message(message.chat.id, text)


@app.on_message(filters.command(["help"]))
def help_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    client.send_message(chat_id, bot_text.help, disable_web_page_preview=True)


@app.on_message(filters.command(["sub"]))
def subscribe_handler(client: "Client", message: "types.Message"):
    vip = VIP()
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    if message.text == "/sub":
        result = vip.get_user_subscription(chat_id)
    else:
        link = message.text.split()[1]
        try:
            result = vip.subscribe_channel(chat_id, link)
        except (IndexError, ValueError):
            result = f"Error: \n{traceback.format_exc()}"
    client.send_message(chat_id, result or "You have no subscription.", disable_web_page_preview=True)


@app.on_message(filters.command(["unsub"]))
def unsubscribe_handler(client: "Client", message: "types.Message"):
    vip = VIP()
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    text = message.text.split(" ")
    if len(text) == 1:
        client.send_message(chat_id, "/unsubscribe channel_id", disable_web_page_preview=True)
        return

    if rows := vip.unsubscribe_channel(chat_id, text[1]):
        text = f"Unsubscribed from {text[1]}"
    else:
        text = "Unable to find the channel."
    client.send_message(chat_id, text, disable_web_page_preview=True)


@app.on_message(filters.command(["patch"]))
def patch_handler(client: "Client", message: "types.Message"):
    username = message.from_user.username
    if username == OWNER:
        celery_app.control.broadcast("hot_patch")
        chat_id = message.chat.id
        client.send_chat_action(chat_id, "typing")
        client.send_message(chat_id, "Oorah!")
        hot_patch()


@app.on_message(filters.command(["uncache"]))
def patch_handler(client: "Client", message: "types.Message"):
    username = message.from_user.username
    if username == OWNER:
        link = message.text.split()[1]
        count = VIP().del_cache(link)
        message.reply_text(f"{count} cache(s) deleted.", quote=True)


@app.on_message(filters.command(["ping"]))
def ping_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    if os.uname().sysname == "Darwin" or ".heroku" in os.getenv("PYTHONHOME", ""):
        bot_info = "ping unavailable."
    else:
        bot_info = get_runtime("ytdlbot_ytdl_1", "YouTube-dl")
    if message.chat.username == OWNER:
        stats = bot_text.ping_worker()[:1000]
        client.send_document(chat_id, Redis().generate_file(), caption=f"{bot_info}\n\n{stats}")
    else:
        client.send_message(chat_id, f"{bot_info}")


@app.on_message(filters.command(["about"]))
def help_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    client.send_message(chat_id, bot_text.about)


@app.on_message(filters.command(["terms"]))
def terms_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    client.send_message(chat_id, bot_text.terms)


@app.on_message(filters.command(["sub_count"]))
def sub_count_handler(client: "Client", message: "types.Message"):
    username = message.from_user.username
    if username == OWNER:
        chat_id = message.chat.id
        with BytesIO() as f:
            f.write(VIP().sub_count().encode("u8"))
            f.name = "subscription count.txt"
            client.send_document(chat_id, f)


@app.on_message(filters.command(["direct"]))
def direct_handler(client: "Client", message: "types.Message"):
    chat_id = message.from_user.id
    client.send_chat_action(chat_id, "typing")
    url = re.sub(r'/direct\s*', '', message.text)
    logging.info("direct start %s", url)
    if not re.findall(r"^https?://", url.lower()):
        Redis().update_metrics("bad_request")
        message.reply_text("Send me a DIRECT LINK.", quote=True)
        return

    bot_msg = message.reply_text("Request received.", quote=True)
    Redis().update_metrics("direct_request")
    direct_download_entrance(bot_msg, client, url)


@app.on_message(filters.command(["settings"]))
def settings_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    data = get_user_settings(str(chat_id))
    set_mode = (data[-1])
    text = {"Local": "Celery", "Celery": "Local"}.get(set_mode, "Local")
    mode_text = f"Download mode: **{set_mode}**"
    if message.chat.username == OWNER:
        extra = [InlineKeyboardButton(f"Change download mode to {text}", callback_data=text)]
    else:
        extra = []

    markup = InlineKeyboardMarkup(
        [
            [  # First row
                InlineKeyboardButton("send as document", callback_data="document"),
                InlineKeyboardButton("send as video", callback_data="video"),
                InlineKeyboardButton("send as audio", callback_data="audio")
            ],
            [  # second row
                InlineKeyboardButton("High Quality", callback_data="high"),
                InlineKeyboardButton("Medium Quality", callback_data="medium"),
                InlineKeyboardButton("Low Quality", callback_data="low"),
            ],
            extra
        ]
    )

    client.send_message(chat_id, bot_text.settings.format(data[1], data[2]) + mode_text, reply_markup=markup)


@app.on_message(filters.command(["vip"]))
def vip_handler(client: "Client", message: "types.Message"):
    # process as chat.id, not from_user.id
    chat_id = message.chat.id
    text = message.text.strip()
    client.send_chat_action(chat_id, "typing")
    if text == "/vip":
        client.send_message(chat_id, bot_text.vip, disable_web_page_preview=True)
    else:
        bm: typing.Union["types.Message", "typing.Any"] = message.reply_text(bot_text.vip_pay, quote=True)
        unique = text.replace("/vip", "").strip()
        msg = verify_payment(chat_id, unique, client)
        bm.edit_text(msg)


@app.on_message(filters.command(["playlist"]) & filters.incoming & filters.private)
@private_use
def playlist_handler(client: "Client", message: "types.Message"):
    cmd = message.command
    if len(cmd) == 1:
        message.reply("/playlist link")
    if len(cmd) == 2:
        try:
            input_link = cmd[1]
            shell_cmd = f"yt-dlp -j --flat-playlist {input_link} --user-agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36' | jq -r .webpage_url"
            url_list = subprocess.run(shell_cmd, capture_output=True, shell=True)
            temp.CANCELLED[message.from_user.id] = False
            url_list = url_list.stdout
            playlist_links = url_list.decode("utf-8").splitlines()

            start = 0

            with contextlib.suppress(Exception):
                start = int(cmd[2])

            playlist_links_len = len(playlist_links)

            playlist_links = playlist_links[start:]

            editable = app.send_message(message.chat.id, "Processing playlist...", disable_web_page_preview=True)
            for i, link in enumerate(playlist_links):
                if "zee5" in link:
                    shell_cmd = f"yt-dlp --dump-json {link} --user-agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36' | jq -r .title"
                    res = subprocess.run(shell_cmd, capture_output=True, shell=True)
                    res = res.stdout
                    res = res.decode("utf-8")
                    slug = slugify(res)
                    link = f'{input_link}/{slug}/{link.replace("zee5:", "")}'
                elif "voot" in link:
                    shell_cmd = f"yt-dlp --dump-json {link} --user-agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36'"
                    res = subprocess.run(shell_cmd, capture_output=True, shell=True)
                    res = res.stdout
                    res = res.decode("utf-8")
                    res = json.loads(res)
                    slug = slugify(res["episode"])
                    season_number = res["season_number"]
                    series = res["series"].lower()
                    code = link.replace("voot:", "")
                    link = f"https://www.voot.com/shows/{series}/{season_number}/{code}/{slug}/{code}"

                editable.edit(f'Downloading {i + 1 + start} of {playlist_links_len}\n\n{link}', disable_web_page_preview=True)

                try:
                    main_video_dl(client, message, link)
                except Exception as e:
                    exc_type, exc_value, exc_traceback = sys.exc_info()
                    print(traceback.format_exception(exc_type, exc_value, exc_traceback))
                    time.sleep(60)

                is_cancelled = temp.CANCELLED.get(message.from_user.id)

                if is_cancelled:
                    break

        except Exception as e:
            print(e)
            app.send_message(message.chat.id, e)
        finally:
            temp.CANCELLED[message.from_user.id] = False
            time.sleep(20)
            editable.edit('Task Completed')


@app.on_message(filters.command("cancel") & filters.incoming & filters.private)
@private_use
def cancel_task(bot, message):
    temp.CANCELLED[message.from_user.id] = True
    message.reply("Your task will be cancelled after current task is completed")


@app.on_message(filters.photo & filters.incoming & filters.private)
def save_photo(bot, message):
    download_location = f"{thumb_location}/{message.from_user.id}.jpg"
    message.download(file_name=download_location)

    message.reply_text(
        text="your custom thumbnail is saved",
        quote=True
    )


@app.on_message(filters.command("thumb") & filters.incoming & filters.private)
def send_photo(bot, message):
    download_location = f"{thumb_location}/{message.from_user.id}.jpg"
    if os.path.isfile(download_location):
        message.reply_photo(
            photo=download_location,
            caption="your custom thumbnail",
            quote=True
        )
    else:
        message.reply_text(text="you don't have set thumbnail yet!. send .jpg img to save as thumbnail.", quote=True)


@app.on_message(filters.command("delthumb") & filters.incoming & filters.private)
def delete_photo(bot, message):
    download_location = f"{thumb_location}/{message.from_user.id}.jpg"
    if os.path.isfile(download_location):
        os.remove(download_location)
        message.reply_text(text="your thumbnail removed successfully.", quote=True)
    else:
        message.reply_text(text="you don't have set thumbnail yet!. send .jpg img to save as thumbnail.", quote=True)

@app.on_message(filters.incoming & filters.text)
@private_use
def download_handler(client: "Client", message: "types.Message"):
    url = re.sub(r'/ytdl\s*', '', message.text)
    main_video_dl(client, message, url)



@app.on_callback_query(filters.regex(r"document|video|audio"))
def send_method_callback(client: "Client", callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    data = callback_query.data
    logging.info("Setting %s file type to %s", chat_id, data)
    set_user_settings(chat_id, "method", data)
    callback_query.answer(f"Your send type was set to {callback_query.data}")


@app.on_callback_query(filters.regex(r"high|medium|low"))
def download_resolution_callback(client: "Client", callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    data = callback_query.data
    logging.info("Setting %s file type to %s", chat_id, data)
    set_user_settings(chat_id, "resolution", data)
    callback_query.answer(f"Your default download quality was set to {callback_query.data}")


@app.on_callback_query(filters.regex(r"convert"))
def audio_callback(client: "Client", callback_query: types.CallbackQuery):
    callback_query.answer("Converting to audio...please wait patiently")
    Redis().update_metrics("audio_request")

    vmsg = callback_query.message
    audio_entrance(vmsg, client)


@app.on_callback_query(filters.regex(r"Local|Celery"))
def owner_local_callback(client: "Client", callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    set_user_settings(chat_id, "mode", callback_query.data)
    callback_query.answer(f"Download mode was changed to {callback_query.data}")


def periodic_sub_check():
    vip = VIP()
    exceptions = pyrogram.errors.exceptions
    for cid, uids in vip.group_subscriber().items():
        if video_url := vip.has_newer_update(cid):
            logging.info(f"periodic update:{video_url} - {uids}")
            for uid in uids:
                try:
                    bot_msg = app.send_message(uid, f"{video_url} is downloading...", disable_web_page_preview=True)
                    ytdl_download_entrance(bot_msg, app, video_url)
                except(exceptions.bad_request_400.PeerIdInvalid, exceptions.bad_request_400.UserIsBlocked) as e:
                    logging.warning("User is blocked or deleted. %s", e)
                    vip.deactivate_user_subscription(uid)
                except Exception as e:
                    logging.error("Unknown error when sending message to user. %s", traceback.format_exc())
                finally:
                    time.sleep(random.random() * 3)


if __name__ == '__main__':
    MySQL()
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai", job_defaults={'max_instances': 5})
    scheduler.add_job(Redis().reset_today, 'cron', hour=0, minute=0)
    scheduler.add_job(auto_restart, 'interval', seconds=5)
    scheduler.add_job(InfluxDB().collect_data, 'interval', seconds=60)
    #  default quota allocation of 10,000 units per day,
    scheduler.add_job(periodic_sub_check, 'interval', seconds=60 * 30)
    scheduler.start()
    banner = f"""
▌ ▌         ▀▛▘     ▌       ▛▀▖              ▜            ▌
▝▞  ▞▀▖ ▌ ▌  ▌  ▌ ▌ ▛▀▖ ▞▀▖ ▌ ▌ ▞▀▖ ▌  ▌ ▛▀▖ ▐  ▞▀▖ ▝▀▖ ▞▀▌
 ▌  ▌ ▌ ▌ ▌  ▌  ▌ ▌ ▌ ▌ ▛▀  ▌ ▌ ▌ ▌ ▐▐▐  ▌ ▌ ▐  ▌ ▌ ▞▀▌ ▌ ▌
 ▘  ▝▀  ▝▀▘  ▘  ▝▀▘ ▀▀  ▝▀▘ ▀▀  ▝▀   ▘▘  ▘ ▘  ▘ ▝▀  ▝▀▘ ▝▀▘

By @BennyThink, VIP mode: {ENABLE_VIP}, Distribution: {ENABLE_CELERY}
Version: {get_revision()}
    """
    print(banner)
    app.run()
