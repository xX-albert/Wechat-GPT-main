# encoding:utf-8

"""
wechat channel
"""

import io
import json
import os
import threading
import time
import datetime

from pathlib import Path
from datetime import datetime, timedelta
import requests
from bridge.context import *
from bridge.reply import *
from channel.chat_channel import ChatChannel
from channel.wechat.wechat_message import *
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from common.time_check import time_checker
from config import conf, get_appdata_dir
from lib import itchat
from lib.itchat.content import *
from plugins import *


# 使用itchat库注册消息处理函数，支持处理文本、语音、图片、通知四种消息
# 这是处理单人聊天消息的函数
@itchat.msg_register([TEXT, VOICE, PICTURE, NOTE])
def handler_single_msg(msg):
    try:
        # 创建微信消息对象
        cmsg = WechatMessage(msg, False)
    except NotImplementedError as e:
        # 如果发生错误，记录日志并跳过
        logger.debug("[WX]single message {} skipped: {}".format(msg["MsgId"], e))
        return None
    # 处理单人聊天消息
    WechatChannel().handle_single(cmsg)
    return None


# 这是处理群聊消息的函数
@itchat.msg_register([TEXT, VOICE, PICTURE, NOTE], isGroupChat=True)
def handler_group_msg(msg):
    try:
        # 创建微信消息对象
        cmsg = WechatMessage(msg, True)
    except NotImplementedError as e:
        # 如果发生错误，记录日志并跳过
        logger.debug("[WX]group message {} skipped: {}".format(msg["MsgId"], e))
        return None
    # 处理群聊消息
    WechatChannel().handle_group(cmsg)
    return None


# 检查接收的消息是否已处理的装饰器
def _check(func):
    def wrapper(self, cmsg: ChatMessage):
        msgId = cmsg.msg_id
        if msgId in self.receivedMsgs:
            # 如果消息已处理，记录日志并跳过
            logger.info("Wechat message {} already received, ignore".format(msgId))
            return
        self.receivedMsgs[msgId] = cmsg
        create_time = cmsg.create_time  # 消息时间戳
        if conf().get("hot_reload") == True and int(create_time) < int(time.time()) - 60:  # 跳过1分钟前的历史消息
            logger.debug("[WX]history message {} skipped".format(msgId))
            return
        return func(self, cmsg)

    return wrapper


# 可用的二维码生成接口
# https://api.qrserver.com/v1/create-qr-code/?size=400×400&data=https://www.abc.com
# https://api.isoyu.com/qr/?m=1&e=L&p=20&url=https://www.abc.com
# 生成二维码的回调函数
def qrCallback(uuid, status, qrcode):
    # logger.debug("qrCallback: {} {}".format(uuid,status))
    if status == "0":
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(qrcode))
            _thread = threading.Thread(target=img.show, args=("QRCode",))
            _thread.setDaemon(True)
            _thread.start()
        except Exception as e:
            pass

        import qrcode

        url = f"https://login.weixin.qq.com/l/{uuid}"

        qr_api1 = "https://api.isoyu.com/qr/?m=1&e=L&p=20&url={}".format(url)
        qr_api2 = "https://api.qrserver.com/v1/create-qr-code/?size=400×400&data={}".format(url)
        qr_api3 = "https://api.pwmqr.com/qrcode/create/?url={}".format(url)
        qr_api4 = "https://my.tv.sohu.com/user/a/wvideo/getQRCode.do?text={}".format(url)
        print("You can also scan QRCode in any website below:")
        print(qr_api3)
        print(qr_api4)
        print(qr_api2)
        print(qr_api1)

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)


