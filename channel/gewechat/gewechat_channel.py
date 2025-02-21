import os
import time
import json
import web
from urllib.parse import urlparse
import re
import cv2
import requests
import threading
import glob

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel
from channel.gewechat.gewechat_message import GeWeChatMessage
from common.log import logger
from common.singleton import singleton
from common.tmp_dir import TmpDir
from config import conf, save_config
from lib.gewechat import GewechatClient
from voice.audio_convert import mp3_to_silk,split_audio
import uuid

MAX_UTF8_LEN = 2048

@singleton
class GeWeChatChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = []

    def __init__(self):
        super().__init__()

        self.base_url = conf().get("gewechat_base_url")
        if not self.base_url:
            logger.error("[gewechat] base_url is not set")
            return
        self.token = conf().get("gewechat_token")
        self.client = GewechatClient(self.base_url, self.token)

        # 设置临时文件的最大保留时间（3小时）​
        self.temp_file_max_age = 3 * 60 * 60  # 秒        ​
        # 启动定期清理任务​
        self._start_cleanup_task()

        # 如果token为空，尝试获取token
        if not self.token:
            logger.warning("[gewechat] token is not set，trying to get token")
            token_resp = self.client.get_token()
            # {'ret': 200, 'msg': '执行成功', 'data': 'tokenxxx'}
            if token_resp.get("ret") != 200:
                logger.error(f"[gewechat] get token failed: {token_resp}")
                return
            self.token = token_resp.get("data")
            conf().set("gewechat_token", self.token)
            save_config()
            logger.info(f"[gewechat] new token saved: {self.token}")
            self.client = GewechatClient(self.base_url, self.token)

        self.app_id = conf().get("gewechat_app_id")
        if not self.app_id:
            logger.warning("[gewechat] app_id is not set，trying to get new app_id when login")

        self.download_url = conf().get("gewechat_download_url")
        if not self.download_url:
            logger.warning("[gewechat] download_url is not set, unable to download image")

        logger.info(f"[gewechat] init: base_url: {self.base_url}, token: {self.token}, app_id: {self.app_id}, download_url: {self.download_url}")

    def _start_cleanup_task(self):
        """启动定期清理任务"""
        def _do_cleanup():
            while True:
                try:
                    # 清理音频文件
                    self._cleanup_audio_files()
                    # 清理视频文件
                    self._cleanup_video_files()
                    # 清理图片文件
                    self._cleanup_image_files()
                    # 每30分钟执行一次清理
                    time.sleep(240 * 60)
                except Exception as e:
                    logger.error(f"[gewechat] 清理任务异常: {e}")
                    time.sleep(60)  # 发生错误时等待1分钟后重试

        cleanup_thread = threading.Thread(target=_do_cleanup, daemon=True)
        cleanup_thread.start()
        logger.info("[gewechat] 清理任务已启动")

    def _cleanup_audio_files(self):
        """清理过期的音频文件"""
        try:
            # 获取临时目录
            tmp_dir = TmpDir().path()
            current_time = time.time()
            # 音频文件最大保留3小时
            max_age = 3 * 60 * 60  

            # 清理.mp3和.silk文件
            for ext in ['.mp3', '.silk']:
                pattern = os.path.join(tmp_dir, f'*{ext}')
                for fpath in glob.glob(pattern):
                    try:
                        # 获取文件修改时间
                        mtime = os.path.getmtime(fpath)
                        # 如果文件超过最大保留时间，则删除
                        if current_time - mtime > max_age:
                            os.remove(fpath)
                            logger.debug(f"[gewechat] 清理过期音频文件: {fpath}")
                    except Exception as e:
                        logger.warning(f"[gewechat] 清理音频文件失败 {fpath}: {e}")

        except Exception as e:
            logger.error(f"[gewechat] 音频文件清理任务异常: {e}")
    
    def _cleanup_video_files(self):
        """清理过期的视频文件"""
        try:
            tmp_dir = TmpDir().path()
            current_time = time.time()
            # 视频文件最大保留3小时
            max_age = 3 * 60 * 60

            # 清理.mp4文件
            pattern = os.path.join(tmp_dir, '*.mp4')
            for fpath in glob.glob(pattern):
                try:
                    mtime = os.path.getmtime(fpath)
                    if current_time - mtime > max_age:
                        os.remove(fpath)
                        logger.debug(f"[gewechat] 清理过期视频文件: {fpath}")
                except Exception as e:
                    logger.warning(f"[gewechat] 清理视频文件失败 {fpath}: {e}")

        except Exception as e:
            logger.error(f"[gewechat] 视频文件清理任务异常: {e}")

    def _cleanup_image_files(self):
        """清理过期的图片文件"""
        try:
            tmp_dir = TmpDir().path()
            current_time = time.time()
            # 图片文件最大保留3小时
            max_age = 3 * 60 * 60

            # 清理.png、.jpg、.gif文件
            for ext in ['.png', '.jpg', '.gif']:
                pattern = os.path.join(tmp_dir, f'*{ext}')
                for fpath in glob.glob(pattern):
                    try:
                        mtime = os.path.getmtime(fpath)
                        if current_time - mtime > max_age:
                            os.remove(fpath)
                            logger.debug(f"[gewechat] 清理过期图片文件: {fpath}")
                    except Exception as e:
                        logger.warning(f"[gewechat] 清理图片文件失败 {fpath}: {e}")

        except Exception as e:
            logger.error(f"[gewechat] 图片文件清理任务异常: {e}")

    def startup(self):
        # 如果app_id为空或登录后获取到新的app_id，保存配置
        app_id, error_msg = self.client.login(self.app_id)
        if error_msg:
            logger.error(f"[gewechat] login failed: {error_msg}")
            return

        # 如果原来的self.app_id为空或登录后获取到新的app_id，保存配置
        if not self.app_id or self.app_id != app_id:
            conf().set("gewechat_app_id", app_id)
            save_config()
            logger.info(f"[gewechat] new app_id saved: {app_id}")
            self.app_id = app_id

        # 获取回调地址，示例地址：http://172.17.0.1:9919/v2/api/callback/collect
        callback_url = conf().get("gewechat_callback_url")
        if not callback_url:
            logger.error("[gewechat] callback_url is not set, unable to start callback server")
            return

        # 创建新线程设置回调地址
        import threading
        def set_callback():
            # 等待服务器启动（给予适当的启动时间）
            import time
            logger.info("[gewechat] sleep 3 seconds waiting for server to start, then set callback")
            time.sleep(3)

            # 设置回调地址，{ "ret": 200, "msg": "操作成功" }
            callback_resp = self.client.set_callback(self.token, callback_url)
            if callback_resp.get("ret") != 200:
                logger.error(f"[gewechat] set callback failed: {callback_resp}")
                return
            logger.info("[gewechat] callback set successfully")

        callback_thread = threading.Thread(target=set_callback, daemon=True)
        callback_thread.start()

        # 从回调地址中解析出端口与url path，启动回调服务器
        parsed_url = urlparse(callback_url)
        path = parsed_url.path
        # 如果没有指定端口，使用默认端口80
        port = parsed_url.port or 80
        logger.info(f"[gewechat] start callback server: {callback_url}, using port {port}")
        urls = (path, "channel.gewechat.gewechat_channel.Query")
        app = web.application(urls, globals(), autoreload=False)
        web.httpserver.runsimple(app.wsgifunc(), ("0.0.0.0", port))
    def get_segment_durations(self, file_paths):
        """
        获取每段音频的时长
        :param file_paths: 分段文件路径列表
        :return: 每段时长列表（毫秒）
        """
        from pydub import AudioSegment
        durations = []
        for path in file_paths:
            audio = AudioSegment.from_file(path)
            durations.append(len(audio))
        return durations

    def send(self, reply: Reply, context: Context):
        receiver = context["receiver"]
        gewechat_message = context.get("msg")
        if reply.type in [ReplyType.TEXT, ReplyType.ERROR, ReplyType.INFO]:
            reply_text = reply.content
            ats = ""
            if gewechat_message and gewechat_message.is_group:
                ats = gewechat_message.actual_user_id

            # 从配置文件读取no_need_at配置，如果为True且是群聊，则移除@
            no_need_at = conf().get("no_need_at", False)
            if gewechat_message and no_need_at and gewechat_message.actual_user_nickname:
                logger.debug(f"[gewechat] no_need_at is True, will remove @{gewechat_message.actual_user_nickname}")
                # 对昵称中的特殊字符进行转义
                escaped_nickname = re.escape(gewechat_message.actual_user_nickname)
                reply_text = re.sub(r'@' + escaped_nickname + r'\s?', '', reply_text, count=1)


            # 使用 !~! 进行分割
            split_messages = reply_text.split('!~!')
            # 过滤空消息和图片链接，并移除前后空格
            split_messages = [msg.strip() for msg in split_messages
                  if msg.strip() and not msg.strip().startswith('< img src=') and not msg.strip().startswith('{"files": "') and not msg.strip().startswith('"}')]

            # 发送消息
            for index, msg in enumerate(split_messages):
                if index == 0:
                    # 第一条消息立即发送
                    self.client.post_text(self.app_id, receiver, msg, ats)
                    logger.info("[gewechat] Do send text to {}: {}".format(receiver, msg))
                else:
                    # 根据当前消息长度计算延迟
                    delay = len(msg) * 0.05  # 每个字符0.05秒延迟
                    time.sleep(delay)
                    self.client.post_text(self.app_id, receiver, msg, ats)
                    logger.info("[gewechat] Do send text to {} after {}s delay: {}".format(
                        receiver, delay, msg))
        elif reply.type == ReplyType.VOICE:
            try:
                content = reply.content
                if not content or not os.path.exists(content):
                    logger.error(f"[gewechat] 语音文件未找到: {content}")
                    return

                if not content.endswith('.mp3'):
                    logger.error(f"[gewechat] 仅支持MP3格式: {content}")
                    return

                # 创建临时文件列表用于后续清理
                temp_files = []
                
                try:
                    # 分割音频文件
                    audio_length_ms, files = split_audio(content, 60 * 1000)
                    if not files:
                        logger.error("[gewechat] 音频分割失败")
                        return
                        
                    temp_files.extend(files)  # 添加分割后的文件到清理列表
                    logger.info(f"[gewechat] 音频分割完成，共 {len(files)} 段")
                    
                    # 获取每段时长
                    segment_durations = self.get_segment_durations(files)
                    tmp_dir = TmpDir().path()
                    
                    # 预先转换所有文件
                    silk_files = []
                    callback_url = conf().get("gewechat_callback_url")
                    
                    for i, fcontent in enumerate(files, 1):
                        try:
                            # 转换为SILK格式
                            silk_name = f"{os.path.basename(fcontent)}_{i}.silk"
                            silk_path = os.path.join(tmp_dir, silk_name)
                            temp_files.append(silk_path)
                            
                            duration = mp3_to_silk(fcontent, silk_path)
                            if duration > 0 and os.path.exists(silk_path):
                                silk_url = callback_url + "file=" + silk_path
                                silk_files.append((silk_url, duration))
                                logger.info(f"[gewechat] 第 {i} 段转换成功，时长: {duration/1000:.1f}秒")
                            else:
                                raise Exception(f"转换失败: {fcontent}")
                                
                        except Exception as e:
                            logger.error(f"[gewechat] 第 {i} 段转换失败: {e}")
                            return
                    
                    # 发送所有语音片段
                    for i, (silk_url, duration) in enumerate(silk_files, 1):
                        try:
                            self.client.post_voice(self.app_id, receiver, silk_url, duration)
                            logger.info(f"[gewechat] 发送第 {i}/{len(silk_files)} 段语音")
                            
                            # 固定0.3秒的发送间隔
                            if i < len(silk_files):
                                time.sleep(0.3)
                                
                        except Exception as e:
                            logger.error(f"[gewechat] 发送第 {i} 段语音失败: {e}")
                            continue
                except Exception as e:
                    logger.error(f"[gewechat] {e}")
                    return

            finally:
                # 清理所有临时文件
                for temp_file in temp_files:
                    try:
                        if os.path.exists(temp_file):
                            os.remove(temp_file)
                            logger.debug(f"[gewechat] 清理临时文件: {temp_file}")
                    except Exception as e:
                        logger.warning(f"[gewechat] 清理文件失败 {temp_file}: {e}")


        elif reply.type == ReplyType.IMAGE_URL or reply.type == ReplyType.IMAGE:
            image_storage = reply.content
            if reply.type == ReplyType.IMAGE_URL:
                import requests
                import io
                img_url = reply.content
                logger.debug(f"[gewechat]sendImage, download image start, img_url={img_url}")
                pic_res = requests.get(img_url, stream=True)
                image_storage = io.BytesIO()
                size = 0
                for block in pic_res.iter_content(1024):
                    size += len(block)
                    image_storage.write(block)
                logger.debug(f"[gewechat]sendImage, download image success, size={size}, img_url={img_url}")
                image_storage.seek(0)
                if ".webp" in img_url:
                    try:
                        from common.utils import convert_webp_to_png
                        image_storage = convert_webp_to_png(image_storage)
                    except Exception as e:
                        logger.error(f"[gewechat]sendImage, failed to convert image: {e}")
                        return
            # Save image to tmp directory
            image_storage.seek(0)
            header = image_storage.read(6)
            image_storage.seek(0)
            img_data = image_storage.read()
            image_storage.seek(0)
            extension = ".gif" if header.startswith((b'GIF87a', b'GIF89a')) else ".png"
            img_file_name = f"img_{str(uuid.uuid4())}{extension}"
            img_file_path = TmpDir().path() + img_file_name
            with open(img_file_path, "wb") as f:
                f.write(img_data)
            # Construct callback URL
            callback_url = conf().get("gewechat_callback_url")
            img_url = callback_url + "?file=" + img_file_path
            if extension == ".gif":
                result = self.client.post_file(self.app_id, receiver, file_url=img_url, file_name=img_file_name)
                logger.info("[gewechat] sendGifAsFile, receiver={}, file_url={}, file_name={}, result={}".format(
                    receiver, img_url, img_file_name, result))
            else:
                result = self.client.post_image(self.app_id, receiver, img_url)
                logger.info("[gewechat] sendImage, receiver={}, url={}, result={}".format(receiver, img_url, result))
            if result.get('ret') == 200:
                newMsgId = result['data'].get('newMsgId')
                new_img_file_path = TmpDir().path() + str(newMsgId) + extension
                os.rename(img_file_path, new_img_file_path)
                logger.info("[gewechat] sendImage rename to {}".format(new_img_file_path))
        
        #   elif reply.type == ReplyType.REVOKE:
            # 处理撤回消息
            #logger.info("[gewechat] Do send revoke message to {}".format(receiver))
            #self.client.post_revoke(self.app_id, receiver)
        #elif reply.type == ReplyType.EMOJI:
            # 处理表情消息
            #logger.info("[gewechat] Do send emoji message to {}".format(receiver))
            #self.client.post_emoji(self.app_id, receiver, reply.content)
        #elif reply.type == ReplyType.MINI_PROGRAM:
            # 处理小程序消息
            #logger.info("[gewechat] Do send mini program message to {}".format(receiver))
            #self.client.post_mini_program(self.app_id, receiver, reply.content)
        #elif reply.type == ReplyType.TRANSFER:
            # 处理转账消息
            #logger.info("[gewechat] Do send transfer message to {}".format(receiver))
            #self.client.post_transfer(self.app_id, receiver, reply.content)
        #elif reply.type == ReplyType.RED_PACKET:
            # 处理红包消息
            #logger.info("[gewechat] Do send red packet message to {}".format(receiver))
            #self.client.post_red_packet(self.app_id, receiver, reply.content)
        elif reply.type == ReplyType.VIDEO_URL:
            video_url = reply.content
            logger.info("[gewechat] sendVideo url={}, receiver={}".format(video_url, receiver))
            try:
                import requests
                # 下载视频到临时文件
                tmp_dir = TmpDir().path()
                temp_video = os.path.join(tmp_dir, f"video_{str(uuid.uuid4())}.mp4")
                logger.info(f"[gewechat] Downloading video to: {temp_video}")
                
                # 下载重试机制
                max_retries = 3
                for i in range(max_retries):
                    try:
                        response = requests.get(video_url, stream=True)
                        if response.status_code == 200:
                            with open(temp_video, "wb") as f:
                                for chunk in response.iter_content(chunk_size=8192):
                                    if chunk:
                                        f.write(chunk)
                            logger.info("[gewechat] Video downloaded successfully")
                            break
                        else:
                            logger.error(f"[gewechat] Download attempt {i+1} failed with status code: {response.status_code}")
                            if i == max_retries - 1:
                                raise Exception(f"Failed to download video after {max_retries} attempts")
                            time.sleep(1)
                    except Exception as e:
                        logger.error(f"[gewechat] Download attempt {i+1} failed: {str(e)}")
                        if i == max_retries - 1:
                            raise
                        time.sleep(1)
                
                # 获取视频信息
                logger.info("[gewechat] Getting video info...")
                cap = cv2.VideoCapture(temp_video)
                
                # 获取视频时长（秒）
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                duration = frame_count / fps
                
                # 读取第一帧作为缩略图
                ret, first_frame = cap.read()
                cap.release()
                
                if ret:
                    # 保存缩略图
                    thumb_path = os.path.join(tmp_dir, f"thumb_{str(uuid.uuid4())}.jpg")
                    logger.info(f"[gewechat] Saving thumbnail to: {thumb_path}")
                    cv2.imwrite(thumb_path, first_frame)
                    
                    # 构建本地文件URL
                    callback_url = conf().get("gewechat_callback_url")
                    video_local_url = callback_url + "?file=" + temp_video
                    thumb_local_url = callback_url + "?file=" + thumb_path
                    
                    try:
                        logger.info("[gewechat] Sending video...")
                        res = self.client.post_video(self.app_id, receiver, video_local_url, thumb_local_url, int(duration))
                        logger.info(f"[gewechat] Send video response: {res}")
                        
                        if isinstance(res, dict):
                            if res.get("ret") == 200:
                                logger.info("[gewechat] Video sent successfully")
                            else:
                                error_msg = res.get("msg", "未知错误")
                                logger.error(f"[gewechat] Failed to send video: {error_msg}")
                                self.client.post_text(self.app_id, receiver, f"视频发送失败：{error_msg}")
                        else:
                            logger.error("[gewechat] Invalid response format")
                            self.client.post_text(self.app_id, receiver, "视频发送失败，返回格式错误")
                    except Exception as e:
                        logger.error(f"[gewechat] Error sending video: {str(e)}")
                        self.client.post_text(self.app_id, receiver, "视频发送失败，请稍后重试")
                    finally:
                        # 清理临时文件
                        try:
                            os.remove(temp_video)
                            os.remove(thumb_path)
                        except:
                            pass
                else:
                    logger.error("[gewechat] Failed to get video frame")
                    self.client.post_text(self.app_id, receiver, "视频处理失败，请稍后重试")
            except Exception as e:
                logger.error(f"[gewechat] Failed to process video: {str(e)}")
                self.client.post_text(self.app_id, receiver, "视频处理失败，请稍后重试")
                return
                
        elif reply.type == ReplyType.FILE:
            # 处理文件消息
            file_url = reply.content
            file_name = file_url.split('/')[-1]  # 从URL中获取文件名
            logger.info(f"[gewechat] sendFile url={file_url}, name={file_name}, receiver={receiver}")
            self.client.post_file(self.app_id, receiver, file_url, file_name)
        else:
            logger.error(f"[gewechat] Unsupported reply type: {reply.type}")

