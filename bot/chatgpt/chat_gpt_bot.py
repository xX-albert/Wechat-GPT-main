# encoding:utf-8

import time

import openai
import openai.error
import requests

from bot.bot import Bot
from bot.chatgpt.chat_gpt_session import ChatGPTSession
from bot.openai.open_ai_image import OpenAIImage
from bot.session_manager import SessionManager
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from common.token_bucket import TokenBucket
from config import conf, load_config


# OpenAI对话模型API (可用)
class ChatGPTBot(Bot, OpenAIImage):
    def __init__(self):
        logger.debug("[ChatGPTBot] Initializing ChatGPTBot")
        super().__init__()

        logger.debug("[ChatGPTBot] Setting up OpenAI API key and configuration")
        openai.api_key = conf().get("open_ai_api_key")
        if conf().get("open_ai_api_base"):
            openai.api_base = conf().get("open_ai_api_base")
        proxy = conf().get("proxy")
        if proxy:
            openai.proxy = proxy
        if conf().get("rate_limit_chatgpt"):
            self.tb4chatgpt = TokenBucket(conf().get("rate_limit_chatgpt", 20))

        logger.debug("[ChatGPTBot] Initializing sessions and model")
        self.sessions = SessionManager(ChatGPTSession, model=conf().get("model") or "gpt-3.5-turbo")
        self.args = {
            "model": conf().get("model") or "gpt-3.5-turbo",
            "temperature": conf().get("temperature", 0.9),
            "top_p": 1,
            "frequency_penalty": conf().get("frequency_penalty", 0.0),
            "presence_penalty": conf().get("presence_penalty", 0.0),
            "request_timeout": conf().get("request_timeout", None),
            "timeout": conf().get("request_timeout", None),
        }
        logger.debug("[ChatGPTBot] ChatGPTBot initialized with args: {}".format(self.args))

    def reply(self, query, context=None):
        global group_name
        logger.debug("[CHATGPT] Entering reply function with query: {}".format(query))

        # acquire reply content
        if context.type == ContextType.TEXT:
            logger.info("[CHATGPT] query={}".format(query))
            session_id = context["session_id"]
            reply = None

            clear_memory_commands = conf().get("clear_memory_commands", ["#清除记忆"])
            if query in clear_memory_commands:
                self.sessions.clear_session(session_id)
                reply = Reply(ReplyType.INFO, "记忆已清除")
            elif query == "#清除所有":
                self.sessions.clear_all_session()
                reply = Reply(ReplyType.INFO, "所有人记忆已清除")
            elif query == "#更新配置":
                load_config()
                reply = Reply(ReplyType.INFO, "配置已更新")

            if reply:
                return reply

            session = self.sessions.session_query(query, session_id)
            # 保存初始的模板
            init_system_template = conf().get("character_desc", "")
            group_system_template = conf().get("group_character_desc", "")

            for message in session.messages:
                # 在每次循环时重新获取botname和name
                bot_name = context.kwargs['msg'].to_user_nickname  # 这里需要你自己确定 botname 应该是哪个昵称
                group_name = ''

                if context.kwargs['isgroup']:  # 如果是群聊
                    if message['role'] == 'system':  # 如果是system message
                        message['content'] = group_system_template  # 使用初始的模板
                    name = context.kwargs['msg'].actual_user_nickname  # 使用实际的用户名
                    group_name = context.kwargs['msg'].from_user_nickname
                else:
                    name = context.kwargs['msg'].from_user_nickname  # 使用发送消息的用户名

                # 如果message['content']中没有 {group_name}，则不需要提供 group_name 的值
                if '{group_name}' in message['content']:
                    try:
                        message['content'] = message['content'].format(group_name=group_name, bot_name=bot_name,
                                                                       name=name)
                    except KeyError:
                        pass
                else:
                    try:
                        message['content'] = message['content'].format(bot_name=bot_name, name=name)
                    except KeyError:
                        pass

            logger.debug("[CHATGPT] session query={}".format(session.messages))

            api_key = context.get("openai_api_key")

            reply_content = self.reply_text(session, api_key)
            logger.debug(
                "[CHATGPT] new_query={}, session_id={}, reply_cont={}, completion_tokens={}".format(
                    session.messages,
                    session_id,
                    reply_content["content"],
                    reply_content["completion_tokens"],
                )
            )

            if reply_content["completion_tokens"] == 0 and len(reply_content["content"]) > 0:
                reply = Reply(ReplyType.ERROR, reply_content["content"])
            elif reply_content["completion_tokens"] > 0:
                self.sessions.session_reply(reply_content["content"], session_id, reply_content["total_tokens"])
                reply = Reply(ReplyType.TEXT, reply_content["content"])
            else:
                reply = Reply(ReplyType.ERROR, reply_content["content"])
                logger.debug("[CHATGPT] reply {} used 0 tokens.".format(reply_content))

            return reply

        elif context.type == ContextType.IMAGE_CREATE:
            logger.debug("[CHATGPT] Handling IMAGE_CREATE query")
            ok, retstring = self.create_img(query, 0)
            reply = None
            if ok:
                reply = Reply(ReplyType.IMAGE_URL, retstring)
            else:
                reply = Reply(ReplyType.ERROR, retstring)

            return reply

        else:
            logger.debug("[CHATGPT] Unsupported context type: {}".format(context.type))
            reply = Reply(ReplyType.ERROR, "Bot不支持处理{}类型的消息".format(context.type))
            return reply

    def reply_text(self, session: ChatGPTSession, api_key=None, retry_count=0) -> dict:
        """
        call openai's ChatCompletion to get the answer
        :param session: a conversation session
        :param session_id: session id
        :param retry_count: retry count
        :return: {}
        """
        logger.debug("[CHATGPT] Entering reply_text function with session: {}".format(session))

        try:
            if conf().get("rate_limit_chatgpt") and not self.tb4chatgpt.get_token():
                raise openai.error.RateLimitError("RateLimitError: rate limit exceeded")

            # if api_key == None, the default openai.api_key will be used
            response = openai.ChatCompletion.create(api_key=api_key, messages=session.messages, **self.args)

            result = {
                "total_tokens": response["usage"]["total_tokens"],
                "completion_tokens": response["usage"]["completion_tokens"],
                "content": response.choices[0]["message"]["content"],
            }
            logger.debug("[CHATGPT] Received reply_text result: {}".format(result))

            return result

        except Exception as e:
            need_retry = retry_count < 2
            result = {"completion_tokens": 0, "content": "我现在有点累了，等会再来吧"}

            if isinstance(e, openai.error.RateLimitError):
                logger.warn("[CHATGPT] RateLimitError: {}".format(e))
                result["content"] = "提问太快啦，请休息一下再问我吧"
                if need_retry:
                    time.sleep(20)
            elif isinstance(e, openai.error.Timeout):
                logger.warn("[CHATGPT] Timeout: {}".format(e))
                result["content"] = "我没有收到你的消息"
                if need_retry:
                    time.sleep(5)
            elif isinstance(e, openai.error.APIConnectionError):
                logger.warn("[CHATGPT] APIConnectionError: {}".format(e))
                need_retry = False
                result["content"] = "我连接不到你的网络"
            else:
                logger.warn("[CHATGPT] Exception: {}".format(e))
                need_retry = False
                self.sessions.clear_session(session.session_id)

            if need_retry:
                logger.warn("[CHATGPT] 第{}次重试".format(retry_count + 1))
                return self.reply_text(session, api_key, retry_count + 1)
            else:
                return result