# 微信聊天通道类，继承了ChatChannel类
@singleton
class WechatChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = []

    def __init__(self):
        super().__init__()
        self.receivedMsgs = ExpiredDict(60 * 60 * 24)

    def startup(self):
        itchat.instance.receivingRetryCount = 600  # 修改断线超时时间
        # login by scan QRCode
        hotReload = conf().get("hot_reload", False)
        status_path = os.path.join(get_appdata_dir(), "itchat.pkl")
        itchat.auto_login(
            enableCmdQR=2,
            hotReload=hotReload,
            statusStorageDir=status_path,
            qrCallback=qrCallback,
        )
        self.user_id = itchat.instance.storageClass.userName
        self.name = itchat.instance.storageClass.nickName
        logger.info("Wechat login success, user_id: {}, nickname: {}".format(self.user_id, self.name))
        # start message listener
        itchat.run()

    # handle_* 系列函数处理收到的消息后构造Context，然后传入produce函数中处理Context和发送回复
    # Context包含了消息的所有信息，包括以下属性
    #   type 消息类型, 包括TEXT、VOICE、IMAGE_CREATE
    #   content 消息内容，如果是TEXT类型，content就是文本内容，如果是VOICE类型，content就是语音文件名，如果是IMAGE_CREATE类型，content就是图片生成命令
    #   kwargs 附加参数字典，包含以下的key：
    #        session_id: 会话id
    #        isgroup: 是否是群聊
    #        receiver: 需要回复的对象
    #        msg: ChatMessage消息对象
    #        origin_ctype: 原始消息类型，语音转文字后，私聊时如果匹配前缀失败，会根据初始消息是否是语音来放宽触发规则
    #        desire_rtype: 希望回复类型，默认是文本回复，设置为ReplyType.VOICE是语音回复

    @time_checker
    @_check
    def handle_single(self, cmsg: ChatMessage):
        current_dir = Path(os.getcwd())
        log_directory = current_dir / 'log'
        log_directory.mkdir(parents=True, exist_ok=True)
        warrant_filename = log_directory / 'warrant.json'
        user_filename = log_directory / 'user.json'
        if not warrant_filename.is_file():
            logger.debug("Warrant file does not exist. Creating the file.")
            with open(warrant_filename, 'w') as file:
                initial_data = {}
                json.dump(initial_data, file)

        if not user_filename.is_file():
            with open(user_filename, 'w') as file:
                initial_data = [
                    {
                        "Signature": "",
                        "NickName": "",
                        "Province": "",
                        "try": 0,
                        "warrant_code": "",
                        "value":-1,
                        "validity_period":""
                    }
                ]
                json.dump(initial_data, file)

        content = cmsg._rawmsg["Content"]
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        max_tries = config['max_tries']
        warning_message = config['warning_message']
        arrive_message = config.get('arrive_message', 'default_arrive_message')

        if not warrant_filename.is_file():
            logger.debug("Warrant file does not exist. Creating the file.")
            with open(warrant_filename, 'w') as file:
                initial_data = {}
                json.dump(initial_data, file)

        if not user_filename.is_file():
            with open(user_filename, 'w') as file:
                initial_data = [
                    {
                        "Signature": "",
                        "NickName": "",
                        "Province": "",
                        "try": 0,
                        "warrant_code": "",
                        "value":-1,
                        "validity_period":""
                    }
                ]
                json.dump(initial_data, file)

        if content.startswith("#") and len(content) == 16:
            logger.debug("Message starts with # and is 11 characters long, potential warrant code.")
            warrant_code = content[1:]
            logger.debug(f"Checking if warrant code {warrant_code} is valid and not used yet.")

            # Set the location of your JSON file
            warrant_filename = 'log/warrant.json'

            with open(warrant_filename, 'r') as file:
                warrant_data = json.load(file)

            if warrant_code not in warrant_data or warrant_data[warrant_code]["status"] != "未使用":
                logger.debug("Invalid or already used warrant code.")
                reply = Reply(type=ReplyType.TEXT, content="授权码无效，请核实或联系管理员")
                context = {"receiver": cmsg._rawmsg["FromUserName"]}
                self.send(reply, context)
                return
            else:
                warrant_data[warrant_code]["status"] = "已使用"
                # Save the updated warrant data
                with open(warrant_filename, 'w') as file:
                    json.dump(warrant_data, file, indent=4)

                # Now, update the user info in user.json
                if os.path.isfile(user_filename):
                    with open(user_filename, 'r') as file:
                        data = json.load(file)
                    # Update the user info
                    for user in data:
                        matched = sum(
                            [user[k] == cmsg._rawmsg["User"][k] for k in ['Signature', 'NickName', 'Province']])
                        if matched == 3:
                            user["warrant_code"] = warrant_code
                            user["value"] = warrant_data[warrant_code]["value"]
                            user["activation_time"] = datetime.today().date().isoformat()
                            break
                    else:  # if no existing user found
                        data.append({
                            "Signature": cmsg._rawmsg["User"]["Signature"],
                            "NickName": cmsg._rawmsg["User"]["NickName"],
                            "Province": cmsg._rawmsg["User"]["Province"],
                            "try": 1,
                            "warrant_code": warrant_code,
                            "value": warrant_data[warrant_code]["value"],
                            "activation_time": datetime.date.today().isoformat()
                        })
                    # Save the updated user info
                    with open(user_filename, 'w') as file:
                        json.dump(data, file, indent=4)
                else:
                    logger.error(f"File {user_filename} does not exist.")

                # Send the reply after updating warrant_data and user.json
                reply = Reply(type=ReplyType.TEXT, content="激活成功")
                context = {"receiver": cmsg._rawmsg["FromUserName"]}
                self.send(reply, context)
                logger.debug(f"Warrant code {warrant_code} used and marked as used.")
                return
        warrant = True
        if user_filename.is_file():
            with open(user_filename, 'r') as file:
                data = json.load(file)

            for user in data:
                matched = sum([user[k] == cmsg._rawmsg["User"][k] for k in ['Signature', 'Province']])
                if matched == 2 and user["warrant_code"] and user["activation_time"]:
                    logger.debug(f'User {user["NickName"]} is an activated user.')

                    activation_date = datetime.strptime(user["activation_time"], '%Y-%m-%d').date()
                    days_since_activation = (datetime.today().date() - activation_date).days
                    print(days_since_activation)

                    if days_since_activation == 0:
                        warrant = False

                    elif days_since_activation > abs(user["value"]):
                        reply = Reply(type=ReplyType.TEXT, content=arrive_message)
                        context = {"receiver": cmsg._rawmsg["FromUserName"]}
                        self.send(reply, context)
                        return


        if warrant:
            if cmsg._rawmsg["User"]["ContactFlag"] not in [1, 2, 3]:
                logger.debug("Received message from unauthorized user: {}".format(cmsg._rawmsg["User"]["NickName"]))
                user_info = {
                    "Signature": cmsg._rawmsg["User"]["Signature"],
                    "NickName": cmsg._rawmsg["User"]["NickName"],
                    "Province": cmsg._rawmsg["User"]["Province"],
                    "try": 1,
                    "warrant_code": "",
                    "activation_time": ""
                }
                if not os.path.exists(user_filename):
                    with open(user_filename, 'w') as file:
                        json.dump([user_info], file, indent=4)
                else:
                    with open(user_filename, 'r') as file:
                        data = json.load(file)
                    for user in data:
                        if all([user_info[k] == user[k] for k in ['Signature', 'NickName', 'Province']]):
                            user["try"] += 1
                            if user["try"] > max_tries:
                                # Create a new reply
                                reply = Reply(type=ReplyType.TEXT, content=warning_message)
                                # Create context for the reply
                                context = {"receiver": cmsg._rawmsg["FromUserName"]}
                                # Send the reply
                                self.send(reply, context)
                                return
                            break
                    else:  # This is executed only if the loop ended normally, i.e., no 'break' was encountered.
                        data.append(user_info)

                    with open(user_filename, 'w') as file:
                        json.dump(data, file, indent=4)

        if cmsg.ctype == ContextType.VOICE:
            if conf().get("speech_recognition") != True:
                return
            logger.debug("[WX]receive voice msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.IMAGE:
            logger.debug("[WX]receive image msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.PATPAT:
            logger.debug("[WX]receive patpat msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.TEXT:
            logger.debug("[WX]receive text msg: {}, cmsg={}".format(json.dumps(cmsg._rawmsg, ensure_ascii=False), cmsg))
        else:
            logger.debug("[WX]receive msg: {}, cmsg={}".format(cmsg.content, cmsg))

        context = self._compose_context(cmsg.ctype, cmsg.content, isgroup=False, msg=cmsg)
        if context:
            self.produce(context)

    @time_checker
    @_check
    def handle_group(self, cmsg: ChatMessage):
        if cmsg.ctype == ContextType.VOICE:
            if conf().get("speech_recognition") != True:
                return
            logger.debug("[WX]receive voice for group msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.IMAGE:
            logger.debug("[WX]receive image for group msg: {}".format(cmsg.content))
        elif cmsg.ctype in [ContextType.JOIN_GROUP, ContextType.PATPAT]:
            logger.debug("[WX]receive note msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.TEXT:
            # logger.debug("[WX]receive group msg: {}, cmsg={}".format(json.dumps(cmsg._rawmsg, ensure_ascii=False), cmsg))
            pass
        else:
            logger.debug("[WX]receive group msg: {}".format(cmsg.content))
        context = self._compose_context(cmsg.ctype, cmsg.content, isgroup=True, msg=cmsg)
        if context:
            self.produce(context)

    # 统一的发送函数，每个Channel自行实现，根据reply的type字段发送不同类型的消息
    def send(self, reply: Reply, context: Context):
        receiver = context["receiver"]
        if reply.type == ReplyType.TEXT:
            itchat.send(reply.content, toUserName=receiver)
            logger.info("[WX] sendMsg={}, receiver={}".format(reply, receiver))
        elif reply.type == ReplyType.ERROR or reply.type == ReplyType.INFO:
            itchat.send(reply.content, toUserName=receiver)
            logger.info("[WX] sendMsg={}, receiver={}".format(reply, receiver))
        elif reply.type == ReplyType.VOICE:
            itchat.send_file(reply.content, toUserName=receiver)
            logger.info("[WX] sendFile={}, receiver={}".format(reply.content, receiver))
        elif reply.type == ReplyType.IMAGE_URL:  # 从网络下载图片
            img_url = reply.content
            pic_res = requests.get(img_url, stream=True)
            image_storage = io.BytesIO()
            for block in pic_res.iter_content(1024):
                image_storage.write(block)
            image_storage.seek(0)
            itchat.send_image(image_storage, toUserName=receiver)
            logger.info("[WX] sendImage url={}, receiver={}".format(img_url, receiver))
        elif reply.type == ReplyType.IMAGE:  # 从文件读取图片
            image_storage = reply.content
            image_storage.seek(0)
            itchat.send_image(image_storage, toUserName=receiver)
            logger.info("[WX] sendImage, receiver={}".format(receiver))
