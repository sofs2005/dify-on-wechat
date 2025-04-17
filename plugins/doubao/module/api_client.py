import json
import requests
from common.log import logger
import uuid
import time
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

class ApiClient:
    def __init__(self, token_manager):
        self.token_manager = token_manager
        self.config = token_manager.config
        self.session = self._create_session()
        self.base_url = "https://www.doubao.com"
        self.headers = self._get_headers()
        self.session_info = {}  # 存储会话信息，包括web_id和device_id
        self.init_session()  # 初始化会话

    def _create_session(self):
        """创建带有重试机制的会话"""
        session = requests.Session()
        
        # 配置重试策略
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504]
        )
        
        # 配置适配器
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        return session

    def _get_headers(self):
        """获取请求头"""
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "agw-js-conv": "str",
            "content-type": "application/json",
            "origin": "https://www.doubao.com",
            "referer": "https://www.doubao.com/chat/create-image",
            "sec-ch-ua": "\"Google Chrome\";v=\"129\", \"Not=A?Brand\";v=\"8\", \"Chromium\";v=\"129\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        }
        
        # 添加认证信息
        auth = self.config.get("auth", {})
        headers.update({
            "cookie": auth.get("cookie", ""),
            "x-bogus": auth.get("a_bogus", "")
        })
        
        return headers

    def init_session(self):
        """初始化会话，获取web_id和其他会话参数"""
        try:
            url = f"{self.base_url}/alice/user/launch"
            
            # 获取基本参数
            params = {
                "version_code": "20800",
                "language": "zh",
                "device_platform": "web",
                "aid": "497858",
                "real_aid": "497858",
                "pkg_type": "release_version",
                "use-olympus-account": "1",
                "region": "CN",
                "sys_region": "CN"
            }
            
            # 添加认证信息
            auth = self.config.get("auth", {})
            params["msToken"] = auth.get("msToken", "")
            params["a_bogus"] = auth.get("a_bogus", "")
            
            # 准备请求头
            headers = {
                'accept': '*/*',
                'accept-language': 'zh-CN,zh;q=0.9',
                'content-type': 'application/json',
                'cookie': auth.get("cookie", ""),
                'origin': 'https://www.doubao.com',
                'referer': 'https://www.doubao.com/chat/',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
            }
            
            # 准备请求数据
            data = {
                "select": {
                    "launch_config": True,
                    "assistant_bot_info": True,
                    "landing_config": True,
                    "user_info": True
                }
            }
            
            # 发送请求
            response = self.session.post(
                url,
                headers=headers,
                params=params,
                json=data,
                timeout=10
            )
            
            # 检查响应
            response.raise_for_status()
            result = response.json()
            
            if result.get("code") == 0 and "data" in result:
                # 正确提取web_id和ttwid
                data = result.get("data", {})
                config = data.get("config", {})
                
                # 从config子对象获取web_id和ttwid
                web_id = config.get("web_id", "")
                ttwid = config.get("ttwid", "")
                
                # 保存会话信息
                self.session_info = {
                    "web_id": web_id,
                    "ttwid": ttwid
                }
                
                logger.info(f"[Doubao] 会话初始化成功, web_id: {web_id}, ttwid: {ttwid}")
                return True
            else:
                logger.warning(f"[Doubao] 会话初始化返回异常: {result}")
                return False
            
        except Exception as e:
            logger.error(f"[Doubao] 会话初始化失败: {e}")
            return False

    def _get_params(self):
        """获取URL参数，使用动态获取的会话信息"""
        params = {
            "aid": "497858",
            "device_platform": "web",
            "language": "zh",
            "pc_version": "2.12.0",
            "pkg_type": "release_version",
            "real_aid": "497858",
            "region": "CN",
            "samantha_web": "1",
            "sys_region": "CN",
            "use-olympus-account": "1",
            "version_code": "20800"
        }
        
        # 使用从初始化会话中获取的web_id
        web_id = self.session_info.get("web_id")
        if web_id:
            params["web_id"] = web_id
            params["device_id"] = web_id
            params["tea_uuid"] = web_id
        
        # 添加msToken
        auth = self.config.get("auth", {})
        params["msToken"] = auth.get("msToken", "")
        
        return params

    def refresh_headers(self):
        """刷新请求头"""
        self.headers = self._get_headers()
        return self.headers

    def send_request(self, data, endpoint):
        """发送API请求
        Args:
            data: 请求数据
            endpoint: API端点
        Returns:
            dict: API响应
        """
        try:
            # 构建完整URL
            url = f"{self.base_url}{endpoint}"
            
            # 获取最新的headers和params
            self.refresh_headers()  # 刷新headers
            params = self._get_params()
            
            # 确保content字段是JSON字符串
            if "messages" in data and data["messages"]:
                for msg in data["messages"]:
                    if isinstance(msg.get("content"), dict):
                        msg["content"] = json.dumps(msg["content"], ensure_ascii=False)
            
            # 发送请求
            response = self.session.post(
                url,
                headers=self.headers,  # 使用实例的headers
                params=params,
                json=data,
                stream=True,
                timeout=30
            )
            
            # 检查响应状态
            response.raise_for_status()
            
            # 处理流式响应
            image_urls = []
            conversation_id = None
            section_id = None
            reply_id = None
            response_data = []
            
            for line in response.iter_lines():
                if not line:
                    continue
                    
                line = line.decode('utf-8')
                if not line.startswith("data:"):
                    continue
                    
                try:
                    # 解析事件数据
                    event_json = json.loads(line[5:])
                    if not event_json.get("event_data"):
                        continue
                        
                    # 解析event_data
                    event_data = json.loads(event_json["event_data"])
                    logger.debug(f"[Doubao] Event data: {event_data}")
                    
                    # 提取会话信息
                    if "conversation_id" in event_data:
                        conversation_id = event_data["conversation_id"]
                        section_id = event_data.get("section_id")
                        reply_id = event_data.get("reply_id")
                        logger.info(f"[Doubao] Found conversation info: id={conversation_id}, section={section_id}, reply={reply_id}")
                    
                    # 提取图片信息
                    if "message" in event_data and event_data["message"].get("content_type") == 2010:
                        content = json.loads(event_data["message"]["content"])
                        if "data" in content:
                            response_data = content["data"]
                            for img_data in content["data"]:
                                if "image_raw" in img_data:
                                    url = img_data["image_raw"]["url"]
                                    image_urls.append(url)
                                    logger.info(f"[Doubao] Found image URL: {url}")
                    
                except json.JSONDecodeError as e:
                    logger.error(f"[Doubao] JSON decode error: {e}")
                    continue
                except Exception as e:
                    logger.error(f"[Doubao] Error processing event: {e}")
                    continue
            
            # 返回处理结果
            if image_urls:
                return {
                    "urls": image_urls,
                    "conversation_id": conversation_id,
                    "section_id": section_id,
                    "reply_id": reply_id,
                    "data": response_data
                }
            else:
                logger.error("[Doubao] No image URLs found in response")
                return None
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[Doubao] 请求失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[Doubao] 处理响应失败: {e}")
            return None

    def upload_image(self, image_data, image_type="png"):
        """上传图片
        Args:
            image_data: 图片数据
            image_type: 图片类型
        Returns:
            dict: 上传结果
        """
        try:
            # 构建上传请求
            url = f"{self.base_url}/samantha/image/upload"
            
            # 准备文件数据
            files = {
                "file": (f"image.{image_type}", image_data, f"image/{image_type}")
            }
            
            # 获取headers和params
            headers = self._get_headers()
            params = self._get_params()
            
            # 移除不需要的headers
            headers.pop("content-type", None)
            
            # 发送请求
            response = self.session.post(
                url,
                headers=headers,
                params=params,
                files=files,
                timeout=30
            )
            
            # 检查响应状态
            response.raise_for_status()
            
            # 解析响应
            result = response.json()
            
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    logger.error(f"[Doubao] 响应格式错误: {result}")
                    return None
                    
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[Doubao] 上传图片失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[Doubao] 处理上传响应失败: {e}")
            return None

    def edit_image(self, image_url: str, edit_prompt: str, conversation_id: str, section_id: str, reply_id: str):
        """编辑图片"""
        try:
            # 从图片URL中提取token
            image_token = image_url.split("/")[-1].split("~")[0]
            
            # 构建编辑请求
            data = {
                "messages": [{
                    "content": json.dumps({
                        "text": edit_prompt,
                        "edit_image": {
                            "edit_image_url": image_url,
                            "edit_image_token": image_token,
                            "description": "",
                            "outline_id": None
                        }
                    }),
                    "content_type": 2009,
                    "attachments": []
                }],
                "completion_option": {
                    "is_regen": False,
                    "with_suggest": False,
                    "need_create_conversation": False,
                    "launch_stage": 1,
                    "is_replace": False,
                    "is_delete": False,
                    "message_from": 0,
                    "event_id": "0"
                },
                "section_id": section_id,
                "conversation_id": conversation_id,
                "local_message_id": str(uuid.uuid1()),
                "reply_id": reply_id
            }
            
            result = self.send_request(data, "/samantha/chat/completion")
            if result and "urls" in result:
                return result["urls"]
            return None
        except Exception as e:
            logger.error(f"[Doubao] Error in edit_image: {e}")
            return None

    def outpaint_image(self, image_url, ratio, conversation_id=None, section_id=None, reply_id=None):
        """扩展图片"""
        try:
            # 计算扩展比例
            expand_ratio = 0.3888889  # 7/18，用于将1:1扩展到16:9
            max_expand = 0.5  # 最大扩展比例
            
            # 根据不同比例设置扩展参数
            if ratio == "16:9":
                left = right = expand_ratio
                top = bottom = 0
            elif ratio == "9:16":
                left = right = 0
                top = bottom = expand_ratio
            elif ratio == "4:3":
                left = right = 0.166667  # 1/6
                top = bottom = 0
            elif ratio == "1:1":
                left = right = top = bottom = 0
            elif ratio == "max":
                left = right = top = bottom = max_expand
            else:
                return None
            
            data = {
                "messages": [{
                    "content": json.dumps({
                        "text": "按新尺寸生成图片",
                        "edit_image": {
                            "edit_image_url": image_url,
                            "edit_image_token": image_url.split("/")[-1].split("~")[0],
                            "description": "扩展图片",
                            "ability": "outpainting",
                            "top": top,
                            "bottom": bottom,
                            "left": left,
                            "right": right,
                            "is_edit_local_image": False,
                            "is_edit_local_image_v2": "false"
                        }
                    }),
                    "content_type": 2009,
                    "attachments": []
                }],
                "completion_option": {
                    "is_regen": False,
                    "with_suggest": False,
                    "need_create_conversation": not bool(conversation_id),
                    "launch_stage": 1,
                    "is_replace": False,
                    "is_delete": False,
                    "message_from": 0,
                    "event_id": "0"
                }
            }
            
            if conversation_id:
                data["conversation_id"] = conversation_id
                data["section_id"] = section_id
                data["local_message_id"] = str(uuid.uuid1())
                if reply_id:
                    data["reply_id"] = reply_id
            
            result = self.send_request(data, "/samantha/chat/completion")
            return result["urls"] if result else None
            
        except Exception as e:
            logger.error(f"[Doubao] Error outpainting image: {e}")
            return None

    def send_heartbeat(self):
        """发送心跳请求以保持账号活跃
        Returns:
            bool: 心跳是否成功
        """
        try:
            url = f"{self.base_url}/ttwid/check/"
            
            # 获取headers
            headers = {
                'accept': 'application/json, text/plain, */*',
                'accept-language': 'zh-CN,zh;q=0.9',
                'content-type': 'application/json',
                'cookie': self.config.get("auth", {}).get("cookie", ""),
                'origin': 'https://www.doubao.com',
                'referer': 'https://www.doubao.com/chat/',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
            }
            
            # 准备请求数据
            data = {
                "aid": 497858,
                "service": "www.doubao.com",
                "host": "",
                "unionHost": "",
                "union": False,
                "needFid": False,
                "fid": "",
                "migrate_priority": 0
            }
            
            # 发送请求
            response = self.session.post(
                url,
                headers=headers,
                json=data,
                timeout=10
            )
            
            # 检查响应
            response.raise_for_status()
            result = response.json()
            
            if result.get("status_code") == 0 and result.get("sub_status_code") == 2001:
                logger.info("[Doubao] 心跳请求成功")
                return True
            else:
                logger.warning(f"[Doubao] 心跳请求返回异常: {result}")
                return False
            
        except Exception as e:
            logger.error(f"[Doubao] 心跳请求失败: {e}")
            return False