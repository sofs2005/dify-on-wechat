import numpy as np
import cv2
import json
import os
import time
import uuid
import base64
import requests
import threading
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from plugins import Plugin, Event, EventAction, EventContext, register
from common.log import logger
from .module.token_manager import TokenManager
from .module.api_client import ApiClient
from .module.image_storage import ImageStorage
from .module.image_processor import ImageProcessor
from .module.image_uploader import ImageUploader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

@register(
    name="Doubao",
    desc="豆包AI绘画插件",
    version="1.0",
    author="lanvent",
    desire_priority=0
)
class DoubaoPlugin(Plugin):
    def __init__(self):
        super().__init__()
        try:
            # 加载配置
            self.config = self._load_config()
            if not self.config:
                raise Exception("Failed to load config")

            # 获取数据保留天数配置
            retention_days = self.config.get("storage", {}).get("retention_days", 7)

            # 初始化存储路径
            storage_dir = os.path.join(os.path.dirname(__file__), "storage")
            if not os.path.exists(storage_dir):
                os.makedirs(storage_dir)

            temp_dir = os.path.join(os.path.dirname(__file__), "temp")
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)

            # 初始化各个模块
            self.image_storage = ImageStorage(
                os.path.join(storage_dir, "images.db"),
                retention_days=retention_days
            )

            # 初始化token管理器和API客户端
            self.token_manager = TokenManager(self.config)
            self.api_client = ApiClient(self.token_manager)

            # 初始化图片处理器和上传器
            self.image_uploader = ImageUploader(self.config)
            self.image_processor = ImageProcessor(temp_dir, self.image_uploader)

            # 从配置文件加载支持的风格列表
            self.styles = self.config.get("styles", [])

            # 初始化会话信息
            self.conversation_id = None
            self.section_id = None
            self.reply_id = None

            # 从数据库恢复上次会话信息
            self._init_conversation_from_storage()

            # 初始化参考图功能相关变量
            self.waiting_for_reference = {}
            self.reference_prompts = {}

            # 初始化区域重绘相关变量
            self.waiting_for_inpaint = {}
            self.inpaint_prompts = {}
            self.inpaint_images = {}

            # 注册事件处理器
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context

            logger.info(f"[Doubao] plugin initialized with {retention_days} days data retention")

            # 在DoubaoPlugin类中添加定时心跳检查
            self._heartbeat_thread = None
            self._stop_heartbeat = False
            self._start_heartbeat_thread()

        except Exception as e:
            logger.error(f"[Doubao] Failed to initialize plugin: {e}")
            raise e

    def _load_config(self):
        """加载配置文件"""
        try:
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
            with open(config_path, "r", encoding='utf-8') as f:
                config = json.load(f)
                return config
        except Exception as e:
            logger.error(f"[Doubao] Failed to load config: {e}")
            return {}

    def get_help_text(self, **kwargs):
        help_text = "豆包AI绘画插件\n"
        help_text += "使用:\n"

        # 获取命令配置
        commands = self.config.get('commands', [])

        # 找到draw命令的别名
        draw_command = "豆包"  # 默认值
        for cmd in commands:
            if cmd.get("name") == "draw" and cmd.get("aliases"):
                draw_command = cmd["aliases"][0]
                break

        help_text += f"{draw_command} : 新建会话\n"
        help_text += f"{draw_command}新建会话 : 强制新建会话\n"
        help_text += f"{draw_command} [提示词] [-风格] [-比例]: 生成图片\n"
        help_text += "支持的风格: " + ", ".join(self.styles) + "\n"
        help_text += "支持的比例: " + ", ".join(self.config.get("params", {}).get("ratios", ["4:3"])) + "\n"
        help_text += "\n图片操作指令:\n"

        # 找到各个命令的别名
        reference_cmd = "参考图"
        koutu_cmd = "抠图"
        inpaint_cmd = "重绘"
        change_bg_cmd = "豆包换背景"
        change_subj_cmd = "豆包换主体"
        enlarge_cmd = "$u"
        edit_cmd = "$v"
        outpaint_cmd = "$k"
        regenerate_cmd = "$r"

        # 从配置中获取命令别名
        for cmd in commands:
            if cmd.get("name") == "reference" and cmd.get("aliases"):
                reference_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "koutu" and cmd.get("aliases"):
                koutu_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "inpaint" and cmd.get("aliases"):
                inpaint_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "change_background" and cmd.get("aliases"):
                change_bg_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "change_subject" and cmd.get("aliases"):
                change_subj_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "enlarge" and cmd.get("aliases"):
                enlarge_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "edit" and cmd.get("aliases"):
                edit_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "outpaint" and cmd.get("aliases"):
                outpaint_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "regenerate" and cmd.get("aliases"):
                regenerate_cmd = cmd["aliases"][0]

        help_text += f"{reference_cmd} [提示词] [-风格] [-比例]: 使用参考图生成图片\n"
        help_text += f"{koutu_cmd}: 抠出图片主体\n"
        help_text += f"{inpaint_cmd} [描述词]: 区域重绘图片\n"
        help_text += f"{change_bg_cmd} [背景描述词]: 自动识别主体并替换背景\n"
        help_text += f"{change_subj_cmd} [主体描述词]: 自动识别主体并替换主体\n"
        help_text += "\n图片编辑指令:\n"
        help_text += f"放大: {enlarge_cmd} 图片ID 序号(1-4)\n"
        help_text += f"编辑: {edit_cmd} 图片ID 序号(1-4) 编辑提示词  # 首次编辑需要序号\n"
        help_text += f"     {edit_cmd} 图片ID 编辑提示词  # 二次编辑直接编辑\n"
        help_text += f"扩图: {outpaint_cmd} 图片ID 序号(1-4) 比例(1:1/4:3/16:9/9:16/max)  # 首次扩图需要序号\n"
        help_text += f"     {outpaint_cmd} 图片ID 比例  # 二次扩图直接扩图\n"
        help_text += f"重新生成: {regenerate_cmd} 图片ID\n"
        return help_text

    def _create_new_conversation(self):
        """创建新的图像生成会话"""
        try:
            # 构建图像生成会话请求数据
            data = {
                "skill_type": 3,
                "condition": {
                    "image_condition": {
                        "category_id": 0
                    }
                }
            }

            result = self.api_client.send_request(data, "/samantha/skill/pack")
            if result and "data" in result and "image" in result["data"]:
                # 获取图像生成相关的会话信息
                self.styles = []
                if "meta" in result["data"]["image"]:
                    category_list = result["data"]["image"]["meta"].get("category_list", [])
                    self.styles = [category["category_name"] for category in category_list]
                    logger.info(f"[Doubao] Loaded {len(self.styles)} styles from API")

                # 获取历史会话列表
                history_data = {
                    "request_list": [{
                        "conversation_id": "0"
                    }]
                }
                history_result = self.api_client.send_request(history_data, "/alice/conversation/latest_messagelist")
                if history_result and "data" in history_result and "message_map" in history_result["data"]:
                    # 找到最新的图像生成会话
                    for conv_id, messages in history_result["data"]["message_map"].items():
                        if messages and len(messages) > 0:
                            self.conversation_id = conv_id
                            self.section_id = messages[0].get("section_id")
                            break

                return True
            return False
        except Exception as e:
            logger.error(f"[Doubao] Error creating new image conversation: {e}")
            return False

    def _parse_style_and_ratio(self, content: str, draw_command: str) -> tuple:
        """解析绘画指令中的风格和比例参数
        Args:
            content: 完整的指令内容
            draw_command: 绘画命令前缀
        Returns:
            tuple: (prompt, style, ratio)
        """
        # 移除命令前缀
        prompt = content[len(draw_command):].strip()
        style = None
        ratio = self.config.get("params", {}).get("default_ratio", "4:3")
        supported_ratios = self.config.get("params", {}).get("ratios", ["1:1", "2:3", "4:3", "9:16", "16:9"])

        # 1. 处理自然语言描述格式
        if "图风格为" in prompt:
            parts = prompt.split("图风格为")
            if len(parts) == 2:
                prompt = parts[0].strip()
                style_part = parts[1].strip()
                # 提取风格
                if "「" in style_part and "」" in style_part:
                    style = style_part[style_part.find("「")+1:style_part.find("」")]
                else:
                    for s in self.styles:
                        if style_part.startswith(s):
                            style = s
                            break

        # 2. 处理比例描述
        if "比例" in prompt:
            parts = prompt.split("比例")
            if len(parts) == 2:
                prompt = parts[0].strip()
                ratio_part = parts[1].strip()
                # 提取比例
                if "「" in ratio_part and "」" in ratio_part:
                    ratio = ratio_part[ratio_part.find("「")+1:ratio_part.find("」")]
                else:
                    for r in supported_ratios:
                        if ratio_part.startswith(r):
                            ratio = r
                            break

        # 3. 处理分隔符格式 (-或空格或逗号)
        if not style or not ratio:
            # 将中文冒号替换为英文冒号
            prompt = prompt.replace("：", ":")

            # 分割所有可能的分隔符
            parts = []
            for sep in ["-", " ", ","]:
                if sep in prompt:
                    parts.extend([p.strip() for p in prompt.split(sep) if p.strip()])

            if parts:
                # 最后两个部分可能是风格和比例
                last_parts = parts[-2:]
                prompt = " ".join(parts[:-2]) if len(parts) > 2 else parts[0]

                for part in last_parts:
                    # 检查是否是比例格式
                    if ":" in part and part in supported_ratios:
                        ratio = part
                    # 检查是否是支持的风格
                    elif part in self.styles:
                        style = part

        return prompt.strip(), style, ratio.replace("：", ":")

    def on_handle_context(self, e_context: EventContext):
        if e_context["context"].type != ContextType.TEXT and e_context["context"].type != ContextType.IMAGE:
            return

        content = e_context["context"].content
        msg = e_context["context"]["msg"]

        # 获取命令配置
        commands = self.config.get('commands', [])

        # 找到draw命令的别名
        draw_command = "豆包"  # 默认值
        for cmd in commands:
            if cmd.get("name") == "draw" and cmd.get("aliases"):
                draw_command = cmd["aliases"][0]
                break

        # 找到各个命令的别名
        reference_cmd = "参考图"
        koutu_cmd = "抠图"
        inpaint_cmds = ["重绘", "圈选", "涂抹"]
        change_bg_cmd = "豆包换背景"
        change_subj_cmd = "豆包换主体"
        enlarge_cmd = "$u"
        edit_cmd = "$v"
        outpaint_cmd = "$k"
        regenerate_cmd = "$r"

        # 从配置中获取命令别名
        for cmd in commands:
            if cmd.get("name") == "reference" and cmd.get("aliases"):
                reference_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "koutu" and cmd.get("aliases"):
                koutu_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "inpaint" and cmd.get("aliases"):
                inpaint_cmds = cmd["aliases"]
            elif cmd.get("name") == "change_background" and cmd.get("aliases"):
                change_bg_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "change_subject" and cmd.get("aliases"):
                change_subj_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "enlarge" and cmd.get("aliases"):
                enlarge_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "edit" and cmd.get("aliases"):
                edit_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "outpaint" and cmd.get("aliases"):
                outpaint_cmd = cmd["aliases"][0]
            elif cmd.get("name") == "regenerate" and cmd.get("aliases"):
                regenerate_cmd = cmd["aliases"][0]

        # 处理换背景命令
        if e_context["context"].type == ContextType.TEXT and content.startswith(change_bg_cmd):
            # 提取背景描述词
            background_prompt = content[len(change_bg_cmd):].strip()
            if not background_prompt:
                e_context["reply"] = Reply(ReplyType.ERROR, f"请在命令后添加背景描述词，如：{change_bg_cmd} 蓝天白云")
                e_context.action = EventAction.BREAK_PASS
                return

            # 记录用户ID和等待状态
            self.waiting_for_reference[msg.from_user_id] = True
            self.reference_prompts[msg.from_user_id] = {
                "type": "change_background",
                "prompt": background_prompt
            }
            e_context["reply"] = Reply(ReplyType.TEXT, "请发送需要更换背景的图片")
            e_context.action = EventAction.BREAK_PASS
            return

        # 处理换主体命令
        if e_context["context"].type == ContextType.TEXT and content.startswith(change_subj_cmd):
            # 提取主体描述词
            subject_prompt = content[len(change_subj_cmd):].strip()
            if not subject_prompt:
                e_context["reply"] = Reply(ReplyType.ERROR, f"请在命令后添加主体描述词，如：{change_subj_cmd} 一个帅气的男人")
                e_context.action = EventAction.BREAK_PASS
                return

            # 记录用户ID和等待状态
            self.waiting_for_reference[msg.from_user_id] = True
            self.reference_prompts[msg.from_user_id] = {
                "type": "change_subject",
                "prompt": subject_prompt
            }
            e_context["reply"] = Reply(ReplyType.TEXT, "请发送需要更换主体的图片")
            e_context.action = EventAction.BREAK_PASS
            return

        # 处理抠图命令
        if e_context["context"].type == ContextType.TEXT and content == koutu_cmd:
            # 记录用户ID和等待状态
            self.waiting_for_reference[msg.from_user_id] = True
            self.reference_prompts[msg.from_user_id] = {"type": "koutu"}
            e_context["reply"] = Reply(ReplyType.TEXT, "请发送需要抠图的图片")
            e_context.action = EventAction.BREAK_PASS
            return

        # 处理参考图命令
        if e_context["context"].type == ContextType.TEXT and content.startswith(reference_cmd):
            # 解析完整命令
            command_text = content[len(reference_cmd):].strip()  # 移除参考图前缀
            if not command_text:
                e_context["reply"] = Reply(ReplyType.ERROR, f"请在{reference_cmd}后添加提示词，如：{reference_cmd} 加个墨镜 人像摄影 4:3")
                e_context.action = EventAction.BREAK_PASS
                return

            # 使用现有的解析方法解析提示词、风格和比例
            prompt, style, ratio = self._parse_style_and_ratio(command_text, "")

            if not prompt:
                e_context["reply"] = Reply(ReplyType.ERROR, "请提供有效的编辑提示词")
                e_context.action = EventAction.BREAK_PASS
                return

            # 记录用户ID和完整参数
            self.waiting_for_reference[msg.from_user_id] = True
            self.reference_prompts[msg.from_user_id] = {
                "prompt": prompt,
                "style": style,
                "ratio": ratio
            }
            e_context["reply"] = Reply(ReplyType.TEXT, "请发送需要编辑的参考图片")
            e_context.action = EventAction.BREAK_PASS
            return

        # 处理参考图片上传
        if e_context["context"].type == ContextType.IMAGE and msg.from_user_id in self.waiting_for_reference:
            try:
                # 获取图片数据
                logger.info("[Doubao] 开始获取图片数据...")
                image_data = self._get_image_data(msg, content)
                if not image_data:
                    logger.error("[Doubao] 获取图片数据失败")
                    e_context["reply"] = Reply(ReplyType.ERROR, "获取图片数据失败，请重试")
                    return

                # 根据类型处理图片
                params = self.reference_prompts.get(msg.from_user_id, {})
                if params.get("type") == "koutu":
                    # 处理抠图
                    self._process_koutu(image_data, msg, e_context)
                elif params.get("type") == "change_background":
                    # 处理换背景
                    self._process_change_background(image_data, msg, e_context)
                elif params.get("type") == "change_subject":
                    # 处理换主体
                    self._process_change_subject(image_data, msg, e_context)
                else:
                    # 处理参考图
                    self._process_image(image_data, msg, e_context)

            except Exception as e:
                logger.error(f"[Doubao] 处理图片时发生错误: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, f"处理图片时发生错误，请重试")

            finally:
                # 清除等待状态
                if msg.from_user_id in self.waiting_for_reference:
                    del self.waiting_for_reference[msg.from_user_id]
                if msg.from_user_id in self.reference_prompts:
                    del self.reference_prompts[msg.from_user_id]

            e_context.action = EventAction.BREAK_PASS
            return

        # 处理区域重绘命令
        if e_context["context"].type == ContextType.TEXT and any(content.startswith(cmd) for cmd in inpaint_cmds):
            # 提取命令类型和描述词
            used_cmd = None
            for cmd in inpaint_cmds:
                if content.startswith(cmd):
                    used_cmd = cmd
                    break

            if used_cmd == "圈选":
                mode = "circle"
                prompt = content[len(used_cmd):].strip()
            elif used_cmd == "涂抹":
                mode = "brush"
                prompt = content[len(used_cmd):].strip()
            else:  # 默认使用圈选模式
                mode = "circle"
                prompt = content[len(used_cmd):].strip()

            # 检查是否是反选命令
            is_invert = False
            if prompt.startswith("反选"):
                is_invert = True
                prompt = prompt[2:].strip()  # 移除"反选"前缀
            elif "反选" in prompt:
                is_invert = True
                prompt = prompt.replace("反选", "").strip()

            if not prompt:
                if mode == "circle":
                    circle_cmd = next((cmd for cmd in inpaint_cmds if cmd == "圈选"), "圈选")
                    e_context["reply"] = Reply(ReplyType.ERROR, f"请在{circle_cmd}后添加描述词，如：{circle_cmd} 添加墨镜")
                elif mode == "brush":
                    brush_cmd = next((cmd for cmd in inpaint_cmds if cmd == "涂抹"), "涂抹")
                    e_context["reply"] = Reply(ReplyType.ERROR, f"请在{brush_cmd}后添加描述词，如：{brush_cmd} 添加墨镜")
                else:
                    inpaint_cmd = inpaint_cmds[0] if inpaint_cmds else "重绘"
                    e_context["reply"] = Reply(ReplyType.ERROR, f"请在{inpaint_cmd}后添加描述词，如：{inpaint_cmd} 添加墨镜")
                e_context.action = EventAction.BREAK_PASS
                return

            # 记录用户ID和等待状态
            self.waiting_for_inpaint[msg.from_user_id] = True
            self.inpaint_prompts[msg.from_user_id] = {"prompt": prompt, "mode": mode, "is_invert": is_invert}
            e_context["reply"] = Reply(ReplyType.TEXT, "请发送需要重绘的原图")
            e_context.action = EventAction.BREAK_PASS
            return

        # 处理区域重绘的图片上传
        if e_context["context"].type == ContextType.IMAGE and msg.from_user_id in self.waiting_for_inpaint:
            try:
                # 获取图片数据
                image_data = self._get_image_data(msg, content)
                if not image_data:
                    e_context["reply"] = Reply(ReplyType.ERROR, "获取图片数据失败，请重试")
                    return

                if msg.from_user_id not in self.inpaint_images:
                    # 第一次上传，保存原图
                    self.inpaint_images[msg.from_user_id] = {"original": image_data}
                    # 根据模式显示不同的提示信息
                    mode = self.inpaint_prompts[msg.from_user_id].get("mode", "circle")
                    if mode == "circle":
                        e_context["reply"] = Reply(ReplyType.TEXT, "请发送在需要重绘区域画圈的图片")
                    else:  # brush mode
                        e_context["reply"] = Reply(ReplyType.TEXT, "请发送用红色涂抹需要重绘区域的图片")
                else:
                    try:
                        # 第二次上传，处理重绘
                        original_image = self.inpaint_images[msg.from_user_id]["original"]
                        prompt = self.inpaint_prompts[msg.from_user_id]["prompt"]
                        mode = self.inpaint_prompts[msg.from_user_id]["mode"]
                        is_invert = self.inpaint_prompts[msg.from_user_id]["is_invert"]

                        # 处理区域重绘
                        self._process_inpaint(original_image, image_data, prompt, msg, e_context)
                    except Exception as e:
                        logger.error(f"[Doubao] Error processing inpaint image: {e}")
                        e_context["reply"] = Reply(ReplyType.ERROR, "处理图片失败，请重试")
                    finally:
                        # 清理状态
                        if msg.from_user_id in self.waiting_for_inpaint:
                            del self.waiting_for_inpaint[msg.from_user_id]
                        if msg.from_user_id in self.inpaint_prompts:
                            del self.inpaint_prompts[msg.from_user_id]
                        if msg.from_user_id in self.inpaint_images:
                            del self.inpaint_images[msg.from_user_id]

                e_context.action = EventAction.BREAK_PASS
                return

            except Exception as e:
                logger.error(f"[Doubao] Error processing inpaint image: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, "处理图片失败，请重试")

                # 清理状态
                if msg.from_user_id in self.waiting_for_inpaint:
                    del self.waiting_for_inpaint[msg.from_user_id]
                if msg.from_user_id in self.inpaint_prompts:
                    del self.inpaint_prompts[msg.from_user_id]

                e_context.action = EventAction.BREAK_PASS
                return

        # 处理其他命令
        if e_context["context"].type == ContextType.TEXT:
            # 处理图片操作命令
            if content.startswith("$"):
                cmd_parts = content.split()
                if len(cmd_parts) < 2:
                    e_context["reply"] = Reply(ReplyType.ERROR, "命令格式错误")
                    e_context.action = EventAction.BREAK_PASS
                    return

                cmd = cmd_parts[0][1:]  # 去掉$前缀
                img_id = cmd_parts[1]

                try:
                    if cmd == enlarge_cmd[1:] and len(cmd_parts) == 3:  # 放大命令
                        index = int(cmd_parts[2])
                        is_valid, error_msg = self.image_storage.validate_image_index(img_id, index)
                        if not is_valid:
                            e_context["reply"] = Reply(ReplyType.ERROR, error_msg)
                        else:
                            image_data = self.image_storage.get_image(img_id)
                            image_url = image_data["urls"][index - 1]
                            e_context["reply"] = Reply(ReplyType.IMAGE_URL, image_url)

                    elif cmd == edit_cmd[1:]:  # 编辑命令
                        if len(cmd_parts) < 3:
                            e_context["reply"] = Reply(ReplyType.ERROR, f"编辑命令格式：\n首次编辑：{edit_cmd} 图片ID 序号(1-4) 编辑提示词\n二次编辑：{edit_cmd} 图片ID 编辑提示词")
                            e_context.action = EventAction.BREAK_PASS
                            return

                        image_data = self.image_storage.get_image(img_id)
                        if not image_data:
                            e_context["reply"] = Reply(ReplyType.ERROR, "找不到对应的图片ID")
                            e_context.action = EventAction.BREAK_PASS
                            return

                        # 判断是否是首次编辑（原始图片有多张）
                        is_first_edit = len(image_data["urls"]) > 1

                        # 解析编辑参数
                        try:
                            if is_first_edit:
                                if len(cmd_parts) < 4:
                                    e_context["reply"] = Reply(ReplyType.ERROR, f"首次编辑需要提供序号：{edit_cmd} 图片ID 序号(1-4) 编辑提示词")
                                    e_context.action = EventAction.BREAK_PASS
                                    return

                                index = int(cmd_parts[2])
                                edit_prompt = " ".join(cmd_parts[3:])

                                # 验证序号
                                is_valid, error_msg = self.image_storage.validate_image_index(img_id, index)
                                if not is_valid:
                                    e_context["reply"] = Reply(ReplyType.ERROR, error_msg)
                                    e_context.action = EventAction.BREAK_PASS
                                    return
                            else:
                                index = 1
                                edit_prompt = " ".join(cmd_parts[2:])

                            # 获取指定序号的图片URL和生成对应的token
                            image_url = image_data["urls"][index - 1]
                            image_token = image_url.split("/")[-1].split("~")[0]

                            description = image_data.get("operation_params", {}).get("description", "")
                            conversation_id = image_data.get("operation_params", {}).get("conversation_id")
                            section_id = image_data.get("operation_params", {}).get("section_id")
                            reply_id = image_data.get("operation_params", {}).get("reply_id")

                            if not all([image_token, image_url, conversation_id, section_id]):
                                e_context["reply"] = Reply(ReplyType.ERROR, "缺少必要的图片信息，无法编辑")
                                e_context.action = EventAction.BREAK_PASS
                                return

                            # 发送等待消息
                            e_context["channel"].send(Reply(ReplyType.INFO, f"正在编辑第 {index} 张图片..."), e_context["context"])

                            # 构建编辑请求
                            data = {
                                "messages": [{
                                    "content": {
                                        "text": edit_prompt,
                                        "edit_image": {
                                            "edit_image_url": image_url,
                                            "edit_image_token": image_token,
                                            "description": description,
                                            "outline_id": None
                                        }
                                    },
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
                                "local_message_id": str(uuid.uuid1())
                            }

                            # 发送重绘请求前记录完整请求参数
                            logger.info(f"[Doubao] 完整请求参数:\n请求URL: /samantha/chat/completion\n请求体: {json.dumps(data, ensure_ascii=False, indent=2)}")

                            # 保存请求参数到temp目录
                            temp_dir = os.path.join(os.path.dirname(__file__), "temp")
                            if not os.path.exists(temp_dir):
                                os.makedirs(temp_dir)
                            request_log_path = os.path.join(temp_dir, "@request_debug.json")
                            with open(request_log_path, "w", encoding="utf-8") as f:
                                json.dump({
                                    "url": "/samantha/chat/completion",
                                    "headers": self.api_client.headers,
                                    "params": self.api_client._get_params(),
                                    "data": data
                                }, f, ensure_ascii=False, indent=2)
                            logger.info(f"[Doubao] 已保存请求参数到: {request_log_path}")

                            # 发送编辑请求
                            result = self.api_client.send_request(data, "/samantha/chat/completion")
                            if result and "urls" in result:
                                # 存储编辑后的图片
                                new_img_id = str(int(time.time()))
                                operation_params = {
                                    "prompt": edit_prompt,
                                    "conversation_id": conversation_id,
                                    "section_id": section_id,
                                    "reply_id": result.get("reply_id"),
                                    "original_img_id": img_id,
                                    "original_index": index,
                                    "image_token": result["urls"][0].split("/")[-1].split("~")[0],  # 保存新图片token
                                    "image_url": result["urls"][0],  # 保存新图片url
                                    "description": description,
                                    "data": result.get("data", [])  # 保存完整的图片数据
                                }

                                # 存储图片信息
                                image_info = {
                                    "urls": result["urls"],
                                    "type": "edit",
                                    "operation_params": operation_params,
                                    "parent_id": img_id,
                                    "create_time": int(time.time())
                                }

                                # 保存到数据库
                                self.image_storage.store_image(new_img_id, image_info)

                                # 发送编辑后的图片
                                e_context["reply"] = Reply(ReplyType.IMAGE_URL, result["urls"][0])

                                # 等待图片发送完成后再发送帮助信息
                                time.sleep(1)  # 等待图片发送完成
                                help_text = self._get_help_text(new_img_id, False)
                                e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
                            else:
                                e_context["reply"] = Reply(ReplyType.ERROR, "图片编辑失败")

                        except ValueError as ve:
                            e_context["reply"] = Reply(ReplyType.ERROR, "图片序号必须是1-4之间的数字")
                        except Exception as e:
                            logger.error(f"[Doubao] Error in edit_image: {e}")
                            e_context["reply"] = Reply(ReplyType.ERROR, "图片编辑失败")

                    elif cmd == outpaint_cmd[1:]:  # 扩图命令
                        if len(cmd_parts) < 3:
                            e_context["reply"] = Reply(ReplyType.ERROR, f"扩图命令格式：\n首次扩图：{outpaint_cmd} 图片ID 序号(1-4) 比例\n二次扩图：{outpaint_cmd} 图片ID 比例")
                            e_context.action = EventAction.BREAK_PASS
                            return

                        image_data = self.image_storage.get_image(img_id)
                        if not image_data:
                            e_context["reply"] = Reply(ReplyType.ERROR, "找不到对应的图片ID")
                            e_context.action = EventAction.BREAK_PASS
                            return

                        is_first_outpaint = len(image_data["urls"]) > 1

                        if is_first_outpaint:
                            if len(cmd_parts) < 4:
                                e_context["reply"] = Reply(ReplyType.ERROR, f"首次扩图需要提供序号：{outpaint_cmd} 图片ID 序号(1-4) 比例")
                                e_context.action = EventAction.BREAK_PASS
                                return

                            index = int(cmd_parts[2])
                            ratio = cmd_parts[3].replace("：", ":")
                        else:
                            index = 1
                            ratio = cmd_parts[2].replace("：", ":")

                        is_valid, error_msg = self.image_storage.validate_image_index(img_id, index)
                        if not is_valid:
                            e_context["reply"] = Reply(ReplyType.ERROR, error_msg)
                            e_context.action = EventAction.BREAK_PASS
                            return

                        # 获取要扩图的图片URL和token
                        image_url = image_data["urls"][index - 1]
                        image_token = image_url.split("/")[-1].split("~")[0]
                        description = image_data.get("operation_params", {}).get("description", "")
                        conversation_id = image_data.get("operation_params", {}).get("conversation_id")
                        section_id = image_data.get("operation_params", {}).get("section_id")
                        reply_id = image_data.get("operation_params", {}).get("reply_id")

                        e_context["channel"].send(Reply(ReplyType.INFO, f"正在将第 {index} 张图片扩展为 {ratio} 比例..."), e_context["context"])

                        try:
                            # 获取原始会话信息和图片参数
                            params = image_data.get("operation_params", {})

                            # 根据序号获取对应的图片URL和token
                            image_url = image_data["urls"][index - 1]
                            image_token = image_url.split("/")[-1].split("~")[0]

                            description = params.get("description", "")
                            conversation_id = params.get("conversation_id")
                            section_id = params.get("section_id")
                            reply_id = params.get("reply_id")

                            if not all([image_token, image_url, conversation_id, section_id]):
                                e_context["reply"] = Reply(ReplyType.ERROR, "缺少必要的图片信息，无法扩展")
                                e_context.action = EventAction.BREAK_PASS
                                return

                            # 获取原始图片尺寸
                            original_width = int(params.get("width", 1024))
                            original_height = int(params.get("height", 1024))

                            # 根据比例和原始尺寸计算扩图参数
                            if ratio == "1:1":
                                # 扩展到正方形
                                expand_ratio = 0.16666667
                                top = bottom = left = right = expand_ratio
                            elif ratio == "2:3":
                                # 扩展到竖向2:3
                                height_ratio = (original_width * 1.5 - original_height) / original_height
                                top = bottom = height_ratio / 2
                                left = right = 0
                            elif ratio == "4:3":
                                # 扩展到横向4:3
                                width_ratio = (original_height * 1.33333 - original_width) / original_width
                                left = right = width_ratio / 2
                                top = bottom = 0
                            elif ratio == "16:9":
                                # 扩展到横向16:9
                                width_ratio = (original_height * 1.77778 - original_width) / original_width
                                left = right = width_ratio / 2
                                top = bottom = 0
                            elif ratio == "9:16":
                                # 扩展到竖向9:16
                                height_ratio = (original_width * 1.77778 - original_height) / original_height
                                top = bottom = height_ratio / 2
                                left = right = 0
                            elif ratio == "max":
                                # 扩展到最大尺寸
                                height_ratio = (4096 - original_height) / original_height
                                width_ratio = (2048 - original_width) / original_width
                                top = bottom = height_ratio / 2
                                left = right = width_ratio / 2
                            else:
                                # 默认4:3比例
                                width_ratio = (original_height * 1.33333 - original_width) / original_width
                                left = right = width_ratio / 2
                                top = bottom = 0

                            # 构建扩图请求
                            data = {
                                "messages": [{
                                    "content": {
                                        "text": "按新尺寸生成图片",
                                        "edit_image": {
                                            "edit_image_url": image_url,
                                            "edit_image_token": image_token,
                                            "ability": "outpainting",
                                            "description": description,
                                            "outline_id": None,
                                            "top": float(top),
                                            "bottom": float(bottom),
                                            "left": float(left),
                                            "right": float(right),
                                            "is_edit_local_image": False,
                                            "is_edit_local_image_v2": "false"
                                        }
                                    },
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
                                "local_message_id": str(uuid.uuid1())
                            }

                            # 发送扩图请求
                            result = self.api_client.send_request(data, "/samantha/chat/completion")
                            if result and "urls" in result:
                                # 存储扩图后的图片
                                new_img_id = str(int(time.time()))
                                operation_params = {
                                    "ratio": ratio,
                                    "conversation_id": conversation_id,
                                    "section_id": section_id,
                                    "reply_id": result.get("reply_id"),
                                    "original_img_id": img_id,
                                    "original_index": index,
                                    "image_token": result["urls"][0].split("/")[-1].split("~")[0],  # 保存新图片token
                                    "image_url": result["urls"][0],  # 保存新图片url
                                    "description": description,
                                    "data": result.get("data", []),  # 保存完整的图片数据
                                    "is_edit_local_image": False,
                                    "is_edit_local_image_v2": "false",
                                    "outpaint_params": {
                                        "top": float(top),
                                        "bottom": float(bottom),
                                        "left": float(left),
                                        "right": float(right)
                                    }
                                }

                                # 存储图片信息
                                image_info = {
                                    "urls": result["urls"],
                                    "type": "outpaint",
                                    "operation_params": operation_params,
                                    "parent_id": img_id,
                                    "create_time": int(time.time())
                                }

                                # 保存到数据库
                                self.image_storage.store_image(new_img_id, image_info)

                                # 发送扩图后的图片
                                e_context["reply"] = Reply(ReplyType.IMAGE_URL, result["urls"][0])

                                # 等待图片发送完成后再发送帮助信息
                                time.sleep(1)  # 等待图片发送完成
                                help_text = self._get_help_text(new_img_id, False)
                                e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
                            else:
                                e_context["reply"] = Reply(ReplyType.ERROR, "图片扩展失败")

                        except Exception as e:
                            logger.error(f"[Doubao] Error in outpaint_image: {e}")
                            e_context["reply"] = Reply(ReplyType.ERROR, "图片扩展失败")

                    elif cmd == "r":  # 重新生成命令
                        image_data = self.image_storage.get_image(img_id)
                        if not image_data:
                            e_context["reply"] = Reply(ReplyType.ERROR, "找不到对应的图片ID")
                            e_context.action = EventAction.BREAK_PASS
                            return

                        # 发送等待消息
                        e_context["channel"].send(Reply(ReplyType.INFO, "正在重新生成图片..."), e_context["context"])

                        # 获取最新的操作信息
                        operation_params = image_data.get("operation_params", {})
                        operation_type = image_data.get("type", "generate")

                        # 获取最新的reply_id
                        reply_id = operation_params.get("reply_id")
                        if not reply_id:
                            e_context["reply"] = Reply(ReplyType.ERROR, "缺少必要的会话信息，无法重新生成")
                            e_context.action = EventAction.BREAK_PASS
                            return

                        # 重新生成图片
                        success, new_img_id, urls, is_multi = self.regenerate_image(
                            image_data,
                            operation_params.get("conversation_id"),
                            operation_params.get("section_id")
                        )

                        if success and urls:
                            # 根据图片数量决定是否使用拼接
                            if len(urls) > 1:
                                # 多图模式，使用拼接
                                image_file = self.image_processor.combine_images(urls)
                                if image_file:
                                    try:
                                        image_reply = Reply(ReplyType.IMAGE, image_file)
                                        e_context["channel"].send(image_reply, e_context["context"])

                                        # 等待图片发送完成后再发送帮助信息
                                        time.sleep(1)  # 等待图片发送完成
                                        help_text = self._get_help_text(new_img_id, True)
                                        e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
                                    finally:
                                        image_file.close()
                            else:
                                # 单图模式，直接发送
                                e_context["reply"] = Reply(ReplyType.IMAGE_URL, urls[0])

                                # 等待图片发送完成后再发送帮助信息
                                time.sleep(1)  # 等待图片发送完成
                                help_text = self._get_help_text(new_img_id, False)
                                e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
                        else:
                            e_context["reply"] = Reply(ReplyType.ERROR, "图片重新生成失败")
                    else:
                        e_context["reply"] = Reply(ReplyType.ERROR, "未知的命令")

                except ValueError:
                    e_context["reply"] = Reply(ReplyType.ERROR, "图片序号必须是1-4之间的数字")
                except Exception as e:
                    logger.error(f"[Doubao] Error processing command: {e}")
                    e_context["reply"] = Reply(ReplyType.ERROR, "处理命令失败")

                e_context.action = EventAction.BREAK_PASS
                return

            # 处理豆包绘画命令
            if content.startswith(draw_command):
                # 处理新建会话命令
                if content == f"{draw_command}新建会话":
                    if self._create_new_conversation():
                        style_text = "、".join(self.styles) if self.styles else "暂无可用风格"
                        e_context["reply"] = Reply(ReplyType.INFO, f"已创建新的绘图会话\n支持的风格：{style_text}")
                    else:
                        e_context["reply"] = Reply(ReplyType.ERROR, "创建绘图会话失败")
                    e_context.action = EventAction.BREAK_PASS
                    return

                # 处理其他豆包命令
                if content.strip() == draw_command:
                    if self._create_new_conversation():
                        style_text = "、".join(self.styles) if self.styles else "暂无可用风格"
                        e_context["reply"] = Reply(ReplyType.INFO, f"已创建新的绘图会话\n支持的风格：{style_text}")
                    else:
                        e_context["reply"] = Reply(ReplyType.ERROR, "创建绘图会话失败")
                    e_context.action = EventAction.BREAK_PASS
                    return

                # 解析绘图命令参数
                prompt, style, ratio = self._parse_style_and_ratio(content, draw_command)

                try:
                    if not prompt:
                        e_context["reply"] = Reply(ReplyType.ERROR, "请在命令后输入绘画提示词")
                        e_context.action = EventAction.BREAK_PASS
                        return

                    if style and style not in self.styles:
                        e_context["reply"] = Reply(ReplyType.ERROR, f"不支持的风格: {style}")
                        e_context.action = EventAction.BREAK_PASS
                        return

                    # 构建完整的提示词
                    full_prompt = prompt
                    if style and style.strip():  # 确保style不为空且不只包含空格
                        full_prompt += f"，图风格为「{style}」"
                    if ratio and ratio.strip():  # 确保ratio不为空且不只包含空格
                        full_prompt += f"，比例「{ratio}」"

                    # 发送等待消息
                    e_context["channel"].send(Reply(ReplyType.INFO, "正在生成图片,请稍候..."), e_context["context"])

                    # 构建请求数据
                    local_message_id = str(uuid.uuid1())
                    local_conversation_id = f"local_{int(time.time()*1000)}"

                    # 构建图像生成请求
                    data = {
                        "messages": [{
                            "content": {
                                "text": full_prompt
                            },
                            "content_type": 2009,
                            "attachments": []
                        }],
                        "completion_option": {
                            "is_regen": False,
                            "with_suggest": False,
                            "need_create_conversation": not bool(self.conversation_id),
                            "launch_stage": 1,
                            "is_replace": False,
                            "is_delete": False,
                            "message_from": 0,
                            "event_id": "0"
                        },
                        "conversation_id": self.conversation_id if self.conversation_id else "0",
                        "section_id": self.section_id,
                        "local_message_id": local_message_id,
                        "local_conversation_id": local_conversation_id
                    }

                    # 生成图片
                    result = self.api_client.send_request(data, "/samantha/chat/completion")
                    if result and "urls" in result:
                        # 更新会话ID
                        if not self.conversation_id:
                            self.conversation_id = result.get("conversation_id")
                            self.section_id = result.get("section_id")
                            self.reply_id = result.get("reply_id")

                        # 提取支持的风格列表
                        if "meta" in result and "option_list" in result["meta"]:
                            for option in result["meta"]["option_list"]:
                                if option["value"] == "style":
                                    self.styles = [opt["value"] for opt in option["options"]]
                                    logger.info(f"[Doubao] Supported styles updated: {self.styles}")
                                    break

                        # 存储图片信息
                        img_id = str(int(time.time()))
                        operation_params = {
                            "prompt": prompt,
                            "style": style,
                            "ratio": ratio,
                            "conversation_id": self.conversation_id,
                            "section_id": self.section_id,
                            "reply_id": self.reply_id,
                            "data": result.get("data", []),  # 保存完整的图片数据
                            "image_token": result["urls"][0].split("/")[-1].split("~")[0],  # 保存第一张图片的token
                            "image_url": result["urls"][0]  # 保存第一张图片的url
                        }

                        # 存储图片信息
                        image_info = {
                            "urls": result["urls"],
                            "type": "generate",
                            "operation_params": operation_params,
                            "parent_id": None,
                            "create_time": int(time.time())
                        }

                        # 保存到数据库
                        self.image_storage.store_image(img_id, image_info)

                        # 发送图片
                        if len(result["urls"]) > 0:
                            image_file = self.image_processor.combine_images(result["urls"])
                            if image_file:
                                try:
                                    image_reply = Reply(ReplyType.IMAGE, image_file)
                                    e_context["channel"].send(image_reply, e_context["context"])

                                    # 等待图片发送完成后再发送帮助信息
                                    time.sleep(1)  # 等待图片发送完成
                                    # 根据图片数量决定是否显示序号选项
                                    is_multi = len(result["urls"]) > 1
                                    help_text = self._get_help_text(img_id, is_multi)
                                    e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
                                finally:
                                    image_file.close()
                            else:
                                e_context["reply"] = Reply(ReplyType.ERROR, "未获取到任何图片")
                        else:
                            e_context["reply"] = Reply(ReplyType.ERROR, "未获取到任何图片")

                        # 移除重复的帮助文本发送
                        # help_text = self._get_help_text(img_id, True)
                        # e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
                    else:
                        e_context["reply"] = Reply(ReplyType.ERROR, "图片生成失败")

                except Exception as e:
                    logger.error(f"[Doubao] Error generating image: {e}")
                    e_context["reply"] = Reply(ReplyType.ERROR, "图片生成失败")

                finally:
                    self.image_processor.cleanup_temp_files()

                e_context.action = EventAction.BREAK_PASS

    def _get_image_data(self, msg, content):
        """获取图片数据"""
        try:
            logger.info(f"[Doubao] 开始处理图片，content类型: {type(content)}")

            # 优先检查msg.content
            if hasattr(msg, 'content'):
                logger.info(f"[Doubao] 检查msg.content: {msg.content}")
                if os.path.isfile(msg.content):
                    logger.info(f"[Doubao] 找到图片文件: {msg.content}")
                    try:
                        with open(msg.content, 'rb') as f:
                            return base64.b64encode(f.read()).decode('utf-8')
                    except Exception as e:
                        logger.error(f"[Doubao] 读取msg.content文件失败: {e}")

            # 如果文件还未下载,先尝试下载
            if hasattr(msg, '_prepare_fn') and not msg._prepared:
                logger.info(f"[Doubao] 开始准备图片文件...")
                msg._prepare_fn()
                msg._prepared = True
                time.sleep(1)  # 等待文件准备完成

                # 下载完成后再次检查msg.content
                if hasattr(msg, 'content') and os.path.isfile(msg.content):
                    logger.info(f"[Doubao] 下载后找到图片文件: {msg.content}")
                    try:
                        with open(msg.content, 'rb') as f:
                            return base64.b64encode(f.read()).decode('utf-8')
                    except Exception as e:
                        logger.error(f"[Doubao] 读取下载的文件失败: {e}")

            # 获取当前工作目录
            cwd = os.getcwd()

            # 尝试的路径列表
            file_paths = [
                content,  # 原始路径
                os.path.abspath(content),  # 绝对路径
                os.path.join(cwd, content),  # 相对于当前目录的路径
                os.path.join(cwd, 'tmp', os.path.basename(content)),  # tmp目录
                os.path.join(cwd, 'plugins', 'doubao', 'tmp', os.path.basename(content)),  # 插件tmp目录
                os.path.join(cwd, 'plugins', 'doubao', 'storage', 'temp', os.path.basename(content)),  # 插件临时目录
            ]

            # 检查每个可能的路径
            for path in file_paths:
                if os.path.isfile(path):
                    logger.info(f"[Doubao] 在路径列表中找到图片文件: {path}")
                    try:
                        with open(path, 'rb') as f:
                            return base64.b64encode(f.read()).decode('utf-8')
                    except Exception as e:
                        logger.error(f"[Doubao] 读取文件失败 {path}: {e}")
                        continue

            # 如果是URL,尝试下载
            if isinstance(content, str) and (content.startswith('http://') or content.startswith('https://')):
                response = requests.get(content, timeout=30)
                if response.status_code == 200:
                    return base64.b64encode(response.content).decode('utf-8')

            # 如果是XML内容，尝试解析
            if isinstance(content, str) and '<?xml' in content:
                try:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(content)
                    img_element = root.find('.//img')
                    if img_element is not None:
                        logger.info("[Doubao] 成功解析XML，查找图片文件...")
                        # 再次检查msg.content
                        if hasattr(msg, 'content') and os.path.isfile(msg.content):
                            logger.info(f"[Doubao] XML解析后找到图片文件: {msg.content}")
                            try:
                                with open(msg.content, 'rb') as f:
                                    return base64.b64encode(f.read()).decode('utf-8')
                            except Exception as e:
                                logger.error(f"[Doubao] 读取XML相关文件失败: {e}")
                except Exception as e:
                    logger.error(f"[Doubao] 解析XML失败: {e}")

            logger.error(f"[Doubao] 未找到图片文件: {content}")
            return None

        except Exception as e:
            logger.error(f"[Doubao] 获取图片数据失败: {e}")
            return None

    def _process_image(self, image_data, msg, e_context):
        """处理参考图片的上传和编辑"""
        try:
            # 获取完整参数
            params = self.reference_prompts.get(msg.from_user_id)
            if not params:
                logger.error("[Doubao] 未找到编辑参数")
                e_context["reply"] = Reply(ReplyType.ERROR, "未找到编辑参数，请重新发送命令")
                return

            # 发送等待消息
            e_context["channel"].send(Reply(ReplyType.INFO, "正在处理图片..."), e_context["context"])

            # 将 base64 字符串转换为字节
            try:
                image_bytes = base64.b64decode(image_data)
            except Exception as e:
                logger.error(f"[Doubao] Base64解码失败: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片数据处理失败，请重试")
                return

            # 上传图片到豆包服务器
            result = self.image_uploader.upload_and_process_image(image_bytes)
            if not result or not result.get('success'):
                error_msg = result.get('error') if result else "未知错误"
                logger.error(f"[Doubao] 图片上传失败: {error_msg}")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片上传失败，请重试")
                return

            # 获取图片key和URL
            image_key = result.get('image_key')
            image_url = result.get('file_info', {}).get('main_url')
            if not image_key or not image_url:
                logger.error("[Doubao] 未获取到图片信息")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片处理失败，请重试")
                return

            # 获取提示词、风格和比例
            prompt = params.get("prompt", "")
            style = params.get("style")
            ratio = params.get("ratio")

            # 构建完整的提示词
            full_prompt = prompt
            if style and style.strip():
                full_prompt += f"，图风格为「{style}」"
            if ratio and ratio.strip():
                full_prompt += f"，比例「{ratio}」"

            # 构建参考图请求数据
            content = {
                "text": full_prompt,
                "edit_image": {
                    "edit_image_token": image_key,
                    "edit_image_url": image_url,
                    "description": "",
                    "is_edit_local_image": True,
                    "is_edit_local_image_v2": "true"
                }
            }

            data = {
                "messages": [{
                    "content": json.dumps(content, ensure_ascii=False),
                    "content_type": 2009,
                    "attachments": []
                }],
                "completion_option": {
                    "is_regen": False,
                    "with_suggest": False,
                    "need_create_conversation": not bool(self.conversation_id),
                    "launch_stage": 1,
                    "is_replace": False,
                    "is_delete": False,
                    "message_from": 0,
                    "event_id": "0"
                },
                "conversation_id": self.conversation_id if self.conversation_id else "0",
                "section_id": self.section_id,
                "local_message_id": str(uuid.uuid4()),
                "local_conversation_id": f"local_{int(time.time()*1000)}"
            }

            # 发送请求前记录完整请求参数
            logger.info(f"[Doubao] 完整请求参数:\n请求URL: /samantha/chat/completion\n请求体: {json.dumps(data, ensure_ascii=False, indent=2)}")

            # 保存请求参数到temp目录
            temp_dir = os.path.join(os.path.dirname(__file__), "temp")
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)
            request_log_path = os.path.join(temp_dir, "@request_debug.json")
            with open(request_log_path, "w", encoding="utf-8") as f:
                json.dump({
                    "url": "/samantha/chat/completion",
                    "headers": self.api_client.headers,
                    "params": self.api_client._get_params(),
                    "data": data
                }, f, ensure_ascii=False, indent=2)
            logger.info(f"[Doubao] 已保存请求参数到: {request_log_path}")

            # 发送参考图请求
            result = self.api_client.send_request(data, "/samantha/chat/completion")
            if result and "urls" in result:
                # 更新会话信息
                if result.get("conversation_id"):
                    self.conversation_id = result["conversation_id"]
                if result.get("section_id"):
                    self.section_id = result["section_id"]

                # 存储生成的图片
                img_id = str(int(time.time()))
                first_url = result["urls"][0]
                image_token = first_url.split("/")[-1].split("~")[0]

                operation_params = {
                    "prompt": full_prompt,
                    "style": style,
                    "ratio": ratio,
                    "conversation_id": self.conversation_id,
                    "section_id": self.section_id,
                    "reply_id": result.get("reply_id"),
                    "image_token": image_token,
                    "image_url": first_url,
                    "original_key": image_key,
                    "original_url": image_url,
                    "data": result.get("data", [])
                }

                # 存储图片信息
                image_info = {
                    "urls": result["urls"],
                    "type": "reference",
                    "operation_params": operation_params,
                    "parent_id": None,
                    "create_time": int(time.time())
                }

                # 保存到数据库
                self.image_storage.store_image(img_id, image_info)

                # 发送生成的图片
                if len(result["urls"]) > 1:
                    # 多图模式，使用拼接
                    image_file = self.image_processor.combine_images(result["urls"])
                    if image_file:
                        try:
                            image_reply = Reply(ReplyType.IMAGE, image_file)
                            e_context["channel"].send(image_reply, e_context["context"])

                            # 等待图片发送完成后再发送帮助信息
                            time.sleep(1)  # 等待图片发送完成
                            help_text = self._get_help_text(img_id, True)
                            e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
                        finally:
                            image_file.close()
                else:
                    # 单图模式，直接发送
                    e_context["channel"].send(Reply(ReplyType.IMAGE_URL, first_url), e_context["context"])
                    time.sleep(1)  # 等待图片发送完成
                    help_text = self._get_help_text(img_id, False)
                    e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
            else:
                logger.error(f"[Doubao] 参考图生成失败，API响应: {result}")
                e_context["reply"] = Reply(ReplyType.ERROR, "参考图生成失败，请重试")

        except Exception as e:
            logger.error(f"[Doubao] 处理参考图失败: {e}")
            e_context["reply"] = Reply(ReplyType.ERROR, "处理参考图失败，请重试")
            return

    def _process_koutu(self, image_data, msg, e_context):
        """处理抠图功能"""
        try:
            # 发送等待消息
            e_context["channel"].send(Reply(ReplyType.INFO, "正在处理图片..."), e_context["context"])

            # 将 base64 字符串转换为字节
            try:
                image_bytes = base64.b64decode(image_data)
            except Exception as e:
                logger.error(f"[Doubao] Base64解码失败: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片数据处理失败，请重试")
                return

            # 上传图片到豆包服务器
            result = self.image_uploader.upload_and_process_image(image_bytes)

            if not result or not result.get('success'):
                error_msg = result.get('error') if result else "未知错误"
                logger.error(f"[Doubao] 图片上传失败: {error_msg}")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片上传失败，请重试")
                return

            # 获取图片key
            image_key = result.get('image_key')
            if not image_key:
                logger.error("[Doubao] 未获取到图片key")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片处理失败，请重试")
                return

            # 获取图片URL和背景蒙版
            file_info = result.get('file_info', {})
            main_url = file_info.get('main_url')
            mask_url = file_info.get('mask_url')

            if not main_url or not mask_url:
                logger.error("[Doubao] 获取图片URL失败")
                e_context["reply"] = Reply(ReplyType.ERROR, "获取图片URL失败，请重试")
                return

            # 存储抠图结果
            img_id = str(int(time.time()))
            operation_params = {
                "image_key": image_key,
                "conversation_id": self.conversation_id,
                "section_id": self.section_id,
                "image_token": image_key.split("/")[-1].split(".")[0],
                "image_url": main_url,
                "original_url": main_url,
                "mask": result.get('mask', ''),
                "without_background": result.get('without_background', False),
                "mask_url": mask_url
            }

            # 存储图片信息
            image_info = {
                "urls": [main_url, mask_url],
                "type": "koutu",
                "operation_params": operation_params,
                "parent_id": None,
                "create_time": int(time.time())
            }

            # 保存到数据库
            try:
                self.image_storage.store_image(img_id, image_info)
            except Exception as e:
                logger.error(f"[Doubao] 保存图片信息失败: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, "保存图片信息失败，请重试")
                return

            # 发送抠图结果
            e_context["reply"] = Reply(ReplyType.IMAGE_URL, mask_url)

            # 发送原图链接信息
            e_context["channel"].send(Reply(ReplyType.TEXT, f"原图链接：{main_url}"), e_context["context"])

            # 等待图片发送完成后再发送帮助信息
            time.sleep(1)  # 等待图片发送完成
            help_text = self._get_help_text(img_id, False)
            e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])

        except Exception as e:
            logger.error(f"[Doubao] 处理抠图失败: {e}")
            e_context["reply"] = Reply(ReplyType.ERROR, "处理抠图失败，请重试")
            return

    def regenerate_image(self, image_data: dict, conversation_id: str, section_id: str):
        """重新生成图片
        Args:
            image_data: 图片数据
            conversation_id: 会话ID
            section_id: 会话分段ID
        Returns:
            tuple: (success, new_img_id, urls, is_multi_images)
        """
        try:
            # 获取最新的操作信息
            operation_params = image_data.get("operation_params", {})
            operation_type = image_data.get("type", "generate")

            # 优先从数据库中获取图片信息
            reply_id = operation_params.get("reply_id")
            prompt = operation_params.get("prompt", "")

            if not reply_id:
                logger.error("[Doubao] Missing reply_id for regeneration")
                return False, None, None, False

            # 构建重新生成请求的基础数据
            content = {
                "text": prompt or "按新尺寸生成图片"
            }

            # 根据不同的操作类型构建不同的请求内容
            if operation_type == "outpaint":
                # 扩图操作的重新生成
                content["edit_image"] = {
                    "edit_image_token": operation_params.get("image_token"),
                    "edit_image_url": operation_params.get("image_url"),
                    "outline_id": None,
                    "description": operation_params.get("description", ""),
                    "is_edit_local_image": False,
                    "is_edit_local_image_v2": "false"
                }
            elif operation_type == "edit":
                # 编辑操作的重新生成
                content["edit_image"] = {
                    "edit_image_token": operation_params.get("image_token"),
                    "edit_image_url": operation_params.get("image_url"),
                    "outline_id": None,
                    "description": operation_params.get("description", "")
                }

            # 构建请求数据
            data = {
                "messages": [{
                    "content": content,
                    "content_type": 2009,
                    "attachments": []
                }],
                "completion_option": {
                    "is_regen": True,
                    "with_suggest": True,
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

            # 发送重新生成请求
            result = self.api_client.send_request(data, "/samantha/chat/completion")
            if result and "urls" in result:
                # 存储图片信息
                new_img_id = str(int(time.time()))
                new_operation_params = operation_params.copy()
                new_operation_params.update({
                    "conversation_id": conversation_id,
                    "section_id": section_id,
                    "reply_id": result.get("reply_id"),
                    "prompt": prompt
                })

                # 获取新生成图片的token和url
                if result.get("urls") and len(result["urls"]) > 0:
                    first_url = result["urls"][0]
                    new_operation_params["image_token"] = first_url.split("/")[-1].split("~")[0]
                    new_operation_params["image_url"] = first_url

                # 构建图片信息
                image_info = {
                    "urls": result["urls"][:1] if operation_type in ["outpaint", "edit"] else result["urls"],  # 对于扩图和编辑操作只保留第一张图片
                    "type": operation_type,
                    "operation_params": new_operation_params,
                    "parent_id": image_data.get("id"),
                    "create_time": int(time.time())
                }

                # 保存到数据库
                self.image_storage.store_image(new_img_id, image_info)

                # 根据操作类型和返回的图片数量判断是否是多图模式
                # 只有初始生成和普通重新生成时才可能是多图模式
                is_multi_images = operation_type == "generate" and len(result["urls"]) > 1

                # 对于扩图和编辑操作，只返回第一张图片
                urls_to_return = result["urls"][:1] if operation_type in ["outpaint", "edit"] else result["urls"]

                return True, new_img_id, urls_to_return, is_multi_images
            return False, None, None, False

        except Exception as e:
            logger.error(f"[Doubao] Error in regenerate_image: {e}")
            return False, None, None, False

    def _store_image_info(self, img_id: str, urls: list, operation_type: str, operation_params: dict, parent_id: str = None):
        """存储图片信息到数据库
        Args:
            img_id: 图片ID
            urls: 图片URL列表
            operation_type: 操作类型(generate/edit/outpaint)
            operation_params: 操作参数
            parent_id: 父图片ID
        """
        try:
            # 更新会话信息
            if operation_params.get("conversation_id"):
                self.conversation_id = operation_params["conversation_id"]
            if operation_params.get("section_id"):
                self.section_id = operation_params["section_id"]
            if operation_params.get("reply_id"):
                self.reply_id = operation_params["reply_id"]

            # 获取第一张图片的token和url
            if urls and len(urls) > 0:
                first_url = urls[0]
                # 直接从URL中提取token
                image_token = first_url.split("/")[-1].split("~")[0]

                # 更新操作参数
                operation_params.update({
                    "image_token": image_token,
                    "image_url": first_url,
                    "conversation_id": self.conversation_id,
                    "section_id": self.section_id,
                    "reply_id": self.reply_id
                })

                # 从响应中提取图片尺寸信息
                if "data" in operation_params and isinstance(operation_params["data"], list) and len(operation_params["data"]) > 0:
                    image_data = operation_params["data"][0]
                    if isinstance(image_data, dict):
                        # 优先使用原始图片尺寸
                        if "image_raw" in image_data:
                            operation_params["width"] = image_data["image_raw"].get("width", 1024)
                            operation_params["height"] = image_data["image_raw"].get("height", 1024)
                        # 如果没有原始图片尺寸，使用其他尺寸信息
                        elif "image_ori" in image_data:
                            operation_params["width"] = image_data["image_ori"].get("width", 1024)
                            operation_params["height"] = image_data["image_ori"].get("height", 1024)

                        if "description" in image_data:
                            operation_params["description"] = image_data["description"]
                else:
                    # 如果响应中没有尺寸信息，使用默认尺寸
                    operation_params["width"] = 1024
                    operation_params["height"] = 1024

                # 对于编辑操作,保存原始图片信息
                if operation_type == "edit" and parent_id:
                    parent_image = self.image_storage.get_image(parent_id)
                    if parent_image:
                        parent_params = parent_image.get("operation_params", {})
                        operation_params["original_image_token"] = parent_params.get("image_token")
                        operation_params["original_image_url"] = parent_params.get("image_url")
                        operation_params["original_reply_id"] = parent_params.get("reply_id")
                        operation_params["original_description"] = parent_params.get("description")
                        # 继承父图片的尺寸信息
                        if parent_params.get("width") and parent_params.get("height"):
                            operation_params["original_width"] = parent_params["width"]
                            operation_params["original_height"] = parent_params["height"]
                            # 如果当前图片没有尺寸信息，使用父图片的尺寸
                            if not operation_params.get("width"):
                                operation_params["width"] = parent_params["width"]
                            if not operation_params.get("height"):
                                operation_params["height"] = parent_params["height"]

                # 对于扩图操作,保存扩图前的图片信息和扩图参数
                elif operation_type == "outpaint" and parent_id:
                    parent_image = self.image_storage.get_image(parent_id)
                    if parent_image:
                        parent_params = parent_image.get("operation_params", {})
                        operation_params["pre_outpaint_image_token"] = parent_params.get("image_token")
                        operation_params["pre_outpaint_image_url"] = parent_params.get("image_url")
                        operation_params["pre_outpaint_reply_id"] = parent_params.get("reply_id")
                        operation_params["pre_outpaint_description"] = parent_params.get("description")
                        # 继承父图片的尺寸信息
                        if parent_params.get("width") and parent_params.get("height"):
                            operation_params["pre_outpaint_width"] = parent_params["width"]
                            operation_params["pre_outpaint_height"] = parent_params["height"]
                            # 如果当前图片没有尺寸信息，使用父图片的尺寸
                            if not operation_params.get("width"):
                                operation_params["width"] = parent_params["width"]
                            if not operation_params.get("height"):
                                operation_params["height"] = parent_params["height"]

            # 存储图片信息
            image_info = {
                "urls": urls,
                "type": operation_type,
                "operation_params": operation_params,
                "parent_id": parent_id,
                "create_time": int(time.time())
            }

            # 保存到数据库
            self.image_storage.store_image(img_id, image_info)

            logger.debug(f"[Doubao] Stored image info: {image_info}")

        except Exception as e:
            logger.error(f"[Doubao] Error storing image info: {e}")
            raise e

    def _init_conversation_from_storage(self):
        '''从数据库初始化会话信息'''
        try:
            # 从数据库获取最新的一条记录
            latest_image = self.image_storage.get_latest_image()
            if latest_image:
                params = latest_image.get("operation_params", {})
                self.conversation_id = params.get("conversation_id")
                self.section_id = params.get("section_id")
                self.reply_id = params.get("reply_id")
                logger.info(f"[Doubao] 已从历史记录恢复会话信息: conversation_id={self.conversation_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"[Doubao] 从数据库恢复会话信息失败: {e}")
            return False

    def _get_help_text(self, img_id, is_multi=False):
        """获取帮助文本
        Args:
            img_id: 图片ID
            is_multi: 是否是多图模式
        Returns:
            str: 帮助文本
        """
        help_text = (
            f"图片ID: {img_id}\n"
            "操作指令:\n"
        )

        if is_multi:
            help_text += (
                f"放大: $u {img_id} 序号(1-4)\n"
                f"编辑: $v {img_id} 序号(1-4) 编辑提示词\n"
                f"扩图: $k {img_id} 序号(1-4) 比例(1:1/4:3/16:9/9:16/max)\n"
            )
        else:
            help_text += (
                f"编辑: $v {img_id} 编辑提示词\n"
                f"扩图: $k {img_id} 比例(1:1/4:3/16:9/9:16/max)\n"
            )

        help_text += f"重新生成: $r {img_id}"
        return help_text

    def _process_inpaint(self, original_image_data, mask_image_data, prompt, msg, e_context):
        """处理区域重绘
        Args:
            original_image_data: base64编码的原图数据
            mask_image_data: base64编码的标记图片数据
            prompt: 重绘描述词
            msg: 消息对象
            e_context: 事件上下文
        """
        try:
            # 发送等待消息
            e_context["channel"].send(Reply(ReplyType.INFO, "正在处理图片..."), e_context["context"])

            # 将base64数据转换为字节
            try:
                original_image_bytes = base64.b64decode(original_image_data)
                mask_image_bytes = base64.b64decode(mask_image_data)
            except Exception as e:
                logger.error(f"[Doubao] Base64解码失败: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片数据处理失败，请重试")
                return

            # 上传原图到服务器
            result = self.image_uploader.upload_and_process_image(original_image_bytes)
            if not result or not result.get('success'):
                error_msg = result.get('error') if result else "未知错误"
                logger.error(f"[Doubao] 图片上传失败: {error_msg}")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片上传失败，请重试")
                return

            # 获取图片key和URL
            image_key = result.get('image_key')
            image_url = result.get('file_info', {}).get('main_url')
            if not image_key or not image_url:
                logger.error("[Doubao] 未获取到图片信息")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片处理失败，请重试")
                return

            # 获取用户的重绘参数
            inpaint_params = self.inpaint_prompts.get(msg.from_user_id, {})
            mode = inpaint_params.get("mode", "circle")  # 默认使用圈选模式
            is_invert = inpaint_params.get("is_invert", False)  # 获取是否反选

            try:
                # 根据模式选择蒙版生成方法
                if mode == "circle":
                    mask_base64 = self.image_processor.create_mask_from_circle_selection(
                        original_image_bytes,
                        mask_image_bytes,
                        invert=is_invert
                    )
                else:  # brush mode
                    mask_base64 = self.image_processor.create_mask_from_marked_image(
                        original_image_bytes,
                        mask_image_bytes,
                        invert=is_invert
                    )

                if not mask_base64:
                    logger.error("[Doubao] 创建蒙版失败")
                    e_context["reply"] = Reply(ReplyType.ERROR, "创建蒙版失败，请重试")
                    return

                # 构建重绘请求数据
                content = {
                    "text": prompt,
                    "edit_image": {
                        "edit_image_token": image_key,
                        "edit_image_url": image_url,
                        "description": "",
                        "ability": "inpainting",
                        "mask": mask_base64,
                        "is_edit_local_image": True,
                        "is_edit_local_image_v2": "true"
                    }
                }

                data = {
                    "messages": [{
                        "content": json.dumps(content, ensure_ascii=False),
                        "content_type": 2009,
                        "attachments": []
                    }],
                    "completion_option": {
                        "is_regen": False,
                        "with_suggest": False,
                        "need_create_conversation": not bool(self.conversation_id),
                        "launch_stage": 1,
                        "is_replace": False,
                        "is_delete": False,
                        "message_from": 0,
                        "event_id": "0"
                    },
                    "conversation_id": self.conversation_id if self.conversation_id else "0",
                    "section_id": self.section_id,
                    "local_message_id": str(uuid.uuid4()),
                    "local_conversation_id": f"local_{int(time.time()*1000)}"
                }

                # 发送请求前记录完整请求参数
                logger.info(f"[Doubao] 完整请求参数:\n请求URL: /samantha/chat/completion\n请求体: {json.dumps(data, ensure_ascii=False, indent=2)}")

                # 保存请求参数到temp目录
                temp_dir = os.path.join(os.path.dirname(__file__), "temp")
                if not os.path.exists(temp_dir):
                    os.makedirs(temp_dir)
                request_log_path = os.path.join(temp_dir, "@request_debug.json")
                with open(request_log_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "url": "/samantha/chat/completion",
                        "headers": self.api_client.headers,
                        "params": self.api_client._get_params(),
                        "data": data
                    }, f, ensure_ascii=False, indent=2)
                logger.info(f"[Doubao] 已保存请求参数到: {request_log_path}")

                # 发送重绘请求
                result = self.api_client.send_request(data, "/samantha/chat/completion")
                if result and "urls" in result:
                    # 更新会话信息
                    if result.get("conversation_id"):
                        self.conversation_id = result["conversation_id"]
                    if result.get("section_id"):
                        self.section_id = result["section_id"]

                    # 存储重绘后的图片
                    img_id = str(int(time.time()))
                    first_url = result["urls"][0]
                    image_token = first_url.split("/")[-1].split("~")[0]

                    operation_params = {
                        "prompt": prompt,
                        "conversation_id": self.conversation_id,
                        "section_id": self.section_id,
                        "reply_id": result.get("reply_id"),
                        "image_token": image_token,
                        "image_url": first_url,
                        "original_key": image_key,
                        "original_url": image_url,
                        "mask": mask_base64,
                        "mode": mode,  # 记录使用的模式
                        "is_invert": is_invert,  # 记录是否使用反选
                        "data": result.get("data", [])
                    }

                    # 存储图片信息
                    image_info = {
                        "urls": result["urls"],
                        "type": "inpaint",
                        "operation_params": operation_params,
                        "parent_id": None,
                        "create_time": int(time.time())
                    }

                    # 保存到数据库
                    self.image_storage.store_image(img_id, image_info)

                    # 发送重绘后的图片
                    e_context["channel"].send(Reply(ReplyType.IMAGE_URL, first_url), e_context["context"])
                    time.sleep(1)  # 等待图片发送完成
                    help_text = self._get_help_text(img_id, False)
                    e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
                else:
                    logger.error(f"[Doubao] 区域重绘失败，API响应: {result}")
                    e_context["reply"] = Reply(ReplyType.ERROR, "区域重绘失败，请重试")

            except Exception as e:
                logger.error(f"[Doubao] 处理失败: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, f"处理失败: {str(e)}")
                return

        except Exception as e:
            logger.error(f"[Doubao] 处理区域重绘失败: {e}")
            e_context["reply"] = Reply(ReplyType.ERROR, "处理区域重绘失败，请重试")
            return

    def _process_change_background(self, image_data, msg, e_context):
        """处理换背景功能"""
        try:
            # 发送等待消息
            e_context["channel"].send(Reply(ReplyType.INFO, "正在处理图片..."), e_context["context"])

            # 将 base64 字符串转换为字节
            try:
                image_bytes = base64.b64decode(image_data)
            except Exception as e:
                logger.error(f"[Doubao] Base64解码失败: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片数据处理失败，请重试")
                return

            # 上传图片到豆包服务器
            result = self.image_uploader.upload_and_process_image(image_bytes)
            if not result or not result.get('success'):
                error_msg = result.get('error') if result else "未知错误"
                logger.error(f"[Doubao] 图片上传失败: {error_msg}")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片上传失败，请重试")
                return

            # 获取图片key和URL
            image_key = result.get('image_key')
            image_url = result.get('file_info', {}).get('main_url')
            if not image_key or not image_url:
                logger.error("[Doubao] 未获取到图片信息")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片处理失败，请重试")
                return

            # 获取主体蒙版
            mask_base64 = result.get('mask')
            if not mask_base64:
                logger.error("[Doubao] 获取主体蒙版失败")
                e_context["reply"] = Reply(ReplyType.ERROR, "获取主体蒙版失败，请重试")
                return

            # 获取背景描述词
            params = self.reference_prompts.get(msg.from_user_id, {})
            background_prompt = params.get("prompt")
            if not background_prompt:
                logger.error("[Doubao] 未找到背景描述词")
                e_context["reply"] = Reply(ReplyType.ERROR, "未找到背景描述词，请重试")
                return

            try:
                # 处理蒙版数据
                if "base64," in mask_base64:
                    mask_base64 = mask_base64.split("base64,")[-1]
                mask_base64 = ''.join(mask_base64.split())
                mask_base64 = mask_base64.rstrip('\\')
                padding = len(mask_base64) % 4
                if padding:
                    mask_base64 += '=' * (4 - padding)

                # 验证并处理蒙版
                try:
                    mask_bytes = base64.b64decode(mask_base64)
                    mask_array = np.frombuffer(mask_bytes, dtype=np.uint8)
                    mask_image = cv2.imdecode(mask_array, cv2.IMREAD_GRAYSCALE)
                    mask_image = cv2.bitwise_not(mask_image)
                    _, buffer = cv2.imencode('.png', mask_image)
                    mask_base64 = base64.b64encode(buffer).decode('utf-8')
                except Exception as e:
                    logger.error(f"[Doubao] 蒙版处理失败: {e}")
                    raise Exception("蒙版处理失败")

                # 构建换背景请求数据
                content = {
                    "text": background_prompt,
                    "edit_image": {
                        "edit_image_token": image_key,
                        "edit_image_url": image_url,
                        "description": "",
                        "ability": "inpainting",
                        "mask": f"data:image/png;base64,{mask_base64}",
                        "is_edit_local_image": True,
                        "is_edit_local_image_v2": "true"
                    }
                }

                data = {
                    "messages": [{
                        "content": json.dumps(content, ensure_ascii=False),
                        "content_type": 2009,
                        "attachments": []
                    }],
                    "completion_option": {
                        "is_regen": False,
                        "with_suggest": False,
                        "need_create_conversation": not bool(self.conversation_id),
                        "launch_stage": 1,
                        "is_replace": False,
                        "is_delete": False,
                        "message_from": 0,
                        "event_id": "0"
                    },
                    "conversation_id": self.conversation_id if self.conversation_id else "0",
                    "section_id": self.section_id,
                    "local_message_id": str(uuid.uuid4()),
                    "local_conversation_id": f"local_{int(time.time()*1000)}"
                }

                # 发送请求前记录完整请求参数
                logger.info(f"[Doubao] 完整请求参数:\n请求URL: /samantha/chat/completion\n请求体: {json.dumps(data, ensure_ascii=False, indent=2)}")

                # 保存请求参数到temp目录
                temp_dir = os.path.join(os.path.dirname(__file__), "temp")
                if not os.path.exists(temp_dir):
                    os.makedirs(temp_dir)
                request_log_path = os.path.join(temp_dir, "@request_debug.json")
                with open(request_log_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "url": "/samantha/chat/completion",
                        "headers": self.api_client.headers,
                        "params": self.api_client._get_params(),
                        "data": data
                    }, f, ensure_ascii=False, indent=2)
                logger.info(f"[Doubao] 已保存请求参数到: {request_log_path}")

                # 发送换背景请求
                result = self.api_client.send_request(data, "/samantha/chat/completion")
                if result and "urls" in result:
                    # 更新会话信息
                    if result.get("conversation_id"):
                        self.conversation_id = result["conversation_id"]
                    if result.get("section_id"):
                        self.section_id = result["section_id"]

                    # 存储换背景后的图片
                    img_id = str(int(time.time()))
                    first_url = result["urls"][0]
                    image_token = first_url.split("/")[-1].split("~")[0]

                    operation_params = {
                        "prompt": background_prompt,
                        "conversation_id": self.conversation_id,
                        "section_id": self.section_id,
                        "reply_id": result.get("reply_id"),
                        "image_token": image_token,
                        "image_url": first_url,
                        "original_key": image_key,
                        "original_url": image_url,
                        "mask": mask_base64,
                        "data": result.get("data", [])
                    }

                    # 存储图片信息
                    image_info = {
                        "urls": result["urls"],
                        "type": "change_background",
                        "operation_params": operation_params,
                        "parent_id": None,
                        "create_time": int(time.time())
                    }

                    # 保存到数据库
                    self.image_storage.store_image(img_id, image_info)

                    # 发送换背景后的图片
                    e_context["channel"].send(Reply(ReplyType.IMAGE_URL, first_url), e_context["context"])
                    time.sleep(1)  # 等待图片发送完成
                    help_text = self._get_help_text(img_id, False)
                    e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
                else:
                    logger.error(f"[Doubao] 换背景失败，API响应: {result}")
                    e_context["reply"] = Reply(ReplyType.ERROR, "换背景失败，请重试")

            except Exception as e:
                logger.error(f"[Doubao] 处理失败: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, f"处理失败: {str(e)}")
                return

        except Exception as e:
            logger.error(f"[Doubao] 处理换背景失败: {e}")
            e_context["reply"] = Reply(ReplyType.ERROR, "处理换背景失败，请重试")
            return

    def _process_change_subject(self, image_data, msg, e_context):
        """处理换主体功能"""
        try:
            # 发送等待消息
            e_context["channel"].send(Reply(ReplyType.INFO, "正在处理图片..."), e_context["context"])

            # 将 base64 字符串转换为字节
            try:
                image_bytes = base64.b64decode(image_data)
            except Exception as e:
                logger.error(f"[Doubao] Base64解码失败: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片数据处理失败，请重试")
                return

            # 上传图片到豆包服务器
            result = self.image_uploader.upload_and_process_image(image_bytes)
            if not result or not result.get('success'):
                error_msg = result.get('error') if result else "未知错误"
                logger.error(f"[Doubao] 图片上传失败: {error_msg}")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片上传失败，请重试")
                return

            # 获取图片key和URL
            image_key = result.get('image_key')
            image_url = result.get('file_info', {}).get('main_url')
            if not image_key or not image_url:
                logger.error("[Doubao] 未获取到图片信息")
                e_context["reply"] = Reply(ReplyType.ERROR, "图片处理失败，请重试")
                return

            # 获取主体蒙版
            mask_base64 = result.get('mask')
            if not mask_base64:
                logger.error("[Doubao] 获取主体蒙版失败")
                e_context["reply"] = Reply(ReplyType.ERROR, "获取主体蒙版失败，请重试")
                return

            # 获取主体描述词
            params = self.reference_prompts.get(msg.from_user_id, {})
            subject_prompt = params.get("prompt")
            if not subject_prompt:
                logger.error("[Doubao] 未找到主体描述词")
                e_context["reply"] = Reply(ReplyType.ERROR, "未找到主体描述词，请重试")
                return

            try:
                # 处理蒙版数据
                if "base64," in mask_base64:
                    mask_base64 = mask_base64.split("base64,")[-1]
                mask_base64 = ''.join(mask_base64.split())
                mask_base64 = mask_base64.rstrip('\\')
                padding = len(mask_base64) % 4
                if padding:
                    mask_base64 += '=' * (4 - padding)

                # 验证并处理蒙版 - 注意这里不需要反转蒙版，因为我们要替换主体
                try:
                    mask_bytes = base64.b64decode(mask_base64)
                    mask_array = np.frombuffer(mask_bytes, dtype=np.uint8)
                    mask_image = cv2.imdecode(mask_array, cv2.IMREAD_GRAYSCALE)
                    # 不需要反转蒙版，因为我们要替换主体区域
                    _, buffer = cv2.imencode('.png', mask_image)
                    mask_base64 = base64.b64encode(buffer).decode('utf-8')
                except Exception as e:
                    logger.error(f"[Doubao] 蒙版处理失败: {e}")
                    raise Exception("蒙版处理失败")

                # 构建换主体请求数据
                content = {
                    "text": subject_prompt,
                    "edit_image": {
                        "edit_image_token": image_key,
                        "edit_image_url": image_url,
                        "description": "",
                        "ability": "inpainting",
                        "mask": f"data:image/png;base64,{mask_base64}",
                        "is_edit_local_image": True,
                        "is_edit_local_image_v2": "true"
                    }
                }

                data = {
                    "messages": [{
                        "content": json.dumps(content, ensure_ascii=False),
                        "content_type": 2009,
                        "attachments": []
                    }],
                    "completion_option": {
                        "is_regen": False,
                        "with_suggest": False,
                        "need_create_conversation": not bool(self.conversation_id),
                        "launch_stage": 1,
                        "is_replace": False,
                        "is_delete": False,
                        "message_from": 0,
                        "event_id": "0"
                    },
                    "conversation_id": self.conversation_id if self.conversation_id else "0",
                    "section_id": self.section_id,
                    "local_message_id": str(uuid.uuid4()),
                    "local_conversation_id": f"local_{int(time.time()*1000)}"
                }

                # 发送请求前记录完整请求参数
                logger.info(f"[Doubao] 完整请求参数:\n请求URL: /samantha/chat/completion\n请求体: {json.dumps(data, ensure_ascii=False, indent=2)}")

                # 保存请求参数到temp目录
                temp_dir = os.path.join(os.path.dirname(__file__), "temp")
                if not os.path.exists(temp_dir):
                    os.makedirs(temp_dir)
                request_log_path = os.path.join(temp_dir, "@request_debug.json")
                with open(request_log_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "url": "/samantha/chat/completion",
                        "headers": self.api_client.headers,
                        "params": self.api_client._get_params(),
                        "data": data
                    }, f, ensure_ascii=False, indent=2)
                logger.info(f"[Doubao] 已保存请求参数到: {request_log_path}")

                # 发送换主体请求
                result = self.api_client.send_request(data, "/samantha/chat/completion")
                if result and "urls" in result:
                    # 更新会话信息
                    if result.get("conversation_id"):
                        self.conversation_id = result["conversation_id"]
                    if result.get("section_id"):
                        self.section_id = result["section_id"]

                    # 存储换主体后的图片
                    img_id = str(int(time.time()))
                    first_url = result["urls"][0]
                    image_token = first_url.split("/")[-1].split("~")[0]

                    operation_params = {
                        "prompt": subject_prompt,
                        "conversation_id": self.conversation_id,
                        "section_id": self.section_id,
                        "reply_id": result.get("reply_id"),
                        "image_token": image_token,
                        "image_url": first_url,
                        "original_key": image_key,
                        "original_url": image_url,
                        "mask": mask_base64,
                        "data": result.get("data", [])
                    }

                    # 存储图片信息
                    image_info = {
                        "urls": result["urls"],
                        "type": "change_subject",
                        "operation_params": operation_params,
                        "parent_id": None,
                        "create_time": int(time.time())
                    }

                    # 保存到数据库
                    self.image_storage.store_image(img_id, image_info)

                    # 发送换主体后的图片
                    e_context["channel"].send(Reply(ReplyType.IMAGE_URL, first_url), e_context["context"])
                    time.sleep(1)  # 等待图片发送完成
                    help_text = self._get_help_text(img_id, False)
                    e_context["channel"].send(Reply(ReplyType.INFO, help_text), e_context["context"])
                else:
                    logger.error(f"[Doubao] 换主体失败，API响应: {result}")
                    e_context["reply"] = Reply(ReplyType.ERROR, "换主体失败，请重试")

            except Exception as e:
                logger.error(f"[Doubao] 处理失败: {e}")
                e_context["reply"] = Reply(ReplyType.ERROR, f"处理失败: {str(e)}")
                return

        except Exception as e:
            logger.error(f"[Doubao] 处理换主体失败: {e}")
            e_context["reply"] = Reply(ReplyType.ERROR, "处理换主体失败，请重试")
            return

    def _start_heartbeat_thread(self):
        """启动心跳线程"""
        def heartbeat_worker():
            while not self._stop_heartbeat:
                try:
                    # 调用心跳检查
                    if hasattr(self, 'api_client'):
                        self.token_manager.check_heartbeat(self.api_client)
                        # 同时检查是否需要刷新会话
                        self.token_manager.check_session_refresh(self.api_client)
                except Exception as e:
                    logger.error(f"[Doubao] 心跳线程异常: {e}")
                # 每60秒检查一次是否需要发送心跳
                time.sleep(60)

        self._heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True)
        self._heartbeat_thread.start()

    def on_disable(self):
        """插件禁用时停止心跳线程"""
        self._stop_heartbeat = True
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)