class Query:
    def GET(self):
        # 搭建简单的文件服务器，用于向gewechat服务传输语音等文件，但只允许访问tmp目录下的文件
        params = web.input(file="")
        file_path = params.file
        if file_path:
            # 使用os.path.abspath清理路径
            clean_path = os.path.abspath(file_path)
            # 获取tmp目录的绝对路径
            tmp_dir = os.path.abspath("tmp")
            # 检查文件路径是否在tmp目录下
            if not clean_path.startswith(tmp_dir):
                logger.error(f"[gewechat] Forbidden access to file outside tmp directory: file_path={file_path}, clean_path={clean_path}, tmp_dir={tmp_dir}")
                raise web.forbidden()

            if os.path.exists(clean_path):
                with open(clean_path, 'rb') as f:
                    return f.read()
            else:
                logger.error(f"[gewechat] File not found: {clean_path}")
                raise web.notfound()
        return "gewechat callback server is running"

    def POST(self):
        channel = GeWeChatChannel()
        web_data = web.data()
        logger.debug("[gewechat] receive data: {}".format(web_data))
        data = json.loads(web_data)

        # gewechat服务发送的回调测试消息
        if isinstance(data, dict) and 'testMsg' in data and 'token' in data:
            logger.debug(f"[gewechat] 收到gewechat服务发送的回调测试消息")
            return "success"

        gewechat_msg = GeWeChatMessage(data, channel.client)

        # 微信客户端的状态同步消息
        if gewechat_msg.ctype == ContextType.STATUS_SYNC:
            logger.debug(f"[gewechat] ignore status sync message: {gewechat_msg.content}")
            return "success"

        # 忽略非用户消息（如公众号、系统通知等）
        if gewechat_msg.ctype == ContextType.NON_USER_MSG:
            logger.debug(f"[gewechat] ignore non-user message from {gewechat_msg.from_user_id}: {gewechat_msg.content}")
            return "success"

        # 忽略来自自己的消息
        if gewechat_msg.my_msg:
            logger.debug(f"[gewechat] ignore message from myself: {gewechat_msg.actual_user_id}: {gewechat_msg.content}")
            return "success"

        # 忽略过期的消息
        if int(gewechat_msg.create_time) < int(time.time()) - 60 * 5: # 跳过5分钟前的历史消息
            logger.debug(f"[gewechat] ignore expired message from {gewechat_msg.actual_user_id}: {gewechat_msg.content}")
            return "success"

        # 根据消息类型处理不同的回调消息
        msg_type = gewechat_msg.msg.get('Data', {}).get('MsgType')
        if msg_type == 1:  # 文本消息
            logger.info(f"[gewechat] 收到文本消息: {gewechat_msg.content}")
        elif msg_type == 3:  # 图片消息
            logger.info(f"[gewechat] 收到图片消息: {gewechat_msg.content}")
        elif msg_type == 34:  # 语音消息
            logger.info(f"[gewechat] 收到语音消息: {gewechat_msg.content}")
        elif msg_type == 49:  # 引用消息、小程序、公众号等
            logger.info(f"[gewechat] 收到引用消息或小程序消息: {gewechat_msg.content}")
        elif msg_type == 10002:  # 系统消息（如撤回消息、拍一拍等）
            logger.info(f"[gewechat] 收到系统消息: {gewechat_msg.content}")
        elif msg_type == 10000:  # 群聊通知（如修改群名、更换群主等）
            logger.info(f"[gewechat] 收到群聊通知: {gewechat_msg.content}")
        elif msg_type == 37:  # 好友添加请求通知
            logger.info(f"[gewechat] 收到好友添加请求: {gewechat_msg.content}")
        elif msg_type == 42:  # 名片消息
            logger.info(f"[gewechat] 收到名片消息: {gewechat_msg.content}")
        elif msg_type == 43:  # 视频消息
            logger.info(f"[gewechat] 收到视频消息: {gewechat_msg.content}")
        elif msg_type == 47:  # 表情消息
            logger.info(f"[gewechat] 收到表情消息: {gewechat_msg.content}")
        elif msg_type == 48:  # 地理位置消息
            logger.info(f"[gewechat] 收到地理位置消息: {gewechat_msg.content}")
        elif msg_type == 51:  # 视频号消息
            logger.info(f"[gewechat] 收到视频号消息: {gewechat_msg.content}")
        elif msg_type == 2000:  # 转账消息
            logger.info(f"[gewechat] 收到转账消息: {gewechat_msg.content}")
        elif msg_type == 2001:  # 红包消息
            logger.info(f"[gewechat] 收到红包消息: {gewechat_msg.content}")
        elif msg_type == 10002:  # 撤回消息
            logger.info(f"[gewechat] 收到撤回消息: {gewechat_msg.content}")
        elif msg_type == 10002:  # 拍一拍消息
            logger.info(f"[gewechat] 收到拍一拍消息: {gewechat_msg.content}")
        elif msg_type == 10002:  # 群公告
            logger.info(f"[gewechat] 收到群公告: {gewechat_msg.content}")
        elif msg_type == 10002:  # 群待办
            logger.info(f"[gewechat] 收到群待办: {gewechat_msg.content}")
        elif msg_type == 10002:  # 踢出群聊通知
            logger.info(f"[gewechat] 收到踢出群聊通知: {gewechat_msg.content}")
        elif msg_type == 10002:  # 解散群聊通知
            logger.info(f"[gewechat] 收到解散群聊通知: {gewechat_msg.content}")
        elif msg_type == 10002:  # 修改群名称
            logger.info(f"[gewechat] 收到修改群名称通知: {gewechat_msg.content}")
        elif msg_type == 10002:  # 更换群主通知
            logger.info(f"[gewechat] 收到更换群主通知: {gewechat_msg.content}")
        elif msg_type == 10002:  # 群信息变更通知
            logger.info(f"[gewechat] 收到群信息变更通知: {gewechat_msg.content}")
        elif msg_type == 10002:  # 删除好友通知
            logger.info(f"[gewechat] 收到删除好友通知: {gewechat_msg.content}")
        elif msg_type == 10002:  # 退出群聊通知
            logger.info(f"[gewechat] 收到退出群聊通知: {gewechat_msg.content}")
        elif msg_type == 10002:  # 掉线通知
            logger.info(f"[gewechat] 收到掉线通知: {gewechat_msg.content}")
        else:
            logger.warning(f"[gewechat] 未知消息类型: {msg_type}, 内容: {gewechat_msg.content}")


        # 检查发送者是否在黑名单中
        # 获取黑名单和白名单
        nick_name_black_list = conf().get("nick_name_black_list", [])
        nick_name_white_list = conf().get("nick_name_white_list", [])

        # 获取发送者的信息
        sender_id = gewechat_msg.from_user_id  # 发送者的微信ID
        sender_nickname = gewechat_msg.actual_user_nickname  # 发送者的昵称  

        # 仅对私聊消息进行黑白名单检查
        if not gewechat_msg.is_group:
            # 检查发送者是否在白名单中
            is_in_white_list = (
                sender_nickname in nick_name_white_list
                or sender_id in nick_name_white_list
            )

            # 如果发送者在白名单中，直接放行
            if is_in_white_list:
                logger.debug(f"[gewechat] 白名单用户放行: {sender_nickname} - ID: {sender_id}")
                context = channel._compose_context(
                    gewechat_msg.ctype,
                    gewechat_msg.content,
                    isgroup=gewechat_msg.is_group,
                    msg=gewechat_msg,
                )
                if context:
                    channel.produce(context)
                return "success"

            # 检查是否所有用户都被列入黑名单
            if "ALL_USER" in nick_name_black_list:
                logger.debug(f"[gewechat] 所有用户被列入黑名单，忽略消息: {sender_nickname} - ID: {sender_id}")
                return "success"

            # 检查发送者是否在黑名单中
            is_in_black_list = (
                sender_nickname in nick_name_black_list
                or sender_id in nick_name_black_list
            )

            # 如果发送者在黑名单中，忽略消息
            if is_in_black_list:
                logger.debug(f"[gewechat] 忽略来自黑名单用户的消息: {sender_nickname} - ID: {sender_id}")
                return "success"

        # 如果是群聊消息或发送者不在黑名单中，处理消息
        context = channel._compose_context(
            gewechat_msg.ctype,
            gewechat_msg.content,
            isgroup=gewechat_msg.is_group,
            msg=gewechat_msg,
        )
        if context:
            channel.produce(context)
        return "success"

