import requests
import logging
import threading
import time
import os
import json
from common.log import logger

class TokenManager:
    def __init__(self, config):
        self.config = config
        self.config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
        self._lock = threading.Lock()
        self._last_refresh_time = 0
        self._refresh_interval = 1800  # 30分钟刷新一次
        self._last_heartbeat_time = 0
        self._heartbeat_interval = 300  # 5分钟发送一次心跳
        self.token = None
        self.token_expiry = 0
        self._last_session_refresh_time = 0
        self._session_refresh_interval = 7200  # 2小时刷新一次会话
        self._load_token()

    def _load_token(self):
        """从配置加载token"""
        try:
            self.token = self.config.get("token", "")
        except Exception as e:
            logger.error(f"[Doubao] Failed to load token: {e}")
            self.token = None

    def get_token(self):
        """获取当前token"""
        return self.token

    def update_token(self, new_token):
        """更新token"""
        self.token = new_token
        self.token_expiry = time.time() + 3600  # token有效期1小时

    def is_token_valid(self):
        """检查token是否有效"""
        return bool(self.token) and time.time() < self.token_expiry

    def get_headers(self):
        """获取请求头"""
        auth = self.config.get('auth', {})
        return {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
            'cache-control': 'no-cache',
            'content-type': 'application/json',
            'cookie': auth.get('cookie', ''),
            'origin': 'https://www.doubao.com',
            'pragma': 'no-cache',
            'priority': 'u=1, i',
            'referer': 'https://www.doubao.com/chat/create-image',
            'sec-ch-ua': '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0',
            'msToken': auth.get('msToken', ''),
            'x-bogus': auth.get('a_bogus', '')
        }

    def get_cookies(self):
        """获取cookies"""
        auth = self.config.get('auth', {})
        return auth.get('cookie', '')

    def get_request_params(self):
        """获取请求参数"""
        auth = self.config.get('auth', {})
        return {
            'version_code': '20800',
            'language': 'zh',
            'device_platform': 'web',
            'aid': '497858',
            'real_aid': '497858',
            'pc_version': '1.51.81',
            'pkg_type': 'release_version',
            'device_id': '7460980997308483113',
            'web_id': '7460981012103120435',
            'tea_uuid': '7460981012103120435',
            'use-olympus-account': '1',
            'region': 'CN',
            'sys_region': 'CN',
            'samantha_web': '1',
            'msToken': auth.get('msToken', ''),
            'a_bogus': auth.get('a_bogus', '')
        }

    def refresh_token(self):
        """刷新token"""
        with self._lock:
            current_time = time.time()
            if current_time - self._last_refresh_time < self._refresh_interval:
                return

            try:
                headers = self.get_headers()
                params = self.get_request_params()
                
                response = requests.get(
                    "https://www.doubao.com/chat/create-image",
                    headers=headers,
                    params=params
                )
                response.raise_for_status()

                # 更新最后刷新时间
                self._last_refresh_time = current_time
                logger.info("[Doubao] Token refreshed successfully")

            except Exception as e:
                logger.error(f"[Doubao] Failed to refresh token: {e}")
                raise

    def save_config(self):
        """保存配置到文件"""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            logger.error(f"[Doubao] 保存配置失败: {e}")
            return False
            
    def update_tokens(self, msToken=None, a_bogus=None):
        """更新认证信息
        Args:
            msToken: 新的msToken
            a_bogus: 新的a_bogus
        Returns:
            bool: 是否更新成功
        """
        try:
            auth = self.config.get("auth", {})
            if msToken:
                auth["msToken"] = msToken
            if a_bogus:
                auth["a_bogus"] = a_bogus
            self.config["auth"] = auth
            return self.save_config()
        except Exception as e:
            logger.error(f"[Doubao] 更新认证信息失败: {e}")
            return False
            
    def get_tokens(self):
        """获取当前的认证信息
        Returns:
            dict: 认证信息
        """
        return self.config.get("auth", {})

    def check_heartbeat(self, api_client):
        """检查是否需要发送心跳并执行
        Args:
            api_client: API客户端实例
        Returns:
            bool: 是否成功执行心跳
        """
        with self._lock:
            current_time = time.time()
            if current_time - self._last_heartbeat_time < self._heartbeat_interval:
                return True  # 不需要发送心跳

            # 发送心跳
            success = api_client.send_heartbeat()
            if success:
                self._last_heartbeat_time = current_time
            return success

    def check_session_refresh(self, api_client):
        """检查是否需要刷新会话信息
        Args:
            api_client: API客户端实例
        Returns:
            bool: 是否成功刷新会话
        """
        with self._lock:
            current_time = time.time()
            if current_time - self._last_session_refresh_time < self._session_refresh_interval:
                return True  # 不需要刷新会话
            
            # 刷新会话
            success = api_client.init_session()
            if success:
                self._last_session_refresh_time = current_time
            return success