class AzureChatGPTBot(ChatGPTBot):
    def __init__(self):
        super().__init__()
        openai.api_type = "azure"
        openai.api_version = "2023-03-15-preview"
        self.args["deployment_id"] = conf().get("azure_deployment_id")

    def create_img(self, query, retry_count=0, api_key=None):
        logger.debug(f"create_img called with query: {query}, retry_count: {retry_count}")
        api_version = "2022-08-03-preview"
        url = "{}dalle/text-to-image?api-version={}".format(openai.api_base, api_version)
        api_key = api_key or openai.api_key
        headers = {"api-key": api_key, "Content-Type": "application/json"}
        try:
            body = {"caption": query, "resolution": conf().get("image_create_size", "256x256")}
            logger.debug(f"Sending image creation request to: {url}, with body: {body}")
            submission = requests.post(url, headers=headers, json=body)
            operation_location = submission.headers["Operation-Location"]
            retry_after = submission.headers["Retry-after"]
            status = ""
            image_url = ""
            while status != "Succeeded":
                logger.info("waiting for image create..., " + status + ",retry after " + retry_after + " seconds")
                time.sleep(int(retry_after))
                response = requests.get(operation_location, headers=headers)
                status = response.json()["status"]
            image_url = response.json()["result"]["contentUrl"]
            logger.debug(f"Image created successfully, URL: {image_url}")
            return True, image_url
        except Exception as e:
            logger.error("create image error: {}".format(e))
            logger.debug("Exception details:", exc_info=True)
            return False, "图片生成失败"
