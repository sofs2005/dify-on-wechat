import os
import re
import base64
import uuid
import requests
from bridge.context import ContextType
from common.log import logger
from common.tmp_dir import TmpDir
from config import conf

class ReferenceHandler:
    """
    引用消息处理工具类
    提供通用的方法，用于处理引用消息
    """

    @staticmethod
    def get_reference_info(context):
        """
        从上下文中提取引用信息

        Args:
            context: 消息上下文

        Returns:
            dict: 引用信息字典，如果没有引用则返回None
        """
        if not context or 'msg' not in context:
            return None

        msg = context['msg']
        if not hasattr(msg, 'reply_to_message_id') or not msg.reply_to_message_id:
            return None

        ref_info = {
            'id': msg.reply_to_message_id,
            'type': msg.reply_to_message_type,
            'content': msg.reply_to_content,
            'user_id': msg.reply_to_user_id,
            'user_nickname': msg.reply_to_user_nickname,
            'media_path': msg.reply_to_media_path,
            'media_url': msg.reply_to_media_url,
            'media_id': msg.reply_to_media_id,
            'metadata': msg.reply_to_metadata
        }
        return ref_info

    @staticmethod
    def is_reference_available(context, required_type=None):
        """
        检查引用是否可用，可选择检查特定类型

        Args:
            context: 消息上下文
            required_type: 要求的引用消息类型 (ContextType)

        Returns:
            bool: 引用是否可用
        """
        ref_info = ReferenceHandler.get_reference_info(context)
        if not ref_info:
            return False

        if required_type and ref_info['type'] != required_type:
            return False

        # 对于媒体类型，检查媒体是否可用
        if ref_info['type'] in [ContextType.IMAGE, ContextType.VIDEO, ContextType.VOICE]:
            # 尝试获取媒体路径
            media_path = ReferenceHandler.get_image_from_reference(context)
            return media_path is not None

        return True

    @staticmethod
    def prepare_reference_media(context):
        """
        准备引用的媒体文件

        Args:
            context: 消息上下文

        Returns:
            bool: 是否成功准备媒体
        """
        if not context or 'msg' not in context:
            return False

        msg = context['msg']
        if hasattr(msg, '_prepare_fn') and not msg._prepared:
            msg._prepare_fn()
            msg._prepared = True
            return True

        return False

    @staticmethod
    def save_media_to_cache(media_data, media_id, media_type="image"):
        """
        保存媒体到缓存

        Args:
            media_data: 媒体数据
            media_id: 媒体ID
            media_type: 媒体类型 (image, video, voice)

        Returns:
            str: 缓存路径
        """
        try:
            # 确保缓存目录存在
            cache_dir = TmpDir().path() + "reference_cache/"
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir, exist_ok=True)

            # 根据媒体类型确定文件扩展名
            ext = {
                "image": ".png",
                "video": ".mp4",
                "voice": ".mp3"
            }.get(media_type, ".bin")

            # 保存媒体文件
            cache_path = cache_dir + f"ref_{media_type}_{media_id}{ext}"
            with open(cache_path, "wb") as f:
                f.write(media_data)

            logger.info(f"[ReferenceHandler] Saved {media_type} to cache: {cache_path}")
            return cache_path
        except Exception as e:
            logger.error(f"[ReferenceHandler] Error saving media to cache: {str(e)}")
            return None

    @staticmethod
    def get_media_from_cache(media_id, media_type="image"):
        """
        从缓存获取媒体路径

        Args:
            media_id: 媒体ID
            media_type: 媒体类型 (image, video, voice)

        Returns:
            str: 缓存路径，如果不存在则返回None
        """
        try:
            cache_dir = TmpDir().path() + "reference_cache/"

            # 根据媒体类型确定文件扩展名
            ext = {
                "image": ".png",
                "video": ".mp4",
                "voice": ".mp3"
            }.get(media_type, ".bin")

            cache_path = cache_dir + f"ref_{media_type}_{media_id}{ext}"

            if os.path.exists(cache_path):
                logger.info(f"[ReferenceHandler] Found {media_type} in cache: {cache_path}")
                return cache_path

            return None
        except Exception as e:
            logger.error(f"[ReferenceHandler] Error getting media from cache: {str(e)}")
            return None

    @staticmethod
    def download_image_via_gewe(xml_content, media_id=None):
        """
        通过gewe服务器下载图片

        Args:
            xml_content: 图片XML内容
            media_id: 媒体ID，用于缓存文件名

        Returns:
            str: 图片路径，如果下载失败则返回None
        """
        try:
            # 从配置获取gewe服务器信息
            try:
                # 直接使用conf()获取配置
                from config import conf

                # 获取gewe相关配置
                gewe_server = conf().get("gewechat_base_url", "")
                gewe_token = conf().get("gewechat_token", "")
                app_id = conf().get("gewechat_app_id", "")

                logger.info(f"[ReferenceHandler] 获取到的配置: server={gewe_server}, token={gewe_token[:3] + '***' if gewe_token else None}, app_id={app_id}")
            except Exception as e:
                logger.error(f"[ReferenceHandler] 获取配置失败: {str(e)}")
                # 尝试使用备用方法获取配置
                try:
                    from channel.gewechat.gewechat_channel import GeWeChatChannel
                    channel_instance = GeWeChatChannel()
                    gewe_server = channel_instance.base_url
                    gewe_token = channel_instance.token
                    app_id = channel_instance.app_id
                    logger.info(f"[ReferenceHandler] 从通道实例获取配置: server={gewe_server}, token={gewe_token[:3] + '***' if gewe_token else None}, app_id={app_id}")
                except Exception as e2:
                    logger.error(f"[ReferenceHandler] 从通道实例获取配置失败: {str(e2)}")
                    gewe_server = ""
                    gewe_token = ""
                    app_id = ""

            # 确保服务器地址有协议前缀
            if gewe_server and not (gewe_server.startswith("http://") or gewe_server.startswith("https://")):
                gewe_server = "http://" + gewe_server

            # 去除末尾的斜杠
            if gewe_server:
                gewe_server = gewe_server.rstrip('/')

            if not gewe_server or not gewe_token or not app_id:
                logger.error(f"[ReferenceHandler] Missing gewe server configuration: server={gewe_server}, token={gewe_token[:3] + '***' if gewe_token else None}, app_id={app_id}")
                return None

            # 准备XML内容
            if not xml_content:
                logger.error(f"[ReferenceHandler] No XML content for download")
                return None

            # 确保XML内容格式正确
            if not xml_content.startswith('<?xml'):
                xml_content = f'<?xml version="1.0"?>\n{xml_content}'

            # 打印XML内容以便调试
            logger.info(f"[ReferenceHandler] XML content: {xml_content[:100]}...")

            # 尝试提取<img>标签
            try:
                import re
                img_match = re.search(r'<img[^>]*>', xml_content)
                if img_match:
                    img_tag = img_match.group(0)
                    logger.info(f"[ReferenceHandler] Found img tag: {img_tag[:100]}...")

                    # 提取cdnthumburl属性
                    cdnthumburl_match = re.search(r'cdnthumburl="([^"]+)"', img_tag)
                    if cdnthumburl_match:
                        cdnthumburl = cdnthumburl_match.group(1)
                        logger.info(f"[ReferenceHandler] Found cdnthumburl: {cdnthumburl}")

                    # 提取aeskey属性
                    aeskey_match = re.search(r'aeskey="([^"]+)"', img_tag)
                    if aeskey_match:
                        aeskey = aeskey_match.group(1)
                        logger.info(f"[ReferenceHandler] Found aeskey: {aeskey}")
            except Exception as e:
                logger.error(f"[ReferenceHandler] Error extracting img tag: {str(e)}")

            # 准备请求
            headers = {
                'X-GEWE-TOKEN': gewe_token,
                'Content-Type': 'application/json'
            }

            # 检查XML内容是否包含<img>标签
            if '<img' not in xml_content:
                logger.error(f"[ReferenceHandler] XML content does not contain <img> tag")
                # 如果是引用消息的XML，直接返回None，让系统尝试其他方法
                if '<appmsg' in xml_content and '<title>' in xml_content:
                    logger.info(f"[ReferenceHandler] Detected reference message XML, returning None")
                    return None

                # 如果不是引用消息的XML，返回None
                return None

            # 构建完整的XML内容
            complete_xml = None
            try:
                import re
                # 确保XML格式正确，添加必要的换行符
                if not xml_content.strip().startswith('<?xml'):
                    xml_content = '<?xml version="1.0"?>\n' + xml_content

                # 确保<msg>和<img>标签之间有换行符
                xml_content = xml_content.replace('<msg>', '<msg>\n')

                # 提取img标签
                img_match = re.search(r'<img[^>]*>', xml_content)
                if img_match:
                    img_tag = img_match.group(0)
                    logger.info(f"[ReferenceHandler] Found img tag: {img_tag[:100]}...")

                    # 提取必要的属性
                    aeskey = re.search(r'aeskey="([^"]+)"', img_tag)
                    cdnthumburl = re.search(r'cdnthumburl="([^"]+)"', img_tag)
                    cdnthumbaeskey = re.search(r'cdnthumbaeskey="([^"]+)"', img_tag)
                    cdnthumblength = re.search(r'cdnthumblength="([^"]+)"', img_tag)
                    cdnthumbheight = re.search(r'cdnthumbheight="([^"]+)"', img_tag)
                    cdnthumbwidth = re.search(r'cdnthumbwidth="([^"]+)"', img_tag)
                    cdnmidheight = re.search(r'cdnmidheight="([^"]+)"', img_tag)
                    cdnmidwidth = re.search(r'cdnmidwidth="([^"]+)"', img_tag)
                    cdnhdheight = re.search(r'cdnhdheight="([^"]+)"', img_tag)
                    cdnhdwidth = re.search(r'cdnhdwidth="([^"]+)"', img_tag)
                    cdnmidimgurl = re.search(r'cdnmidimgurl="([^"]+)"', img_tag)
                    length = re.search(r'length="([^"]+)"', img_tag)
                    md5 = re.search(r'md5="([^"]+)"', img_tag)
                    hevc_mid_size = re.search(r'hevc_mid_size="([^"]+)"', img_tag)

                    # 添加其他可能的属性
                    encryver = re.search(r'encryver="([^"]+)"', img_tag)

                    if aeskey and cdnthumburl:
                        # 构建完整的XML，确保包含所有属性和正确的格式
                        complete_xml = f'<?xml version="1.0"?>\n<msg>\n<img aeskey="{aeskey.group(1)}" '

                        # 添加encryver属性
                        if encryver:
                            complete_xml += f'encryver="{encryver.group(1)}" '
                        else:
                            complete_xml += 'encryver="1" '

                        # 添加cdnthumbaeskey属性
                        if cdnthumbaeskey:
                            complete_xml += f'cdnthumbaeskey="{cdnthumbaeskey.group(1)}" '
                        else:
                            complete_xml += f'cdnthumbaeskey="{aeskey.group(1)}" '

                        # 添加cdnthumburl属性（必需）
                        complete_xml += f'cdnthumburl="{cdnthumburl.group(1)}" '

                        # 添加其他属性
                        if cdnthumblength:
                            complete_xml += f'cdnthumblength="{cdnthumblength.group(1)}" '
                        if cdnthumbheight:
                            complete_xml += f'cdnthumbheight="{cdnthumbheight.group(1)}" '
                        if cdnthumbwidth:
                            complete_xml += f'cdnthumbwidth="{cdnthumbwidth.group(1)}" '
                        if cdnmidheight:
                            complete_xml += f'cdnmidheight="{cdnmidheight.group(1)}" '
                        if cdnmidwidth:
                            complete_xml += f'cdnmidwidth="{cdnmidwidth.group(1)}" '
                        if cdnhdheight:
                            complete_xml += f'cdnhdheight="{cdnhdheight.group(1)}" '
                        if cdnhdwidth:
                            complete_xml += f'cdnhdwidth="{cdnhdwidth.group(1)}" '
                        if cdnmidimgurl:
                            complete_xml += f'cdnmidimgurl="{cdnmidimgurl.group(1)}" '
                        if length:
                            complete_xml += f'length="{length.group(1)}" '
                        if md5:
                            complete_xml += f'md5="{md5.group(1)}" '
                        if hevc_mid_size:
                            complete_xml += f'hevc_mid_size="{hevc_mid_size.group(1)}" '

                        # 完成XML结构
                        complete_xml = complete_xml.rstrip() + '>\n'
                        complete_xml += '\t<sechashinfobase64></sechashinfobase64>\n'
                        complete_xml += '\t<live>\n'
                        complete_xml += '\t\t<duration>0</duration>\n'
                        complete_xml += '\t\t<size>0</size>\n'
                        complete_xml += '\t\t<md5 />\n'
                        complete_xml += '\t\t<fileid />\n'
                        complete_xml += '\t\t<hdsize>0</hdsize>\n'
                        complete_xml += '\t\t<hdmd5 />\n'
                        complete_xml += '\t\t<hdfileid />\n'
                        complete_xml += '\t\t<stillimagetimems>0</stillimagetimems>\n'
                        complete_xml += '\t</live>\n'
                        complete_xml += '</img>\n'
                        complete_xml += '<platform_signature />\n'
                        complete_xml += '<imgdatahash />\n'
                        complete_xml += '<ImgSourceInfo>\n'
                        complete_xml += '\t<ImgSourceUrl />\n'
                        complete_xml += '\t<BizType>0</BizType>\n'
                        complete_xml += '</ImgSourceInfo>\n'
                        complete_xml += '</msg>'

                        logger.info(f"[ReferenceHandler] Created complete XML: {complete_xml[:100]}...")
                    else:
                        logger.error(f"[ReferenceHandler] Missing required attributes in img tag")
                else:
                    logger.error(f"[ReferenceHandler] Could not find img tag in XML content")
            except Exception as e:
                logger.error(f"[ReferenceHandler] Error creating complete XML: {str(e)}")

            # 使用完整的XML或原始XML
            final_xml = complete_xml if complete_xml else xml_content

            payload = {
                "appId": app_id,
                "type": 2,  # 使用普通质量，更可靠
                "xml": final_xml
            }

            # 发送请求
            logger.info(f"[ReferenceHandler] Downloading image via gewe server: {gewe_server}/message/downloadImage")
            response = requests.post(
                f"{gewe_server}/message/downloadImage",
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                logger.info(f"[ReferenceHandler] Gewe server response: {result}")
                # 检查响应结果，注意返回的ret可能是数字或字符串
                if (result.get('ret') == 200 or result.get('ret') == '200') and result.get('data'):
                    # 检查响应中是否有fileUrl
                    if isinstance(result['data'], dict) and 'fileUrl' in result['data']:
                        # 获取图片URL
                        file_url = result['data']['fileUrl']
                        logger.info(f"[ReferenceHandler] Got image URL from gewe server: {file_url}")

                        # 检查是否需要添加下载域名
                        download_url = conf().get("gewechat_download_url", "")
                        if download_url:
                            download_url = download_url.rstrip('/')
                            # 如果file_url是相对路径，添加下载域名
                            if file_url.startswith('/'):
                                full_url = download_url + file_url
                            else:
                                full_url = download_url + "/" + file_url
                        else:
                            # 如果没有配置下载域名，尝试使用gewe服务器地址
                            base_url = gewe_server.split('/v2/api')[0] if '/v2/api' in gewe_server else gewe_server
                            if file_url.startswith('/'):
                                full_url = base_url + file_url
                            else:
                                full_url = base_url + "/" + file_url

                        logger.info(f"[ReferenceHandler] Downloading image from: {full_url}")

                        try:
                            # 下载图片
                            img_response = requests.get(full_url, timeout=30)
                            if img_response.status_code == 200:
                                file_data = img_response.content

                                # 生成缓存路径
                                cache_dir = TmpDir().path() + "reference_cache/"
                                if not os.path.exists(cache_dir):
                                    os.makedirs(cache_dir, exist_ok=True)

                                # 使用media_id或生成随机ID作为文件名
                                file_id = media_id or str(uuid.uuid4())
                                cache_path = cache_dir + f"ref_image_{file_id}.png"

                                with open(cache_path, "wb") as f:
                                    f.write(file_data)

                                logger.info(f"[ReferenceHandler] Downloaded image via gewe server: {cache_path}")
                                return cache_path
                            else:
                                logger.error(f"[ReferenceHandler] Failed to download image from URL: {img_response.status_code} - {img_response.text}")
                        except Exception as e:
                            logger.error(f"[ReferenceHandler] Error downloading image from URL: {str(e)}")
                    elif isinstance(result['data'], str):
                        # 如果data是字符串，尝试将其解析为base64编码的图片数据
                        try:
                            file_data = base64.b64decode(result['data'])

                            # 生成缓存路径
                            cache_dir = TmpDir().path() + "reference_cache/"
                            if not os.path.exists(cache_dir):
                                os.makedirs(cache_dir, exist_ok=True)

                            # 使用media_id或生成随机ID作为文件名
                            file_id = media_id or str(uuid.uuid4())
                            cache_path = cache_dir + f"ref_image_{file_id}.png"

                            with open(cache_path, "wb") as f:
                                f.write(file_data)

                            logger.info(f"[ReferenceHandler] Downloaded image via gewe server (base64): {cache_path}")
                            return cache_path
                        except Exception as e:
                            logger.error(f"[ReferenceHandler] Error decoding base64 data: {str(e)}")
                    else:
                        logger.error(f"[ReferenceHandler] Unexpected data format in response: {result['data']}")
                else:
                    logger.error(f"[ReferenceHandler] Gewe server returned error: {result}")
            else:
                logger.error(f"[ReferenceHandler] Gewe server request failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"[ReferenceHandler] Error downloading image via gewe: {str(e)}")

        return None

    @staticmethod
    def get_image_from_reference(context):
        """
        从引用消息中获取图片路径

        Args:
            context: 消息上下文

        Returns:
            str: 图片路径，如果没有找到则返回None
        """
        ref_info = ReferenceHandler.get_reference_info(context)
        if not ref_info or ref_info['type'] != ContextType.IMAGE:
            return None

        # 1. 首先检查引用消息是否已有媒体路径
        if ref_info['media_path'] and os.path.exists(ref_info['media_path']):
            return ref_info['media_path']

        # 2. 从消息缓存中获取
        from common.memory import MESSAGE_CACHE
        if ref_info['id'] and ref_info['id'] in MESSAGE_CACHE:
            ref_msg = MESSAGE_CACHE[ref_info['id']]
            if hasattr(ref_msg, 'prepare'):
                ref_msg.prepare()
            if hasattr(ref_msg, 'content') and os.path.exists(ref_msg.content):
                return ref_msg.content

        # 3. 从框架缓存中获取
        from common.memory import USER_IMAGE_CACHE
        for session_id, image_info in USER_IMAGE_CACHE.items():
            if image_info and 'path' in image_info and os.path.exists(image_info['path']):
                return image_info['path']

        # 4. 尝试从临时目录中查找
        if ref_info['id']:
            path_image = TmpDir().path() + ref_info['id'] + ".png"
            if os.path.exists(path_image):
                return path_image

        # 5. 兵底方案：通过gewe服务器下载图片
        xml_content = None
        media_id = ref_info.get('media_id')

        # 优先使用content中的XML内容，因为它包含原始图片的XML
        if ref_info.get('content'):
            xml_content = ref_info['content']
        # 如果没有content，尝试从元数据中获取XML内容
        elif ref_info.get('metadata') and isinstance(ref_info['metadata'], dict):
            xml_content = ref_info['metadata'].get('original_xml')

        # 如果没有从元数据中获取到，尝试从消息内容中提取
        if hasattr(context, 'msg') and hasattr(context.msg, 'content'):
            content = context.msg.content
            if isinstance(content, str):
                # 直接从消息内容中提取原始图片XML
                if '「' in content and '」' in content:
                    # 提取引用部分
                    quoted_part = content.split('」', 1)[0].split('「', 1)[1]

                    # 如果引用部分包含用户名和冒号，尝试去除这部分
                    if ': ' in quoted_part and quoted_part.find(': ') < 50:  # 假设用户名不会太长
                        quoted_part = quoted_part.split(': ', 1)[1]

                    # 直接从引用部分提取属性构建XML
                    aeskey_match = re.search(r'aeskey="([^"]+)"', quoted_part)
                    cdnthumburl_match = re.search(r'cdnthumburl="([^"]+)"', quoted_part)
                    md5_match = re.search(r'md5="([^"]+)"', quoted_part)

                    if aeskey_match and cdnthumburl_match:
                        aeskey = aeskey_match.group(1)
                        cdnthumburl = cdnthumburl_match.group(1)
                        md5 = md5_match.group(1) if md5_match else ""

                        # 构建简化的XML
                        xml_content = f'<?xml version="1.0"?>\n<msg>\n<img aeskey="{aeskey}" encryver="1" cdnthumbaeskey="{aeskey}" cdnthumburl="{cdnthumburl}"'
                        if md5:
                            xml_content += f' md5="{md5}"'
                        xml_content += ' />\n</msg>'
                    else:
                        logger.error(f"[ReferenceHandler] 无法从引用部分提取属性")

                # 如果上面的方法失败，尝试直接从消息内容中提取
                if not xml_content and '<msg>' in content and '</msg>' in content:
                    xml_match = re.search(r'<msg>.*?</msg>', content, re.DOTALL)
                    if xml_match:
                        xml_content = xml_match.group(0)

        # 如果获取到了XML内容，尝试通过gewe服务器下载
        if xml_content:
            image_path = ReferenceHandler.download_image_via_gewe(xml_content, media_id)
            if image_path:
                return image_path

        return None
